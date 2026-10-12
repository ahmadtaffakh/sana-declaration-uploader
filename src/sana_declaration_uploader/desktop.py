"""Review-first desktop interface with an explicit live-registration boundary."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
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
)

from .extraction import DocumentExtractionError, extract_recipients
from .live_browser import (
    LiveBrowserError,
    LoginCredentials,
    run_live_upload_batch,
    run_login_check,
)
from .lookups import LookupResolutionError, resolve_lookup_fields
from .models import Attachment, DeclarationDraft, RecipientCandidate
from .preprocessing import preprocess_candidates
from .results import ResultPersistenceError, save_verified_result


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
_GENDER_DISPLAY = ("", "مرد", "زن")
_ATTACHMENT_DEFAULTS = {
    "attachment_kind": "سایر دلایل و مستندات",
    "attachment_number": "شماره مدرک",
    "attachment_date": "۱۴۰۵/۰۷/۲۰",
}
_DECLARATION_DEFAULTS = {
    "subject": "سایر موضوعات",
    "description": "شرح موضوع",
}
_DEFAULT_LOCATION = {
    "province": "استان تهران",
    "county": "شهرستان تهران",
    "city": "تهران",
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


def _build_desktop_drafts(
    candidates: tuple[RecipientCandidate, ...],
    payloads: dict[int, dict[str, object]],
    *,
    shared: bool,
    lookup_dir: Path,
) -> tuple[DeclarationDraft, ...]:
    drafts = []
    for index, candidate in enumerate(candidates):
        payload = payloads.get(-1 if shared else index, {})
        default_location = _DEFAULT_LOCATION if candidate.national_id else {}
        lookup_fields = resolve_lookup_fields(
            {
                "job_title": candidate.job_title,
                "province": candidate.province or default_location.get("province", ""),
                "county": candidate.county or default_location.get("county", ""),
                "city": candidate.city or default_location.get("city", ""),
                "court_unit_name": candidate.court_unit_name,
                "court_unit_code": candidate.court_unit_code,
            },
            lookup_dir,
        )
        fields = {
            **lookup_fields,
            "recipient_id": f"desktop-{index + 1}",
            "data_source": "manual",
            "person_type": _PERSON_TYPE_DISPLAY.get(
                candidate.person_type, candidate.person_type
            ),
            "recipient_national_id": candidate.national_id,
            "recipient_first_name": candidate.first_name,
            "recipient_last_name": candidate.last_name,
            "recipient_father_name": candidate.father_name,
            "recipient_birth_date": candidate.birth_date,
            "recipient_gender": candidate.gender,
            "recipient_main_street": candidate.address or " ",
            "recipient_address": candidate.address,
            "recipient_postal_code": candidate.postal_code,
            "notice_number": candidate.notice_number,
            "related_declaration_number": candidate.related_declaration_number,
            "related_declaration_date": candidate.related_declaration_date,
            "declaration_subject": str(payload.get("subject", "")),
            "declaration_text": str(payload.get("description", "")),
        }
        attachments = tuple(
            Attachment(
                path=Path(path),
                title=Path(path).stem,
                kind=str(payload.get("attachment_kind", "")),
                document_number=str(payload.get("attachment_number", "")),
                document_date=str(payload.get("attachment_date", "")),
            )
            for path in payload.get("attachments", ())
        )
        drafts.append(DeclarationDraft(
            source_dir=candidate.source_path.resolve().parent,
            fields=fields,
            attachments=attachments,
        ))
    return tuple(drafts)


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
        "انتخاب",
        "فایل منبع",
        "شماره ابلاغیه",
        "شماره اظهارنامه مرتبط",
        "تاریخ اظهارنامه مرتبط",
        "نقش",
        "نوع شخص",
        "نام و نام خانوادگی",
        "نام",
        "نام خانوادگی",
        "نام پدر",
        "جنسیت",
        "شغل",
        "کد ملی",
        "کد پرسنلی",
        "تاریخ تولد",
        "کدپستی",
        "آدرس",
        "شاهد استخراج",
        "وضعیت",
        "حذف",
    )

    def __init__(self) -> None:
        super().__init__()
        self.setTitle("<b>بازبینی و تکمیل مشخصات اشخاص</b>")
        self.setSubTitle(
            "خروجی استخراج قطعی تلقی نمی‌شود؛ هر ردیف را با سند اصلی تطبیق دهید."
        )

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        _configure_table(self.table)

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
            "روش کار: ۱) نام و نقش هر شخص را کنترل کنید. ۲) خانه‌های خالی یا اشتباه "
            "را مستقیم در جدول اصلاح کنید. ۳) اگر یک ردیف به پرونده شما مربوط نیست، "
            "تیک آن را بزنید یا از کلید حذف همان ردیف استفاده کنید. وضعیت «نیازمند "
            "اصلاح» یعنی دست‌کم یک مقدار "
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
        suggested_first, suggested_last = _split_full_name(
            candidate.full_name if candidate else ""
        )
        values = (
            "",
            candidate.source_path.name if candidate else "دستی",
            candidate.notice_number if candidate else "",
            candidate.related_declaration_number if candidate else "",
            candidate.related_declaration_date if candidate else "",
            _ROLE_DISPLAY.get(candidate.role, candidate.role) if candidate else "",
            _PERSON_TYPE_DISPLAY.get(candidate.person_type, candidate.person_type) if candidate else "",
            candidate.full_name if candidate else "",
            (candidate.first_name or suggested_first) if candidate else "",
            (candidate.last_name or suggested_last) if candidate else "",
            candidate.father_name if candidate else "",
            candidate.gender if candidate else "",
            candidate.job_title if candidate else "",
            candidate.national_id if candidate else "",
            candidate.employee_code if candidate else "",
            candidate.birth_date if candidate else "",
            candidate.postal_code if candidate else "",
            candidate.address if candidate else "",
            candidate.evidence if candidate else "",
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
            if column == 1:
                item.setData(Qt.ItemDataRole.UserRole, candidate)
            if column in (
                1,
                len(self.HEADERS) - 3,
                len(self.HEADERS) - 2,
                len(self.HEADERS) - 1,
            ):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, column, item)
        remove_button = QPushButton("حذف")
        remove_button.clicked.connect(
            lambda _checked=False, marker=self.table.item(row, 0):
            self.table.removeRow(marker.row())
        )
        self.table.setCellWidget(row, len(self.HEADERS) - 1, remove_button)

    def candidates(self) -> tuple[RecipientCandidate, ...]:
        result = []
        for row in range(self.table.rowCount()):
            original = self.table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            if not isinstance(original, RecipientCandidate):
                original = RecipientCandidate(source_path=Path("manual"))

            def value(header: str) -> str:
                item = self.table.item(row, self.HEADERS.index(header))
                return item.text().strip() if item else ""

            updated = replace(
                original,
                notice_number=value("شماره ابلاغیه"),
                related_declaration_number=value("شماره اظهارنامه مرتبط"),
                related_declaration_date=value("تاریخ اظهارنامه مرتبط"),
                role=_ROLE_VALUE.get(value("نقش"), value("نقش")),
                person_type=_PERSON_TYPE_VALUE.get(
                    value("نوع شخص"), value("نوع شخص")
                ),
                full_name=value("نام و نام خانوادگی"),
                first_name=value("نام"),
                last_name=value("نام خانوادگی"),
                father_name=value("نام پدر"),
                gender=value("جنسیت"),
                job_title=value("شغل"),
                national_id=value("کد ملی"),
                employee_code=value("کد پرسنلی"),
                birth_date=value("تاریخ تولد"),
                postal_code=value("کدپستی"),
                address=value("آدرس"),
            )
            self.table.item(row, 1).setData(Qt.ItemDataRole.UserRole, updated)
            result.append(updated)
        return tuple(result)

    def _remove_selected(self) -> None:
        rows = {
            row for row in range(self.table.rowCount())
            if self.table.item(row, 0).checkState() == Qt.CheckState.Checked
        }
        rows.update(index.row() for index in self.table.selectedIndexes())
        rows = sorted(rows, reverse=True)
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
        _configure_table(self.table)

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

    def candidates(self) -> tuple[RecipientCandidate, ...]:
        result = []
        for row in range(self.table.rowCount()):
            original = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if not isinstance(original, RecipientCandidate):
                continue

            def value(header: str) -> str:
                item = self.table.item(row, self.HEADERS.index(header))
                return item.text().strip() if item else ""

            updated = replace(
                original,
                job_title=value("شغل"),
                job_code=value("کد شغل"),
                province=value("استان"),
                province_code=value("کد استان"),
                county=value("شهرستان"),
                county_code=value("کد شهرستان"),
                city=value("شهر"),
                city_code=value("کد شهر"),
                court_unit_name=value("واحد قضایی"),
                court_unit_code=value("کد واحد قضایی"),
            )
            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, updated)
            result.append(updated)
        return tuple(result)

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
        self.setTitle("<b>شرح اظهارنامه و فایل‌های پیوست</b>")
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
            "شرح و فایل‌های پیوست برای همهٔ اشخاص مشترک است"
        )
        self.shared_checkbox.setChecked(True)
        self.recipient_combo = QComboBox()
        self.recipient_combo.setEnabled(False)
        self.subject = QLineEdit(self._declaration_defaults["subject"])
        self.subject.setPlaceholderText("موضوع دقیق اظهارنامه را وارد کنید")
        self.description = QTextEdit()
        self.description.setPlainText(self._declaration_defaults["description"])
        self.description.setPlaceholderText(
            "شرح اظهارنامه را کامل و دقیق وارد کنید. این متن قبل از ثبت قابل بازبینی است."
        )
        self.description.setMinimumHeight(160)
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
        form = QFormLayout(group)
        form.addRow("دامنهٔ محتوا:", self.recipient_combo)
        form.addRow("موضوع اظهارنامه:", self.subject)
        form.addRow("شرح اظهارنامه:", self.description)
        form.addRow("فایل‌های پیوست:", self.attachment_list)
        form.addRow("نوع پیوست‌ها:", self.attachment_kind)
        form.addRow("شماره پیوست‌ها:", self.attachment_number)
        form.addRow("تاریخ پیوست‌ها:", self.attachment_date)
        form.addRow("", attachment_buttons)

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
        layout.addWidget(group)

        self.shared_checkbox.toggled.connect(self._scope_changed)
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
            use_shared = len(shared_content) <= 1
            self.shared_checkbox.blockSignals(True)
            self.shared_checkbox.setChecked(use_shared)
            self.shared_checkbox.blockSignals(False)
            if seeded:
                if use_shared:
                    self._payloads[-1] = self._copy_payload(seeded[0])
                else:
                    self._payloads.update(
                        (index, self._copy_payload(payload))
                        for index, payload in enumerate(seeded)
                    )
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

    def _scope_changed(self, shared: bool) -> None:
        self._save_active()
        previous = self._payloads.get(self._active_key, self._empty_payload())
        if shared:
            self._payloads.setdefault(-1, self._copy_payload(previous))
            self._active_key = -1
        else:
            shared_payload = self._payloads.get(-1, previous)
            for index in range(len(self._candidates)):
                self._payloads.setdefault(index, self._copy_payload(shared_payload))
            self._active_key = 0
        self.recipient_combo.setEnabled(not shared)
        self._rebuild_recipient_combo()

    def _rebuild_recipient_combo(self) -> None:
        self.recipient_combo.blockSignals(True)
        self.recipient_combo.clear()
        if self.shared_checkbox.isChecked():
            self.recipient_combo.addItem("همهٔ اشخاص", -1)
            self._active_key = -1
        else:
            for index, candidate in enumerate(self._candidates):
                self.recipient_combo.addItem(candidate.full_name or f"ردیف {index + 1}", index)
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
        self._payloads[self._active_key] = {
            "subject": self.subject.text().strip(),
            "description": self.description.toPlainText().strip(),
            "attachments": tuple(self._attachment_paths()),
            "attachment_kind": self.attachment_kind.text().strip(),
            "attachment_number": self.attachment_number.text().strip(),
            "attachment_date": self.attachment_date.text().strip(),
        }

    def _load_active(self) -> None:
        payload = self._payloads.get(self._active_key, self._empty_payload())
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
        keys = (-1,) if self.shared_checkbox.isChecked() else range(len(self._candidates))
        for key in keys:
            payload = payloads.get(key, {})
            label = "همه اشخاص" if key == -1 else f"ردیف {key + 1}"
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
    def __init__(self) -> None:
        super().__init__()
        self.registration_callback = None
        self.setTitle("<b>ورود امن به سامانه ثنا</b>")
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

        layout = QVBoxLayout(self)
        layout.addWidget(guidance)
        layout.addWidget(form_group)
        layout.addLayout(test_row)
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
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            run_login_check(self._credentials())
        except LiveBrowserError as exc:
            QMessageBox.warning(self, "ورود ناموفق", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
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
        candidates = self.preprocessing_page.candidates()
        if not candidates:
            QMessageBox.warning(self, "مخاطب وجود ندارد", "حداقل یک ردیف لازم است.")
            return False
        try:
            drafts = _build_desktop_drafts(
                candidates,
                self.content_page.payloads(),
                shared=self.content_page.shared_checkbox.isChecked(),
                lookup_dir=self._lookup_dir,
            )
        except LookupResolutionError as exc:
            QMessageBox.warning(self, "کدهای ثنا ناقص", str(exc))
            return False

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
            results = run_live_upload_batch(
                drafts,
                credentials_provider,
                final_confirmation,
            )
            for index, (draft, result) in enumerate(zip(drafts, results), start=1):
                save_verified_result(
                    draft,
                    result,
                    filename=f"result-{index:03d}.json",
                )
        except (LiveBrowserError, ResultPersistenceError, ValueError) as exc:
            QMessageBox.critical(self, "ثبت متوقف شد", str(exc))
            return False

        QMessageBox.information(
            self,
            "ثبت کامل شد",
            f"تعداد {len(results)} اظهارنامه با کد رهگیری معتبر ثبت شد.",
        )
        return True

    def _page_changed(self, page_id: int) -> None:
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
