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
    def test_logs_in_waits_for_close_signal_then_closes_browser(self):
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
        result = SetPetitionDataResult("1" * 16, "12", "A" * 32)
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
                    return_value={"Description": "شرح"},
                ),
                patch.object(
                    api_test_flow,
                    "submit_one_petition_data",
                    return_value=result,
                ) as submit,
            ):
                close_event = Event()
                close_event.set()
                actual = api_test_flow.run_live_setpetitiondata_test(
                    draft,
                    LoginCredentials("n", "p", "l", "o"),
                    lambda: True,
                    {"Description": "template"},
                    body_path,
                    progress=events.append,
                    close_browser_event=close_event,
                )

            envelope = json.loads(body_path.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(envelope["data"]), {"Description": "شرح"})
            self.assertTrue(body_path.with_name("request.curl.txt").is_file())

        self.assertEqual(actual, result)
        login.assert_called_once()
        submit.assert_called_once_with(page, {"Description": "شرح"})
        page.goto.assert_not_called()
        page.locator.assert_not_called()
        self.assertIn("__api_browser_ready_to_close__", events)
        self.assertEqual(context.close.call_count, 1)
        browser.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
