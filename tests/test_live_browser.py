import unittest
from unittest.mock import Mock, patch

from sana_declaration_uploader.live_browser import (
    LiveBrowserError,
    LoginCredentials,
    PlaywrightDeclarationUploader,
    PlaywrightDeclarationPage,
    PlaywrightContentPage,
    PlaywrightRecipientPage,
    PlaywrightSubmissionPage,
    ReusableLoginSession,
    extract_tracking_code,
    login_self_service,
    run_authenticated_batch,
    upload_authenticated_batch,
)
from sana_declaration_uploader.models import DeclarationDraft, UploadResult
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

    def press(self, key):
        self.page.calls.append(("press", self.selector, key))

    def click(self):
        self.page.calls.append(("click", self.selector))
        if self.selector == "#frmCheckOTP button":
            self.page.authenticated = True

    def wait_for(self, **kwargs):
        self.page.calls.append(("wait_for", self.selector, kwargs["state"]))

    def locator(self, selector):
        return _Locator(self.page, f"{self.selector} >> {selector}")

    def get_attribute(self, name):
        self.page.calls.append(("get_attribute", self.selector, name))
        return ""

    def is_visible(self):
        if self.selector == "#menu4Container":
            return self.page.authenticated
        return self.selector == "#chkAgreement"

    def is_checked(self):
        return False

    def filter(self, **kwargs):
        self.page.calls.append(("filter", self.selector, kwargs))
        return self

    def count(self):
        return 1


class _Page:
    def __init__(self):
        self.calls = []
        self.authenticated = False

    def goto(self, url, **kwargs):
        self.calls.append(("goto", url))

    def locator(self, selector):
        return _Locator(self, selector)

    def evaluate(self, script, payload):
        self.calls.append(("evaluate", payload))
        return {
            name: values["label"]
            for name, values in payload.items()
        }

    def wait_for_function(self, _script, **kwargs):
        self.calls.append(
            ("wait_for_function", kwargs["timeout"], kwargs.get("arg"))
        )


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
                ("press", "#txtUnitCode"),
                ("wait_for_function", 60000),
                ("click", "#newPetition"),
                (
                    "wait_for",
                    "//div[contains(@class, 'main-step')]"
                    "//h5[normalize-space()='ثبت و اصلاح اظهارنامه']",
                ),
                (
                    "click",
                    "//div[contains(@class, 'main-step')]"
                    "//h5[normalize-space()='ثبت و اصلاح اظهارنامه']",
                ),
                ("click", "#rdbReplayToDeclaration"),
                ("wait_for", "#replayToDeclaration"),
                ("fill", "#replayToDeclaration"),
                ("click", "#btnNext"),
                ("wait_for", "#legalIranianPerson"),
            ],
        )
        self.assertNotIn(("click", "#legalIranianPerson"), page.calls)

    def test_enters_ministry_court_unit_code_before_opening_wizard(self):
        page = _Page()
        draft = DeclarationDraft(source_dir=Path("input"), fields={})

        PlaywrightDeclarationPage(page).open_draft_wizard(draft)

        self.assertIn(("fill", "#txtUnitCode", "00006"), page.calls)
        self.assertLess(
            page.calls.index(("fill", "#txtUnitCode", "00006")),
            page.calls.index(("click", "#newPetition")),
        )
        self.assertNotIn(("click", "#rdbReplayToDeclaration"), page.calls)
        self.assertFalse(
            any(call[0:2] == ("fill", "#replayToDeclaration") for call in page.calls)
        )

    def test_stops_before_opening_wizard_when_ministry_unit_is_not_resolved(self):
        page = _Page()
        page.wait_for_function = Mock(side_effect=TimeoutError)
        draft = DeclarationDraft(source_dir=Path("input"), fields={})

        with self.assertRaisesRegex(
            LiveBrowserError, "ministry_court_unit_not_resolved"
        ):
            PlaywrightDeclarationPage(page).open_draft_wizard(draft)

        self.assertNotIn(("click", "#newPetition"), page.calls)

    def test_stops_when_declaration_edit_step_does_not_become_visible(self):
        page = _Page()
        original_locator = page.locator
        edit_step = Mock()
        edit_step.wait_for.side_effect = TimeoutError
        page.locator = lambda selector: (
            edit_step
            if selector
            == (
                "//div[contains(@class, 'main-step')]"
                "//h5[normalize-space()='ثبت و اصلاح اظهارنامه']"
            )
            else original_locator(selector)
        )
        draft = DeclarationDraft(source_dir=Path("input"), fields={})

        with self.assertRaisesRegex(
            LiveBrowserError, "declaration_edit_step_not_unique"
        ):
            PlaywrightDeclarationPage(page).open_draft_wizard(draft)

        edit_step.click.assert_not_called()


class RecipientPageTests(unittest.TestCase):
    @staticmethod
    def _draft(**changes):
        fields = {
            "person_type": "حقیقی",
            "recipient_national_id": "0000000000",
            "recipient_birth_date": "1370/01/01",
            "recipient_gender": "مرد",
            "recipient_first_name": "نام",
            "recipient_last_name": "خانوادگی",
            "job_title": "کارمند",
            "job_id": "job-id",
            "job_code": "00001",
            "province": "استان نمونه",
            "province_id": "province-id",
            "province_code": "01",
            "county": "شهرستان نمونه",
            "county_id": "county-id",
            "county_code": "0101",
            "city": "شهر نمونه",
            "city_id": "city-id",
            "city_code": "010101",
            "recipient_main_street": "خیابان نمونه",
            "recipient_address": "نشانی نمونه",
        }
        fields.update(changes)
        return DeclarationDraft(source_dir=Path("input"), fields=fields)

    def test_fills_observed_natural_person_fields_and_verified_lookups(self):
        page = _Page()

        PlaywrightRecipientPage(page).fill_natural_person(self._draft())

        self.assertIn(("fill", "#txtRealIrNationalityCode1", "0000000000"), page.calls)
        self.assertIn(("fill", "#txtRealIrName", "نام"), page.calls)
        self.assertIn(("fill", "#txtRealIrFamily", "خانوادگی"), page.calls)
        self.assertIn(("click", "#rdbRealIrMale"), page.calls)
        self.assertEqual(
            [call for call in page.calls if call[0:2] == ("click", "#btnNext")],
            [
                ("click", "#btnNext"),
                ("click", "#btnNext"),
            ],
        )
        subject_step = (
            "//*[@ng-repeat='step in navCtrl.currentStep.innerSteps']"
            "[.//*[contains(concat(' ', normalize-space(@class), ' '), ' step ') "
            "and normalize-space()='موضوع اظهارنامه']]"
        )
        self.assertIn(("click", subject_step), page.calls)
        self.assertIn(
            ("wait_for_function", 60000, "موضوع اظهارنامه"), page.calls
        )
        self.assertIn(("wait_for", "#btnAddSection", "visible"), page.calls)

    def test_stops_when_subject_step_is_not_available(self):
        page = _Page()
        original_locator = page.locator
        subject_step = Mock()
        subject_step.count.return_value = 1
        subject_step.locator.return_value.get_attribute.return_value = "lock"
        page.locator = lambda selector: (
            subject_step
            if "normalize-space()='موضوع اظهارنامه'" in selector
            else original_locator(selector)
        )

        with self.assertRaisesRegex(
            LiveBrowserError, "form_step_not_available:موضوع اظهارنامه"
        ):
            PlaywrightRecipientPage(page).fill_natural_person(self._draft())

        subject_step.click.assert_not_called()

    def test_optional_identity_fields_are_not_filled_when_absent(self):
        page = _Page()

        PlaywrightRecipientPage(page).fill_natural_person(
            self._draft(
                recipient_national_id="",
                recipient_birth_date="",
                recipient_gender="",
            )
        )

        self.assertFalse(any(
            call[0:2] == ("fill", "#txtRealIrNationalityCode1")
            for call in page.calls
        ))
        self.assertFalse(any(
            call[0:2] == ("fill", "input[name='RealIrBirthDate']")
            for call in page.calls
        ))
        self.assertFalse(any(
            call[0:2] == ("click", "#rdbRealIrMale")
            or call[0:2] == ("click", "#rdbRealIrFemale")
            for call in page.calls
        ))

    def test_single_space_is_allowed_for_required_main_street(self):
        page = _Page()

        PlaywrightRecipientPage(page).fill_natural_person(
            self._draft(recipient_main_street=" ", recipient_address="")
        )

        self.assertIn(("fill", "#txtBasicAvenue", " "), page.calls)

    def test_missing_recipient_data_stops_before_browser_activity(self):
        page = _Page()

        with self.assertRaisesRegex(
            LiveBrowserError, "recipient_fields_incomplete:recipient_first_name"
        ):
            PlaywrightRecipientPage(page).fill_natural_person(
                self._draft(recipient_first_name="")
            )

        self.assertEqual(page.calls, [])

    def test_legal_recipient_stops_before_browser_activity(self):
        page = _Page()

        with self.assertRaisesRegex(
            LiveBrowserError, "recipient_person_type_not_supported"
        ):
            PlaywrightRecipientPage(page).fill_natural_person(
                self._draft(person_type="حقوقی")
            )

        self.assertEqual(page.calls, [])

    def test_unsupported_gender_stops_before_browser_activity(self):
        page = _Page()

        with self.assertRaisesRegex(
            LiveBrowserError, "recipient_gender_unsupported"
        ):
            PlaywrightRecipientPage(page).fill_natural_person(
                self._draft(recipient_gender="نامشخص")
            )

        self.assertEqual(page.calls, [])


class DeclarationUploaderTests(unittest.TestCase):
    def test_orchestrates_registration_and_confirms_at_signature_time(self):
        page = _Page()
        draft = DeclarationDraft(
            source_dir=Path("input"),
            fields={
                "declaration_subject": "موضوع",
                "declaration_text": "شرح",
            },
        )
        confirmations = []

        with (
            patch(
                "sana_declaration_uploader.live_browser._validate_live_draft"
            ),
            patch.object(PlaywrightRecipientPage, "validate"),
            patch.object(PlaywrightDeclarationPage, "open_draft_wizard"),
            patch.object(PlaywrightRecipientPage, "fill_natural_person"),
            patch.object(PlaywrightContentPage, "select_subject") as subject,
            patch.object(PlaywrightContentPage, "select_other_evidence_and_save"),
            patch.object(PlaywrightContentPage, "fill_description_and_save") as text,
            patch.object(PlaywrightSubmissionPage, "open_signature"),
            patch.object(PlaywrightSubmissionPage, "verify_ready_for_signature"),
            patch.object(
                PlaywrightSubmissionPage,
                "submit_signature",
                return_value="1405220000000001",
            ) as submit,
        ):
            result = PlaywrightDeclarationUploader(
                page,
                lambda current: confirmations.append(current) or True,
            ).upload(draft)

        subject.assert_called_once_with("موضوع")
        text.assert_called_once_with("شرح")
        submit.assert_called_once_with(final_confirmed=True)
        self.assertEqual(confirmations, [draft])
        self.assertEqual(result.tracking_code, "1405220000000001")

    def test_authenticated_batch_reuses_page_for_each_draft(self):
        page = _Page()
        page.authenticated = True
        drafts = (
            DeclarationDraft(source_dir=Path("one"), fields={"name": "one"}),
            DeclarationDraft(source_dir=Path("two"), fields={"name": "two"}),
        )
        expected = (
            UploadResult(status="success", tracking_code="111111111111"),
            UploadResult(status="success", tracking_code="222222222222"),
        )

        with patch.object(
            PlaywrightDeclarationUploader, "upload", side_effect=expected
        ) as upload, patch(
            "sana_declaration_uploader.live_browser._validate_live_draft"
        ):
            results = upload_authenticated_batch(
                page,
                drafts,
                lambda: None,
                lambda _draft: True,
            )

        self.assertEqual(results, expected)
        self.assertEqual([call.args[0] for call in upload.call_args_list], list(drafts))

    def test_batch_skips_invalid_draft_and_processes_valid_rows(self):
        page = _Page()
        page.authenticated = True
        drafts = (
            DeclarationDraft(source_dir=Path("one"), fields={}),
            DeclarationDraft(source_dir=Path("two"), fields={}),
        )
        uploaded = UploadResult(status="success", tracking_code="111111111111")

        with patch(
            "sana_declaration_uploader.live_browser._validate_live_draft",
            side_effect=(LiveBrowserError("draft_invalid"), None),
        ), patch.object(
            PlaywrightDeclarationUploader, "upload", return_value=uploaded
        ) as upload:
            results = upload_authenticated_batch(
                page,
                drafts,
                lambda: None,
                lambda _draft: True,
            )

        self.assertEqual(results[0].status, "failed")
        self.assertEqual(results[0].message, "draft_invalid")
        self.assertEqual(results[1], uploaded)
        upload.assert_called_once_with(drafts[1])


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
