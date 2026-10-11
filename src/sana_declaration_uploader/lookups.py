"""Exact, fail-closed mapping from operator labels to SANA identifiers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping
import unicodedata


class LookupResolutionError(ValueError):
    """Raised when a label cannot be mapped to exactly one verified entry."""


def normalize_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).translate(
        str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک"})
    )
    return " ".join(normalized.split()).casefold()


def _load_entries(lookup_dir: Path, name: str) -> list[dict[str, str]]:
    path = lookup_dir / f"{name}.json"
    if not path.is_file():
        raise LookupResolutionError(f"lookup_file_missing:{name}")

    try:
        raw_entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LookupResolutionError(f"lookup_file_invalid:{name}") from exc

    if not isinstance(raw_entries, list) or not all(
        isinstance(entry, dict) for entry in raw_entries
    ):
        raise LookupResolutionError(f"lookup_schema_invalid:{name}")

    return [
        {str(key): str(value).strip() for key, value in entry.items()}
        for entry in raw_entries
    ]


def _resolve_by_label(
    entries: list[dict[str, str]],
    *,
    category: str,
    label: str,
    entry_type: str | None = None,
    parent_key: str | None = None,
) -> dict[str, str]:
    expected = normalize_label(label)
    matches = []
    for entry in entries:
        if normalize_label(entry.get("label", "")) != expected:
            continue
        if entry_type is not None and entry.get("type") != entry_type:
            continue
        if parent_key is not None and entry.get("parent_key") != parent_key:
            continue
        matches.append(entry)

    if not matches:
        raise LookupResolutionError(f"lookup_not_found:{category}:{label}")
    if len(matches) > 1:
        raise LookupResolutionError(f"lookup_ambiguous:{category}:{label}")
    return matches[0]


def _resolve_by_code(
    entries: list[dict[str, str]], *, category: str, code: str
) -> dict[str, str]:
    matches = [entry for entry in entries if entry.get("code") == code]
    if not matches:
        raise LookupResolutionError(f"lookup_not_found:{category}:{code}")
    if len(matches) > 1:
        raise LookupResolutionError(f"lookup_ambiguous:{category}:{code}")
    return matches[0]


def resolve_lookup_fields(
    fields: Mapping[str, str], lookup_dir: Path
) -> dict[str, str]:
    """Return fields enriched with verified site IDs/codes.

    Lookup files are loaded only for labels present in ``fields``. No fuzzy
    matching or fallback selection is performed.
    """

    resolved = _resolve_job(dict(fields), lookup_dir)
    resolved = _resolve_locations(resolved, lookup_dir)
    return _resolve_court(resolved, lookup_dir)


def _resolve_job(resolved: dict[str, str], lookup_dir: Path) -> dict[str, str]:
    job_title = resolved.get("job_title", "").strip()
    if not job_title:
        return resolved
    job = _resolve_by_label(
        _load_entries(lookup_dir, "jobs"), category="job", label=job_title
    )
    for key in ("key", "id", "code"):
        if not job.get(key):
            raise LookupResolutionError(f"lookup_entry_invalid:job:{job_title}")
    resolved.update(
        job_key=job["key"], job_id=job["id"], job_code=job["code"]
    )
    return resolved


def _resolve_locations(
    resolved: dict[str, str], lookup_dir: Path
) -> dict[str, str]:
    location_labels = (
        ("province", "province"),
        ("county", "county"),
        ("city", "city"),
        ("village", "village"),
    )
    if not any(resolved.get(field, "").strip() for field, _ in location_labels):
        return resolved
    locations = _load_entries(lookup_dir, "locations")
    parent_key: str | None = None
    missing_parent = False
    for field, entry_type in location_labels:
        label = resolved.get(field, "").strip()
        if not label:
            missing_parent = True
            continue
        if missing_parent:
            raise LookupResolutionError(f"lookup_parent_missing:{field}")
        location = _resolve_by_label(
            locations,
            category=field,
            label=label,
            entry_type=entry_type,
            parent_key=parent_key,
        )
        for key in ("key", "id", "code"):
            if not location.get(key):
                raise LookupResolutionError(
                    f"lookup_entry_invalid:{field}:{label}"
                )
        parent_key = location["key"]
        resolved.update(
            {
                f"{field}_key": location["key"],
                f"{field}_id": location["id"],
                f"{field}_code": location["code"],
            }
        )
    return resolved


def _resolve_court(
    resolved: dict[str, str], lookup_dir: Path
) -> dict[str, str]:
    court_name = resolved.get("court_unit_name", "").strip()
    court_code = resolved.get("court_unit_code", "").strip()
    if court_name:
        court = _resolve_by_label(
            _load_entries(lookup_dir, "court_units"),
            category="court_unit",
            label=court_name,
        )
        if not court.get("key") or not court.get("code"):
            raise LookupResolutionError(
                f"lookup_entry_invalid:court_unit:{court_name}"
            )
        if court_code and court_code != court["code"]:
            raise LookupResolutionError(f"court_unit_code_mismatch:{court_name}")
        resolved.update(
            court_unit_key=court["key"], court_unit_code=court["code"]
        )
    elif court_code:
        court = _resolve_by_code(
            _load_entries(lookup_dir, "court_units"),
            category="court_unit",
            code=court_code,
        )
        if not court.get("key") or not court.get("label"):
            raise LookupResolutionError(
                f"lookup_entry_invalid:court_unit:{court_code}"
            )
        resolved.update(
            court_unit_key=court["key"], court_unit_name=court["label"]
        )

    return resolved
