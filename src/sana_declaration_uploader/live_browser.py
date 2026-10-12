"""Minimal Playwright boundary for the observed self-service portal flow."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import re
from typing import TypeVar

from .models import Attachment, DeclarationDraft


LOGIN_URL = "https://person.adliran.ir/LegalPerson/Index"
OTP_SELECTOR = "#otpPassowrd, #otpPassword, #otp, input[placeholder*='کد']"
REQUIRED_CREDENTIALS = (
    "personNationalCode",
    "personUserPassword",
    "legalIdentity",
    "otpPassowrd",
)


class LiveBrowserError(RuntimeError):
    """Fail-closed live-browser error with no sensitive page content."""


@dataclass(frozen=True)
class LoginCredentials:
    national_code: str
    password: str
    legal_identity: str
    otp: str

    def as_portal_fields(self) -> dict[str, str]:
        return {
            "personNationalCode": self.national_code,
            "personUserPassword": self.password,
            "legalIdentity": self.legal_identity,
            "otpPassowrd": self.otp,
        }


_BatchItem = TypeVar("_BatchItem")


class ReusableLoginSession:
    """Reuse one authenticated page and reauthenticate only between items."""

    def __init__(self, page: object, *, timeout_ms: int = 60_000) -> None:
        self.page = page
        self.timeout_ms = timeout_ms

    def ensure_authenticated(
        self, credentials_provider: Callable[[], LoginCredentials | None]
    ) -> None:
        try:
            if self.page.locator("#menu4Container").is_visible():
                return
        except Exception:
            pass
        credentials = credentials_provider()
        if credentials is None:
            raise LiveBrowserError("session_reauthentication_required")
        login_self_service(
            self.page,
            credentials.as_portal_fields(),
            timeout_ms=self.timeout_ms,
        )


def run_authenticated_batch(
    items: Iterable[_BatchItem],
    session: ReusableLoginSession,
    credentials_provider: Callable[[], LoginCredentials | None],
    process_item: Callable[[_BatchItem, object], None],
) -> None:
    """Process each item once, reauthenticating only before a new item.

    An error during ``process_item`` is never retried because the remote write
    state may be ambiguous. The caller may resume only after operator review.
    """

    for item in items:
        session.ensure_authenticated(credentials_provider)
        process_item(item, session.page)


def login_self_service(
    page: object,
    credentials: Mapping[str, str],
    *,
    timeout_ms: int = 60_000,
) -> None:
    """Log in exactly once and verify the authenticated declaration menu.

    No retry is attempted after timeout, an ambiguous response, or a portal
    error. Callers own the browser/context lifecycle.
    """

    missing = [name for name in REQUIRED_CREDENTIALS if not credentials.get(name)]
    if missing:
        raise LiveBrowserError("login_credentials_incomplete")

    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    page.locator("#personNationalCode").fill(credentials["personNationalCode"])
    page.locator("#personUserPassword").fill(credentials["personUserPassword"])
    page.locator("#legalIdentity").fill(credentials["legalIdentity"])
    page.locator("#frmAuthenticate button").click()

    otp = page.locator(OTP_SELECTOR).first
    try:
        otp.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:
        raise LiveBrowserError(_login_failure_code(page)) from exc
    otp.fill(credentials["otpPassowrd"])
    page.locator("#frmCheckOTP button").click()

    try:
        page.locator("#menu4Container").wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:
        raise LiveBrowserError(_login_failure_code(page)) from exc


def run_login_check(
    credentials: LoginCredentials,
    *,
    headless: bool = False,
    timeout_ms: int = 60_000,
) -> None:
    """Open installed Chrome, verify login, then close without site writes."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LiveBrowserError(
            "playwright_not_installed: install the project with [browser]"
        ) from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=headless)
        context = browser.new_context()
        try:
            page = context.new_page()
            login_self_service(
                page, credentials.as_portal_fields(), timeout_ms=timeout_ms
            )
        finally:
            context.close()
            browser.close()


class PlaywrightAttachmentPage:
    """Observed attachment flow; the caller navigates to «منضمات» first."""

    def __init__(self, page: object, *, timeout_ms: int = 60_000) -> None:
        self.page = page
        self.timeout_ms = timeout_ms

    def register_attachment(self, attachment: Attachment) -> None:
        page = self.page
        page.locator("#attachmentType").select_option(label=attachment.kind)
        page.locator("#txtNo").fill(attachment.document_number)
        page.locator("#txtIssueDate input").fill(attachment.document_date)
        checkbox = page.locator("#txtIsShowDocument")
        if checkbox.is_checked() != attachment.show_to_recipient:
            checkbox.click()
        page.locator("#incAttach0").click()
        self._close_success_dialog()

    def upload_attachment_file(self, attachment: Attachment) -> None:
        page = self.page
        page.locator("#files_multipleFileUploader").set_input_files(
            str(attachment.path)
        )
        page.locator("#btnSaveDoc").click()
        self._close_success_dialog()

    def confirm_attachment_authenticity(self, attachment: Attachment) -> None:
        rows = self.page.locator("#frmAttachment table tbody tr")
        row = rows.filter(has_text=attachment.title)
        if row.count() != 1:
            raise LiveBrowserError(
                f"attachment_row_not_unique:{attachment.path.name}"
            )
        row.get_by_role("button").first.click()
        apply_button = self.page.locator("#btnApplyNo0")
        apply_button.wait_for(state="visible", timeout=self.timeout_ms)
        apply_button.click()

    def _close_success_dialog(self) -> None:
        _close_success_dialog(self.page, self.timeout_ms)


class PlaywrightDeclarationPage:
    """Navigate the observed new-declaration wizard without saving it."""

    def __init__(self, page: object, *, timeout_ms: int = 60_000) -> None:
        self.page = page
        self.timeout_ms = timeout_ms

    def open_draft_wizard(self, draft: DeclarationDraft) -> None:
        """Open the recipient-type step using only confirmed stable fields.

        This method does not select a recipient type, save a draft, calculate
        charges, sign, or submit anything.
        """

        court_code = draft.fields.get("court_unit_code", "").strip()
        if not court_code:
            raise LiveBrowserError("court_unit_code_missing")

        page = self.page
        page.locator("#menu4Container").click()
        page.locator("#rdbSetPetition").click()
        agreement = page.locator("#chkAgreement")
        if agreement.is_visible():
            if not agreement.is_checked():
                agreement.click()
            page.locator("#frmSetPetitions button").click()

        unit_code = page.locator("#txtUnitCode")
        unit_code.wait_for(state="visible", timeout=self.timeout_ms)
        unit_code.fill(court_code)
        page.locator("#newPetition").click()

        related_number = page.locator("#replayToDeclaration")
        related_number.wait_for(state="visible", timeout=self.timeout_ms)
        value = draft.fields.get("related_declaration_number", "").strip()
        if value:
            related_number.fill(value)
        page.locator("#btnNext").click()
        page.locator("#legalIranianPerson").wait_for(
            state="visible", timeout=self.timeout_ms
        )


class PlaywrightSubmissionPage:
    """Observed payment-readiness and final-signature boundary."""

    def __init__(self, page: object, *, timeout_ms: int = 60_000) -> None:
        self.page = page
        self.timeout_ms = timeout_ms

    def verify_ready_for_signature(self) -> None:
        """Ask the portal to validate the draft; do not sign it."""

        self.page.locator("#btnCalculateCash").click()
        dialog = self._visible_dialog()
        text = dialog.inner_text()
        if not any(marker in text for marker in ("تایید اطلاعات", "موفق")):
            raise LiveBrowserError("draft_not_ready_for_signature")
        dialog.locator("button.confirm").click()

    def submit_signature(self, *, final_confirmed: bool = False) -> str:
        """Perform the irreversible signature click and return tracking code."""

        if not final_confirmed:
            raise LiveBrowserError("final_submission_confirmation_required")

        rows = self.page.locator("#frmSignature table tbody tr")
        if rows.count() != 1:
            raise LiveBrowserError("signature_row_not_unique")
        rows.first.get_by_role("button").click()

        dialog = self._visible_dialog()
        if "اظهارنامه با موفقیت ثبت شد" not in dialog.inner_text():
            raise LiveBrowserError("final_submission_not_verified")
        dialog.locator("button.confirm").click()

        body_text = self.page.locator("body").inner_text(timeout=self.timeout_ms)
        return extract_tracking_code(body_text)

    def _visible_dialog(self):
        dialog = self.page.locator(".sweet-alert:visible")
        try:
            dialog.wait_for(state="visible", timeout=self.timeout_ms)
        except Exception as exc:
            raise LiveBrowserError("site_response_ambiguous") from exc
        return dialog


class PlaywrightContentPage:
    """Observed subject, evidence and description steps of the wizard."""

    def __init__(self, page: object, *, timeout_ms: int = 60_000) -> None:
        self.page = page
        self.timeout_ms = timeout_ms

    def select_subject(self, subject: str) -> None:
        subject = subject.strip()
        if not subject:
            raise LiveBrowserError("declaration_subject_missing")
        page = self.page
        page.locator("#btnAddSection").click()
        control = page.locator("#jssPetitionRelief1")
        control.locator("input").first.fill(subject)
        choices = control.locator(".ui-select-choices-row").filter(has_text=subject)
        if choices.count() != 1:
            raise LiveBrowserError("declaration_subject_not_unique")
        if choices.first.inner_text().strip() != subject:
            raise LiveBrowserError("declaration_subject_not_exact")
        choices.first.click()
        page.locator("#btnNext").click()

    def select_other_evidence_and_save(self) -> None:
        checkbox = self.page.locator("#chk7")
        if not checkbox.is_checked():
            checkbox.click()
        self.page.locator("#btnSave").click()
        _close_success_dialog(self.page, self.timeout_ms)

    def fill_description_and_save(self, description: str) -> None:
        description = description.strip()
        if not description:
            raise LiveBrowserError("declaration_text_missing")
        textareas = self.page.locator("textarea[id^='taTextElement']")
        if textareas.count() != 1:
            raise LiveBrowserError("declaration_textarea_not_unique")
        textareas.first.fill(description)
        self.page.locator("#btnNext").click()
        self.page.locator("#btnSave").click()
        _close_success_dialog(self.page, self.timeout_ms)


_TRACKING_LABEL = re.compile(
    r"کد\s*رهگیری\s*[:：]?\s*([۰-۹0-9]{12,24})"
)
_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def extract_tracking_code(text: str) -> str:
    matches = {
        match.translate(_PERSIAN_DIGITS) for match in _TRACKING_LABEL.findall(text)
    }
    if len(matches) != 1:
        raise LiveBrowserError("tracking_code_not_unique")
    return matches.pop()


def _close_success_dialog(page: object, timeout_ms: int) -> None:
    dialog = page.locator(".sweet-alert:visible")
    try:
        dialog.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:
        raise LiveBrowserError("site_response_ambiguous") from exc
    if not any(marker in dialog.inner_text() for marker in ("موفقیت", "موفق", "ثبت شد")):
        raise LiveBrowserError("site_operation_rejected")
    dialog.locator("button.confirm").click()


def _login_failure_code(page: object) -> str:
    try:
        text = page.locator("body").inner_text(timeout=2_000)
    except Exception:
        return "login_response_ambiguous"
    if "همزمان" in text or "هم‌زمان" in text or "اعتبار دسترسی شما پایان" in text:
        return "concurrent_login"
    if "رمز موقت" in text or "کد امنیتی" in text:
        return "login_rejected"
    return "login_not_verified"
