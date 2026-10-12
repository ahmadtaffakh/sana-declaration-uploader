from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sana_declaration_uploader.models import Attachment, DeclarationDraft
from sana_declaration_uploader.validator import validate_draft


class ValidatorTests(unittest.TestCase):
    def test_valid_draft_has_no_errors(self) -> None:
        with TemporaryDirectory() as directory:
            source_dir = Path(directory)
            attachment_path = source_dir / "attachment.pdf"
            attachment_path.write_bytes(b"%PDF-test")
            draft = DeclarationDraft(
                source_dir=source_dir,
                fields={"recipient_name": "Test User"},
                attachments=(
                    Attachment(
                        path=attachment_path,
                        title="Test",
                        kind="document",
                    ),
                ),
            )

            self.assertEqual(validate_draft(draft), ())

    def test_attachment_outside_source_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            source_dir.mkdir()
            outside = root / "outside.pdf"
            outside.write_bytes(b"%PDF-test")
            draft = DeclarationDraft(
                source_dir=source_dir,
                fields={"recipient_name": "Test User"},
                attachments=(
                    Attachment(path=outside, title="Test", kind="document"),
                ),
            )

            self.assertIn(
                "attachment_outside_source:outside.pdf",
                validate_draft(draft),
            )

    def test_notice_source_must_be_resolved_before_upload(self) -> None:
        with TemporaryDirectory() as directory:
            draft = DeclarationDraft(
                source_dir=Path(directory),
                fields={
                    "recipient_id": "r1",
                    "data_source": "notice",
                    "notice_number": "N-1",
                    "notice_resolved": "false",
                    "declaration_subject": "Subject",
                    "declaration_text": "Text",
                },
            )

            self.assertIn("notice_not_resolved", validate_draft(draft))

    def test_site_lookup_values_are_required_for_combo_labels(self) -> None:
        with TemporaryDirectory() as directory:
            draft = DeclarationDraft(
                source_dir=Path(directory),
                fields={
                    "recipient_id": "r1",
                    "data_source": "manual",
                    "recipient_first_name": "Test",
                    "recipient_last_name": "User",
                    "job_title": "Job",
                    "province": "Province",
                    "county": "County",
                    "city": "City",
                    "recipient_main_street": "Street",
                    "declaration_subject": "Subject",
                    "declaration_text": "Text",
                },
            )

            errors = validate_draft(draft)

            self.assertIn("lookup_unresolved:job_id", errors)
            self.assertIn("lookup_unresolved:province_code", errors)
            self.assertIn("lookup_unresolved:city_id", errors)
            self.assertNotIn("field_missing:recipient_national_id", errors)


if __name__ == "__main__":
    unittest.main()
