"""Sequential SetPetitionData pilot kept separate from normal upload flow."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
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
from .models import DeclarationDraft
from .petition_api import (
    SetPetitionDataResult,
    build_setpetitiondata_payload,
    save_setpetitiondata_debug_text,
    submit_one_petition_data,
)


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
    petition_payloads = []
    for index, draft in enumerate(drafts, start=1):
        _validate_live_draft(draft)
        try:
            jurisdiction = resolve_captured_jurisdiction_fields(
                draft.fields, lookup_dir
            )
        except LookupResolutionError as exc:
            raise LiveBrowserError(str(exc)) from exc
        payload_draft = replace(draft, fields={**draft.fields, **jurisdiction})
        petition_data = build_setpetitiondata_payload(
            petition_template, payload_draft
        )
        debug_path = _request_path_for_row(request_text_path, index, len(drafts))
        body_path, command_path = save_setpetitiondata_debug_text(
            petition_data, debug_path
        )
        petition_payloads.append(petition_data)
        _report(
            progress,
            f"[ردیف {index} از {len(drafts)}] متن درخواست: {body_path}\n"
            f"دستور curl: {command_path}\n"
            f"کد کنسول: {body_path.with_name(f'{body_path.stem}.console.txt')}",
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
            results = []
            for index, petition_data in enumerate(petition_payloads, start=1):
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
                if draft.attachments:
                    try:
                        _attach_draft_files(
                            page,
                            petition_data,
                            result,
                            draft,
                            index,
                            len(drafts),
                            progress,
                        )
                    except Exception as exc:
                        detail = (
                            str(exc)
                            if isinstance(exc, (AttachmentApiError, LiveBrowserError))
                            else type(exc).__name__
                        )
                        _report(
                            progress,
                            f"[ردیف {index} از {len(drafts)}] ثبت اظهارنامه موفق "
                            f"است، اما پیوست کامل نشد؛ کد رهگیری: "
                            f"{result.tracking_code}؛ شماره اظهارنامه: "
                            f"{result.petition_number}؛ علت: {detail}. "
                            "ردیف بعدی اجرا نمی‌شود؛ وضعیت پیوست را پیش از "
                            "هر اقدام دوباره بررسی کنید.",
                        )
                        raise LiveBrowserError(
                            "attachment_failed_after_petition_registered:"
                            f"row={index}:tracking_code={result.tracking_code}:"
                            f"petition_number={result.petition_number}:"
                            f"cause={detail}"
                        ) from exc
                else:
                    _report(
                        progress,
                        f"[ردیف {index} از {len(drafts)}] پیوستی انتخاب نشده؛ "
                        "مرحله پیوست رد شد.",
                    )
            return tuple(results)
        finally:
            try:
                if page is not None:
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
