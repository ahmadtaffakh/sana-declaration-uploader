"""Sequential SetPetitionData pilot kept separate from normal upload flow."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from queue import Empty, Queue
from threading import Event
from typing import Mapping
import unicodedata

from .attachment_api import AttachmentApiError, AttachmentClient, AttachmentType
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
from .results import RESULTS_FILENAME, save_verified_result


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
    on_result: Callable[[DeclarationDraft, SetPetitionDataResult], None] | None = None,
    pending_requests: Queue | None = None,
    reauth_credentials_provider: Callable[
        [LoginCredentials], LoginCredentials | None
    ] | None = None,
    close_browser_event: Event,
) -> tuple[SetPetitionDataResult, ...] | None:
    """Submit rows sequentially; advance only after a verified tracking code."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LiveBrowserError(
            "playwright_not_installed: install the project with [browser]"
        ) from exc

    if not drafts:
        raise LiveBrowserError("set_petition_data_recipients_missing")
    petition_payloads = _prepare_petition_payloads(
        drafts, petition_template, request_text_path, lookup_dir, progress
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=headless)
        context = browser.new_context()
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
            results = _submit_petition_payloads(
                page, drafts, petition_payloads, ensure_authenticated,
                progress, on_result,
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
                (
                    queued_drafts, queued_credentials, queued_template,
                    queued_path, queued_lookup_dir,
                ) = queued
                current_credentials[0] = queued_credentials
                queued_payloads = _prepare_petition_payloads(
                    queued_drafts, queued_template, queued_path,
                    queued_lookup_dir, progress,
                )
                queued_results = _submit_petition_payloads(
                    page, queued_drafts, queued_payloads, ensure_authenticated,
                    progress, on_result,
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
) -> list[Mapping[str, object]]:
    if not drafts:
        raise LiveBrowserError("set_petition_data_recipients_missing")
    payloads = []
    for index, draft in enumerate(drafts, start=1):
        _validate_live_draft(draft)
        try:
            jurisdiction = resolve_captured_jurisdiction_fields(
                draft.fields, lookup_dir
            )
        except LookupResolutionError as exc:
            raise LiveBrowserError(str(exc)) from exc
        payload = build_setpetitiondata_payload(
            petition_template, replace(draft, fields={**draft.fields, **jurisdiction})
        )
        debug_path = _request_path_for_row(request_text_path, index, len(drafts))
        body_path, command_path = save_setpetitiondata_debug_text(payload, debug_path)
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


def _submit_petition_payloads(
    page: object,
    drafts: tuple[DeclarationDraft, ...],
    petition_payloads: list[Mapping[str, object]],
    ensure_authenticated: Callable[[], None],
    progress: ProgressCallback | None,
    on_result: Callable[[DeclarationDraft, SetPetitionDataResult], None] | None,
) -> tuple[SetPetitionDataResult, ...]:
    results = []
    for index, petition_data in enumerate(petition_payloads, start=1):
        ensure_authenticated()
        result = _run_logged_stage(
            progress,
            f"ثبت موقت SetPetitionData ردیف {index} از {len(drafts)}",
            lambda: submit_one_petition_data(page, petition_data),
        )
        results.append(result)
        _report(
            progress,
            f"[ردیف {index} از {len(drafts)}] ثبت موفق؛ "
            f"کد رهگیری: {result.tracking_code}؛ "
            f"شماره اظهارنامه: {result.petition_number}",
        )
        draft = drafts[index - 1]
        if on_result is not None:
            on_result(draft, result)
        elif draft.source_path is not None:
            save_verified_result(
                draft,
                UploadResult("success", result.tracking_code),
                filename=RESULTS_FILENAME,
            )
        if draft.attachments:
            try:
                _attach_draft_files(
                    page, petition_data, result, draft, index, len(drafts), progress
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
            _report(
                progress,
                f"[ردیف {index} از {len(drafts)}] پیوستی انتخاب نشده؛ "
                "مرحله پیوست رد شد.",
            )
    return tuple(results)


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
) -> None:
    """Attach this draft's files to the petition returned for the same row."""

    prefix = f"[ردیف {row_index} از {row_count}]"
    reliefs = _reliefs_for_attachment_lookup(
        petition_data.get("theJSSPetitionRelief"), result.petition_id
    )
    lookup_client = AttachmentClient(
        page,
        result.petition_number,
        result.petition_id,
        progress=lambda message: _report(progress, f"{prefix} {message}"),
    )
    attachment_types = _run_logged_stage(
        progress,
        f"دریافت نوع‌های مجاز پیوست برای اظهارنامه {result.petition_number}",
        lambda: lookup_client.get_valid_attachment_types(
            reliefs,
            petition_type_code=str(
                petition_data.get("PetitionTypeCode") or "110"
            ),
        ),
    )

    for attachment_index, attachment in enumerate(draft.attachments, start=1):
        attachment_type = _match_attachment_type(
            attachment.kind, attachment_types
        )
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
            result.petition_number,
            result.petition_id,
            progress=lambda message, p=attachment_prefix: _report(
                progress, f"{p} {message}"
            ),
        )
        _run_logged_stage(
            progress,
            f"افزودن پیوست {attachment_index}/{len(draft.attachments)} "
            f"به اظهارنامه {result.petition_number}",
            lambda: attachment_client.register_attachment(
                attachment_type,
                document_number=attachment.document_number,
                document_date=attachment.document_date,
                file_path=attachment.path,
                show_original=attachment.show_to_recipient,
                document_title=attachment.title,
            ),
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
