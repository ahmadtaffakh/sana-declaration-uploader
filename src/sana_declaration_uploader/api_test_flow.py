"""Sequential SetPetitionData pilot kept separate from normal upload flow."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Event
from typing import Mapping
import unicodedata
import json
import re

from .attachment_api import (
    AttachmentApiError,
    AttachmentClient,
    AttachmentType,
    save_attachment_console_text,
)
from .live_browser import (
    LiveBrowserError,
    LoginCredentials,
    ProgressCallback,
    _report,
    _run_logged_stage,
    _validate_live_draft,
    login_self_service,
)
from .lookups import LookupResolutionError, resolve_captured_jurisdiction_fields
from .models import DeclarationDraft, UploadResult
from .petition_api import (
    SetPetitionDataResult,
    build_setpetitiondata_payload,
    save_setpetitiondata_debug_text,
    submit_one_petition_data,
)
from .results import (
    RESULTS_FILENAME,
    ResultPersistenceError,
    migrate_legacy_registration,
    read_legacy_tracking_codes,
    read_saved_attachment_context,
    save_attachment_count,
    save_verified_result,
)


GET_PETITION_BY_TRACKING_CODE_URL = (
    "https://person.adliran.ir/Offices/GetPetitionWidthPetitionType"
)
GET_PETITION_DOCUMENTS_URL = (
    "https://person.adliran.ir/Offices/GetJssPetitonDocument"
)
GET_PORTAL_DATETIME_URL = "https://person.adliran.ir/Offices/GetDateTime"
GET_PETITION_LIST_URL = (
    "https://person.adliran.ir/Offices/GetPetitionListOnReport"
)
READY_FOR_PAYMENT_URL = (
    "https://person.adliran.ir/Offices/SetPetitionToReadyForPaymentState"
)
GET_PERSON_SIGNATURE_URL = "https://person.adliran.ir/Offices/GetPersonDataSign"
StatusCallback = Callable[[dict[str, object]], None]


def _report_status(
    callback: StatusCallback | None,
    draft: DeclarationDraft,
    stage: str,
    state: str,
    *,
    tracking_code: str = "",
    message: str = "",
    completed: int = 0,
    total: int = 0,
    attachment_count: int | None = None,
    attachment_count_checked_at: str = "",
) -> None:
    if callback is None:
        return
    callback({
        "source_path": str(draft.source_path.resolve()) if draft.source_path else "",
        "source_recipient_ordinal": draft.source_recipient_ordinal,
        "stage": stage,
        "state": state,
        "tracking_code": tracking_code,
        "message": message,
        "completed": completed,
        "total": total,
        "attachment_count": attachment_count,
        "attachment_count_checked_at": attachment_count_checked_at,
    })


def run_live_setpetitiondata_batch(
    drafts: tuple[DeclarationDraft, ...],
    credentials: LoginCredentials,
    confirm: Callable[[], bool],
    petition_template: Mapping[str, object],
    request_text_path: Path,
    *,
    lookup_dir: Path,
    headless: bool = False,
    timeout_ms: int = 60_000,
    progress: ProgressCallback | None = None,
    status: StatusCallback | None = None,
    on_result: Callable[[DeclarationDraft, SetPetitionDataResult], None] | None = None,
    pending_requests: Queue | None = None,
    reauth_credentials_provider: Callable[
        [LoginCredentials], LoginCredentials | None
    ] | None = None,
    close_browser_event: Event,
    attachment_only: tuple[tuple[DeclarationDraft, str], ...] = (),
    legacy_registration: tuple[DeclarationDraft, ...] = (),
    legacy_attachment_only: tuple[DeclarationDraft, ...] = (),
    count_refresh: tuple[tuple[DeclarationDraft, str], ...] = (),
    petition_reviews: tuple[str, ...] = (),
    on_petition_review: Callable[
        [str, Mapping[str, object]], None
    ] | None = None,
    recent_registration_codes: tuple[str, ...] = (),
    recent_registration_months: int | None = None,
    on_recent_registration_refresh: Callable[
        [Mapping[str, object]], None
    ] | None = None,
    complete_after_attachments: bool = False,
) -> tuple[SetPetitionDataResult, ...] | None:
    """Submit rows sequentially; advance only after a verified tracking code."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LiveBrowserError(
            "playwright_not_installed: install the project with [browser]"
        ) from exc

    if not (
        drafts or attachment_only or legacy_registration
        or legacy_attachment_only or count_refresh or petition_reviews
        or recent_registration_codes or recent_registration_months is not None
    ):
        raise LiveBrowserError("set_petition_data_recipients_missing")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="chrome",
            headless=False,
            # proxy={"server": "http://127.0.0.1:8080"},
        )
        context = browser.new_context(
            ignore_https_errors=True,
        )
        page = None
        try:
            page = context.new_page()
            _run_logged_stage(
                progress,
                "ورود برای آزمایش API",
                lambda: login_self_service(
                    page,
                    credentials.as_portal_fields(),
                    timeout_ms=timeout_ms,
                ),
            )
            if not confirm():
                return None
            current_credentials = [credentials]
            ensure_authenticated = _authentication_guard(
                page,
                current_credentials,
                reauth_credentials_provider,
                timeout_ms,
                progress,
            )
            _matched_registration, safe_legacy_registration = (
                _reconcile_legacy_registrations(
                    page, legacy_registration, ensure_authenticated, progress,
                    status,
                )
                if legacy_registration else ((), ())
            )
            registration_drafts = drafts + safe_legacy_registration
            petition_payloads = (
                _prepare_petition_payloads(
                    registration_drafts, petition_template, request_text_path,
                    lookup_dir, progress, status,
                )
                if registration_drafts else []
            )
            results = (
                _submit_petition_payloads(
                    page, registration_drafts, petition_payloads,
                    ensure_authenticated,
                    progress, status, on_result, request_text_path,
                    current_credentials[0] if complete_after_attachments else None,
                )
                if registration_drafts else ()
            )
            reconciled_attachment, _safe_unused = (
                _reconcile_legacy_registrations(
                    page, legacy_attachment_only, ensure_authenticated,
                    progress, status,
                )
                if legacy_attachment_only else ((), ())
            )
            attachment_requests = attachment_only + reconciled_attachment
            if attachment_requests:
                _attach_existing_petitions(
                    page, attachment_requests, ensure_authenticated, progress,
                    request_text_path, status,
                    current_credentials[0] if complete_after_attachments else None,
                )
            if count_refresh:
                _refresh_attachment_counts(
                    page, count_refresh, ensure_authenticated, progress, status,
                )
            if petition_reviews:
                _review_petitions(
                    page, petition_reviews, ensure_authenticated, progress,
                    on_petition_review,
                )
            if recent_registration_months is not None:
                _refresh_recent_registrations(
                    page, recent_registration_codes, ensure_authenticated,
                    progress, on_recent_registration_refresh,
                    months=recent_registration_months,
                )
            all_results = list(results)
            while pending_requests is not None and not close_browser_event.is_set():
                _report(progress, "__api_browser_ready_to_close__")
                queued = None
                while queued is None and not close_browser_event.is_set():
                    try:
                        queued = pending_requests.get(timeout=0.25)
                    except Empty:
                        continue
                if queued is None:
                    break
                if isinstance(queued, dict) and queued.get("kind") == "refresh_counts":
                    current_credentials[0] = queued["credentials"]
                    _refresh_attachment_counts(
                        page, queued["requests"], ensure_authenticated,
                        progress, status,
                    )
                    continue
                if isinstance(queued, dict) and queued.get("kind") == "review_petition":
                    current_credentials[0] = queued["credentials"]
                    _review_petitions(
                        page, queued["tracking_codes"], ensure_authenticated,
                        progress,
                        queued.get("on_petition_review", on_petition_review),
                    )
                    continue
                if (
                    isinstance(queued, dict)
                    and queued.get("kind") == "refresh_recent_registrations"
                ):
                    current_credentials[0] = queued["credentials"]
                    _refresh_recent_registrations(
                        page, queued["tracking_codes"], ensure_authenticated,
                        progress,
                        queued.get(
                            "on_recent_registration_refresh",
                            on_recent_registration_refresh,
                        ),
                        months=int(queued["months"]),
                    )
                    continue
                if isinstance(queued, dict) and queued.get("kind") == "attach_existing":
                    current_credentials[0] = queued["credentials"]
                    reconciled, _safe_unused = _reconcile_legacy_registrations(
                        page,
                        queued.get("legacy_drafts", ()),
                        ensure_authenticated,
                        progress,
                        status,
                    )
                    _attach_existing_petitions(
                        page, queued["requests"] + reconciled,
                        ensure_authenticated, progress,
                        request_text_path, status,
                        current_credentials[0] if complete_after_attachments else None,
                    )
                    continue
                queued_drafts, queued_credentials, queued_template, queued_path, \
                    queued_lookup_dir = queued[:5]
                queued_legacy = queued[5] if len(queued) > 5 else ()
                current_credentials[0] = queued_credentials
                _matched, safe_queued_legacy = _reconcile_legacy_registrations(
                    page, queued_legacy, ensure_authenticated, progress, status,
                )
                queued_drafts = queued_drafts + safe_queued_legacy
                if not queued_drafts:
                    continue
                queued_payloads = _prepare_petition_payloads(
                    queued_drafts, queued_template, queued_path,
                    queued_lookup_dir, progress, status,
                )
                queued_results = _submit_petition_payloads(
                    page, queued_drafts, queued_payloads, ensure_authenticated,
                    progress, status, on_result, queued_path,
                    current_credentials[0] if complete_after_attachments else None,
                )
                all_results.extend(queued_results)
            return tuple(all_results)
        except Exception:
            if page is not None and pending_requests is not None:
                _report(progress, "__api_browser_failed__")
                close_browser_event.wait()
            raise
        finally:
            try:
                if page is not None:
                    if pending_requests is None:
                        _report(progress, "__api_browser_ready_to_close__")
                        close_browser_event.wait()
            finally:
                context.close()
                browser.close()


def run_live_setpetitiondata_test(
    draft: DeclarationDraft,
    credentials: LoginCredentials,
    confirm: Callable[[], bool],
    petition_template: Mapping[str, object],
    request_text_path: Path,
    *,
    lookup_dir: Path,
    headless: bool = False,
    timeout_ms: int = 60_000,
    progress: ProgressCallback | None = None,
    close_browser_event: Event,
) -> SetPetitionDataResult | None:
    """Backward-compatible one-row wrapper for the sequential API flow."""

    results = run_live_setpetitiondata_batch(
        (draft,), credentials, confirm, petition_template, request_text_path,
        lookup_dir=lookup_dir,
        headless=headless,
        timeout_ms=timeout_ms,
        progress=progress,
        close_browser_event=close_browser_event,
    )
    return results[0] if results else None


def _prepare_petition_payloads(
    drafts: tuple[DeclarationDraft, ...],
    petition_template: Mapping[str, object],
    request_text_path: Path,
    lookup_dir: Path,
    progress: ProgressCallback | None,
    status: StatusCallback | None = None,
) -> list[Mapping[str, object]]:
    if not drafts:
        raise LiveBrowserError("set_petition_data_recipients_missing")
    payloads = []
    for index, draft in enumerate(drafts, start=1):
        try:
            _validate_live_draft(draft)
            jurisdiction = resolve_captured_jurisdiction_fields(
                draft.fields, lookup_dir
            )
            payload = build_setpetitiondata_payload(
                petition_template,
                replace(draft, fields={**draft.fields, **jurisdiction}),
            )
            debug_path = _request_path_for_row(request_text_path, index, len(drafts))
            body_path, command_path = save_setpetitiondata_debug_text(
                payload, debug_path
            )
        except Exception as exc:
            _report_status(
                status,
                draft,
                "petition",
                "failed",
                message="اطلاعات این ردیف برای ثبت معتبر نیست.",
            )
            if isinstance(exc, LookupResolutionError):
                raise LiveBrowserError(str(exc)) from exc
            raise
        payloads.append(payload)
        _report(
            progress,
            f"[ردیف {index} از {len(drafts)}] متن درخواست: {body_path}\n"
            f"دستور curl: {command_path}\n"
            f"کد کنسول: {body_path.with_name(f'{body_path.stem}.console.txt')}",
        )
    return payloads


def _authentication_guard(
    page: object,
    current_credentials: list[LoginCredentials],
    credentials_provider: Callable[
        [LoginCredentials], LoginCredentials | None
    ] | None,
    timeout_ms: int,
    progress: ProgressCallback | None,
) -> Callable[[], None]:
    def ensure_authenticated() -> None:
        try:
            if page.locator("#menu4Container").is_visible():
                return
        except Exception:
            pass
        if credentials_provider is None:
            raise LiveBrowserError("session_expired_credentials_required")
        _report(progress, "نشست ثنا منقضی شده؛ برای ادامه ورود مجدد لازم است.")
        credentials = credentials_provider(current_credentials[0])
        if credentials is None:
            raise LiveBrowserError("session_reauthentication_cancelled")
        _run_logged_stage(
            progress,
            "ورود مجدد پس از پایان نشست",
            lambda: login_self_service(
                page, credentials.as_portal_fields(), timeout_ms=timeout_ms
            ),
        )
        current_credentials[0] = credentials

    return ensure_authenticated


def _normalize_recipient_identity(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = normalized.translate(str.maketrans({
        "ي": "ی", "ك": "ک", "۰": "0", "۱": "1", "۲": "2",
        "۳": "3", "۴": "4", "۵": "5", "۶": "6", "۷": "7",
        "۸": "8", "۹": "9", "٠": "0", "١": "1", "٢": "2",
        "٣": "3", "٤": "4", "٥": "5", "٦": "6", "٧": "7",
        "٨": "8", "٩": "9", "\u200c": " ",
    }))
    return " ".join(normalized.split()).casefold()


def _match_legacy_draft(
    petition: Mapping[str, object],
    drafts: tuple[DeclarationDraft, ...],
) -> DeclarationDraft | None:
    people = petition.get("theJSSPetitionPerson")
    if not isinstance(people, list):
        return None
    recipients = [
        person for person in people
        if isinstance(person, dict) and str(person.get("RoleType")) == "2"
    ]
    if len(recipients) != 1:
        return None
    recipient = recipients[0]
    portal_national_id = _normalize_recipient_identity(
        recipient.get("NationalityCode")
    )
    name_candidates = drafts
    if portal_national_id:
        matches = [
            draft for draft in drafts
            if _normalize_recipient_identity(
                draft.fields.get("recipient_national_id")
            ) == portal_national_id
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return None
        name_candidates = tuple(
            draft for draft in drafts
            if not _normalize_recipient_identity(
                draft.fields.get("recipient_national_id")
            )
        )

    portal_name = _normalize_recipient_identity(
        f"{recipient.get('Name', '')} {recipient.get('Family', '')}"
    )
    if not portal_name:
        return None
    matches = [
        draft for draft in name_candidates
        if _normalize_recipient_identity(
            f"{draft.fields.get('recipient_first_name', '')} "
            f"{draft.fields.get('recipient_last_name', '')}"
        ) == portal_name
    ]
    return matches[0] if len(matches) == 1 else None


def _reconcile_legacy_registrations(
    page: object,
    drafts: tuple[DeclarationDraft, ...],
    ensure_authenticated: Callable[[], None],
    progress: ProgressCallback | None,
    status: StatusCallback | None = None,
) -> tuple[
    tuple[tuple[DeclarationDraft, str], ...],
    tuple[DeclarationDraft, ...],
]:
    """Match old file-level codes to recipients without guessing."""

    groups: dict[tuple[Path, Path], list[DeclarationDraft]] = {}
    for draft in drafts:
        if draft.source_path is None:
            continue
        key = draft.source_dir.resolve(), draft.source_path.resolve()
        groups.setdefault(key, []).append(draft)

    matched: list[tuple[DeclarationDraft, str]] = []
    safe_unregistered: list[DeclarationDraft] = []
    for group in groups.values():
        source_path = group[0].source_path
        if source_path is None:
            continue
        codes = read_legacy_tracking_codes(group[0].source_dir, source_path)
        unresolved = list(group)
        complete = bool(codes)
        for code in codes:
            ensure_authenticated()
            petition = _run_logged_stage(
                progress,
                "بررسی سابقه قدیمی اظهارنامه چندمخاطبی",
                lambda code=code: _get_petition_by_tracking_code(page, code),
            )
            if str(petition.get("No") or "").strip() != code:
                complete = False
                continue
            draft = _match_legacy_draft(petition, tuple(unresolved))
            if draft is None:
                complete = False
                continue
            migrate_legacy_registration(draft, code)
            attachment_count = petition.get("JSSPetitionDocumentCount")
            if (
                isinstance(attachment_count, int)
                and not isinstance(attachment_count, bool)
                and attachment_count >= 0
            ):
                save_attachment_count(draft, code, attachment_count)
                _report_status(
                    status,
                    draft,
                    "attachment_count",
                    "success",
                    tracking_code=code,
                    attachment_count=attachment_count,
                    attachment_count_checked_at=(
                        datetime.now().astimezone().isoformat(timespec="seconds")
                    ),
                )
            unresolved.remove(draft)
            matched.append((draft, code))
            _report_status(
                status,
                draft,
                "petition",
                "success",
                tracking_code=code,
                message=(
                    "کد رهگیری سابقه قدیمی به مخاطب همین ردیف تطبیق داده شد؛ "
                    "ثبت دوباره انجام نشد."
                ),
            )
        if complete:
            safe_unregistered.extend(unresolved)
        elif unresolved:
            _report(
                progress,
                f"{len(unresolved)} ردیف از فایل {source_path.name} به‌دلیل "
                "تطبیق‌ناپذیری قطعی سابقه قدیمی کنار گذاشته شد؛ ثبت دوباره "
                "انجام نمی‌شود.",
            )
    return tuple(matched), tuple(safe_unregistered)


def _submit_petition_payloads(
    page: object,
    drafts: tuple[DeclarationDraft, ...],
    petition_payloads: list[Mapping[str, object]],
    ensure_authenticated: Callable[[], None],
    progress: ProgressCallback | None,
    status: StatusCallback | None,
    on_result: Callable[[DeclarationDraft, SetPetitionDataResult], None] | None,
    request_text_path: Path,
    signature_credentials: LoginCredentials | None = None,
) -> tuple[SetPetitionDataResult, ...]:
    results = []
    for index, petition_data in enumerate(petition_payloads, start=1):
        draft = drafts[index - 1]
        try:
            ensure_authenticated()
        except Exception:
            _report_status(
                status,
                draft,
                "petition",
                "failed",
                message="ورود معتبر برای ثبت این ردیف فراهم نشد.",
            )
            raise
        _report_status(status, draft, "petition", "running")
        try:
            result = _run_logged_stage(
                progress,
                f"ثبت موقت SetPetitionData ردیف {index} از {len(drafts)}",
                lambda: submit_one_petition_data(page, petition_data),
            )
        except Exception:
            _report_status(
                status,
                draft,
                "petition",
                "unknown",
                message="پاسخ ثبت قطعی نیست؛ پیش از تکرار بررسی شود.",
            )
            raise
        results.append(result)
        _report_status(
            status,
            draft,
            "petition",
            "success",
            tracking_code=result.tracking_code,
        )
        _report(
            progress,
            f"[ردیف {index} از {len(drafts)}] ثبت موفق؛ "
            f"کد رهگیری: {result.tracking_code}؛ "
            f"شماره اظهارنامه: {result.petition_number}",
        )
        if on_result is not None:
            on_result(draft, result)
        elif draft.source_path is not None:
            save_verified_result(
                draft,
                UploadResult("success", result.tracking_code),
                filename=RESULTS_FILENAME,
                attachment_context={
                    "petition_number": result.tracking_code,
                    "internal_petition_number": result.petition_number,
                    "petition_id": result.petition_id,
                    "petition_type_code": str(
                        petition_data.get("PetitionTypeCode") or "110"
                    ),
                    "reliefs": petition_data.get("theJSSPetitionRelief"),
                },
            )
        if draft.attachments:
            _report_status(
                status,
                draft,
                "attachment",
                "running",
                tracking_code=result.tracking_code,
                total=len(draft.attachments),
            )
            try:
                _attach_draft_files(
                    page, petition_data, result, draft, index, len(drafts), progress,
                    request_text_path, status,
                )
            except Exception as exc:
                detail = (
                    str(exc)
                    if isinstance(exc, (AttachmentApiError, LiveBrowserError))
                    else type(exc).__name__
                )
                _report(
                    progress,
                    f"[ردیف {index} از {len(drafts)}] ثبت اظهارنامه موفق است، "
                    f"اما پیوست کامل نشد؛ کد رهگیری: {result.tracking_code}؛ "
                    f"شماره اظهارنامه: {result.petition_number}؛ علت: {detail}. "
                    "ردیف بعدی اجرا نمی‌شود؛ وضعیت پیوست را پیش از هر اقدام "
                    "دوباره بررسی کنید.",
                )
                raise LiveBrowserError(
                    "attachment_failed_after_petition_registered:"
                    f"row={index}:tracking_code={result.tracking_code}:"
                    f"petition_number={result.petition_number}:cause={detail}"
                ) from exc
        else:
            _report_status(
                status, draft, "attachment", "skipped",
                tracking_code=result.tracking_code,
            )
            _report(
                progress,
                f"[ردیف {index} از {len(drafts)}] پیوستی انتخاب نشده؛ "
                "مرحله پیوست رد شد.",
            )
        ensure_authenticated()
        _store_final_attachment_count(
            page, draft, result.tracking_code, index, len(drafts), progress,
            status,
        )
        if draft.attachments and signature_credentials is not None:
            _complete_after_attachments(
                page, result, signature_credentials, index, len(drafts), progress,
            )
    return tuple(results)


def _attach_existing_petitions(
    page: object,
    requests: tuple[tuple[DeclarationDraft, str], ...],
    ensure_authenticated: Callable[[], None],
    progress: ProgressCallback | None,
    request_text_path: Path,
    status: StatusCallback | None = None,
    signature_credentials: LoginCredentials | None = None,
) -> None:
    source_counts: dict[Path, int] = {}
    for draft, _tracking_code in requests:
        if draft.source_path is not None:
            source = draft.source_path.resolve()
            source_counts[source] = source_counts.get(source, 0) + 1
    for draft, _tracking_code in requests:
        for attachment in draft.attachments:
            if (
                not attachment.path.is_file()
                or not attachment.kind.strip()
                or not attachment.title.strip()
                or not attachment.document_number.strip()
                or not attachment.document_date.strip()
            ):
                _report_status(
                    status,
                    draft,
                    "attachment",
                    "failed",
                    message="مشخصات یا فایل پیوست ناقص است.",
                    total=len(draft.attachments),
                )
                raise LiveBrowserError("attachment_preflight_invalid")
    for index, (draft, tracking_code) in enumerate(requests, start=1):
        if not draft.attachments:
            _report_status(status, draft, "attachment", "skipped")
            _report(progress, f"[ردیف {index} از {len(requests)}] پیوستی انتخاب نشده؛ رد شد.")
            ensure_authenticated()
            _store_final_attachment_count(
                page, draft, tracking_code, index, len(requests), progress,
                status,
            )
            continue
        _report_status(
            status,
            draft,
            "attachment",
            "running",
            tracking_code=tracking_code,
            total=len(draft.attachments),
        )
        try:
            ensure_authenticated()
        except Exception:
            _report_status(
                status,
                draft,
                "attachment",
                "failed",
                tracking_code=tracking_code,
                message="ورود معتبر برای افزودن پیوست فراهم نشد.",
                total=len(draft.attachments),
            )
            raise
        try:
            saved = (
                read_saved_attachment_context(
                    draft.source_dir,
                    draft.source_path,
                    source_recipient_ordinal=draft.source_recipient_ordinal,
                    source_recipient_count=source_counts[
                        draft.source_path.resolve()
                    ],
                )
                if draft.source_path is not None else None
            )
        except ResultPersistenceError:
            saved = None
        if saved and saved.get("tracking_code") == tracking_code:
            petition_data = {
                "No": tracking_code,
                "Id": saved["petition_id"],
                "PetitionTypeCode": saved["petition_type_code"],
                "theJSSPetitionRelief": saved["reliefs"],
            }
        else:
            try:
                petition_data = _run_logged_stage(
                    progress,
                    f"بازیابی اظهارنامه با کد رهگیری {tracking_code}",
                    lambda: _get_petition_by_tracking_code(page, tracking_code),
                )
            except Exception:
                _report_status(
                    status,
                    draft,
                    "attachment",
                    "failed",
                    tracking_code=tracking_code,
                    message="اظهارنامه برای افزودن پیوست بازیابی نشد.",
                    total=len(draft.attachments),
                )
                raise
        petition_number = str(petition_data.get("No") or "").strip()
        petition_id = str(petition_data.get("Id") or "").strip()
        reliefs = petition_data.get("theJSSPetitionRelief")
        type_code = str(petition_data.get("PetitionTypeCode") or "").strip()
        if petition_number != tracking_code.strip() or not petition_id or not type_code:
            _report_status(
                status,
                draft,
                "attachment",
                "failed",
                tracking_code=tracking_code,
                message="مشخصات اظهارنامه با کد رهگیری تطابق ندارد.",
                total=len(draft.attachments),
            )
            raise LiveBrowserError("tracked_petition_context_mismatch")
        result = SetPetitionDataResult(tracking_code, petition_number, petition_id)
        _report(
            progress,
            f"[ردیف {index} از {len(requests)}] اظهارنامه با کد رهگیری "
            f"{tracking_code} پیدا شد؛ فقط پیوست افزوده می‌شود و ثبت اظهارنامه اجرا نمی‌شود.",
        )
        _attach_draft_files(
            page, petition_data, result, draft, index, len(requests), progress,
            request_text_path, status,
        )
        ensure_authenticated()
        _store_final_attachment_count(
            page, draft, tracking_code, index, len(requests), progress,
            status,
        )
        if signature_credentials is not None:
            _complete_after_attachments(
                page, result, signature_credentials, index, len(requests), progress,
            )


def _complete_after_attachments(
    page: object,
    result: SetPetitionDataResult,
    credentials: LoginCredentials,
    index: int,
    total: int,
    progress: ProgressCallback | None,
) -> None:
    """Make the petition ready, then add only the person's SANA signature."""

    national_code = credentials.national_code.strip()
    if not national_code:
        raise LiveBrowserError("signature_identity_missing")
    prefix = f"[ردیف {index} از {total}]"
    ready = _run_logged_stage(
        progress,
        f"{prefix} آمادگی برای پرداخت",
        lambda: _post_portal_action_once(
            page,
            READY_FOR_PAYMENT_URL,
            {
                "petitionNo": result.tracking_code,
                "petitionType": 3,
                "masterJudgeUnitId": None,
                "specialPermissionTypeCode": "",
                "forceReadyPayment": True,
            },
            "ready_for_payment",
        ),
    )
    if (
        str(ready.get("No") or "").strip() != result.tracking_code
        or ready.get("State") != 2
        or str(ready.get("Id") or "").strip() != result.petition_id
    ):
        raise LiveBrowserError("ready_for_payment_response_invalid")

    person_signature = _run_logged_stage(
        progress,
        f"{prefix} درج امضای ثنا",
        lambda: _post_portal_action_once(
            page,
            GET_PERSON_SIGNATURE_URL,
            {
                "petitionNo": result.tracking_code,
                "nationalityCode": national_code,
            },
            "person_signature",
        ),
    )
    _validate_signature_document(
        person_signature, result.petition_id, "person_signature_response_invalid"
    )
    _report(
        progress,
        f"{prefix} آمادگی برای پرداخت و درج امضای شخص تکمیل شد.",
    )


def _post_portal_action_once(
    page: object,
    url: str,
    payload: Mapping[str, object],
    operation: str,
) -> dict[str, object]:
    try:
        response = page.evaluate(
            """async ({ url, payload }) => {
                const response = await fetch(url, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'accept': 'application/json, text/plain, */*',
                        'content-type': 'application/json;charset=UTF-8',
                        'x-requested-with': 'XMLHttpRequest',
                    },
                    body: JSON.stringify(payload),
                });
                return { status: response.status, text: await response.text() };
            }""",
            {"url": url, "payload": dict(payload)},
        )
    except Exception:
        raise LiveBrowserError(f"{operation}_transport_ambiguous") from None
    if not isinstance(response, dict):
        raise LiveBrowserError(f"{operation}_response_unreadable")
    status = response.get("status")
    if not isinstance(status, int) or not 200 <= status < 300:
        detail = f":http_status={status}" if isinstance(status, int) else ""
        raise LiveBrowserError(f"{operation}_http_failed{detail}")
    text = response.get("text")
    if not isinstance(text, str):
        raise LiveBrowserError(f"{operation}_response_unreadable")
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        raise LiveBrowserError(f"{operation}_response_invalid") from None
    if not isinstance(value, dict):
        raise LiveBrowserError(f"{operation}_response_invalid")
    return value


def _validate_signature_document(
    value: Mapping[str, object], petition_id: str, error: str
) -> None:
    files = value.get("TheJSSPetitionDocumentFileList")
    if (
        str(value.get("JSSPetitionId") or "").strip() != petition_id
        or not str(value.get("Id") or "").strip()
        or not isinstance(files, list)
        or len(files) != 1
        or not isinstance(files[0], dict)
        or not str(files[0].get("Id") or "").strip()
    ):
        raise LiveBrowserError(error)


def _get_petition_by_tracking_code(
    page: object, tracking_code: str
) -> Mapping[str, object]:
    if not tracking_code.strip():
        raise LiveBrowserError("tracking_code_missing")
    try:
        response = page.evaluate(
            """async ({ url, trackingCode }) => {
            const response = await fetch(url, {
                method: 'POST', credentials: 'include',
                headers: {
                    'accept': 'application/json, text/plain, */*',
                    'content-type': 'application/json;charset=UTF-8',
                    'x-requested-with': 'XMLHttpRequest',
                },
                body: JSON.stringify({ petitionNo: trackingCode, petitionType: 3 }),
            });
            return { status: response.status, text: await response.text() };
        }""",
            {"url": GET_PETITION_BY_TRACKING_CODE_URL, "trackingCode": tracking_code},
        )
    except Exception as exc:
        raise LiveBrowserError("tracked_petition_lookup_failed") from exc
    if not isinstance(response, dict) or response.get("status") != 200:
        raise LiveBrowserError("tracked_petition_lookup_http_error")
    try:
        value = json.loads(str(response.get("text", "")))
    except (TypeError, ValueError) as exc:
        raise LiveBrowserError("tracked_petition_lookup_response_invalid") from exc
    if not isinstance(value, dict):
        raise LiveBrowserError("tracked_petition_lookup_response_invalid")
    return value


def _review_petitions(
    page: object,
    tracking_codes: tuple[str, ...],
    ensure_authenticated: Callable[[], None],
    progress: ProgressCallback | None,
    on_petition_review: Callable[
        [str, Mapping[str, object]], None
    ] | None,
) -> None:
    """Read petitions once and return only responses matching their tracking code."""

    for tracking_code in tracking_codes:
        code = str(tracking_code).strip()
        ensure_authenticated()
        petition = _run_logged_stage(
            progress,
            f"بررسی خواندنی اظهارنامه با کد رهگیری {code}",
            lambda code=code: _get_petition_by_tracking_code(page, code),
        )
        if str(petition.get("No") or "").strip() != code:
            raise LiveBrowserError("tracked_petition_number_mismatch")
        petition = dict(petition)
        petition["JSSPetitionDocumentCount"] = _get_uploaded_attachment_count(
            page, code
        )
        if on_petition_review is not None:
            on_petition_review(code, petition)


def _portal_json_request(
    page: object,
    url: str,
    *,
    payload: Mapping[str, object] | None = None,
) -> object:
    try:
        response = page.evaluate(
            """async ({ url, payload }) => {
            const options = {
                method: payload === null ? 'GET' : 'POST',
                credentials: 'include',
                headers: {
                    'accept': 'application/json, text/plain, */*',
                    'x-requested-with': 'XMLHttpRequest',
                },
            };
            if (payload !== null) {
                options.headers['content-type'] = 'application/json;charset=UTF-8';
                options.body = JSON.stringify(payload);
            }
            const response = await fetch(url, options);
            return { status: response.status, text: await response.text() };
        }""",
            {"url": url, "payload": dict(payload) if payload is not None else None},
        )
    except Exception as exc:
        raise LiveBrowserError("recent_registrations_lookup_failed") from exc
    if not isinstance(response, dict) or response.get("status") != 200:
        raise LiveBrowserError("recent_registrations_lookup_http_error")
    try:
        value = json.loads(str(response.get("text", "")))
        if isinstance(value, str):
            value = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise LiveBrowserError("recent_registrations_response_invalid") from exc
    return value


def _refresh_recent_registrations(
    page: object,
    tracking_codes: tuple[str, ...],
    ensure_authenticated: Callable[[], None],
    progress: ProgressCallback | None,
    callback: Callable[[Mapping[str, object]], None] | None,
    *,
    months: int,
) -> None:
    """Read the observed paginated report and return only exact local matches."""

    targets = {str(code).strip() for code in tracking_codes if str(code).strip()}
    date_field = {
        1: "OneMonthLaterDate",
        3: "ThreeMonthLaterDate",
    }.get(months)
    if date_field is None:
        raise LiveBrowserError("recent_registrations_period_invalid")
    ensure_authenticated()
    date_info = _portal_json_request(page, GET_PORTAL_DATETIME_URL)
    if not isinstance(date_info, Mapping):
        raise LiveBrowserError("recent_registrations_response_invalid")
    from_date = str(date_info.get(date_field) or "").strip()
    to_date = str(date_info.get("TodayDate") or "").strip()
    if not from_date or not to_date:
        raise LiveBrowserError("recent_registrations_response_invalid")
    from_key = _portal_date_key(from_date)
    to_key = _portal_date_key(to_date)
    if from_key is None or to_key is None or from_key > to_key:
        raise LiveBrowserError("recent_registrations_response_invalid")

    report = _recent_registration_report(from_date, to_date)
    listed_codes: set[str] = set()
    listed_petitions: dict[str, Mapping[str, object]] = {}
    matched_codes: set[str] = set()
    expected_total: int | None = None
    page_number = 0
    while True:
        ensure_authenticated()
        response = _portal_json_request(
            page,
            GET_PETITION_LIST_URL,
            payload={
                "reportData": json.dumps(
                    report, ensure_ascii=False, separators=(",", ":")
                ),
                "fromPage": page_number,
            },
        )
        if not isinstance(response, Mapping):
            raise LiveBrowserError("recent_registrations_response_invalid")
        rows = response.get("ObjectList")
        total = response.get("TotalRecords")
        returned_page = response.get("PageNo")
        if (
            not isinstance(rows, list)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total < 0
            or isinstance(returned_page, bool)
            or not isinstance(returned_page, int)
            or returned_page != page_number
        ):
            raise LiveBrowserError("recent_registrations_response_invalid")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise LiveBrowserError("recent_registrations_pagination_inconsistent")
        if not rows:
            if len(listed_codes) != total:
                raise LiveBrowserError("recent_registrations_pagination_incomplete")
            break
        for row in rows:
            if not isinstance(row, Mapping):
                raise LiveBrowserError("recent_registrations_response_invalid")
            code = str(row.get("No") or "").strip()
            if not re.fullmatch(r"\d{16}", code):
                raise LiveBrowserError("recent_registration_tracking_code_invalid")
            if code in listed_codes:
                raise LiveBrowserError("recent_registrations_page_repeated")
            created = _portal_date_key(row.get("CreateDateTime"))
            if created is None or not from_key <= created <= to_key:
                raise LiveBrowserError("recent_registration_date_invalid")
            listed_codes.add(code)
            listed_petitions[code] = dict(row)
            if code in targets:
                matched_codes.add(code)
        if len(listed_codes) > total:
            raise LiveBrowserError("recent_registrations_pagination_inconsistent")
        if len(listed_codes) == total:
            break
        page_number += 1
        if page_number > total:
            raise LiveBrowserError("recent_registrations_pagination_incomplete")

    detailed_registrations: dict[str, Mapping[str, object]] = {}
    for code in sorted(listed_codes):
        ensure_authenticated()
        petition = _get_petition_by_tracking_code(page, code)
        if str(petition.get("No") or "").strip() != code:
            raise LiveBrowserError("tracked_petition_number_mismatch")
        detailed_registrations[code] = {
            **listed_petitions[code],
            **petition,
        }
    petitions = {
        code: detailed_registrations[code] for code in matched_codes
    }
    _report(
        progress,
        f"بازخوانی {months} ماه اخیر پایان یافت؛ دریافت‌شده: "
        f"{len(listed_codes)}، تطبیق‌یافته: {len(petitions)}.",
    )
    if callback is not None:
        callback({
            "months": months,
            "received": len(listed_codes),
            "checked": len(listed_codes),
            "matched": len(petitions),
            "unmatched": len(listed_codes) - len(petitions),
            "petitions": petitions,
            "registrations": detailed_registrations,
        })


def _recent_registration_report(
    from_date: str, to_date: str
) -> dict[str, object]:
    """Build the report payload observed in the portal's report controller."""

    return {
        "isJSSFinalPetition": False,
        "isJSSLawyerContractSelfSign": False,
        "isJSSLawyerContractSelfLawyer": False,
        "isJSSLawyerContractSelfPerson": False,
        "isJSSLawyerContractSelfSuperLawyer": False,
        "isJSSLawyerContractSelfCancelLawyer": False,
        "saveFromDate": from_date,
        "saveToDate": to_date,
        "sendFromDate": "",
        "sendToDate": "",
        "returnFromDate": "",
        "returnToDate": "",
        "lastModifiedFromDate": "",
        "lastModifiedToDate": "",
        "sendOfficeTitle": "",
        "sendOfficeCode": "",
        "sendOfficeId": "",
        "sendUnitTitle": "",
        "sendUnitCode": "",
        "sendUnitId": "",
        "petitionTypeCodes": "",
        "billTypeCodes": "",
        "specialPermissionTypeIds": "",
        "petitionTypes": "",
        "accusationTypeId": "",
        "actionSubjectId": "",
        "declarationSubjectId": "",
        "declarationNo": "",
        "creatorNationalityCode": "",
        "name": "",
        "family": "",
        "nationalityCode": "",
        "vakilNo": "",
        "posNo": "",
        "fromPage": 0,
        "state": "0",
        "returnReason": "0",
        "roleType": "0",
        "orderBy": "PosNo",
        "groupBy": "5",
        "objectStateId": "",
        "reportName": "JSSPetitionByJSSUnit",
        "totalRecords": 0,
        "currentPage": 1,
        "itemsPerPage": "20",
        "pageSize": "20",
        "allPages": 1,
        "theJSSBillReportResultList": [],
        "theJSSClearanceCertResultList": [],
        "theJSSPetitionReportResultList": [],
        "theJSSDeclarationReportResultList": [],
        "theRepostTempList": [],
        "theAllReportlist": [],
        "theAllBillReportlist": [],
        "theAllLawyerContractReportlist": [],
        "theJSSBillType": {},
        "theJSSPetitionType": {},
        "theJSSPetitionRelief": {},
        "theJSSSpecialPermissionType": {},
        "theAllEdocumentReportlist": [],
        "theAllLawyerTaxReportlist": [],
    }


def _portal_date_key(value: object) -> tuple[int, int, int] | None:
    normalized = _normalize_recipient_identity(value)
    match = re.search(r"(?<!\d)(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?!\d)", normalized)
    if match is None:
        return None
    year, month, day = map(int, match.groups())
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    return year, month, day


def _get_uploaded_attachment_count(page: object, petition_no: str) -> int:
    """Return the attachment count displayed by the portal's attachment table."""

    if not petition_no.strip():
        raise LiveBrowserError("attachment_count_petition_number_missing")
    try:
        response = page.evaluate(
            """async ({ url, petitionNo }) => {
            const response = await fetch(url, {
                method: 'POST', credentials: 'include',
                headers: {
                    'accept': 'application/json, text/plain, */*',
                    'content-type': 'application/json;charset=UTF-8',
                    'x-requested-with': 'XMLHttpRequest',
                },
                body: JSON.stringify({
                    petitionNo,
                    pageNumber: 0,
                    pageSize: 1000,
                    stateDocument: '1',
                }),
            });
            return { status: response.status, text: await response.text() };
        }""",
            {"url": GET_PETITION_DOCUMENTS_URL, "petitionNo": petition_no},
        )
    except Exception as exc:
        raise LiveBrowserError("attachment_count_lookup_failed") from exc
    if not isinstance(response, dict) or response.get("status") != 200:
        raise LiveBrowserError("attachment_count_lookup_http_error")
    try:
        value = json.loads(str(response.get("text", "")))
        wrappers = value["ObjectList"]
        documents = wrappers[0]["theJSSPetitionDocument"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise LiveBrowserError("attachment_count_response_invalid") from exc
    if not isinstance(documents, list):
        raise LiveBrowserError("attachment_count_response_invalid")

    return sum(_portal_document_attachment_count(document) for document in documents)


def _portal_document_attachment_count(document: object) -> int:
    """Reproduce the CountFileList calculation used by the SANA UI."""

    if not isinstance(document, dict):
        raise LiveBrowserError("attachment_count_response_invalid")
    try:
        definition = document.get("OtherDataDefinition")
        if isinstance(definition, str):
            definition = json.loads(definition)
        input_files = definition["TheInputFileList"]
        if not isinstance(input_files, list):
            raise TypeError
        if len(input_files) > 1:
            return len(input_files)

        other_data = document.get("OtherData")
        if isinstance(other_data, str):
            other_data = json.loads(other_data)
        if not isinstance(other_data, list):
            raise TypeError
        has_file_upload = any(
            isinstance(item, dict) and item.get("Type") == "FileUpload"
            for item in other_data
        )
        if not has_file_upload:
            return 0
        value = int(str(other_data[-1]["Value"]).strip())
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LiveBrowserError("attachment_count_response_invalid") from exc
    if value < 0:
        raise LiveBrowserError("attachment_count_response_invalid")
    return value


def _store_final_attachment_count(
    page: object,
    draft: DeclarationDraft,
    tracking_code: str,
    row_index: int,
    row_count: int,
    progress: ProgressCallback | None,
    status: StatusCallback | None = None,
    *,
    expected_minimum: int | None = None,
) -> int:
    """Read and persist the portal's final attachment count for one row."""

    petition = _run_logged_stage(
        progress,
        f"بررسی نهایی تعداد ضمیمه‌های ردیف {row_index} از {row_count}",
        lambda: _get_petition_by_tracking_code(page, tracking_code),
    )
    if str(petition.get("No") or "").strip() != tracking_code.strip():
        raise LiveBrowserError("attachment_count_petition_mismatch")
    count = _get_uploaded_attachment_count(page, tracking_code)
    minimum = len(draft.attachments) if expected_minimum is None else expected_minimum
    if count < minimum:
        raise LiveBrowserError("attachment_count_inconsistent")
    try:
        save_attachment_count(draft, tracking_code, count)
    except ResultPersistenceError as exc:
        raise LiveBrowserError("attachment_count_persistence_failed") from exc
    _report_status(
        status,
        draft,
        "attachment_count",
        "success",
        tracking_code=tracking_code,
        attachment_count=count,
        attachment_count_checked_at=(
            datetime.now().astimezone().isoformat(timespec="seconds")
        ),
    )
    _report(
        progress,
        f"[ردیف {row_index} از {row_count}] تعداد ضمیمه‌های ثبت‌شده در ثنا: "
        f"{count}؛ مقدار در {RESULTS_FILENAME} ذخیره شد.",
    )
    return count


def _refresh_attachment_counts(
    page: object,
    requests: tuple[tuple[DeclarationDraft, str], ...],
    ensure_authenticated: Callable[[], None],
    progress: ProgressCallback | None,
    status: StatusCallback | None = None,
) -> int:
    """Refresh stored counts without registering or uploading anything."""

    refreshed = 0
    for index, (draft, tracking_code) in enumerate(requests, start=1):
        ensure_authenticated()
        try:
            _store_final_attachment_count(
                page, draft, tracking_code, index, len(requests), progress,
                status, expected_minimum=0,
            )
        except LiveBrowserError as exc:
            _report_status(
                status, draft, "attachment_count", "failed",
                tracking_code=tracking_code, message=str(exc),
            )
            _report(
                progress,
                f"[ردیف {index} از {len(requests)}] بازخوانی تعداد ضمیمه "
                f"ناموفق بود: {exc}",
            )
            continue
        refreshed += 1
    _report(
        progress,
        f"بازخوانی تعداد ضمیمه‌ها پایان یافت؛ موفق: {refreshed} از "
        f"{len(requests)} ردیف.",
    )
    return refreshed


def _request_path_for_row(path: Path, index: int, total: int) -> Path:
    if total == 1:
        return path
    return path.with_name(f"{path.stem}-row-{index:03d}{path.suffix}")


def _attach_draft_files(
    page: object,
    petition_data: Mapping[str, object],
    result: SetPetitionDataResult,
    draft: DeclarationDraft,
    row_index: int,
    row_count: int,
    progress: ProgressCallback | None,
    request_text_path: Path,
    status: StatusCallback | None = None,
) -> None:
    """Attach this draft's files to the petition returned for the same row."""

    prefix = f"[ردیف {row_index} از {row_count}]"
    try:
        reliefs = _reliefs_for_attachment_lookup(
            petition_data.get("theJSSPetitionRelief"), result.petition_id
        )
    except Exception:
        _report_status(
            status,
            draft,
            "attachment",
            "failed",
            tracking_code=result.tracking_code,
            message="اطلاعات لازم برای پیوست کامل نیست.",
            total=len(draft.attachments),
        )
        raise
    for attachment_index, attachment in enumerate(draft.attachments, start=1):
        row_path = _request_path_for_row(
            request_text_path, row_index, row_count
        )
        console_path = row_path.with_name(
            f"{row_path.stem}.attachment-{attachment_index:03d}.console.txt"
        )
        save_attachment_console_text(
            None,
            result.tracking_code,
            result.petition_id,
            reliefs,
            str(petition_data.get("PetitionTypeCode") or "110"),
            attachment.document_number,
            attachment.document_date,
            attachment.path,
            console_path,
            show_original=attachment.show_to_recipient,
            document_title=attachment.title,
            requested_title=attachment.kind,
        )
        _report(
            progress,
            f"{prefix} [پیوست {attachment_index}/{len(draft.attachments)}] "
            f"کد چهار درخواست برای کنسول Chrome پیش از ارسال ساخته شد: "
            f"{console_path}",
        )
    lookup_client = AttachmentClient(
        page,
        result.tracking_code,
        result.petition_id,
        progress=lambda message: _report(progress, f"{prefix} {message}"),
    )
    try:
        attachment_types = _run_logged_stage(
            progress,
            f"دریافت نوع‌های مجاز پیوست برای اظهارنامه {result.tracking_code}",
            lambda: lookup_client.get_valid_attachment_types(
                reliefs,
                petition_type_code=str(
                    petition_data.get("PetitionTypeCode") or "110"
                ),
            ),
        )
    except Exception:
        _report_status(
            status,
            draft,
            "attachment",
            "failed",
            tracking_code=result.tracking_code,
            message="نوع‌های مجاز پیوست دریافت نشد.",
            total=len(draft.attachments),
        )
        raise

    for attachment_index, attachment in enumerate(draft.attachments, start=1):
        try:
            attachment_type = _match_attachment_type(
                attachment.kind, attachment_types
            )
        except Exception:
            _report_status(
                status,
                draft,
                "attachment",
                "failed",
                tracking_code=result.tracking_code,
                message="نوع انتخاب‌شده برای این اظهارنامه مجاز نیست.",
                completed=attachment_index - 1,
                total=len(draft.attachments),
            )
            raise
        attachment_prefix = (
            f"{prefix} [پیوست {attachment_index}/"
            f"{len(draft.attachments)}]"
        )
        _report(
            progress,
            f"{attachment_prefix} نوع مجاز انتخاب شد: "
            f"{attachment_type.title} (کد {attachment_type.code})؛ "
            f"پسوند فایل: {attachment.path.suffix or 'نامشخص'}",
        )
        attachment_client = AttachmentClient(
            page,
            result.tracking_code,
            result.petition_id,
            progress=lambda message, p=attachment_prefix: _report(
                progress, f"{p} {message}"
            ),
        )
        try:
            _run_logged_stage(
                progress,
                f"افزودن پیوست {attachment_index}/{len(draft.attachments)} "
                f"به اظهارنامه {result.tracking_code}",
                lambda: attachment_client.register_attachment(
                    attachment_type,
                    document_number=attachment.document_number,
                    document_date=attachment.document_date,
                    file_path=attachment.path,
                    show_original=attachment.show_to_recipient,
                    document_title=attachment.title,
                ),
            )
        except Exception:
            _report_status(
                status,
                draft,
                "attachment",
                "unknown",
                tracking_code=result.tracking_code,
                message="نتیجه پیوست قطعی نیست؛ پیش از تکرار بررسی شود.",
                completed=attachment_index - 1,
                total=len(draft.attachments),
            )
            raise
        _report_status(
            status,
            draft,
            "attachment",
            "success",
            tracking_code=result.tracking_code,
            completed=attachment_index,
            total=len(draft.attachments),
        )


def _reliefs_for_attachment_lookup(
    value: object, petition_id: str
) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not value:
        raise LiveBrowserError("attachment_reliefs_missing")
    reliefs: list[Mapping[str, object]] = []
    for relief in value:
        if not isinstance(relief, Mapping):
            raise LiveBrowserError("attachment_relief_invalid")
        copied = deepcopy(dict(relief))
        copied["JSSPetitionId"] = petition_id
        reliefs.append(copied)
    return reliefs


def _match_attachment_type(
    requested_title: str, attachment_types: list[AttachmentType]
) -> AttachmentType:
    normalized = _normalize_attachment_title(requested_title)
    matches = [
        item for item in attachment_types
        if _normalize_attachment_title(item.title) == normalized
    ]
    if len(matches) == 1:
        return matches[0]
    available = ", ".join(
        f"{item.title} ({item.code})" for item in attachment_types
    ) or "هیچ نوعی برنگشت"
    error = (
        "attachment_type_not_unique"
        if len(matches) > 1
        else "attachment_type_not_valid_for_relief"
    )
    raise LiveBrowserError(
        f"{error}:requested={requested_title!r};available={available}"
    )


def _normalize_attachment_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.replace("ي", "ی").replace("ك", "ک")
    normalized = normalized.replace("\u200c", " ")
    return " ".join(normalized.split())
