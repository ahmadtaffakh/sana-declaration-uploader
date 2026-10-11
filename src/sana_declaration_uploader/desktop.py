"""Review-first desktop interface; this phase performs no browser activity."""

from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from .extraction import DocumentExtractionError, extract_recipients
from .models import RecipientCandidate


_SUPPORTED_SUFFIXES = {".docx", ".pdf", ".txt"}


class FileSelectionPage(QWizardPage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("انتخاب اسناد")
        self.setSubTitle("فایل‌های Word، PDF متنی یا متن ساده را انتخاب کنید.")
        self.setAcceptDrops(True)

        self.file_list = QListWidget()
        add_button = QPushButton("افزودن فایل‌ها")
        remove_button = QPushButton("حذف انتخاب‌شده")
        add_button.clicked.connect(self._choose_files)
        remove_button.clicked.connect(self._remove_selected)

        buttons = QHBoxLayout()
        buttons.addWidget(add_button)
        buttons.addWidget(remove_button)
        buttons.addStretch()

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("فایل‌ها را اینجا رها کنید یا از دکمه زیر انتخاب کنید."))
        layout.addWidget(self.file_list)
        layout.addLayout(buttons)

    def selected_paths(self) -> list[Path]:
        return [
            Path(self.file_list.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.file_list.count())
        ]

    def isComplete(self) -> bool:
        return self.file_list.count() > 0

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        self._add_paths(Path(url.toLocalFile()) for url in event.mimeData().urls())
        event.acceptProposedAction()

    def _choose_files(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(
            self,
            "انتخاب اسناد مخاطبان",
            "",
            "اسناد پشتیبانی‌شده (*.docx *.pdf *.txt)",
        )
        self._add_paths(Path(name) for name in names)

    def _add_paths(self, paths) -> None:
        existing = {str(path) for path in self.selected_paths()}
        for path in paths:
            resolved = path.resolve()
            if not resolved.is_file() or resolved.suffix.lower() not in _SUPPORTED_SUFFIXES:
                continue
            if str(resolved) in existing:
                continue
            item = QListWidgetItem(resolved.name)
            item.setData(Qt.ItemDataRole.UserRole, str(resolved))
            self.file_list.addItem(item)
            existing.add(str(resolved))
        self.completeChanged.emit()

    def _remove_selected(self) -> None:
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))
        self.completeChanged.emit()


class RecipientReviewPage(QWizardPage):
    HEADERS = (
        "فایل منبع",
        "نام و نام خانوادگی",
        "کد ملی",
        "کد پرسنلی",
        "تاریخ تولد",
        "کدپستی",
        "آدرس",
        "وضعیت",
    )

    def __init__(self) -> None:
        super().__init__()
        self.setTitle("بازبینی اشخاص")
        self.setSubTitle("مقادیر استخراج‌شده پیشنهادی هستند؛ آنها را بررسی و اصلاح کنید.")

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        self.table.horizontalHeader().setStretchLastSection(True)

        add_button = QPushButton("افزودن شخص")
        remove_button = QPushButton("حذف شخص")
        add_button.clicked.connect(lambda: self.add_candidate(None))
        remove_button.clicked.connect(self._remove_selected)
        buttons = QHBoxLayout()
        buttons.addWidget(add_button)
        buttons.addWidget(remove_button)
        buttons.addStretch()

        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addLayout(buttons)

    def set_candidates(self, candidates: tuple[RecipientCandidate, ...]) -> None:
        self.table.setRowCount(0)
        for candidate in candidates:
            self.add_candidate(candidate)
        self.table.resizeColumnsToContents()

    def add_candidate(self, candidate: RecipientCandidate | None) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = (
            candidate.source_path.name if candidate else "دستی",
            candidate.full_name if candidate else "",
            candidate.national_id if candidate else "",
            candidate.employee_code if candidate else "",
            candidate.birth_date if candidate else "",
            candidate.postal_code if candidate else "",
            candidate.address if candidate else "",
            self._status(candidate),
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column in (0, 7):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, column, item)

    def _remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    @staticmethod
    def _status(candidate: RecipientCandidate | None) -> str:
        if candidate is None:
            return "ورود دستی"
        if candidate.issues:
            return "نیازمند اصلاح"
        return "نیازمند تأیید"


class RecipientWizard(QWizard):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("دستیار ثبت اظهارنامه ثنا")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.resize(1180, 680)
        self.files_page = FileSelectionPage()
        self.review_page = RecipientReviewPage()
        self.addPage(self.files_page)
        self.addPage(self.review_page)
        self.currentIdChanged.connect(self._page_changed)
        self.button(QWizard.WizardButton.FinishButton).setText("پایان بازبینی")

    def _page_changed(self, page_id: int) -> None:
        if page_id != 1:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            candidates = extract_recipients(self.files_page.selected_paths())
        except DocumentExtractionError as exc:
            self.review_page.set_candidates(())
            QMessageBox.warning(self, "خطا در استخراج", str(exc))
        else:
            self.review_page.set_candidates(candidates)
        finally:
            QApplication.restoreOverrideCursor()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("SANA Declaration Uploader")
    wizard = RecipientWizard()
    wizard.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
