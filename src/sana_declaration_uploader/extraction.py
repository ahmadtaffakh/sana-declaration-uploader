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
_DECLARATION_DATE = r"[0-9]{1,4}/[0-9]{1,2}/[0-9]{1,4}"
_ADDRESS_SEPARATOR = r"[-‐‑‒–—،,]"
_ADDRESS_DETAIL_PREFIXES = (
    "خیابان", "خ ", "کوچه", "شهرک", "کوی", "بیمارستان", "ملک شهر",
    "چهار راه", "چهارراه", "دفتر", "بلوار", "میدان", "محله", "جاده",
    "روستا", "پلاک",
)
_DEFAULT_LOCATION = {
    "province": "استان تهران",
    "county": "شهرستان تهران",
    "city": "تهران",
}


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
    return _number_source_recipients(
        _merge_unique_notice_duplicates(tuple(result))
    )


def extract_recipient(path: Path) -> RecipientCandidate:
    """Keep the original single-candidate API for structured documents."""

    return extract_recipient_candidates(path)[0]


def extract_recipient_candidates(path: Path) -> tuple[RecipientCandidate, ...]:
    return _number_source_recipients(_extract_document(path)[0])


def _number_source_recipients(
    candidates: tuple[RecipientCandidate, ...],
) -> tuple[RecipientCandidate, ...]:
    counts: dict[Path, int] = {}
    numbered = []
    for candidate in candidates:
        source = candidate.source_path.resolve()
        counts[source] = counts.get(source, 0) + 1
        numbered.append(
            replace(candidate, source_recipient_ordinal=counts[source])
        )
    return tuple(numbered)


def _extract_document(
    path: Path,
) -> tuple[tuple[RecipientCandidate, ...], str, str]:
    source = path.resolve()
    if not source.is_file():
        raise DocumentExtractionError(f"source_file_missing:{path.name}")

    document = _load_docx(source) if source.suffix.lower() == ".docx" else None
    text = _read_text(source, document=document)
    normalized = _normalize_text(text)
    notice_number = _document_number(normalized, source, "ابلاغیه")
    declaration_number = _document_number(normalized, source, "اظهارنامه")
    if document is not None:
        table_parties = _docx_table_parties(document, source)
    elif source.suffix.lower() == ".pdf":
        table_parties = _pdf_table_parties(source)
    else:
        table_parties = ()
    parties = tuple(
        replace(
            candidate,
            notice_number=notice_number,
            related_declaration_number=(
                candidate.related_declaration_number or declaration_number
            ),
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
    identity_no = _find_digits(normalized, r"شماره\s*شناسنامه", minimum=1, maximum=20)
    employee_code = _find_digits(
        normalized, r"(?:کد|شماره)\s*پرسنلی", minimum=1, maximum=20
    )
    birth_date = _find_value(normalized, r"تاریخ\s*تولد")
    postal_code = _find_digits(normalized, r"کد\s*پستی", 10)
    address = _find_value(normalized, r"(?:نشانی|آدرس)", maximum=500)
    job_title = _find_value(normalized, r"شغل")

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
        identity_no=identity_no,
        employee_code=employee_code,
        birth_date=birth_date,
        postal_code=postal_code,
        address=address,
        job_title=job_title,
        notice_number=notice_number,
        related_declaration_number=declaration_number,
        issues=tuple(issues),
    ),)
    return candidates, notice_number, declaration_number


def _load_docx(source: Path) -> object:
    try:
        return Document(source)
    except Exception as exc:
        raise DocumentExtractionError(f"source_file_invalid:{source.name}") from exc


def _docx_table_parties(document: object, source: Path) -> tuple[RecipientCandidate, ...]:
    candidates = []
    descriptions = []
    for table in document.tables:
        rows = [
            tuple(_normalize_cell(cell.text) for cell in row.cells)
            for row in table.rows
        ]
        candidates.extend(_tabular_addressees(rows, source))
        candidates.extend(_labeled_addressee(rows, source))
        descriptions.extend(_table_descriptions(rows))

    if not candidates:
        return ()
    description = max(descriptions, key=len, default="")
    return tuple(
        replace(
            candidate,
            declaration_subject=candidate.declaration_subject or "سایر موضوعات",
            declaration_text=candidate.declaration_text or description,
        )
        for candidate in candidates
    )


def _tabular_addressees(
    rows: list[tuple[str, ...]], source: Path
) -> tuple[RecipientCandidate, ...]:
    for header_index, row in enumerate(rows):
        columns = _recipient_columns(row)
        if columns is None:
            continue
        candidates = []
        for values in rows[header_index + 1:]:
            name = values[columns["name"]].strip()
            reference = values[columns["reference"]].strip()
            if not name or not re.search(_DOCUMENT_NUMBER_LENGTH, reference):
                continue
            candidates.append(_table_candidate(
                source,
                name=name,
                national_id=values[columns["national_id"]].strip(),
                address=values[columns["address"]].strip(),
                reference=reference,
            ))
        return tuple(candidates)
    return ()


def _recipient_columns(row: tuple[str, ...]) -> dict[str, int] | None:
    columns = {}
    for index, value in enumerate(row):
        if "شماره" in value and "اظهارنامه" in value:
            columns["reference"] = index
        elif "نام" in value and "نام خانوادگی" in value:
            columns["name"] = index
        elif "کد ملی" in value:
            columns["national_id"] = index
        elif value == "آدرس" or value == "نشانی":
            columns["address"] = index
    required = {"reference", "name", "national_id", "address"}
    return columns if required.issubset(columns) else None


def _labeled_addressee(
    rows: list[tuple[str, ...]], source: Path
) -> tuple[RecipientCandidate, ...]:
    for header_index, row in enumerate(rows[:-1]):
        recipient_index = next(
            (index for index, value in enumerate(row) if "اقامتگاه مخاطب" in value),
            None,
        )
        reference_index = next(
            (
                index for index, value in enumerate(row)
                if value == "موضوع اظهارنامه" or "شماره و تاریخ اظهارنامه" in value
            ),
            None,
        )
        if recipient_index is None or reference_index is None:
            continue
        values = rows[header_index + 1]
        if len(values) <= max(recipient_index, reference_index):
            return ()
        recipient = values[recipient_index]
        reference = values[reference_index]
        if not recipient or not re.search(_DOCUMENT_NUMBER_LENGTH, reference):
            return ()
        name, address = _split_labeled_recipient(recipient)
        national_id = _find_digits(recipient, r"(?:کد|شماره)\s*ملی", 10)
        return (_table_candidate(
            source,
            name=name,
            national_id=national_id,
            address=address,
            reference=reference,
        ),)
    return ()


def _split_labeled_recipient(value: str) -> tuple[str, str]:
    match = re.search(r"[،,]\s*(?=(?:استان|شهرستان|شهر|خیابان|نشانی|آدرس)\b)", value)
    if not match:
        return value, ""
    return value[:match.start()].strip(), value[match.start() + 1:].strip()


def _table_candidate(
    source: Path,
    *,
    name: str,
    national_id: str,
    address: str,
    reference: str,
) -> RecipientCandidate:
    full_name, father_name = _split_father_name(_clean_party_name(name))
    person_type = _person_type(full_name)
    first_name, last_name = _split_natural_name(full_name, person_type)
    national_id = national_id if re.fullmatch(r"[0-9]{10}", national_id) else ""
    declaration_number, declaration_date = _split_declaration_reference(reference)
    locations = _address_locations(address)
    if not all(locations.get(field) for field in ("province", "county", "city")):
        locations = dict(_DEFAULT_LOCATION)
    issues = []
    if person_type != "legal" and (not first_name or not last_name):
        issues.append("name_parts_review_required")
    if national_id and not _valid_national_id(national_id):
        issues.append("national_id_invalid")
    if not national_id:
        issues.append("national_id_missing")
    return RecipientCandidate(
        source_path=source,
        full_name=full_name,
        first_name=first_name,
        last_name=last_name,
        father_name=father_name,
        national_id=national_id,
        address=address,
        province=locations.get("province", ""),
        county=locations.get("county", ""),
        city=locations.get("city", ""),
        related_declaration_number=declaration_number,
        related_declaration_date=declaration_date,
        role="addressee",
        person_type=person_type if person_type == "legal" else "natural",
        evidence=" | ".join(value for value in (reference, name, national_id, address) if value),
        extraction_method="local_docx_table",
        issues=tuple(issues),
    )


def _split_father_name(value: str) -> tuple[str, str]:
    match = re.search(r"\s+فرزند\s+(.+)$", value)
    if not match:
        return value.strip(), ""
    father_name = match.group(1).strip()
    detail = re.search(
        r"\s+(?:(?:به|با)\s+)?(?:کد\s*ملی|شماره\s*ملی|نشانی|آدرس)\b",
        father_name,
    )
    if detail:
        father_name = father_name[:detail.start()].strip()
    return value[:match.start()].strip(), father_name


def _split_natural_name(value: str, person_type: str) -> tuple[str, str]:
    if person_type == "legal":
        return "", ""
    parts = value.split()
    if len(parts) >= 4:
        return parts[0], " ".join(parts[1:])
    return (" ".join(parts[:-1]), parts[-1]) if len(parts) >= 2 else (value.strip(), "")


def _split_declaration_reference(value: str) -> tuple[str, str]:
    number = re.search(_DOCUMENT_NUMBER_LENGTH, value)
    date = re.search(_DECLARATION_DATE, value)
    return (
        number.group(0) if number else "",
        date.group(0) if date else "",
    )


def _address_locations(address: str) -> dict[str, str]:
    normalized = _normalize_cell(address)
    result = {}
    province_match = re.search(
        rf"(?:^|{_ADDRESS_SEPARATOR})\s*استان\s+(.+?)"
        rf"(?=\s+شهرستان\b|\s+شهر\b|{_ADDRESS_SEPARATOR}|$)",
        normalized,
    )
    if province_match:
        result["province"] = f"استان {province_match.group(1).strip()}"
    detail_pattern = "|".join(re.escape(value) for value in _ADDRESS_DETAIL_PREFIXES)
    county_match = re.search(
        rf"(?:^|{_ADDRESS_SEPARATOR}|\s)شهرستان\s+(.+?)"
        rf"(?=\s+شهر\b|\s+(?:{detail_pattern})|{_ADDRESS_SEPARATOR}|$)",
        normalized,
    )
    if county_match:
        result["county"] = f"شهرستان {county_match.group(1).strip()}"
    city_match = re.search(
        rf"(?:^|{_ADDRESS_SEPARATOR})\s*شهر\s+"
        rf"([^{_ADDRESS_SEPARATOR[1:-1]}]+)",
        normalized,
    )
    if city_match:
        result["city"] = city_match.group(1).strip()
    elif result.get("county"):
        following_county = re.search(
            rf"شهرستان\s+[^{_ADDRESS_SEPARATOR[1:-1]}]+"
            rf"{_ADDRESS_SEPARATOR}\s*([^{_ADDRESS_SEPARATOR[1:-1]}]+)",
            normalized,
        )
        if following_county and not following_county.group(1).strip().startswith(
            _ADDRESS_DETAIL_PREFIXES
        ):
            result["city"] = following_county.group(1).strip()
        else:
            result["city"] = result["county"].removeprefix("شهرستان ")
    segments = [
        segment.strip(" .")
        for segment in re.split(rf"\s*{_ADDRESS_SEPARATOR}\s*", normalized)
        if segment.strip(" .")
    ]
    if not result.get("province") and result.get("county"):
        result["province"] = result["county"]
    elif not result.get("province") and segments:
        if segments[0] == "تهران":
            result.update(_DEFAULT_LOCATION)
        elif len(segments) >= 2 and not segments[1].startswith(
            _ADDRESS_DETAIL_PREFIXES
        ):
            result["province"] = f"استان {segments[0]}"
            result["county"] = f"شهرستان {segments[1]}"
            result["city"] = (
                segments[2]
                if len(segments) >= 3
                and not segments[2].startswith(_ADDRESS_DETAIL_PREFIXES)
                else segments[1]
            )
    return result


def extract_address_details(
    address: str, province: str = "", county: str = "", city: str = ""
) -> tuple[str, str]:
    """Return the address continuation (street) and explicitly labeled postal code."""
    normalized = _normalize_text(address)
    postal_match = re.search(
        r"(?:کد\s*پستی|کدپستی)\s*[:：\-]?\s*([0-9]{10})", normalized
    )
    postal_code = postal_match.group(1) if postal_match else ""

    remainder = _without_address_locations(
        normalized, province, county, city
    )

    street_match = re.search(
        r"خیابان\s*اصلی\s*[:：\-]?\s*(.*)", remainder
    )
    street = street_match.group(1) if street_match else remainder
    street = re.sub(
        r"(?:کد\s*پستی|کدپستی)\s*[:：\-]?\s*[0-9]{10}", "", street
    )
    street = re.sub(
        r"(?:تلفن|موبایل|همراه|شماره\s*تماس)\s*[:：\-]?.*$", "", street
    )
    street = re.sub(
        r"(?:،|,|\s+)(?:پلاک|واحد|طبقه|منزل|ساختمان)\s*[:：\-]?.*$",
        "",
        street,
    )
    street = re.sub(r"\s+", " ", street)
    street = street.strip(" \t\r\n-‐‑‒–—،,؛.:")
    return street or "خیابان اصلی", postal_code


def format_recipient_address(
    address: str, province: str = "", county: str = "", city: str = ""
) -> str:
    """Build titled province - county - city - remainder without repetition."""

    normalized_address = re.sub(
        r"(?<!\w)منطقه\s+تهران(?!\w)", "", _normalize_text(address)
    )
    remainder = _without_address_locations(
        normalized_address, province, county, city
    )
    remainder = re.sub(r"^\s*(?:نشانی|آدرس)\s*[:：\-]?\s*", "", remainder)
    remainder = re.sub(r"خیابان\s*اصلی\s*[:：\-]?\s*", "", remainder, count=1)
    remainder = re.sub(
        r"(?:تلفن|موبایل|همراه|شماره\s*تماس)\s*[:：\-]?.*$", "", remainder
    )
    remainder = " - ".join(
        segment.strip(" \t\r\n،,؛.:")
        for segment in re.split(r"\s*[-‐‑‒–—]\s*", remainder)
        if segment.strip(" \t\r\n،,؛.:")
    )
    remainder = re.sub(r"\s+", " ", remainder)
    remainder = remainder.strip(" \t\r\n-‐‑‒–—،,؛.:")

    normalized_province = _address_location_title(province, "استان")
    normalized_county = _address_location_title(county, "شهرستان")
    normalized_city = _address_location_title(city, "شهر")
    parts = [
        value
        for value in (normalized_province, normalized_county, normalized_city, remainder)
        if value
    ]
    return " - ".join(parts)


def _address_location_title(value: str, title: str) -> str:
    normalized = _normalize_text(value).strip()
    return (
        normalized
        if not normalized or normalized.startswith(f"{title} ")
        else f"{title} {normalized}"
    )


def _without_address_locations(
    address: str, province: str, county: str, city: str
) -> str:
    remainder = address
    locations = tuple(
        value for value in (
            province.removeprefix("استان "),
            county.removeprefix("شهرستان "),
            city.removeprefix("شهر "),
            province,
            county,
            f"شهر {city.removeprefix('شهر ')}" if city else "",
            city,
        ) if value
    )
    for location in sorted(
        {_normalize_text(value).strip() for value in locations if value.strip()},
        key=len,
        reverse=True,
    ):
        remainder = re.sub(
            rf"(?<!\w){re.escape(location)}(?!\w)", " ", remainder
        )
    return remainder


def _table_descriptions(rows: list[tuple[str, ...]]) -> tuple[str, ...]:
    result = []
    for row in rows:
        unique = tuple(dict.fromkeys(value for value in row if value))
        if len(unique) == 1 and len(unique[0]) >= 80 and "مخاطب محترم" in unique[0]:
            result.append(unique[0])
    return tuple(result)


def _normalize_cell(value: str) -> str:
    return " ".join(_normalize_text(value).split())


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
        jobs = tuple(
            str(value) for value in group.get("jobs", ()) if str(value).strip()
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
                job_title="، ".join(dict.fromkeys(jobs)),
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
            identity_no=primary.identity_no or detail.identity_no,
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
    identity_no = _find_digits(evidence, r"شماره\s*شناسنامه", minimum=1, maximum=20)
    employee_code = _find_digits(
        evidence, r"(?:کد|شماره)\s*پرسنلی", minimum=1, maximum=20
    )
    postal_code = _find_digits(evidence, r"کد\s*پستی", 10)
    address = _find_inline_value(evidence, r"(?:نشانی|آدرس)", maximum=500)
    job_title = _find_inline_value(evidence, r"شغل", maximum=120)
    issues = []
    if national_id and not _valid_national_id(national_id):
        issues.append("national_id_invalid")
    if not any((national_id, employee_code, address, postal_code)):
        issues.append("identity_details_missing")
    return RecipientCandidate(
        source_path=source,
        full_name=name,
        national_id=national_id,
        identity_no=identity_no,
        employee_code=employee_code,
        postal_code=postal_code,
        address=address,
        job_title=job_title,
        role=role,
        person_type=_person_type(name),
        evidence=evidence,
        extraction_method="local_party",
        issues=tuple(issues),
    )


def _clean_party_name(value: str) -> str:
    titled_suffix = re.search(
        rf"(?:^|{_ADDRESS_SEPARATOR})\s*(?:آقای|خانم)\s+(.+)$", value
    )
    if titled_suffix:
        value = titled_suffix.group(1)
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


def _read_text(path: Path, *, document: object | None = None) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            document = document or Document(path)
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
