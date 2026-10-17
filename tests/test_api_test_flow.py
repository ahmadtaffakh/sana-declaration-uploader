import sys
import json
import unittest
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from threading import Event
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from sana_declaration_uploader import api_test_flow
from sana_declaration_uploader.attachment_api import AttachmentApiError, AttachmentType
from sana_declaration_uploader.live_browser import LoginCredentials
from sana_declaration_uploader.models import Attachment, DeclarationDraft
from sana_declaration_uploader.petition_api import SetPetitionDataResult


class DirectSetPetitionDataFlowTests(unittest.TestCase):
    def test_matches_representation_document_default_to_live_title(self):
        live_type = AttachmentType(
            "021", "005", "تصوير مدرک نمايندگي", "شرح", {}, 1
        )

        actual = api_test_flow._match_attachment_type(
            "تصویر مدرک نمایندگی", [live_type]
        )

        self.assertIs(actual, live_type)

    def test_console_file_exists_when_attachment_lookup_fails(self):
        lookup_client = Mock()
        lookup_client.get_valid_attachment_types.side_effect = AttachmentApiError(
            "attachment_lookup_http_failed:http_status=500"
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            attachment_path = root / "file.pdf"
            attachment_path.write_bytes(b"fixture")
            draft = DeclarationDraft(
                root,
                {},
                (Attachment(
                    attachment_path,
                    "سایر ضمائم",
                    "عنوان پیوست",
                    "1",
                    "1405/01/01",
                ),),
                root / "source.docx",
            )
            request_path = root / "request.txt"

            with patch.object(
                api_test_flow, "AttachmentClient", return_value=lookup_client
            ):
                with self.assertRaises(AttachmentApiError):
                    api_test_flow._attach_draft_files(
                        Mock(),
                        {
                            "PetitionTypeCode": "110",
                            "theJSSPetitionRelief": [{"Id": "relief-id"}],
                        },
                        SetPetitionDataResult("1" * 16, "12", "petition-id"),
                        draft,
                        1,
                        1,
                        None,
                        request_path,
                    )
            self.assertTrue(
                (root / "request.attachment-001.console.txt").is_file()
            )

    def test_ambiguous_legacy_attachment_context_falls_back_to_tracking_lookup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            attachment_path = root / "file.pdf"
            attachment_path.write_bytes(b"fixture")
            draft = DeclarationDraft(
                root,
                {},
                (Attachment(
                    attachment_path,
                    "سایر ضمائم",
                    "عنوان پیوست",
                    "1",
                    "1405/01/01",
                ),),
                root / "source.docx",
                source_recipient_ordinal=2,
            )
            petition = {
                "No": "T-SECOND",
                "Id": "petition-second",
                "PetitionTypeCode": "110",
                "theJSSPetitionRelief": [{"Id": "relief-second"}],
            }

            with (
                patch.object(
                    api_test_flow,
                    "read_saved_attachment_context",
                    side_effect=api_test_flow.ResultPersistenceError(
                        "legacy_results_ambiguous_for_multiple_recipients"
                    ),
                ),
                patch.object(
                    api_test_flow,
                    "_get_petition_by_tracking_code",
                    return_value=petition,
                ) as lookup,
                patch.object(api_test_flow, "_attach_draft_files") as attach,
            ):
                api_test_flow._attach_existing_petitions(
                    Mock(),
                    ((draft, "T-SECOND"),),
                    Mock(),
                    None,
                    root / "request.txt",
                )

            lookup.assert_called_once()
            attach.assert_called_once()

    def test_attaches_to_tracking_code_without_registering_petition(self):
        events = []
        page = Mock()
        page.evaluate.return_value = {
            "status": 200,
            "text": json.dumps({
                "No": "1405000000000001",
                "Id": "petition-id",
                "PetitionTypeCode": "110",
                "theJSSPetitionRelief": [{"Id": "relief-id"}],
            }),
        }
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
        package = ModuleType("playwright")
        attachment_type = AttachmentType("099", "099", "ضمیمه", "شرح", {}, 1)
        lookup_client, register_client = Mock(), Mock()
        draft = None

        with TemporaryDirectory() as directory:
            attachment_path = Path(directory) / "file.pdf"
            attachment_path.write_bytes(b"fixture")
            draft = DeclarationDraft(
                Path(directory), {},
                (Attachment(attachment_path, "ضمیمه", "ضمیمه", "1", "1405/01/01"),),
                Path(directory) / "source.docx",
            )
            close_event = Event()
            close_event.set()
            with (
                patch.dict(
                    sys.modules,
                    {"playwright": package, "playwright.sync_api": sync_api},
                ),
                patch.object(api_test_flow, "login_self_service"),
                patch.object(api_test_flow, "submit_one_petition_data") as submit,
                patch.object(
                    api_test_flow, "AttachmentClient",
                    side_effect=[lookup_client, register_client],
                ),
                patch.object(api_test_flow, "save_attachment_console_text"),
            ):
                lookup_client.get_valid_attachment_types.return_value = [attachment_type]
                actual = api_test_flow.run_live_setpetitiondata_batch(
                    (), LoginCredentials("n", "p", "l", "o"), lambda: True,
                    {}, Path(directory) / "request.txt", lookup_dir=Path(directory),
                    progress=events.append, close_browser_event=close_event,
                    attachment_only=((draft, "1405000000000001"),),
                )

        self.assertEqual(actual, ())
        submit.assert_not_called()
        self.assertIn("1405000000000001", str(page.evaluate.call_args))
        self.assertTrue(any("ثبت اظهارنامه اجرا نمی‌شود" in event for event in events))
        register_client.register_attachment.assert_called_once()

    def test_logs_tracking_code_then_submits_next_row_once(self):
        events = []
        page = Mock()
        context = Mock()
        context.new_page.return_value = page
        context.close.side_effect = lambda: events.append("close")
        browser = Mock()
        browser.new_context.return_value = context
        playwright = SimpleNamespace(chromium=Mock())
        playwright.chromium.launch.return_value = browser
        manager = Mock()
        manager.__enter__ = Mock(return_value=playwright)
        manager.__exit__ = Mock(return_value=False)
        sync_api = ModuleType("playwright.sync_api")
        sync_api.sync_playwright = Mock(return_value=manager)
        package = ModuleType("playwright")
        results = (
            SetPetitionDataResult("1" * 16, "12", "A" * 32),
            SetPetitionDataResult("2" * 16, "13", "B" * 32),
        )
        drafts = (
            DeclarationDraft(
                Path("."), {"declaration_text": "شرح اول"},
                source_path=Path("اول.docx"),
            ),
        )
        queued_draft = DeclarationDraft(
            Path("."), {"declaration_text": "شرح دوم"},
            source_path=Path("دوم.docx"),
        )
        close_event = Event()
        request_queue: Queue = Queue()

        with TemporaryDirectory() as directory:
            body_path = Path(directory) / "request.txt"
            with (
                patch.dict(
                    sys.modules,
                    {"playwright": package, "playwright.sync_api": sync_api},
                ),
                patch.object(api_test_flow, "_validate_live_draft"),
                patch.object(api_test_flow, "login_self_service") as login,
                patch.object(
                    api_test_flow,
                    "build_setpetitiondata_payload",
                    side_effect=lambda _template, draft: {
                        "Description": draft.fields["declaration_text"]
                    },
                ) as build_payload,
                patch.object(
                    api_test_flow,
                    "resolve_captured_jurisdiction_fields",
                    return_value={
                        "notify_owner_organization_id": "court-id",
                        "notify_owner_organization_code": "00100",
                        "notify_owner_organization_title": "حوزه نمونه",
                        "get_location_for_address_id": "address-id",
                    },
                ),
                patch.object(
                    api_test_flow,
                    "submit_one_petition_data",
                ) as submit,
                patch.object(
                    api_test_flow, "save_verified_result"
                ) as save_result,
            ):
                def submit_side_effect(_page, petition_data):
                    if petition_data["Description"] == "شرح دوم":
                        self.assertTrue(
                            any(
                                "کد رهگیری: " + results[0].tracking_code in event
                                for event in events
                            )
                        )
                    result = results[len(submit.call_args_list) - 1]
                    if len(submit.call_args_list) == 2:
                        close_event.set()
                    return result

                submit.side_effect = submit_side_effect
                request_queue.put((
                    (queued_draft,),
                    LoginCredentials("n", "p", "l", "o"),
                    {"Description": "template"},
                    Path(directory) / "queued-request.txt",
                    Path(directory),
                ))
                actual = api_test_flow.run_live_setpetitiondata_batch(
                    drafts,
                    LoginCredentials("n", "p", "l", "o"),
                    lambda: True,
                    {"Description": "template"},
                    body_path,
                    lookup_dir=Path(directory),
                    progress=events.append,
                    pending_requests=request_queue,
                    close_browser_event=close_event,
                )

            first_envelope = json.loads(body_path.read_text(encoding="utf-8"))
            second_envelope = json.loads(
                (Path(directory) / "queued-request.txt").read_text(encoding="utf-8")
            )
            self.assertEqual(
                json.loads(first_envelope["data"]),
                {"Description": "شرح اول"},
            )
            self.assertEqual(
                json.loads(second_envelope["data"]),
                {"Description": "شرح دوم"},
            )

        self.assertEqual(actual, results)
        login.assert_called_once()
        self.assertEqual(build_payload.call_count, 2)
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(save_result.call_count, 2)
        self.assertEqual(save_result.call_args_list[0].args[0], drafts[0])
        self.assertEqual(
            save_result.call_args_list[0].args[1].tracking_code,
            results[0].tracking_code,
        )
        self.assertEqual(
            submit.call_args_list[0].args[1],
            {"Description": "شرح اول"},
        )
        self.assertEqual(
            submit.call_args_list[1].args[1],
            {"Description": "شرح دوم"},
        )
        self.assertTrue(any("کد رهگیری: " + "1" * 16 in e for e in events))
        self.assertTrue(any("کد رهگیری: " + "2" * 16 in e for e in events))
        page.goto.assert_not_called()
        self.assertEqual(page.locator.call_count, 2)
        self.assertIn("__api_browser_ready_to_close__", events)
        self.assertEqual(context.close.call_count, 1)
        browser.close.assert_called_once()

    def test_expired_session_reauthenticates_before_the_next_write(self):
        page = Mock()
        page.locator.return_value.is_visible.side_effect = [False, True]
        initial = LoginCredentials("n", "p", "l", "old")
        refreshed = LoginCredentials("n", "p", "l", "fresh")
        credentials = [initial]
        provider = Mock(return_value=refreshed)
        progress = []
        guard = api_test_flow._authentication_guard(
            page, credentials, provider, 60_000, progress.append
        )

        with patch.object(api_test_flow, "login_self_service") as login:
            guard()
            guard()

        provider.assert_called_once_with(initial)
        login.assert_called_once_with(
            page, refreshed.as_portal_fields(), timeout_ms=60_000
        )
        self.assertEqual(credentials[0], refreshed)

    def test_attaches_to_same_petition_after_tracking_code(self):
        events = []
        page = Mock()
        context = Mock()
        context.new_page.return_value = page
        context.close.return_value = None
        browser = Mock()
        browser.new_context.return_value = context
        playwright = SimpleNamespace(chromium=Mock())
        playwright.chromium.launch.return_value = browser
        manager = Mock()
        manager.__enter__ = Mock(return_value=playwright)
        manager.__exit__ = Mock(return_value=False)
        sync_api = ModuleType("playwright.sync_api")
        sync_api.sync_playwright = Mock(return_value=manager)
        package = ModuleType("playwright")
        result = SetPetitionDataResult("1" * 16, "12", "A" * 32)
        attachment_type = AttachmentType(
            "099", "099", "سایر ضمائم", "شرح", {}, 1
        )
        attachment = Attachment(
            Path("attachment.pdf"), "attachment", "سایر ضمائم",
            "17", "1405/05/05",
        )
        draft = DeclarationDraft(
            Path("."), {"declaration_text": "شرح"}, (attachment,)
        )
        lookup_client = Mock()
        lookup_client.get_valid_attachment_types.return_value = [attachment_type]
        register_client = Mock()

        with TemporaryDirectory() as directory:
            request_path = Path(directory) / "request.txt"
            close_event = Event()
            close_event.set()
            with (
                patch.dict(
                    sys.modules,
                    {"playwright": package, "playwright.sync_api": sync_api},
                ),
                patch.object(api_test_flow, "_validate_live_draft"),
                patch.object(api_test_flow, "login_self_service"),
                patch.object(
                    api_test_flow,
                    "build_setpetitiondata_payload",
                    return_value={
                        "PetitionTypeCode": "110",
                        "theJSSPetitionRelief": [{"Id": "relief-id"}],
                    },
                ),
                patch.object(
                    api_test_flow,
                    "resolve_captured_jurisdiction_fields",
                    return_value={},
                ),
                patch.object(
                    api_test_flow, "submit_one_petition_data", return_value=result
                ) as submit,
                patch.object(
                    api_test_flow,
                    "AttachmentClient",
                    side_effect=[lookup_client, register_client],
                ) as client_factory,
                patch.object(
                    api_test_flow, "save_attachment_console_text"
                ) as save_console,
            ):
                actual = api_test_flow.run_live_setpetitiondata_batch(
                    (draft,),
                    LoginCredentials("n", "p", "l", "o"),
                    lambda: True,
                    {"Description": "template"},
                    request_path,
                    lookup_dir=Path(directory),
                    progress=events.append,
                    close_browser_event=close_event,
                )

        self.assertEqual(actual, (result,))
        submit.assert_called_once()
        self.assertEqual(save_console.call_args.args[1], result.tracking_code)
        self.assertEqual(client_factory.call_count, 2)
        lookup_reliefs = lookup_client.get_valid_attachment_types.call_args.args[0]
        self.assertEqual(lookup_reliefs[0]["JSSPetitionId"], result.petition_id)
        self.assertEqual(
            lookup_client.get_valid_attachment_types.call_args.kwargs[
                "petition_type_code"
            ],
            "110",
        )
        lookup_args, lookup_kwargs = client_factory.call_args_list[0]
        self.assertEqual(lookup_args[1], result.tracking_code)
        self.assertEqual(
            lookup_args[2], result.petition_id
        )
        register_client.register_attachment.assert_called_once_with(
            attachment_type,
            document_number="17",
            document_date="1405/05/05",
            file_path=attachment.path,
            show_original=True,
            document_title="attachment",
        )
        tracking_event = next(
            i for i, message in enumerate(events)
            if "کد رهگیری: " + result.tracking_code in message
        )
        lookup_event = next(
            i for i, message in enumerate(events)
            if "شروع مرحله: دریافت نوع‌های مجاز پیوست" in message
        )
        self.assertLess(tracking_event, lookup_event)

    def test_attachment_failure_after_registration_stops_batch(self):
        events = []
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
        package = ModuleType("playwright")
        first_result = SetPetitionDataResult("1" * 16, "12", "A" * 32)
        draft = DeclarationDraft(
            Path("."), {"declaration_text": "شرح"},
            (Attachment(Path("attachment.pdf"), "attachment", "نوع", "1", "1405/05/05"),),
        )
        lookup_client = Mock()
        lookup_client.get_valid_attachment_types.return_value = [
            AttachmentType("099", "099", "نوع", "شرح", {}, 1)
        ]
        register_client = Mock()
        register_client.register_attachment.side_effect = AttachmentApiError(
            "attachment_file_upload_transport_ambiguous"
        )

        with TemporaryDirectory() as directory:
            close_event = Event()
            close_event.set()
            with (
                patch.dict(
                    sys.modules,
                    {"playwright": package, "playwright.sync_api": sync_api},
                ),
                patch.object(api_test_flow, "_validate_live_draft"),
                patch.object(api_test_flow, "login_self_service"),
                patch.object(
                    api_test_flow,
                    "build_setpetitiondata_payload",
                    return_value={"theJSSPetitionRelief": [{"Id": "relief"}]},
                ),
                patch.object(
                    api_test_flow,
                    "resolve_captured_jurisdiction_fields",
                    return_value={},
                ),
                patch.object(
                    api_test_flow,
                    "submit_one_petition_data",
                    return_value=first_result,
                ) as submit,
                patch.object(
                    api_test_flow,
                    "AttachmentClient",
                    side_effect=[lookup_client, register_client],
                ),
                patch.object(api_test_flow, "save_attachment_console_text"),
            ):
                with self.assertRaisesRegex(
                    api_test_flow.LiveBrowserError,
                    "attachment_failed_after_petition_registered",
                ):
                    api_test_flow.run_live_setpetitiondata_batch(
                        (draft, draft),
                        LoginCredentials("n", "p", "l", "o"),
                        lambda: True,
                        {"Description": "template"},
                        Path(directory) / "request.txt",
                        lookup_dir=Path(directory),
                        progress=events.append,
                        close_browser_event=close_event,
                    )

        submit.assert_called_once()
        self.assertTrue(
            any("ثبت اظهارنامه موفق است، اما پیوست کامل نشد" in m for m in events)
        )


if __name__ == "__main__":
    unittest.main()
