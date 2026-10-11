"""Local, review-first extraction of recipient candidates from documents."""

from __future__ import annotations

from pathlib import Path
import re

from docx import Document

from .models import RecipientCandidate


class DocumentExtractionError(ValueError):
    """Raised when a source document cannot be read safely."""


_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_LABEL_VALUE = r"\s*[:：\-]?\s*"


def extract_recipients(paths: list[Path]) -> tuple[RecipientCandidate, ...]:
    """Extract one reviewable candidate per source file without external calls."""

    if not paths:
        raise DocumentExtractionError("source_files_missing")
    return tuple(extract_recipient(path) for path in paths)


def extract_recipient(path: Path) -> RecipientCandidate:
    source = path.resolve()
    if not source.is_file():
        raise DocumentExtractionError(f"source_file_missing:{path.name}")

    text = _read_text(source)
    normalized = text.translate(_DIGITS)
    full_name = _find_value(
        normalized,
        r"(?:نام\s+و\s+نام\s+خانوادگی|نام\s+مخاطب|نام\s+شخص)",
    )
    if not full_name:
        first_name = _find_value(
            normalized, r"نام(?!\s+خانوادگی)(?![\w‌])"
        )
        last_name = _find_value(normalized, r"نام\s+خانوادگی")
        full_name = " ".join(value for value in (first_name, last_name) if value)

    national_id = _find_digits(normalized, r"(?:کد|شماره)\s*ملی", 10)
    employee_code = _find_digits(
        normalized, r"(?:کد|شماره)\s*پرسنلی", minimum=1, maximum=20
    )
    birth_date = _find_value(normalized, r"تاریخ\s*تولد")
    postal_code = _find_digits(normalized, r"کد\s*پستی", 10)
    address = _find_value(normalized, r"(?:نشانی|آدرس)", maximum=500)

    issues = []
    if not full_name:
        issues.append("name_missing")
    if national_id and not _valid_national_id(national_id):
        issues.append("national_id_invalid")
    if not any((national_id, employee_code, address, postal_code)):
        issues.append("identity_details_missing")

    return RecipientCandidate(
        source_path=source,
        full_name=full_name,
        national_id=national_id,
        employee_code=employee_code,
        birth_date=birth_date,
        postal_code=postal_code,
        address=address,
        issues=tuple(issues),
    )


def _read_text(path: Path) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            document = Document(path)
            paragraphs = [paragraph.text for paragraph in document.paragraphs]
            table_rows = []
            for table in document.tables:
                for row in table.rows:
                    values = [cell.text.strip() for cell in row.cells]
                    if len(values) >= 2:
                        table_rows.append(f"{values[0]}: {values[1]}")
                    elif values:
                        table_rows.append(values[0])
            return "\n".join((*paragraphs, *table_rows))
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise DocumentExtractionError("pdf_support_not_installed") from exc
            return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
        if suffix == ".txt":
            return path.read_text(encoding="utf-8")
    except DocumentExtractionError:
        raise
    except Exception as exc:
        raise DocumentExtractionError(f"source_file_invalid:{path.name}") from exc
    raise DocumentExtractionError(f"source_type_unsupported:{path.suffix.lower()}")


def _find_value(text: str, label: str, *, maximum: int = 120) -> str:
    match = re.search(
        rf"(?:^|\n)\s*{label}{_LABEL_VALUE}([^\n\r]{{1,{maximum}}})",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    value = re.split(
        r"\s+(?:کد|شماره|تاریخ|نشانی|آدرس)\s*[^\s]*\s*[:：]",
        match.group(1),
        maxsplit=1,
    )[0]
    return " ".join(value.strip(" |؛,").split())


def _find_digits(
    text: str,
    label: str,
    length: int | None = None,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> str:
    lower = length if length is not None else minimum
    upper = length if length is not None else maximum
    assert lower is not None and upper is not None
    match = re.search(
        rf"{label}{_LABEL_VALUE}([0-9]{{{lower},{upper}}})",
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _valid_national_id(value: str) -> bool:
    if not re.fullmatch(r"\d{10}", value) or len(set(value)) == 1:
        return False
    checksum = sum(int(value[index]) * (10 - index) for index in range(9)) % 11
    expected = checksum if checksum < 2 else 11 - checksum
    return int(value[-1]) == expected
