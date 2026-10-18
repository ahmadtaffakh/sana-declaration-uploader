"""Verified registration history scoped to declaration input files."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Mapping

from .models import DeclarationDraft, UploadResult


RESULTS_FILENAME = "results.json"


class ResultPersistenceError(RuntimeError):
    """Raised when a verified result cannot safely be persisted."""


def _load_manifest(source_dir: Path) -> dict[str, object]:
    path = source_dir / RESULTS_FILENAME
    if not path.exists():
        return {"schema_version": 1, "declarations": {}}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ResultPersistenceError("existing_results_invalid") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or not isinstance(manifest.get("declarations"), dict)
    ):
        raise ResultPersistenceError("existing_results_invalid")
    return manifest


def _recipient_source_key(source_path: Path, source_recipient_ordinal: int) -> str:
    if source_recipient_ordinal < 1:
        raise ResultPersistenceError("source_recipient_ordinal_invalid")
    return f"{source_path.name}::recipient-{source_recipient_ordinal:03d}"


def _source_key(draft: DeclarationDraft, source_key: str | None = None) -> str:
    key = source_key or (
        _recipient_source_key(
            draft.source_path, draft.source_recipient_ordinal
        )
        if draft.source_path else ""
    )
    if not key:
        raise ResultPersistenceError("source_file_missing")
    return key


def _record_for_recipient(
    declarations: object,
    source_path: Path,
    source_recipient_ordinal: int,
    source_recipient_count: int | None,
) -> dict[str, object] | None:
    if not isinstance(declarations, dict):
        raise ResultPersistenceError("existing_results_invalid")
    key = _recipient_source_key(source_path, source_recipient_ordinal)
    record = declarations.get(key)
    if record is not None:
        if not isinstance(record, dict):
            raise ResultPersistenceError("existing_results_invalid")
        return record

    legacy = declarations.get(source_path.name)
    if legacy is None:
        return None
    if not isinstance(legacy, dict):
        raise ResultPersistenceError("existing_results_invalid")
    registrations = legacy.get("registrations")
    if not isinstance(registrations, list) or not registrations:
        raise ResultPersistenceError("existing_results_invalid")
    if source_recipient_count in (None, 1) and source_recipient_ordinal == 1:
        return legacy
    if (
        source_recipient_count is None
        or source_recipient_count < source_recipient_ordinal
        or len(registrations) != source_recipient_count
    ):
        raise ResultPersistenceError(
            "legacy_results_ambiguous_for_multiple_recipients"
        )
    return {"registrations": [registrations[source_recipient_ordinal - 1]]}


def read_saved_result(
    source_dir: Path,
    source_path: Path,
    *,
    source_recipient_ordinal: int = 1,
    source_recipient_count: int | None = None,
) -> dict[str, str] | None:
    """Return the latest saved registration for one source recipient."""

    declarations = _load_manifest(source_dir.resolve())["declarations"]
    record = _record_for_recipient(
        declarations,
        source_path,
        source_recipient_ordinal,
        source_recipient_count,
    )
    if record is None:
        return None
    registrations = record.get("registrations")
    if not isinstance(registrations, list) or not registrations:
        raise ResultPersistenceError("existing_results_invalid")
    latest = registrations[-1]
    if not isinstance(latest, dict):
        raise ResultPersistenceError("existing_results_invalid")
    tracking_code = str(latest.get("tracking_code", ""))
    return {
        "tracking_code": tracking_code or "رسید نامعتبر",
        "registered_at": str(latest.get("registered_at", "")),
    }


def read_saved_attachment_context(
    source_dir: Path,
    source_path: Path,
    *,
    source_recipient_ordinal: int = 1,
    source_recipient_count: int | None = None,
) -> dict[str, object] | None:
    """Return the identifiers and reliefs needed to append to a prior record."""

    declarations = _load_manifest(source_dir.resolve())["declarations"]
    record = _record_for_recipient(
        declarations,
        source_path,
        source_recipient_ordinal,
        source_recipient_count,
    )
    if record is None:
        return None
    registrations = record.get("registrations")
    if not isinstance(registrations, list) or not registrations:
        return None
    latest = registrations[-1]
    if not isinstance(latest, dict):
        return None
    context = latest.get("attachment_context")
    if not isinstance(context, dict):
        return None
    if not all(
        isinstance(context.get(key), str) and str(context[key]).strip()
        for key in ("petition_number", "petition_id", "petition_type_code")
    ):
        return None
    reliefs = context.get("reliefs")
    if not isinstance(reliefs, list) or not reliefs or not all(
        isinstance(relief, dict) for relief in reliefs
    ):
        return None
    return {
        "tracking_code": str(latest.get("tracking_code", "")),
        "petition_number": str(context["petition_number"]),
        "petition_id": str(context["petition_id"]),
        "petition_type_code": str(context["petition_type_code"]),
        "reliefs": reliefs,
    }


def read_legacy_tracking_codes(
    source_dir: Path,
    source_path: Path,
) -> tuple[str, ...]:
    """Return verified-looking codes stored under the old file-level key."""

    declarations = _load_manifest(source_dir.resolve())["declarations"]
    if not isinstance(declarations, dict):
        raise ResultPersistenceError("existing_results_invalid")
    record = declarations.get(source_path.name)
    if record is None:
        return ()
    if not isinstance(record, dict):
        raise ResultPersistenceError("existing_results_invalid")
    registrations = record.get("registrations")
    if not isinstance(registrations, list):
        raise ResultPersistenceError("existing_results_invalid")
    codes = []
    for registration in registrations:
        if not isinstance(registration, dict):
            raise ResultPersistenceError("existing_results_invalid")
        code = str(registration.get("tracking_code", "")).strip()
        if code and code != "رسید نامعتبر" and code not in codes:
            codes.append(code)
    return tuple(codes)


def migrate_legacy_registration(
    draft: DeclarationDraft,
    tracking_code: str,
) -> Path:
    """Move one uniquely matched legacy registration to its recipient key."""

    code = tracking_code.strip()
    if not code or draft.source_path is None:
        raise ResultPersistenceError("legacy_migration_input_invalid")
    source_dir = draft.source_dir.resolve()
    target = source_dir / RESULTS_FILENAME
    manifest = _load_manifest(source_dir)
    declarations = manifest["declarations"]
    if not isinstance(declarations, dict):
        raise ResultPersistenceError("existing_results_invalid")
    legacy_key = draft.source_path.name
    legacy = declarations.get(legacy_key)
    if not isinstance(legacy, dict):
        raise ResultPersistenceError("legacy_registration_not_found")
    legacy_registrations = legacy.get("registrations")
    if not isinstance(legacy_registrations, list):
        raise ResultPersistenceError("existing_results_invalid")
    matches = [
        registration for registration in legacy_registrations
        if isinstance(registration, dict)
        and str(registration.get("tracking_code", "")).strip() == code
    ]
    if len(matches) != 1:
        raise ResultPersistenceError("legacy_registration_not_unique")

    key = _source_key(draft)
    destination = declarations.setdefault(key, {"registrations": []})
    if not isinstance(destination, dict):
        raise ResultPersistenceError("existing_results_invalid")
    registrations = destination.get("registrations")
    if not isinstance(registrations, list):
        raise ResultPersistenceError("existing_results_invalid")
    if registrations and not any(
        isinstance(item, dict)
        and str(item.get("tracking_code", "")).strip() == code
        for item in registrations
    ):
        raise ResultPersistenceError("recipient_registration_conflict")
    if not registrations:
        registrations.append(matches[0])
    destination["source_file"] = draft.source_path.name
    destination["source_recipient_ordinal"] = draft.source_recipient_ordinal
    legacy_registrations.remove(matches[0])
    if not legacy_registrations:
        declarations.pop(legacy_key)
    _write_manifest(target, manifest)
    return target


def existing_results(
    drafts: tuple[DeclarationDraft, ...],
) -> tuple[tuple[str, dict[str, str]], ...]:
    """Return prior registrations matching the Word files in this batch."""

    source_counts: dict[Path, int] = {}
    for draft in drafts:
        if draft.source_path is not None:
            source = draft.source_path.resolve()
            source_counts[source] = source_counts.get(source, 0) + 1

    prior = []
    for draft in drafts:
        if draft.source_path is None:
            continue
        source = draft.source_path.resolve()
        source_count = source_counts[source]
        try:
            result = read_saved_result(
                draft.source_dir,
                draft.source_path,
                source_recipient_ordinal=draft.source_recipient_ordinal,
                source_recipient_count=source_count,
            )
        except ResultPersistenceError:
            result = {"tracking_code": "رسید نامعتبر", "registered_at": ""}
        if result is not None:
            label = draft.source_path.name
            if source_count > 1:
                label += f" — شخص {draft.source_recipient_ordinal}"
            prior.append((label, result))
    return tuple(prior)


def save_verified_result(
    draft: DeclarationDraft,
    result: UploadResult,
    *,
    filename: str = RESULTS_FILENAME,
    source_key: str | None = None,
    attachment_context: Mapping[str, object] | None = None,
) -> Path:
    if result.status != "success" or not result.tracking_code.strip():
        raise ResultPersistenceError("unverified_result")

    source_dir = draft.source_dir.resolve()
    if not source_dir.is_dir():
        raise ResultPersistenceError("source_dir_missing")

    key = _source_key(draft, source_key)
    target = source_dir / filename
    if target.exists():
        manifest = _load_manifest(source_dir)
    else:
        manifest = {"schema_version": 1, "declarations": {}}
    declarations = manifest["declarations"]
    record = declarations.setdefault(key, {"registrations": []})
    if not isinstance(record, dict):
        raise ResultPersistenceError("existing_results_invalid")
    if draft.source_path is not None:
        record["source_file"] = draft.source_path.name
        record["source_recipient_ordinal"] = draft.source_recipient_ordinal
    registrations = record.get("registrations")
    if not isinstance(registrations, list):
        raise ResultPersistenceError("existing_results_invalid")
    registration: dict[str, object] = {
        "tracking_code": result.tracking_code.strip(),
        "registered_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if attachment_context is not None:
        registration["attachment_context"] = dict(attachment_context)
    registrations.append(registration)

    _write_manifest(target, manifest)
    return target


def save_attachment_count(
    draft: DeclarationDraft,
    tracking_code: str,
    attachment_count: int,
    *,
    filename: str = RESULTS_FILENAME,
) -> Path:
    """Update the matching verified registration with the observed count."""

    code = tracking_code.strip()
    if not code:
        raise ResultPersistenceError("tracking_code_missing")
    if (
        isinstance(attachment_count, bool)
        or not isinstance(attachment_count, int)
        or attachment_count < 0
    ):
        raise ResultPersistenceError("attachment_count_invalid")

    source_dir = draft.source_dir.resolve()
    if not source_dir.is_dir():
        raise ResultPersistenceError("source_dir_missing")
    target = source_dir / filename
    manifest = _load_manifest(source_dir)
    declarations = manifest["declarations"]
    if not isinstance(declarations, dict):
        raise ResultPersistenceError("existing_results_invalid")

    keys = [_source_key(draft)]
    if draft.source_path is not None and draft.source_path.name not in keys:
        keys.append(draft.source_path.name)
    registration = None
    for key in keys:
        record = declarations.get(key)
        if record is None:
            continue
        if not isinstance(record, dict):
            raise ResultPersistenceError("existing_results_invalid")
        registrations = record.get("registrations")
        if not isinstance(registrations, list):
            raise ResultPersistenceError("existing_results_invalid")
        registration = next((
            item for item in reversed(registrations)
            if isinstance(item, dict)
            and str(item.get("tracking_code", "")).strip() == code
        ), None)
        if registration is not None:
            break
    if registration is None:
        raise ResultPersistenceError("verified_registration_not_found")

    registration["attachment_count"] = attachment_count
    registration["attachment_count_checked_at"] = (
        datetime.now().astimezone().isoformat(timespec="seconds")
    )
    _write_manifest(target, manifest)
    return target


def _write_manifest(target: Path, manifest: Mapping[str, object]) -> None:
    temporary = target.with_suffix(target.suffix + ".tmp")

    try:
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    except OSError as exc:
        raise ResultPersistenceError("result_write_failed") from exc
