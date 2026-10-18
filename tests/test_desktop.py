import os
import json
import threading
import time
import unittest
from datetime import date
from queue import Queue
from threading import Event
from unittest.mock import Mock, patch
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QWizard,
)

from sana_declaration_uploader.desktop import (
    DeclarationContentPage,
    PreprocessingPage,
    RecipientReviewPage,
    RecipientWizard,
    _build_desktop_drafts,
    _days_since_related_date,
    _load_attachment_settings,
    _load_declaration_settings,
    _load_last_source_directory,
    _load_login_settings,
    _load_saved_attachment_paths,
    _save_attachment_settings,
    _save_declaration_settings,
    _save_last_source_directory,
    _save_login_settings,
)
from sana_declaration_uploader.live_browser import LiveBrowserError, LoginCredentials
from sana_declaration_uploader.lookups import LookupResolutionError
from sana_declaration_uploader.models import (
    Attachment,
    DeclarationDraft,
    RecipientCandidate,
    UploadResult,
)
from sana_declaration_uploader.results import ResultPersistenceError, save_verified_result


class RecipientWizardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _wait_for_background(
        self,
        wizard: RecipientWizard,
        timeout: float = 3.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        while wizard._background_thread is not None and time.monotonic() < deadline:
            self.app.processEvents()
            thread = wizard._background_thread
            if thread is not None:
                thread.wait(10)
        self.app.processEvents()
        self.assertIsNone(wizard._background_thread)

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
            "ثبت نهایی در ثنا با روش مرورگر",
        )
        finish_button = wizard.button(QWizard.WizardButton.FinishButton)
        self.assertEqual(finish_button.objectName(), "DangerButton")
        self.assertIn("غیرقابل‌بازگشت", finish_button.toolTip())
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

    def test_last_page_has_read_only_copyable_python_output(self) -> None:
        wizard = RecipientWizard()
        output = wizard.login_page.python_output

        output.setPlainText("traceback sample")
        self.assertTrue(output.isReadOnly())
        self.assertEqual(output.toPlainText(), "traceback sample")
        self.assertTrue(
            output.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse
        )

    def test_last_page_status_tab_tracks_each_source_recipient(self) -> None:
        page = RecipientWizard().login_page
        source = Path("shared.docx")
        drafts = (
            DeclarationDraft(
                Path("."),
                {
                    "recipient_first_name": "علی",
                    "recipient_last_name": "احمدی",
                    "declaration_subject": "موضوع اول",
                },
                source_path=source,
                source_recipient_ordinal=1,
            ),
            DeclarationDraft(
                Path("."),
                {
                    "recipient_first_name": "رضا",
                    "recipient_last_name": "کریمی",
                    "declaration_subject": "موضوع دوم",
                },
                source_path=source,
                source_recipient_ordinal=2,
            ),
        )

        page.set_operation_drafts(drafts, reset=True)
        page.update_operation_status({
            "source_path": str(source.resolve()),
            "source_recipient_ordinal": 1,
            "stage": "petition",
            "state": "success",
            "tracking_code": "T-FIRST",
        })
        page.update_operation_status({
            "source_path": str(source.resolve()),
            "source_recipient_ordinal": 2,
            "stage": "attachment",
            "state": "unknown",
            "completed": 1,
            "total": 2,
            "message": "نیازمند بررسی دستی",
        })

        self.assertEqual(page.output_tabs.tabText(0), "وضعیت اظهارنامه‌ها")
        self.assertEqual(page.output_tabs.tabText(1), "گزارش فنی")
        self.assertEqual(page.output_tabs.currentIndex(), 0)
        self.assertEqual(page.status_table.rowCount(), 2)
        tracking_column = page.STATUS_HEADERS.index("کد رهگیری")
        petition_column = page.STATUS_HEADERS.index("وضعیت اظهارنامه")
        attachment_column = page.STATUS_HEADERS.index("وضعیت پیوست")
        self.assertEqual(page.status_table.item(0, tracking_column).text(), "T-FIRST")
        self.assertEqual(page.status_table.item(0, petition_column).text(), "✓ موفق")
        self.assertEqual(
            page.status_table.item(1, attachment_column).text(),
            "؟ نیازمند بررسی 1 از 2",
        )

        page.append_output("خطای نمونه", level="error")
        self.assertEqual(page.output_tabs.tabText(1), "گزارش فنی — دارای خطا")

    def test_entering_last_page_prepares_status_rows_before_execution(self) -> None:
        wizard = RecipientWizard()
        draft = DeclarationDraft(
            Path("."),
            {
                "recipient_first_name": "نام",
                "recipient_last_name": "نمونه",
                "declaration_subject": "موضوع نمونه",
            },
            source_path=Path("input.docx"),
        )

        with (
            patch(
                "sana_declaration_uploader.desktop._build_desktop_drafts",
                return_value=(draft,),
            ),
            patch(
                "sana_declaration_uploader.desktop.read_saved_result",
                return_value={
                    "tracking_code": "TRACK-SAVED",
                    "registered_at": "1405/07/26",
                },
            ),
        ):
            wizard._page_changed(wizard.login_page_id)

        page = wizard.login_page
        self.assertEqual(page.status_table.rowCount(), 1)
        self.assertEqual(
            page.status_table.item(
                0, page.STATUS_HEADERS.index("نام مخاطب")
            ).text(),
            "نام نمونه",
        )
        self.assertEqual(
            page.status_table.item(
                0, page.STATUS_HEADERS.index("وضعیت اظهارنامه")
            ).text(),
            "✓ موفق",
        )
        self.assertEqual(
            page.status_table.item(
                0, page.STATUS_HEADERS.index("کد رهگیری")
            ).text(),
            "TRACK-SAVED",
        )
        self.assertEqual(
            page.status_table.item(
                0, page.STATUS_HEADERS.index("وضعیت اظهارنامه")
            ).foreground().color().name(),
            "#247a3d",
        )

    def test_last_page_log_is_colored_numbered_and_traceable(self) -> None:
        page = RecipientWizard().login_page
        page.start_log()
        page.append_output("مرحله جاری", level="progress")
        page.append_output("مرحله موفق", level="success")
        page.append_output("هشدار نمونه", level="warning")
        page.append_output("خطای نمونه", level="error")
        page.append_output("Traceback\nValueError", level="debug")

        text = page.python_output.toPlainText()
        html = page.python_output.toHtml().lower()
        self.assertRegex(text, r"RUN-\d{8}-\d{6}-\d{3}")
        self.assertIn("[#001]", text)
        self.assertIn("[#006]", text)
        for label in (
            "[اطلاعات]", "[در حال اجرا]", "[موفق]", "[هشدار]",
            "[خطا]", "[جزئیات فنی]",
        ):
            self.assertIn(label, text)
        for color in ("#e8f1fb", "#e6f7fa", "#e8f6ec", "#fff4dd", "#fdebec"):
            self.assertIn(f'bgcolor="{color}"', html)
        self.assertIn("dir='rtl'", html)
        self.assertIn("consolas", html)

    def test_last_page_log_escapes_markup_and_redacts_credentials(self) -> None:
        page = RecipientWizard().login_page
        page.national_code.setText("0012345678")
        page.password.setText("secret-password")
        page.legal_identity.setText("legal-secret")
        page.otp.setText("otp-secret")
        page.start_log()
        page.append_output(
            "<b>خطا</b> 0012345678 secret-password legal-secret otp-secret",
            level="error",
        )

        text = page.python_output.toPlainText()
        self.assertIn("<b>خطا</b>", text)
        self.assertEqual(text.count("[حذف‌شده]"), 4)
        for secret in (
            "0012345678", "secret-password", "legal-secret", "otp-secret",
        ):
            self.assertNotIn(secret, text)

    def test_file_page_shows_loaded_file_count(self) -> None:
        with TemporaryDirectory() as directory:
            first = Path(directory) / "one.docx"
            second = Path(directory) / "two.docx"
            first.write_bytes(b"test")
            second.write_bytes(b"test")
            page = RecipientWizard().files_page

            page._add_paths((first, second))

            self.assertEqual(
                page.file_count_label.text(), "تعداد فایل‌های بارگذاری‌شده: 2"
            )

    def test_file_page_reopens_last_selected_source_directory(self) -> None:
        with TemporaryDirectory() as directory:
            source_directory = Path(directory) / "documents"
            source_directory.mkdir()
            source = source_directory / "input.docx"
            source.write_bytes(b"test")
            with (
                patch(
                    "sana_declaration_uploader.desktop._load_last_source_directory",
                    return_value=source_directory,
                ),
                patch(
                    "sana_declaration_uploader.desktop._save_last_source_directory"
                ) as save,
                patch(
                    "sana_declaration_uploader.desktop.QFileDialog.getOpenFileNames",
                    return_value=([str(source)], ""),
                ) as choose,
            ):
                page = RecipientWizard().files_page
                page._choose_files()

        self.assertEqual(choose.call_args.args[2], str(source_directory))
        save.assert_called_once_with(source_directory.resolve())
        self.assertEqual(page.selected_paths(), [source.resolve()])

    def test_last_source_directory_is_saved_and_loaded(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_directory = root / "documents"
            source_directory.mkdir()
            settings_path = root / "file-selection.json"

            _save_last_source_directory(source_directory, settings_path)

            self.assertEqual(
                _load_last_source_directory(settings_path),
                source_directory.resolve(),
            )

    def test_review_edits_are_preserved_for_preprocessing(self) -> None:
        page = RecipientReviewPage()
        page.set_candidates((RecipientCandidate(
            source_path=Path("input.pdf"),
            full_name="نام اولیه",
            job_title="کارمند",
            address="نشانی اولیه",
        ),))

        page.table.item(0, page.HEADERS.index("آدرس")).setText("نشانی اصلاح‌شده")

        candidate = page.candidates()[0]
        self.assertEqual(candidate.job_title, "کارمند")
        self.assertEqual(candidate.address, "نشانی اصلاح‌شده")

        page.set_candidates(page.candidates())
        self.assertEqual(
            page.table.item(0, page.HEADERS.index("آدرس")).text(),
            "نشانی اصلاح‌شده",
        )

    def test_review_rows_can_be_removed_by_selection_or_row_button(self) -> None:
        page = RecipientReviewPage()
        page.set_candidates((
            RecipientCandidate(source_path=Path("one.docx"), full_name="شخص اول"),
            RecipientCandidate(source_path=Path("two.docx"), full_name="شخص دوم"),
            RecipientCandidate(source_path=Path("three.docx"), full_name="شخص سوم"),
        ))

        page.table.selectRow(0)
        page._remove_selected()
        self.assertEqual(
            [candidate.full_name for candidate in page.candidates()],
            ["شخص دوم", "شخص سوم"],
        )

        button = page.table.cellWidget(
            0, page.HEADERS.index("حذف")
        ).findChild(QPushButton, "TableIconButton")
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
                self._wait_for_background(wizard)

            extract.assert_called_once_with([source.resolve()])
            self.assertEqual(
                [candidate.full_name for candidate in wizard.review_page.candidates()],
                ["شخص اول", "شخص دوم"],
            )
            self.assertEqual(wizard._preprocess_source, ())

    def test_file_extraction_runs_without_blocking_the_ui_thread(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "input.docx"
            source.write_bytes(b"test")
            wizard = RecipientWizard()
            wizard.files_page._add_paths((source,))
            started = threading.Event()
            release = threading.Event()

            def extract(_paths):
                started.set()
                release.wait(2)
                return (RecipientCandidate(
                    source_path=source,
                    full_name="نام نمونه",
                ),)

            with patch(
                "sana_declaration_uploader.desktop.extract_recipients",
                side_effect=extract,
            ):
                wizard._page_changed(wizard.review_page_id)
                self.assertTrue(started.wait(1))
                self.assertIsNotNone(wizard._background_thread)
                self.assertFalse(
                    wizard.button(QWizard.WizardButton.NextButton).isEnabled()
                )
                release.set()
                self._wait_for_background(wizard)

            self.assertEqual(
                wizard.review_page.candidates()[0].full_name,
                "نام نمونه",
            )

    def test_last_page_keeps_essential_buttons_with_beginner_tooltips(self) -> None:
        page = RecipientWizard().login_page

        self.assertFalse(hasattr(page, "test_login_button"))
        self.assertEqual(page.primary_action_group.title(), "اقدام پیشنهادی")
        self.assertEqual(
            page.api_test_button.text(),
            "ثبت موقت اظهارنامه‌ها و افزودن پیوست‌ها",
        )
        self.assertEqual(page.api_test_button.objectName(), "PrimaryButton")
        self.assertTrue(page.api_test_button.toolTip())
        self.assertFalse(page.api_test_button.isHidden())
        self.assertEqual(
            page.existing_attachment_group.title(),
            "اظهارنامه قبلاً ثبت شده است؟",
        )
        self.assertTrue(page.attach_existing_button.text().startswith("فقط افزودن"))
        self.assertEqual(page.advanced_group.title(), "تنظیمات پیشرفته")
        self.assertNotIn("API", page.api_template_button.text())
        self.assertTrue(page.api_template_button.toolTip())
        self.assertEqual(page.close_browser_button.text(), "بستن نشست ثنا")
        self.assertTrue(page.close_browser_button.toolTip())
        self.assertTrue(page.close_browser_button.isHidden())
        self.assertTrue(page.stop_button.toolTip())
        self.assertTrue(page.stop_button.isHidden())
        self.assertIn("غیرقابل‌بازگشت", page.final_registration_warning.text())

    def test_last_page_places_all_credential_fields_on_one_row(self) -> None:
        wizard = RecipientWizard()
        wizard.setStartId(wizard.login_page_id)
        wizard.show()
        self.app.processEvents()

        page = wizard.login_page
        fields = (
            page.national_code,
            page.password,
            page.legal_identity,
            page.otp,
        )
        field_y_positions = {
            field.mapToGlobal(field.rect().topLeft()).y() for field in fields
        }

        self.assertEqual(len(field_y_positions), 1)
        self.assertTrue(all(field.isVisible() for field in fields))
        wizard.close()

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
        self.assertEqual(
            table.item(
                0, wizard.preprocessing_page.HEADERS.index("نام شخص")
            ).text(),
            "نمونه",
        )
        self.assertEqual(
            table.item(0, wizard.preprocessing_page.HEADERS.index("کد استان")).text(),
            "23",
        )
        for header in ("شغل", "کد شغل"):
            self.assertFalse(
                table.item(0, wizard.preprocessing_page.HEADERS.index(header)).flags()
                & Qt.ItemFlag.ItemIsEditable
            )
        self.assertEqual(
            table.item(
                0, wizard.preprocessing_page.HEADERS.index("وضعیت")
            ).text(),
            "نیازمند تکمیل",
        )

    def test_preprocessing_page_shows_previous_tracking_code_date_and_color(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory)
            source_path = source / "input.docx"
            (source / "results.json").write_text(json.dumps({
                "schema_version": 1,
                "declarations": {
                    "input.docx": {"registrations": [{
                        "tracking_code": "1405111222333444",
                        "registered_at": "2026-10-16T10:20:00+03:30",
                    }]}
                },
            }), encoding="utf-8")
            page = PreprocessingPage()
            page.set_candidates((RecipientCandidate(source_path=source_path),))

            code = page.table.item(0, page.HEADERS.index("کد رهگیری"))
            registered = page.table.item(0, page.HEADERS.index("تاریخ ثبت"))
            self.assertEqual(code.text(), "1405111222333444")
            self.assertEqual(registered.text(), "1405/07/24")
            self.assertEqual(code.background().color().name(), "#fff0c2")

    def test_preprocessing_page_keeps_same_file_recipients_separate(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory)
            source_path = source / "input.docx"
            candidates = (
                RecipientCandidate(
                    source_path=source_path,
                    source_recipient_ordinal=1,
                ),
                RecipientCandidate(
                    source_path=source_path,
                    source_recipient_ordinal=2,
                ),
            )
            for candidate, tracking_code in zip(
                candidates, ("T-FIRST", "T-SECOND"), strict=True
            ):
                save_verified_result(
                    DeclarationDraft(
                        source_dir=source,
                        fields={},
                        source_path=source_path,
                        source_recipient_ordinal=(
                            candidate.source_recipient_ordinal
                        ),
                    ),
                    UploadResult("success", tracking_code),
                )

            page = PreprocessingPage()
            page.set_candidates(candidates)

            code_column = page.HEADERS.index("کد رهگیری")
            self.assertEqual(
                [page.table.item(row, code_column).text() for row in range(2)],
                ["T-FIRST", "T-SECOND"],
            )

    def test_preprocessing_actions_are_rightmost_icon_buttons_with_tooltips(self) -> None:
        page = PreprocessingPage()
        page.set_candidates((RecipientCandidate(source_path=Path("sample.docx")),))

        self.assertEqual(page.HEADERS[:2], ("حذف", "بازکردن"))
        for column, tooltip in (("حذف", "حذف ردیف"), ("بازکردن", "بازکردن فایل")):
            button = page.table.cellWidget(
                0, page.HEADERS.index(column)
            ).findChild(QPushButton)
            self.assertEqual(button.toolTip(), tooltip)
            self.assertEqual(button.accessibleName(), tooltip)
            self.assertEqual(button.text(), "")

    def test_preprocessing_page_refreshes_newly_saved_tracking_result(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory)
            source_path = source / "input.docx"
            candidate = RecipientCandidate(source_path=source_path)
            page = PreprocessingPage()
            page.set_candidates((candidate,))
            save_verified_result(
                DeclarationDraft(
                    source_dir=source,
                    fields={},
                    source_path=source_path,
                ),
                UploadResult(status="success", tracking_code="1405111222333444"),
            )

            page.refresh_registration_results()

            self.assertEqual(
                page.table.item(0, page.HEADERS.index("کد رهگیری")).text(),
                "1405111222333444",
            )
            self.assertNotEqual(
                page.table.item(0, page.HEADERS.index("تاریخ ثبت")).text(),
                "نامشخص",
            )
            self.assertEqual(
                page.table.item(0, page.HEADERS.index("نام شخص"))
                .background().color().name(),
                "#fff0c2",
            )

    def test_review_uses_requested_column_order_and_numbers_every_page(self) -> None:
        wizard = RecipientWizard()

        self.assertEqual(
            [
                page.title().split("—", 1)[0].replace("<b>", "").strip()
                for page in (
                    wizard.files_page,
                    wizard.review_page,
                    wizard.preprocessing_page,
                    wizard.content_page,
                    wizard.login_page,
                )
            ],
            [f"مرحله {number} از ۵" for number in "۱۲۳۴۵"],
        )
        self.assertEqual(wizard.review_page.HEADERS, (
            "حذف", "بازکردن", "تاریخ اظهارنامه مرتبط",
            "تعداد روز گذشته از آن تاریخ", "کد رهگیری", "تاریخ ثبت",
            "نام و نام خانوادگی", "نام پدر", "کد ملی", "شماره اظهارنامه",
            "نام", "نام خانوادگی", "نام فایل مرتبط", "آدرس", "شاهد استخراج",
        ))
        self.assertTrue(wizard.review_page.table.isSortingEnabled())
        self.assertFalse(wizard.review_page.table.wordWrap())
        self.assertEqual(
            wizard.review_page.table.columnWidth(
                wizard.review_page.HEADERS.index("آدرس")
            ),
            520,
        )
        self.assertEqual(
            wizard.review_page.table.columnWidth(
                wizard.review_page.HEADERS.index("شاهد استخراج")
            ),
            660,
        )
        self.assertLess(
            wizard.review_page.table.columnWidth(
                wizard.review_page.HEADERS.index("نام پدر")
            ),
            wizard.review_page.table.columnWidth(
                wizard.review_page.HEADERS.index("نام و نام خانوادگی")
            ),
        )
        for removed in ("شماره ابلاغیه", "شغل", "کد پرسنلی"):
            self.assertNotIn(removed, wizard.review_page.HEADERS)

    def test_review_opens_source_and_displays_date_left_to_right(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "input.docx"
            source.write_bytes(b"test")
            page = RecipientReviewPage()
            page.set_candidates((RecipientCandidate(
                source_path=source,
                full_name="نام نمونه",
                related_declaration_date="1405/01/02",
            ),))

            date_item = page.table.item(
                0, page.HEADERS.index("تاریخ اظهارنامه مرتبط")
            )
            self.assertEqual(date_item.text(), "1405/01/02")
            self.assertTrue(
                date_item.textAlignment() & Qt.AlignmentFlag.AlignLeft
            )
            self.assertIsNone(page.table.cellWidget(
                0, page.HEADERS.index("تاریخ اظهارنامه مرتبط")
            ))
            self.assertIsInstance(
                page.table.item(
                    0, page.HEADERS.index("تعداد روز گذشته از آن تاریخ")
                ).data(Qt.ItemDataRole.DisplayRole),
                int,
            )
            with patch(
                "sana_declaration_uploader.desktop.QDesktopServices.openUrl"
            ) as open_url:
                page.table.cellWidget(
                    0, page.HEADERS.index("بازکردن")
                ).findChild(QPushButton, "TableIconButton").click()

            open_url.assert_called_once()

    def test_review_splits_long_full_name_before_compound_family_name(self) -> None:
        page = RecipientReviewPage()
        page.set_candidates((RecipientCandidate(
            source_path=Path("sample.docx"),
            full_name="مریم شمسیان جهان بخش",
        ),))

        self.assertEqual(page.table.item(0, page.HEADERS.index("نام")).text(), "مریم")
        self.assertEqual(
            page.table.item(0, page.HEADERS.index("نام خانوادگی")).text(),
            "شمسیان جهان بخش",
        )

    def test_review_row_actions_use_compact_accessible_icons(self) -> None:
        page = RecipientReviewPage()
        page.set_candidates((RecipientCandidate(
            source_path=Path("input.docx"),
            full_name="نام نمونه",
        ),))

        for header, label in (
            ("حذف", "حذف ردیف"),
            ("بازکردن", "بازکردن فایل"),
        ):
            button = page.table.cellWidget(
                0, page.HEADERS.index(header)
            ).findChild(QPushButton, "TableIconButton")
            self.assertEqual(button.text(), "")
            self.assertEqual(button.toolTip(), label)
            self.assertEqual(button.accessibleName(), label)
            self.assertFalse(button.icon().isNull())
            self.assertEqual(button.iconSize(), QSize(14, 14))
            self.assertEqual(button.maximumSize(), QSize(24, 24))

    def test_review_normalizes_and_sorts_related_dates(self) -> None:
        page = RecipientReviewPage()
        page.set_candidates((
            RecipientCandidate(
                source_path=Path("later.docx"),
                full_name="بعدی",
                related_declaration_date="10/06/1405",
            ),
            RecipientCandidate(
                source_path=Path("earlier.docx"),
                full_name="قبلی",
                related_declaration_date="۱۴۰۵/۰۱/۰۲",
            ),
        ))

        date_column = page.HEADERS.index("تاریخ اظهارنامه مرتبط")
        page.table.sortItems(date_column, Qt.SortOrder.AscendingOrder)

        self.assertEqual(page.table.item(0, date_column).text(), "1405/01/02")
        self.assertEqual(page.table.item(1, date_column).text(), "1405/06/10")
        self.assertEqual(
            _days_since_related_date("1405/01/02", today=date(2026, 10, 13)),
            205,
        )

    def test_preprocessing_edit_resolves_codes_and_row_can_be_removed(self) -> None:
        wizard = RecipientWizard()
        page = wizard.preprocessing_page
        page.set_candidates((RecipientCandidate(
            source_path=Path(__file__),
            full_name="نام نمونه",
            job_title="آزاد",
            job_code="00002",
            address=(
                "استان خوزستان - شهرستان امیدیه - شهر امیدیه - "
                "خیابان اصلی آزادی - کد پستی 1234567890"
            ),
        ),))

        with patch(
            "sana_declaration_uploader.desktop.resolve_captured_jurisdiction_fields",
            return_value={
                "notify_owner_organization_title": "حوزه قضایی شهرستان امیدیه",
                "notify_owner_organization_code": "06160",
                "notify_owner_organization_id": "court-id",
                "get_location_for_address_id": "address-id",
            },
        ):
            for header, value in (
                ("استان", "خوزستان"),
                ("شهرستان", "امیدیه"),
                ("شهر", "امیدیه"),
            ):
                page.table.item(0, page.HEADERS.index(header)).setText(value)

        self.assertEqual(
            page.table.item(0, page.HEADERS.index("کد استان")).text(), "06"
        )
        self.assertEqual(
            page.table.item(0, page.HEADERS.index("کد شهرستان")).text(), "0616"
        )
        self.assertEqual(
            page.table.item(0, page.HEADERS.index("کد شهر")).text(),
            "0616022111",
        )
        self.assertEqual(
            page.HEADERS.index("حوزه قضایی"),
            page.HEADERS.index("کد شهر") + 1,
        )
        self.assertEqual(
            page.table.item(0, page.HEADERS.index("حوزه قضایی")).text(),
            "حوزه قضایی شهرستان امیدیه",
        )
        self.assertEqual(
            page.table.item(0, page.HEADERS.index("کد حوزه")).text(),
            "06160",
        )
        self.assertEqual(
            page.table.item(0, page.HEADERS.index("خیابان اصلی")).text(),
            "آزادی",
        )
        self.assertEqual(
            page.table.item(0, page.HEADERS.index("کد پستی")).text(),
            "1234567890",
        )
        candidate = page.candidates()[0]
        self.assertEqual(candidate.province, "استان خوزستان")
        self.assertEqual(candidate.county, "شهرستان امیدیه")
        self.assertEqual(candidate.court_unit_name, "حوزه قضایی شهرستان امیدیه")
        self.assertEqual(candidate.court_unit_code, "06160")
        self.assertEqual(candidate.main_street, "آزادی")
        self.assertEqual(candidate.postal_code, "1234567890")

        address_item = page.table.item(0, page.HEADERS.index("آدرس"))
        address_item.setText("نشانی اصلاح‌شده")
        self.assertEqual(
            page.candidates()[0].address,
            "خوزستان - امیدیه - امیدیه - اصلاح‌شده",
        )

        with patch(
            "sana_declaration_uploader.desktop.QDesktopServices.openUrl"
        ) as open_url:
            page.table.cellWidget(
                0, page.HEADERS.index("بازکردن")
            ).findChild(QPushButton).click()
        open_url.assert_called_once()

        page.table.item(
            0, page.HEADERS.index("انتخاب")
        ).setCheckState(Qt.CheckState.Checked)
        page._remove_selected()
        self.assertEqual(page.table.rowCount(), 0)

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

    def test_preprocessing_delete_action_is_compact_and_right_aligned(self) -> None:
        page = PreprocessingPage()
        page.resize(900, 500)
        page.show()
        self.app.processEvents()

        button = page.findChild(QPushButton, "TableIconButton")
        self.assertIsNotNone(button)
        assert button is not None
        self.assertEqual(button.size(), QSize(24, 24))
        self.assertEqual(button.text(), "")
        self.assertEqual(button.toolTip(), "حذف ردیف")
        self.assertGreater(button.mapTo(page, button.rect().topLeft()).x(), page.width() // 2)
        page.close()

    def test_common_content_is_seeded_for_each_person_when_scope_changes(self) -> None:
        with patch(
            "sana_declaration_uploader.desktop._load_attachment_settings",
            return_value={
                "attachment_kind": "سایر دلایل و مستندات",
                "attachment_title": "",
                "attachment_number": "شماره مدرک",
                "attachment_date": "۱۴۰۵/۰۷/۲۰",
                "shared_description": "true",
                "shared_attachments": "true",
            },
        ):
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

    def test_content_page_uses_compact_right_side_fields_and_tall_description(self) -> None:
        page = DeclarationContentPage()
        page.resize(1200, 900)
        page.show()
        self.app.processEvents()

        recipient_panel = page.recipient_combo.parentWidget()
        description_panel = page.description.parentWidget()
        attachment_panel = page.attachment_kind.parentWidget()

        self.assertIs(page.subject.parentWidget(), recipient_panel)
        self.assertGreater(recipient_panel.x(), description_panel.x())
        self.assertGreaterEqual(
            description_panel.width(), recipient_panel.width() * 2
        )
        self.assertEqual(page.description.minimumHeight(), 480)
        self.assertIs(page.attachment_number.parentWidget(), attachment_panel)
        self.assertIs(page.attachment_date.parentWidget(), attachment_panel)
        self.assertIs(page.attachment_title.parentWidget(), attachment_panel)
        self.assertLessEqual(attachment_panel.width(), page.width() // 3)
        self.assertGreater(attachment_panel.x(), page.width() // 2)

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

    def test_content_page_uses_combo_with_representation_document_default(self) -> None:
        with patch(
            "sana_declaration_uploader.desktop._load_attachment_settings",
            return_value={
                "attachment_kind": "تصویر مدرک نمایندگی",
                "attachment_title": "",
                "attachment_number": "شماره مدرک",
                "attachment_date": "۱۴۰۵/۰۷/۲۰",
                "shared_description": "true",
                "shared_attachments": "true",
            },
        ):
            page = DeclarationContentPage()

        self.assertFalse(page.attachment_kind.isEditable())
        self.assertEqual(
            page.attachment_kind.currentText(), "تصویر مدرک نمایندگی"
        )
        self.assertGreater(page.attachment_kind.count(), 1)

    def test_saved_attachment_files_are_restored_without_reselection(self) -> None:
        with TemporaryDirectory() as directory:
            attachment = Path(directory) / "representation.jpg"
            attachment.write_bytes(b"image")
            with patch(
                "sana_declaration_uploader.desktop._load_saved_attachment_paths",
                return_value=(attachment.resolve(),),
            ):
                page = DeclarationContentPage()
                page.set_candidates((RecipientCandidate(
                    source_path=Path("input.docx"), full_name="شخص اول"
                ),))

            self.assertEqual(page._attachment_paths(), [attachment.resolve()])
            self.assertEqual(page.payloads()[0]["attachments"], (attachment.resolve(),))

    def test_choosing_attachment_immediately_persists_its_path(self) -> None:
        with TemporaryDirectory() as directory:
            attachment = Path(directory) / "representation.jpg"
            attachment.write_bytes(b"image")
            with (
                patch(
                    "sana_declaration_uploader.desktop._load_saved_attachment_paths",
                    return_value=(),
                ),
                patch(
                    "sana_declaration_uploader.desktop.QFileDialog.getOpenFileNames",
                    return_value=([str(attachment)], ""),
                ),
                patch(
                    "sana_declaration_uploader.desktop._save_attachment_settings"
                ) as save,
            ):
                page = DeclarationContentPage()
                page.set_candidates((RecipientCandidate(
                    source_path=Path("input.docx"), full_name="شخص اول"
                ),))
                page._choose_attachments()

        self.assertEqual(
            save.call_args.kwargs["attachment_paths"], (attachment.resolve(),)
        )

    def test_descriptions_can_differ_while_attachments_stay_shared(self) -> None:
        with (
            TemporaryDirectory() as directory,
            patch(
                "sana_declaration_uploader.desktop._load_saved_attachment_paths",
                return_value=(),
            ),
        ):
            attachment = Path(directory) / "shared.pdf"
            attachment.write_bytes(b"%PDF-test")
            page = DeclarationContentPage()
            page.set_candidates((
                RecipientCandidate(
                    source_path=Path("one.docx"),
                    full_name="شخص اول",
                    declaration_text="شرح اول",
                ),
                RecipientCandidate(
                    source_path=Path("two.docx"),
                    full_name="شخص دوم",
                    declaration_text="شرح دوم",
                ),
            ))

            self.assertFalse(page.shared_checkbox.isChecked())
            self.assertTrue(page.shared_attachments_checkbox.isChecked())
            page._add_attachment(attachment)
            page.recipient_combo.setCurrentIndex(1)

            self.assertEqual(page.description.toPlainText(), "شرح دوم")
            self.assertIn("شخص دوم", page.active_recipient_label.text())
            self.assertEqual(page.attachment_list.count(), 1)
            page.description.setPlainText("شرح دوم اصلاح‌شده")
            page.recipient_combo.setCurrentIndex(0)

            self.assertEqual(page.description.toPlainText(), "شرح اول")
            self.assertEqual(page.attachment_list.count(), 1)
            payloads = page.payloads()
            self.assertEqual(payloads[1]["description"], "شرح دوم اصلاح‌شده")
            self.assertEqual(
                payloads[0]["attachments"], payloads[1]["attachments"]
            )

    def test_api_test_draft_contains_only_the_active_recipient(self) -> None:
        page = DeclarationContentPage()
        candidates = (
            RecipientCandidate(
                source_path=Path("one.docx"), full_name="شخص اول"
            ),
            RecipientCandidate(
                source_path=Path("two.docx"), full_name="شخص دوم"
            ),
        )
        page.set_candidates(candidates)
        page.recipient_combo.setCurrentIndex(1)
        page.subject.setText("موضوع نفر دوم")
        page.description.setPlainText("شرح نفر دوم")
        draft = DeclarationDraft(source_dir=Path("source"), fields={})

        with (
            patch(
                "sana_declaration_uploader.desktop.resolve_lookup_fields",
                return_value={
                    "province": "استان",
                    "province_id": "province-id",
                    "province_code": "province-code",
                    "county": "شهرستان",
                    "county_id": "county-id",
                    "county_code": "county-code",
                    "city": "شهر",
                    "city_id": "city-id",
                    "city_code": "city-code",
                },
            ) as resolve_locations,
            patch(
                "sana_declaration_uploader.desktop._build_desktop_drafts",
                return_value=(draft,),
            ) as build_drafts,
        ):
            result = page.active_draft(Path("lookups"))

        self.assertIsNot(result, draft)
        build_drafts.assert_called_once()
        args, kwargs = build_drafts.call_args
        self.assertEqual(args[0], (candidates[1],))
        self.assertEqual(args[1][0]["subject"], "موضوع نفر دوم")
        self.assertEqual(args[1][0]["description"], "شرح نفر دوم")
        self.assertFalse(kwargs["shared"])
        resolve_locations.assert_called_once_with(
            {
                "job_title": "آزاد",
                "province": "",
                "county": "",
                "city": "",
            },
            Path("lookups"),
        )
        self.assertEqual(result.fields["city_code"], "city-code")

    def test_api_draft_rejects_location_that_was_defaulted_to_tehran(self) -> None:
        candidate = RecipientCandidate(
            source_path=Path("one.docx"),
            province="استان تهران",
            county="شهرستان تهران",
            city="تهران",
            issues=("location_defaulted",),
        )
        page = DeclarationContentPage()
        page.set_candidates((candidate,))

        with self.assertRaisesRegex(
            LookupResolutionError, "recipient_location_not_found"
        ):
            page.active_draft(Path("lookups"))

    def test_api_test_button_requires_complete_credentials(self) -> None:
        wizard = RecipientWizard()
        callback = Mock()
        wizard.login_page.api_test_callback = callback
        for field in (
            wizard.login_page.national_code,
            wizard.login_page.password,
            wizard.login_page.legal_identity,
            wizard.login_page.otp,
        ):
            field.clear()

        with patch.object(QMessageBox, "warning") as warning:
            wizard.login_page.api_test_button.click()

        callback.assert_not_called()
        warning.assert_called_once()

    def test_api_test_button_calls_single_item_callback(self) -> None:
        wizard = RecipientWizard()
        callback = Mock()
        wizard.login_page.api_test_callback = callback
        page = wizard.login_page
        page.national_code.setText("national")
        page.password.setText("password")
        page.legal_identity.setText("identity")
        page.otp.setText("otp")

        with patch.object(page, "_save_credentials"):
            page.api_test_button.click()

        callback.assert_called_once_with(
            LoginCredentials("national", "password", "identity", "otp")
        )

    def test_api_test_button_allows_active_session_without_otp(self) -> None:
        page = RecipientWizard().login_page
        callback = Mock()
        page.api_test_callback = callback
        page.api_session_active_callback = lambda: True
        page.national_code.setText("national")
        page.password.setText("password")
        page.legal_identity.setText("identity")
        page.otp.clear()

        with patch.object(page, "_save_credentials"):
            page._test_api_registration()

        callback.assert_called_once_with(
            LoginCredentials("national", "password", "identity", "")
        )

    def test_attach_existing_button_calls_attachment_only_callback(self) -> None:
        page = RecipientWizard().login_page
        callback = Mock()
        page.attach_existing_callback = callback
        page.api_session_active_callback = lambda: True
        page.national_code.setText("national")
        page.password.setText("password")
        page.legal_identity.setText("identity")
        page.otp.clear()

        with patch.object(page, "_save_credentials"):
            page.attach_existing_button.click()

        callback.assert_called_once_with(
            LoginCredentials("national", "password", "identity", "")
        )
        self.assertIn("دوباره ثبت", page.attach_existing_button.toolTip())

    def test_ambiguous_legacy_result_asks_for_exact_tracking_code(self) -> None:
        wizard = RecipientWizard()
        candidate = RecipientCandidate(source_path=Path("source.docx"))
        draft = DeclarationDraft(
            source_dir=Path("."),
            fields={},
            attachments=(Attachment(Path("attachment.pdf"), "نوع", "عنوان"),),
            source_path=Path("source.docx"),
            source_recipient_ordinal=2,
        )
        wizard._api_request_queue = Queue()
        wizard._api_browser_close_event = Event()

        with (
            patch.object(
                wizard.preprocessing_page, "candidates", return_value=(candidate,)
            ),
            patch.object(wizard.content_page, "payloads", return_value={}),
            patch(
                "sana_declaration_uploader.desktop._build_desktop_drafts",
                return_value=(draft,),
            ),
            patch(
                "sana_declaration_uploader.desktop.read_saved_result",
                side_effect=ResultPersistenceError(
                    "legacy_results_ambiguous_for_multiple_recipients"
                ),
            ),
            patch.object(QInputDialog, "getText", return_value=("TRACK-2", True)),
            patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
            ),
        ):
            wizard._start_attach_existing(
                LoginCredentials("national", "password", "identity", "otp")
            )

        queued = wizard._api_request_queue.get_nowait()
        self.assertEqual(queued["kind"], "attach_existing")
        self.assertEqual(queued["requests"], ((draft, "TRACK-2"),))

    def test_api_registration_skips_unsupported_person_type(self) -> None:
        wizard = RecipientWizard()
        candidates = (
            RecipientCandidate(source_path=Path("natural.docx")),
            RecipientCandidate(source_path=Path("legal.docx")),
        )
        natural = DeclarationDraft(Path("."), {"person_type": "حقیقی"})
        legal = DeclarationDraft(Path("."), {"person_type": "حقوقی"})
        wizard._api_request_queue = Queue()
        wizard._api_browser_close_event = Event()

        with (
            patch(
                "sana_declaration_uploader.desktop.load_setpetitiondata_template",
                return_value={"template": True},
            ),
            patch.object(
                wizard.preprocessing_page, "candidates", return_value=candidates
            ),
            patch.object(wizard.content_page, "payloads", return_value={}),
            patch(
                "sana_declaration_uploader.desktop._build_desktop_drafts",
                return_value=(natural, legal),
            ),
            patch.object(QMessageBox, "warning") as warning,
            patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
            ),
        ):
            wizard._start_api_test(
                LoginCredentials("national", "password", "identity", "otp")
            )

        queued_drafts, *_rest = wizard._api_request_queue.get_nowait()
        self.assertEqual(queued_drafts, (natural,))
        warning.assert_not_called()

    def test_api_registration_silently_skips_saved_tracking_code(self) -> None:
        wizard = RecipientWizard()
        candidates = (
            RecipientCandidate(source_path=Path("registered.docx")),
            RecipientCandidate(source_path=Path("new.docx")),
        )
        registered = DeclarationDraft(
            Path("."), {"person_type": "حقیقی"},
            source_path=Path("registered.docx"),
        )
        new = DeclarationDraft(
            Path("."), {"person_type": "حقیقی"},
            source_path=Path("new.docx"),
        )
        wizard._api_request_queue = Queue()
        wizard._api_browser_close_event = Event()

        with (
            patch(
                "sana_declaration_uploader.desktop.load_setpetitiondata_template",
                return_value={"template": True},
            ),
            patch.object(
                wizard.preprocessing_page, "candidates", return_value=candidates
            ),
            patch.object(wizard.content_page, "payloads", return_value={}),
            patch(
                "sana_declaration_uploader.desktop._build_desktop_drafts",
                return_value=(registered, new),
            ),
            patch(
                "sana_declaration_uploader.desktop.read_saved_result",
                side_effect=[{"tracking_code": "1" * 16}, None],
            ),
            patch.object(QMessageBox, "warning") as warning,
            patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
            ),
        ):
            wizard._start_api_test(
                LoginCredentials("national", "password", "identity", "otp")
            )

        queued_drafts, *_rest = wizard._api_request_queue.get_nowait()
        self.assertEqual(queued_drafts, (new,))
        warning.assert_not_called()

    def test_attachment_only_silently_skips_unsupported_person_type(self) -> None:
        wizard = RecipientWizard()
        candidates = (
            RecipientCandidate(source_path=Path("natural.docx")),
            RecipientCandidate(source_path=Path("legal.docx")),
        )
        attachment = Attachment(Path("attachment.pdf"), "نوع", "عنوان")
        natural = DeclarationDraft(
            Path("."), {"person_type": "حقیقی"},
            attachments=(attachment,), source_path=Path("natural.docx"),
        )
        legal = DeclarationDraft(
            Path("."), {"person_type": "حقوقی"},
            attachments=(attachment,), source_path=Path("legal.docx"),
        )
        wizard._api_request_queue = Queue()
        wizard._api_browser_close_event = Event()

        with (
            patch.object(
                wizard.preprocessing_page, "candidates", return_value=candidates
            ),
            patch.object(wizard.content_page, "payloads", return_value={}),
            patch(
                "sana_declaration_uploader.desktop._build_desktop_drafts",
                return_value=(natural, legal),
            ),
            patch(
                "sana_declaration_uploader.desktop.read_saved_result",
                return_value={"tracking_code": "1" * 16},
            ) as read_result,
            patch.object(QMessageBox, "warning") as warning,
            patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
            ),
        ):
            wizard._start_attach_existing(
                LoginCredentials("national", "password", "identity", "otp")
            )

        queued = wizard._api_request_queue.get_nowait()
        self.assertEqual(queued["requests"], ((natural, "1" * 16),))
        read_result.assert_called_once()
        warning.assert_not_called()

    def test_api_browser_close_button_releases_browser_wait(self) -> None:
        wizard = RecipientWizard()
        close_event = threading.Event()
        wizard._api_browser_close_event = close_event
        wizard._api_browser_ready = True
        wizard.login_page.close_browser_button.setEnabled(True)

        wizard.login_page.close_browser_button.click()

        self.assertTrue(close_event.is_set())
        self.assertIs(wizard._api_browser_close_event, close_event)
        self.assertFalse(wizard.login_page.close_browser_button.isEnabled())

    def test_open_api_session_allows_navigation_for_new_file_extraction(self) -> None:
        wizard = RecipientWizard()
        wizard._api_browser_ready = True

        wizard._set_api_session_controls_enabled()

        self.assertTrue(wizard.login_page.api_test_button.isEnabled())
        self.assertTrue(wizard.login_page.close_browser_button.isEnabled())
        self.assertTrue(wizard.button(QWizard.WizardButton.BackButton).isEnabled())
        self.assertTrue(wizard.button(QWizard.WizardButton.NextButton).isEnabled())
        self.assertFalse(
            wizard.button(QWizard.WizardButton.FinishButton).isEnabled()
        )
        self.assertTrue(wizard.login_page.stop_button.isHidden())

    def test_new_file_extraction_uses_separate_worker_while_api_browser_is_open(self) -> None:
        with TemporaryDirectory() as directory:
            source_path = Path(directory) / "new.txt"
            source_path.write_text("متن", encoding="utf-8")
            wizard = RecipientWizard()
            wizard._api_browser_ready = True
            wizard._background_thread = object()
            wizard.files_page._add_paths((source_path,))

            with (
                patch.object(wizard, "_start_session_extraction") as start_extraction,
                patch.object(wizard, "_start_background") as start_background,
            ):
                wizard._page_changed(wizard.review_page_id)

            start_extraction.assert_called_once()
            start_background.assert_not_called()

    def test_separate_file_extraction_worker_returns_result_on_ui_thread(self) -> None:
        wizard = RecipientWizard()
        results = []
        failures = []

        wizard._start_session_extraction(
            lambda _progress: ("candidate",), results.append,
            lambda exc, _details: failures.append(exc),
        )
        deadline = time.monotonic() + 3.0
        while wizard._file_extraction_thread is not None and time.monotonic() < deadline:
            self.app.processEvents()
            thread = wizard._file_extraction_thread
            if thread is not None:
                thread.wait(10)
        self.app.processEvents()

        self.assertIsNone(wizard._file_extraction_thread)
        self.assertEqual(results, [("candidate",)])
        self.assertEqual(failures, [])

    def test_review_page_shows_saved_tracking_and_single_line_tooltips(self) -> None:
        with TemporaryDirectory() as directory:
            source_path = Path(directory) / "input.docx"
            (Path(directory) / "results.json").write_text(json.dumps({
                "schema_version": 1,
                "declarations": {
                    "input.docx": {"registrations": [{
                        "tracking_code": "1405111222333444",
                        "registered_at": "2026-10-16T10:20:00+03:30",
                    }]}
                },
            }), encoding="utf-8")
            address = "نشانی نمونه\n" + "کوچه نمونه " * 30
            evidence = "شاخص استخراج نمونه " * 30
            page = RecipientReviewPage()
            page.set_candidates((RecipientCandidate(
                source_path=source_path,
                address=address,
                evidence=evidence,
            ),))

            table = page.table
            self.assertEqual(
                table.item(0, page.HEADERS.index("کد رهگیری")).text(),
                "1405111222333444",
            )
            self.assertEqual(
                table.item(0, page.HEADERS.index("تاریخ ثبت")).text(), "1405/07/24"
            )
            self.assertEqual(
                table.item(0, page.HEADERS.index("آدرس")).toolTip(), address
            )
            self.assertEqual(
                table.item(0, page.HEADERS.index("شاهد استخراج")).toolTip(), evidence
            )
            for header in ("آدرس", "شاهد استخراج"):
                self.assertNotIn(
                    "\n", table.item(0, page.HEADERS.index(header)).text()
                )
            self.assertEqual(
                table.item(0, page.HEADERS.index("نام و نام خانوادگی"))
                .background().color().name(),
                "#fff0c2",
            )

    def test_file_selection_page_shows_saved_tracking_result(self) -> None:
        with TemporaryDirectory() as directory:
            source_path = Path(directory) / "input.txt"
            source_path.write_text("متن", encoding="utf-8")
            (Path(directory) / "results.json").write_text(json.dumps({
                "schema_version": 1,
                "declarations": {
                    "input.txt": {"registrations": [{
                        "tracking_code": "1405111222333444",
                        "registered_at": "2026-10-16T10:20:00+03:30",
                    }]}
                },
            }), encoding="utf-8")
            wizard = RecipientWizard()

            wizard.files_page._add_paths((source_path,))

            item = wizard.files_page.file_list.item(0)
            self.assertIn("1405111222333444", item.text())
            self.assertIn("1405/07/24", item.text())
            self.assertEqual(item.background().color().name(), "#fff0c2")

    def test_file_list_selected_row_uses_readable_light_colors(self) -> None:
        wizard = RecipientWizard()
        stylesheet = wizard.styleSheet()
        palette = wizard.files_page.file_list.palette()

        self.assertIn("QListWidget::item:selected", stylesheet)
        self.assertIn("background-color: #B9D7EE", stylesheet)
        self.assertIn("color: #102F49", stylesheet)
        self.assertEqual(
            palette.color(QPalette.ColorRole.Highlight).name(), "#b9d7ee"
        )
        self.assertEqual(
            palette.color(QPalette.ColorRole.HighlightedText).name(), "#102f49"
        )

    def test_declaration_fields_survive_back_navigation_and_reentry(self) -> None:
        with (
            TemporaryDirectory() as directory,
            patch("sana_declaration_uploader.desktop._save_declaration_settings"),
            patch("sana_declaration_uploader.desktop._save_attachment_settings"),
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
            page.attachment_kind.setCurrentText("سایر ضمائم")
            page.attachment_title.setText("عنوان نمونه")
            page.attachment_number.setText("۱۲۳")
            page.attachment_date.setText("۱۴۰۵/۰۷/۲۰")
            page._add_attachment(attachment)

            page.cleanupPage()
            page.set_candidates(candidates)

            self.assertEqual(page.subject.text(), "موضوع اظهارنامه")
            self.assertEqual(page.description.toPlainText(), "شرح اظهارنامه")
            self.assertEqual(page.attachment_kind.currentText(), "سایر ضمائم")
            self.assertEqual(page.attachment_title.text(), "عنوان نمونه")
            self.assertEqual(page.attachment_number.text(), "۱۲۳")
            self.assertEqual(page.attachment_date.text(), "۱۴۰۵/۰۷/۲۰")
            self.assertEqual(page.attachment_list.count(), 1)
            self.assertEqual(page._attachment_paths(), [attachment.resolve()])
            self.assertEqual(page.payloads()[0]["attachment_title"], "عنوان نمونه")

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
            with patch(
                "sana_declaration_uploader.desktop._load_login_settings",
                return_value=raw,
            ):
                self.assertEqual(RecipientWizard().login_page.otp.text(), "otp")

            _save_login_settings("", "", "", "", remember=False, path=path)
            self.assertFalse(path.exists())

    def test_editing_otp_preserves_it_in_login_settings(self) -> None:
        page = RecipientWizard().login_page
        page.national_code.setText("national")
        page.password.setText("password")
        page.legal_identity.setText("legal")
        page.otp.setText("new-otp")

        with patch("sana_declaration_uploader.desktop._save_login_settings") as save:
            page.otp.editingFinished.emit()

        save.assert_called_once_with(
            "national", "password", "legal", "new-otp", remember=True
        )

    def test_attachment_defaults_are_persisted_for_the_next_run(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "attachment-defaults.json"
            attachment = Path(directory) / "representation.jpg"
            attachment.write_bytes(b"image")

            _save_attachment_settings(
                "نوع دلخواه", "عنوان دلخواه", "شماره دلخواه",
                "۱۴۰۵/۰۷/۲۱", shared_description=False,
                shared_attachments=False,
                attachment_paths=(attachment,),
                path=path,
            )

            self.assertEqual(_load_attachment_settings(path), {
                "attachment_kind": "نوع دلخواه",
                "attachment_title": "عنوان دلخواه",
                "attachment_number": "شماره دلخواه",
                "attachment_date": "۱۴۰۵/۰۷/۲۱",
                "shared_description": "false",
                "shared_attachments": "false",
            })
            self.assertEqual(
                _load_saved_attachment_paths(path), (attachment.resolve(),)
            )

            defaults = _load_attachment_settings(path)
            with patch(
                "sana_declaration_uploader.desktop._load_attachment_settings",
                return_value=defaults,
            ):
                page = DeclarationContentPage()
            self.assertFalse(page.shared_checkbox.isChecked())
            self.assertFalse(page.shared_attachments_checkbox.isChecked())

    def test_legacy_attachment_kind_migrates_to_representation_document(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "attachment-defaults.json"
            path.write_text(
                json.dumps({
                    "attachment_kind": "سایر دلایل و مستندات",
                    "attachment_title": "عنوان ذخیره‌شده",
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            settings = _load_attachment_settings(path)

        self.assertEqual(
            settings["attachment_kind"], "تصویر مدرک نمایندگی"
        )
        self.assertEqual(settings["attachment_title"], "عنوان ذخیره‌شده")

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

    def test_default_settings_fall_back_for_invalid_json_payloads(self) -> None:
        cases = (
            (
                "attachment-defaults.json",
                _load_attachment_settings,
                {
                    "attachment_kind": "تصویر مدرک نمایندگی",
                    "attachment_title": "",
                    "attachment_number": "شماره مدرک",
                    "attachment_date": "۱۴۰۵/۰۷/۲۰",
                    "shared_description": "",
                    "shared_attachments": "true",
                },
            ),
            (
                "declaration-defaults.json",
                _load_declaration_settings,
                {
                    "subject": "سایر موضوعات",
                    "description": "شرح موضوع",
                },
            ),
        )
        with TemporaryDirectory() as directory:
            for filename, loader, defaults in cases:
                with self.subTest(filename=filename, payload="invalid-json"):
                    path = Path(directory) / filename
                    path.write_text("{", encoding="utf-8")
                    self.assertEqual(loader(path), defaults)
                with self.subTest(filename=filename, payload="list"):
                    path.write_text("[]", encoding="utf-8")
                    self.assertEqual(loader(path), defaults)

    def test_default_settings_strip_values_and_fill_missing_keys(self) -> None:
        with TemporaryDirectory() as directory:
            attachment_path = Path(directory) / "attachment-defaults.json"
            attachment_path.write_text(
                json.dumps({"attachment_kind": "  نوع دلخواه  "}),
                encoding="utf-8",
            )
            self.assertEqual(_load_attachment_settings(attachment_path), {
                "attachment_kind": "نوع دلخواه",
                "attachment_title": "",
                "attachment_number": "شماره مدرک",
                "attachment_date": "۱۴۰۵/۰۷/۲۰",
                "shared_description": "",
                "shared_attachments": "true",
            })

            declaration_path = Path(directory) / "declaration-defaults.json"
            declaration_path.write_text(
                json.dumps({"subject": "  موضوع دلخواه  "}),
                encoding="utf-8",
            )
            self.assertEqual(_load_declaration_settings(declaration_path), {
                "subject": "موضوع دلخواه",
                "description": "شرح موضوع",
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
            "attachment_title": "عنوان ذخیره‌شده",
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
            patch(
                "sana_declaration_uploader.desktop._load_saved_attachment_paths",
                return_value=(),
            ),
        ):
            page = DeclarationContentPage()
            self.assertEqual(
                page.attachment_kind.currentText(), defaults["attachment_kind"]
            )
            self.assertEqual(page.attachment_title.text(), defaults["attachment_title"])
            self.assertEqual(
                page.attachment_number.text(), defaults["attachment_number"]
            )
            self.assertEqual(page.attachment_date.text(), defaults["attachment_date"])

            page.attachment_kind.setCurrentText("سایر ضمائم")
            page.attachment_title.setText("عنوان جدید")
            page.attachment_number.setText("۴۲")
            page.attachment_date.setText("۱۴۰۵/۰۷/۲۲")
            page.attachment_title.editingFinished.emit()

            save.assert_called_once_with(
                "سایر ضمائم", "عنوان جدید", "۴۲", "۱۴۰۵/۰۷/۲۲",
                shared_description=True,
                shared_attachments=True,
                attachment_paths=(),
            )

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
        self.assertFalse(hasattr(page, "test_login_button"))
        self.assertIn("ثبت موقت اظهارنامه‌ها و افزودن پیوست‌ها", [
            button.text() for button in page.findChildren(QPushButton)
        ])

    def test_registration_saves_successes_and_reports_failed_rows(self) -> None:
        wizard = RecipientWizard()
        candidates = (
            RecipientCandidate(source_path=Path("one.docx"), full_name="شخص اول"),
            RecipientCandidate(source_path=Path("two.docx"), full_name="شخص دوم"),
        )
        drafts = (
            DeclarationDraft(source_dir=Path("one"), fields={
                "recipient_first_name": "شخص", "recipient_last_name": "اول",
            }, source_path=Path("one.docx")),
            DeclarationDraft(source_dir=Path("two"), fields={
                "recipient_first_name": "شخص", "recipient_last_name": "دوم",
            }, source_path=Path("two.docx")),
        )
        results = (
            UploadResult(status="success", tracking_code="111111111111"),
            UploadResult(status="failed", message="recipient_person_type_not_supported"),
        )

        def run_batch(*_args, progress=None, **_kwargs):
            progress("[ردیف 1 از 2] اقدام جاری: ثبت اطلاعات مخاطب")
            return results

        with (
            patch.object(wizard.preprocessing_page, "candidates", return_value=candidates),
            patch.object(wizard.content_page, "payloads", return_value={}),
            patch("sana_declaration_uploader.desktop._build_desktop_drafts", return_value=drafts),
            patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
            ),
            patch(
                "sana_declaration_uploader.desktop.run_live_upload_batch",
                side_effect=run_batch,
            ),
            patch("sana_declaration_uploader.desktop.save_verified_result") as save,
            patch.object(QMessageBox, "warning") as warning,
        ):
            completed = wizard._register_declarations(
                LoginCredentials("national", "password", "identity", "otp")
            )
            self._wait_for_background(wizard)

        self.assertFalse(completed)
        save.assert_called_once_with(
            drafts[0], results[0], filename="results.json"
        )
        self.assertIn("ناموفق: 1", warning.call_args.args[2])
        self.assertIn("recipient_person_type_not_supported", warning.call_args.args[2])
        self.assertIn("ردیف 1: ثبت موفق", wizard.login_page.python_output.toPlainText())
        self.assertIn(
            "اقدام جاری: ثبت اطلاعات مخاطب",
            wizard.login_page.python_output.toPlainText(),
        )
        self.assertIn(
            "recipient_person_type_not_supported",
            wizard.login_page.python_output.toPlainText(),
        )

    def test_registration_warns_about_prior_result_then_continues(self) -> None:
        wizard = RecipientWizard()
        with TemporaryDirectory() as directory:
            source_dir = Path(directory)
            source_path = source_dir / "one.docx"
            candidate = RecipientCandidate(source_path=source_path, full_name="نمونه")
            draft = DeclarationDraft(
                source_dir=source_dir, fields={}, source_path=source_path
            )
            from sana_declaration_uploader.results import save_verified_result

            save_verified_result(
                draft, UploadResult(status="success", tracking_code="T-OLD")
            )
            wizard.preprocessing_page.set_candidates((candidate,))
            with (
                patch.object(
                    wizard.preprocessing_page, "candidates", return_value=(candidate,)
                ),
                patch.object(wizard.content_page, "payloads", return_value={}),
                patch(
                    "sana_declaration_uploader.desktop._build_desktop_drafts",
                    return_value=(draft,),
                ),
                patch.object(QMessageBox, "warning") as warning,
                patch.object(
                    QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
                ) as question,
                patch.object(QMessageBox, "information"),
                patch(
                    "sana_declaration_uploader.desktop.run_live_upload_batch",
                    return_value=(UploadResult(
                        status="success", tracking_code="T-NEW"
                    ),),
                ) as run_batch,
            ):
                completed = wizard._register_declarations(
                    LoginCredentials("national", "password", "identity", "otp")
                )
                self._wait_for_background(wizard)
            manifest = json.loads(
                (source_dir / "results.json").read_text(encoding="utf-8")
            )

        self.assertFalse(completed)
        warning.assert_called_once()
        warning_text = warning.call_args.args[2]
        self.assertIn("T-OLD", warning_text)
        self.assertIn("تاریخ ثبت", warning_text)
        question.assert_called_once()
        run_batch.assert_called_once()
        self.assertEqual(
            wizard.preprocessing_page.table.item(
                0, wizard.preprocessing_page.HEADERS.index("کد رهگیری")
            ).text(),
            "T-NEW",
        )
        self.assertEqual(
            [
                item["tracking_code"]
                for item in manifest["declarations"]
                ["one.docx::recipient-001"]["registrations"]
            ],
            ["T-OLD", "T-NEW"],
        )

    def test_registration_writes_chained_traceback_to_python_output(self) -> None:
        wizard = RecipientWizard()
        candidate = RecipientCandidate(source_path=Path("one.docx"), full_name="نمونه")
        draft = DeclarationDraft(source_dir=Path("one"), fields={})

        def fail_upload(*_args, **_kwargs):
            try:
                raise TimeoutError("browser timed out")
            except TimeoutError as exc:
                raise LiveBrowserError("site_response_ambiguous") from exc

        with (
            patch.object(
                wizard.preprocessing_page, "candidates", return_value=(candidate,)
            ),
            patch.object(wizard.content_page, "payloads", return_value={}),
            patch(
                "sana_declaration_uploader.desktop._build_desktop_drafts",
                return_value=(draft,),
            ),
            patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
            ),
            patch(
                "sana_declaration_uploader.desktop.run_live_upload_batch",
                side_effect=fail_upload,
            ),
            patch.object(QMessageBox, "critical"),
        ):
            completed = wizard._register_declarations(
                LoginCredentials("national", "password", "identity", "otp")
            )
            self._wait_for_background(wizard)

        output = wizard.login_page.python_output.toPlainText()
        self.assertFalse(completed)
        self.assertIn("TimeoutError: browser timed out", output)
        self.assertIn("LiveBrowserError: site_response_ambiguous", output)
        self.assertNotIn("password", output)
        self.assertNotIn("otp", output)

    def test_registration_prompts_are_marshaled_to_the_ui_thread(self) -> None:
        wizard = RecipientWizard()
        candidate = RecipientCandidate(
            source_path=Path("one.docx"), full_name="نمونه"
        )
        draft = DeclarationDraft(source_dir=Path("one"), fields={
            "recipient_first_name": "نام",
            "recipient_last_name": "نمونه",
        })
        ui_thread = self.app.thread()
        prompt_threads = []

        def question(*_args, **_kwargs):
            prompt_threads.append(self.app.thread().currentThread())
            return QMessageBox.StandardButton.Yes

        def get_text(*_args, **_kwargs):
            prompt_threads.append(self.app.thread().currentThread())
            return "new-otp", True

        def run_batch(
            _drafts,
            credentials_provider,
            final_confirmation,
            **_kwargs,
        ):
            self.assertEqual(credentials_provider().otp, "otp")
            self.assertEqual(credentials_provider().otp, "new-otp")
            self.assertTrue(final_confirmation(draft))
            return (UploadResult(
                status="success", tracking_code="111111111111"
            ),)

        with (
            patch.object(
                wizard.preprocessing_page, "candidates", return_value=(candidate,)
            ),
            patch.object(wizard.content_page, "payloads", return_value={}),
            patch(
                "sana_declaration_uploader.desktop._build_desktop_drafts",
                return_value=(draft,),
            ),
            patch.object(QMessageBox, "question", side_effect=question),
            patch.object(QInputDialog, "getText", side_effect=get_text),
            patch.object(QMessageBox, "information"),
            patch(
                "sana_declaration_uploader.desktop.run_live_upload_batch",
                side_effect=run_batch,
            ),
            patch("sana_declaration_uploader.desktop.save_verified_result"),
        ):
            self.assertFalse(wizard._register_declarations(
                LoginCredentials("national", "password", "identity", "otp")
            ))
            self._wait_for_background(wizard)

        self.assertEqual(prompt_threads, [ui_thread, ui_thread, ui_thread])

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
