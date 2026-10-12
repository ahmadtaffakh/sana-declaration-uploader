import os
import json
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
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
    _load_attachment_settings,
    _load_declaration_settings,
    _load_login_settings,
    _save_attachment_settings,
    _save_declaration_settings,
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

    def test_back_button_keeps_its_position_on_the_last_page(self) -> None:
        positions = []
        for page_name in ("content_page_id", "login_page_id"):
            wizard = RecipientWizard()
            wizard.setStartId(getattr(wizard, page_name))
            wizard.show()
            self.app.processEvents()
            positions.append(
                wizard.button(QWizard.WizardButton.BackButton).geometry().x()
            )
            wizard.close()

        self.assertEqual(positions[0], positions[1])

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

        page.set_candidates(page.candidates())
        self.assertEqual(
            page.table.item(0, page.HEADERS.index("شغل")).text(),
            "بازنشسته",
        )
        self.assertEqual(
            page.table.item(0, page.HEADERS.index("آدرس")).text(),
            "نشانی اصلاح‌شده",
        )

    def test_review_rows_can_be_removed_by_checkbox_or_row_button(self) -> None:
        page = RecipientReviewPage()
        page.set_candidates((
            RecipientCandidate(source_path=Path("one.docx"), full_name="شخص اول"),
            RecipientCandidate(source_path=Path("two.docx"), full_name="شخص دوم"),
            RecipientCandidate(source_path=Path("three.docx"), full_name="شخص سوم"),
        ))

        page.table.item(0, page.HEADERS.index("انتخاب")).setCheckState(
            Qt.CheckState.Checked
        )
        page._remove_selected()
        self.assertEqual(
            [candidate.full_name for candidate in page.candidates()],
            ["شخص دوم", "شخص سوم"],
        )

        button = page.table.cellWidget(0, page.HEADERS.index("حذف"))
        button.click()
        self.assertEqual(
            [candidate.full_name for candidate in page.candidates()],
            ["شخص سوم"],
        )

    def test_returning_to_file_page_reloads_removed_rows(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "input.docx"
            source.write_bytes(b"test")
            candidates = (
                RecipientCandidate(source_path=source, full_name="شخص اول"),
                RecipientCandidate(source_path=source, full_name="شخص دوم"),
            )
            wizard = RecipientWizard()
            wizard.files_page._add_paths((source,))
            wizard._loaded_paths = (source.resolve(),)
            wizard._preprocess_source = candidates
            wizard.review_page.set_candidates(candidates[1:])

            wizard._page_changed(wizard.files_page_id)
            with patch(
                "sana_declaration_uploader.desktop.extract_recipients",
                return_value=candidates,
            ) as extract:
                wizard._page_changed(wizard.review_page_id)

            extract.assert_called_once_with([source.resolve()])
            self.assertEqual(
                [candidate.full_name for candidate in wizard.review_page.candidates()],
                ["شخص اول", "شخص دوم"],
            )
            self.assertEqual(wizard._preprocess_source, ())

    def test_preprocessing_location_edits_survive_reloading_the_same_rows(self) -> None:
        wizard = RecipientWizard()
        page = wizard.preprocessing_page
        page.set_candidates((RecipientCandidate(
            source_path=Path("input.pdf"),
            full_name="نام نمونه",
            job_title="کارمند",
            city="تهران",
        ),))

        page.table.item(0, page.HEADERS.index("شهر")).setText("ری")
        page.table.item(0, page.HEADERS.index("کد شهر")).setText("123")

        page.set_candidates(page.candidates())

        for header, expected in (
            ("شهر", "ری"),
            ("کد شهر", "123"),
        ):
            self.assertEqual(
                page.table.item(0, page.HEADERS.index(header)).text(),
                expected,
            )

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
        for header in ("شغل", "کد شغل"):
            self.assertFalse(
                table.item(0, wizard.preprocessing_page.HEADERS.index(header)).flags()
                & Qt.ItemFlag.ItemIsEditable
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

    def test_extracted_descriptions_seed_separate_recipient_content(self) -> None:
        page = DeclarationContentPage()
        page.set_candidates((
            RecipientCandidate(
                source_path=Path("one.docx"),
                full_name="شخص اول",
                declaration_subject="سایر موضوعات",
                declaration_text="شرح اول",
            ),
            RecipientCandidate(
                source_path=Path("two.docx"),
                full_name="شخص دوم",
                declaration_subject="سایر موضوعات",
                declaration_text="شرح دوم",
            ),
        ))

        self.assertFalse(page.shared_checkbox.isChecked())
        payloads = page.payloads()
        self.assertEqual(payloads[0]["description"], "شرح اول")
        self.assertEqual(payloads[1]["description"], "شرح دوم")

    def test_declaration_fields_survive_back_navigation_and_reentry(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "sana_declaration_uploader.desktop._save_declaration_settings"
        ):
            attachment = Path(directory) / "attachment.pdf"
            attachment.write_bytes(b"%PDF-test")
            page = DeclarationContentPage()
            candidates = (
                RecipientCandidate(source_path=Path("one.pdf"), full_name="شخص اول"),
            )
            page.set_candidates(candidates)
            page.subject.setText("موضوع اظهارنامه")
            page.description.setPlainText("شرح اظهارنامه")
            page.attachment_kind.setText("سایر دلایل و مستندات")
            page.attachment_number.setText("۱۲۳")
            page.attachment_date.setText("۱۴۰۵/۰۷/۲۰")
            page._add_attachment(attachment)

            page.cleanupPage()
            page.set_candidates(candidates)

            self.assertEqual(page.subject.text(), "موضوع اظهارنامه")
            self.assertEqual(page.description.toPlainText(), "شرح اظهارنامه")
            self.assertEqual(page.attachment_kind.text(), "سایر دلایل و مستندات")
            self.assertEqual(page.attachment_number.text(), "۱۲۳")
            self.assertEqual(page.attachment_date.text(), "۱۴۰۵/۰۷/۲۰")
            self.assertEqual(page.attachment_list.count(), 1)
            self.assertEqual(page._attachment_paths(), [attachment.resolve()])

            renamed_candidates = (
                RecipientCandidate(
                    source_path=Path("one.pdf"), full_name="نام اصلاح‌شده"
                ),
            )
            page.set_candidates(renamed_candidates)
            self.assertEqual(page.subject.text(), "موضوع اظهارنامه")
            self.assertEqual(page.description.toPlainText(), "شرح اظهارنامه")

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

    def test_attachment_defaults_are_persisted_for_the_next_run(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "attachment-defaults.json"

            _save_attachment_settings(
                "نوع دلخواه", "شماره دلخواه", "۱۴۰۵/۰۷/۲۱", path=path
            )

            self.assertEqual(_load_attachment_settings(path), {
                "attachment_kind": "نوع دلخواه",
                "attachment_number": "شماره دلخواه",
                "attachment_date": "۱۴۰۵/۰۷/۲۱",
            })

    def test_declaration_defaults_are_persisted_for_the_next_run(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "declaration-defaults.json"

            _save_declaration_settings(
                "موضوع دلخواه", "شرح دلخواه", path=path
            )

            self.assertEqual(_load_declaration_settings(path), {
                "subject": "موضوع دلخواه",
                "description": "شرح دلخواه",
            })

    def test_declaration_fields_start_with_defaults_and_save_edits(self) -> None:
        defaults = {
            "subject": "سایر موضوعات",
            "description": "شرح موضوع",
        }
        with (
            patch(
                "sana_declaration_uploader.desktop._load_declaration_settings",
                return_value=defaults,
            ),
            patch(
                "sana_declaration_uploader.desktop._save_declaration_settings"
            ) as save,
        ):
            page = DeclarationContentPage()
            self.assertEqual(page.subject.text(), "سایر موضوعات")
            self.assertEqual(page.description.toPlainText(), "شرح موضوع")

            page.subject.setText("موضوع جدید")
            page.description.setPlainText("شرح جدید")
            page.cleanupPage()

            save.assert_called_once_with("موضوع جدید", "شرح جدید")

    def test_attachment_fields_start_with_defaults_and_save_edits(self) -> None:
        defaults = {
            "attachment_kind": "سایر دلایل و مستندات",
            "attachment_number": "شماره مدرک",
            "attachment_date": "۱۴۰۵/۰۷/۲۰",
        }
        with (
            patch(
                "sana_declaration_uploader.desktop._load_attachment_settings",
                return_value=defaults,
            ),
            patch(
                "sana_declaration_uploader.desktop._save_attachment_settings"
            ) as save,
        ):
            page = DeclarationContentPage()
            self.assertEqual(page.attachment_kind.text(), defaults["attachment_kind"])
            self.assertEqual(
                page.attachment_number.text(), defaults["attachment_number"]
            )
            self.assertEqual(page.attachment_date.text(), defaults["attachment_date"])

            page.attachment_kind.setText("نوع جدید")
            page.attachment_number.setText("۴۲")
            page.attachment_date.setText("۱۴۰۵/۰۷/۲۲")
            page.attachment_date.editingFinished.emit()

            save.assert_called_once_with("نوع جدید", "۴۲", "۱۴۰۵/۰۷/۲۲")

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
                court_unit_name="واحد قدیمی که باید نادیده گرفته شود",
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
            self.assertNotIn("court_unit_code", drafts[0].fields)
            self.assertEqual(drafts[0].fields["declaration_text"], "شرح")

    def test_draft_forces_free_job_and_tehran_for_unknown_location(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.pdf"
            source.write_bytes(b"%PDF-test")
            candidate = RecipientCandidate(
                source_path=source,
                full_name="نام خانوادگی",
                first_name="نام",
                last_name="خانوادگی",
                job_title="کارمند",
                province="استان ناشناخته",
                county="شهرستان ناشناخته",
                city="شهر ناشناخته",
            )

            draft = _build_desktop_drafts(
                (candidate,), {-1: {}}, shared=True,
                lookup_dir=Path(__file__).resolve().parents[1] / "lookups",
            )[0]

            self.assertEqual(draft.fields["job_title"], "آزاد")
            self.assertEqual(draft.fields["province"], "استان تهران")
            self.assertEqual(draft.fields["county"], "شهرستان تهران")
            self.assertEqual(draft.fields["city"], "تهران")


if __name__ == "__main__":
    unittest.main()
