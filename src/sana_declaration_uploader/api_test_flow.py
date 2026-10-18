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
        or legacy_attachment_only
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
    return tuple(results)


def _attach_existing_petitions(
    page: object,
    requests: tuple[tuple[DeclarationDraft, str], ...],
    ensure_authenticated: Callable[[], None],
    progress: ProgressCallback | None,
    request_text_path: Path,
    status: StatusCallback | None = None,
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
                    stateDocument: '3',
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
