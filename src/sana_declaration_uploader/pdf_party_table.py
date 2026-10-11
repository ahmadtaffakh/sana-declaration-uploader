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
    "خواهان": "plaintiff",
    "خوانده": "defendant",
    "خواندگان": "defendant",
}
_SECTION_LABELS = frozenset(
    {"وکیل", "سایر", "قانونی", "خواسته", "دلایل", "منضمات"}
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
            return _extract_groups(words, float(page.width))
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
    for index, (top, role) in enumerate(anchors):
        end = anchors[index + 1][0] if index + 1 < len(anchors) else float("inf")
        section_words = [
            word for word in words if top - 2 <= float(word["top"]) < end - 8
        ]
        if role == "plaintiff":
            group = _plaintiff_group(section_words, top, end, page_width)
            if group:
                groups.append(group)
        elif role == "defendant":
            groups.extend(_defendant_groups(section_words, top, end, page_width))
    return tuple(groups)


def _has_party_table(words: list[dict[str, object]]) -> bool:
    texts = {str(word["text"]) for word in words}
    return "مشخصات" in texts and "شغل" in texts and any(
        text in _ROLE_LABELS for text in texts
    )


def _section_anchors(
    words: list[dict[str, object]], page_width: float
) -> list[tuple[float, str]]:
    anchors = []
    for word in words:
        text = str(word["text"])
        center = (float(word["x0"]) + float(word["x1"])) / 2
        if center < page_width * 0.82:
            continue
        if text in _ROLE_LABELS:
            anchors.append((float(word["top"]), _ROLE_LABELS[text]))
        elif text in _SECTION_LABELS:
            anchors.append((float(word["top"]), "stop"))
    anchors.sort()
    return anchors


def _plaintiff_group(
    words: list[dict[str, object]], start: float, end: float, page_width: float
) -> dict[str, object] | None:
    first_name_words = [
        word
        for word in words
        if page_width * 0.74 < _center(word) < page_width * 0.85
        and str(word["text"]) not in _ROLE_LABELS
    ]
    row_tops = _cluster_tops(first_name_words, tolerance=3)
    if not row_tops:
        return None

    names = []
    addresses = []
    for index, row_top in enumerate(row_tops):
        lower = start if index == 0 else (row_tops[index - 1] + row_top) / 2
        upper = end if index + 1 == len(row_tops) else (row_top + row_tops[index + 1]) / 2
        first_name = _cell_text(
            words, lower, upper, page_width * 0.74, page_width * 0.85
        )
        last_name = _cell_text(
            words, lower, upper, page_width * 0.64, page_width * 0.74
        )
        name = _clean_person_name(f"{first_name} {last_name}")
        if name:
            names.append(name)
            addresses.append(
                _cell_text(words, lower, upper, 0, page_width * 0.53)
            )
    if not names:
        return None
    return {
        "role": "plaintiff",
        "names": tuple(names),
        "addresses": tuple(addresses),
        "person_type": "group",
        "evidence": "، ".join(names),
    }


def _defendant_groups(
    words: list[dict[str, object]], start: float, end: float, page_width: float
) -> list[dict[str, object]]:
    name_words = [
        word
        for word in words
        if page_width * 0.53 < _center(word) < page_width * 0.82
        and str(word["text"]) not in _ROLE_LABELS
    ]
    row_tops = _cluster_tops(name_words, tolerance=4)
    if not row_tops:
        return []
    rows = []
    for index, row_top in enumerate(row_tops):
        lower = start if index == 0 else (row_tops[index - 1] + row_top) / 2
        upper = end if index + 1 == len(row_tops) else (row_top + row_tops[index + 1]) / 2
        name = _cell_text(
            words, lower, upper, page_width * 0.53, page_width * 0.82
        )
        if not name:
            continue
        address = _cell_text(words, lower, upper, 0, page_width * 0.53)
        rows.append(
            {
                "role": "defendant",
                "names": (name,),
                "addresses": (address,),
                "person_type": "legal" if any(m in name for m in _LEGAL_MARKERS) else "unknown",
                "evidence": name,
            }
        )
    return rows


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
