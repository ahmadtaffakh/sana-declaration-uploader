"""Import reusable SANA lookup records from a legacy browser capture."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable


_LOCATION_TYPES = {2: "province", 3: "county", 5: "city"}


def import_legacy_lookups(source: Path, target_dir: Path) -> dict[str, int]:
    text = source.read_text(encoding="utf-8-sig")
    objects = tuple(_walk_objects(_decode_embedded_values(text)))
    locations = _location_entries(objects, source.name)
    courts = _court_entries(objects, source.name)
    jobs = _job_entries(objects, source.name)
    if not any((locations, courts, jobs)):
        raise ValueError("legacy_lookup_records_missing")

    target_dir.mkdir(parents=True, exist_ok=True)
    catalogs = {
        "locations": locations,
        "court_units": courts,
        "jobs": jobs,
    }
    for name, entries in catalogs.items():
        if entries:
            (target_dir / f"{name}.json").write_text(
                json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    return {name: len(entries) for name, entries in catalogs.items()}


def _decode_embedded_values(text: str) -> tuple[object, ...]:
    decoder = json.JSONDecoder()
    values = []
    index = 0
    while index < len(text):
        if text[index] not in "[{":
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index += 1
            continue
        values.append(value)
        index = end
    return tuple(values)


def _walk_objects(values: Iterable[object]) -> Iterable[dict[str, object]]:
    for value in values:
        if isinstance(value, dict):
            yield value
            yield from _walk_objects(value.values())
        elif isinstance(value, list):
            yield from _walk_objects(value)


def _location_entries(
    objects: Iterable[dict[str, object]], source_name: str
) -> list[dict[str, str]]:
    records: dict[str, dict[str, object]] = {}
    for item in objects:
        required = ("Id", "LocationCode", "LocationName", "LocationType")
        if not all(item.get(key) not in (None, "") for key in required):
            continue
        try:
            location_type = int(item["LocationType"])
        except (TypeError, ValueError):
            continue
        if location_type not in _LOCATION_TYPES:
            continue
        records[str(item["Id"])] = item

    by_type = {
        entry_type: sorted(
            (
                (str(item["LocationCode"]), str(item["Id"]))
                for item in records.values()
                if _LOCATION_TYPES[int(item["LocationType"])] == entry_type
            ),
            key=lambda pair: len(pair[0]),
            reverse=True,
        )
        for entry_type in _LOCATION_TYPES.values()
    }
    entries = []
    for item in records.values():
        entry_type = _LOCATION_TYPES[int(item["LocationType"])]
        code = str(item["LocationCode"])
        parent_type = {"county": "province", "city": "county"}.get(entry_type)
        parent_key = ""
        if parent_type:
            matches = [
                key for parent_code, key in by_type[parent_type]
                if len(parent_code) < len(code) and code.startswith(parent_code)
            ]
            if matches:
                parent_key = matches[0]
        entries.append({
            "key": str(item["Id"]),
            "label": str(item["LocationName"]).strip(),
            "type": entry_type,
            "parent_key": parent_key,
            "id": str(item["Id"]),
            "code": code,
            "source": source_name,
        })
    return sorted(entries, key=lambda item: (item["type"], item["code"], item["label"]))


def _court_entries(
    objects: Iterable[dict[str, object]], source_name: str
) -> list[dict[str, str]]:
    entries: dict[str, dict[str, str]] = {}
    for item in objects:
        if not all(item.get(key) not in (None, "") for key in ("Id", "Code", "UnitName")):
            continue
        key = str(item["Id"])
        entries[key] = {
            "key": key,
            "label": str(item["UnitName"]).strip(),
            "code": str(item["Code"]).strip(),
            "location_id": str(item.get("GeoLocationId") or "").strip(),
            "source": source_name,
        }
    return sorted(entries.values(), key=lambda item: (item["code"], item["label"]))


def _job_entries(
    objects: Iterable[dict[str, object]], source_name: str
) -> list[dict[str, str]]:
    entries: dict[str, dict[str, str]] = {}
    for item in objects:
        if not all(item.get(key) not in (None, "") for key in ("Id", "JobCode", "JobTitle")):
            continue
        key = str(item["Id"])
        entries[key] = {
            "key": key,
            "label": str(item["JobTitle"]).strip(),
            "id": key,
            "code": str(item["JobCode"]).strip(),
            "source": source_name,
        }
    return sorted(entries.values(), key=lambda item: (item["code"], item["label"]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target_dir", type=Path)
    args = parser.parse_args()
    counts = import_legacy_lookups(args.source.resolve(), args.target_dir.resolve())
    print(json.dumps(counts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
