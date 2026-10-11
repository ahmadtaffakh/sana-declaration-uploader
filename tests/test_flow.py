from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sana_declaration_uploader.browser import (
    AttachmentWorkflowError,
    run_attachment_workflow,
)
from sana_declaration_uploader.flow import UploadNotConfirmed, run_upload
from sana_declaration_uploader.models import Attachment, DeclarationDraft, UploadResult


class StubUploader:
    def __init__(self, result: UploadResult) -> None:
        self.result = result
        self.called = False

    def upload(self, draft: DeclarationDraft) -> UploadResult:
        self.called = True
        return self.result


class StubAttachmentPage:
    def __init__(self, *, fail_on: str = "") -> None:
        self.fail_on = fail_on
        self.calls: list[tuple[str, str]] = []

    def _record(self, action: str, attachment: Attachment) -> None:
        self.calls.append((action, attachment.path.name))
        if self.fail_on == action:
            raise RuntimeError("ambiguous site response")

    def register_attachment(self, attachment: Attachment) -> None:
        self._record("register", attachment)

    def upload_attachment_file(self, attachment: Attachment) -> None:
        self._record("upload", attachment)

    def confirm_attachment_authenticity(self, attachment: Attachment) -> None:
        self._record("confirm_authenticity", attachment)


class FlowTests(unittest.TestCase):
    def test_upload_requires_explicit_confirmation(self) -> None:
        with TemporaryDirectory() as directory:
            draft = DeclarationDraft(
                source_dir=Path(directory),
                fields={"name": "Test"},
            )
            uploader = StubUploader(
                UploadResult(status="success", tracking_code="123456")
            )

            with self.assertRaises(UploadNotConfirmed):
                run_upload(draft, uploader)

            self.assertFalse(uploader.called)

    def test_confirmed_upload_requires_verified_tracking_code(self) -> None:
        with TemporaryDirectory() as directory:
            draft = DeclarationDraft(
                source_dir=Path(directory),
                fields={"name": "Test"},
            )
            uploader = StubUploader(
                UploadResult(status="success", tracking_code="")
            )

            with self.assertRaisesRegex(RuntimeError, "verified tracking code"):
                run_upload(draft, uploader, confirmed=True)

    def test_attachment_workflow_runs_once_in_observed_order(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            attachments = tuple(
                Attachment(
                    path=root / name,
                    title="مدرک نمایندگی",
                    kind="تصویر مدرک نمایندگی",
                    document_number=number,
                    document_date="1405/07/18",
                )
                for name, number in (("agency.pdf", "100"), ("hr.pdf", "101"))
            )
            page = StubAttachmentPage()

            run_attachment_workflow(attachments, page)

            self.assertEqual(
                page.calls,
                [
                    ("register", "agency.pdf"),
                    ("upload", "agency.pdf"),
                    ("confirm_authenticity", "agency.pdf"),
                    ("register", "hr.pdf"),
                    ("upload", "hr.pdf"),
                    ("confirm_authenticity", "hr.pdf"),
                ],
            )

    def test_attachment_failure_stops_without_retry_or_next_item(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            attachments = tuple(
                Attachment(
                    path=root / name,
                    title="مدرک نمایندگی",
                    kind="تصویر مدرک نمایندگی",
                    document_number="100",
                    document_date="1405/07/18",
                )
                for name in ("agency.pdf", "hr.pdf")
            )
            page = StubAttachmentPage(fail_on="upload")

            with self.assertRaisesRegex(RuntimeError, "ambiguous site response"):
                run_attachment_workflow(attachments, page)

            self.assertEqual(
                page.calls,
                [("register", "agency.pdf"), ("upload", "agency.pdf")],
            )

    def test_attachment_metadata_is_validated_before_browser_write(self) -> None:
        with TemporaryDirectory() as directory:
            attachment = Attachment(
                path=Path(directory) / "agency.pdf",
                title="مدرک نمایندگی",
                kind="تصویر مدرک نمایندگی",
                document_number="",
                document_date="1405/07/18",
            )
            page = StubAttachmentPage()

            with self.assertRaisesRegex(
                AttachmentWorkflowError, "attachment_document_number_missing"
            ):
                run_attachment_workflow((attachment,), page)

            self.assertEqual(page.calls, [])


if __name__ == "__main__":
    unittest.main()
