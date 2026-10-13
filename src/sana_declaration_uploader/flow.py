"""Upload orchestration with an explicit human-confirmation gate."""

from __future__ import annotations

from pathlib import Path

from .browser import BrowserUploader
from .lookups import LookupResolutionError, resolve_lookup_fields
from .models import (
    Attachment,
    DeclarationBatch,
    DeclarationDraft,
    RecipientCandidate,
    UploadResult,
)
from .templating import render_request
from .validator import validate_draft


_PERSON_TYPE_DISPLAY = {
    "natural": "حقیقی",
    "legal": "حقوقی",
    "group": "گروه",
    "unknown": "نامشخص",
}
_DEFAULT_LOCATION = {
    "province": "استان تهران",
    "county": "شهرستان تهران",
    "city": "تهران",
}


class UploadNotConfirmed(RuntimeError):
    """Raised before any browser write when the operator did not confirm."""


class InvalidDeclaration(RuntimeError):
    """Raised before browser activity when validation fails."""


def prepare_drafts(batch: DeclarationBatch) -> tuple[DeclarationDraft, ...]:
    """Render and validate every request without browser activity."""

    if not batch.requests:
        raise InvalidDeclaration("recipients_missing")

    drafts = tuple(render_request(request) for request in batch.requests)
    for draft in drafts:
        _require_valid_draft(draft)
    return drafts


def _build_desktop_drafts(
    candidates: tuple[RecipientCandidate, ...],
    payloads: dict[int, dict[str, object]],
    *,
    shared: bool,
    lookup_dir: Path,
) -> tuple[DeclarationDraft, ...]:
    drafts = []
    for index, candidate in enumerate(candidates):
        payload = payloads.get(-1 if shared else index, {})
        lookup_fields = {
            "job_title": "آزاد",
            "province": candidate.province or _DEFAULT_LOCATION["province"],
            "county": candidate.county or _DEFAULT_LOCATION["county"],
            "city": candidate.city or _DEFAULT_LOCATION["city"],
        }
        try:
            lookup_fields = resolve_lookup_fields(lookup_fields, lookup_dir)
        except LookupResolutionError:
            lookup_fields = resolve_lookup_fields(
                {"job_title": "آزاد", **_DEFAULT_LOCATION}, lookup_dir
            )
        fields = {
            **lookup_fields,
            "recipient_id": f"desktop-{index + 1}",
            "data_source": "manual",
            "person_type": _PERSON_TYPE_DISPLAY.get(
                candidate.person_type, candidate.person_type
            ),
            "recipient_national_id": candidate.national_id,
            "recipient_first_name": candidate.first_name,
            "recipient_last_name": candidate.last_name,
            "recipient_father_name": candidate.father_name,
            "recipient_birth_date": candidate.birth_date,
            "recipient_gender": candidate.gender,
            "recipient_main_street": candidate.address or "-",
            "recipient_address": candidate.address,
            "recipient_postal_code": candidate.postal_code,
            "notice_number": candidate.notice_number,
            "related_declaration_number": candidate.related_declaration_number,
            "related_declaration_date": candidate.related_declaration_date,
            "declaration_subject": str(payload.get("subject", "")),
            "declaration_text": str(payload.get("description", "")),
        }
        attachments = tuple(
            Attachment(
                path=Path(path),
                title=Path(path).stem,
                kind=str(payload.get("attachment_kind", "")),
                document_number=str(payload.get("attachment_number", "")),
                document_date=str(payload.get("attachment_date", "")),
            )
            for path in payload.get("attachments", ())
        )
        drafts.append(
            DeclarationDraft(
                source_dir=candidate.source_path.resolve().parent,
                fields=fields,
                attachments=attachments,
            )
        )
    return tuple(drafts)


def run_upload(
    draft: DeclarationDraft,
    uploader: BrowserUploader,
    *,
    confirmed: bool = False,
) -> UploadResult:
    return run_upload_batch((draft,), uploader, confirmed=confirmed)[0]


def run_upload_batch(
    drafts: tuple[DeclarationDraft, ...],
    uploader: BrowserUploader,
    *,
    confirmed: bool = False,
) -> tuple[UploadResult, ...]:
    """Validate first, then upload each draft once with one shared uploader."""

    if not drafts:
        raise InvalidDeclaration("recipients_missing")
    for draft in drafts:
        _require_valid_draft(draft)
    if not confirmed:
        raise UploadNotConfirmed("Explicit operator confirmation is required.")

    results = []
    for draft in drafts:
        result = uploader.upload(draft)
        if result.status != "success" or not result.tracking_code.strip():
            raise RuntimeError("Upload did not return a verified tracking code.")
        results.append(result)
    return tuple(results)


def _require_valid_draft(draft: DeclarationDraft) -> None:
    errors = validate_draft(draft)
    if errors:
        raise InvalidDeclaration(",".join(errors))
