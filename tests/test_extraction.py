from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from docx import Document

from sana_declaration_uploader.extraction import (
    DocumentExtractionError,
    _address_locations,
    _split_father_name,
    _split_natural_name,
    extract_address_details,
    extract_recipient,
    extract_recipient_candidates,
    extract_recipients,
    format_recipient_address,
)


class RecipientExtractionTests(unittest.TestCase):
    def test_father_name_stops_before_identity_and_address_details(self) -> None:
        self.assertEqual(
            _split_father_name(
                "علی رضایی فرزند حسن به شماره ملی 0000000000 به آدرس: تهران"
            ),
            ("علی رضایی", "حسن"),
        )
        self.assertEqual(
            _split_father_name("علی رضایی فرزند محمد رضا کد ملی 0000000000"),
            ("علی رضایی", "محمد رضا"),
        )

    def test_address_starting_with_county_preserves_it_for_parent_lookup(self) -> None:
        self.assertEqual(
            _address_locations("شهرستان ری - شهر ری - خیابان نمونه"),
            {
                "province": "شهرستان ری",
                "county": "شهرستان ری",
                "city": "ری",
            },
        )

    def test_splits_four_word_name_as_given_name_and_compound_family_name(self) -> None:
        self.assertEqual(
            _split_natural_name("مریم شمسیان جهان بخش", "natural"),
            ("مریم", "شمسیان جهان بخش"),
        )

    def test_extracts_street_and_postal_code_after_city(self) -> None:
        address = (
            "استان خوزستان - شهرستان امیدیه - شهر امیدیه - "
            "خیابان اصلی آزادی - کد پستی ۱۲۳۴۵۶۷۸۹۰"
        )

        self.assertEqual(
            extract_address_details(
                address, "استان خوزستان", "شهرستان امیدیه", "امیدیه"
            ),
            ("آزادی", "1234567890"),
        )

    def test_address_without_street_uses_continuation_then_default(self) -> None:
        self.assertEqual(
            extract_address_details(
                "استان فارس - شهرستان لار - شهر لار - کوچه بهار",
                "استان فارس", "شهرستان لار", "لار",
            ),
            ("کوچه بهار", ""),
        )
        self.assertEqual(extract_address_details(""), ("خیابان اصلی", ""))

    def test_main_street_removes_city_and_non_street_address_parts(self) -> None:
        self.assertEqual(
            extract_address_details(
                "شهر تهران، خیابان اصلی تهران، خیابان ولیعصر، پلاک ۱۲، "
                "واحد ۳، تلفن: ۰۲۱۱۲۳۴۵۶۷۸",
                city="شهر تهران",
            ),
            ("خیابان ولیعصر", ""),
        )

    def test_main_street_keeps_street_name_and_cleans_separators(self) -> None:
        self.assertEqual(
            extract_address_details(
                "استان تهران - شهرستان تهران - شهر تهران - خیابان اصلی: "
                "تهران، بلوار کشاورز، پلاک ۱۰ - کدپستی ۱۲۳۴۵۶۷۸۹۰",
                "استان تهران", "شهرستان تهران", "تهران",
            ),
            ("بلوار کشاورز", "1234567890"),
        )

    def test_formats_address_hierarchy_without_repeating_locations(self) -> None:
        actual = format_recipient_address(
            "استان خوزستان - شهرستان امیدیه - شهر امیدیه - "
            "خیابان آزادی، پلاک ۱۲ - کد پستی ۱۲۳۴۵۶۷۸۹۰",
            "استان خوزستان",
            "شهرستان امیدیه",
            "امیدیه",
        )

        self.assertEqual(
            actual,
            "استان خوزستان - شهرستان امیدیه - شهر امیدیه - "
            "خیابان آزادی، پلاک 12 - کد پستی 1234567890",
        )
        self.assertEqual(actual.count("خوزستان"), 1)
        self.assertEqual(actual.count("امیدیه"), 2)

    def test_formats_address_with_hierarchy_when_remainder_is_empty(self) -> None:
        self.assertEqual(
            format_recipient_address(
                "استان تهران - شهرستان تهران - شهر تهران",
                "استان تهران",
                "شهرستان تهران",
                "تهران",
            ),
            "استان تهران - شهرستان تهران - شهر تهران",
        )

    def test_address_removes_tehran_region_but_keeps_postal_code(self) -> None:
        self.assertEqual(
            format_recipient_address(
                "منطقه تهران - خیابان آزادی - کد پستی ۱۲۳۴۵۶۷۸۹۰",
                "استان تهران", "شهرستان تهران", "تهران",
            ),
            "استان تهران - شهرستان تهران - شهر تهران - خیابان آزادی - "
            "کد پستی 1234567890",
        )

    def test_cleans_organization_prefix_and_keeps_multiword_given_name(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "declarations.docx"
            document = Document()
            table = document.add_table(rows=3, cols=5)
            for index, value in enumerate((
                "ردیف", "شماره و تاریخ اظهارنامه", "نام و نام خانوادگی",
                "کد ملی", "آدرس",
            )):
                table.cell(0, index).text = value
            for index, value in enumerate((
                "1", "1405220012345678 1405/01/02",
                "مرکز نمونه درمان بخش- آقای روح اله فکوری", "ندارد",
                "استان لرستان شهرستان خرم آباد کوی نمونه بیمارستان نمونه",
            )):
                table.cell(1, index).text = value
            table.cell(2, 0).merge(table.cell(2, 4)).text = (
                "مخاطب محترم: این متن ساختگی فقط برای بررسی پاک‌سازی نام و "
                "نشانی بدون جداکننده تهیه شده و طول کافی دارد."
            )
            document.save(path)

            candidate = extract_recipient_candidates(path)[0]

            self.assertEqual(candidate.full_name, "روح اله فکوری")
            self.assertEqual(candidate.first_name, "روح اله")
            self.assertEqual(candidate.last_name, "فکوری")
            self.assertEqual(candidate.province, "استان لرستان")
            self.assertEqual(candidate.county, "شهرستان خرم آباد")
            self.assertEqual(candidate.city, "خرم آباد")
            self.assertEqual(candidate.related_declaration_date, "1405/01/02")

    def test_extracts_each_declaration_table_row_with_shared_description(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "declarations.docx"
            document = Document()
            table = document.add_table(rows=4, cols=5)
            headers = (
                "ردیف", "شماره و تاریخ اظهارنامه", "نام و نام خانوادگی",
                "کد ملی", "آدرس",
            )
            for index, value in enumerate(headers):
                table.cell(0, index).text = value
            values = (
                ("1", "1405220012345678 5/6/1405", "علی رضایی", "1234567891", "تهران"),
                ("2", "1405220087654321 مورخ 7/6/1405", "رضا کریمی فرزند حسن", "ندارد", "استان فارس- شهرستان شیراز- شیراز- خیابان نمونه"),
            )
            for row_index, row_values in enumerate(values, start=1):
                for column, value in enumerate(row_values):
                    table.cell(row_index, column).text = value
            description = (
                "مخاطب محترم: این متن ساختگی برای بررسی استخراج شرح اظهارنامه "
                "از سلول ادغام‌شده جدول ایجاد شده و هیچ محتوای حقوقی واقعی ندارد."
            )
            table.cell(3, 0).merge(table.cell(3, 4)).text = description
            document.save(path)

            candidates = extract_recipient_candidates(path)

            self.assertEqual(len(candidates), 2)
            self.assertEqual(candidates[0].first_name, "علی")
            self.assertEqual(candidates[0].last_name, "رضایی")
            self.assertEqual(candidates[0].related_declaration_number, "1405220012345678")
            self.assertEqual(candidates[0].related_declaration_date, "5/6/1405")
            self.assertEqual(candidates[0].province, "استان تهران")
            self.assertEqual(candidates[0].county, "شهرستان تهران")
            self.assertEqual(candidates[0].city, "تهران")
            self.assertEqual(candidates[0].declaration_subject, "سایر موضوعات")
            self.assertEqual(candidates[0].declaration_text, description)
            self.assertEqual(candidates[1].full_name, "رضا کریمی")
            self.assertEqual(candidates[1].father_name, "حسن")
            self.assertEqual(candidates[1].national_id, "")
            self.assertEqual(candidates[1].province, "استان فارس")
            self.assertEqual(candidates[1].county, "شهرستان شیراز")
            self.assertEqual(candidates[1].city, "شیراز")

    def test_no_national_id_uses_unlabeled_address_hierarchy_for_review(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "address.docx"
            document = Document()
            table = document.add_table(rows=3, cols=5)
            for index, value in enumerate((
                "ردیف", "شماره و تاریخ اظهارنامه", "نام و نام خانوادگی",
                "کد ملی", "آدرس",
            )):
                table.cell(0, index).text = value
            for index, value in enumerate((
                "1", "1405220012345678 5/6/1405", "علی رضایی",
                "ندارد", "فارس- شیراز- صدرا- خیابان نمونه",
            )):
                table.cell(1, index).text = value
            table.cell(2, 0).merge(table.cell(2, 4)).text = (
                "مخاطب محترم: این متن ساختگی فقط برای تکمیل حداقل طول شرح و "
                "آزمایش تجزیه سلسله‌مراتب آدرس بدون برچسب استفاده می‌شود."
            )
            document.save(path)

            candidate = extract_recipient_candidates(path)[0]

            self.assertEqual(candidate.province, "استان فارس")
            self.assertEqual(candidate.county, "شهرستان شیراز")
            self.assertEqual(candidate.city, "صدرا")
            self.assertNotIn("address_location_review_required", candidate.issues)

    def test_unlabeled_county_with_en_dash_and_neighborhood_uses_county_city(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "address.docx"
            document = Document()
            table = document.add_table(rows=3, cols=5)
            for index, value in enumerate((
                "ردیف", "شماره و تاریخ اظهارنامه", "نام و نام خانوادگی",
                "کد ملی", "آدرس",
            )):
                table.cell(0, index).text = value
            for index, value in enumerate((
                "1", "1405220012345678 5/6/1405", "علی رضایی",
                "ندارد", "خوزستان - امیدیه – شهرک گلستان - خیابان نمونه",
            )):
                table.cell(1, index).text = value
            table.cell(2, 0).merge(table.cell(2, 4)).text = (
                "مخاطب محترم: این متن ساختگی فقط برای بررسی انواع جداکننده "
                "نشانی ایجاد شده و دارای طول کافی برای استخراج است."
            )
            document.save(path)

            candidate = extract_recipient_candidates(path)[0]

            self.assertEqual(candidate.province, "استان خوزستان")
            self.assertEqual(candidate.county, "شهرستان امیدیه")
            self.assertEqual(candidate.city, "امیدیه")

    def test_incomplete_address_without_national_id_uses_tehran_defaults(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "address.docx"
            document = Document()
            table = document.add_table(rows=3, cols=5)
            for index, value in enumerate((
                "ردیف", "شماره و تاریخ اظهارنامه", "نام و نام خانوادگی",
                "کد ملی", "آدرس",
            )):
                table.cell(0, index).text = value
            for index, value in enumerate((
                "1", "1405220012345678 5/6/1405", "علی رضایی",
                "ندارد", "خیابان نمونه",
            )):
                table.cell(1, index).text = value
            table.cell(2, 0).merge(table.cell(2, 4)).text = (
                "مخاطب محترم: این متن ساختگی فقط برای بررسی پیش فرض نشانی ناقص "
                "ایجاد شده و دارای طول کافی برای استخراج شرح اظهارنامه است."
            )
            document.save(path)

            candidate = extract_recipient_candidates(path)[0]

            self.assertEqual(candidate.province, "استان تهران")
            self.assertEqual(candidate.county, "شهرستان تهران")
            self.assertEqual(candidate.city, "تهران")
            self.assertNotIn("address_location_review_required", candidate.issues)

    def test_extracts_labeled_legal_addressee_without_treating_body_as_name(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "legal.docx"
            document = Document()
            table = document.add_table(rows=3, cols=3)
            for index, value in enumerate((
                "مشخصات و اقامتگاه اظهارکننده",
                "موضوع اظهارنامه",
                "مشخصات و اقامتگاه مخاطب",
            )):
                table.cell(0, index).text = value
            table.cell(1, 0).text = "شرکت اظهارکننده نمونه"
            table.cell(1, 1).text = "اظهارنامه شماره 1405220012345678 مورخ 10/06/1405"
            table.cell(1, 2).text = (
                "شرکت مخاطب نمونه، استان بوشهر، شهرستان بوشهر، شهر خارک، خیابان نمونه"
            )
            table.cell(2, 0).merge(table.cell(2, 2)).text = (
                "مخاطب محترم: این شرح ساختگی طول کافی دارد و فقط برای اطمینان از "
                "استخراج صحیح مخاطب حقوقی و نادیده گرفتن متن بدنه ایجاد شده است."
            )
            document.save(path)

            candidate = extract_recipient_candidates(path)[0]

            self.assertEqual(candidate.full_name, "شرکت مخاطب نمونه")
            self.assertEqual(candidate.person_type, "legal")
            self.assertEqual(candidate.province, "استان بوشهر")
            self.assertEqual(candidate.county, "شهرستان بوشهر")
            self.assertEqual(candidate.city, "خارک")
            self.assertEqual(candidate.related_declaration_date, "10/06/1405")

    def test_extracts_labeled_recipient_fields_from_word(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "person.docx"
            document = Document()
            for value in (
                "نام و نام خانوادگی: علی رضایی",
                "کد ملی: ۱۲۳۴۵۶۷۸۹۱",
                "شماره شناسنامه: ۷۶۵۴۳۲۱",
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
            self.assertEqual(candidate.identity_no, "7654321")
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
            self.assertEqual(
                [item.source_recipient_ordinal for item in candidates], [1, 2]
            )
            self.assertTrue(all(item.evidence for item in candidates))
            self.assertTrue(all(item.extraction_method == "local_party" for item in candidates))

    def test_extract_recipients_flattens_multiple_parties_per_file(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "judgment.txt"
            path.write_text(
                "خواهان: علی احمدی\nخوانده: رضا کریمی",
                encoding="utf-8",
            )

            candidates = extract_recipients([path])
            self.assertEqual(len(candidates), 2)
            self.assertEqual(
                [item.source_recipient_ordinal for item in candidates], [1, 2]
            )

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

    def test_structured_but_unparsed_pdf_does_not_fall_back_to_narrative(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "unknown.pdf"
            path.write_bytes(b"placeholder")
            table_result = ({
                "role": "",
                "names": (),
                "addresses": (),
                "person_type": "unknown",
                "evidence": "جدول اشخاص شناسایی شد اما قابل استخراج نبود.",
                "issues": ("party_table_unparsed",),
            },)
            with (
                patch(
                    "sana_declaration_uploader.extraction._read_text",
                    return_value="مخاطبین: شرکت اشتباه در متن شرح",
                ),
                patch(
                    "sana_declaration_uploader.extraction.extract_pdf_party_groups",
                    return_value=table_result,
                ),
            ):
                candidates = extract_recipient_candidates(path)

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].full_name, "")
            self.assertIn("party_table_unparsed", candidates[0].issues)

    def test_unique_notice_addressee_enriches_matching_party_without_duplicate(self) -> None:
        with TemporaryDirectory() as directory:
            source_dir = Path(directory) / "1405-07-19__140550100018568161__attachments-1"
            source_dir.mkdir()
            appeal = source_dir / "appeal.pdf"
            notice = source_dir / "notice.pdf"
            appeal.write_bytes(b"appeal")
            notice.write_bytes(b"notice")

            def table_groups(path: Path):
                if path.name == "appeal.pdf":
                    return ({
                        "role": "respondent",
                        "names": ("شرکت نمونه",),
                        "addresses": ("",),
                        "person_type": "legal",
                        "evidence": "شرکت نمونه",
                    },)
                return ({
                    "role": "addressee",
                    "names": ("نمونه",),
                    "addresses": ("",),
                    "national_ids": ("12345678901",),
                    "person_type": "legal",
                    "evidence": "نمونه",
                },)

            with (
                patch(
                    "sana_declaration_uploader.extraction._read_text",
                    return_value="",
                ),
                patch(
                    "sana_declaration_uploader.extraction.extract_pdf_party_groups",
                    side_effect=table_groups,
                ),
            ):
                candidates = extract_recipients([appeal, notice])

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].role, "respondent")
            self.assertEqual(candidates[0].national_id, "12345678901")

    def test_empty_source_list_is_rejected(self) -> None:
        with self.assertRaisesRegex(DocumentExtractionError, "source_files_missing"):
            extract_recipients([])


if __name__ == "__main__":
    unittest.main()
