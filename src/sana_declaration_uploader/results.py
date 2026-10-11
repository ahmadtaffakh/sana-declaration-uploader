"""Verified result persistence scoped to one declaration input folder."""

from __future__ import annotations

import json
from pathlib import Path

from .models import DeclarationDraft, UploadResult


class ResultPersistenceError(RuntimeError):
    """Raised when a verified result cannot safely be persisted."""


def save_verified_result(
    draft: DeclarationDraft,
    result: UploadResult,
    *,
    filename: str = "result.json",
) -> Path:
    if result.status != "success" or not result.tracking_code.strip():
        raise ResultPersistenceError("unverified_result")

    source_dir = draft.source_dir.resolve()
    if not source_dir.is_dir():
        raise ResultPersistenceError("source_dir_missing")

    target = source_dir / filename
    payload = {
        "status": "success",
        "tracking_code": result.tracking_code.strip(),
    }
    if target.exists():
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ResultPersistenceError("existing_result_invalid") from exc
        if current != payload:
            raise ResultPersistenceError("existing_result_conflict")
        return target

    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    except OSError as exc:
        raise ResultPersistenceError("result_write_failed") from exc
    return target
