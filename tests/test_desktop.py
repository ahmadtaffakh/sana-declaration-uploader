import os
import json
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLabel,
    QPushButton,
    QWizard,
)

from sana_declaration_uploader.desktop import (
    DeclarationContentPage,
    RecipientReviewPage,
    RecipientWizard,
    _build_desktop_drafts,
    _load_login_settings,
    _save_login_settings,
)
from sana_declaration_uploader.models import RecipientCandidate


class RecipientWizardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_navigation_is_persian_and_aligned_to_the_right(self) -> None:
        wizard = RecipientWizard()
        wizard.show()
        self.app.processEvents()

        buttons = [
            wizard.button(QWizard.WizardButton.CancelButton),
            wizard.button(QWizard.WizardButton.BackButton),
            wizard.button(QWizard.WizardButton.NextButton),
        ]
        self.assertEqual([button.text() for button in buttons], [
            "انصراف", "قبلی", "بعدی",
        ])
        self.assertTrue(all(button.geometry().x() > wizard.width() / 2 for button in buttons))
        self.assertEqual(
            wizard.buttonText(QWizard.WizardButton.FinishButton),
            "شروع پردازش و ثبت",
        )
        self.assertFalse(wizard.button(QWizard.WizardButton.BackButton).icon().isNull())
        self.assertFalse(wizard.button(QWizard.WizardButton.NextButton).icon().isNull())
        self.assertGreater(buttons[1].geometry().x(), buttons[2].geometry().x())
        wizard.close()

    def test_pages_have_bold_titles_guidance_and_organizational_style(self) -> None:
        wizard = RecipientWizard()

        self.assertTrue(wizard.files_page.title().startswith("<b>"))
        self.assertTrue(wizard.review_page.title().startswith("<b>"))
        self.assertTrue(wizard.preprocessing_page.title().startswith("<b>"))
        self.assertTrue(wizard.content_page.title().startswith("<b>"))
        self.assertTrue(wizard.login_page.title().startswith("<b>"))
        guidance = wizard.findChildren(QLabel, "Guidance")
        self.assertEqual(len(guidance), 5)
        self.assertTrue(all(label.wordWrap() for label in guidance))
        self.assertIn("#1769AA", wizard.styleSheet())
        self.assertIn("alternate-background-color: #E9EFF5", wizard.styleSheet())
        self.assertIn(
            "حذف ردیف",
            [button.text() for button in wizard.review_page.findChildren(QPushButton)],
        )

    def test_review_edits_are_preserved_for_preprocessing(self) -> None:
        page = RecipientReviewPage()
        page.set_candidates((RecipientCandidate(
            source_path=Path("input.pdf"),
            full_name="نام اولیه",
            job_title="کارمند",
            address="نشانی اولیه",
        ),))

        page.table.item(0, page.HEADERS.index("شغل")).setText("بازنشسته")
        page.table.item(0, page.HEADERS.index("آدرس")).setText("نشانی اصلاح‌شده")

        candidate = page.candidates()[0]
        self.assertEqual(candidate.job_title, "بازنشسته")
        self.assertEqual(candidate.address, "نشانی اصلاح‌شده")

    def test_preprocessing_page_shows_codes_and_review_status(self) -> None:
        wizard = RecipientWizard()
        candidate = RecipientCandidate(
            source_path=Path("input.pdf"),
            full_name="نمونه",
            province="استان تهران",
            province_code="23",
            issues=("court_unit_unresolved",),
        )

        wizard.preprocessing_page.set_candidates((candidate,))

        table = wizard.preprocessing_page.table
        self.assertEqual(table.item(0, 0).text(), "نمونه")
        self.assertEqual(
            table.item(0, wizard.preprocessing_page.HEADERS.index("کد استان")).text(),
            "23",
        )
        self.assertEqual(table.item(0, table.columnCount() - 1).text(), "نیازمند تکمیل")

    def test_clicking_a_cell_selects_its_whole_row(self) -> None:
        wizard = RecipientWizard()

        self.assertEqual(
            wizard.review_page.table.selectionBehavior(),
            QAbstractItemView.SelectionBehavior.SelectRows,
        )
        self.assertEqual(
            wizard.preprocessing_page.table.selectionBehavior(),
            QAbstractItemView.SelectionBehavior.SelectRows,
        )
        self.assertTrue(wizard.review_page.table.alternatingRowColors())
        self.assertTrue(wizard.preprocessing_page.table.alternatingRowColors())

    def test_common_content_is_seeded_for_each_person_when_scope_changes(self) -> None:
        page = DeclarationContentPage()
        page.set_candidates((
            RecipientCandidate(source_path=Path("one.pdf"), full_name="شخص اول"),
            RecipientCandidate(source_path=Path("two.pdf"), full_name="شخص دوم"),
        ))
        page.description.setPlainText("شرح مشترک")

        page.shared_checkbox.setChecked(False)

        payloads = page.payloads()
        self.assertEqual(payloads[0]["description"], "شرح مشترک")
        self.assertEqual(payloads[1]["description"], "شرح مشترک")
        self.assertTrue(page.recipient_combo.isEnabled())

    def test_login_json_contains_all_fields_when_requested(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "login.json"

            _save_login_settings(
                "national", "password", "legal", "otp", remember=True, path=path
            )

            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw, {
                "national_code": "national",
                "password": "password",
                "legal_identity": "legal",
                "otp": "otp",
            })
            self.assertEqual(_load_login_settings(path), raw)

            _save_login_settings("", "", "", "", remember=False, path=path)
            self.assertFalse(path.exists())

    def test_finish_invokes_registration_instead_of_login_check(self) -> None:
        wizard = RecipientWizard()
        page = wizard.login_page
        page.national_code.setText("national")
        page.password.setText("password")
        page.legal_identity.setText("legal")
        page.otp.setText("otp")
        received = []
        page.registration_callback = lambda credentials: received.append(credentials) or True

        with patch("sana_declaration_uploader.desktop._save_login_settings"):
            self.assertTrue(page.validatePage())

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].otp, "otp")
        self.assertIn(
            "آزمایش ورود بدون ثبت",
            [button.text() for button in page.findChildren(QPushButton)],
        )

    def test_reviewed_rows_build_registration_drafts_with_exact_lookups(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.pdf"
            source.write_bytes(b"%PDF-test")
            candidate = RecipientCandidate(
                source_path=source,
                full_name="نام خانوادگی",
                first_name="نام",
                last_name="خانوادگی",
                gender="مرد",
                national_id="0000000000",
                birth_date="1370/01/01",
                address="خیابان نمونه",
                job_title="آزاد",
                province="استان تهران",
                county="شهرستان تهران",
                city="تهران",
                court_unit_name="دادگستري كل استان تهران",
            )

            drafts = _build_desktop_drafts(
                (candidate,),
                {-1: {
                    "subject": "موضوع",
                    "description": "شرح",
                    "attachments": (),
                }},
                shared=True,
                lookup_dir=Path(__file__).resolve().parents[1] / "lookups",
            )

            self.assertEqual(len(drafts), 1)
            self.assertEqual(drafts[0].fields["recipient_first_name"], "نام")
            self.assertEqual(drafts[0].fields["job_code"], "00002")
            self.assertEqual(drafts[0].fields["province_code"], "23")
            self.assertEqual(drafts[0].fields["court_unit_code"], "00159")
            self.assertEqual(drafts[0].fields["declaration_text"], "شرح")


if __name__ == "__main__":
    unittest.main()
