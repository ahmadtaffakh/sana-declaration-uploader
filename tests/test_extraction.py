from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from docx import Document

from sana_declaration_uploader.extraction import (
    DocumentExtractionError,
    extract_recipient,
    extract_recipient_candidates,
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

    def test_extracts_multiple_grounded_parties_after_nfkc_normalization(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "judgment.txt"
            path.write_text(
                "ﺧﻮﺍﻫﺎﻥ: ﻋﻠﻰ ﺍﺣﻤﺪﻱ\n"
                "ﺧﻮﺍﻧﺪﻫ: ﺭﺿﺎ ﻛﺮﻳﻤﻰ\n",
                encoding="utf-8",
            )

            candidates = extract_recipient_candidates(path)

            self.assertEqual([item.full_name for item in candidates], ["علی احمدی", "رضا کریمی"])
            self.assertEqual([item.role for item in candidates], ["plaintiff", "defendant"])
            self.assertTrue(all(item.evidence for item in candidates))
            self.assertTrue(all(item.extraction_method == "local_party" for item in candidates))

    def test_extract_recipients_flattens_multiple_parties_per_file(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "judgment.txt"
            path.write_text(
                "خواهان: علی احمدی\nخوانده: رضا کریمی",
                encoding="utf-8",
            )

            self.assertEqual(len(extract_recipients([path])), 2)

    def test_ignores_role_words_in_narrative_after_structured_section(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "judgment.txt"
            path.write_text(
                "خواهان: علی احمدی\n"
                "خواسته: مطالبه وجه\n"
                "دادگاه اظهار خواهان و خوانده را بررسی کرد.",
                encoding="utf-8",
            )

            candidates = extract_recipient_candidates(path)

            self.assertEqual([(item.role, item.full_name) for item in candidates], [("plaintiff", "علی احمدی")])

    def test_extracts_numbered_party_block_until_next_section(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "judgment.txt"
            path.write_text(
                "خواندگان:\n"
                "1- شرکت آب و برق به نشانی تهران\n"
                "2- رضا کریمی فرزند علی به نشانی شیراز\n"
                "خواسته: مطالبه وجه",
                encoding="utf-8",
            )

            candidates = extract_recipient_candidates(path)

            self.assertEqual([item.full_name for item in candidates], ["شرکت آب و برق", "رضا کریمی"])
            self.assertEqual([item.person_type for item in candidates], ["legal", "unknown"])

    def test_propagates_unique_declaration_number_across_same_notice(self) -> None:
        with TemporaryDirectory() as directory:
            source_dir = Path(directory) / "1405-07-19__140550100018568161__attachments-1"
            source_dir.mkdir()
            parties = source_dir / "parties.txt"
            reference = source_dir / "reference.txt"
            parties.write_text("خوانده: رضا کریمی", encoding="utf-8")
            reference.write_text(
                "شماره ابلاغیه: 140550100018568161\n"
                "شماره اظهارنامه: 140522000012345678",
                encoding="utf-8",
            )

            candidates = extract_recipients([parties, reference])

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].notice_number, "140550100018568161")
            self.assertEqual(candidates[0].related_declaration_number, "140522000012345678")

    def test_does_not_propagate_ambiguous_declaration_numbers(self) -> None:
        with TemporaryDirectory() as directory:
            source_dir = Path(directory) / "1405-07-19__140550100018568161__attachments-1"
            source_dir.mkdir()
            paths = []
            for name, text in (
                ("party.txt", "خوانده: رضا کریمی"),
                ("one.txt", "شماره اظهارنامه: 140522000012345678"),
                ("two.txt", "شماره اظهارنامه: 140522000087654321"),
            ):
                path = source_dir / name
                path.write_text(text, encoding="utf-8")
                paths.append(path)

            candidates = extract_recipients(paths)

            party = next(candidate for candidate in candidates if candidate.full_name)
            self.assertEqual(party.related_declaration_number, "")
            self.assertIn("related_declaration_ambiguous", party.issues)

    def test_empty_source_list_is_rejected(self) -> None:
        with self.assertRaisesRegex(DocumentExtractionError, "source_files_missing"):
            extract_recipients([])


if __name__ == "__main__":
    unittest.main()
