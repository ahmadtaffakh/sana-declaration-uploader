"""One-item SetPetitionData pilot kept separate from normal upload flow."""

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
from .models import DeclarationDraft
from .petition_api import (
    SetPetitionDataResult,
    build_setpetitiondata_payload,
    save_setpetitiondata_debug_text,
    submit_one_petition_data,
)


def run_live_setpetitiondata_test(
    draft: DeclarationDraft,
    credentials: LoginCredentials,
    confirm: Callable[[], bool],
    petition_template: Mapping[str, object],
    request_text_path: Path,
    *,
    headless: bool = False,
    timeout_ms: int = 60_000,
    progress: ProgressCallback | None = None,
    close_browser_event: Event,
) -> SetPetitionDataResult | None:
    """Log in and submit one template-based temporary save without form navigation."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LiveBrowserError(
            "playwright_not_installed: install the project with [browser]"
        ) from exc

    _validate_live_draft(replace(draft, attachments=()))
    petition_data = build_setpetitiondata_payload(petition_template, draft)
    body_path, command_path = save_setpetitiondata_debug_text(
        petition_data, request_text_path
    )
    _report(
        progress,
        f"متن درخواست برای خطایابی: {body_path}\n"
        f"دستور curl دستی: {command_path}\n"
        f"کد اجرای دستی در کنسول مرورگر: "
        f"{body_path.with_name(f'{body_path.stem}.console.txt')}",
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
            return _run_logged_stage(
                progress,
                "ثبت مستقیم و موقت با SetPetitionData",
                lambda: (
                    submit_one_petition_data(page, petition_data)
                    if confirm()
                    else None
                ),
            )
        finally:
            try:
                if page is not None:
                    _report(progress, "__api_browser_ready_to_close__")
                    close_browser_event.wait()
            finally:
                context.close()
                browser.close()
