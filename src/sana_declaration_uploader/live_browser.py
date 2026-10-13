"""Minimal Playwright boundary for the observed self-service portal flow."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import re
from typing import TypeVar

from .browser import run_attachment_workflow, validate_attachments_for_upload
from .models import Attachment, DeclarationDraft, UploadResult
from .validator import validate_draft


LOGIN_URL = "https://person.adliran.ir/LegalPerson/Index"
OTP_SELECTOR = "#otpPassowrd, #otpPassword, #otp, input[placeholder*='کد']"
MINISTRY_COURT_UNIT_CODE = "00006"
ERROR_INSPECTION_MINUTES = 5
ERROR_INSPECTION_INTERVAL_MS = 60_000
PAGE_LOADING_TIMEOUT_MS = 5 * 60_000
LOADER_APPEARANCE_TIMEOUT_MS = 1_000
NATURAL_PERSON_TAB_SELECTOR = (
    "div.margin-bottom-5 > div > div > div:nth-of-type(1) "
    "div:nth-of-type(3) > div > div > div"
)
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
ProgressCallback = Callable[[str], None]


def _report(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _run_logged_stage(
    progress: ProgressCallback | None,
    title: str,
    action: Callable[[], _BatchItem],
) -> _BatchItem:
    _report(progress, f"شروع مرحله: {title}")
    try:
        result = action()
    except Exception as exc:
        detail = str(exc) if isinstance(exc, LiveBrowserError) else type(exc).__name__
        _report(progress, f"پایان ناموفق مرحله: {title}؛ علت: {detail}")
        raise
    _report(progress, f"پایان موفق مرحله: {title}")
    return result


def _hold_browser_for_error_inspection(
    page: object,
    progress: ProgressCallback | None,
) -> None:
    for remaining in range(ERROR_INSPECTION_MINUTES, 0, -1):
        _report(
            progress,
            "اجرای مرورگر متوقف شده است؛ "
            f"صفحه برای بررسی داده‌ها باز می‌ماند (زمان باقی‌مانده: {remaining} دقیقه).",
        )
        try:
            page.wait_for_timeout(ERROR_INSPECTION_INTERVAL_MS)
        except Exception:
            return
    _report(progress, "مهلت بررسی پایان یافت؛ مرورگر بسته می‌شود.")


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
    before_item: Callable[[_BatchItem], None] | None = None,
) -> None:
    """Process each item once, reauthenticating only before a new item.

    An error during ``process_item`` is never retried because the remote write
    state may be ambiguous. The caller may resume only after operator review.
    """

    for item in items:
        if before_item is not None:
            before_item(item)
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


def run_live_upload_batch(
    drafts: Iterable[DeclarationDraft],
    credentials_provider: Callable[[], LoginCredentials | None],
    final_confirmation: Callable[[DeclarationDraft], bool],
    *,
    headless: bool = False,
    timeout_ms: int = 60_000,
    progress: ProgressCallback | None = None,
) -> tuple[UploadResult, ...]:
    """Open one Chrome context and execute the reviewed batch exactly once."""

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
            try:
                return upload_authenticated_batch(
                    page,
                    drafts,
                    credentials_provider,
                    final_confirmation,
                    timeout_ms=timeout_ms,
                    progress=progress,
                )
            except Exception:
                _hold_browser_for_error_inspection(page, progress)
                raise
        finally:
            context.close()
            browser.close()


class PlaywrightAttachmentPage:
    """Observed attachment flow; the caller navigates to «منضمات» first."""

    def __init__(self, page: object, *, timeout_ms: int = 60_000) -> None:
        self.page = page
        self.timeout_ms = timeout_ms

    def open_attachments(self) -> None:
        steps = self.page.locator(
            "div[ng-click*='navCtrl.onStepSelect'] .step"
        ).filter(has_text="منضمات")
        if steps.count() != 1:
            raise LiveBrowserError("attachments_step_not_unique")
        steps.click()
        self.page.locator("#frmAttachment").wait_for(
            state="visible", timeout=self.timeout_ms
        )

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
        apply_button.wait_for(state="hidden", timeout=self.timeout_ms)

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

        page = self.page
        page.locator("#menu4Container").click()
        page.locator("#rdbSetPetition").wait_for(
            state="visible", timeout=self.timeout_ms
        )
        page.locator("#rdbSetPetition").click()
        page.wait_for_function(
            """() => {
                const visible = element => Boolean(element)
                    && element.offsetParent !== null;
                return visible(document.querySelector('#chkAgreement'))
                    || visible(document.querySelector('#txtUnitCode'));
            }""",
            timeout=self.timeout_ms,
        )
        agreement = page.locator("#chkAgreement")
        if agreement.is_visible():
            if not agreement.is_checked():
                agreement.click()
            page.locator("#frmSetPetitions button").click()

        court_unit_code = page.locator("#txtUnitCode")
        court_unit_code.wait_for(state="visible", timeout=self.timeout_ms)
        court_unit_code.fill(MINISTRY_COURT_UNIT_CODE)
        court_unit_code.press("Tab")
        try:
            page.wait_for_function(
                """() => {
                    const name = document.querySelector('#txtUnitName');
                    const button = document.querySelector('#newPetition');
                    return Boolean(name?.value?.trim())
                        && button
                        && !button.disabled
                        && !button.classList.contains('disabled');
                }""",
                timeout=self.timeout_ms,
            )
        except Exception as exc:
            raise LiveBrowserError("ministry_court_unit_not_resolved") from exc
        page.locator("#newPetition").click()

        edit_step = page.locator(
            "//div[contains(@class, 'main-step')]"
            "//h5[normalize-space()='ثبت و اصلاح اظهارنامه']"
        )
        try:
            edit_step.wait_for(state="visible", timeout=self.timeout_ms)
        except Exception as exc:
            raise LiveBrowserError("declaration_edit_step_not_unique") from exc
        edit_step.click()
        page.locator("#btnNext").wait_for(
            state="visible", timeout=self.timeout_ms
        )

        value = draft.fields.get("related_declaration_number", "").strip()
        if value:
            page.locator("#rdbReplayToDeclaration").click()
            related_number = page.locator("#replayToDeclaration")
            related_number.wait_for(state="visible", timeout=self.timeout_ms)
            related_number.fill(value)
        page.locator("#btnNext").click()
        page.locator(NATURAL_PERSON_TAB_SELECTOR).wait_for(
            state="visible", timeout=self.timeout_ms
        )


class PlaywrightRecipientPage:
    """Fill one observed Iranian natural-person recipient form."""

    REQUIRED_FIELDS = (
        "recipient_first_name",
        "recipient_last_name",
        "job_title",
        "job_id",
        "job_code",
        "province",
        "province_id",
        "province_code",
        "county",
        "county_id",
        "county_code",
        "city",
        "city_id",
        "city_code",
        "recipient_main_street",
    )
    GENDER_SELECTORS = {
        "مرد": "#rdbRealIrMale",
        "مذکر": "#rdbRealIrMale",
        "زن": "#rdbRealIrFemale",
        "مونث": "#rdbRealIrFemale",
    }

    def __init__(self, page: object, *, timeout_ms: int = 60_000) -> None:
        self.page = page
        self.timeout_ms = timeout_ms

    def fill_natural_person(self, draft: DeclarationDraft) -> None:
        self.validate(draft)
        fields = draft.fields

        page = self.page
        natural_person_tab = page.locator(NATURAL_PERSON_TAB_SELECTOR)
        if natural_person_tab.count() != 1:
            raise LiveBrowserError("natural_person_tab_not_unique")
        try:
            natural_person_tab.click(timeout=self.timeout_ms)
        except Exception as exc:
            raise LiveBrowserError("natural_person_tab_navigation_failed") from exc

        add_button = page.locator("#btnAddSection")
        _wait_for_transition(
            page,
            self.timeout_ms,
            ready_selector="#btnAddSection",
            failure_code="page_load_not_completed:#btnAddSection",
        )
        add_button.click()
        _wait_for_transition(
            page,
            self.timeout_ms,
            ready_selector="#txtRealIrName",
            failure_code="page_load_not_completed:#txtRealIrName",
        )
        national_id = fields.get("recipient_national_id", "").strip()
        if national_id:
            page.locator("#txtRealIrNationalityCode1").fill(national_id)
        birth_date = fields.get("recipient_birth_date", "").strip()
        if birth_date:
            page.locator("input[name='RealIrBirthDate']").fill(birth_date)
        gender = fields.get("recipient_gender", "").strip()
        if gender:
            self._select_gender(gender)
        page.locator("#txtRealIrName").fill(fields["recipient_first_name"])
        page.locator("#txtRealIrFamily").fill(fields["recipient_last_name"])
        father_name = fields.get("recipient_father_name", "").strip()
        if father_name:
            page.locator("#txtRealIrFatherName").fill(father_name)

        try:
            selected = page.evaluate(
                _SELECT_RECIPIENT_LOOKUPS,
                {
                    "job": _lookup_payload(fields, "job", "job_title", None),
                    "province": _lookup_payload(fields, "province", "province", 2),
                    "county": _lookup_payload(fields, "county", "county", 3),
                    "city": _lookup_payload(fields, "city", "city", 5),
                },
            )
        except Exception as exc:
            raise LiveBrowserError("recipient_lookup_selection_failed") from exc
        expected = {
            "job": fields["job_title"].strip(),
            "province": fields["province"].strip(),
            "county": fields["county"].strip(),
            "city": fields["city"].strip(),
        }
        if selected != expected:
            raise LiveBrowserError("recipient_lookup_selection_not_verified")

        page.locator("#txtBasicAvenue").fill(fields["recipient_main_street"])
        address = fields.get("recipient_address", "").strip()
        if address:
            page.locator(
                "textarea[ng-model='viewModel.currentPetitionPerson.HomeAddress']"
            ).fill(address)
        _open_form_step(
            page,
            "موضوع اظهارنامه",
            "#btnAddSection",
            self.timeout_ms,
        )

    def validate(self, draft: DeclarationDraft) -> None:
        fields = draft.fields
        person_type = fields.get("person_type", "حقیقی").strip()
        if person_type not in {"", "حقیقی", "شخص حقیقی"}:
            raise LiveBrowserError("recipient_person_type_not_supported")
        missing = [
            name for name in self.REQUIRED_FIELDS
            if (
                fields.get(name, "") == ""
                if name == "recipient_main_street"
                else not fields.get(name, "").strip()
            )
        ]
        if missing:
            raise LiveBrowserError(f"recipient_fields_incomplete:{','.join(missing)}")
        gender = fields.get("recipient_gender", "").strip()
        if gender and gender not in self.GENDER_SELECTORS:
            raise LiveBrowserError("recipient_gender_unsupported")

    def _select_gender(self, gender: str) -> None:
        normalized = gender.strip()
        selector = self.GENDER_SELECTORS.get(normalized)
        if selector is None:
            raise LiveBrowserError("recipient_gender_unsupported")
        self.page.locator(selector).click()

_SELECT_RECIPIENT_LOOKUPS = """async payload => {
    const containers = document.querySelectorAll('.ui-select-container');
    const container = name => document.querySelector(
        `.ui-select-container[name="${name}"]`
    );
    const selectScope = name => {
        const node = container(name);
        if (!node) throw new Error(`lookup_container_missing:${name}`);
        const scope = angular.element(node).scope();
        if (!scope || !scope.$select) {
            throw new Error(`lookup_scope_missing:${name}`);
        }
        return scope;
    };
    const waitFor = async (predicate, code) => {
        const deadline = Date.now() + 30000;
        while (Date.now() < deadline) {
            const value = predicate();
            if (value) return value;
            await new Promise(resolve => setTimeout(resolve, 100));
        }
        throw new Error(code);
    };
    const choose = (name, value) => {
        const scope = selectScope(name);
        scope.$apply(() => scope.$select.select(value, false));
    };
    const findById = (items, id) => Array.isArray(items)
        ? items.find(item => String(item.Id || '') === String(id || ''))
        : null;
    const result = {};

    const jobContainer = containers[0];
    if (!jobContainer) throw new Error('lookup_container_missing:job');
    const jobScope = angular.element(jobContainer).scope();
    if (!jobScope || !jobScope.$select) {
        throw new Error('lookup_scope_missing:job');
    }
    jobScope.$apply(() => jobScope.$select.select(
        {Id: payload.job.id, Title: payload.job.label, Code: payload.job.code},
        false
    ));
    const jobMatch = jobContainer.querySelector('.ui-select-match-text');
    result.job = jobMatch ? jobMatch.textContent.trim() : '';

    const stateScope = selectScope('state');
    const viewModel = stateScope.viewModel;
    if (!viewModel || !viewModel.currentPetitionPerson) {
        throw new Error('recipient_model_missing');
    }
    choose('state', {
        Id: payload.province.id,
        LocationName: payload.province.label,
        LocationCode: payload.province.code,
        LocationType: payload.province.type,
    });
    const county = await waitFor(
        () => findById(viewModel.theProvinceTitleList, payload.county.id),
        'lookup_children_missing:county'
    );
    choose('province', county);
    const city = await waitFor(
        () => findById(viewModel.theCityTitleList, payload.city.id),
        'lookup_children_missing:city'
    );
    choose('city', city);

    for (const [name, control] of [
        ['province', 'state'],
        ['county', 'province'],
        ['city', 'city'],
    ]) {
        const match = container(control).querySelector('.ui-select-match-text');
        result[name] = match ? match.textContent.trim() : '';
    }
    return result;
}"""


def _open_form_step(
    page: object,
    title: str,
    ready_selector: str,
    timeout_ms: int,
) -> None:
    step = page.locator(
        "//*[@ng-repeat='step in navCtrl.currentStep.innerSteps']"
        "[.//*[contains(concat(' ', normalize-space(@class), ' '), ' step ') "
        f"and normalize-space()='{title}']]"
    )
    _wait_for_transition(
        page,
        timeout_ms,
        step_title=title,
        failure_code=f"form_step_not_visible:{title}",
    )
    if step.count() != 1:
        raise LiveBrowserError(f"form_step_not_unique:{title}")

    try:
        step.locator(".step.ng-binding").click()
        page.wait_for_function(
            r"""title => {
                const normalize = value => (value || '')
                    .replace(/\u0643/g, '\u06a9')
                    .replace(/[\u064a\u0649]/g, '\u06cc')
                    .replace(/\s+/g, ' ')
                    .trim();
                return [...document.querySelectorAll(
                    '[ng-repeat="step in navCtrl.currentStep.innerSteps"]'
                )].some(item => {
                    const label = item.querySelector('.step.ng-binding');
                    const status = item.querySelector(
                        '[jud-static-include] > div'
                    );
                    return normalize(label?.textContent) === normalize(title)
                        && status?.classList.contains('hover');
                });
            }""",
            arg=title,
            timeout=timeout_ms,
        )
        page.locator(ready_selector).wait_for(
            state="visible", timeout=timeout_ms
        )
    except Exception as exc:
        raise LiveBrowserError(f"form_step_navigation_failed:{title}") from exc


def _wait_for_transition(
    page: object,
    timeout_ms: int,
    *,
    ready_selector: str | None = None,
    step_title: str | None = None,
    failure_code: str,
) -> None:
    loader = page.locator(".blockUI:visible").first
    try:
        loader.wait_for(state="visible", timeout=LOADER_APPEARANCE_TIMEOUT_MS)
    except Exception:
        pass
    else:
        try:
            loader.wait_for(state="hidden", timeout=PAGE_LOADING_TIMEOUT_MS)
        except Exception as exc:
            raise LiveBrowserError("page_loading_timeout") from exc

    try:
        page.wait_for_function(
            r"""target => {
                const visible = element => Boolean(element)
                    && element.getClientRects().length > 0
                    && getComputedStyle(element).visibility !== 'hidden';
                if ([...document.querySelectorAll('.blockUI')].some(visible)) {
                    return false;
                }
                if (visible(document.querySelector('.sweet-alert'))) return true;
                if (target.readySelector) {
                    return visible(document.querySelector(target.readySelector));
                }
                const normalize = value => (value || '')
                    .replace(/\u0643/g, '\u06a9')
                    .replace(/[\u064a\u0649]/g, '\u06cc')
                    .replace(/\s+/g, ' ')
                    .trim();
                return [...document.querySelectorAll(
                    '[ng-repeat="step in navCtrl.currentStep.innerSteps"]'
                )].some(item => {
                    const label = item.querySelector('.step.ng-binding');
                    return visible(item)
                        && normalize(label?.textContent)
                            === normalize(target.stepTitle);
                });
            }""",
            arg={"readySelector": ready_selector, "stepTitle": step_title},
            timeout=timeout_ms,
        )
    except Exception as exc:
        raise LiveBrowserError(failure_code) from exc
    _raise_visible_site_error(page)


def _raise_visible_site_error(page: object) -> None:
    dialog = page.locator(".sweet-alert:visible")
    try:
        if not dialog.is_visible():
            return
        message = dialog.inner_text(timeout=2_000)
    except Exception as exc:
        raise LiveBrowserError("site_response_ambiguous") from exc
    if "اقامتگاه" in message:
        raise LiveBrowserError("site_validation_rejected:recipient_residence_incomplete")
    raise LiveBrowserError("site_operation_rejected")


def _lookup_payload(
    fields: Mapping[str, str], prefix: str, label_field: str, location_type: int | None
) -> dict[str, str | int]:
    payload: dict[str, str | int] = {
        "id": fields[f"{prefix}_id"].strip(),
        "code": fields[f"{prefix}_code"].strip(),
        "label": fields[label_field].strip(),
    }
    if location_type is not None:
        payload["type"] = location_type
    return payload


class PlaywrightSubmissionPage:
    """Observed payment-readiness and final-signature boundary."""

    def __init__(self, page: object, *, timeout_ms: int = 60_000) -> None:
        self.page = page
        self.timeout_ms = timeout_ms

    def open_signature(self) -> None:
        self.page.locator("#btnSubmit").click()
        self.page.locator("#btnCalculateCash").wait_for(
            state="visible", timeout=self.timeout_ms
        )

    def verify_ready_for_signature(self) -> None:
        """Ask the portal to validate the draft; do not sign it."""

        self.page.locator("#btnCalculateCash").click()
        dialog = self._visible_dialog()
        text = dialog.inner_text()
        if not any(marker in text for marker in ("تایید اطلاعات", "موفق")):
            raise LiveBrowserError("draft_not_ready_for_signature")
        dialog.locator("button.confirm").click()
        dialog.wait_for(state="hidden", timeout=self.timeout_ms)

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
        dialog.wait_for(state="hidden", timeout=self.timeout_ms)

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
        choices = control.locator(".ui-select-choices-row:visible").filter(
            has_text=re.compile(rf"^\s*{re.escape(subject)}\s*$")
        )
        try:
            choices.first.wait_for(state="visible", timeout=self.timeout_ms)
        except Exception as exc:
            raise LiveBrowserError("declaration_subject_not_found") from exc
        if choices.count() != 1:
            raise LiveBrowserError("declaration_subject_not_unique")
        if choices.first.inner_text().strip() != subject:
            raise LiveBrowserError("declaration_subject_not_exact")
        choices.first.click()
        _open_form_step(
            page,
            "شرح اظهارنامه",
            "textarea[id^='taTextElement']",
            self.timeout_ms,
        )

    def fill_description_and_save(self, description: str) -> None:
        description = description.strip()
        if not description:
            raise LiveBrowserError("declaration_text_missing")
        textareas = self.page.locator("textarea[id^='taTextElement']")
        if textareas.count() != 1:
            raise LiveBrowserError("declaration_textarea_not_unique")
        textareas.first.fill(description)
        self.page.locator("#btnSave").wait_for(
            state="visible", timeout=self.timeout_ms
        )
        self.page.locator("#btnSave").click()
        _close_success_dialog(self.page, self.timeout_ms)


class PlaywrightDeclarationUploader:
    """Execute one fully validated draft on an already authenticated page."""

    def __init__(
        self,
        page: object,
        final_confirmation: Callable[[DeclarationDraft], bool],
        *,
        timeout_ms: int = 60_000,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.page = page
        self.final_confirmation = final_confirmation
        self.timeout_ms = timeout_ms
        self.progress = progress

    def upload(self, draft: DeclarationDraft) -> UploadResult:
        _validate_live_draft(draft)
        recipient = PlaywrightRecipientPage(self.page, timeout_ms=self.timeout_ms)
        subject = draft.fields.get("declaration_subject", "").strip()
        description = draft.fields.get("declaration_text", "").strip()

        _run_logged_stage(
            self.progress,
            "بازکردن فرم اظهارنامه",
            lambda: PlaywrightDeclarationPage(
                self.page, timeout_ms=self.timeout_ms
            ).open_draft_wizard(draft),
        )
        _run_logged_stage(
            self.progress,
            "ثبت اطلاعات مخاطب",
            lambda: recipient.fill_natural_person(draft),
        )

        content = PlaywrightContentPage(self.page, timeout_ms=self.timeout_ms)
        _run_logged_stage(
            self.progress,
            "ثبت موضوع اظهارنامه",
            lambda: content.select_subject(subject),
        )
        _run_logged_stage(
            self.progress,
            "ثبت شرح و ذخیره موقت اظهارنامه",
            lambda: content.fill_description_and_save(description),
        )

        if draft.attachments:
            attachment_page = PlaywrightAttachmentPage(
                self.page, timeout_ms=self.timeout_ms
            )
            _run_logged_stage(
                self.progress,
                f"پردازش {len(draft.attachments)} پیوست",
                lambda: (
                    attachment_page.open_attachments(),
                    run_attachment_workflow(draft.attachments, attachment_page),
                ),
            )
        else:
            _report(
                self.progress,
                "مرحله پیوست‌ها رد شد: پیوستی وجود ندارد.",
            )

        submission = PlaywrightSubmissionPage(
            self.page, timeout_ms=self.timeout_ms
        )
        _run_logged_stage(
            self.progress,
            "کنترل آمادگی امضا",
            lambda: (
                submission.open_signature(),
                submission.verify_ready_for_signature(),
            ),
        )
        tracking_code = _run_logged_stage(
            self.progress,
            "ثبت نهایی اظهارنامه",
            lambda: submission.submit_signature(
                final_confirmed=self.final_confirmation(draft)
            ),
        )
        return UploadResult(status="success", tracking_code=tracking_code)


def upload_authenticated_batch(
    page: object,
    drafts: Iterable[DeclarationDraft],
    credentials_provider: Callable[[], LoginCredentials | None],
    final_confirmation: Callable[[DeclarationDraft], bool],
    *,
    timeout_ms: int = 60_000,
    progress: ProgressCallback | None = None,
) -> tuple[UploadResult, ...]:
    """Upload drafts once on one reusable page, reauthenticating between items."""

    draft_list = tuple(drafts)
    results: list[UploadResult | None] = [None] * len(draft_list)
    valid_drafts: list[tuple[int, DeclarationDraft]] = []
    for index, draft in enumerate(draft_list):
        try:
            _validate_live_draft(draft)
        except LiveBrowserError as exc:
            results[index] = UploadResult(status="failed", message=str(exc))
        else:
            valid_drafts.append((index, draft))

    session = ReusableLoginSession(page, timeout_ms=timeout_ms)

    def upload_one(
        item: tuple[int, DeclarationDraft], current_page: object
    ) -> None:
        index, draft = item
        item_progress = lambda message: _report(
            progress, f"[ردیف {index + 1} از {len(draft_list)}] {message}"
        )
        item_progress("نشست ورود تأیید شد؛ اقدام بعدی: بازکردن فرم اظهارنامه")
        try:
            result = PlaywrightDeclarationUploader(
                current_page,
                final_confirmation,
                timeout_ms=timeout_ms,
                progress=item_progress,
            ).upload(draft)
        except LiveBrowserError:
            raise
        except Exception as exc:
            raise LiveBrowserError("site_response_ambiguous") from exc
        results[index] = result

    run_authenticated_batch(
        valid_drafts,
        session,
        credentials_provider,
        upload_one,
        before_item=lambda item: _report(
            progress,
            f"[ردیف {item[0] + 1} از {len(draft_list)}] "
            "اقدام جاری: بررسی نشست ورود",
        ),
    )
    return tuple(result for result in results if result is not None)


def _validate_live_draft(draft: DeclarationDraft) -> None:
    errors = validate_draft(draft)
    if errors:
        raise LiveBrowserError(f"draft_invalid:{','.join(errors)}")
    PlaywrightRecipientPage(None).validate(draft)
    validate_attachments_for_upload(draft.attachments)
    if not draft.fields.get("declaration_subject", "").strip():
        raise LiveBrowserError("declaration_subject_missing")
    if not draft.fields.get("declaration_text", "").strip():
        raise LiveBrowserError("declaration_text_missing")

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
    dialog.wait_for(state="hidden", timeout=timeout_ms)


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
