"""Sequential SetPetitionData pilot kept separate from normal upload flow."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Mapping

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
        _validate_live_draft(replace(draft, attachments=()))
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
