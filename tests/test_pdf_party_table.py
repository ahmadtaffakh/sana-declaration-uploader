import unittest

from sana_declaration_uploader.pdf_party_table import _extract_groups, _fix_word


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


if __name__ == "__main__":
    unittest.main()
