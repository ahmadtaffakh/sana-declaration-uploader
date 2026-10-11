"""Domain models with no browser or file-format dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Attachment:
    path: Path
    title: str
    kind: str
    document_number: str = ""
    document_date: str = ""
    show_to_recipient: bool = True


@dataclass(frozen=True)
class DeclarationDraft:
    source_dir: Path
    fields: Mapping[str, str]
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DeclarationRequest:
    source_dir: Path
    recipient_id: str
    fields: Mapping[str, str]
    description_template: str
    parameters: Mapping[str, str]
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DeclarationBatch:
    source_dir: Path
    title: str
    requests: tuple[DeclarationRequest, ...]


@dataclass(frozen=True)
class UploadResult:
    status: str
    tracking_code: str = ""
    message: str = ""


@dataclass(frozen=True)
class RecipientCandidate:
    """Locally extracted recipient data awaiting operator review."""

    source_path: Path
    full_name: str = ""
    national_id: str = ""
    employee_code: str = ""
    birth_date: str = ""
    postal_code: str = ""
    address: str = ""
    issues: tuple[str, ...] = field(default_factory=tuple)
