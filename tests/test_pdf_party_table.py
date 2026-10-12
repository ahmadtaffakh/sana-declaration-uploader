import unittest

from sana_declaration_uploader.pdf_party_table import (
    _extract_groups,
    _extract_notice_groups,
    _fix_word,
)


def _word(text: str, x0: float, x1: float, top: float) -> dict[str, object]:
    return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": top + 10}


class PdfPartyTableTests(unittest.TestCase):
    def test_restores_pdf_visual_order_and_arabic_letter_variants(self) -> None:
        self.assertEqual(_fix_word("نﺎھﺍﻮﺧ"), "خواهان")

    def test_groups_all_plaintiffs_and_keeps_defendants_separate(self) -> None:
        words = [
            _word("مشخصات", 515, 565, 140),
            _word("شغل", 343, 370, 140),
            _word("خواهان", 520, 558, 168),
            _word("علی", 458, 482, 168),
            _word("احمدی", 399, 435, 168),
            _word("تهران", 220, 270, 168),
            _word("رضا", 458, 482, 198),
            _word("کریمی", 399, 435, 198),
            _word("شیراز", 220, 270, 198),
            _word("خوانده", 520, 558, 240),
            _word("شرکت", 405, 440, 240),
            _word("نمونه", 360, 400, 240),
            _word("اصفهان", 220, 270, 240),
            _word("وکیل", 525, 552, 280),
        ]

        groups = _extract_groups(words, 596)

        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["role"], "plaintiff")
        self.assertEqual(groups[0]["names"], ("علی احمدی", "رضا کریمی"))
        self.assertEqual(groups[1]["role"], "defendant")
        self.assertEqual(groups[1]["names"], ("شرکت نمونه",))

    def test_extracts_declarant_and_each_addressee_from_declaration_table(self) -> None:
        words = [
            _word("مشخصات", 515, 565, 140),
            _word("شغل", 343, 370, 140),
            _word("اظهارکننده", 510, 566, 168),
            _word("علی", 458, 482, 168),
            _word("احمدی", 399, 435, 168),
            _word("پور", 399, 435, 175),
            _word("کارمند", 340, 370, 168),
            _word("تهران", 220, 270, 168),
            _word("مخاطب", 518, 558, 198),
            _word("شرکت", 405, 440, 198),
            _word("نمونه", 360, 400, 198),
            _word("مرکزی", 360, 400, 205),
            _word("اصفهان", 220, 270, 198),
            _word("اداره", 405, 440, 228),
            _word("آزمایشی", 355, 400, 228),
            _word("شیراز", 220, 270, 228),
            _word("موضوع", 520, 558, 270),
        ]

        groups = _extract_groups(words, 596)

        self.assertEqual(
            [(group["role"], group["names"]) for group in groups],
            [
                ("declarant", ("علی احمدی پور",)),
                ("addressee", ("شرکت نمونه مرکزی",)),
                ("addressee", ("اداره آزمایشی",)),
            ],
        )
        self.assertEqual(groups[0]["jobs"], ("کارمند",))

    def test_groups_appellants_and_keeps_respondents_separate(self) -> None:
        words = [
            _word("مشخصات", 515, 565, 140),
            _word("شغل", 343, 370, 140),
            _word("تجدیدنظرخواه", 500, 567, 168),
            _word("علی", 458, 482, 168),
            _word("احمدی", 399, 435, 168),
            _word("تهران", 220, 270, 168),
            _word("تجدیدنظرخوانده", 490, 568, 210),
            _word("شرکت", 405, 440, 210),
            _word("نمونه", 360, 400, 210),
            _word("اصفهان", 220, 270, 210),
            _word("وکیل", 525, 552, 250),
        ]

        groups = _extract_groups(words, 596)

        self.assertEqual(groups[0]["role"], "appellant")
        self.assertEqual(groups[0]["names"], ("علی احمدی",))
        self.assertEqual(groups[1]["role"], "respondent")
        self.assertEqual(groups[1]["names"], ("شرکت نمونه",))

    def test_extracts_notice_addressee_from_notice_table(self) -> None:
        words = [
            _word("مشخصات", 79, 126, 203),
            _word("شونده:", 17, 52, 203),
            _word("کدملی", 328, 364, 228),
            _word("شرکت", 500, 540, 245),
            _word("نمونه", 450, 495, 245),
            _word("12345678901", 310, 370, 245),
        ]

        groups = _extract_notice_groups(words, 596)

        self.assertEqual(groups[0]["role"], "addressee")
        self.assertEqual(groups[0]["names"], ("شرکت نمونه",))
        self.assertEqual(groups[0]["national_ids"], ("12345678901",))


if __name__ == "__main__":
    unittest.main()
