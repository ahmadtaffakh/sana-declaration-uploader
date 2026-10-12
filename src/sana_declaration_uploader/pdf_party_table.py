"""Coordinate-aware extraction for SANA petition party tables."""

from __future__ import annotations

from pathlib import Path
import re
import unicodedata


_ARABIC_TO_PERSIAN = str.maketrans(
    {"ي": "ی", "ى": "ی", "ك": "ک", "ھ": "ه", "ہ": "ه"}
)
_ARABIC_RE = re.compile(r"[؀-ۿﭐ-﷿ﹰ-﻿]")
_ARABIC_RUN_RE = re.compile(r"[؀-ۿﭐ-﷿ﹰ-﻿]+")
_WORD_PART_RE = re.compile(
    r"[0-9]+(?:[/:.\-][0-9]+)*|[؀-ۿﭐ-﷿ﹰ-﻿]+|."
)
_ROLE_LABELS = {
    "خواهان": ("plaintiff", "group_natural"),
    "خوانده": ("defendant", "wide_rows"),
    "خواندگان": ("defendant", "wide_rows"),
    "تجدیدنظرخواه": ("appellant", "group_natural"),
    "تجدیدنظرخوانده": ("respondent", "wide_rows"),
    "اظهارکننده": ("declarant", "natural_rows"),
    "مخاطب": ("addressee", "wide_rows"),
    "مخاطبین": ("addressee", "wide_rows"),
    "مخاطبان": ("addressee", "wide_rows"),
}
_SECTION_LABELS = frozenset(
    {"وکیل", "سایر", "قانونی", "خواسته", "موضوع", "دلایل", "منضمات"}
)
_LEGAL_MARKERS = (
    "شرکت", "سازمان", "وزارت", "اداره", "بانک", "مؤسسه", "شهرداری",
    "خطوط لوله", "مخابرات نفت", "ملی نفت",
)


def extract_pdf_party_groups(path: Path) -> tuple[dict[str, object], ...]:
    """Return grounded party groups from the first structured PDF page."""

    try:
        import pdfplumber
    except ImportError:
        return ()

    try:
        with pdfplumber.open(str(path)) as pdf:
            if not pdf.pages:
                return ()
            page = pdf.pages[0]
            words = [
                {
                    "text": _fix_word(word["text"]),
                    "x0": float(word["x0"]),
                    "x1": float(word["x1"]),
                    "top": float(word["top"]),
                    "bottom": float(word["bottom"]),
                }
                for word in page.extract_words(use_text_flow=False)
            ]
            groups = _extract_groups(words, float(page.width))
            if groups:
                return groups
            notice_groups = _extract_notice_groups(words, float(page.width))
            if notice_groups:
                return notice_groups
            if _has_table_headers(words) or _has_notice_headers(words):
                return (_unparsed_table(),)
            return ()
    except Exception:
        return ()


def _fix_word(value: str) -> str:
    """Restore logical order for one pdfplumber visual-order word."""

    normalized = unicodedata.normalize("NFKC", value)
    if not _ARABIC_RE.search(normalized):
        return normalized
    parts = _WORD_PART_RE.findall(normalized)
    fixed = [part[::-1] if _ARABIC_RUN_RE.fullmatch(part) else part for part in parts]
    return "".join(reversed(fixed)).translate(_ARABIC_TO_PERSIAN)


def _extract_groups(
    words: list[dict[str, object]], page_width: float
) -> tuple[dict[str, object], ...]:
    if not _has_party_table(words):
        return ()

    anchors = _section_anchors(words, page_width)
    groups = []
    for index, (top, role, parser) in enumerate(anchors):
        end = anchors[index + 1][0] if index + 1 < len(anchors) else float("inf")
        section_words = [
            word for word in words if top - 2 <= float(word["top"]) < end - 8
        ]
        if parser in {"group_natural", "natural_rows"}:
            rows = _natural_party_rows(
                section_words, top, end, page_width, role
            )
            if parser == "group_natural" and rows:
                groups.append(_group_rows(rows, role))
            else:
                groups.extend(rows)
        elif parser == "wide_rows":
            groups.extend(
                _wide_party_rows(section_words, top, end, page_width, role)
            )
    return tuple(groups)


def _has_party_table(words: list[dict[str, object]]) -> bool:
    texts = {str(word["text"]) for word in words}
    return _has_table_headers(words) and any(
        text in _ROLE_LABELS for text in texts
    )


def _has_table_headers(words: list[dict[str, object]]) -> bool:
    texts = {str(word["text"]) for word in words}
    return "مشخصات" in texts and "شغل" in texts


def _section_anchors(
    words: list[dict[str, object]], page_width: float
) -> list[tuple[float, str, str]]:
    anchors = []
    for word in words:
        text = str(word["text"])
        center = (float(word["x0"]) + float(word["x1"])) / 2
        if center < page_width * 0.82:
            continue
        if text in _ROLE_LABELS:
            role, parser = _ROLE_LABELS[text]
            anchors.append((float(word["top"]), role, parser))
        elif text in _SECTION_LABELS:
            anchors.append((float(word["top"]), "stop", "stop"))
    anchors.sort()
    return anchors


def _natural_party_rows(
    words: list[dict[str, object]],
    start: float,
    end: float,
    page_width: float,
    role: str,
) -> list[dict[str, object]]:
    first_name_words = [
        word
        for word in words
        if page_width * 0.74 < _center(word) < page_width * 0.85
        and str(word["text"]) not in _ROLE_LABELS
    ]
    row_tops = _cluster_tops(first_name_words, tolerance=10)
    if not row_tops:
        return []

    rows = []
    for index, row_top in enumerate(row_tops):
        lower = start if index == 0 else (row_tops[index - 1] + row_top) / 2
        upper = end if index + 1 == len(row_tops) else (row_top + row_tops[index + 1]) / 2
        name = _clean_person_name(
            _cell_text(words, lower, upper, page_width * 0.64, page_width * 0.85)
        )
        if name:
            address = _cell_text(words, lower, upper, 0, page_width * 0.53)
            job = _cell_text(
                words, lower, upper, page_width * 0.53, page_width * 0.625
            )
            rows.append(
                {
                    "role": role,
                    "names": (name,),
                    "addresses": (address,),
                    "jobs": (job,),
                    "person_type": "natural",
                    "evidence": name,
                }
            )
    return rows


def _group_rows(
    rows: list[dict[str, object]], role: str
) -> dict[str, object]:
    names = tuple(str(row["names"][0]) for row in rows)
    addresses = tuple(str(row["addresses"][0]) for row in rows)
    jobs = tuple(str(row["jobs"][0]) for row in rows)
    return {
        "role": role,
        "names": names,
        "addresses": addresses,
        "jobs": jobs,
        "person_type": "group",
        "evidence": "، ".join(names),
    }


def _wide_party_rows(
    words: list[dict[str, object]],
    start: float,
    end: float,
    page_width: float,
    role: str,
) -> list[dict[str, object]]:
    name_words = [
        word
        for word in words
        if page_width * 0.58 < _center(word) < page_width * 0.82
        and str(word["text"]) not in _ROLE_LABELS
    ]
    row_tops = _cluster_tops(name_words, tolerance=10)
    if not row_tops:
        return []
    rows = []
    for index, row_top in enumerate(row_tops):
        lower = start if index == 0 else (row_tops[index - 1] + row_top) / 2
        upper = end if index + 1 == len(row_tops) else (row_top + row_tops[index + 1]) / 2
        name = _wide_name_text(words, lower, upper, page_width)
        if not name:
            continue
        address = _cell_text(words, lower, upper, 0, page_width * 0.53)
        job = _cell_text(
            words, lower, upper, page_width * 0.53, page_width * 0.625
        )
        rows.append(
            {
                "role": role,
                "names": (name,),
                "addresses": (address,),
                "jobs": (job,),
                "person_type": "legal" if any(m in name for m in _LEGAL_MARKERS) else "unknown",
                "evidence": name,
            }
        )
    return rows


def _wide_name_text(
    words: list[dict[str, object]],
    lower: float,
    upper: float,
    page_width: float,
) -> str:
    selected = [
        word
        for word in words
        if lower <= float(word["top"]) < upper
        and page_width * 0.53 < _center(word) < page_width * 0.82
    ]
    anchor_tops = [
        float(word["top"])
        for word in selected
        if _center(word) > page_width * 0.58
    ]
    selected = [
        word
        for word in selected
        if any(abs(float(word["top"]) - top) <= 3 for top in anchor_tops)
    ]
    return _cell_text(selected, lower, upper, page_width * 0.53, page_width * 0.82)


def _has_notice_headers(words: list[dict[str, object]]) -> bool:
    texts = {str(word["text"]).strip(":") for word in words}
    return {"مشخصات", "شونده", "کدملی"}.issubset(texts)


def _extract_notice_groups(
    words: list[dict[str, object]], page_width: float
) -> tuple[dict[str, object], ...]:
    if not _has_notice_headers(words):
        return ()
    code_header = next(
        word for word in words if str(word["text"]).strip(":") == "کدملی"
    )
    header_top = float(code_header["top"])
    data_words = [
        word
        for word in words
        if header_top + 4 < float(word["top"]) < header_top + 25
    ]
    name_words = [
        word for word in data_words
        if not re.fullmatch(r"[0-9\s\-/]+", str(word["text"]))
    ]
    name = _cell_text(
        name_words, header_top + 4, header_top + 25,
        page_width * 0.50, page_width * 0.93,
    )
    national_id = _cell_text(
        data_words, header_top + 4, header_top + 25,
        page_width * 0.48, page_width * 0.63,
    )
    national_id = re.sub(r"\D", "", national_id)
    if not name:
        return ()
    return ({
        "role": "addressee",
        "names": (name,),
        "addresses": ("",),
        "jobs": ("",),
        "national_ids": (national_id,),
        "person_type": "legal" if any(m in name for m in _LEGAL_MARKERS) else "unknown",
        "evidence": name,
    },)


def _unparsed_table() -> dict[str, object]:
    return {
        "role": "",
        "names": (),
        "addresses": (),
        "jobs": (),
        "person_type": "unknown",
        "evidence": "جدول اشخاص شناسایی شد اما قابل استخراج نبود.",
        "issues": ("party_table_unparsed",),
    }


def _cluster_tops(
    words: list[dict[str, object]], *, tolerance: float
) -> list[float]:
    tops = sorted(float(word["top"]) for word in words)
    clusters: list[list[float]] = []
    for top in tops:
        if not clusters or top - clusters[-1][-1] > tolerance:
            clusters.append([top])
        else:
            clusters[-1].append(top)
    return [sum(cluster) / len(cluster) for cluster in clusters]


def _cell_text(
    words: list[dict[str, object]],
    lower: float,
    upper: float,
    x_min: float,
    x_max: float,
) -> str:
    selected = [
        word
        for word in words
        if lower <= float(word["top"]) < upper and x_min < _center(word) < x_max
    ]
    lines: list[list[dict[str, object]]] = []
    for word in sorted(selected, key=lambda item: float(item["top"])):
        if not lines or float(word["top"]) - float(lines[-1][0]["top"]) > 3:
            lines.append([word])
        else:
            lines[-1].append(word)
    parts = []
    for line in lines:
        line.sort(key=lambda item: float(item["x1"]), reverse=True)
        parts.append(" ".join(str(word["text"]) for word in line))
    return " ".join(parts).strip()


def _center(word: dict[str, object]) -> float:
    return (float(word["x0"]) + float(word["x1"])) / 2


def _clean_person_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"^(سید)(?=\S)", r"\1 ", value)
    return value
