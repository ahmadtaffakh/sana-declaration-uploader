"""Local, review-first extraction of recipient candidates from documents."""

from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import re
import unicodedata

from docx import Document

from .models import RecipientCandidate
from .pdf_party_table import extract_pdf_party_groups


class DocumentExtractionError(ValueError):
    """Raised when a source document cannot be read safely."""


_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_ARABIC_TO_PERSIAN = str.maketrans(
    {"ي": "ی", "ى": "ی", "ك": "ک", "ھ": "ه"}
)
_LABEL_VALUE = r"\s*[:：\-]?\s*"
_ROLE_LABELS = (
    ("declarant", "اظهارکننده"),
    ("addressee", "مخاطبان"),
    ("addressee", "مخاطبین"),
    ("addressee", "مخاطب"),
    ("plaintiff", "خواهان"),
    ("defendant", "خواندگان"),
    ("defendant", "خوانده"),
    ("appellant", "تجدیدنظرخواه"),
    ("respondent", "تجدیدنظرخوانده"),
    ("complainant", "شاکی"),
    ("accused", "مشتکی عنه"),
    ("accused", "متهم"),
    ("petitioner", "درخواست کننده"),
    ("claimant", "محکوم له"),
    ("respondent", "محکوم علیه"),
)
_ROLE_RE = re.compile(
    r"\b(?:" + "|".join(
        rf"(?P<r{index}>{re.escape(label).replace(r'\ ', r'[\s‌]*')})"
        for index, (_role, label) in enumerate(_ROLE_LABELS)
    ) + r")\b"
)
_NAME_STOP_RE = re.compile(
    r"\b(?:فرزند|(?:به|با)\s*(?:نشانی|آدرس|وکالت|نمایندگی)|وکیل|نشانی|آدرس|کد\s*ملی|شماره\s*ملی)\b"
)
_NAME_SPLIT_RE = re.compile(r"[،,؛]|(?:\s+و\s+)")
_SECTION_STOP_RE = re.compile(
    r"^\s*[:：\-.ـ|]*\s*(?:\d+\s*)?(?:خواسته|خواسته\s*ها|گردشکار|موضوع|رأی|رای)\b"
)
_BLOCK_PARTY_DETAIL_RE = re.compile(
    r"\b(?:فرزند|نشانی|آدرس|استان|شهرستان)\b"
)
_LEGAL_PERSON_MARKERS = (
    "شرکت", "سازمان", "وزارت", "اداره", "بانک", "مؤسسه", "شهرداری"
)
_DOCUMENT_NUMBER_LENGTH = r"[0-9]{8,30}"


def extract_recipients(paths: list[Path]) -> tuple[RecipientCandidate, ...]:
    """Extract all reviewable candidates from each source without external calls."""

    if not paths:
        raise DocumentExtractionError("source_files_missing")
    documents = [_extract_document(path) for path in paths]
    declaration_numbers: dict[str, set[str]] = {}
    groups_with_parties = {
        document[1]
        for document in documents
        if document[1] and any(candidate.full_name for candidate in document[0])
    }
    for _candidates, notice_number, declaration_number in documents:
        if notice_number and declaration_number:
            declaration_numbers.setdefault(notice_number, set()).add(declaration_number)

    result = []
    for candidates, notice_number, declaration_number in documents:
        group_numbers = declaration_numbers.get(notice_number, set())
        propagated = next(iter(group_numbers)) if len(group_numbers) == 1 else ""
        ambiguous = len(group_numbers) > 1
        for candidate in candidates:
            if (
                not candidate.full_name
                and notice_number in groups_with_parties
                and "party_table_unparsed" not in candidate.issues
            ):
                continue
            issues = candidate.issues
            if ambiguous and "related_declaration_ambiguous" not in issues:
                issues = (*issues, "related_declaration_ambiguous")
            result.append(
                replace(
                    candidate,
                    notice_number=notice_number,
                    related_declaration_number=(
                        candidate.related_declaration_number or propagated
                    ),
                    issues=issues,
                )
            )
    return _merge_unique_notice_duplicates(tuple(result))


def extract_recipient(path: Path) -> RecipientCandidate:
    """Keep the original single-candidate API for structured documents."""

    return extract_recipient_candidates(path)[0]


def extract_recipient_candidates(path: Path) -> tuple[RecipientCandidate, ...]:
    return _extract_document(path)[0]


def _extract_document(
    path: Path,
) -> tuple[tuple[RecipientCandidate, ...], str, str]:
    source = path.resolve()
    if not source.is_file():
        raise DocumentExtractionError(f"source_file_missing:{path.name}")

    text = _read_text(source)
    normalized = _normalize_text(text)
    notice_number = _document_number(normalized, source, "ابلاغیه")
    declaration_number = _document_number(normalized, source, "اظهارنامه")
    table_parties = _pdf_table_parties(source) if source.suffix.lower() == ".pdf" else ()
    parties = tuple(
        replace(
            candidate,
            notice_number=notice_number,
            related_declaration_number=declaration_number,
        )
        for candidate in (table_parties or _extract_parties(normalized, source))
    )
    if parties:
        return parties, notice_number, declaration_number

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

    candidates = (RecipientCandidate(
        source_path=source,
        full_name=full_name,
        national_id=national_id,
        employee_code=employee_code,
        birth_date=birth_date,
        postal_code=postal_code,
        address=address,
        notice_number=notice_number,
        related_declaration_number=declaration_number,
        issues=tuple(issues),
    ),)
    return candidates, notice_number, declaration_number


def _pdf_table_parties(source: Path) -> tuple[RecipientCandidate, ...]:
    candidates = []
    for group in extract_pdf_party_groups(source):
        names = tuple(str(name) for name in group["names"] if str(name).strip())
        group_issues = tuple(str(issue) for issue in group.get("issues", ()))
        if not names:
            if group_issues:
                candidates.append(
                    RecipientCandidate(
                        source_path=source,
                        role=str(group["role"]),
                        person_type=str(group["person_type"]),
                        evidence=str(group["evidence"]),
                        extraction_method="local_pdf_table",
                        issues=group_issues,
                    )
                )
            continue
        addresses = tuple(
            str(address) for address in group["addresses"] if str(address).strip()
        )
        national_ids = tuple(
            str(value) for value in group.get("national_ids", ()) if str(value).strip()
        )
        grouped = len(names) > 1
        issues = group_issues
        if grouped and "grouped_parties_requires_review" not in issues:
            issues = (*issues, "grouped_parties_requires_review")
        candidates.append(
            RecipientCandidate(
                source_path=source,
                full_name="، ".join(names),
                national_id=national_ids[0] if len(national_ids) == 1 else "",
                address=" | ".join(addresses),
                role=str(group["role"]),
                person_type=str(group["person_type"]),
                evidence=str(group["evidence"]),
                extraction_method="local_pdf_table",
                issues=issues,
            )
        )
    return tuple(candidates)


def _merge_unique_notice_duplicates(
    candidates: tuple[RecipientCandidate, ...],
) -> tuple[RecipientCandidate, ...]:
    """Enrich one uniquely matched party across files in the same notice."""

    merged = list(candidates)
    removed: set[int] = set()
    for index, candidate in enumerate(merged):
        if index in removed or not candidate.notice_number or not candidate.full_name:
            continue
        key = _name_key(candidate.full_name)
        matching_indexes = [
            position
            for position, item in enumerate(merged)
            if position not in removed
            and position != index
            and item.notice_number == candidate.notice_number
            and item.source_path != candidate.source_path
            and _name_key(item.full_name) == key
            and not {candidate.role, item.role}.isdisjoint({"addressee"})
        ]
        if len(matching_indexes) != 1:
            continue
        other_index = matching_indexes[0]
        if other_index < index:
            continue
        other = merged[other_index]
        primary_index, detail_index = (
            (other_index, index)
            if candidate.role == "addressee" and other.role != "addressee"
            else (index, other_index)
        )
        primary = merged[primary_index]
        detail = merged[detail_index]
        identity_available = any(
            (
                primary.national_id or detail.national_id,
                primary.employee_code or detail.employee_code,
                primary.address or detail.address,
                primary.postal_code or detail.postal_code,
            )
        )
        issues = tuple(dict.fromkeys((*primary.issues, *detail.issues)))
        if identity_available:
            issues = tuple(
                issue for issue in issues if issue != "identity_details_missing"
            )
        merged[primary_index] = replace(
            primary,
            national_id=primary.national_id or detail.national_id,
            employee_code=primary.employee_code or detail.employee_code,
            birth_date=primary.birth_date or detail.birth_date,
            postal_code=primary.postal_code or detail.postal_code,
            address=primary.address or detail.address,
            evidence=primary.evidence or detail.evidence,
            issues=issues,
        )
        removed.add(detail_index)
    return tuple(item for index, item in enumerate(merged) if index not in removed)


def _name_key(value: str) -> str:
    normalized = " ".join(_normalize_text(value).split()).casefold()
    for marker in _LEGAL_PERSON_MARKERS:
        prefix = marker.casefold() + " "
        if normalized.startswith(prefix):
            return normalized[len(prefix):]
    return normalized


def _normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_DIGITS).translate(
        _ARABIC_TO_PERSIAN
    )


def _document_number(text: str, source: Path, kind: str) -> str:
    escaped_kind = re.escape(kind)
    for pattern in (
        rf"(?:شماره|کد)\s*{escaped_kind}{_LABEL_VALUE}({_DOCUMENT_NUMBER_LENGTH})",
        rf"{escaped_kind}(?:\s*مرتبط)?\s*(?:به\s*)?(?:شماره|کد){_LABEL_VALUE}({_DOCUMENT_NUMBER_LENGTH})",
    ):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    if kind == "ابلاغیه":
        for value in (source.name, source.parent.name):
            match = re.search(r"(?:^|__|_)([0-9]{18})(?=__|_|$)", value)
            if match:
                return match.group(1)
    return ""


def _extract_parties(text: str, source: Path) -> tuple[RecipientCandidate, ...]:
    candidates = []
    seen = set()
    lines = text.splitlines()
    for line_index, line in enumerate(lines):
        match = _ROLE_RE.search(line)
        if not match or line[:match.start()].strip(" \t:：-ـ.|۱١"):
            continue
        role_index = int(match.lastgroup[1:])
        role = _ROLE_LABELS[role_index][0]
        party_lines = [(line[match.end():], True)]
        if not party_lines[0][0].strip(" \t:：-ـ.|"):
            party_lines = []
            for block_line in lines[line_index + 1:]:
                if _SECTION_STOP_RE.search(block_line) or _header_role_match(block_line):
                    break
                if _BLOCK_PARTY_DETAIL_RE.search(block_line):
                    party_lines.append((block_line, False))
        for party_line, allow_split in party_lines:
            stop = _NAME_STOP_RE.search(party_line)
            names_text = party_line[:stop.start()] if stop else party_line
            raw_names = _NAME_SPLIT_RE.split(names_text) if allow_split else [names_text]
            for raw_name in raw_names:
                name = _clean_party_name(raw_name)
                key = (role, name.casefold())
                if not _plausible_party_name(name) or key in seen:
                    continue
                seen.add(key)
                candidates.append(_party_candidate(source, role, name, party_line))
    return tuple(candidates)


def _header_role_match(line: str) -> bool:
    match = _ROLE_RE.search(line)
    return bool(
        match
        and not line[:match.start()].strip(" \t:：-ـ.|۱١")
    )


def _party_candidate(
    source: Path, role: str, name: str, evidence: str
) -> RecipientCandidate:
    evidence = " ".join(evidence.strip().split())[:700]
    national_id = _find_digits(evidence, r"(?:کد|شماره)\s*ملی", 10)
    employee_code = _find_digits(
        evidence, r"(?:کد|شماره)\s*پرسنلی", minimum=1, maximum=20
    )
    postal_code = _find_digits(evidence, r"کد\s*پستی", 10)
    address = _find_inline_value(evidence, r"(?:نشانی|آدرس)", maximum=500)
    issues = []
    if national_id and not _valid_national_id(national_id):
        issues.append("national_id_invalid")
    if not any((national_id, employee_code, address, postal_code)):
        issues.append("identity_details_missing")
    return RecipientCandidate(
        source_path=source,
        full_name=name,
        national_id=national_id,
        employee_code=employee_code,
        postal_code=postal_code,
        address=address,
        role=role,
        person_type=_person_type(name),
        evidence=evidence,
        extraction_method="local_party",
        issues=tuple(issues),
    )


def _clean_party_name(value: str) -> str:
    value = re.sub(r"^\s*(?:\d+\s*[.\-)ـ]\s*)?", "", value)
    value = re.sub(r"^\s*(?:آقای|خانم)\s+", "", value)
    return " ".join(value.strip(" \t\r\n:：-ـ.،,؛/|").split())


def _plausible_party_name(value: str) -> bool:
    words = value.split()
    return (
        2 <= len(value) <= 200
        and 1 <= len(words) <= 12
        and any(len(word) >= 2 and word.isalpha() for word in words)
        and not any(token in value for token in ("در خصوص", "به طرفیت", "خواسته"))
    )


def _find_inline_value(text: str, label: str, *, maximum: int) -> str:
    match = re.search(
        rf"{label}{_LABEL_VALUE}(.{{1,{maximum}}})", text, flags=re.IGNORECASE
    )
    return " ".join(match.group(1).strip(" |؛,").split()) if match else ""


def _person_type(name: str) -> str:
    if any(marker in name for marker in _LEGAL_PERSON_MARKERS):
        return "legal"
    return "unknown"


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
