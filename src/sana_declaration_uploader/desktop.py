"""Review-first desktop interface; this phase performs no browser activity."""

from __future__ import annotations

from pathlib import Path
import sys
from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
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
from .preprocessing import preprocess_candidates


_SUPPORTED_SUFFIXES = {".docx", ".pdf", ".txt"}
_ROLE_DISPLAY = {
    "declarant": "اظهارکننده",
    "addressee": "مخاطب",
    "plaintiff": "خواهان",
    "defendant": "خوانده",
    "appellant": "تجدیدنظرخواه",
    "respondent": "تجدیدنظرخوانده",
    "complainant": "شاکی",
    "accused": "مشتکی‌عنه/متهم",
    "petitioner": "درخواست‌کننده",
    "claimant": "محکوم‌له",
}
_PERSON_TYPE_DISPLAY = {
    "natural": "حقیقی",
    "legal": "حقوقی",
    "group": "گروه",
    "unknown": "نامشخص",
}
_ROLE_VALUE = {label: value for value, label in _ROLE_DISPLAY.items()}
_PERSON_TYPE_VALUE = {label: value for value, label in _PERSON_TYPE_DISPLAY.items()}
_ORGANIZATIONAL_STYLE = """
QWizard, QWizardPage {
    background-color: #F4F7FB;
    color: #1F2937;
    font-family: "Segoe UI", "Tahoma";
    font-size: 10pt;
}
QLabel#qt_wizard_title {
    color: #173B5E;
    font-size: 15pt;
    font-weight: 700;
}
QLabel#qt_wizard_subtitle {
    color: #526579;
    font-size: 10pt;
}
QLabel#Guidance {
    background-color: #EAF2F8;
    border: 1px solid #C8D9E8;
    border-radius: 6px;
    color: #234765;
    padding: 10px 12px;
}
QListWidget, QTableWidget {
    background-color: #FFFFFF;
    alternate-background-color: #F7FAFC;
    border: 1px solid #CFDAE5;
    border-radius: 6px;
    gridline-color: #DCE5ED;
    selection-background-color: #D9EAF7;
    selection-color: #173B5E;
}
QHeaderView::section {
    background-color: #E7EEF5;
    color: #173B5E;
    border: 0;
    border-left: 1px solid #CFDAE5;
    border-bottom: 1px solid #BCCBDA;
    padding: 8px;
    font-weight: 600;
}
QPushButton {
    min-height: 32px;
    min-width: 92px;
    background-color: #FFFFFF;
    border: 1px solid #AFC0D0;
    border-radius: 5px;
    color: #173B5E;
    padding: 2px 14px;
}
QPushButton:hover { background-color: #EDF4FA; }
QPushButton:pressed { background-color: #DCEAF5; }
QPushButton:disabled {
    background-color: #EEF1F4;
    border-color: #D7DEE5;
    color: #94A0AC;
}
QPushButton#PrimaryButton {
    background-color: #1769AA;
    border-color: #1769AA;
    color: #FFFFFF;
    font-weight: 600;
}
QPushButton#PrimaryButton:hover { background-color: #125A92; }
"""


class FileSelectionPage(QWizardPage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("<b>انتخاب اسناد و فایل‌های منبع</b>")
        self.setSubTitle(
            "فایل‌های Word، PDF متنی یا متن ساده را اضافه کنید؛ در این مرحله "
            "هیچ اطلاعاتی در سامانه ثنا ثبت نمی‌شود."
        )
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
        guidance = QLabel(
            "راهنما: فایل یا فایل‌های مربوط به یک پرونده را هم‌زمان اضافه کنید. "
            "برنامه مشخصات اشخاص، نقش آنها و شماره‌های مرتبط را استخراج می‌کند. "
            "در مرحله بعد فرصت دارید همه مقادیر را قبل از ادامه بررسی و اصلاح کنید."
        )
        guidance.setObjectName("Guidance")
        guidance.setWordWrap(True)
        layout.addWidget(guidance)
        layout.addWidget(QLabel("فایل‌ها را اینجا رها کنید یا با دکمه زیر انتخاب کنید:"))
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
        "شماره ابلاغیه",
        "شماره اظهارنامه مرتبط",
        "نقش",
        "نوع شخص",
        "نام و نام خانوادگی",
        "شغل",
        "کد ملی",
        "کد پرسنلی",
        "تاریخ تولد",
        "کدپستی",
        "آدرس",
        "شاهد استخراج",
        "وضعیت",
    )

    def __init__(self) -> None:
        super().__init__()
        self.setTitle("<b>بازبینی و تکمیل مشخصات اشخاص</b>")
        self.setSubTitle(
            "خروجی استخراج قطعی تلقی نمی‌شود؛ هر ردیف را با سند اصلی تطبیق دهید."
        )

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
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
        guidance = QLabel(
            "روش کار: ۱) نام و نقش هر شخص را کنترل کنید. ۲) خانه‌های خالی یا اشتباه "
            "را مستقیم در جدول اصلاح کنید. ۳) اگر یک ردیف به پرونده شما مربوط نیست، "
            "آن را انتخاب و حذف کنید. وضعیت «نیازمند اصلاح» یعنی دست‌کم یک مقدار "
            "ضروری ناقص یا مشکوک است. پایان بازبینی به معنی ثبت نهایی در ثنا نیست."
        )
        guidance.setObjectName("Guidance")
        guidance.setWordWrap(True)
        layout.addWidget(guidance)
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
            candidate.notice_number if candidate else "",
            candidate.related_declaration_number if candidate else "",
            _ROLE_DISPLAY.get(candidate.role, candidate.role) if candidate else "",
            _PERSON_TYPE_DISPLAY.get(candidate.person_type, candidate.person_type) if candidate else "",
            candidate.full_name if candidate else "",
            candidate.job_title if candidate else "",
            candidate.national_id if candidate else "",
            candidate.employee_code if candidate else "",
            candidate.birth_date if candidate else "",
            candidate.postal_code if candidate else "",
            candidate.address if candidate else "",
            candidate.evidence if candidate else "",
            self._status(candidate),
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setData(Qt.ItemDataRole.UserRole, candidate)
            if column in (0, len(self.HEADERS) - 2, len(self.HEADERS) - 1):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, column, item)

    def candidates(self) -> tuple[RecipientCandidate, ...]:
        result = []
        for row in range(self.table.rowCount()):
            original = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if not isinstance(original, RecipientCandidate):
                original = RecipientCandidate(source_path=Path("manual"))

            def value(header: str) -> str:
                item = self.table.item(row, self.HEADERS.index(header))
                return item.text().strip() if item else ""

            result.append(replace(
                original,
                notice_number=value("شماره ابلاغیه"),
                related_declaration_number=value("شماره اظهارنامه مرتبط"),
                role=_ROLE_VALUE.get(value("نقش"), value("نقش")),
                person_type=_PERSON_TYPE_VALUE.get(
                    value("نوع شخص"), value("نوع شخص")
                ),
                full_name=value("نام و نام خانوادگی"),
                job_title=value("شغل"),
                national_id=value("کد ملی"),
                employee_code=value("کد پرسنلی"),
                birth_date=value("تاریخ تولد"),
                postal_code=value("کدپستی"),
                address=value("آدرس"),
            ))
        return tuple(result)

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


class PreprocessingPage(QWizardPage):
    HEADERS = (
        "نام شخص",
        "شغل",
        "کد شغل",
        "استان",
        "کد استان",
        "شهرستان",
        "کد شهرستان",
        "شهر",
        "کد شهر",
        "واحد قضایی",
        "کد واحد قضایی",
        "وضعیت",
    )

    def __init__(self) -> None:
        super().__init__()
        self.setTitle("<b>پیش‌پردازش نشانی و کدهای ثنا</b>")
        self.setSubTitle(
            "پیشنهادهای محلی را بررسی و موارد خالی یا اشتباه را پیش از ثبت اصلاح کنید."
        )

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        self.table.horizontalHeader().setStretchLastSection(True)

        guidance = QLabel(
            "روش کار: برنامه عنوان شغل و اجزای نشانی را فقط با کاتالوگ محلی و "
            "تطبیق دقیق به کد تبدیل می‌کند. خانهٔ خالی به معنی نبودن دادهٔ کافی "
            "یا مبهم بودن نتیجه است و باید با سند یا سامانه ثنا تکمیل شود. این "
            "مرحله صرفاً پیش‌نمایش است و هیچ ثبت یا ورود خودکاری انجام نمی‌دهد."
        )
        guidance.setObjectName("Guidance")
        guidance.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(guidance)
        layout.addWidget(self.table)

    def set_candidates(self, candidates: tuple[RecipientCandidate, ...]) -> None:
        self.table.setRowCount(0)
        for candidate in candidates:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (
                candidate.full_name,
                candidate.job_title,
                candidate.job_code,
                candidate.province,
                candidate.province_code,
                candidate.county,
                candidate.county_code,
                candidate.city,
                candidate.city_code,
                candidate.court_unit_name,
                candidate.court_unit_code,
                self._status(candidate),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, candidate)
                if column in (0, len(self.HEADERS) - 1):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()

    @staticmethod
    def _status(candidate: RecipientCandidate) -> str:
        lookup_issues = (
            issue for issue in candidate.issues
            if "lookup" in issue or "unresolved" in issue or "ambiguous" in issue
        )
        return "نیازمند تکمیل" if next(lookup_issues, None) else "آماده بازبینی"


class RecipientWizard(QWizard):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("دستیار ثبت اظهارنامه ثنا")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setStyleSheet(_ORGANIZATIONAL_STYLE)
        self.resize(1180, 680)
        self.files_page = FileSelectionPage()
        self.review_page = RecipientReviewPage()
        self.preprocessing_page = PreprocessingPage()
        self.files_page_id = self.addPage(self.files_page)
        self.review_page_id = self.addPage(self.review_page)
        self.preprocessing_page_id = self.addPage(self.preprocessing_page)
        self._loaded_paths: tuple[Path, ...] = ()
        self._lookup_dir = Path(__file__).resolve().parents[2] / "lookups"
        self.currentIdChanged.connect(self._page_changed)
        self.setButtonLayout([
            QWizard.WizardButton.FinishButton,
            QWizard.WizardButton.NextButton,
            QWizard.WizardButton.BackButton,
            QWizard.WizardButton.CancelButton,
            QWizard.WizardButton.Stretch,
        ])
        self.setButtonText(QWizard.WizardButton.BackButton, "قبلی")
        self.setButtonText(QWizard.WizardButton.NextButton, "بعدی")
        self.setButtonText(QWizard.WizardButton.FinishButton, "پایان بازبینی")
        self.setButtonText(QWizard.WizardButton.CancelButton, "انصراف")
        self.button(QWizard.WizardButton.NextButton).setObjectName("PrimaryButton")
        self.button(QWizard.WizardButton.FinishButton).setObjectName("PrimaryButton")

    def _page_changed(self, page_id: int) -> None:
        if page_id == self.preprocessing_page_id:
            candidates = preprocess_candidates(
                self.review_page.candidates(), self._lookup_dir
            )
            self.preprocessing_page.set_candidates(candidates)
            return
        if page_id != self.review_page_id:
            return
        selected_paths = tuple(self.files_page.selected_paths())
        if selected_paths == self._loaded_paths:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            candidates = extract_recipients(list(selected_paths))
        except DocumentExtractionError as exc:
            self.review_page.set_candidates(())
            QMessageBox.warning(self, "خطا در استخراج", str(exc))
        else:
            self.review_page.set_candidates(candidates)
            self._loaded_paths = selected_paths
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
