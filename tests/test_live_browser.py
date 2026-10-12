import unittest

from sana_declaration_uploader.live_browser import (
    LiveBrowserError,
    LoginCredentials,
    PlaywrightDeclarationPage,
    PlaywrightContentPage,
    PlaywrightSubmissionPage,
    ReusableLoginSession,
    extract_tracking_code,
    login_self_service,
    run_authenticated_batch,
)
from sana_declaration_uploader.models import DeclarationDraft
from pathlib import Path


class _Locator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def fill(self, value):
        self.page.calls.append(("fill", self.selector, value))

    def click(self):
        self.page.calls.append(("click", self.selector))
        if self.selector == "#frmCheckOTP button":
            self.page.authenticated = True

    def wait_for(self, **kwargs):
        self.page.calls.append(("wait_for", self.selector, kwargs["state"]))

    def is_visible(self):
        if self.selector == "#menu4Container":
            return self.page.authenticated
        return self.selector == "#chkAgreement"

    def is_checked(self):
        return False


class _Page:
    def __init__(self):
        self.calls = []
        self.authenticated = False

    def goto(self, url, **kwargs):
        self.calls.append(("goto", url))

    def locator(self, selector):
        return _Locator(self, selector)


class LiveBrowserLoginTests(unittest.TestCase):
    def test_login_uses_observed_two_step_flow_once(self):
        page = _Page()
        credentials = {
            "personNationalCode": "national",
            "personUserPassword": "password",
            "legalIdentity": "identity",
            "otpPassowrd": "otp",
        }

        login_self_service(page, credentials)

        self.assertEqual(
            [call[0:2] for call in page.calls],
            [
                ("goto", "https://person.adliran.ir/LegalPerson/Index"),
                ("fill", "#personNationalCode"),
                ("fill", "#personUserPassword"),
                ("fill", "#legalIdentity"),
                ("click", "#frmAuthenticate button"),
                (
                    "wait_for",
                    "#otpPassowrd, #otpPassword, #otp, input[placeholder*='کد']",
                ),
                (
                    "fill",
                    "#otpPassowrd, #otpPassword, #otp, input[placeholder*='کد']",
                ),
                ("click", "#frmCheckOTP button"),
                ("wait_for", "#menu4Container"),
            ],
        )

    def test_missing_secret_stops_before_browser_activity(self):
        page = _Page()
        credentials = {
            "personNationalCode": "national",
            "personUserPassword": "password",
            "legalIdentity": "identity",
            "otpPassowrd": "",
        }

        with self.assertRaisesRegex(LiveBrowserError, "login_credentials_incomplete"):
            login_self_service(page, credentials)

        self.assertEqual(page.calls, [])

    def test_batch_reuses_login_and_processes_each_item_once(self):
        page = _Page()
        session = ReusableLoginSession(page)
        provider_calls = []
        processed = []

        def credentials_provider():
            provider_calls.append(True)
            return LoginCredentials("national", "password", "identity", "otp")

        run_authenticated_batch(
            ("first", "second"),
            session,
            credentials_provider,
            lambda item, _page: processed.append(item),
        )

        self.assertEqual(len(provider_calls), 1)
        self.assertEqual(processed, ["first", "second"])

    def test_batch_reauthenticates_before_next_item_after_session_loss(self):
        page = _Page()
        session = ReusableLoginSession(page)
        provider_calls = []

        def credentials_provider():
            provider_calls.append(True)
            return LoginCredentials("national", "password", "identity", "new-otp")

        def process(item, current_page):
            if item == "first":
                current_page.authenticated = False

        run_authenticated_batch(
            ("first", "second"), session, credentials_provider, process
        )

        self.assertEqual(len(provider_calls), 2)

    def test_batch_never_retries_an_item_after_ambiguous_failure(self):
        page = _Page()
        session = ReusableLoginSession(page)
        processed = []

        def process(item, _page):
            processed.append(item)
            raise LiveBrowserError("site_response_ambiguous")

        with self.assertRaisesRegex(LiveBrowserError, "site_response_ambiguous"):
            run_authenticated_batch(
                ("first", "second"),
                session,
                lambda: LoginCredentials("national", "password", "identity", "otp"),
                process,
            )

        self.assertEqual(processed, ["first"])


class DeclarationPageTests(unittest.TestCase):
    def test_opens_wizard_and_stops_before_recipient_selection(self):
        page = _Page()
        draft = DeclarationDraft(
            source_dir=Path("input"),
            fields={
                "court_unit_code": "12345",
                "related_declaration_number": "98765",
            },
        )

        PlaywrightDeclarationPage(page).open_draft_wizard(draft)

        self.assertEqual(
            [call[0:2] for call in page.calls],
            [
                ("click", "#menu4Container"),
                ("click", "#rdbSetPetition"),
                ("click", "#chkAgreement"),
                ("click", "#frmSetPetitions button"),
                ("wait_for", "#txtUnitCode"),
                ("fill", "#txtUnitCode"),
                ("click", "#newPetition"),
                ("wait_for", "#replayToDeclaration"),
                ("fill", "#replayToDeclaration"),
                ("click", "#btnNext"),
                ("wait_for", "#legalIranianPerson"),
            ],
        )
        self.assertNotIn(("click", "#legalIranianPerson"), page.calls)

    def test_missing_court_code_stops_before_browser_activity(self):
        page = _Page()
        draft = DeclarationDraft(source_dir=Path("input"), fields={})

        with self.assertRaisesRegex(LiveBrowserError, "court_unit_code_missing"):
            PlaywrightDeclarationPage(page).open_draft_wizard(draft)

        self.assertEqual(page.calls, [])


class SubmissionPageTests(unittest.TestCase):
    def test_final_click_requires_action_time_confirmation(self):
        page = _Page()

        with self.assertRaisesRegex(
            LiveBrowserError, "final_submission_confirmation_required"
        ):
            PlaywrightSubmissionPage(page).submit_signature()

        self.assertEqual(page.calls, [])

    def test_extracts_one_labeled_tracking_code_and_normalizes_digits(self):
        self.assertEqual(
            extract_tracking_code("کد رهگیری: ۱۴۰۵۲۲۰۰۰۰۰۰۰۰۰۱"),
            "1405220000000001",
        )

    def test_rejects_ambiguous_tracking_codes(self):
        with self.assertRaisesRegex(LiveBrowserError, "tracking_code_not_unique"):
            extract_tracking_code(
                "کد رهگیری: 1405220000000001 / کد رهگیری: 1405220000000002"
            )


class ContentPageTests(unittest.TestCase):
    def test_empty_subject_stops_before_browser_activity(self):
        page = _Page()

        with self.assertRaisesRegex(LiveBrowserError, "declaration_subject_missing"):
            PlaywrightContentPage(page).select_subject("  ")

        self.assertEqual(page.calls, [])


if __name__ == "__main__":
    unittest.main()
