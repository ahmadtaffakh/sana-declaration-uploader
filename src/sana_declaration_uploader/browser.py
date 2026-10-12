"""Browser boundaries and the observed fail-closed attachment workflow."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Protocol

from .models import Attachment, DeclarationDraft, UploadResult


class BrowserUploader(Protocol):
    def upload(self, draft: DeclarationDraft) -> UploadResult: ...


class AttachmentPage(Protocol):
    """Site-specific operations implemented by a future live adapter."""

    def register_attachment(self, attachment: Attachment) -> None: ...

    def upload_attachment_file(self, attachment: Attachment) -> None: ...

    def confirm_attachment_authenticity(self, attachment: Attachment) -> None: ...


class AttachmentWorkflowError(ValueError):
    """Raised before site writes when attachment metadata is incomplete."""


_DOCUMENT_DATE = re.compile(r"^\d{4}/\d{2}/\d{2}$")


def run_attachment_workflow(
    attachments: Iterable[Attachment], page: AttachmentPage
) -> None:
    """Register, upload and certify each attachment exactly once.

    Browser exceptions deliberately propagate. There is no retry, re-upload or
    navigation back to the attachments page after an ambiguous response.
    """

    attachment_list = validate_attachments_for_upload(attachments)
    for attachment in attachment_list:
        page.register_attachment(attachment)
        page.upload_attachment_file(attachment)
        page.confirm_attachment_authenticity(attachment)


def validate_attachments_for_upload(
    attachments: Iterable[Attachment],
) -> tuple[Attachment, ...]:
    """Validate every attachment before the first remote attachment write."""

    attachment_list = tuple(attachments)
    for attachment in attachment_list:
        _validate_site_attachment(attachment)
    return attachment_list


def _validate_site_attachment(attachment: Attachment) -> None:
    if not attachment.document_number.strip():
        raise AttachmentWorkflowError(
            f"attachment_document_number_missing:{attachment.path.name}"
        )
    if not _DOCUMENT_DATE.fullmatch(attachment.document_date.strip()):
        raise AttachmentWorkflowError(
            f"attachment_document_date_invalid:{attachment.path.name}"
        )
