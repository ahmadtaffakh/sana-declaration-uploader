"""Review-first desktop interface with an explicit live-registration boundary."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from html import escape
import json
import os
from pathlib import Path
from queue import Queue
import sys
from threading import Event
import traceback
from typing import Callable

from PySide6.QtCore import QObject, QEventLoop, QSize, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QPalette,
)
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
    QProgressDialog,
    QPushButton,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWizard,
    QWizardPage,
    QWidget,
)

from .extraction import (
    extract_address_details,
    extract_recipients,
    format_recipient_address,
)
from .flow import _build_desktop_drafts
from .live_browser import (
    LoginCredentials,
    run_live_upload_batch,
)
from .api_test_flow import run_live_setpetitiondata_batch
from .petition_api import load_setpetitiondata_template
from .lookups import (
    LookupResolutionError,
    resolve_captured_jurisdiction_fields,
    resolve_lookup_fields,
)
from .models import DeclarationDraft, MAIN_STREET_MAX_LENGTH, RecipientCandidate
from .preprocessing import (
    city_for_location,
    preprocess_candidate,
    preprocess_candidates,
)
from .results import (
    ResultPersistenceError,
    RESULTS_FILENAME,
    existing_results,
    read_saved_result,
    save_verified_result,
)


_SUPPORTED_SUFFIXES = {".docx", ".pdf", ".txt"}
_SUPPORTED_API_PERSON_TYPES = {"", "حقیقی", "شخص حقیقی"}
_ATTACHMENT_KIND_OPTIONS = (
    "تصویر مدرک نمایندگی",
    "تصویر وکالت نامه",
    "سایر ضمائم",
    "تصویر فیش نقدی بانک",
    "استشهادیه محلی",
    "تصویر نامه اداره معاضدت",
    "تصویرسند الکترونیک",
    "تصویر الکترونیک وکالت نامه",
    "تصویرپاسپورت اتباع",
    "تقاضای استرداد وجه",
)
_ATTACHMENT_DEFAULTS = {
    "attachment_kind": "تصویر مدرک نمایندگی",
    "attachment_title": "",
    "attachment_number": "شماره مدرک",
    "attachment_date": "۱۴۰۵/۰۷/۲۰",
    "shared_description": "",
    "shared_attachments": "true",
}
_LEGACY_ATTACHMENT_KIND = "سایر دلایل و مستندات"
_DECLARATION_DEFAULTS = {
    "subject": "سایر موضوعات",
    "description": "شرح موضوع",
}


def _saved_attachment_count(
    saved: dict[str, object] | None,
) -> tuple[int, str] | None:
    count = (saved or {}).get("attachment_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    return count, str((saved or {}).get("attachment_count_checked_at", ""))


class _BackgroundWorker(QObject):
    progress = Signal(str)
    succeeded = Signal(object)
    failed = Signal(object, str)

    def __init__(self, task: Callable[[Callable[[str], None]], object]) -> None:
        super().__init__()
        self._task = task

    @Slot()
    def run(self) -> None:
        try:
            result = self._task(self.progress.emit)
        except Exception as exc:
            self.failed.emit(exc, traceback.format_exc())
        else:
            self.succeeded.emit(result)
_ORGANIZATIONAL_STYLE = """
QWizard, QWizardPage {
    background-color: #F3F6FA;
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
QLabel#FieldLabel, QLabel#MetricLabel {
    color: #173B5E;
    font-weight: 700;
}
QLabel#MetricLabel {
    background-color: #E7EEF5;
    border-radius: 5px;
    padding: 6px 10px;
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
QListWidget::item {
    color: #1F2937;
    padding: 4px 6px;
}
QListWidget::item:selected {
    background-color: #B9D7EE;
    color: #102F49;
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
    font-weight: 700;
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
    font-weight: 700;
}
QTabWidget::pane {
    background-color: #FFFFFF;
    border: 1px solid #CFDAE5;
    border-radius: 6px;
}
QTabBar::tab {
    background-color: #E7EEF5;
    border: 1px solid #CFDAE5;
    color: #36546F;
    font-weight: 700;
    min-width: 130px;
    padding: 8px 14px;
}
QTabBar::tab:selected {
    background-color: #FFFFFF;
    color: #173B5E;
    border-bottom-color: #FFFFFF;
}
QCheckBox {
    color: #294861;
    font-weight: 600;
    spacing: 7px;
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
QPushButton#TableIconButton {
    min-width: 24px;
    max-width: 24px;
    min-height: 24px;
    max-height: 24px;
    padding: 0;
    border: 0;
    background-color: transparent;
}
QPushButton#TableIconButton:hover { background-color: #EAF2F8; }
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
QPushButton#SuccessButton {
    background-color: #247A3D;
    border-color: #247A3D;
    color: #FFFFFF;
    font-weight: 600;
}
QPushButton#SuccessButton:hover { background-color: #1D6532; }
QPushButton#SecondaryButton {
    background-color: #E7EEF5;
    border-color: #AFC0D0;
    color: #173B5E;
    font-weight: 600;
}
QPushButton#SecondaryButton:hover { background-color: #D8E4EF; }
QPushButton#OutlineDangerButton {
    background-color: #FFF7F7;
    border-color: #D6A09C;
    color: #9B241B;
    font-weight: 600;
}
QPushButton#OutlineDangerButton:hover { background-color: #FDEBEC; }
QPushButton#SessionButton {
    background-color: #0F766E;
    border-color: #0F766E;
    color: #FFFFFF;
    font-weight: 700;
}
QPushButton#SessionButton:hover { background-color: #0B615A; }
QPushButton#ExitButton {
    background-color: #B42318;
    border-color: #B42318;
    color: #FFFFFF;
    font-weight: 700;
}
QPushButton#ExitButton:hover { background-color: #8F1D14; }
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


def _file_selection_settings_path() -> Path:
    return _settings_dir() / "file-selection.json"


def _setpetitiondata_template_path() -> Path:
    return _settings_dir() / "setpetitiondata-template.json"


def _setpetitiondata_request_text_path() -> Path:
    return _settings_dir() / "setpetitiondata-request.txt"


def _load_default_settings(
    target: Path,
    defaults: dict[str, str],
) -> dict[str, str]:
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return dict(defaults)
    if not isinstance(payload, dict):
        return dict(defaults)
    return {
        key: str(payload.get(key, default)).strip()
        for key, default in defaults.items()
    }


def _write_default_settings(target: Path, payload: dict[str, object]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_declaration_settings(path: Path | None = None) -> dict[str, str]:
    target = path or _declaration_settings_path()
    return _load_default_settings(target, _DECLARATION_DEFAULTS)


def _load_last_source_directory(path: Path | None = None) -> Path | None:
    target = path or _file_selection_settings_path()
    value = _load_default_settings(target, {"last_directory": ""})[
        "last_directory"
    ]
    directory = Path(value) if value else None
    return directory if directory and directory.is_dir() else None


def _save_last_source_directory(
    directory: Path, path: Path | None = None
) -> None:
    resolved = directory.resolve()
    if not resolved.is_dir():
        return
    _write_default_settings(
        path or _file_selection_settings_path(),
        {"last_directory": str(resolved)},
    )


def _save_declaration_settings(
    subject: str,
    description: str,
    *,
    path: Path | None = None,
) -> None:
    target = path or _declaration_settings_path()
    _write_default_settings(
        target,
        {
            "subject": subject.strip(),
            "description": description.strip(),
        },
    )


def _load_attachment_settings(path: Path | None = None) -> dict[str, str]:
    target = path or _attachment_settings_path()
    settings = _load_default_settings(target, _ATTACHMENT_DEFAULTS)
    if settings["attachment_kind"] == _LEGACY_ATTACHMENT_KIND:
        settings["attachment_kind"] = _ATTACHMENT_DEFAULTS["attachment_kind"]
    return settings


def _load_saved_attachment_paths(path: Path | None = None) -> tuple[Path, ...]:
    target = path or _attachment_settings_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ()
    values = payload.get("attachment_paths") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        return ()
    paths = tuple(Path(value) for value in values if isinstance(value, str))
    return tuple(path.resolve() for path in paths if path.is_file())


def _save_attachment_settings(
    kind: str,
    title: str,
    document_number: str,
    document_date: str,
    *,
    shared_description: bool = True,
    shared_attachments: bool = True,
    attachment_paths: tuple[Path, ...] = (),
    path: Path | None = None,
) -> None:
    target = path or _attachment_settings_path()
    _write_default_settings(
        target,
        {
            "attachment_kind": kind.strip(),
            "attachment_title": title.strip(),
            "attachment_number": document_number.strip(),
            "attachment_date": document_date.strip(),
            "shared_description": str(shared_description).lower(),
            "shared_attachments": str(shared_attachments).lower(),
            "attachment_paths": [str(item.resolve()) for item in attachment_paths],
        },
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
    if len(parts) >= 4:
        return (parts[0], " ".join(parts[1:]))
    return (" ".join(parts[:-1]), parts[-1])


def _location_display(value: str, prefix: str) -> str:
    return value.removeprefix(f"{prefix} ").strip()


def _location_value(value: str, prefix: str) -> str:
    value = value.strip()
    return f"{prefix} {value}" if value and not value.startswith(f"{prefix} ") else value


def _open_source(path: Path) -> None:
    if path.is_file():
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))


def _table_icon_action(
    icon: QStyle.StandardPixmap,
    label: str,
) -> tuple[QWidget, QPushButton]:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
    button = QPushButton(container)
    button.setObjectName("TableIconButton")
    button.setIcon(button.style().standardIcon(icon))
    button.setIconSize(QSize(14, 14))
    button.setFixedSize(24, 24)
    button.setToolTip(label)
    button.setAccessibleName(label)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    layout.addWidget(button)
    return container, button


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


def _registration_date_display(value: str) -> str:
    if not value:
        return "نامشخص"
    try:
        registered = datetime.fromisoformat(value).date()
    except ValueError:
        return "نامشخص"
    year, month, day = registered.year, registered.month, registered.day
    month_days = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    adjusted_year = year + (1 if month > 2 else 0)
    days = (
        355666 + 365 * year + (adjusted_year + 3) // 4
        - (adjusted_year + 99) // 100 + (adjusted_year + 399) // 400
        + day + month_days[month - 1]
    )
    jalali_year = -1595 + 33 * (days // 12053)
    days %= 12053
    jalali_year += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jalali_year += (days - 1) // 365
        days = (days - 1) % 365
    jalali_month = 1 + days // 31 if days < 186 else 7 + (days - 186) // 30
    jalali_day = 1 + days % 31 if days < 186 else 1 + (days - 186) % 30
    return f"{jalali_year:04d}/{jalali_month:02d}/{jalali_day:02d}"


def _days_since_related_date(value: str, *, today: date | None = None) -> int | None:
    _display, parsed = _parse_related_date(value)
    if parsed is None:
        return None
    return ((today or date.today()) - parsed).days


def _single_line_preview(value: str) -> str:
    return " ".join(value.split())


def _saved_registration(
    source_path: Path,
    *,
    source_recipient_ordinal: int = 1,
    source_recipient_count: int | None = None,
) -> dict[str, str] | None:
    try:
        return read_saved_result(
            source_path.parent,
            source_path,
            source_recipient_ordinal=source_recipient_ordinal,
            source_recipient_count=source_recipient_count,
        )
    except ResultPersistenceError:
        return {"tracking_code": "رسید نامعتبر", "registered_at": ""}


def _candidate_source_counts(
    candidates: tuple[RecipientCandidate, ...],
) -> dict[Path, int]:
    counts: dict[Path, int] = {}
    for candidate in candidates:
        source = candidate.source_path.resolve()
        counts[source] = counts.get(source, 0) + 1
    return counts


class FileSelectionPage(QWizardPage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("<b>مرحله ۱ از ۵ — انتخاب اسناد و فایل‌های منبع</b>")
        self.setSubTitle(
            "فایل‌های Word، PDF متنی یا متن ساده را اضافه کنید؛ در این مرحله "
            "هیچ اطلاعاتی در سامانه ثنا ثبت نمی‌شود."
        )
        self.setAcceptDrops(True)
        self._last_source_directory = _load_last_source_directory()

        self.file_list = QListWidget()
        palette = self.file_list.palette()
        palette.setColor(QPalette.ColorRole.Base, QColor("#FFFFFF"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#1F2937"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#B9D7EE"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#102F49"))
        self.file_list.setPalette(palette)
        self.file_count_label = QLabel("تعداد فایل‌های بارگذاری‌شده: ۰")
        self.file_count_label.setObjectName("MetricLabel")
        add_button = QPushButton("افزودن فایل‌ها")
        remove_button = QPushButton("حذف انتخاب‌شده")
        add_button.setObjectName("SecondaryButton")
        remove_button.setObjectName("OutlineDangerButton")
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
        files_label = QLabel(
            "فایل‌ها را اینجا رها کنید یا با دکمه زیر انتخاب کنید:"
        )
        files_label.setObjectName("FieldLabel")
        layout.addWidget(files_label)
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
            str(self._last_source_directory or ""),
            "اسناد پشتیبانی‌شده (*.docx *.pdf *.txt)",
        )
        if names:
            self._last_source_directory = Path(names[0]).resolve().parent
            _save_last_source_directory(self._last_source_directory)
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
        self.refresh_registration_results()
        self.completeChanged.emit()

    def refresh_registration_results(self) -> None:
        for index in range(self.file_list.count()):
            item = self.file_list.item(index)
            source_path = Path(item.data(Qt.ItemDataRole.UserRole))
            result = _saved_registration(source_path)
            item.setBackground(QColor("#FFF0C2") if result else QColor("#FFFFFF"))
            item.setForeground(QColor("#1F2937"))
            if result:
                code = result["tracking_code"] or "نامشخص"
                registered = _registration_date_display(result["registered_at"])
                item.setText(f"{source_path.name} — کد رهگیری: {code} — تاریخ ثبت: {registered}")
                item.setToolTip(
                    f"فایل: {source_path}\nکد رهگیری: {code}\nتاریخ ثبت: {registered}"
                )
            else:
                item.setText(source_path.name)
                item.setToolTip(str(source_path))

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
        "کد رهگیری",
        "تاریخ ثبت",
        "نام و نام خانوادگی",
        "نام پدر",
        "کد ملی",
        "شماره اظهارنامه",
        "نام",
        "نام خانوادگی",
        "نام فایل مرتبط",
        "آدرس",
        "شاهد استخراج",
    )
    COLUMN_WIDTHS = {
        "حذف": 48,
        "بازکردن": 64,
        "تاریخ اظهارنامه مرتبط": 126,
        "تعداد روز گذشته از آن تاریخ": 112,
        "نام و نام خانوادگی": 180,
        "نام پدر": 82,
        "کد ملی": 118,
        "شماره اظهارنامه": 150,
        "آدرس": 520,
        "شاهد استخراج": 660,
        "نام": 110,
        "نام خانوادگی": 135,
        "نام فایل مرتبط": 175,
        "کد رهگیری": 160,
        "تاریخ ثبت": 110,
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
        self.table.setWordWrap(False)
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
        add_button.setObjectName("SecondaryButton")
        remove_button.setObjectName("OutlineDangerButton")
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
            "",
            "",
            candidate.full_name if candidate else "",
            candidate.father_name if candidate else "",
            candidate.national_id if candidate else "",
            candidate.related_declaration_number if candidate else "",
            (candidate.first_name or suggested_first) if candidate else "",
            (candidate.last_name or suggested_last) if candidate else "",
            candidate.source_path.name if candidate else "دستی",
            candidate.address if candidate else "",
            candidate.evidence if candidate else "",
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem()
            item.setData(Qt.ItemDataRole.DisplayRole, value if value is not None else "")
            header_name = self.HEADERS[column]
            if header_name in ("آدرس", "شاهد استخراج"):
                full_text = str(value or "")
                item.setText(_single_line_preview(full_text))
                item.setToolTip(full_text)
            if self.HEADERS[column] in (
                "حذف", "بازکردن", "تعداد روز گذشته از آن تاریخ",
                "شاهد استخراج", "نام فایل مرتبط", "کد رهگیری", "تاریخ ثبت",
            ):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if self.HEADERS[column] == "تاریخ اظهارنامه مرتبط":
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
            if self.HEADERS[column] == "نام فایل مرتبط":
                item.setData(Qt.ItemDataRole.UserRole, candidate)
            self.table.setItem(row, column, item)
        open_widget, open_button = _table_icon_action(
            QStyle.StandardPixmap.SP_DialogOpenButton,
            "بازکردن فایل",
        )
        if candidate:
            open_button.clicked.connect(
                lambda _checked=False, path=candidate.source_path: _open_source(path)
            )
        else:
            open_button.setEnabled(False)
        self.table.setCellWidget(row, self.HEADERS.index("بازکردن"), open_widget)
        remove_widget, remove_button = _table_icon_action(
            QStyle.StandardPixmap.SP_TrashIcon,
            "حذف ردیف",
        )
        remove_button.clicked.connect(
            lambda _checked=False, marker=self.table.item(row, 0):
            self.table.removeRow(marker.row())
        )
        self.table.setCellWidget(row, self.HEADERS.index("حذف"), remove_widget)
        self.table.blockSignals(signals_blocked)
        self.table.setSortingEnabled(sorting)
        self.refresh_registration_results()

    def refresh_registration_results(self) -> None:
        candidates = []
        for row in range(self.table.rowCount()):
            source_item = self.table.item(row, self.HEADERS.index("نام فایل مرتبط"))
            candidate = source_item.data(Qt.ItemDataRole.UserRole) if source_item else None
            if isinstance(candidate, RecipientCandidate):
                candidates.append(candidate)
        source_counts = _candidate_source_counts(tuple(candidates))
        for row in range(self.table.rowCount()):
            source_item = self.table.item(row, self.HEADERS.index("نام فایل مرتبط"))
            candidate = source_item.data(Qt.ItemDataRole.UserRole) if source_item else None
            if not isinstance(candidate, RecipientCandidate):
                continue
            result = _saved_registration(
                candidate.source_path,
                source_recipient_ordinal=candidate.source_recipient_ordinal,
                source_recipient_count=source_counts[candidate.source_path.resolve()],
            )
            if result is None:
                continue
            code_item = self.table.item(row, self.HEADERS.index("کد رهگیری"))
            date_item = self.table.item(row, self.HEADERS.index("تاریخ ثبت"))
            code_item.setText(result["tracking_code"] or "نامشخص")
            date_item.setText(_registration_date_display(result["registered_at"]))
            for column in range(self.table.columnCount()):
                item = self.table.item(row, column)
                if item is not None:
                    item.setBackground(QColor("#FFF0C2"))

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
        "حذف",
        "بازکردن",
        "انتخاب",
        "فایل منبع",
        "کد رهگیری",
        "تاریخ ثبت",
        "نام شخص",
        "شغل",
        "کد شغل",
        "استان",
        "کد استان",
        "شهرستان",
        "کد شهرستان",
        "شهر",
        "کد شهر",
        "حوزه قضایی",
        "کد حوزه",
        "خیابان اصلی",
        "کد پستی",
        "آدرس",
        "وضعیت",
    )

    def __init__(self) -> None:
        super().__init__()
        self.setTitle("<b>مرحله ۳ از ۵ — پیش‌پردازش نشانی و کدهای ثنا</b>")
        self.setSubTitle(
            "کدهای نشانی را بررسی کنید؛ ردیف‌های دارای کد رهگیری قبلی رنگی‌اند. "
            "شغل همیشه آزاد و واحد قضایی خودکار است."
        )

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        _configure_table(self.table)
        self._lookup_dir = Path(__file__).resolve().parents[2] / "lookups"
        self.table.itemChanged.connect(self._location_changed)

        remove_widget, remove_button = _table_icon_action(
            QStyle.StandardPixmap.SP_TrashIcon, "حذف ردیف"
        )
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
        layout.addWidget(remove_widget, alignment=Qt.AlignmentFlag.AlignRight)

    def set_candidates(self, candidates: tuple[RecipientCandidate, ...]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for candidate in candidates:
            row = self.table.rowCount()
            self.table.insertRow(row)
            main_street, postal_code = extract_address_details(
                candidate.address,
                candidate.province,
                candidate.county,
                candidate.city,
            )
            formatted_address = format_recipient_address(
                candidate.address,
                candidate.province,
                candidate.county,
                candidate.city,
            )
            values = (
                "",
                "",
                "",
                candidate.source_path.name,
                "",
                "",
                candidate.full_name,
                candidate.job_title,
                candidate.job_code,
                _location_display(candidate.province, "استان"),
                candidate.province_code,
                _location_display(candidate.county, "شهرستان"),
                candidate.county_code,
                candidate.city,
                candidate.city_code,
                *self._jurisdiction_display({
                    "province": candidate.province,
                    "county": candidate.county,
                    "city": candidate.city,
                }),
                (candidate.main_street or main_street)[
                    :MAIN_STREET_MAX_LENGTH
                ].strip(),
                candidate.postal_code or postal_code,
                formatted_address,
                self._status(candidate),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if self.HEADERS[column] == "انتخاب":
                    item.setFlags(
                        (item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        & ~Qt.ItemFlag.ItemIsEditable
                    )
                    item.setCheckState(Qt.CheckState.Unchecked)
                if self.HEADERS[column] == "نام شخص":
                    item.setData(Qt.ItemDataRole.UserRole, candidate)
                if self.HEADERS[column] in (
                    "انتخاب", "فایل منبع", "نام شخص", "شغل", "کد شغل",
                    "کد رهگیری", "تاریخ ثبت", "حوزه قضایی", "کد حوزه",
                    "بازکردن", "وضعیت", "حذف",
                ):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, column, item)
            open_widget, open_button = _table_icon_action(
                QStyle.StandardPixmap.SP_DialogOpenButton, "بازکردن فایل"
            )
            open_button.clicked.connect(
                lambda _checked=False, path=candidate.source_path: _open_source(path)
            )
            remove_widget, remove_button = _table_icon_action(
                QStyle.StandardPixmap.SP_TrashIcon, "حذف ردیف"
            )
            remove_button.clicked.connect(
                lambda _checked=False, marker=self.table.item(
                    row, self.HEADERS.index("نام شخص")
                ):
                self.table.removeRow(marker.row())
            )
            self.table.setCellWidget(row, self.HEADERS.index("بازکردن"), open_widget)
            self.table.setCellWidget(row, self.HEADERS.index("حذف"), remove_widget)
        self.refresh_registration_results()
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()

    def refresh_registration_results(self) -> None:
        candidates = []
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, self.HEADERS.index("نام شخص"))
            candidate = name_item.data(Qt.ItemDataRole.UserRole) if name_item else None
            if isinstance(candidate, RecipientCandidate):
                candidates.append(candidate)
        source_counts = _candidate_source_counts(tuple(candidates))
        for row in range(self.table.rowCount()):
            candidate = self.table.item(
                row, self.HEADERS.index("نام شخص")
            ).data(Qt.ItemDataRole.UserRole)
            if not isinstance(candidate, RecipientCandidate):
                continue
            try:
                saved_result = read_saved_result(
                    candidate.source_path.parent,
                    candidate.source_path,
                    source_recipient_ordinal=candidate.source_recipient_ordinal,
                    source_recipient_count=source_counts[
                        candidate.source_path.resolve()
                    ],
                )
            except ResultPersistenceError:
                saved_result = {"tracking_code": "رسید نامعتبر", "registered_at": ""}
            if saved_result is None:
                continue
            self.table.item(row, self.HEADERS.index("کد رهگیری")).setText(
                saved_result["tracking_code"] or "نامشخص"
            )
            self.table.item(row, self.HEADERS.index("تاریخ ثبت")).setText(
                _registration_date_display(saved_result["registered_at"])
            )
            for column in range(len(self.HEADERS)):
                item = self.table.item(row, column)
                if item is not None:
                    item.setBackground(QColor("#FFF0C2"))

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

            province = _location_value(value("استان"), "استان")
            county = _location_value(value("شهرستان"), "شهرستان")
            city = value("شهر")
            updated = replace(
                original,
                job_title=value("شغل"),
                job_code=value("کد شغل"),
                province=province,
                province_code=value("کد استان"),
                county=county,
                county_code=value("کد شهرستان"),
                city=city,
                city_code=value("کد شهر"),
                main_street=value("خیابان اصلی")[
                    :MAIN_STREET_MAX_LENGTH
                ].strip(),
                postal_code=value("کد پستی"),
                address=format_recipient_address(
                    value("آدرس"), province, county, city
                ),
                court_unit_name=value("حوزه قضایی"),
                court_unit_code=value("کد حوزه"),
            )
            self.table.item(row, self.HEADERS.index("نام شخص")).setData(
                Qt.ItemDataRole.UserRole, updated
            )
            result.append(updated)
        return tuple(result)

    def _location_changed(self, item: QTableWidgetItem) -> None:
        header = self.HEADERS[item.column()]
        if header == "خیابان اصلی":
            limited = item.text()[:MAIN_STREET_MAX_LENGTH].strip()
            if item.text() != limited:
                self.table.blockSignals(True)
                item.setText(limited)
                self.table.blockSignals(False)
            return
        if header not in ("استان", "شهرستان", "شهر"):
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
        labels["city"] = city_for_location(
            labels["province"], labels["county"], labels["city"]
        )
        self.table.blockSignals(True)
        try:
            self.table.item(row, self.HEADERS.index("شهر")).setText(labels["city"])
            for header in ("کد استان", "کد شهرستان", "کد شهر"):
                self.table.item(row, self.HEADERS.index(header)).setText("")
            self.table.item(
                row, self.HEADERS.index("حوزه قضایی")
            ).setText("")
            self.table.item(row, self.HEADERS.index("کد حوزه")).setText("")
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
            if complete:
                court_name, court_code = self._jurisdiction_display(resolved)
                self.table.item(
                    row, self.HEADERS.index("حوزه قضایی")
                ).setText(court_name)
                self.table.item(
                    row, self.HEADERS.index("کد حوزه")
                ).setText(court_code)
            self.table.item(row, self.HEADERS.index("وضعیت")).setText(
                "آماده بازبینی" if complete else "نیازمند تکمیل"
            )
        finally:
            self.table.blockSignals(False)

    def _jurisdiction_display(self, location: dict[str, str]) -> tuple[str, str]:
        try:
            resolved = resolve_lookup_fields(location, self._lookup_dir)
            jurisdiction = resolve_captured_jurisdiction_fields(
                resolved, self._lookup_dir
            )
        except LookupResolutionError:
            return "", ""
        return (
            jurisdiction["notify_owner_organization_title"],
            jurisdiction["notify_owner_organization_code"],
        )

    def _remove_selected(self) -> None:
        rows = {
            row for row in range(self.table.rowCount())
            if self.table.item(
                row, self.HEADERS.index("انتخاب")
            ).checkState() == Qt.CheckState.Checked
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
        self._saved_attachment_paths = _load_saved_attachment_paths()
        self._declaration_defaults = _load_declaration_settings()

        self.shared_checkbox = QCheckBox(
            "موضوع و شرح برای همهٔ اشخاص مشترک است"
        )
        self.shared_checkbox.setChecked(
            self._attachment_defaults.get("shared_description", "true").lower()
            != "false"
        )
        self.shared_attachments_checkbox = QCheckBox(
            "فایل‌ها و مشخصات پیوست برای همهٔ اشخاص مشترک است"
        )
        self.shared_attachments_checkbox.setChecked(
            self._attachment_defaults.get("shared_attachments", "true").lower()
            == "true"
        )
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
        self.attachment_kind = QComboBox()
        self.attachment_kind.addItems(_ATTACHMENT_KIND_OPTIONS)
        saved_attachment_kind = self._attachment_defaults["attachment_kind"]
        if self.attachment_kind.findText(saved_attachment_kind) < 0:
            self.attachment_kind.addItem(saved_attachment_kind)
        self.attachment_kind.setCurrentText(saved_attachment_kind)
        self.attachment_number = QLineEdit(
            self._attachment_defaults["attachment_number"]
        )
        self.attachment_number.setPlaceholderText("شماره مدرک")
        self.attachment_date = QLineEdit(
            self._attachment_defaults["attachment_date"]
        )
        self.attachment_date.setPlaceholderText("نمونه: ۱۴۰۵/۰۷/۲۰")
        self.attachment_title = QLineEdit(
            self._attachment_defaults["attachment_title"]
        )
        self.attachment_title.setPlaceholderText("عنوانی که در اظهارنامه نمایش داده می‌شود")
        for editor in (
            self.attachment_title, self.attachment_number, self.attachment_date
        ):
            editor.editingFinished.connect(self._save_attachment_defaults)
        self.attachment_kind.activated.connect(
            lambda _index: self._save_attachment_defaults()
        )

        add_button = QPushButton("افزودن پیوست‌ها")
        remove_button = QPushButton("حذف پیوست انتخاب‌شده")
        add_button.setObjectName("SecondaryButton")
        remove_button.setObjectName("OutlineDangerButton")
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
        for editor in (self.recipient_combo, self.subject):
            label = recipient_form.labelForField(editor)
            if label is not None:
                label.setObjectName("FieldLabel")

        description_field = QWidget()
        description_layout = QVBoxLayout(description_field)
        description_layout.setContentsMargins(0, 0, 0, 0)
        description_label = QLabel("شرح اظهارنامه:")
        description_label.setObjectName("FieldLabel")
        description_layout.addWidget(description_label)
        description_layout.addWidget(self.description)

        content_row = QHBoxLayout()
        content_row.setDirection(QBoxLayout.Direction.RightToLeft)
        content_row.addWidget(recipient_fields, 1)
        content_row.addWidget(description_field, 3)
        group_layout.addLayout(content_row)

        attachments_label = QLabel("فایل‌های پیوست:")
        attachments_label.setObjectName("FieldLabel")
        group_layout.addWidget(attachments_label)
        group_layout.addWidget(self.attachment_list)

        attachment_fields = QWidget()
        attachment_form = QFormLayout(attachment_fields)
        attachment_form.setContentsMargins(0, 0, 0, 0)
        attachment_form.addRow("نوع پیوست‌ها:", self.attachment_kind)
        attachment_form.addRow("عنوان پیوست:", self.attachment_title)
        attachment_form.addRow("شماره پیوست‌ها:", self.attachment_number)
        attachment_form.addRow("تاریخ پیوست‌ها:", self.attachment_date)
        for editor in (
            self.attachment_kind,
            self.attachment_title,
            self.attachment_number,
            self.attachment_date,
        ):
            label = attachment_form.labelForField(editor)
            if label is not None:
                label.setObjectName("FieldLabel")

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
            "نوع، عنوان، شماره و تاریخ واردشده برای همه پیوست‌های همان ردیف اعمال می‌شود."
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
            saved_shared_description = self._attachment_defaults.get(
                "shared_description", ""
            ).lower()
            use_shared_description = (
                len(shared_content) <= 1
                if not saved_shared_description
                else saved_shared_description == "true"
            )
            self.shared_checkbox.blockSignals(True)
            self.shared_checkbox.setChecked(use_shared_description)
            self.shared_checkbox.blockSignals(False)
            self.shared_attachments_checkbox.blockSignals(True)
            self.shared_attachments_checkbox.setChecked(
                self._attachment_defaults.get("shared_attachments", "true").lower()
                == "true"
            )
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
                "attachment_title": str(value.get("attachment_title", "")),
                "attachment_number": str(value.get("attachment_number", "")),
                "attachment_date": str(value.get("attachment_date", "")),
            }
            for key, value in self._payloads.items()
        }

    def active_draft(self, lookup_dir: Path) -> DeclarationDraft:
        index = self.recipient_combo.currentData()
        if index is None or not 0 <= int(index) < len(self._candidates):
            raise ValueError("api_test_recipient_not_selected")
        candidate = self._candidates[int(index)]
        if "location_defaulted" in candidate.issues:
            candidate = preprocess_candidate(
                replace(
                    candidate,
                    province="",
                    province_code="",
                    county="",
                    county_code="",
                    city="",
                    city_code="",
                ),
                lookup_dir,
            )
            if "location_defaulted" in candidate.issues:
                raise LookupResolutionError("recipient_location_not_found")
        location = resolve_lookup_fields(
            {
                "job_title": "آزاد",
                "province": candidate.province,
                "county": candidate.county,
                "city": candidate.city,
            },
            lookup_dir,
        )
        if any(
            not location.get(f"{field}_code", "").strip()
            for field in ("province", "county", "city")
        ):
            raise LookupResolutionError("recipient_location_codes_missing")
        payload = self.payloads().get(int(index), {})
        draft = _build_desktop_drafts(
            (candidate,),
            {0: payload},
            shared=False,
            lookup_dir=lookup_dir,
        )[0]
        fields = dict(draft.fields)
        fields.update(
            {
                key: value
                for key, value in location.items()
                if key in {
                    "province", "province_id", "province_code",
                    "county", "county_id", "county_code",
                    "city", "city_id", "city_code",
                }
            }
        )
        return replace(draft, fields=fields)

    def _description_scope_changed(self, _shared: bool) -> None:
        current = self._editor_payload()
        for payload in self._payloads.values():
            payload["subject"] = current["subject"]
            payload["description"] = current["description"]
        self._load_active()
        self._save_attachment_defaults()

    def _attachment_scope_changed(self, _shared: bool) -> None:
        current = self._editor_payload()
        for payload in self._payloads.values():
            for name in (
                "attachments", "attachment_kind", "attachment_title",
                "attachment_number", "attachment_date",
            ):
                payload[name] = current[name]
        self._load_active()
        self._save_attachment_defaults()

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
                    "attachments", "attachment_kind", "attachment_title",
                    "attachment_number", "attachment_date",
                ):
                    payload[name] = current[name]

    def _editor_payload(self) -> dict[str, object]:
        return {
            "subject": self.subject.text().strip(),
            "description": self.description.toPlainText().strip(),
            "attachments": tuple(self._attachment_paths()),
            "attachment_kind": self.attachment_kind.currentText().strip(),
            "attachment_title": self.attachment_title.text().strip(),
            "attachment_title": self.attachment_title.text().strip(),
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
        attachment_kind = str(payload.get("attachment_kind", ""))
        if self.attachment_kind.findText(attachment_kind) < 0:
            self.attachment_kind.addItem(attachment_kind)
        self.attachment_kind.setCurrentText(attachment_kind)
        self.attachment_title.setText(str(payload.get("attachment_title", "")))
        self.attachment_number.setText(str(payload.get("attachment_number", "")))
        self.attachment_date.setText(str(payload.get("attachment_date", "")))
        self.attachment_list.clear()
        for path in payload.get("attachments", ()):
            self._add_attachment(Path(path))

    def _choose_attachments(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "انتخاب فایل‌های پیوست")
        for name in names:
            self._add_attachment(Path(name))
        if names:
            self._save_active()
            self._save_attachment_defaults()

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
        self._save_active()
        self._save_attachment_defaults()

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
                    "attachment_title",
                    "attachment_number",
                    "attachment_date",
                )
            ):
                QMessageBox.warning(
                    self,
                    "مشخصات پیوست ناقص",
                    f"نوع، عنوان، شماره و تاریخ پیوست‌های {label} را کامل کنید.",
                )
                return False
        self._save_declaration_defaults()
        self._save_attachment_defaults()
        return True

    def cleanupPage(self) -> None:
        self._save_active()
        self._save_declaration_defaults()
        self._save_attachment_defaults()

    def _empty_payload(self) -> dict[str, object]:
        return {
            **self._declaration_defaults,
            "attachments": self._saved_attachment_paths,
            "attachment_title": "",
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
        self._saved_attachment_paths = tuple(self._attachment_paths())
        self._attachment_defaults = {
            "attachment_kind": self.attachment_kind.currentText().strip(),
            "attachment_title": self.attachment_title.text().strip(),
            "attachment_number": self.attachment_number.text().strip(),
            "attachment_date": self.attachment_date.text().strip(),
            "shared_description": str(self.shared_checkbox.isChecked()).lower(),
            "shared_attachments": str(
                self.shared_attachments_checkbox.isChecked()
            ).lower(),
        }
        _save_attachment_settings(
            self._attachment_defaults["attachment_kind"],
            self._attachment_defaults["attachment_title"],
            self._attachment_defaults["attachment_number"],
            self._attachment_defaults["attachment_date"],
            shared_description=self.shared_checkbox.isChecked(),
            shared_attachments=self.shared_attachments_checkbox.isChecked(),
            attachment_paths=self._saved_attachment_paths,
        )

    @staticmethod
    def _copy_payload(payload: dict[str, object]) -> dict[str, object]:
        return {
            "subject": str(payload.get("subject", "")),
            "description": str(payload.get("description", "")),
            "attachments": tuple(payload.get("attachments", ())),
            "attachment_kind": str(payload.get("attachment_kind", "")),
            "attachment_title": str(payload.get("attachment_title", "")),
            "attachment_number": str(payload.get("attachment_number", "")),
            "attachment_date": str(payload.get("attachment_date", "")),
        }


class LoginPage(QWizardPage):
    STATUS_HEADERS = (
        "ردیف",
        "نام مخاطب",
        "فایل Word",
        "موضوع اظهارنامه",
        "کد رهگیری",
        "وضعیت اظهارنامه",
        "وضعیت پیوست",
        "تعداد ضمائم در ثنا",
        "توضیح",
    )
    STATUS_DISPLAY = {
        "pending": ("… در انتظار", "#667085"),
        "running": ("● در حال اجرا", "#1769AA"),
        "success": ("✓ موفق", "#247A3D"),
        "failed": ("✕ ناموفق", "#B42318"),
        "unknown": ("؟ نیازمند بررسی", "#A15C00"),
        "skipped": ("— پیوستی ندارد", "#667085"),
    }

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
        self.api_test_callback = None
        self.attach_existing_callback = None
        self.refresh_counts_callback = None
        self.api_session_active_callback = None
        self.close_browser_callback = None
        self.exit_callback = None
        self.stop_callback = None
        self._operation_rows: dict[tuple[str, int], int] = {}
        self.setTitle("<b>مرحله ۵ از ۵ — ورود امن به سامانه ثنا</b>")
        self.setSubTitle(
            "مشخصات ورود را کامل کنید، سپس عملیات موردنظر را از بخش مناسب انتخاب کنید."
        )

        saved = _load_login_settings()
        self.national_code = QLineEdit(saved.get("national_code", ""))
        self.password = QLineEdit(saved.get("password", ""))
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.legal_identity = QLineEdit(saved.get("legal_identity", ""))
        self.otp = QLineEdit(saved.get("otp", ""))
        self.otp.setEchoMode(QLineEdit.EchoMode.Password)
        self.otp.setPlaceholderText("برای ورود مجدد در صورت پایان نشست")
        self.remember_credentials = QCheckBox(
            "تمام مشخصات ورود، شامل رمز عبور و OTP، در این دستگاه ذخیره شود"
        )
        self.remember_credentials.setChecked(True)

        self.credentials_group = QGroupBox("مشخصات ورود")
        credentials_layout = QVBoxLayout(self.credentials_group)
        credentials_row = QHBoxLayout()
        for label_text, editor in (
            ("کد ملی/شناسه ورود:", self.national_code),
            ("رمز عبور:", self.password),
            ("شناسه شخص حقوقی:", self.legal_identity),
            ("رمز موقت (OTP):", self.otp),
        ):
            field_widget = QWidget()
            field_layout = QVBoxLayout(field_widget)
            field_layout.setContentsMargins(0, 0, 0, 0)
            field_layout.setSpacing(3)
            label = QLabel(label_text)
            label.setObjectName("FieldLabel")
            label.setBuddy(editor)
            field_layout.addWidget(label)
            field_layout.addWidget(editor)
            credentials_row.addWidget(field_widget, 1)
        credentials_layout.addLayout(credentials_row)
        credentials_layout.addWidget(self.remember_credentials)

        guidance = QLabel(
            "هشدار امنیتی: با روشن بودن تیک، همهٔ مقادیر این صفحه به‌صورت خوانا "
            "در فایل JSON همین دستگاه ذخیره می‌شوند. OTP ممکن است در ورود بعدی "
            "منقضی شده باشد. با برداشتن تیک، فایل ذخیره‌شده حذف می‌شود."
        )
        guidance.setObjectName("Guidance")
        guidance.setWordWrap(True)

        self.api_test_button = QPushButton(
            "ثبت موقت اظهارنامه‌ها و افزودن پیوست‌ها"
        )
        self.api_test_button.setObjectName("PrimaryButton")
        self.api_test_button.setToolTip(
            "اظهارنامه‌های آماده را موقت ثبت می‌کند و امضای نهایی ندارد. "
            "تا وقتی مرورگر باز است از همان نشست استفاده می‌شود؛ اگر نشست "
            "تمام شود، برای ورود دوباره OTP تازه می‌پرسد."
        )
        self.api_test_button.setAccessibleName(
            "ثبت موقت اظهارنامه‌ها و افزودن پیوست‌ها"
        )
        self.api_test_button.clicked.connect(self._test_api_registration)
        self.attach_existing_button = QPushButton(
            "فقط افزودن پیوست به اظهارنامه‌های دارای کد رهگیری"
        )
        self.attach_existing_button.setObjectName("SuccessButton")
        self.attach_existing_button.setToolTip(
            "کد رهگیری‌های ذخیره‌شده را جستجو می‌کند و فایل‌های انتخاب‌شده را "
            "به همان اظهارنامه‌ها می‌افزاید؛ اظهارنامه دوباره ثبت نمی‌شود."
        )
        self.attach_existing_button.setAccessibleName(
            "افزودن پیوست بدون ثبت دوباره اظهارنامه"
        )
        self.attach_existing_button.clicked.connect(self._attach_existing)
        self.refresh_counts_button = QPushButton("بازخوانی تعداد ضمائم")
        self.refresh_counts_button.setObjectName("SecondaryButton")
        self.refresh_counts_button.setToolTip(
            "فقط اظهارنامه‌های دارای کد رهگیری را از ثنا می‌خواند و تعداد "
            "ضمائم را در جدول و فایل JSON به‌روز می‌کند؛ هیچ ثبت یا آپلودی ندارد."
        )
        self.refresh_counts_button.setAccessibleName(
            "بازخوانی خواندنی تعداد ضمائم از ثنا"
        )
        self.refresh_counts_button.clicked.connect(self._refresh_counts)
        self.close_browser_button = QPushButton("بستن نشست ثنا")
        self.close_browser_button.setObjectName("SessionButton")
        self.close_browser_button.setEnabled(False)
        self.close_browser_button.setVisible(False)
        self.close_browser_button.setToolTip(
            "مرورگر و نشست ثنا را می‌بندد. برای درخواست بعدی باید دوباره وارد شوید."
        )
        self.close_browser_button.setAccessibleName("بستن مرورگر ثنا")
        self.close_browser_button.clicked.connect(self._close_api_browser)
        self.exit_button = QPushButton("خروج از برنامه")
        self.exit_button.setObjectName("ExitButton")
        self.exit_button.setToolTip(
            "نشست مرورگر ثنا و برنامه را بدون نمایش پیام تأیید می‌بندد."
        )
        self.exit_button.setAccessibleName("بستن مرورگر ثنا و خروج از برنامه")
        self.exit_button.clicked.connect(self._exit_application)
        self.stop_button = QPushButton("توقف پس از ردیف جاری")
        self.stop_button.setEnabled(False)
        self.stop_button.setVisible(False)
        self.stop_button.setToolTip(
            "بعد از تمام‌شدن ردیف در حال اجرا متوقف می‌کند؛ همان ردیف دوباره فرستاده نمی‌شود."
        )
        self.stop_button.setAccessibleName("توقف پس از ردیف جاری")
        self.stop_button.setStyleSheet(
            "QPushButton { background:#B42318; color:white; font-weight:bold; }"
            "QPushButton:hover { background:#8F1D14; }"
            "QPushButton:disabled { background:#D8A6A3; color:#F7EEEE; }"
        )
        self.stop_button.clicked.connect(self._request_stop)
        self.api_template_button = QPushButton("انتخاب یا تغییر قالب فنی ثبت")
        self.api_template_button.setAccessibleName("انتخاب فایل قالب فنی ثبت")
        self.api_template_button.clicked.connect(self._choose_api_template)
        self._refresh_api_template_status()

        self.action_row = QHBoxLayout()
        self.action_row.addWidget(self.api_test_button, 3)
        self.action_row.addWidget(self.attach_existing_button, 3)
        self.action_row.addWidget(self.refresh_counts_button, 2)
        self.action_row.addWidget(self.api_template_button, 2)
        self.action_row.addWidget(self.close_browser_button, 1)
        self.action_row.addWidget(self.exit_button, 1)
        self.action_row.addWidget(self.stop_button, 1)

        self.python_output = QTextEdit()
        self.python_output.setReadOnly(True)
        self.python_output.setMinimumHeight(220)
        self.python_output.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.python_output.setPlaceholderText(
            "گزارش مرحله‌به‌مرحلهٔ عملیات و جزئیات فنی در این بخش نمایش داده می‌شود."
        )
        self.status_table = QTableWidget(0, len(self.STATUS_HEADERS))
        self.status_table.setHorizontalHeaderLabels(self.STATUS_HEADERS)
        _configure_table(self.status_table)
        self.status_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.status_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        status_header = self.status_table.horizontalHeader()
        status_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        status_header.setSectionResizeMode(
            self.STATUS_HEADERS.index("توضیح"), QHeaderView.ResizeMode.Stretch
        )

        self.output_tabs = QTabWidget()
        self.output_tabs.addTab(self.status_table, "وضعیت اظهارنامه‌ها")
        self.output_tabs.addTab(self.python_output, "گزارش فنی")
        self.output_tabs.setCurrentIndex(0)
        self.output_tabs.setMinimumHeight(220)
        self._log_run_id = ""
        self._log_sequence = 0

        layout = QVBoxLayout(self)
        layout.addWidget(guidance)
        layout.addWidget(self.credentials_group)
        layout.addLayout(self.action_row)
        layout.addWidget(self.output_tabs, 1)

        for field in (
            self.national_code, self.password, self.legal_identity, self.otp
        ):
            field.textChanged.connect(lambda: self.completeChanged.emit())
        self.otp.editingFinished.connect(self._save_credentials)

    def _refresh_api_template_status(self) -> None:
        path = _setpetitiondata_template_path()
        if not path.is_file():
            status = "قالب فنی ثبت را یک‌بار انتخاب کنید."
        else:
            try:
                load_setpetitiondata_template(path)
            except (OSError, ValueError, RuntimeError):
                status = "قالب فنی ثبت نامعتبر است؛ آن را دوباره انتخاب کنید."
            else:
                status = "قالب فنی ثبت آماده است."
        self.api_template_button.setToolTip(
            "فایل قالب فنی ثبت را انتخاب می‌کند؛ این انتخاب برای اجراهای بعدی "
            f"حفظ می‌شود.\n{status}"
        )

    def _request_stop(self) -> None:
        if self.stop_callback is not None:
            self.stop_callback()

    @staticmethod
    def _draft_status_key(draft: DeclarationDraft) -> tuple[str, int]:
        source = str(draft.source_path.resolve()) if draft.source_path else ""
        return source, draft.source_recipient_ordinal

    def set_operation_drafts(
        self,
        drafts: tuple[DeclarationDraft, ...],
        *,
        reset: bool,
        tracking_codes: dict[tuple[str, int], str] | None = None,
        attachment_counts: dict[tuple[str, int], tuple[int, str]] | None = None,
    ) -> None:
        if reset:
            self.status_table.setRowCount(0)
            self._operation_rows.clear()
        known_codes = tracking_codes or {}
        known_attachment_counts = attachment_counts or {}
        for draft in drafts:
            key = self._draft_status_key(draft)
            row = self._operation_rows.get(key)
            if row is None:
                row = self.status_table.rowCount()
                self.status_table.insertRow(row)
                self._operation_rows[key] = row
            code = known_codes.get(key, "")
            name = " ".join(filter(None, (
                str(draft.fields.get("recipient_first_name", "")).strip(),
                str(draft.fields.get("recipient_last_name", "")).strip(),
            )))
            values = (
                str(row + 1),
                name or "نامشخص",
                draft.source_path.name if draft.source_path else "دستی",
                str(draft.fields.get("declaration_subject", "")),
                code,
                "success" if code else "pending",
                "pending" if draft.attachments else "skipped",
                "",
                "",
            )
            for column, value in enumerate(values):
                item = self.status_table.item(row, column) or QTableWidgetItem()
                self.status_table.setItem(row, column, item)
                if column == self.STATUS_HEADERS.index("تعداد ضمائم در ثنا"):
                    count_data = known_attachment_counts.get(key)
                    self._set_attachment_count_item(
                        item,
                        count_data[0] if count_data else None,
                        count_data[1] if count_data else "",
                    )
                elif column in (
                    self.STATUS_HEADERS.index("وضعیت اظهارنامه"),
                    self.STATUS_HEADERS.index("وضعیت پیوست"),
                ):
                    self._set_status_item(item, value)
                else:
                    item.setText(value)

    def update_operation_status(self, event: object) -> None:
        if not isinstance(event, dict):
            return
        key = (
            str(event.get("source_path", "")),
            int(event.get("source_recipient_ordinal", 1)),
        )
        row = self._operation_rows.get(key)
        if row is None:
            return
        stage = str(event.get("stage", ""))
        if stage == "attachment_count":
            count = event.get("attachment_count")
            if (
                isinstance(count, int)
                and not isinstance(count, bool)
                and count >= 0
            ):
                item = self.status_table.item(
                    row, self.STATUS_HEADERS.index("تعداد ضمائم در ثنا")
                )
                if item is None:
                    item = QTableWidgetItem()
                    self.status_table.setItem(
                        row,
                        self.STATUS_HEADERS.index("تعداد ضمائم در ثنا"),
                        item,
                    )
                self._set_attachment_count_item(
                    item,
                    count,
                    str(event.get("attachment_count_checked_at", "")),
                )
            return
        header = "وضعیت اظهارنامه" if stage == "petition" else "وضعیت پیوست"
        item = self.status_table.item(row, self.STATUS_HEADERS.index(header))
        if item is None:
            item = QTableWidgetItem()
            self.status_table.setItem(row, self.STATUS_HEADERS.index(header), item)
        state = str(event.get("state", "pending"))
        completed = int(event.get("completed", 0) or 0)
        total = int(event.get("total", 0) or 0)
        suffix = f" {completed} از {total}" if stage == "attachment" and total else ""
        self._set_status_item(item, state, suffix=suffix)
        tracking_code = str(event.get("tracking_code", "")).strip()
        if tracking_code:
            self.status_table.item(
                row, self.STATUS_HEADERS.index("کد رهگیری")
            ).setText(tracking_code)
        detail = str(event.get("message", "")).strip()
        self.status_table.item(row, self.STATUS_HEADERS.index("توضیح")).setText(
            detail
        )

    def _set_status_item(
        self, item: QTableWidgetItem, state: str, *, suffix: str = ""
    ) -> None:
        text, color = self.STATUS_DISPLAY.get(
            state, self.STATUS_DISPLAY["unknown"]
        )
        item.setText(f"{text}{suffix}")
        item.setForeground(QColor(color))
        item.setData(Qt.ItemDataRole.UserRole, state)

    @staticmethod
    def _set_attachment_count_item(
        item: QTableWidgetItem,
        count: int | None,
        checked_at: str,
    ) -> None:
        if count is None:
            item.setText("—")
            item.setForeground(QColor("#667085"))
            item.setToolTip("هنوز تعداد واقعی ضمائم از ثنا دریافت نشده است.")
            return
        item.setText(str(count))
        item.setForeground(QColor("#247A3D"))
        item.setToolTip(
            f"آخرین بررسی ثنا: {checked_at}"
            if checked_at else "تعداد واقعی تأییدشده توسط ثنا"
        )

    def _choose_api_template(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self,
            "انتخاب payload نمونه SetPetitionData",
            str(Path.home()),
            "JSON یا متن درخواست (*.json *.txt);;همه فایل‌ها (*.*)",
        )
        if not source:
            return
        try:
            template = load_setpetitiondata_template(Path(source))
            target = _setpetitiondata_template_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(template, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except (OSError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(
                self, "قالب نامعتبر", f"قالب ذخیره نشد: {exc}"
            )
            return
        self._refresh_api_template_status()
        QMessageBox.information(
            self,
            "قالب ذخیره شد",
            "قالب در تنظیمات محلی همین دستگاه ذخیره شد؛ اطلاعات مخاطب، شرح و "
            "کدهای مکان هنگام اجرا جایگزین می‌شوند.",
        )

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

    def _test_api_registration(self) -> None:
        if self.api_test_callback is None:
            QMessageBox.warning(
                self,
                "ثبت موقت در دسترس نیست",
                "مسیر ثبت موقت مقداردهی نشده است.",
            )
            return
        missing = any(
            not field.text().strip()
            for field in (self.national_code, self.password, self.legal_identity)
        )
        session_active = bool(
            self.api_session_active_callback
            and self.api_session_active_callback()
        )
        if missing or (not self.otp.text().strip() and not session_active):
            QMessageBox.warning(
                self,
                "اطلاعات ورود ناقص",
                "کد ملی/شناسه، رمز عبور و شناسه شخص حقوقی را کامل کنید. "
                "برای نشست تازه، OTP هم لازم است.",
            )
            return
        self._save_credentials()
        self.api_test_callback(self._credentials())

    def _attach_existing(self) -> None:
        if self.attach_existing_callback is None:
            return
        active = bool(
            self.api_session_active_callback
            and self.api_session_active_callback()
        )
        if (
            not active
            and not all((self.national_code.text().strip(), self.password.text(),
                         self.legal_identity.text().strip(), self.otp.text().strip()))
        ):
            QMessageBox.warning(
                self, "اطلاعات ورود ناقص",
                "برای ورود تازه، مشخصات ورود و OTP را کامل کنید.",
            )
            return
        self._save_credentials()
        self.attach_existing_callback(self._credentials())

    def _refresh_counts(self) -> None:
        if self.refresh_counts_callback is None:
            return
        active = bool(
            self.api_session_active_callback
            and self.api_session_active_callback()
        )
        if (
            not active
            and not all((self.national_code.text().strip(), self.password.text(),
                         self.legal_identity.text().strip(), self.otp.text().strip()))
        ):
            QMessageBox.warning(
                self, "اطلاعات ورود ناقص",
                "برای ورود تازه، مشخصات ورود و OTP را کامل کنید.",
            )
            return
        self._save_credentials()
        self.refresh_counts_callback(self._credentials())

    def _close_api_browser(self) -> None:
        if self.close_browser_callback is not None:
            self.close_browser_callback()

    def _exit_application(self) -> None:
        if self.exit_callback is not None:
            self.exit_callback()

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
        self.output_tabs.setTabText(1, "گزارش فنی")
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
        if level == "error":
            self.output_tabs.setTabText(1, "گزارش فنی — دارای خطا")
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
    otp_requested = Signal(object)
    final_confirmation_requested = Signal(object, object)
    operation_status_changed = Signal(object)

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
        self._api_browser_close_event: Event | None = None
        self._api_browser_ready = False
        self._api_request_queue: Queue | None = None
        self._api_drafts: list[DeclarationDraft] | None = None
        self._exit_requested = False
        # مسیر قدیمی فرم‌پرکن مرورگر عمداً از رابط جدا شده است. کدهای
        # run_live_upload_batch، _register_declarations و _finish_registration
        # فعلاً برای حذف مرحله‌ای نگه داشته شده‌اند و نباید به UI متصل شوند.
        # self.login_page.registration_callback = self._register_declarations
        self.login_page.stop_callback = self._request_registration_stop
        self.login_page.api_test_callback = self._start_api_test
        self.login_page.attach_existing_callback = self._start_attach_existing
        self.login_page.refresh_counts_callback = self._start_attachment_count_refresh
        self.login_page.api_session_active_callback = self._api_session_active
        self.login_page.close_browser_callback = self._close_api_browser
        self.login_page.exit_callback = self._request_application_exit
        self.files_page_id = self.addPage(self.files_page)
        self.review_page_id = self.addPage(self.review_page)
        self.preprocessing_page_id = self.addPage(self.preprocessing_page)
        self.content_page_id = self.addPage(self.content_page)
        self.login_page_id = self.addPage(self.login_page)
        self._loaded_paths: tuple[Path, ...] = ()
        self._preprocess_source: tuple[RecipientCandidate, ...] = ()
        self._lookup_dir = Path(__file__).resolve().parents[2] / "lookups"
        self._background_thread: QThread | None = None
        self._background_worker: _BackgroundWorker | None = None
        self._background_dialog: QProgressDialog | None = None
        self._background_success: Callable[[object], None] | None = None
        self._background_failure: Callable[[Exception, str], None] | None = None
        self._background_progress: Callable[[str], None] | None = None
        self._background_result: object | None = None
        self._background_error: tuple[Exception, str] | None = None
        self._file_extraction_thread: QThread | None = None
        self._file_extraction_worker: _BackgroundWorker | None = None
        self._file_extraction_dialog: QProgressDialog | None = None
        self._file_extraction_result: object | None = None
        self._file_extraction_error: tuple[Exception, str] | None = None
        self._file_extraction_succeeded: Callable[[object], None] | None = None
        self._file_extraction_failed: Callable[[Exception, str], None] | None = None
        self._registration_stop_event: Event | None = None
        self.currentIdChanged.connect(self._page_changed)
        self.otp_requested.connect(self._show_otp_prompt)
        self.final_confirmation_requested.connect(self._show_final_confirmation)
        self.operation_status_changed.connect(
            self.login_page.update_operation_status
        )
        self.setButtonLayout([
            QWizard.WizardButton.BackButton,
            QWizard.WizardButton.NextButton,
            QWizard.WizardButton.CancelButton,
            QWizard.WizardButton.Stretch,
        ])
        self.setButtonText(QWizard.WizardButton.BackButton, "قبلی")
        self.setButtonText(QWizard.WizardButton.NextButton, "بعدی")
        self.setButtonText(QWizard.WizardButton.CancelButton, "انصراف")
        self.button(QWizard.WizardButton.BackButton).setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowForward)
        )
        self.button(QWizard.WizardButton.NextButton).setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack)
        )
        self.button(QWizard.WizardButton.NextButton).setObjectName("PrimaryButton")
        finish_button = self.button(QWizard.WizardButton.FinishButton)
        finish_button.setVisible(False)

    def _start_background(
        self,
        label: str,
        task: Callable[[Callable[[str], None]], object],
        succeeded: Callable[[object], None],
        failed: Callable[[Exception, str], None],
        progressed: Callable[[str], None] | None = None,
        stop_event: Event | None = None,
    ) -> bool:
        if self._background_thread is not None:
            QMessageBox.information(
                self,
                "عملیات در حال اجرا",
                "لطفاً تا پایان عملیات جاری صبر کنید.",
            )
            return False

        dialog = QProgressDialog(label, "", 0, 0, self)
        dialog.setCancelButton(None)
        dialog.setWindowTitle("در حال پردازش")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)

        thread = QThread(self)
        worker = _BackgroundWorker(task)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._background_progressed)
        worker.succeeded.connect(self._background_succeeded)
        worker.failed.connect(self._background_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._background_thread_finished)

        self._background_thread = thread
        self._background_worker = worker
        self._background_dialog = dialog
        self._background_success = succeeded
        self._background_failure = failed
        self._background_progress = progressed
        self._background_result = None
        self._background_error = None
        self._registration_stop_event = stop_event
        self._set_background_controls_enabled(False)
        dialog.show()
        thread.start()
        return True

    @Slot(str)
    def _background_progressed(self, message: str) -> None:
        if message == "__api_browser_failed__":
            self._api_browser_ready = True
            self._api_request_queue = None
            self.preprocessing_page.refresh_registration_results()
            self.review_page.refresh_registration_results()
            self.files_page.refresh_registration_results()
            self.login_page.append_output(
                "درخواست ثبت موقت ناموفق شد؛ مرورگر برای بررسی باز می‌ماند. "
                "درخواست تازه تا بستن مرورگر پذیرفته نمی‌شود.",
                level="error",
            )
            self._finish_background_ui()
            self._set_api_session_controls_enabled(allow_requests=False)
            return
        if message == "__api_browser_ready_to_close__":
            self._api_browser_ready = True
            self.preprocessing_page.refresh_registration_results()
            self.review_page.refresh_registration_results()
            self.files_page.refresh_registration_results()
            self.login_page.append_output(
                "درخواست فعلی پایان یافت؛ نشست مرورگر باز و آمادهٔ درخواست بعدی است. "
                "برای بستن نشست «خروج از مرورگر» را بزنید.",
                level="progress",
            )
            self._finish_background_ui()
            self._set_api_session_controls_enabled()
            return
        if self._background_dialog is not None:
            self._background_dialog.setLabelText(message)
        if self._background_progress is not None:
            self._background_progress(message)

    @Slot(object)
    def _background_succeeded(self, result: object) -> None:
        self._background_result = result

    @Slot(object, str)
    def _background_failed(self, exc: Exception, details: str) -> None:
        self._background_error = (exc, details)

    @Slot()
    def _background_thread_finished(self) -> None:
        succeeded = self._background_success
        failed = self._background_failure
        result = self._background_result
        error = self._background_error
        self._registration_stop_event = None
        self._background_thread = None
        self._background_worker = None
        self._background_success = None
        self._background_failure = None
        self._background_progress = None
        self._background_result = None
        self._background_error = None
        self._api_browser_ready = False
        self._api_request_queue = None
        self._api_browser_close_event = None
        self._finish_background_ui()
        if self._exit_requested:
            self._api_drafts = None
            self.close()
            return
        if error is not None:
            if failed is not None:
                failed(*error)
        elif succeeded is not None:
            succeeded(result)
        self._api_drafts = None

    def _finish_background_ui(self) -> None:
        if self._background_dialog is not None:
            self._background_dialog.close()
            self._background_dialog.deleteLater()
            self._background_dialog = None
        self._set_background_controls_enabled(True)

    def _set_background_controls_enabled(self, enabled: bool) -> None:
        for role in (
            QWizard.WizardButton.BackButton,
            QWizard.WizardButton.NextButton,
            QWizard.WizardButton.FinishButton,
            QWizard.WizardButton.CancelButton,
        ):
            self.button(role).setEnabled(enabled)
        self.login_page.api_test_button.setEnabled(enabled)
        self.login_page.attach_existing_button.setEnabled(enabled)
        self.login_page.refresh_counts_button.setEnabled(enabled)
        close_enabled = enabled and self._api_browser_ready
        self.login_page.close_browser_button.setEnabled(close_enabled)
        self.login_page.close_browser_button.setVisible(close_enabled)
        stop_enabled = (
            not enabled and self._registration_stop_event is not None
            and not self._registration_stop_event.is_set()
        )
        self.login_page.stop_button.setEnabled(stop_enabled)
        self.login_page.stop_button.setVisible(stop_enabled)
        if enabled:
            self.login_page.stop_button.setText("توقف پس از ردیف جاری")

    def _set_api_session_controls_enabled(
        self, *, allow_requests: bool = True
    ) -> None:
        self.button(QWizard.WizardButton.BackButton).setEnabled(True)
        self.button(QWizard.WizardButton.NextButton).setEnabled(True)
        self.button(QWizard.WizardButton.FinishButton).setEnabled(False)
        self.button(QWizard.WizardButton.CancelButton).setEnabled(False)
        self.login_page.api_test_button.setEnabled(allow_requests)
        self.login_page.attach_existing_button.setEnabled(allow_requests)
        self.login_page.refresh_counts_button.setEnabled(allow_requests)
        self.login_page.close_browser_button.setEnabled(True)
        self.login_page.close_browser_button.setVisible(True)
        self.login_page.stop_button.setEnabled(False)
        self.login_page.stop_button.setVisible(False)

    def _request_registration_stop(self) -> None:
        event = self._registration_stop_event
        if event is None or event.is_set():
            return
        event.set()
        self.login_page.stop_button.setEnabled(False)
        self.login_page.stop_button.setText("در حال توقف...")
        self.login_page.append_output(
            "درخواست توقف ثبت شد؛ اظهارنامهٔ جاری کامل می‌شود و ردیف بعدی آغاز نخواهد شد.",
            level="warning",
        )

    def _api_session_active(self) -> bool:
        return bool(
            self._api_request_queue is not None
            and self._api_browser_close_event is not None
            and not self._api_browser_close_event.is_set()
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._background_thread is not None:
            QMessageBox.information(
                self,
                "عملیات در حال اجرا",
                "برای جلوگیری از نیمه‌کاره ماندن عملیات، تا پایان آن صبر کنید.",
            )
            event.ignore()
            return
        super().closeEvent(event)

    def _request_application_exit(self) -> None:
        self._exit_requested = True
        if self._api_browser_close_event is not None:
            self._api_browser_close_event.set()
        if self._registration_stop_event is not None:
            self._registration_stop_event.set()
        self.setEnabled(False)
        if (
            self._background_thread is None
            and self._file_extraction_thread is None
        ):
            self.close()

    @Slot(object)
    def _show_otp_prompt(self, response: Queue) -> None:
        otp, accepted = QInputDialog.getText(
            self,
            "ورود مجدد به ثنا",
            "نشست پایان یافته است. OTP جدید را وارد کنید:",
            QLineEdit.EchoMode.Password,
        )
        value = otp.strip() if accepted else ""
        if value:
            self.login_page.otp.setText(value)
            self.login_page._save_credentials()
        response.put(value or None)

    @Slot(object, object)
    def _show_final_confirmation(
        self,
        draft: DeclarationDraft,
        response: Queue,
    ) -> None:
        name = " ".join(
            value for value in (
                draft.fields.get("recipient_first_name", ""),
                draft.fields.get("recipient_last_name", ""),
            ) if value
        )
        answer = QMessageBox.question(
            self,
            "تأیید امضا و ثبت نهایی",
            f"اظهارنامه مخاطب «{name}» آماده امضا است.\n"
            "این اقدام نهایی و غیرقابل‌برگشت است. ثبت انجام شود؟",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        response.put(answer == QMessageBox.StandardButton.Yes)

    def _start_api_test(self, credentials: LoginCredentials) -> None:
        try:
            petition_template = load_setpetitiondata_template(
                _setpetitiondata_template_path()
            )
        except (OSError, ValueError, RuntimeError) as exc:
            message = (
                "قالب فنی ثبت نامعتبر است؛ از بخش تنظیمات پیشرفته قالب صحیح "
                "را دوباره انتخاب کنید."
                if str(exc) == "set_petition_data_template_invalid"
                else str(exc)
            )
            QMessageBox.warning(self, "قالب فنی ثبت آماده نیست", message)
            return
        candidates = self.preprocessing_page.candidates()
        if not candidates:
            QMessageBox.warning(self, "مخاطب وجود ندارد", "حداقل یک ردیف لازم است.")
            return
        try:
            drafts = _build_desktop_drafts(
                candidates,
                self.content_page.payloads(),
                shared=False,
                lookup_dir=self._lookup_dir,
            )
        except (ValueError, LookupResolutionError) as exc:
            QMessageBox.warning(self, "پیش‌نویس آزمایشی نامعتبر", str(exc))
            return
        source_counts: dict[Path, int] = {}
        for draft in drafts:
            if draft.source_path is not None:
                source = draft.source_path.resolve()
                source_counts[source] = source_counts.get(source, 0) + 1
        eligible_drafts = []
        legacy_drafts = []
        for draft in drafts:
            if (
                draft.fields.get("person_type", "").strip()
                not in _SUPPORTED_API_PERSON_TYPES
            ):
                continue
            if draft.source_path is not None:
                try:
                    saved = read_saved_result(
                        draft.source_dir,
                        draft.source_path,
                        source_recipient_ordinal=draft.source_recipient_ordinal,
                        source_recipient_count=source_counts[
                            draft.source_path.resolve()
                        ],
                    )
                except ResultPersistenceError as exc:
                    if str(exc) == (
                        "legacy_results_ambiguous_for_multiple_recipients"
                    ):
                        legacy_drafts.append(draft)
                    continue
                tracking_code = str(
                    (saved or {}).get("tracking_code", "")
                ).strip()
                if tracking_code and tracking_code != "رسید نامعتبر":
                    continue
            eligible_drafts.append(draft)
        drafts = tuple(eligible_drafts)
        legacy_drafts = tuple(legacy_drafts)
        operation_drafts = drafts + legacy_drafts
        if not operation_drafts:
            return
        answer = QMessageBox.question(
            self,
            "تأیید ثبت موقت و پیوست",
            f"برای حداکثر {len(operation_drafts)} ردیف، SetPetitionData به‌ترتیب "
            "و هر ردیف "
            "فقط یک‌بار فراخوانی می‌شود. پس از دریافت کد رهگیری، پیوست‌های "
            "همان ردیف به همان اظهارنامه افزوده می‌شوند. در خطا یا پاسخ مبهم، "
            "ادامه متوقف می‌شود؛ پیش از اقدام دوباره باید وضعیت بررسی شود. "
            "سوابق قدیمی ابتدا از ثنا تطبیق داده می‌شوند و دوباره ثبت نخواهند "
            "شد. امضای نهایی انجام نمی‌شود.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        request_text_path = _setpetitiondata_request_text_path()
        self.login_page.set_operation_drafts(
            operation_drafts,
            reset=self._api_request_queue is None,
        )
        if self._api_request_queue is not None:
            if (
                self._api_browser_close_event is None
                or self._api_browser_close_event.is_set()
            ):
                self.login_page.append_output(
                    "مرورگر در حال بسته‌شدن است؛ پس از پایان بستن، درخواست را دوباره بفرستید.",
                    level="warning",
                )
                return
            self._api_browser_ready = False
            self._set_background_controls_enabled(False)
            if self._api_drafts is not None:
                self._api_drafts.extend(operation_drafts)
            self._api_request_queue.put((
                drafts,
                credentials,
                petition_template,
                request_text_path,
                self._lookup_dir,
                legacy_drafts,
            ))
            self.login_page.append_output(
                f"درخواست {len(operation_drafts)} ردیفی به نشست فعلی صف شد؛ ورود دوباره "
                "فقط در صورت پایان نشست لازم می‌شود.",
                level="system",
            )
            return
        self.login_page.start_log()
        close_browser_event = Event()
        request_queue: Queue = Queue()
        self._api_drafts = list(operation_drafts)
        self._api_browser_close_event = close_browser_event
        self._api_request_queue = request_queue
        self._api_browser_ready = False
        self.login_page.close_browser_button.setEnabled(False)
        self.login_page.close_browser_button.setVisible(False)
        report = self.login_page.append_output
        report(
            f"بررسی و ثبت موقت برای {len(operation_drafts)} ردیف آغاز شد؛ "
            "پیوست هر ردیف پس از دریافت کد رهگیری به همان اظهارنامه افزوده "
            "می‌شود؛ امضای نهایی اجرا نمی‌شود.",
            level="system",
        )

        def task(progress: Callable[[str], None]) -> object:
            progress("در حال راه‌اندازی مرورگر برای آزمایش یک‌موردی...")

            def reauth_credentials_provider(
                current: LoginCredentials,
            ) -> LoginCredentials | None:
                response: Queue = Queue(maxsize=1)
                self.otp_requested.emit(response)
                otp = response.get()
                return replace(current, otp=otp) if otp else None

            return run_live_setpetitiondata_batch(
                drafts,
                credentials,
                lambda: True,
                petition_template,
                request_text_path,
                lookup_dir=self._lookup_dir,
                progress=progress,
                status=lambda event: self.operation_status_changed.emit(event),
                pending_requests=request_queue,
                reauth_credentials_provider=reauth_credentials_provider,
                close_browser_event=close_browser_event,
                legacy_registration=legacy_drafts,
            )

        def succeeded(result: object) -> None:
            results = tuple(result)
            if not results:
                report("هیچ ردیفی ثبت نشد.", level="warning")
                return
            session_drafts = self._api_drafts or list(drafts)
            attachment_count = sum(len(draft.attachments) for draft in session_drafts)
            report(
                f"ثبت موقت و پیوست تمام شد؛ موفق: {len(results)} ردیف؛ "
                f"پیوست تأییدشده: {attachment_count} فایل.",
                level="success",
            )
            QMessageBox.information(
                self,
                "ثبت موقت و پیوست موفق",
                f"{len(results)} ردیف با موفقیت ثبت شد و {attachment_count} "
                "پیوست تأیید شد. کد رهگیری و جزئیات مراحل در لاگ نمایش داده "
                "شده است. امضای نهایی انجام نشده است.",
            )

        def failed(exc: Exception, details: str) -> None:
            report(f"ثبت موقت متوقف شد: {exc}", level="error")
            report("ردیابی کامل خطا:", level="debug")
            report(details, level="debug")
            report(
                f"متن درخواست: {_setpetitiondata_request_text_path()}\n"
                f"دستور curl: "
                f"{_setpetitiondata_request_text_path().with_name('setpetitiondata-request.curl.txt')}\n"
                f"کد کنسول: "
                f"{_setpetitiondata_request_text_path().with_name('setpetitiondata-request.console.txt')}",
                level="debug",
            )
            message = (
                "قالب فنی ثبت با ساختار موردنیاز سازگار نیست؛ قالب صحیح را "
                "از بخش تنظیمات پیشرفته دوباره انتخاب کنید."
                if str(exc) == "set_petition_data_template_invalid"
                else str(exc)
            )
            QMessageBox.warning(self, "ثبت موقت ناموفق", message)

        self._start_background(
            "ثبت موقت اظهارنامه‌ها...",
            task,
            succeeded,
            failed,
            lambda message: report(message, level="progress"),
        )

    def _close_api_browser(self) -> None:
        if self._api_browser_close_event is None:
            return
        self._api_browser_close_event.set()
        self.login_page.close_browser_button.setEnabled(False)
        self.login_page.close_browser_button.setVisible(False)
        self.login_page.api_test_button.setEnabled(False)
        self.login_page.attach_existing_button.setEnabled(False)
        self.login_page.refresh_counts_button.setEnabled(False)
        self.login_page.append_output(
            "درخواست بستن مرورگر ثبت شد؛ تا بسته‌شدن آن درخواست تازه پذیرفته نمی‌شود.",
            level="system",
        )

    def _start_attachment_count_refresh(
        self, credentials: LoginCredentials
    ) -> None:
        candidates = self.preprocessing_page.candidates()
        if not candidates:
            QMessageBox.warning(self, "مخاطب وجود ندارد", "حداقل یک ردیف لازم است.")
            return
        try:
            drafts = _build_desktop_drafts(
                candidates, self.content_page.payloads(), shared=False,
                lookup_dir=self._lookup_dir,
            )
        except (ValueError, LookupResolutionError) as exc:
            QMessageBox.warning(self, "پیش‌نویس نامعتبر", str(exc))
            return

        source_counts: dict[Path, int] = {}
        for draft in drafts:
            if draft.source_path is not None:
                source = draft.source_path.resolve()
                source_counts[source] = source_counts.get(source, 0) + 1
        requests = []
        skipped = 0
        for draft in drafts:
            if draft.source_path is None:
                skipped += 1
                continue
            try:
                saved = read_saved_result(
                    draft.source_dir,
                    draft.source_path,
                    source_recipient_ordinal=draft.source_recipient_ordinal,
                    source_recipient_count=source_counts[
                        draft.source_path.resolve()
                    ],
                )
            except ResultPersistenceError:
                skipped += 1
                continue
            tracking_code = str(
                (saved or {}).get("tracking_code", "")
            ).strip()
            if not tracking_code or tracking_code == "رسید نامعتبر":
                skipped += 1
                continue
            requests.append((draft, tracking_code))

        if skipped:
            self.login_page.append_output(
                f"{skipped} ردیف بدون کد رهگیری قطعی از بازخوانی کنار گذاشته شد.",
                level="system",
            )
        if not requests:
            self.login_page.append_output(
                "هیچ ردیف دارای کد رهگیری قطعی برای بازخوانی پیدا نشد.",
                level="warning",
            )
            return

        request_tuple = tuple(requests)
        tracking_codes = {
            self.login_page._draft_status_key(draft): tracking_code
            for draft, tracking_code in request_tuple
        }
        self.login_page.set_operation_drafts(
            tuple(draft for draft, _tracking_code in request_tuple),
            reset=self._api_request_queue is None,
            tracking_codes=tracking_codes,
        )
        if self._api_request_queue is not None:
            if (
                self._api_browser_close_event is None
                or self._api_browser_close_event.is_set()
            ):
                self.login_page.append_output(
                    "مرورگر در حال بسته‌شدن است؛ پس از بسته‌شدن بازخوانی را دوباره بزنید.",
                    level="warning",
                )
                return
            self._api_browser_ready = False
            self._set_background_controls_enabled(False)
            self._api_request_queue.put({
                "kind": "refresh_counts",
                "requests": request_tuple,
                "credentials": credentials,
            })
            self.login_page.append_output(
                f"بازخوانی خواندنی تعداد ضمائم {len(request_tuple)} اظهارنامه "
                "به نشست فعلی صف شد.",
                level="system",
            )
            return

        self.login_page.start_log()
        close_browser_event = Event()
        request_queue: Queue = Queue()
        self._api_drafts = [draft for draft, _code in request_tuple]
        self._api_browser_close_event = close_browser_event
        self._api_request_queue = request_queue
        self._api_browser_ready = False
        self.login_page.close_browser_button.setEnabled(False)
        self.login_page.close_browser_button.setVisible(False)
        report = self.login_page.append_output
        report(
            f"بازخوانی خواندنی تعداد ضمائم {len(request_tuple)} اظهارنامه آغاز شد؛ "
            "هیچ ثبت یا آپلودی انجام نمی‌شود.",
            level="system",
        )

        def task(progress: Callable[[str], None]) -> object:
            def reauth_credentials_provider(
                current: LoginCredentials,
            ) -> LoginCredentials | None:
                response: Queue = Queue(maxsize=1)
                self.otp_requested.emit(response)
                otp = response.get()
                return replace(current, otp=otp) if otp else None

            return run_live_setpetitiondata_batch(
                (), credentials, lambda: True, {},
                _setpetitiondata_request_text_path(),
                lookup_dir=self._lookup_dir,
                progress=progress,
                status=lambda event: self.operation_status_changed.emit(event),
                pending_requests=request_queue,
                reauth_credentials_provider=reauth_credentials_provider,
                close_browser_event=close_browser_event,
                count_refresh=request_tuple,
            )

        def succeeded(_result: object) -> None:
            report(
                "بازخوانی تعداد ضمائم پایان یافت؛ نتیجه هر ردیف در جدول و JSON به‌روز شد.",
                level="success",
            )

        def failed(exc: Exception, details: str) -> None:
            report(f"بازخوانی تعداد ضمائم متوقف شد: {exc}", level="error")
            report(details, level="debug")
            QMessageBox.warning(self, "بازخوانی ناموفق", str(exc))

        self._start_background(
            "در حال بازخوانی تعداد ضمائم...",
            task, succeeded, failed,
            lambda message: report(message, level="progress"),
        )

    def _start_attach_existing(self, credentials: LoginCredentials) -> None:
        candidates = self.preprocessing_page.candidates()
        if not candidates:
            QMessageBox.warning(self, "مخاطب وجود ندارد", "حداقل یک ردیف لازم است.")
            return
        try:
            drafts = _build_desktop_drafts(
                candidates, self.content_page.payloads(), shared=False,
                lookup_dir=self._lookup_dir,
            )
        except (ValueError, LookupResolutionError) as exc:
            QMessageBox.warning(self, "پیش‌نویس پیوست نامعتبر", str(exc))
            return
        source_counts: dict[Path, int] = {}
        for draft in drafts:
            if draft.source_path is not None:
                source = draft.source_path.resolve()
                source_counts[source] = source_counts.get(source, 0) + 1
        drafts = tuple(
            draft for draft in drafts
            if draft.fields.get("person_type", "").strip()
            in _SUPPORTED_API_PERSON_TYPES
        )
        if not drafts:
            return
        requests = []
        legacy_drafts = []
        saved_attachment_counts: dict[tuple[str, int], tuple[int, str]] = {}
        skipped_without_tracking = 0
        for draft in drafts:
            if not draft.attachments or draft.source_path is None:
                continue
            try:
                saved = read_saved_result(
                    draft.source_dir,
                    draft.source_path,
                    source_recipient_ordinal=draft.source_recipient_ordinal,
                    source_recipient_count=source_counts[
                        draft.source_path.resolve()
                    ],
                )
            except ResultPersistenceError as exc:
                if str(exc) == (
                    "legacy_results_ambiguous_for_multiple_recipients"
                ):
                    legacy_drafts.append(draft)
                else:
                    skipped_without_tracking += 1
                continue
            tracking_code = str(
                (saved or {}).get("tracking_code", "")
            ).strip()
            if tracking_code and tracking_code != "رسید نامعتبر":
                requests.append((draft, tracking_code))
                saved_count = _saved_attachment_count(saved)
                if saved_count is not None:
                    saved_attachment_counts[
                        self.login_page._draft_status_key(draft)
                    ] = saved_count
            else:
                skipped_without_tracking += 1
        if skipped_without_tracking:
            self.login_page.append_output(
                f"{skipped_without_tracking} ردیف بدون کد رهگیری قطعی "
                "به‌صورت خودکار کنار گذاشته شد.",
                level="system",
            )
        if not requests and not legacy_drafts:
            return
        attachment_count = sum(len(draft.attachments) for draft, _code in requests)
        attachment_count += sum(len(draft.attachments) for draft in legacy_drafts)
        operation_count = len(requests) + len(legacy_drafts)
        answer = QMessageBox.question(
            self,
            "تأیید افزودن پیوست",
            f"برای حداکثر {operation_count} اظهارنامه، {attachment_count} پیوست "
            "افزوده می‌شود. سوابق قدیمی چندمخاطبی ابتدا با اطلاعات ثنا تطبیق "
            "داده می‌شوند؛ SetPetitionData و ثبت دوباره اجرا نمی‌شود. "
            "در صورت خطا یا پاسخ مبهم، اجرا متوقف می‌شود.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        request_tuple = tuple(requests)
        legacy_tuple = tuple(legacy_drafts)
        tracking_codes = {
            self.login_page._draft_status_key(draft): tracking_code
            for draft, tracking_code in request_tuple
        }
        self.login_page.set_operation_drafts(
            tuple(draft for draft, _tracking_code in request_tuple) + legacy_tuple,
            reset=self._api_request_queue is None,
            tracking_codes=tracking_codes,
            attachment_counts=saved_attachment_counts,
        )
        if self._api_request_queue is not None:
            if (
                self._api_browser_close_event is None
                or self._api_browser_close_event.is_set()
            ):
                self.login_page.append_output(
                    "مرورگر در حال بسته‌شدن است؛ پس از بسته‌شدن درخواست را دوباره بفرستید.",
                    level="warning",
                )
                return
            self._api_browser_ready = False
            self._set_background_controls_enabled(False)
            self._api_request_queue.put({
                "kind": "attach_existing",
                "requests": request_tuple,
                "legacy_drafts": legacy_tuple,
                "credentials": credentials,
            })
            self.login_page.append_output(
                f"درخواست افزودن {attachment_count} پیوست به نشست فعلی صف شد؛ "
                "اظهارنامه دوباره ثبت نمی‌شود.",
                level="system",
            )
            return

        self.login_page.start_log()
        close_browser_event = Event()
        request_queue: Queue = Queue()
        self._api_drafts = (
            [draft for draft, _code in request_tuple] + list(legacy_tuple)
        )
        self._api_browser_close_event = close_browser_event
        self._api_request_queue = request_queue
        self._api_browser_ready = False
        self.login_page.close_browser_button.setEnabled(False)
        self.login_page.close_browser_button.setVisible(False)
        report = self.login_page.append_output
        report(
            f"افزودن حداکثر {attachment_count} پیوست به {operation_count} اظهارنامه آغاز شد؛ "
            "ثبت مجدد اظهارنامه اجرا نمی‌شود.",
            level="system",
        )

        def task(progress: Callable[[str], None]) -> object:
            def reauth_credentials_provider(
                current: LoginCredentials,
            ) -> LoginCredentials | None:
                response: Queue = Queue(maxsize=1)
                self.otp_requested.emit(response)
                otp = response.get()
                return replace(current, otp=otp) if otp else None

            return run_live_setpetitiondata_batch(
                (), credentials, lambda: True, {},
                _setpetitiondata_request_text_path(),
                lookup_dir=self._lookup_dir,
                progress=progress,
                status=lambda event: self.operation_status_changed.emit(event),
                pending_requests=request_queue,
                reauth_credentials_provider=reauth_credentials_provider,
                close_browser_event=close_browser_event,
                attachment_only=request_tuple,
                legacy_attachment_only=legacy_tuple,
            )

        def succeeded(_result: object) -> None:
            report(
                "پیوست‌گذاری پایان یافت؛ نتیجه دقیق هر ردیف و کد رهگیری "
                "تطبیق‌داده‌شده در جدول و لاگ آمده است.",
                level="success",
            )

        def failed(exc: Exception, details: str) -> None:
            report(f"افزودن پیوست متوقف شد: {exc}", level="error")
            report("ردیابی کامل خطا:", level="debug")
            report(details, level="debug")
            QMessageBox.warning(self, "افزودن پیوست ناموفق", str(exc))

        self._start_background(
            "افزودن پیوست به اظهارنامه‌های ثبت‌شده...",
            task, succeeded, failed,
            lambda message: report(message, level="progress"),
        )

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

        prior_results = existing_results(drafts)
        if prior_results:
            details = "\n".join(
                f"{name}: کد رهگیری {result['tracking_code'] or 'نامشخص'}، "
                f"تاریخ ثبت {_registration_date_display(result['registered_at'])}"
                for name, result in prior_results
            )
            message = (
                "برای این فایل‌های Word قبلاً کد رهگیری ثبت شده است. اگر ادامه دهید، "
                "دوباره در سامانه ثبت می‌شوند؛ ردیف‌های ناخواسته را از ورودی حذف کنید.\n\n"
                + details
            )
            report(message, level="warning")
            QMessageBox.warning(self, "احتمال ثبت تکراری", message)

        report(f"تعداد اظهارنامه‌های آماده: {len(drafts)}", level="system")

        answer = QMessageBox.question(
            self,
            f"تأیید بارگذاری {len(drafts)} اظهارنامه",
            f"قرار است {len(drafts)} اظهارنامه بارگذاری و در سامانه ثبت شود. "
            "ادامه می‌دهید؟\n"
            "پیش از امضای نهایی هر اظهارنامه، تأیید جداگانه گرفته می‌شود.\n"
            "در صورت خطای مبهم، اجرای گروه متوقف و همان مورد تکرار نمی‌شود.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            report("عملیات توسط کاربر لغو شد.", level="warning")
            return False

        stop_event = Event()

        def task(progress: Callable[[str], None]) -> object:
            first_login = True
            current_credentials = credentials

            def credentials_provider() -> LoginCredentials | None:
                nonlocal first_login, current_credentials
                if first_login:
                    first_login = False
                    return current_credentials
                response: Queue = Queue(maxsize=1)
                self.otp_requested.emit(response)
                otp = response.get()
                if not otp:
                    return None
                current_credentials = replace(current_credentials, otp=otp)
                return current_credentials

            def final_confirmation(draft: DeclarationDraft) -> bool:
                response: Queue = Queue(maxsize=1)
                self.final_confirmation_requested.emit(draft, response)
                return bool(response.get())

            progress("در حال راه‌اندازی مرورگر...")
            return run_live_upload_batch(
                drafts,
                credentials_provider,
                final_confirmation,
                progress=progress,
                stop_event=stop_event,
            )

        def succeeded(results: object) -> None:
            completed = self._finish_registration(drafts, tuple(results))
            if completed:
                self.accept()

        def failed(exc: Exception, details: str) -> None:
            report(f"ثبت متوقف شد: {exc}", level="error")
            report(details, level="debug")
            QMessageBox.critical(self, "ثبت متوقف شد", str(exc))

        report("شروع عملیات مرورگر...", level="system")
        self._start_background(
            "در حال راه‌اندازی مرورگر...",
            task,
            succeeded,
            failed,
            lambda message: report(message, level="progress"),
            stop_event=stop_event,
        )
        return False

    def _finish_registration(
        self,
        drafts: tuple[DeclarationDraft, ...],
        results: tuple[object, ...],
    ) -> bool:
        report = self.login_page.append_output
        successful = 0
        failures = []
        stopped = 0
        for index, draft in enumerate(drafts, start=1):
            if index > len(results):
                stopped += 1
                continue
            result = results[index - 1]
            if result.status == "stopped":
                stopped += 1
                report(f"ردیف {index}: با درخواست کاربر آغاز نشد.", level="warning")
                continue
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
                    filename=RESULTS_FILENAME,
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

        if failures or stopped:
            self.preprocessing_page.refresh_registration_results()
            self.review_page.refresh_registration_results()
            self.files_page.refresh_registration_results()
            QMessageBox.warning(
                self,
                (
                    "پردازش متوقف شد"
                    if stopped and not failures
                    else "پردازش با خطا پایان یافت"
                ),
                f"موفق: {successful}\nناموفق: {len(failures)}\nمتوقف/آغازنشده: {stopped}\n\n"
                + "\n".join(failures),
            )
            report(
                f"پردازش پایان یافت. موفق: {successful}، ناموفق: {len(failures)}، "
                f"متوقف/آغازنشده: {stopped}",
                level="warning",
            )
            return successful > 0 and stopped == 0

        self.preprocessing_page.refresh_registration_results()
        self.review_page.refresh_registration_results()
        self.files_page.refresh_registration_results()
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
            self.files_page.refresh_registration_results()
            return
        if page_id == self.login_page_id:
            if self._api_session_active() or self._api_browser_ready:
                return
            try:
                drafts = _build_desktop_drafts(
                    self.preprocessing_page.candidates(),
                    self.content_page.payloads(),
                    shared=False,
                    lookup_dir=self._lookup_dir,
                )
            except (ValueError, LookupResolutionError):
                self.login_page.set_operation_drafts((), reset=True)
            else:
                source_counts: dict[Path, int] = {}
                for draft in drafts:
                    if draft.source_path is not None:
                        source = draft.source_path.resolve()
                        source_counts[source] = source_counts.get(source, 0) + 1
                tracking_codes = {}
                attachment_counts = {}
                for draft in drafts:
                    if draft.source_path is None:
                        continue
                    try:
                        saved = read_saved_result(
                            draft.source_dir,
                            draft.source_path,
                            source_recipient_ordinal=draft.source_recipient_ordinal,
                            source_recipient_count=source_counts[
                                draft.source_path.resolve()
                            ],
                        )
                    except ResultPersistenceError:
                        continue
                    tracking_code = str(
                        (saved or {}).get("tracking_code", "")
                    ).strip()
                    if tracking_code and tracking_code != "رسید نامعتبر":
                        key = self.login_page._draft_status_key(draft)
                        tracking_codes[key] = tracking_code
                        saved_count = _saved_attachment_count(saved)
                        if saved_count is not None:
                            attachment_counts[key] = saved_count
                self.login_page.set_operation_drafts(
                    drafts,
                    reset=True,
                    tracking_codes=tracking_codes,
                    attachment_counts=attachment_counts,
                )
            return
        if page_id == self.preprocessing_page_id:
            source = self.review_page.candidates()
            self.preprocessing_page.refresh_registration_results()
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
        self.review_page.refresh_registration_results()
        if selected_paths == self._loaded_paths:
            return

        def task(progress: Callable[[str], None]) -> object:
            progress(
                f"در حال خواندن و استخراج اطلاعات {len(selected_paths)} فایل..."
            )
            return extract_recipients(list(selected_paths))

        def succeeded(candidates: object) -> None:
            self.review_page.set_candidates(tuple(candidates))
            self._loaded_paths = selected_paths

        def failed(exc: Exception, _details: str) -> None:
            self.review_page.set_candidates(())
            QMessageBox.warning(self, "خطا در استخراج", str(exc))

        if (
            self._background_thread is not None
            and (self._api_session_active() or self._api_browser_ready)
        ):
            self._start_session_extraction(task, succeeded, failed)
        else:
            self._start_background(
                "در حال بارگذاری و استخراج فایل‌ها...",
                task,
                succeeded,
                failed,
            )

    def _start_session_extraction(
        self,
        task: Callable[[Callable[[str], None]], object],
        succeeded: Callable[[object], None],
        failed: Callable[[Exception, str], None],
    ) -> None:
        if self._file_extraction_thread is not None:
            return
        dialog = QProgressDialog("در حال خواندن و استخراج فایل‌های جدید...", "", 0, 0, self)
        dialog.setCancelButton(None)
        dialog.setWindowTitle("استخراج فایل‌ها")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        thread = QThread(self)
        worker = _BackgroundWorker(task)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._session_extraction_succeeded)
        worker.failed.connect(self._session_extraction_failed)
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._session_extraction_thread_finished)
        self._file_extraction_thread = thread
        self._file_extraction_worker = worker
        self._file_extraction_dialog = dialog
        self._file_extraction_result = None
        self._file_extraction_error = None
        self._file_extraction_succeeded = succeeded
        self._file_extraction_failed = failed
        dialog.show()
        thread.start()

    @Slot(object)
    def _session_extraction_succeeded(self, result: object) -> None:
        self._file_extraction_result = result

    @Slot(object, str)
    def _session_extraction_failed(self, exc: Exception, details: str) -> None:
        self._file_extraction_error = (exc, details)

    @Slot()
    def _session_extraction_thread_finished(self) -> None:
        result, error = self._file_extraction_result, self._file_extraction_error
        succeeded = self._file_extraction_succeeded
        failed = self._file_extraction_failed
        self._file_extraction_thread = None
        self._file_extraction_worker = None
        self._file_extraction_result = None
        self._file_extraction_error = None
        self._file_extraction_succeeded = None
        self._file_extraction_failed = None
        if self._file_extraction_dialog is not None:
            self._file_extraction_dialog.close()
            self._file_extraction_dialog.deleteLater()
            self._file_extraction_dialog = None
        if self._exit_requested:
            self.close()
            return
        if error is not None:
            if failed is not None:
                failed(*error)
        else:
            if succeeded is not None:
                succeeded(result)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("SANA Declaration Uploader")
    wizard = RecipientWizard()
    wizard.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
