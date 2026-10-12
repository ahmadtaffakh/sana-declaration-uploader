"""Deterministic, review-first preprocessing for extracted recipients."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re
import unicodedata

from .lookups import LookupResolutionError, resolve_lookup_fields, suggest_court_unit
from .models import RecipientCandidate


_ARABIC_TO_PERSIAN = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک"})
_SEGMENT_RE = re.compile(r"\s*[-‐‑‒–—]\s*")
_STREET_PREFIXES = (
    "خیابان", "خ ", "بلوار", "کوچه", "پلاک", "بخش", "دهستان", "روستا",
    "محله", "جاده", "میدان", "شهرک",
)


def preprocess_candidates(
    candidates: tuple[RecipientCandidate, ...], lookup_dir: Path
) -> tuple[RecipientCandidate, ...]:
    return tuple(preprocess_candidate(candidate, lookup_dir) for candidate in candidates)


def preprocess_candidate(
    candidate: RecipientCandidate, lookup_dir: Path
) -> RecipientCandidate:
    labels, address_issues = extract_address_labels(candidate.address)
    province = candidate.province or labels.get("province", "")
    county = candidate.county or labels.get("county", "")
    city = candidate.city or labels.get("city", "")
    issues = list(dict.fromkeys((*candidate.issues, *address_issues)))

    job_code = candidate.job_code
    if candidate.job_title and not job_code:
        try:
            job = resolve_lookup_fields(
                {"job_title": candidate.job_title}, lookup_dir
            )
        except LookupResolutionError:
            issues.append("job_lookup_unresolved")
        else:
            job_code = job.get("job_code", "")

    resolved_locations: dict[str, str] = {}
    location_labels = (("province", province), ("county", county), ("city", city))
    for field, label in location_labels:
        if not label:
            continue
        requested = {
            name: value for name, value in location_labels
            if value and name in {"province", "county", "city"}
            and (name == "province" or name == field or name in resolved_locations)
        }
        try:
            resolved_locations = resolve_lookup_fields(requested, lookup_dir)
        except LookupResolutionError:
            issues.append(f"{field}_lookup_unresolved")
            break

    court_fields = {
        **resolved_locations,
        "court_unit_name": candidate.court_unit_name,
        "court_unit_code": candidate.court_unit_code,
    }
    try:
        court_fields = suggest_court_unit(court_fields, lookup_dir)
    except LookupResolutionError:
        issues.append("court_unit_lookup_ambiguous")
    if (province or county or city) and not court_fields.get("court_unit_code", ""):
        issues.append("court_unit_unresolved")

    return replace(
        candidate,
        job_code=job_code,
        province=province,
        province_code=resolved_locations.get("province_code", candidate.province_code),
        county=county,
        county_code=resolved_locations.get("county_code", candidate.county_code),
        city=city,
        city_code=resolved_locations.get("city_code", candidate.city_code),
        court_unit_name=court_fields.get("court_unit_name", candidate.court_unit_name),
        court_unit_code=court_fields.get("court_unit_code", candidate.court_unit_code),
        issues=tuple(dict.fromkeys(issues)),
    )


def extract_address_labels(address: str) -> tuple[dict[str, str], tuple[str, ...]]:
    values: dict[str, set[str]] = {"province": set(), "county": set(), "city": set()}
    for raw_address in address.split("|"):
        segments = [
            _normalize_display(segment).strip(" ،؛")
            for segment in _SEGMENT_RE.split(raw_address)
            if segment.strip()
        ]
        county_seen = False
        city_seen = False
        for segment in segments:
            if segment.startswith("استان "):
                values["province"].add(segment)
            elif segment.startswith("شهرستان "):
                values["county"].add(segment)
                county_seen = True
            elif segment.startswith("شهر "):
                values["city"].add(segment.removeprefix("شهر ").strip())
                city_seen = True
            elif county_seen and not city_seen and _plausible_city(segment):
                values["city"].add(segment)
                city_seen = True

    labels = {}
    issues = []
    for field, matches in values.items():
        if len(matches) == 1:
            labels[field] = next(iter(matches))
        elif len(matches) > 1:
            issues.append(f"{field}_address_ambiguous")
    return labels, tuple(issues)


def _normalize_display(value: str) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", value).translate(_ARABIC_TO_PERSIAN).split()
    )


def _plausible_city(value: str) -> bool:
    return (
        1 < len(value) <= 60
        and not value.startswith(_STREET_PREFIXES)
        and not any(char.isdigit() for char in value)
    )
