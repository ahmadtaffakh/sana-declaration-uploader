from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sana_declaration_uploader.browser import (
    AttachmentWorkflowError,
    run_attachment_workflow,
)
from sana_declaration_uploader.flow import (
    InvalidDeclaration,
    UploadNotConfirmed,
    _build_desktop_drafts,
    prepare_drafts,
    run_upload,
    run_upload_batch,
)
from sana_declaration_uploader.models import (
    Attachment,
    DeclarationBatch,
    DeclarationDraft,
    DeclarationRequest,
    RecipientCandidate,
    UploadResult,
)


class StubUploader:
    def __init__(self, result: UploadResult) -> None:
        self.result = result
        self.called = False

    def upload(self, draft: DeclarationDraft) -> UploadResult:
        self.called = True
        return self.result


class BatchStubUploader:
    def __init__(self, results: list[UploadResult]) -> None:
        self.results = iter(results)
        self.calls: list[str] = []

    def upload(self, draft: DeclarationDraft) -> UploadResult:
        self.calls.append(draft.fields.get("name", ""))
        return next(self.results)


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
    def test_reviewed_unknown_person_is_treated_as_natural(self) -> None:
        candidate = RecipientCandidate(
            source_path=Path("recipient.docx"),
            first_name="نام",
            last_name="خانوادگی",
            person_type="unknown",
        )

        draft = _build_desktop_drafts(
            (candidate,),
            {-1: {"subject": "موضوع", "description": "شرح"}},
            shared=True,
            lookup_dir=Path(__file__).parents[1] / "lookups",
        )[0]

        self.assertEqual(draft.fields["person_type"], "حقیقی")

    def test_build_desktop_drafts_preserves_default_lookup_behavior(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "recipient.docx"
            candidate = RecipientCandidate(
                source_path=source_path,
                first_name="علی",
                last_name="رضایی",
                person_type="natural",
                address="",
                province="استان نامعتبر",
                county="",
                city="",
                source_recipient_ordinal=2,
            )
            payload = {
                "subject": "سایر موضوعات",
                "description": "شرح اظهارنامه",
                "attachments": (root / "agency.pdf",),
                "attachment_kind": "مدرک نمایندگی",
                "attachment_title": "وکالت‌نامه",
                "attachment_number": "100",
                "attachment_date": "1405/07/20",
            }

            draft = _build_desktop_drafts(
                (candidate,),
                {-1: payload},
                shared=True,
                lookup_dir=Path(__file__).parents[1] / "lookups",
            )[0]

            self.assertEqual(draft.fields["job_title"], "آزاد")
            self.assertEqual(draft.fields["province"], "استان تهران")
            self.assertEqual(draft.fields["county"], "شهرستان تهران")
            self.assertEqual(draft.fields["city"], "تهران")
            self.assertEqual(draft.fields["person_type"], "حقیقی")
            self.assertEqual(draft.fields["recipient_main_street"], "تهران")
            self.assertEqual(
                draft.fields["recipient_address"],
                "استان تهران - شهرستان تهران - شهر تهران",
            )
            self.assertEqual(draft.fields["declaration_text"], "شرح اظهارنامه")
            self.assertEqual(draft.attachments[0].path, root / "agency.pdf")
            self.assertEqual(draft.attachments[0].title, "وکالت‌نامه")
            self.assertEqual(draft.source_path, source_path)
            self.assertEqual(draft.source_recipient_ordinal, 2)

    def test_prepare_drafts_renders_and_validates_each_request(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            batch = DeclarationBatch(
                source_dir=root,
                title="بسته",
                requests=(
                    DeclarationRequest(
                        source_dir=root,
                        recipient_id="person1",
                        fields={
                            "recipient_id": "person1",
                            "recipient_first_name": "علی",
                            "recipient_last_name": "رضایی",
                            "data_source": "manual",
                            "recipient_national_id": "0012345678",
                            "recipient_birth_date": "1370/01/01",
                            "recipient_gender": "مرد",
                            "recipient_father_name": "حسن",
                            "job_title": "کارمند",
                            "job_key": "1",
                            "job_id": "1",
                            "job_code": "1",
                            "province": "تهران",
                            "province_key": "2",
                            "province_id": "2",
                            "province_code": "2",
                            "county": "تهران",
                            "county_key": "3",
                            "county_id": "3",
                            "county_code": "3",
                            "city": "تهران",
                            "city_key": "4",
                            "city_id": "4",
                            "city_code": "4",
                            "recipient_main_street": "خیابان اصلی",
                            "recipient_address": "نشانی کامل",
                            "court_unit_code": "123",
                            "declaration_subject": "موضوع",
                        },
                        description_template="خطاب به {{recipient.full_name}}",
                        parameters={},
                    ),
                ),
            )

            drafts = prepare_drafts(batch)

            self.assertEqual(len(drafts), 1)
            self.assertEqual(
                drafts[0].fields["declaration_text"], "خطاب به علی رضایی"
            )

    def test_prepare_drafts_rejects_invalid_request(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            batch = DeclarationBatch(
                source_dir=root,
                title="بسته",
                requests=(
                    DeclarationRequest(
                        source_dir=root,
                        recipient_id="person1",
                        fields={"data_source": "notice"},
                        description_template="شرح ثابت",
                        parameters={},
                    ),
                ),
            )

            with self.assertRaisesRegex(
                InvalidDeclaration, "notice_not_resolved"
            ):
                prepare_drafts(batch)

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

    def test_batch_validates_every_draft_before_first_browser_write(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            uploader = BatchStubUploader([
                UploadResult(status="success", tracking_code="123456"),
            ])
            drafts = (
                DeclarationDraft(source_dir=root, fields={"name": "first"}),
                DeclarationDraft(source_dir=root / "missing", fields={"name": "second"}),
            )

            with self.assertRaisesRegex(InvalidDeclaration, "source_dir_missing"):
                run_upload_batch(drafts, uploader, confirmed=True)

            self.assertEqual(uploader.calls, [])

    def test_batch_uses_one_uploader_and_stops_without_retry(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            uploader = BatchStubUploader([
                UploadResult(status="success", tracking_code="111111"),
                UploadResult(status="failed"),
                UploadResult(status="success", tracking_code="333333"),
            ])
            drafts = tuple(
                DeclarationDraft(source_dir=root, fields={"name": name})
                for name in ("first", "second", "third")
            )

            with self.assertRaisesRegex(RuntimeError, "verified tracking code"):
                run_upload_batch(drafts, uploader, confirmed=True)

            self.assertEqual(uploader.calls, ["first", "second"])

    def test_batch_requires_one_explicit_confirmation(self) -> None:
        with TemporaryDirectory() as directory:
            draft = DeclarationDraft(
                source_dir=Path(directory), fields={"name": "first"}
            )
            uploader = BatchStubUploader([
                UploadResult(status="success", tracking_code="111111"),
            ])

            with self.assertRaises(UploadNotConfirmed):
                run_upload_batch((draft,), uploader)

            self.assertEqual(uploader.calls, [])

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

    def test_all_attachment_metadata_is_checked_before_first_write(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            attachments = (
                Attachment(
                    path=root / "valid.pdf",
                    title="مدرک اول",
                    kind="سند",
                    document_number="100",
                    document_date="1405/07/18",
                ),
                Attachment(
                    path=root / "invalid.pdf",
                    title="مدرک دوم",
                    kind="سند",
                    document_number="",
                    document_date="1405/07/18",
                ),
            )
            page = StubAttachmentPage()

            with self.assertRaisesRegex(
                AttachmentWorkflowError, "attachment_document_number_missing"
            ):
                run_attachment_workflow(attachments, page)

            self.assertEqual(page.calls, [])


if __name__ == "__main__":
    unittest.main()
