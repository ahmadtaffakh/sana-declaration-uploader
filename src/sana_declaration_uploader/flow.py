"""Upload orchestration with an explicit human-confirmation gate."""

from __future__ import annotations

from .browser import BrowserUploader
from .models import DeclarationDraft, UploadResult
from .validator import validate_draft


class UploadNotConfirmed(RuntimeError):
    """Raised before any browser write when the operator did not confirm."""


class InvalidDeclaration(RuntimeError):
    """Raised before browser activity when validation fails."""


def run_upload(
    draft: DeclarationDraft,
    uploader: BrowserUploader,
    *,
    confirmed: bool = False,
) -> UploadResult:
    errors = validate_draft(draft)
    if errors:
        raise InvalidDeclaration(",".join(errors))
    if not confirmed:
        raise UploadNotConfirmed("Explicit operator confirmation is required.")

    result = uploader.upload(draft)
    if result.status != "success" or not result.tracking_code.strip():
        raise RuntimeError("Upload did not return a verified tracking code.")
    return result
