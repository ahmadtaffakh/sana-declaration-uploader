"""Fail-closed validation independent from browser automation."""

from __future__ import annotations

from pathlib import Path

from .models import DeclarationDraft


def validate_draft(draft: DeclarationDraft) -> tuple[str, ...]:
    errors: list[str] = []
    source_root = draft.source_dir.resolve()

    if not source_root.is_dir():
        errors.append("source_dir_missing")
    if not draft.fields:
        errors.append("fields_missing")

    data_source = draft.fields.get("data_source", "").strip()
    if data_source:
        _validate_contract_fields(draft, errors, data_source)

    seen_paths: set[Path] = set()
    for attachment in draft.attachments:
        path = attachment.path.resolve()
        if path in seen_paths:
            errors.append(f"attachment_duplicate:{path.name}")
            continue
        seen_paths.add(path)

        if not path.is_file():
            errors.append(f"attachment_missing:{path.name}")
        if not attachment.title.strip():
            errors.append(f"attachment_title_missing:{path.name}")
        if not attachment.kind.strip():
            errors.append(f"attachment_kind_missing:{path.name}")

    return tuple(errors)


def _validate_contract_fields(
    draft: DeclarationDraft, errors: list[str], data_source: str
) -> None:
    fields = draft.fields
    for name in ("recipient_id", "declaration_subject", "declaration_text"):
        if not fields.get(name, "").strip():
            errors.append(f"field_missing:{name}")

    if data_source == "notice":
        if not fields.get("notice_number", "").strip():
            errors.append("field_missing:notice_number")
        if fields.get("notice_resolved", "").strip().lower() != "true":
            errors.append("notice_not_resolved")
    elif data_source == "manual":
        for name in (
            "recipient_first_name",
            "recipient_last_name",
            "job_title",
            "province",
            "county",
            "city",
        ):
            if not fields.get(name, "").strip():
                errors.append(f"field_missing:{name}")
    else:
        errors.append(f"data_source_invalid:{data_source}")

    for prefix in ("job", "province", "county", "city", "village"):
        label_name = "job_title" if prefix == "job" else prefix
        if not fields.get(label_name, "").strip():
            continue
        for suffix in ("id", "code"):
            if not fields.get(f"{prefix}_{suffix}", "").strip():
                errors.append(f"lookup_unresolved:{prefix}_{suffix}")
