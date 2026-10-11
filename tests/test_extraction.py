from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from docx import Document

from sana_declaration_uploader.extraction import (
    DocumentExtractionError,
    extract_recipient,
    extract_recipients,
)


class RecipientExtractionTests(unittest.TestCase):
    def test_extracts_labeled_recipient_fields_from_word(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "person.docx"
            document = Document()
            for value in (
                "نام و نام خانوادگی: علی رضایی",
                "کد ملی: ۱۲۳۴۵۶۷۸۹۱",
                "کد پرسنلی: ۴۲۱",
                "تاریخ تولد: ۱۳۷۰/۰۱/۰۲",
                "کد پستی: ۱۱۱۱۱۱۱۱۱۱",
                "آدرس: تهران، خیابان نمونه، پلاک ۱",
            ):
                document.add_paragraph(value)
            document.save(path)

            candidate = extract_recipient(path)

            self.assertEqual(candidate.full_name, "علی رضایی")
            self.assertEqual(candidate.national_id, "1234567891")
            self.assertEqual(candidate.employee_code, "421")
            self.assertEqual(candidate.birth_date, "1370/01/02")
            self.assertEqual(candidate.postal_code, "1111111111")
            self.assertEqual(candidate.address, "تهران، خیابان نمونه، پلاک 1")
            self.assertEqual(candidate.issues, ())

    def test_missing_identity_details_remains_reviewable(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "letter.txt"
            path.write_text("نامه اداری بدون مشخصات ساختاریافته", encoding="utf-8")

            candidate = extract_recipient(path)

            self.assertIn("name_missing", candidate.issues)
            self.assertIn("identity_details_missing", candidate.issues)

    def test_empty_source_list_is_rejected(self) -> None:
        with self.assertRaisesRegex(DocumentExtractionError, "source_files_missing"):
            extract_recipients([])


if __name__ == "__main__":
    unittest.main()
