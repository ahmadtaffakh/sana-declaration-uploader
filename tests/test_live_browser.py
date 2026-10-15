import unittest
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call, patch

from sana_declaration_uploader.live_browser import (
    ERROR_INSPECTION_INTERVAL_MS,
    ERROR_INSPECTION_MINUTES,
    LOADER_APPEARANCE_TIMEOUT_MS,
    PAGE_LOADING_TIMEOUT_MS,
    LiveBrowserError,
    LoginCredentials,
    PlaywrightDeclarationUploader,
    PlaywrightDeclarationPage,
    PlaywrightContentPage,
    PlaywrightRecipientPage,
    PlaywrightSubmissionPage,
    ReusableLoginSession,
    _SELECT_RECIPIENT_LOOKUPS,
    _hold_browser_for_error_inspection,
    _wait_for_transition,
    extract_tracking_code,
    login_self_service,
    run_authenticated_batch,
    run_live_upload_batch,
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
        self.page.input_values[self.selector] = value

    def input_value(self):
        self.page.calls.append(("input_value", self.selector))
        return self.page.input_values.get(self.selector, "")

    def press(self, key):
        self.page.calls.append(("press", self.selector, key))
        if key == "Backspace":
            self.page.input_values[self.selector] = ""

    def press_sequentially(self, value, **kwargs):
        self.page.calls.append(
            ("press_sequentially", self.selector, value, kwargs.get("delay"))
        )
        self.page.input_values[self.selector] = value

    def click(self, **_kwargs):
        self.page.calls.append(("click", self.selector))
        if self.selector == "#frmCheckOTP button":
            self.page.authenticated = True

    def wait_for(self, **kwargs):
        self.page.calls.append(("wait_for", self.selector, kwargs["state"]))
        if self.selector in self.page.wait_failures:
            raise TimeoutError

    def locator(self, selector):
        return _Locator(self.page, f"{self.selector} >> {selector}")

    def get_attribute(self, name):
        self.page.calls.append(("get_attribute", self.selector, name))
        return ""

    def inner_text(self, **_kwargs):
        self.page.calls.append(("inner_text", self.selector))
        return self.page.inner_texts.get(self.selector, "")

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
        self.inner_texts = {}
        self.input_values = {}
        self.wait_failures = set()

    def goto(self, url, **kwargs):
        self.calls.append(("goto", url))

    def locator(self, selector):
        return _Locator(self, selector)

    def evaluate(self, script, payload):
        self.calls.append(("evaluate", payload))
        if isinstance(payload, list):
            return "موضوع اظهارنامه" if "موضوع اظهارنامه" in payload else None
        if isinstance(payload, dict) and "label" in payload:
            return payload["label"]
        return {
            name: values["id"]
            for name, values in payload.items()
        }

    def wait_for_function(self, _script, **kwargs):
        self.calls.append(
            ("wait_for_function", kwargs["timeout"], kwargs.get("arg"))
        )

    def wait_for_timeout(self, timeout):
        self.calls.append(("wait_for_timeout", timeout))


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
                ("wait_for", "#rdbSetPetition"),
                ("click", "#rdbSetPetition"),
                ("wait_for_function", 60000),
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
                ("wait_for", "#btnNext"),
                ("click", "#rdbReplayToDeclaration"),
                ("wait_for", "#replayToDeclaration"),
                ("fill", "#replayToDeclaration"),
                ("click", "#btnNext"),
                ("wait_for_function", 60000),
                ("click", "#btnNext"),
                ("wait_for_function", 60000),
                (
                    "wait_for",
                    "div.margin-bottom-5 > div > div > div:nth-of-type(1) "
                    "div:nth-of-type(3) > div > div > div",
                ),
            ],
        )
        self.assertNotIn(("click", "#legalIranianPerson"), page.calls)

    def test_stops_before_second_next_when_declarant_step_is_not_active(self):
        page = _Page()
        original_wait_for_function = page.wait_for_function

        def wait_for_function(script, **kwargs):
            if kwargs.get("arg") == "اظهارکننده":
                raise TimeoutError
            return original_wait_for_function(script, **kwargs)

        page.wait_for_function = wait_for_function

        with self.assertRaisesRegex(LiveBrowserError, "declarant_step_not_active"):
            PlaywrightDeclarationPage(page).open_draft_wizard(
                DeclarationDraft(source_dir=Path("input"), fields={})
            )

        self.assertEqual(page.calls.count(("click", "#btnNext")), 1)

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
        page.wait_for_function = Mock(side_effect=(None, TimeoutError))
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
    RECORDED_NATURAL_PERSON_TAB = (
        "div.margin-bottom-5 > div > div > div:nth-of-type(1) "
        "div:nth-of-type(3) > div > div > div"
    )

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
        next_click = ("click", "#btnNext")
        self.assertEqual(page.calls.count(next_click), 2)
        self.assertLess(
            page.calls.index(("input_value", "#txtBasicAvenue")),
            page.calls.index(next_click),
        )
        self.assertIn(
            ("wait_for_function", 60000, "نماینده"),
            page.calls,
        )
        self.assertIn(
            ("wait_for_function", 60000, "موضوع اظهارنامه"),
            page.calls,
        )
        self.assertIn(("wait_for", "#btnAddSection", "visible"), page.calls)

    def test_location_lookups_wait_for_each_dependent_list(self):
        script = _SELECT_RECIPIENT_LOOKUPS

        state_choice = script.index("choose('state'")
        county_wait = script.index("viewModel.theProvinceTitleList")
        county_choice = script.index("choose('province'")
        city_wait = script.index("viewModel.theCityTitleList")
        city_choice = script.index("choose('city'")

        self.assertTrue(script.startswith("async payload =>"))
        self.assertLess(state_choice, county_wait)
        self.assertLess(county_wait, county_choice)
        self.assertLess(county_choice, city_wait)
        self.assertLess(city_wait, city_choice)
        self.assertIn('.ui-select-container[name="${name}"]', script)
        self.assertIn("scope.$select.selected?.Id", script)
        self.assertNotIn(".ui-select-match-text", script)

    def test_lookup_verification_uses_selected_ids(self):
        page = _Page()
        original_evaluate = page.evaluate

        def evaluate(script, payload):
            selected = original_evaluate(script, payload)
            selected["city"] = "wrong-city-id"
            return selected

        page.evaluate = evaluate

        with self.assertRaisesRegex(
            LiveBrowserError, "recipient_lookup_selection_not_verified"
        ):
            PlaywrightRecipientPage(page).fill_natural_person(self._draft())

        self.assertNotIn(("click", "#txtBasicAvenue"), page.calls)

    def test_opens_natural_person_tab_before_registering_recipient(self):
        page = _Page()

        PlaywrightRecipientPage(page).fill_natural_person(self._draft())

        tab_click = ("click", self.RECORDED_NATURAL_PERSON_TAB)
        add_click = ("click", "#btnAddSection")
        next_click = ("click", "#btnNext")
        self.assertIn(tab_click, page.calls)
        self.assertLess(page.calls.index(tab_click), page.calls.index(add_click))
        self.assertEqual(page.calls.count(next_click), 2)
        self.assertLess(page.calls.index(add_click), page.calls.index(next_click))

    def test_tab_navigation_failure_stops_before_adding_recipient(self):
        page = _Page()
        original_locator = page.locator
        tab = Mock()
        tab.count.return_value = 1
        tab.click.side_effect = TimeoutError
        page.locator = lambda selector: (
            tab
            if selector == self.RECORDED_NATURAL_PERSON_TAB
            else original_locator(selector)
        )

        with self.assertRaisesRegex(
            LiveBrowserError, "natural_person_tab_navigation_failed"
        ):
            PlaywrightRecipientPage(page).fill_natural_person(self._draft())

        tab.click.assert_called_once_with(timeout=60_000)
        self.assertNotIn(("click", "#btnAddSection"), page.calls)

    def test_representative_next_reaches_subject_without_direct_tab_click(self):
        page = _Page()

        PlaywrightRecipientPage(page).fill_natural_person(self._draft())

        self.assertEqual(page.calls.count(("click", "#btnNext")), 2)
        self.assertIn(("wait_for_function", 60000, "نماینده"), page.calls)
        self.assertIn(
            ("wait_for_function", 60000, "موضوع اظهارنامه"), page.calls
        )
        self.assertFalse(any(
            "normalize-space()='موضوع اظهارنامه'" in str(call)
            for call in page.calls
        ))

    def test_clicks_next_on_representative_from_confirmed_active_step(self):
        page = _Page()
        with patch(
            "sana_declaration_uploader.live_browser._wait_for_any_active_form_step",
            side_effect=("نماینده", "موضوع اظهارنامه"),
        ):
            PlaywrightRecipientPage(page).fill_natural_person(self._draft())

        self.assertEqual(page.calls.count(("click", "#btnNext")), 2)
        self.assertIn(("wait_for_timeout", 500), page.calls)

    def test_subject_transition_failure_stops_after_representative_next(self):
        page = _Page()
        original_wait_for_function = page.wait_for_function

        def wait_for_function(script, **kwargs):
            if kwargs.get("arg") == "موضوع اظهارنامه":
                raise TimeoutError
            return original_wait_for_function(script, **kwargs)

        page.wait_for_function = wait_for_function

        with self.assertRaisesRegex(
            LiveBrowserError, "subject_step_not_active"
        ):
            PlaywrightRecipientPage(page).fill_natural_person(self._draft())

        self.assertEqual(page.calls.count(("click", "#btnNext")), 2)

    def test_reports_recipient_residence_error_without_waiting_for_subject(self):
        page = _Page()
        original_locator = page.locator
        dialog = Mock()
        dialog.is_visible.side_effect = (False, False, True)
        dialog.inner_text.return_value = (
            "لطفا اقامتگاه اشخاص مخاطب را مشخص یا تکمیل نمایید"
        )
        page.locator = lambda selector: (
            dialog if selector == ".sweet-alert:visible" else original_locator(selector)
        )

        with self.assertRaisesRegex(
            LiveBrowserError,
            "site_validation_rejected:recipient_residence_incomplete",
        ):
            PlaywrightRecipientPage(page).fill_natural_person(self._draft())

        dialog.inner_text.assert_called_once_with(timeout=2_000)

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

    def test_blank_main_street_and_address_use_unknown_fallback(self):
        page = _Page()

        PlaywrightRecipientPage(page).fill_natural_person(
            self._draft(recipient_main_street=" ", recipient_address="")
        )

        self.assertIn(("click", "#txtBasicAvenue"), page.calls)
        self.assertIn(("press", "#txtBasicAvenue", "Control+A"), page.calls)
        self.assertIn(("press", "#txtBasicAvenue", "Backspace"), page.calls)
        self.assertIn(
            ("press_sequentially", "#txtBasicAvenue", "نامشخص", 100),
            page.calls,
        )
        self.assertIn(("press", "#txtBasicAvenue", "Tab"), page.calls)
        self.assertIn(("input_value", "#txtBasicAvenue"), page.calls)
        self.assertNotIn(
            ("wait_for_function", 60000, "نامشخص"),
            page.calls,
        )

    def test_blank_main_street_is_filled_with_truncated_address(self):
        page = _Page()
        address = "نشانی بسیار طولانی برای کنترل محدودیت خیابان اصلی"
        expected = address[:30].strip()

        PlaywrightRecipientPage(page).fill_natural_person(
            self._draft(recipient_main_street="", recipient_address=address)
        )

        self.assertIn(
            ("press_sequentially", "#txtBasicAvenue", expected, 100),
            page.calls,
        )
        self.assertIn(
            (
                "fill",
                "textarea[ng-model='viewModel.currentPetitionPerson.HomeAddress']",
                address,
            ),
            page.calls,
        )

    def test_unset_main_street_stops_before_subject_navigation(self):
        page = _Page()
        original_locator = page.locator
        main_street = Mock()
        main_street.input_value.return_value = ""
        page.locator = lambda selector: (
            main_street if selector == "#txtBasicAvenue" else original_locator(selector)
        )

        with self.assertRaisesRegex(
            LiveBrowserError, "recipient_main_street_not_set"
        ):
            PlaywrightRecipientPage(page).fill_natural_person(
                self._draft(recipient_main_street="", recipient_address="")
            )

        main_street.press_sequentially.assert_called_once_with(
            "نامشخص", delay=100
        )
        self.assertFalse(any(
            "normalize-space()='موضوع اظهارنامه'" in str(call)
            for call in page.calls
        ))

    def test_unconfirmed_recipient_transition_stops_before_representative_next(self):
        page = _Page()
        original_wait_for_function = page.wait_for_function

        def wait_for_function(script, **kwargs):
            if kwargs.get("arg") == "نماینده":
                raise TimeoutError
            return original_wait_for_function(script, **kwargs)

        page.wait_for_function = wait_for_function

        with self.assertRaisesRegex(
            LiveBrowserError, "representative_step_not_active"
        ):
            PlaywrightRecipientPage(page).fill_natural_person(self._draft())

        self.assertEqual(page.calls.count(("click", "#btnNext")), 1)
        self.assertFalse(any(
            "normalize-space()='موضوع اظهارنامه'" in str(call)
            for call in page.calls
        ))

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

    def test_reports_current_and_next_stage_after_readiness(self):
        page = _Page()
        draft = DeclarationDraft(
            source_dir=Path("input"),
            fields={"declaration_subject": "موضوع", "declaration_text": "شرح"},
        )
        progress = []

        with (
            patch("sana_declaration_uploader.live_browser._validate_live_draft"),
            patch.object(PlaywrightRecipientPage, "validate"),
            patch.object(PlaywrightDeclarationPage, "open_draft_wizard"),
            patch.object(PlaywrightRecipientPage, "fill_natural_person"),
            patch.object(PlaywrightContentPage, "select_subject"),
            patch.object(PlaywrightContentPage, "fill_description_and_save"),
            patch.object(PlaywrightSubmissionPage, "open_signature"),
            patch.object(PlaywrightSubmissionPage, "verify_ready_for_signature"),
            patch.object(
                PlaywrightSubmissionPage,
                "submit_signature",
                return_value="1405220000000001",
            ),
        ):
            PlaywrightDeclarationUploader(
                page,
                lambda _draft: True,
                progress=progress.append,
            ).upload(draft)

        self.assertEqual(progress[0], "شروع مرحله: بازکردن فرم اظهارنامه")
        self.assertIn("پایان موفق مرحله: ثبت اطلاعات مخاطب", progress)
        self.assertEqual(progress[-1], "پایان موفق مرحله: ثبت نهایی اظهارنامه")

    def test_reports_failed_stage_and_preserves_original_error(self):
        page = _Page()
        draft = DeclarationDraft(
            source_dir=Path("input"),
            fields={"declaration_subject": "موضوع", "declaration_text": "شرح"},
        )
        progress = []

        with (
            patch("sana_declaration_uploader.live_browser._validate_live_draft"),
            patch.object(PlaywrightRecipientPage, "validate"),
            patch.object(PlaywrightDeclarationPage, "open_draft_wizard"),
            patch.object(
                PlaywrightRecipientPage,
                "fill_natural_person",
                side_effect=LiveBrowserError("recipient_residence_incomplete"),
            ),
        ):
            with self.assertRaisesRegex(
                LiveBrowserError, "recipient_residence_incomplete"
            ):
                PlaywrightDeclarationUploader(
                    page,
                    lambda _draft: True,
                    progress=progress.append,
                ).upload(draft)

        self.assertIn("شروع مرحله: ثبت اطلاعات مخاطب", progress)
        self.assertIn(
            "پایان ناموفق مرحله: ثبت اطلاعات مخاطب؛ "
            "علت: recipient_residence_incomplete",
            progress,
        )

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


class BrowserErrorInspectionTests(unittest.TestCase):
    def test_keeps_page_open_for_five_minutes_with_minute_updates(self):
        page = _Page()
        progress = []

        _hold_browser_for_error_inspection(page, progress.append)

        self.assertEqual(
            [call for call in page.calls if call[0] == "wait_for_timeout"],
            [
                ("wait_for_timeout", ERROR_INSPECTION_INTERVAL_MS)
                for _ in range(ERROR_INSPECTION_MINUTES)
            ],
        )
        self.assertIn("زمان باقی‌مانده: 5 دقیقه", progress[0])
        self.assertEqual(progress[-1], "مهلت بررسی پایان یافت؛ مرورگر بسته می‌شود.")

    def test_live_batch_holds_browser_before_closing_after_error(self):
        page = Mock()
        context = Mock()
        context.new_page.return_value = page
        browser = Mock()
        browser.new_context.return_value = context
        playwright = SimpleNamespace(chromium=Mock())
        playwright.chromium.launch.return_value = browser
        manager = Mock()
        manager.__enter__ = Mock(return_value=playwright)
        manager.__exit__ = Mock(return_value=False)
        sync_api = ModuleType("playwright.sync_api")
        sync_api.sync_playwright = Mock(return_value=manager)
        playwright_package = ModuleType("playwright")

        with (
            patch.dict(
                sys.modules,
                {
                    "playwright": playwright_package,
                    "playwright.sync_api": sync_api,
                },
            ),
            patch(
                "sana_declaration_uploader.live_browser.upload_authenticated_batch",
                side_effect=LiveBrowserError("site_operation_rejected"),
            ),
            patch(
                "sana_declaration_uploader.live_browser."
                "_hold_browser_for_error_inspection"
            ) as hold,
        ):
            with self.assertRaisesRegex(
                LiveBrowserError, "site_operation_rejected"
            ):
                run_live_upload_batch((), lambda: None, lambda _draft: False)

        hold.assert_called_once_with(page, None)
        context.close.assert_called_once_with()
        browser.close.assert_called_once_with()

    def test_waits_up_to_five_minutes_when_loading_overlay_is_visible(self):
        page = _Page()
        original_locator = page.locator
        loader = Mock()
        loader.first = loader
        page.locator = lambda selector: (
            loader if selector == ".blockUI:visible" else original_locator(selector)
        )

        _wait_for_transition(
            page,
            60_000,
            ready_selector="#ready",
            failure_code="ready_not_found",
        )

        self.assertEqual(
            loader.wait_for.call_args_list,
            [
                call(state="visible", timeout=LOADER_APPEARANCE_TIMEOUT_MS),
                call(state="hidden", timeout=PAGE_LOADING_TIMEOUT_MS),
            ],
        )


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
    DESCRIPTION_STEP = (
        "//*[@ng-repeat='step in navCtrl.currentStep.innerSteps']"
        "[.//*[contains(concat(' ', normalize-space(@class), ' '), ' step ') "
        "and normalize-space()='شرح']]"
    )
    SUBJECT_CHOICE = "#jssPetitionRelief1 >> .ui-select-choices-row:visible"
    SUBJECT_SELECTED = "#jssPetitionRelief1 >> .ui-select-match-text"
    DESCRIPTION_EDITOR = (
        "text-angular[ng-model='Model.Description'] div[contenteditable='true']"
    )

    def test_empty_subject_stops_before_browser_activity(self):
        page = _Page()

        with self.assertRaisesRegex(LiveBrowserError, "declaration_subject_missing"):
            PlaywrightContentPage(page).select_subject("  ")

        self.assertEqual(page.calls, [])

    def test_subject_is_registered_before_opening_description_tab(self):
        page = _Page()
        page.inner_texts[self.SUBJECT_CHOICE] = "موضوع"
        page.inner_texts[self.SUBJECT_SELECTED] = "موضوع"

        PlaywrightContentPage(page).select_subject("موضوع")

        self.assertIn(
            ("click", f"{self.DESCRIPTION_STEP} >> .step.ng-binding"),
            page.calls,
        )
        self.assertIn(
            (
                "wait_for_function",
                60000,
                {"readySelector": None, "stepTitle": "شرح"},
            ),
            page.calls,
        )
        self.assertIn(
            ("wait_for", self.DESCRIPTION_EDITOR, "visible"),
            page.calls,
        )
        next_click = ("click", "#btnNext")
        description_click = ("click", f"{self.DESCRIPTION_STEP} >> .step.ng-binding")
        self.assertEqual(page.calls.count(next_click), 1)
        self.assertLess(
            page.calls.index(("click", self.SUBJECT_CHOICE)),
            page.calls.index(next_click),
        )
        self.assertLess(
            page.calls.index(next_click),
            page.calls.index(description_click),
        )
        self.assertFalse(any("#chk7" in str(call) for call in page.calls))

    def test_subject_waits_for_an_exact_visible_choice(self):
        page = _Page()
        page.inner_texts[self.SUBJECT_CHOICE] = "سایر موضوعات اظهارنامه"
        page.inner_texts[self.SUBJECT_SELECTED] = "سایر موضوعات اظهارنامه"

        PlaywrightContentPage(page).select_subject("سایر موضوعات")

        self.assertIn(("wait_for", "#btnAddSection", "visible"), page.calls)
        self.assertLess(
            page.calls.index(("click", "#btnAddSection")),
            page.calls.index(
                (
                    "wait_for_function",
                    60000,
                    {"readySelector": "#jssPetitionRelief1", "stepTitle": None},
                )
            ),
        )
        self.assertIn(
            ("click", "#jssPetitionRelief1 >> button.ui-select-toggle"),
            page.calls,
        )
        self.assertIn(
            ("fill", "#jssPetitionRelief1 >> input.ui-select-search", "سایر"),
            page.calls,
        )
        self.assertIn(("wait_for", self.SUBJECT_CHOICE, "visible"), page.calls)
        self.assertIn(("click", self.SUBJECT_CHOICE), page.calls)
        self.assertIn(("wait_for", self.SUBJECT_SELECTED, "visible"), page.calls)

    def test_subject_form_must_open_after_add(self):
        page = _Page()
        original_wait_for_function = page.wait_for_function

        def wait_for_function(script, **kwargs):
            target = kwargs.get("arg")
            if isinstance(target, dict) and target.get("readySelector") == (
                "#jssPetitionRelief1"
            ):
                raise TimeoutError
            return original_wait_for_function(script, **kwargs)

        page.wait_for_function = wait_for_function

        with self.assertRaisesRegex(
            LiveBrowserError, "declaration_subject_form_not_opened"
        ):
            PlaywrightContentPage(page).select_subject("سایر موضوعات")

        self.assertIn(("click", "#btnAddSection"), page.calls)
        self.assertNotIn(
            ("click", "#jssPetitionRelief1 >> button.ui-select-toggle"),
            page.calls,
        )

    def test_missing_subject_choice_stops_before_description_navigation(self):
        page = _Page()
        page.wait_failures.add(self.SUBJECT_CHOICE)

        with self.assertRaisesRegex(
            LiveBrowserError, "declaration_subject_not_found"
        ):
            PlaywrightContentPage(page).select_subject("سایر موضوعات")

        self.assertFalse(any(
            "normalize-space()='شرح'" in str(call) for call in page.calls
        ))

    def test_unverified_subject_selection_stops_before_description(self):
        page = _Page()
        page.inner_texts[self.SUBJECT_CHOICE] = "سایر موضوعات اظهارنامه"
        page.inner_texts[self.SUBJECT_SELECTED] = "موضوع دیگری"

        with self.assertRaisesRegex(
            LiveBrowserError, "declaration_subject_not_selected"
        ):
            PlaywrightContentPage(page).select_subject("سایر موضوعات")

        self.assertFalse(any(
            "normalize-space()='شرح'" in str(call) for call in page.calls
        ))

    def test_unconfirmed_subject_registration_stops_before_description(self):
        page = _Page()
        page.inner_texts[self.SUBJECT_CHOICE] = "سایر موضوعات اظهارنامه"
        page.inner_texts[self.SUBJECT_SELECTED] = "سایر موضوعات اظهارنامه"
        original_wait_for_function = page.wait_for_function

        def wait_for_function(script, **kwargs):
            if "!visible(document.querySelector('#jssPetitionRelief1'))" in script:
                raise TimeoutError
            return original_wait_for_function(script, **kwargs)

        page.wait_for_function = wait_for_function

        with self.assertRaisesRegex(
            LiveBrowserError,
            "declaration_subject_registration_not_completed",
        ):
            PlaywrightContentPage(page).select_subject("سایر موضوعات")

        self.assertEqual(page.calls.count(("click", "#btnNext")), 1)
        self.assertFalse(any(
            "normalize-space()='شرح'" in str(call) for call in page.calls
        ))

    def test_description_is_temporarily_saved_without_next(self):
        page = _Page()

        with patch(
            "sana_declaration_uploader.live_browser._close_success_dialog"
        ) as close_dialog:
            PlaywrightContentPage(page).fill_description_and_save("شرح")

        self.assertIn(
            ("fill", self.DESCRIPTION_EDITOR, "شرح"), page.calls
        )
        self.assertIn(("wait_for", "#btnSave", "visible"), page.calls)
        self.assertIn(("click", "#btnSave"), page.calls)
        self.assertFalse(any(
            call[0:2] == ("click", "#btnNext") for call in page.calls
        ))
        close_dialog.assert_called_once_with(page, 60_000)


if __name__ == "__main__":
    unittest.main()
