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

    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    except OSError as exc:
        raise ResultPersistenceError("result_write_failed") from exc
    return target
