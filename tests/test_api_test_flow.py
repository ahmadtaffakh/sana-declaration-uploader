import sys
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from sana_declaration_uploader import api_test_flow
from sana_declaration_uploader.live_browser import LoginCredentials
from sana_declaration_uploader.models import DeclarationDraft
from sana_declaration_uploader.petition_api import SetPetitionDataResult


class DirectSetPetitionDataFlowTests(unittest.TestCase):
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
            DeclarationDraft(Path("."), {"declaration_text": "شرح اول"}),
            DeclarationDraft(Path("."), {"declaration_text": "شرح دوم"}),
        )
        draft = DeclarationDraft(Path("."), {"declaration_text": "شرح"})

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
            ):
                def submit_side_effect(_page, petition_data):
                    if petition_data["Description"] == "شرح دوم":
                        self.assertTrue(
                            any(
                                "کد رهگیری: " + results[0].tracking_code in event
                                for event in events
                            )
                        )
                    return results[len(submit.call_args_list) - 1]

                submit.side_effect = submit_side_effect
                close_event = Event()
                close_event.set()
                actual = api_test_flow.run_live_setpetitiondata_batch(
                    drafts,
                    LoginCredentials("n", "p", "l", "o"),
                    lambda: True,
                    {"Description": "template"},
                    body_path,
                    lookup_dir=Path(directory),
                    progress=events.append,
                    close_browser_event=close_event,
                )

            first_envelope = json.loads(
                body_path.with_name("request-row-001.txt").read_text(
                    encoding="utf-8"
                )
            )
            second_envelope = json.loads(
                body_path.with_name("request-row-002.txt").read_text(
                    encoding="utf-8"
                )
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
        page.locator.assert_not_called()
        self.assertIn("__api_browser_ready_to_close__", events)
        self.assertEqual(context.close.call_count, 1)
        browser.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
