"""Review-first desktop interface with an explicit live-registration boundary."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from html import escape
import json
import os
from pathlib import Path
import sys
import traceback

from PySide6.QtCore import QEventLoop, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWizard,
    QWizardPage,
    QWidget,
)

from .extraction import DocumentExtractionError, extract_recipients
from .flow import _build_desktop_drafts
from .live_browser import (
    LiveBrowserError,
    LoginCredentials,
    run_live_upload_batch,
    run_login_check,
)
from .lookups import LookupResolutionError, resolve_lookup_fields
from .models import DeclarationDraft, RecipientCandidate
from .preprocessing import preprocess_candidates
from .results import ResultPersistenceError, save_verified_result


_SUPPORTED_SUFFIXES = {".docx", ".pdf", ".txt"}
_ATTACHMENT_DEFAULTS = {
    "attachment_kind": "سایر دلایل و مستندات",
    "attachment_number": "شماره مدرک",
    "attachment_date": "۱۴۰۵/۰۷/۲۰",
}
_DECLARATION_DEFAULTS = {
    "subject": "سایر موضوعات",
    "description": "شرح موضوع",
}
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
    alternate-background-color: #E9EFF5;
    border: 1px solid #CFDAE5;
    border-radius: 6px;
    gridline-color: #DCE5ED;
    selection-background-color: #B9D7EE;
    selection-color: #102F49;
}
QLineEdit, QTextEdit, QComboBox {
    background-color: #FFFFFF;
    border: 1px solid #B9C8D6;
    border-radius: 5px;
    min-height: 30px;
    padding: 3px 7px;
}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus {
    border: 2px solid #2B78B5;
}
QGroupBox {
    background-color: #FFFFFF;
    border: 1px solid #CFDAE5;
    border-radius: 7px;
    font-weight: 600;
    margin-top: 10px;
    padding-top: 10px;
}
QGroupBox::title {
    color: #173B5E;
    subcontrol-origin: margin;
    right: 12px;
    padding: 0 5px;
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


def _configure_table(table: QTableWidget) -> None:
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(True)
    table.setWordWrap(True)
    table.setShowGrid(False)
    table.verticalHeader().setDefaultSectionSize(38)
    table.horizontalHeader().setStretchLastSection(True)


def _settings_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "SanaDeclarationUploader"


def _login_settings_path() -> Path:
    return _settings_dir() / "login.json"


def _attachment_settings_path() -> Path:
    return _settings_dir() / "attachment-defaults.json"


def _declaration_settings_path() -> Path:
    return _settings_dir() / "declaration-defaults.json"


def _load_declaration_settings(path: Path | None = None) -> dict[str, str]:
    target = path or _declaration_settings_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return dict(_DECLARATION_DEFAULTS)
    if not isinstance(payload, dict):
        return dict(_DECLARATION_DEFAULTS)
    return {
        key: str(payload.get(key, default)).strip()
        for key, default in _DECLARATION_DEFAULTS.items()
    }


def _save_declaration_settings(
    subject: str,
    description: str,
    *,
    path: Path | None = None,
) -> None:
    target = path or _declaration_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "subject": subject.strip(),
        "description": description.strip(),
    }
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_attachment_settings(path: Path | None = None) -> dict[str, str]:
    target = path or _attachment_settings_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return dict(_ATTACHMENT_DEFAULTS)
    if not isinstance(payload, dict):
        return dict(_ATTACHMENT_DEFAULTS)
    return {
        key: str(payload.get(key, default)).strip()
        for key, default in _ATTACHMENT_DEFAULTS.items()
    }


def _save_attachment_settings(
    kind: str,
    document_number: str,
    document_date: str,
    *,
    path: Path | None = None,
) -> None:
    target = path or _attachment_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "attachment_kind": kind.strip(),
        "attachment_number": document_number.strip(),
        "attachment_date": document_date.strip(),
    }
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_login_settings(path: Path | None = None) -> dict[str, str]:
    target = path or _login_settings_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        key: str(payload.get(key, "")).strip()
        for key in ("national_code", "password", "legal_identity", "otp")
    }


def _save_login_settings(
    national_code: str,
    password: str,
    legal_identity: str,
    otp: str,
    *,
    remember: bool,
    path: Path | None = None,
) -> None:
    target = path or _login_settings_path()
    if not remember:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "national_code": national_code.strip(),
        "password": password,
        "legal_identity": legal_identity.strip(),
        "otp": otp.strip(),
    }
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _split_full_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split()
    if len(parts) < 2:
        return (full_name.strip(), "")
    return (" ".join(parts[:-1]), parts[-1])


def _location_display(value: str, prefix: str) -> str:
    return value.removeprefix(f"{prefix} ").strip()


def _location_value(value: str, prefix: str) -> str:
    value = value.strip()
    return f"{prefix} {value}" if value and not value.startswith(f"{prefix} ") else value


def _open_source(path: Path) -> None:
    if path.is_file():
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))


_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _parse_related_date(value: str) -> tuple[str, date | None]:
    normalized = value.translate(_PERSIAN_DIGITS).strip().replace("-", "/")
    parts = [part.strip() for part in normalized.split("/")]
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return normalized, None
    numbers = [int(part) for part in parts]
    if len(parts[0]) == 4:
        year, month, day = numbers
    elif len(parts[2]) == 4:
        day, month, year = numbers
    else:
        return normalized, None
    display = f"{year:04d}/{month:02d}/{day:02d}"
    if year < 1200 or not 1 <= month <= 12 or not 1 <= day <= 31:
        return display, None
    try:
        return display, _jalali_to_gregorian(year, month, day)
    except ValueError:
        return display, None


def _jalali_to_gregorian(year: int, month: int, day: int) -> date:
    jalali_year = year + 1595
    days = (
        -355668
        + 365 * jalali_year
        + (jalali_year // 33) * 8
        + ((jalali_year % 33 + 3) // 4)
        + day
    )
    days += (month - 1) * 31 if month < 7 else (month - 7) * 30 + 186
    gregorian_year = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        gregorian_year += 100 * ((days - 1) // 36524)
        days = (days - 1) % 36524
        if days >= 365:
            days += 1
    gregorian_year += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gregorian_year += (days - 1) // 365
        days = (days - 1) % 365
    gregorian_day = days + 1
    month_days = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    if gregorian_year % 4 == 0 and (
        gregorian_year % 100 != 0 or gregorian_year % 400 == 0
    ):
        month_days[2] = 29
    gregorian_month = 1
    while gregorian_month <= 12 and gregorian_day > month_days[gregorian_month]:
        gregorian_day -= month_days[gregorian_month]
        gregorian_month += 1
    return date(gregorian_year, gregorian_month, gregorian_day)


def _days_since_related_date(value: str, *, today: date | None = None) -> int | None:
    _display, parsed = _parse_related_date(value)
    if parsed is None:
        return None
    return ((today or date.today()) - parsed).days


class FileSelectionPage(QWizardPage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("<b>مرحله ۱ از ۵ — انتخاب اسناد و فایل‌های منبع</b>")
        self.setSubTitle(
            "فایل‌های Word، PDF متنی یا متن ساده را اضافه کنید؛ در این مرحله "
            "هیچ اطلاعاتی در سامانه ثنا ثبت نمی‌شود."
        )
        self.setAcceptDrops(True)

        self.file_list = QListWidget()
        self.file_count_label = QLabel("تعداد فایل‌های بارگذاری‌شده: ۰")
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
        layout.addWidget(self.file_count_label)
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
        self._update_file_count()
        self.completeChanged.emit()

    def _remove_selected(self) -> None:
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))
        self._update_file_count()
        self.completeChanged.emit()

    def _update_file_count(self) -> None:
        self.file_count_label.setText(
            f"تعداد فایل‌های بارگذاری‌شده: {self.file_list.count()}"
        )


class RecipientReviewPage(QWizardPage):
    HEADERS = (
        "حذف",
        "بازکردن",
        "تاریخ اظهارنامه مرتبط",
        "تعداد روز گذشته از آن تاریخ",
        "نام و نام خانوادگی",
        "نام پدر",
        "کد ملی",
        "شماره اظهارنامه",
        "آدرس",
        "شاهد استخراج",
        "نام",
        "نام خانوادگی",
        "نام فایل مرتبط",
    )
    COLUMN_WIDTHS = {
        "حذف": 104,
        "بازکردن": 112,
        "تاریخ اظهارنامه مرتبط": 126,
        "تعداد روز گذشته از آن تاریخ": 112,
        "نام و نام خانوادگی": 180,
        "نام پدر": 82,
        "کد ملی": 118,
        "شماره اظهارنامه": 150,
        "آدرس": 260,
        "شاهد استخراج": 220,
        "نام": 110,
        "نام خانوادگی": 135,
        "نام فایل مرتبط": 175,
    }

    def __init__(self) -> None:
        super().__init__()
        self.setTitle("<b>مرحله ۲ از ۵ — بازبینی و تکمیل مشخصات اشخاص</b>")
        self.setSubTitle(
            "خروجی استخراج قطعی تلقی نمی‌شود؛ هر ردیف را با سند اصلی تطبیق دهید."
        )

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        _configure_table(self.table)
        self.table.setSortingEnabled(True)
        self.table.itemChanged.connect(self._date_changed)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        for name, width in self.COLUMN_WIDTHS.items():
            self.table.setColumnWidth(self.HEADERS.index(name), width)

        add_button = QPushButton("افزودن شخص")
        remove_button = QPushButton("حذف ردیف")
        add_button.clicked.connect(lambda: self.add_candidate(None))
        remove_button.clicked.connect(self._remove_selected)
        buttons = QHBoxLayout()
        buttons.addWidget(add_button)
        buttons.addWidget(remove_button)
        buttons.addStretch()

        layout = QVBoxLayout(self)
        guidance = QLabel(
            "روش کار: ۱) تاریخ و مشخصات هر شخص را کنترل کنید. ۲) خانه‌های خالی یا اشتباه "
            "را مستقیم در جدول اصلاح کنید. ۳) اگر یک ردیف به پرونده شما مربوط نیست، "
            "آن را انتخاب و حذف کنید یا از کلید حذف همان ردیف استفاده کنید. "
            "پایان بازبینی به معنی ثبت نهایی در ثنا نیست."
        )
        guidance.setObjectName("Guidance")
        guidance.setWordWrap(True)
        layout.addWidget(guidance)
        layout.addWidget(self.table)
        layout.addLayout(buttons)

    def set_candidates(self, candidates: tuple[RecipientCandidate, ...]) -> None:
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for candidate in candidates:
            self.add_candidate(candidate)
        self.table.setSortingEnabled(sorting)

    def add_candidate(self, candidate: RecipientCandidate | None) -> None:
        sorting = self.table.isSortingEnabled()
        signals_blocked = self.table.signalsBlocked()
        self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        row = self.table.rowCount()
        self.table.insertRow(row)
        suggested_first, suggested_last = _split_full_name(
            candidate.full_name if candidate else ""
        )
        date_value, _parsed_date = _parse_related_date(
            candidate.related_declaration_date if candidate else ""
        )
        days_since = _days_since_related_date(date_value)
        values = (
            "",
            "",
            date_value,
            days_since,
            candidate.full_name if candidate else "",
            candidate.father_name if candidate else "",
            candidate.national_id if candidate else "",
            candidate.related_declaration_number if candidate else "",
            candidate.address if candidate else "",
            candidate.evidence if candidate else "",
            (candidate.first_name or suggested_first) if candidate else "",
            (candidate.last_name or suggested_last) if candidate else "",
            candidate.source_path.name if candidate else "دستی",
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem()
            item.setData(Qt.ItemDataRole.DisplayRole, value if value is not None else "")
            if self.HEADERS[column] in (
                "حذف", "بازکردن", "تعداد روز گذشته از آن تاریخ",
                "شاهد استخراج", "نام فایل مرتبط",
            ):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if self.HEADERS[column] == "تاریخ اظهارنامه مرتبط":
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
            if self.HEADERS[column] == "نام فایل مرتبط":
                item.setData(Qt.ItemDataRole.UserRole, candidate)
            self.table.setItem(row, column, item)
        open_button = QPushButton("بازکردن")
        if candidate:
            open_button.clicked.connect(
                lambda _checked=False, path=candidate.source_path: _open_source(path)
            )
        else:
            open_button.setEnabled(False)
        self.table.setCellWidget(row, self.HEADERS.index("بازکردن"), open_button)
        remove_button = QPushButton("حذف")
        remove_button.clicked.connect(
            lambda _checked=False, marker=self.table.item(row, 0):
            self.table.removeRow(marker.row())
        )
        self.table.setCellWidget(row, self.HEADERS.index("حذف"), remove_button)
        self.table.blockSignals(signals_blocked)
        self.table.setSortingEnabled(sorting)

    def candidates(self) -> tuple[RecipientCandidate, ...]:
        result = []
        for row in range(self.table.rowCount()):
            original = self.table.item(
                row, self.HEADERS.index("نام فایل مرتبط")
            ).data(Qt.ItemDataRole.UserRole)
            if not isinstance(original, RecipientCandidate):
                original = RecipientCandidate(source_path=Path("manual"))

            def value(header: str) -> str:
                column = self.HEADERS.index(header)
                editor = self.table.cellWidget(row, column)
                if isinstance(editor, QLineEdit):
                    return editor.text().strip()
                item = self.table.item(row, column)
                return item.text().strip() if item else ""

            updated = replace(
                original,
                related_declaration_number=value("شماره اظهارنامه"),
                related_declaration_date=value("تاریخ اظهارنامه مرتبط"),
                full_name=value("نام و نام خانوادگی"),
                first_name=value("نام"),
                last_name=value("نام خانوادگی"),
                father_name=value("نام پدر"),
                national_id=value("کد ملی"),
                address=value("آدرس"),
            )
            self.table.item(
                row, self.HEADERS.index("نام فایل مرتبط")
            ).setData(Qt.ItemDataRole.UserRole, updated)
            result.append(updated)
        return tuple(result)

    def _remove_selected(self) -> None:
        rows = {index.row() for index in self.table.selectedIndexes()}
        rows = sorted(rows, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    def _date_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != self.HEADERS.index("تاریخ اظهارنامه مرتبط"):
            return
        display, _parsed = _parse_related_date(item.text())
        days = _days_since_related_date(display)
        self.table.blockSignals(True)
        try:
            if item.text() != display:
                item.setText(display)
            days_item = self.table.item(
                item.row(), self.HEADERS.index("تعداد روز گذشته از آن تاریخ")
            )
            days_item.setData(
                Qt.ItemDataRole.DisplayRole, days if days is not None else ""
            )
        finally:
            self.table.blockSignals(False)

class PreprocessingPage(QWizardPage):
    HEADERS = (
        "انتخاب",
        "فایل منبع",
        "نام شخص",
        "شغل",
        "کد شغل",
        "استان",
        "کد استان",
        "شهرستان",
        "کد شهرستان",
        "شهر",
        "کد شهر",
        "آدرس",
        "بازکردن",
        "وضعیت",
        "حذف",
    )

    def __init__(self) -> None:
        super().__init__()
        self.setTitle("<b>مرحله ۳ از ۵ — پیش‌پردازش نشانی و کدهای ثنا</b>")
        self.setSubTitle(
            "کدهای نشانی را بررسی کنید؛ شغل همیشه آزاد و واحد قضایی خودکار است."
        )

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        _configure_table(self.table)
        self._lookup_dir = Path(__file__).resolve().parents[2] / "lookups"
        self.table.itemChanged.connect(self._location_changed)

        remove_button = QPushButton("حذف ردیف")
        remove_button.clicked.connect(self._remove_selected)

        guidance = QLabel(
            "روش کار: شغل همیشه «آزاد» است. اجزای نشانی فقط با کاتالوگ محلی و "
            "تطبیق دقیق به کد تبدیل می‌شوند و نشانی ناقص یا ناشناخته به تهران، "
            "تهران، تهران برمی‌گردد. واحد قضایی از نشانی در ثنا به‌روز می‌شود. این "
            "مرحله صرفاً پیش‌نمایش است و هیچ ثبت یا ورود خودکاری انجام نمی‌دهد."
        )
        guidance.setObjectName("Guidance")
        guidance.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(guidance)
        layout.addWidget(self.table)
        layout.addWidget(remove_button)

    def set_candidates(self, candidates: tuple[RecipientCandidate, ...]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for candidate in candidates:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (
                "",
                candidate.source_path.name,
                candidate.full_name,
                candidate.job_title,
                candidate.job_code,
                _location_display(candidate.province, "استان"),
                candidate.province_code,
                _location_display(candidate.county, "شهرستان"),
                candidate.county_code,
                candidate.city,
                candidate.city_code,
                candidate.address,
                "",
                self._status(candidate),
                "",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setFlags(
                        (item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        & ~Qt.ItemFlag.ItemIsEditable
                    )
                    item.setCheckState(Qt.CheckState.Unchecked)
                if self.HEADERS[column] == "نام شخص":
                    item.setData(Qt.ItemDataRole.UserRole, candidate)
                if self.HEADERS[column] in (
                    "انتخاب", "فایل منبع", "نام شخص", "شغل", "کد شغل",
                    "بازکردن", "وضعیت", "حذف",
                ):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, column, item)
            open_button = QPushButton("بازکردن")
            open_button.clicked.connect(
                lambda _checked=False, path=candidate.source_path: _open_source(path)
            )
            self.table.setCellWidget(
                row, self.HEADERS.index("بازکردن"), open_button
            )
            remove_button = QPushButton("حذف")
            remove_button.clicked.connect(
                lambda _checked=False, marker=self.table.item(row, 0):
                self.table.removeRow(marker.row())
            )
            self.table.setCellWidget(row, self.HEADERS.index("حذف"), remove_button)
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()

    def candidates(self) -> tuple[RecipientCandidate, ...]:
        result = []
        for row in range(self.table.rowCount()):
            original = self.table.item(
                row, self.HEADERS.index("نام شخص")
            ).data(Qt.ItemDataRole.UserRole)
            if not isinstance(original, RecipientCandidate):
                continue

            def value(header: str) -> str:
                item = self.table.item(row, self.HEADERS.index(header))
                return item.text().strip() if item else ""

            updated = replace(
                original,
                job_title=value("شغل"),
                job_code=value("کد شغل"),
                province=_location_value(value("استان"), "استان"),
                province_code=value("کد استان"),
                county=_location_value(value("شهرستان"), "شهرستان"),
                county_code=value("کد شهرستان"),
                city=value("شهر"),
                city_code=value("کد شهر"),
                address=value("آدرس"),
                court_unit_name="",
                court_unit_code="",
            )
            self.table.item(row, self.HEADERS.index("نام شخص")).setData(
                Qt.ItemDataRole.UserRole, updated
            )
            result.append(updated)
        return tuple(result)

    def _location_changed(self, item: QTableWidgetItem) -> None:
        if self.HEADERS[item.column()] not in ("استان", "شهرستان", "شهر"):
            return
        row = item.row()
        labels = {
            "province": _location_value(
                self.table.item(row, self.HEADERS.index("استان")).text(), "استان"
            ),
            "county": _location_value(
                self.table.item(row, self.HEADERS.index("شهرستان")).text(),
                "شهرستان",
            ),
            "city": self.table.item(row, self.HEADERS.index("شهر")).text().strip(),
        }
        self.table.blockSignals(True)
        try:
            for header in ("کد استان", "کد شهرستان", "کد شهر"):
                self.table.item(row, self.HEADERS.index(header)).setText("")
            complete = True
            for fields, code_header, prefix in (
                (("province",), "کد استان", "province"),
                (("province", "county"), "کد شهرستان", "county"),
                (("province", "county", "city"), "کد شهر", "city"),
            ):
                if not all(labels[field] for field in fields):
                    complete = False
                    break
                try:
                    resolved = resolve_lookup_fields(
                        {field: labels[field] for field in fields}, self._lookup_dir
                    )
                except LookupResolutionError:
                    if prefix == "city":
                        county_city = labels["county"].removeprefix(
                            "شهرستان "
                        ).strip()
                        try:
                            resolved = resolve_lookup_fields(
                                {
                                    "province": labels["province"],
                                    "county": labels["county"],
                                    "city": county_city,
                                },
                                self._lookup_dir,
                            )
                        except LookupResolutionError:
                            complete = False
                            break
                        labels["city"] = county_city
                        self.table.item(
                            row, self.HEADERS.index("شهر")
                        ).setText(county_city)
                    else:
                        complete = False
                        break
                self.table.item(row, self.HEADERS.index(code_header)).setText(
                    resolved.get(f"{prefix}_code", "")
                )
            self.table.item(row, self.HEADERS.index("وضعیت")).setText(
                "آماده بازبینی" if complete else "نیازمند تکمیل"
            )
        finally:
            self.table.blockSignals(False)

    def _remove_selected(self) -> None:
        rows = {
            row for row in range(self.table.rowCount())
            if self.table.item(row, 0).checkState() == Qt.CheckState.Checked
        }
        rows.update(index.row() for index in self.table.selectedIndexes())
        for row in sorted(rows, reverse=True):
            self.table.removeRow(row)

    @staticmethod
    def _status(candidate: RecipientCandidate) -> str:
        lookup_issues = (
            issue for issue in candidate.issues
            if "lookup" in issue or "unresolved" in issue or "ambiguous" in issue
        )
        return "نیازمند تکمیل" if next(lookup_issues, None) else "آماده بازبینی"


class DeclarationContentPage(QWizardPage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("<b>مرحله ۴ از ۵ — شرح اظهارنامه و فایل‌های پیوست</b>")
        self.setSubTitle(
            "شرح و اسناد را مشترک برای همه یا جداگانه برای هر ردیف تعیین کنید."
        )
        self._candidates: tuple[RecipientCandidate, ...] = ()
        self._signature: tuple[str, ...] = ()
        self._payloads: dict[int, dict[str, object]] = {}
        self._active_key = -1
        self._attachment_defaults = _load_attachment_settings()
        self._declaration_defaults = _load_declaration_settings()

        self.shared_checkbox = QCheckBox(
            "موضوع و شرح برای همهٔ اشخاص مشترک است"
        )
        self.shared_checkbox.setChecked(True)
        self.shared_attachments_checkbox = QCheckBox(
            "فایل‌ها و مشخصات پیوست برای همهٔ اشخاص مشترک است"
        )
        self.shared_attachments_checkbox.setChecked(True)
        self.recipient_combo = QComboBox()
        self.active_recipient_label = QLabel()
        self.active_recipient_label.setStyleSheet(
            "color: #1769AA; font-weight: 600; padding: 4px;"
        )
        self.subject = QLineEdit(self._declaration_defaults["subject"])
        self.subject.setPlaceholderText("موضوع دقیق اظهارنامه را وارد کنید")
        self.description = QTextEdit()
        self.description.setPlainText(self._declaration_defaults["description"])
        self.description.setPlaceholderText(
            "شرح اظهارنامه را کامل و دقیق وارد کنید. این متن قبل از ثبت قابل بازبینی است."
        )
        self.description.setMinimumHeight(480)
        self.attachment_list = QListWidget()
        self.attachment_list.setAlternatingRowColors(True)
        self.attachment_kind = QLineEdit(
            self._attachment_defaults["attachment_kind"]
        )
        self.attachment_number = QLineEdit(
            self._attachment_defaults["attachment_number"]
        )
        self.attachment_number.setPlaceholderText("شماره مدرک")
        self.attachment_date = QLineEdit(
            self._attachment_defaults["attachment_date"]
        )
        self.attachment_date.setPlaceholderText("نمونه: ۱۴۰۵/۰۷/۲۰")
        for editor in (
            self.attachment_kind,
            self.attachment_number,
            self.attachment_date,
        ):
            editor.editingFinished.connect(self._save_attachment_defaults)

        add_button = QPushButton("افزودن پیوست‌ها")
        remove_button = QPushButton("حذف پیوست انتخاب‌شده")
        add_button.clicked.connect(self._choose_attachments)
        remove_button.clicked.connect(self._remove_attachments)
        attachment_buttons = QHBoxLayout()
        attachment_buttons.addWidget(add_button)
        attachment_buttons.addWidget(remove_button)
        attachment_buttons.addStretch()

        group = QGroupBox("محتوای اظهارنامه")
        group_layout = QVBoxLayout(group)

        recipient_fields = QWidget()
        recipient_form = QFormLayout(recipient_fields)
        recipient_form.setContentsMargins(0, 0, 0, 0)
        recipient_form.addRow("مخاطب فعال:", self.recipient_combo)
        recipient_form.addRow("", self.active_recipient_label)
        recipient_form.addRow("موضوع اظهارنامه:", self.subject)

        description_field = QWidget()
        description_layout = QVBoxLayout(description_field)
        description_layout.setContentsMargins(0, 0, 0, 0)
        description_layout.addWidget(QLabel("شرح اظهارنامه:"))
        description_layout.addWidget(self.description)

        content_row = QHBoxLayout()
        content_row.setDirection(QBoxLayout.Direction.RightToLeft)
        content_row.addWidget(recipient_fields, 1)
        content_row.addWidget(description_field, 3)
        group_layout.addLayout(content_row)

        group_layout.addWidget(QLabel("فایل‌های پیوست:"))
        group_layout.addWidget(self.attachment_list)

        attachment_fields = QWidget()
        attachment_form = QFormLayout(attachment_fields)
        attachment_form.setContentsMargins(0, 0, 0, 0)
        attachment_form.addRow("نوع پیوست‌ها:", self.attachment_kind)
        attachment_form.addRow("شماره پیوست‌ها:", self.attachment_number)
        attachment_form.addRow("تاریخ پیوست‌ها:", self.attachment_date)

        attachment_fields_row = QHBoxLayout()
        attachment_fields_row.setDirection(QBoxLayout.Direction.RightToLeft)
        attachment_fields_row.addWidget(attachment_fields, 1)
        attachment_fields_row.addStretch(3)
        group_layout.addLayout(attachment_fields_row)
        group_layout.addLayout(attachment_buttons)

        guidance = QLabel(
            "راهنما: اگر شرح و پیوست‌ها برای همه یکسان است، تیک بالا را روشن "
            "بگذارید و فقط یک‌بار اطلاعات را وارد کنید. در حالت اختصاصی، از فهرست "
            "«دامنهٔ محتوا» هر شخص را انتخاب و شرح و پیوست‌های همان ردیف را ثبت کنید. "
            "نوع، شماره و تاریخ واردشده فعلاً برای همه پیوست‌های همان ردیف اعمال می‌شود."
        )
        guidance.setObjectName("Guidance")
        guidance.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(guidance)
        layout.addWidget(self.shared_checkbox)
        layout.addWidget(self.shared_attachments_checkbox)
        layout.addWidget(group)

        self.shared_checkbox.toggled.connect(self._description_scope_changed)
        self.shared_attachments_checkbox.toggled.connect(
            self._attachment_scope_changed
        )
        self.recipient_combo.currentIndexChanged.connect(self._recipient_changed)

    def set_candidates(self, candidates: tuple[RecipientCandidate, ...]) -> None:
        signature = tuple(
            str(candidate.source_path)
            for candidate in candidates
        )
        if signature == self._signature:
            self._save_active()
        self._candidates = candidates
        if signature != self._signature:
            self._signature = signature
            self._payloads.clear()
            self._active_key = -1
            seeded = []
            for candidate in candidates:
                payload = self._empty_payload()
                payload["subject"] = (
                    candidate.declaration_subject or payload["subject"]
                )
                payload["description"] = (
                    candidate.declaration_text or payload["description"]
                )
                seeded.append(payload)
            shared_content = {
                (str(payload["subject"]), str(payload["description"]))
                for payload in seeded
            }
            use_shared_description = len(shared_content) <= 1
            self.shared_checkbox.blockSignals(True)
            self.shared_checkbox.setChecked(use_shared_description)
            self.shared_checkbox.blockSignals(False)
            self.shared_attachments_checkbox.blockSignals(True)
            self.shared_attachments_checkbox.setChecked(True)
            self.shared_attachments_checkbox.blockSignals(False)
            self._payloads.update(
                (index, self._copy_payload(payload))
                for index, payload in enumerate(seeded)
            )
            self._active_key = 0 if seeded else -1
        self._rebuild_recipient_combo()

    def payloads(self) -> dict[int, dict[str, object]]:
        self._save_active()
        return {
            key: {
                "subject": str(value.get("subject", "")),
                "description": str(value.get("description", "")),
                "attachments": tuple(value.get("attachments", ())),
                "attachment_kind": str(value.get("attachment_kind", "")),
                "attachment_number": str(value.get("attachment_number", "")),
                "attachment_date": str(value.get("attachment_date", "")),
            }
            for key, value in self._payloads.items()
        }

    def _description_scope_changed(self, _shared: bool) -> None:
        current = self._editor_payload()
        for payload in self._payloads.values():
            payload["subject"] = current["subject"]
            payload["description"] = current["description"]
        self._load_active()

    def _attachment_scope_changed(self, _shared: bool) -> None:
        current = self._editor_payload()
        for payload in self._payloads.values():
            for name in (
                "attachments", "attachment_kind", "attachment_number",
                "attachment_date",
            ):
                payload[name] = current[name]
        self._load_active()

    def _rebuild_recipient_combo(self) -> None:
        self.recipient_combo.blockSignals(True)
        self.recipient_combo.clear()
        for index, candidate in enumerate(self._candidates):
            self.recipient_combo.addItem(
                candidate.full_name or f"ردیف {index + 1}", index
            )
        if self.recipient_combo.count():
            selected = max(0, self.recipient_combo.findData(self._active_key))
            self.recipient_combo.setCurrentIndex(selected)
            self._active_key = int(self.recipient_combo.currentData())
        self.recipient_combo.blockSignals(False)
        self._load_active()

    def _recipient_changed(self, _index: int) -> None:
        self._save_active()
        data = self.recipient_combo.currentData()
        if data is None:
            return
        self._active_key = int(data)
        self._load_active()

    def _save_active(self) -> None:
        if self._active_key < 0:
            return
        current = self._editor_payload()
        self._payloads[self._active_key] = current
        if self.shared_checkbox.isChecked():
            for key, payload in self._payloads.items():
                if key == self._active_key:
                    continue
                payload["subject"] = current["subject"]
                payload["description"] = current["description"]
        if self.shared_attachments_checkbox.isChecked():
            for key, payload in self._payloads.items():
                if key == self._active_key:
                    continue
                for name in (
                    "attachments", "attachment_kind", "attachment_number",
                    "attachment_date",
                ):
                    payload[name] = current[name]

    def _editor_payload(self) -> dict[str, object]:
        return {
            "subject": self.subject.text().strip(),
            "description": self.description.toPlainText().strip(),
            "attachments": tuple(self._attachment_paths()),
            "attachment_kind": self.attachment_kind.text().strip(),
            "attachment_number": self.attachment_number.text().strip(),
            "attachment_date": self.attachment_date.text().strip(),
        }

    def _load_active(self) -> None:
        payload = self._payloads.get(self._active_key, self._empty_payload())
        current_name = self.recipient_combo.currentText().strip()
        self.active_recipient_label.setText(
            f"در حال نمایش و ویرایش شرح: {current_name}"
            if current_name else "مخاطبی انتخاب نشده است"
        )
        self.subject.setText(str(payload.get("subject", "")))
        self.description.setPlainText(str(payload.get("description", "")))
        self.attachment_kind.setText(str(payload.get("attachment_kind", "")))
        self.attachment_number.setText(str(payload.get("attachment_number", "")))
        self.attachment_date.setText(str(payload.get("attachment_date", "")))
        self.attachment_list.clear()
        for path in payload.get("attachments", ()):
            self._add_attachment(Path(path))

    def _choose_attachments(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "انتخاب فایل‌های پیوست")
        for name in names:
            self._add_attachment(Path(name))

    def _add_attachment(self, path: Path) -> None:
        resolved = path.resolve()
        if not resolved.is_file() or resolved in self._attachment_paths():
            return
        item = QListWidgetItem(resolved.name)
        item.setToolTip(str(resolved))
        item.setData(Qt.ItemDataRole.UserRole, str(resolved))
        self.attachment_list.addItem(item)

    def _attachment_paths(self) -> list[Path]:
        return [
            Path(self.attachment_list.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.attachment_list.count())
        ]

    def _remove_attachments(self) -> None:
        for item in self.attachment_list.selectedItems():
            self.attachment_list.takeItem(self.attachment_list.row(item))

    def validatePage(self) -> bool:
        payloads = self.payloads()
        for key in range(len(self._candidates)):
            payload = payloads.get(key, {})
            label = f"ردیف {key + 1}"
            if not str(payload.get("subject", "")).strip():
                QMessageBox.warning(self, "موضوع ناقص", f"موضوع {label} وارد نشده است.")
                return False
            if not str(payload.get("description", "")).strip():
                QMessageBox.warning(self, "شرح ناقص", f"شرح {label} وارد نشده است.")
                return False
            if payload.get("attachments") and not all(
                str(payload.get(name, "")).strip()
                for name in (
                    "attachment_kind",
                    "attachment_number",
                    "attachment_date",
                )
            ):
                QMessageBox.warning(
                    self,
                    "مشخصات پیوست ناقص",
                    f"نوع، شماره و تاریخ پیوست‌های {label} را کامل کنید.",
                )
                return False
        self._save_declaration_defaults()
        return True

    def cleanupPage(self) -> None:
        self._save_active()
        self._save_declaration_defaults()

    def _empty_payload(self) -> dict[str, object]:
        return {
            **self._declaration_defaults,
            "attachments": (),
            **self._attachment_defaults,
        }

    def _save_declaration_defaults(self) -> None:
        subject = self.subject.text().strip()
        description = self.description.toPlainText().strip()
        if not subject or not description:
            return
        self._declaration_defaults = {
            "subject": subject,
            "description": description,
        }
        _save_declaration_settings(subject, description)

    def _save_attachment_defaults(self) -> None:
        self._attachment_defaults = {
            "attachment_kind": self.attachment_kind.text().strip(),
            "attachment_number": self.attachment_number.text().strip(),
            "attachment_date": self.attachment_date.text().strip(),
        }
        _save_attachment_settings(
            self._attachment_defaults["attachment_kind"],
            self._attachment_defaults["attachment_number"],
            self._attachment_defaults["attachment_date"],
        )

    @staticmethod
    def _copy_payload(payload: dict[str, object]) -> dict[str, object]:
        return {
            "subject": str(payload.get("subject", "")),
            "description": str(payload.get("description", "")),
            "attachments": tuple(payload.get("attachments", ())),
            "attachment_kind": str(payload.get("attachment_kind", "")),
            "attachment_number": str(payload.get("attachment_number", "")),
            "attachment_date": str(payload.get("attachment_date", "")),
        }


class LoginPage(QWizardPage):
    LOG_COLORS = {
        "system": ("#E8F1FB", "#245B88", "اطلاعات"),
        "progress": ("#E6F7FA", "#087C8C", "در حال اجرا"),
        "success": ("#E8F6EC", "#247A3D", "موفق"),
        "warning": ("#FFF4DD", "#A15C00", "هشدار"),
        "error": ("#FDEBEC", "#B42318", "خطا"),
        "debug": ("#F1F3F5", "#4B5563", "جزئیات فنی"),
    }

    def __init__(self) -> None:
        super().__init__()
        self.registration_callback = None
        self.setTitle("<b>مرحله ۵ از ۵ — ورود امن به سامانه ثنا</b>")
        self.setSubTitle(
            "ورود را جداگانه آزمایش کنید یا پس از بازبینی، پردازش و ثبت را شروع کنید."
        )

        saved = _load_login_settings()
        self.national_code = QLineEdit(saved.get("national_code", ""))
        self.password = QLineEdit(saved.get("password", ""))
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.legal_identity = QLineEdit(saved.get("legal_identity", ""))
        self.otp = QLineEdit(saved.get("otp", ""))
        self.otp.setEchoMode(QLineEdit.EchoMode.Password)
        self.remember_credentials = QCheckBox(
            "تمام مشخصات ورود، شامل رمز عبور و OTP، در این دستگاه ذخیره شود"
        )
        self.remember_credentials.setChecked(True)

        form_group = QGroupBox("مشخصات ورود")
        form = QFormLayout(form_group)
        form.addRow("کد ملی/شناسه ورود:", self.national_code)
        form.addRow("رمز عبور:", self.password)
        form.addRow("شناسه شخص حقوقی:", self.legal_identity)
        form.addRow("رمز موقت (OTP):", self.otp)
        form.addRow("", self.remember_credentials)

        guidance = QLabel(
            "هشدار امنیتی: با روشن بودن تیک، همهٔ مقادیر این صفحه به‌صورت خوانا "
            "در فایل JSON همین دستگاه ذخیره می‌شوند. OTP ممکن است در ورود بعدی "
            "منقضی شده باشد. با برداشتن تیک، فایل ذخیره‌شده حذف می‌شود."
        )
        guidance.setObjectName("Guidance")
        guidance.setWordWrap(True)

        test_button = QPushButton("آزمایش ورود بدون ثبت")
        test_button.clicked.connect(self._test_login)
        test_row = QHBoxLayout()
        test_row.addWidget(test_button)
        test_row.addStretch()

        self.python_output = QTextEdit()
        self.python_output.setReadOnly(True)
        self.python_output.setMinimumHeight(220)
        self.python_output.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.python_output.setPlaceholderText(
            "گزارش مرحله‌به‌مرحلهٔ عملیات و جزئیات فنی در این بخش نمایش داده می‌شود."
        )
        self._log_run_id = ""
        self._log_sequence = 0

        layout = QVBoxLayout(self)
        layout.addWidget(guidance)
        layout.addWidget(form_group)
        layout.addLayout(test_row)
        layout.addWidget(QLabel("گزارش اجرای عملیات (قابل انتخاب و کپی):"))
        layout.addWidget(self.python_output)
        layout.addStretch()

        for field in (
            self.national_code, self.password, self.legal_identity, self.otp
        ):
            field.textChanged.connect(lambda: self.completeChanged.emit())

    def isComplete(self) -> bool:
        return all(
            field.text().strip()
            for field in (
                self.national_code, self.password, self.legal_identity, self.otp
            )
        )

    def validatePage(self) -> bool:
        self._save_credentials()
        if self.registration_callback is None:
            QMessageBox.warning(self, "ثبت در دسترس نیست", "مسیر ثبت مقداردهی نشده است.")
            return False
        return bool(self.registration_callback(self._credentials()))

    def _test_login(self) -> None:
        answer = QMessageBox.question(
            self,
            "تأیید ورود زنده",
            "مرورگر باز شود و ورود به سامانه ثنا یک‌بار آزمایش شود؟\n"
            "در این مرحله هیچ اظهارنامه‌ای ثبت یا امضا نمی‌شود.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._save_credentials()
        self.start_log()
        self.append_output(
            "اقدام جاری: آزمایش ورود به سامانه ثنا؛ اقدام بعدی: تأیید منوی اظهارنامه",
            level="progress",
        )
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            run_login_check(self._credentials())
        except LiveBrowserError as exc:
            self.append_output(f"آزمایش ورود متوقف شد: {exc}", level="error")
            QMessageBox.warning(self, "ورود ناموفق", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.append_output(
            "ورود و منوی اظهارنامه تأیید شد؛ عملیات ثبت انجام نشد.",
            level="success",
        )
        QMessageBox.information(
            self,
            "ورود موفق",
            "ورود با موفقیت تأیید شد. هیچ اظهارنامه‌ای ثبت نشده است.",
        )

    def _credentials(self) -> LoginCredentials:
        return LoginCredentials(
            national_code=self.national_code.text().strip(),
            password=self.password.text(),
            legal_identity=self.legal_identity.text().strip(),
            otp=self.otp.text().strip(),
        )

    def _save_credentials(self) -> None:
        _save_login_settings(
            self.national_code.text(),
            self.password.text(),
            self.legal_identity.text(),
            self.otp.text(),
            remember=self.remember_credentials.isChecked(),
        )

    def start_log(self) -> None:
        now = datetime.now()
        self._log_run_id = (
            f"RUN-{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}"
        )
        self._log_sequence = 0
        self.python_output.clear()
        self.append_output("اجرای جدید آغاز شد.", level="system")

    def append_output(self, message: str, *, level: str = "system") -> None:
        if not self._log_run_id:
            self.start_log()
            if message == "اجرای جدید آغاز شد.":
                return
        self._log_sequence += 1
        background, foreground, label = self.LOG_COLORS.get(
            level, self.LOG_COLORS["system"]
        )
        technical = level == "debug"
        direction = "ltr" if technical else "rtl"
        alignment = "left" if technical else "right"
        font = "Consolas, monospace" if technical else "Segoe UI, Tahoma"
        timestamp = datetime.now().strftime("%H:%M:%S")
        safe_message = escape(self._redact_log_text(message)).replace("\n", "<br>")
        safe_run_id = escape(self._log_run_id)
        self.python_output.append(
            f'<table width="100%" cellspacing="0" cellpadding="6" '
            f'bgcolor="{background}"><tr><td dir="{direction}" '
            f'style="text-align:{alignment}; color:{foreground}; '
            f'font-family:{font};">'
            f'<span style="font-weight:700;">[{label}]</span> '
            f'<span dir="ltr" style="font-family:Consolas, monospace;">'
            f'[{timestamp}] [#{self._log_sequence:03d}] [{safe_run_id}]</span><br>'
            f'{safe_message}</td></tr></table>'
        )
        self.python_output.ensureCursorVisible()
        QApplication.processEvents(
            QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
        )

    def _redact_log_text(self, message: str) -> str:
        redacted = message
        for value in (
            self.national_code.text().strip(),
            self.password.text(),
            self.legal_identity.text().strip(),
            self.otp.text().strip(),
        ):
            if value:
                redacted = redacted.replace(value, "[حذف‌شده]")
        return redacted


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
        self.content_page = DeclarationContentPage()
        self.login_page = LoginPage()
        self.login_page.registration_callback = self._register_declarations
        self.files_page_id = self.addPage(self.files_page)
        self.review_page_id = self.addPage(self.review_page)
        self.preprocessing_page_id = self.addPage(self.preprocessing_page)
        self.content_page_id = self.addPage(self.content_page)
        self.login_page_id = self.addPage(self.login_page)
        self._loaded_paths: tuple[Path, ...] = ()
        self._preprocess_source: tuple[RecipientCandidate, ...] = ()
        self._lookup_dir = Path(__file__).resolve().parents[2] / "lookups"
        self.currentIdChanged.connect(self._page_changed)
        self.setButtonLayout([
            QWizard.WizardButton.BackButton,
            QWizard.WizardButton.NextButton,
            QWizard.WizardButton.FinishButton,
            QWizard.WizardButton.CancelButton,
            QWizard.WizardButton.Stretch,
        ])
        self.setButtonText(QWizard.WizardButton.BackButton, "قبلی")
        self.setButtonText(QWizard.WizardButton.NextButton, "بعدی")
        self.setButtonText(
            QWizard.WizardButton.FinishButton, "شروع پردازش و ثبت"
        )
        self.setButtonText(QWizard.WizardButton.CancelButton, "انصراف")
        self.button(QWizard.WizardButton.BackButton).setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowForward)
        )
        self.button(QWizard.WizardButton.NextButton).setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack)
        )
        self.button(QWizard.WizardButton.NextButton).setObjectName("PrimaryButton")
        self.button(QWizard.WizardButton.FinishButton).setObjectName("PrimaryButton")

    def _register_declarations(self, credentials: LoginCredentials) -> bool:
        self.login_page.start_log()
        report = self.login_page.append_output
        report("شروع آماده‌سازی اظهارنامه‌ها...", level="system")
        candidates = self.preprocessing_page.candidates()
        if not candidates:
            report("هیچ مخاطبی برای پردازش وجود ندارد.", level="error")
            QMessageBox.warning(self, "مخاطب وجود ندارد", "حداقل یک ردیف لازم است.")
            return False
        try:
            drafts = _build_desktop_drafts(
                candidates,
                self.content_page.payloads(),
                shared=False,
                lookup_dir=self._lookup_dir,
            )
        except LookupResolutionError as exc:
            report(f"خطای کدهای ثنا: {exc}", level="error")
            QMessageBox.warning(self, "کدهای ثنا ناقص", str(exc))
            return False

        report(f"تعداد اظهارنامه‌های آماده: {len(drafts)}", level="system")

        answer = QMessageBox.question(
            self,
            "تأیید شروع عملیات زنده",
            f"مرورگر باز شود و پردازش {len(drafts)} اظهارنامه آغاز گردد؟\n"
            "پیش از امضای نهایی هر اظهارنامه، تأیید جداگانه گرفته می‌شود.\n"
            "در صورت خطای مبهم، اجرای گروه متوقف و همان مورد تکرار نمی‌شود.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            report("عملیات توسط کاربر لغو شد.", level="warning")
            return False

        first_login = True

        def credentials_provider() -> LoginCredentials | None:
            nonlocal first_login, credentials
            if first_login:
                first_login = False
                return credentials
            otp, accepted = QInputDialog.getText(
                self,
                "ورود مجدد به ثنا",
                "نشست پایان یافته است. OTP جدید را وارد کنید:",
                QLineEdit.EchoMode.Password,
            )
            if not accepted or not otp.strip():
                return None
            credentials = replace(credentials, otp=otp.strip())
            self.login_page.otp.setText(otp.strip())
            self.login_page._save_credentials()
            return credentials

        def final_confirmation(draft: DeclarationDraft) -> bool:
            name = " ".join(
                value for value in (
                    draft.fields.get("recipient_first_name", ""),
                    draft.fields.get("recipient_last_name", ""),
                ) if value
            )
            return QMessageBox.question(
                self,
                "تأیید امضا و ثبت نهایی",
                f"اظهارنامه مخاطب «{name}» آماده امضا است.\n"
                "این اقدام نهایی و غیرقابل‌برگشت است. ثبت انجام شود؟",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) == QMessageBox.StandardButton.Yes

        try:
            report("شروع عملیات مرورگر...", level="system")
            results = run_live_upload_batch(
                drafts,
                credentials_provider,
                final_confirmation,
                progress=lambda message: report(message, level="progress"),
            )
            successful = 0
            failures = []
            for index, (draft, result) in enumerate(zip(drafts, results), start=1):
                if result.status != "success":
                    name = " ".join(filter(None, (
                        draft.fields.get("recipient_first_name", ""),
                        draft.fields.get("recipient_last_name", ""),
                    ))) or "بدون نام"
                    failures.append(
                        f"ردیف {index} ({name}): {result.message or result.status}"
                    )
                    report(failures[-1], level="warning")
                    continue
                try:
                    save_verified_result(
                        draft,
                        result,
                        filename=f"result-{index:03d}.json",
                    )
                except ResultPersistenceError as exc:
                    failures.append(f"ردیف {index}: {exc}")
                    report(failures[-1], level="error")
                else:
                    successful += 1
                    report(
                        f"ردیف {index}: ثبت موفق با کد رهگیری "
                        f"{result.tracking_code}",
                        level="success",
                    )
        except (LiveBrowserError, ValueError) as exc:
            report(f"ثبت متوقف شد: {exc}", level="error")
            report(traceback.format_exc(), level="debug")
            QMessageBox.critical(self, "ثبت متوقف شد", str(exc))
            return False

        if failures:
            QMessageBox.warning(
                self,
                "پردازش با خطا پایان یافت",
                f"موفق: {successful}\nناموفق: {len(failures)}\n\n"
                + "\n".join(failures),
            )
            return successful > 0

        QMessageBox.information(
            self,
            "ثبت کامل شد",
            f"تعداد {successful} اظهارنامه با کد رهگیری معتبر ثبت شد.",
        )
        report(f"پردازش کامل شد. موفق: {successful}", level="success")
        return True

    def _page_changed(self, page_id: int) -> None:
        if page_id == self.files_page_id:
            self._loaded_paths = ()
            self._preprocess_source = ()
            return
        if page_id == self.preprocessing_page_id:
            source = self.review_page.candidates()
            if source != self._preprocess_source:
                candidates = preprocess_candidates(source, self._lookup_dir)
                self.preprocessing_page.set_candidates(candidates)
                self._preprocess_source = source
            return
        if page_id == self.content_page_id:
            self.content_page.set_candidates(self.preprocessing_page.candidates())
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
