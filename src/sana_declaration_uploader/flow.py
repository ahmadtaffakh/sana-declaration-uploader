"""Upload orchestration with an explicit human-confirmation gate."""

from __future__ import annotations

from .browser import BrowserUploader
from .models import DeclarationBatch, DeclarationDraft, UploadResult
from .templating import render_request
from .validator import validate_draft


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


def run_upload(
    draft: DeclarationDraft,
    uploader: BrowserUploader,
    *,
    confirmed: bool = False,
) -> UploadResult:
    _require_valid_draft(draft)
    if not confirmed:
        raise UploadNotConfirmed("Explicit operator confirmation is required.")

    result = uploader.upload(draft)
    if result.status != "success" or not result.tracking_code.strip():
        raise RuntimeError("Upload did not return a verified tracking code.")
    return result


def _require_valid_draft(draft: DeclarationDraft) -> None:
    errors = validate_draft(draft)
    if errors:
        raise InvalidDeclaration(",".join(errors))
