"""Central verified registration history with legacy JSON migration."""

from __future__ import annotations

from datetime import datetime
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Mapping

from .models import DeclarationDraft, UploadResult


RESULTS_FILENAME = "sana-history.sqlite3"
LEGACY_RESULTS_FILENAME = "results.json"


class ResultPersistenceError(RuntimeError):
    """Raised when verified history cannot safely be read or persisted."""


def history_database_path() -> Path:
    """Return the central database beside the executable or source project."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / RESULTS_FILENAME
    return Path(__file__).resolve().parents[2] / RESULTS_FILENAME


def _database_path(filename: str = RESULTS_FILENAME) -> Path:
    path = history_database_path()
    return path if filename == RESULTS_FILENAME else path.with_name(filename)


@contextmanager
def _connect(filename: str = RESULTS_FILENAME):
    path = _database_path(filename)
    connection = None
    try:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS registrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                source_file TEXT NOT NULL,
                source_recipient_ordinal INTEGER NOT NULL,
                tracking_code TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                related_declaration_number TEXT NOT NULL DEFAULT '',
                national_id TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                attachment_count INTEGER,
                attachment_count_checked_at TEXT NOT NULL DEFAULT '',
                attachment_context TEXT,
                origin TEXT NOT NULL DEFAULT 'local',
                UNIQUE(
                    source_path, source_recipient_ordinal,
                    tracking_code, registered_at
                )
            )
            """
        )
        columns = {
            str(row[1]) for row in connection.execute(
                "PRAGMA table_info(registrations)"
            )
        }
        if "origin" not in columns:
            connection.execute(
                "ALTER TABLE registrations "
                "ADD COLUMN origin TEXT NOT NULL DEFAULT 'local'"
            )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS registrations_source_idx
               ON registrations(source_path, source_recipient_ordinal, id DESC)"""
        )
        yield connection
        connection.commit()
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.rollback()
        raise ResultPersistenceError("history_database_unavailable") from exc
    finally:
        if connection is not None:
            connection.close()


def _source_path(source_dir: Path, source_path: Path) -> str:
    path = source_path if source_path.is_absolute() else source_dir / source_path
    return str(path.resolve())


def _source_key(draft: DeclarationDraft) -> str:
    if draft.source_path is None:
        raise ResultPersistenceError("source_file_missing")
    if draft.source_recipient_ordinal < 1:
        raise ResultPersistenceError("source_recipient_ordinal_invalid")
    return _source_path(draft.source_dir, draft.source_path)


def _row_result(row: sqlite3.Row) -> dict[str, object]:
    count = row["attachment_count"]
    return {
        "tracking_code": str(row["tracking_code"] or "رسید نامعتبر"),
        "registered_at": str(row["registered_at"] or ""),
        "attachment_count": count if isinstance(count, int) and count >= 0 else None,
        "attachment_count_checked_at": str(
            row["attachment_count_checked_at"] or ""
        ),
    }


def _legacy_result(registration: Mapping[str, object]) -> dict[str, object]:
    count = registration.get("attachment_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        count = None
    return {
        "tracking_code": str(registration.get("tracking_code", "")) or "رسید نامعتبر",
        "registered_at": str(registration.get("registered_at", "")),
        "attachment_count": count,
        "attachment_count_checked_at": str(
            registration.get("attachment_count_checked_at", "")
        ),
    }


def _latest_row(
    connection: sqlite3.Connection,
    source: str,
    ordinal: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT * FROM registrations
           WHERE source_path = ? AND source_recipient_ordinal = ?
           ORDER BY id DESC LIMIT 1""",
        (source, ordinal),
    ).fetchone()


def _load_legacy_manifest(source_dir: Path) -> dict[str, object] | None:
    path = source_dir / LEGACY_RESULTS_FILENAME
    if not path.exists():
        return None
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


def _legacy_registrations(
    source_dir: Path,
    source_path: Path,
    ordinal: int,
    count: int | None,
) -> list[dict[str, object]]:
    manifest = _load_legacy_manifest(source_dir)
    if manifest is None:
        return []
    declarations = manifest["declarations"]
    assert isinstance(declarations, dict)
    recipient_key = f"{source_path.name}::recipient-{ordinal:03d}"
    record = declarations.get(recipient_key)
    if record is None:
        record = declarations.get(source_path.name)
        if record is None:
            return []
        if not isinstance(record, dict):
            raise ResultPersistenceError("existing_results_invalid")
        registrations = record.get("registrations")
        if not isinstance(registrations, list) or not registrations:
            raise ResultPersistenceError("existing_results_invalid")
        if count in (None, 1) and ordinal == 1:
            selected = registrations
        elif count is not None and count >= ordinal and len(registrations) == count:
            selected = [registrations[ordinal - 1]]
        else:
            raise ResultPersistenceError(
                "legacy_results_ambiguous_for_multiple_recipients"
            )
    else:
        if not isinstance(record, dict):
            raise ResultPersistenceError("existing_results_invalid")
        selected = record.get("registrations")
    if not isinstance(selected, list) or not all(
        isinstance(item, dict) for item in selected
    ):
        raise ResultPersistenceError("existing_results_invalid")
    return selected


def _insert_registration(
    connection: sqlite3.Connection,
    *,
    source: str,
    source_file: str,
    ordinal: int,
    registration: Mapping[str, object],
    fields: Mapping[str, str] | None = None,
) -> int:
    tracking_code = str(registration.get("tracking_code", "")).strip()
    registered_at = str(registration.get("registered_at", "")).strip()
    if not tracking_code or not registered_at:
        raise ResultPersistenceError("existing_results_invalid")
    context = registration.get("attachment_context")
    context_json = (
        json.dumps(context, ensure_ascii=False)
        if isinstance(context, dict)
        else None
    )
    values = fields or {}
    count = registration.get("attachment_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        count = None
    origin = str(registration.get("origin", "local") or "local").strip()
    if origin not in ("local", "sana"):
        raise ResultPersistenceError("registration_origin_invalid")
    cursor = connection.execute(
        """INSERT OR IGNORE INTO registrations (
               source_path, source_file, source_recipient_ordinal,
               tracking_code, registered_at, related_declaration_number,
               national_id, first_name, last_name, attachment_count,
               attachment_count_checked_at, attachment_context, origin
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            source, source_file, ordinal, tracking_code, registered_at,
            str(values.get("related_declaration_number", "")).strip(),
            str(values.get("recipient_national_id", "")).strip(),
            str(values.get("recipient_first_name", "")).strip(),
            str(values.get("recipient_last_name", "")).strip(),
            count,
            str(registration.get("attachment_count_checked_at", "")).strip(),
            context_json,
            origin,
        ),
    )
    return int(cursor.rowcount)


def _import_legacy_for_recipient(
    connection: sqlite3.Connection,
    source_dir: Path,
    source_path: Path,
    ordinal: int,
    count: int | None,
) -> None:
    source = _source_path(source_dir, source_path)
    for registration in _legacy_registrations(
        source_dir, source_path, ordinal, count
    ):
        _insert_registration(
            connection,
            source=source,
            source_file=source_path.name,
            ordinal=ordinal,
            registration=registration,
        )


def read_saved_result(
    source_dir: Path,
    source_path: Path,
    *,
    source_recipient_ordinal: int = 1,
    source_recipient_count: int | None = None,
) -> dict[str, object] | None:
    source = _source_path(source_dir, source_path)
    with _connect() as connection:
        row = _latest_row(connection, source, source_recipient_ordinal)
        if row is None:
            if source_recipient_count is None:
                legacy = _legacy_registrations(
                    source_dir, source_path, source_recipient_ordinal, None
                )
                return _legacy_result(legacy[-1]) if legacy else None
            _import_legacy_for_recipient(
                connection, source_dir, source_path,
                source_recipient_ordinal, source_recipient_count,
            )
            row = _latest_row(connection, source, source_recipient_ordinal)
        return _row_result(row) if row is not None else None


def read_saved_attachment_context(
    source_dir: Path,
    source_path: Path,
    *,
    source_recipient_ordinal: int = 1,
    source_recipient_count: int | None = None,
) -> dict[str, object] | None:
    source = _source_path(source_dir, source_path)
    with _connect() as connection:
        row = _latest_row(connection, source, source_recipient_ordinal)
        if row is None:
            _import_legacy_for_recipient(
                connection, source_dir, source_path,
                source_recipient_ordinal, source_recipient_count,
            )
            row = _latest_row(connection, source, source_recipient_ordinal)
        if row is None or not row["attachment_context"]:
            return None
        try:
            context = json.loads(row["attachment_context"])
        except (TypeError, ValueError) as exc:
            raise ResultPersistenceError("existing_results_invalid") from exc
        if not isinstance(context, dict):
            return None
        if not all(str(context.get(key, "")).strip() for key in (
            "petition_number", "petition_id", "petition_type_code"
        )):
            return None
        reliefs = context.get("reliefs")
        if not isinstance(reliefs, list) or not reliefs or not all(
            isinstance(relief, dict) for relief in reliefs
        ):
            return None
        return {"tracking_code": str(row["tracking_code"]), **context}


def read_legacy_tracking_codes(
    source_dir: Path,
    source_path: Path,
) -> tuple[str, ...]:
    manifest = _load_legacy_manifest(source_dir)
    if manifest is None:
        return ()
    declarations = manifest["declarations"]
    assert isinstance(declarations, dict)
    record = declarations.get(source_path.name)
    if record is None:
        return ()
    if not isinstance(record, dict) or not isinstance(record.get("registrations"), list):
        raise ResultPersistenceError("existing_results_invalid")
    source = _source_path(source_dir, source_path)
    with _connect() as connection:
        imported = {
            str(row[0]) for row in connection.execute(
                "SELECT tracking_code FROM registrations WHERE source_path = ?",
                (source,),
            )
        }
    codes = []
    for registration in record["registrations"]:
        if not isinstance(registration, dict):
            raise ResultPersistenceError("existing_results_invalid")
        code = str(registration.get("tracking_code", "")).strip()
        if code and code != "رسید نامعتبر" and code not in imported and code not in codes:
            codes.append(code)
    return tuple(codes)


def migrate_legacy_registration(
    draft: DeclarationDraft,
    tracking_code: str,
) -> Path:
    source = _source_key(draft)
    manifest = _load_legacy_manifest(draft.source_dir)
    if manifest is None:
        raise ResultPersistenceError("legacy_registration_not_found")
    declarations = manifest["declarations"]
    assert isinstance(declarations, dict)
    record = declarations.get(draft.source_path.name)
    registrations = record.get("registrations") if isinstance(record, dict) else None
    matches = [
        item for item in registrations or []
        if isinstance(item, dict)
        and str(item.get("tracking_code", "")).strip() == tracking_code.strip()
    ]
    if len(matches) != 1:
        raise ResultPersistenceError("legacy_registration_not_unique")
    with _connect() as connection:
        _insert_registration(
            connection,
            source=source,
            source_file=draft.source_path.name,
            ordinal=draft.source_recipient_ordinal,
            registration=matches[0],
            fields=draft.fields,
        )
    return _database_path()


def existing_results(
    drafts: tuple[DeclarationDraft, ...],
) -> tuple[tuple[str, dict[str, object]], ...]:
    counts: dict[Path, int] = {}
    for draft in drafts:
        if draft.source_path is not None:
            source = draft.source_path.resolve()
            counts[source] = counts.get(source, 0) + 1
    prior = []
    for draft in drafts:
        if draft.source_path is None:
            continue
        try:
            result = read_saved_result(
                draft.source_dir, draft.source_path,
                source_recipient_ordinal=draft.source_recipient_ordinal,
                source_recipient_count=counts[draft.source_path.resolve()],
            )
        except ResultPersistenceError:
            result = {"tracking_code": "رسید نامعتبر", "registered_at": ""}
        if result is not None:
            label = draft.source_path.name
            if counts[draft.source_path.resolve()] > 1:
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
    del source_key
    if result.status != "success" or not result.tracking_code.strip():
        raise ResultPersistenceError("unverified_result")
    if not draft.source_dir.resolve().is_dir():
        raise ResultPersistenceError("source_dir_missing")
    source = _source_key(draft)
    registration: dict[str, object] = {
        "tracking_code": result.tracking_code.strip(),
        "registered_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if attachment_context is not None:
        registration["attachment_context"] = dict(attachment_context)
    with _connect(filename) as connection:
        _insert_registration(
            connection,
            source=source,
            source_file=draft.source_path.name,
            ordinal=draft.source_recipient_ordinal,
            registration=registration,
            fields=draft.fields,
        )
    return _database_path(filename)


def save_attachment_count(
    draft: DeclarationDraft,
    tracking_code: str,
    attachment_count: int,
    *,
    filename: str = RESULTS_FILENAME,
) -> Path:
    if not tracking_code.strip():
        raise ResultPersistenceError("tracking_code_missing")
    if isinstance(attachment_count, bool) or not isinstance(attachment_count, int) or attachment_count < 0:
        raise ResultPersistenceError("attachment_count_invalid")
    source = _source_key(draft)
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    with _connect(filename) as connection:
        cursor = connection.execute(
            """UPDATE registrations
               SET attachment_count = ?, attachment_count_checked_at = ?
               WHERE id = (
                   SELECT id FROM registrations
                   WHERE source_path = ? AND source_recipient_ordinal = ?
                     AND tracking_code = ? ORDER BY id DESC LIMIT 1
               )""",
            (
                attachment_count, checked_at, source,
                draft.source_recipient_ordinal, tracking_code.strip(),
            ),
        )
        if cursor.rowcount != 1:
            raise ResultPersistenceError("verified_registration_not_found")
    return _database_path(filename)


def import_sana_registration(petition: Mapping[str, object]) -> int:
    """Insert one previously unknown, verified SANA registration once."""

    tracking_code = str(petition.get("No") or "").strip()
    registered_at = str(petition.get("CreateDateTime") or "").strip()
    if not re.fullmatch(r"\d{16}", tracking_code):
        raise ResultPersistenceError("tracking_code_invalid")
    if not registered_at:
        raise ResultPersistenceError("registered_at_missing")
    people = petition.get("theJSSPetitionPerson")
    recipients = [
        person for person in people
        if isinstance(person, Mapping) and str(person.get("RoleType")) == "2"
    ] if isinstance(people, list) else []
    recipient = recipients[0] if len(recipients) == 1 else {}
    fields = {
        "related_declaration_number": str(
            petition.get("RelatedNo") or ""
        ).strip(),
        "recipient_national_id": str(
            recipient.get("NationalityCode") or ""
        ).strip(),
        "recipient_first_name": str(recipient.get("Name") or "").strip(),
        "recipient_last_name": str(recipient.get("Family") or "").strip(),
    }
    source = str(
        (history_database_path().resolve().parent / "__sana__" /
         f"{tracking_code}.remote").resolve()
    )
    with _connect() as connection:
        existing = connection.execute(
            "SELECT COUNT(*) FROM registrations WHERE tracking_code = ?",
            (tracking_code,),
        ).fetchone()[0]
        if existing:
            return 0
        return _insert_registration(
            connection,
            source=source,
            source_file="دریافت‌شده از ثنا",
            ordinal=1,
            registration={
                "tracking_code": tracking_code,
                "registered_at": registered_at,
                "origin": "sana",
            },
            fields=fields,
        )


def registration_history() -> tuple[dict[str, object], ...]:
    """Return all centrally stored registrations, newest first."""

    with _connect() as connection:
        rows = connection.execute(
            """SELECT source_path, source_file, source_recipient_ordinal,
                      tracking_code, registered_at, related_declaration_number,
                      national_id, first_name, last_name, attachment_count,
                      origin
               FROM registrations
               ORDER BY registered_at DESC, id DESC"""
        ).fetchall()
    return tuple({key: row[key] for key in row.keys()} for row in rows)


def registrations_missing_report_fields() -> tuple[tuple[Path, int], ...]:
    """Return source recipients whose searchable identity is incomplete."""

    with _connect() as connection:
        rows = connection.execute(
            """SELECT source_path, source_recipient_ordinal
               FROM registrations
               WHERE origin = 'local'
                 AND (trim(first_name) = '' OR trim(last_name) = '')
               GROUP BY source_path, source_recipient_ordinal"""
        ).fetchall()
    return tuple((Path(row[0]), int(row[1])) for row in rows)


def backfill_registration_report_fields(
    source_path: Path,
    source_recipient_ordinal: int,
    fields: Mapping[str, str],
    *,
    tracking_code: str | None = None,
) -> int:
    """Fill only missing report fields from the matching reviewed source row."""

    if source_recipient_ordinal < 1:
        raise ResultPersistenceError("source_recipient_ordinal_invalid")
    values = (
        str(fields.get("related_declaration_number", "")).strip(),
        str(fields.get("recipient_national_id", "")).strip(),
        str(fields.get("recipient_first_name", "")).strip(),
        str(fields.get("recipient_last_name", "")).strip(),
    )
    tracking = str(tracking_code or "").strip()
    where = "source_path = ? AND source_recipient_ordinal = ?"
    parameters: tuple[object, ...] = (
        *values,
        str(source_path.resolve()),
        source_recipient_ordinal,
    )
    if tracking_code is not None:
        if not tracking:
            raise ResultPersistenceError("tracking_code_missing")
        where += " AND tracking_code = ?"
        parameters += (tracking,)
    with _connect() as connection:
        cursor = connection.execute(
            f"""UPDATE registrations SET
                   related_declaration_number = CASE
                       WHEN trim(related_declaration_number) = '' THEN ?
                       ELSE related_declaration_number END,
                   national_id = CASE WHEN trim(national_id) = '' THEN ?
                       ELSE national_id END,
                   first_name = CASE WHEN trim(first_name) = '' THEN ?
                       ELSE first_name END,
                   last_name = CASE WHEN trim(last_name) = '' THEN ?
                       ELSE last_name END
               WHERE {where}""",
            parameters,
        )
        return cursor.rowcount
