"""Parser for the versioned, operator-facing Word input template."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import re

from docx import Document

from .lookups import normalize_label, resolve_lookup_fields
from .models import Attachment, DeclarationBatch, DeclarationRequest


class TemplateContractNotConfigured(RuntimeError):
    """Raised when the supplied Word file does not match the contract."""


_METADATA_HEADERS = ("نام فیلد", "مقدار")
_TEMPLATE_HEADERS = ("شناسه الگو", "عنوان الگو", "شرح")
_RECIPIENT_HEADERS = (
    "شناسه مخاطب",
    "منبع اطلاعات",
    "شماره ابلاغیه",
    "شماره اظهارنامه مرتبط",
    "شناسه الگو",
    "شرح اختصاصی",
)
_FIELD_HEADERS = ("شناسه مخاطب", "نام فیلد", "مقدار")
_PARAMETER_HEADERS = ("شناسه مخاطب", "نام پارامتر", "مقدار")
_ATTACHMENT_HEADERS_V1 = (
    "شناسه پیوست",
    "مسیر نسبی",
    "عنوان پیوست",
    "نوع پیوست",
    "دامنه",
    "شناسه هدف",
)
_ATTACHMENT_HEADERS_V2 = (
    "شناسه پیوست",
    "مسیر نسبی",
    "عنوان پیوست",
    "نوع پیوست",
    "شماره مدرک",
    "تاریخ مدرک",
    "نمایش برای مخاطب",
    "دامنه",
    "شناسه هدف",
)

_METADATA_FIELDS = {
    "نسخه قالب": "template_version",
    "عنوان بسته": "batch_title",
    "موضوع اظهارنامه": "declaration_subject",
    "شناسه الگوی پیش فرض": "default_template_id",
    "نام واحد قضایی": "court_unit_name",
    "کد واحد قضایی": "court_unit_code",
}
_MANUAL_FIELDS = {
    "کد ملی": "recipient_national_id",
    "شماره شناسنامه": "recipient_identity_no",
    "کد پستی": "recipient_postal_code",
    "تاریخ تولد": "recipient_birth_date",
    "جنسیت": "recipient_gender",
    "نام": "recipient_first_name",
    "نام خانوادگی": "recipient_last_name",
    "نام پدر": "recipient_father_name",
    "شغل": "job_title",
    "استان": "province",
    "شهرستان": "county",
    "شهر": "city",
    "روستا": "village",
    "خیابان اصلی": "recipient_main_street",
    "نشانی": "recipient_address",
}
_SOURCE_TYPES = {"ابلاغیه": "notice", "دستی": "manual"}
_ATTACHMENT_SCOPES = {
    "مشترک": "common",
    "الگو": "template",
    "مخاطب": "recipient",
}
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_PARAMETER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def parse_declaration_folder(
    source_dir: Path, *, lookup_dir: Path | None = None
) -> DeclarationBatch:
    source_root = source_dir.resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"Input folder does not exist: {source_dir}")

    document_path = source_root / "declaration.docx"
    if not document_path.is_file():
        raise FileNotFoundError(f"Word template does not exist: {document_path}")

    document = _load_document(document_path)
    (
        metadata,
        templates,
        recipients,
        manual_fields,
        parameters,
        attachment_rules,
    ) = _parse_contract(document, source_root)
    requests = _build_requests(
        source_root=source_root,
        metadata=metadata,
        templates=templates,
        recipients=recipients,
        manual_fields=manual_fields,
        parameters=parameters,
        attachment_rules=attachment_rules,
        lookup_dir=lookup_dir,
    )
    return DeclarationBatch(
        source_dir=source_root,
        title=metadata.get("batch_title", ""),
        requests=requests,
    )


def _load_document(document_path: Path) -> object:
    try:
        return Document(document_path)
    except Exception as exc:
        raise TemplateContractNotConfigured("word_document_invalid") from exc


def _parse_contract(document: object, source_root: Path) -> tuple:
    metadata = _parse_metadata(_rows(document, _METADATA_HEADERS))
    template_version = metadata.get("template_version")
    if template_version not in {"1", "2"}:
        raise TemplateContractNotConfigured("template_version_unsupported")
    if not metadata.get("declaration_subject"):
        raise TemplateContractNotConfigured("declaration_subject_missing")

    templates = _parse_templates(_rows(document, _TEMPLATE_HEADERS))
    recipients = _parse_recipients(_rows(document, _RECIPIENT_HEADERS))
    manual_fields = _parse_key_values(
        _rows(document, _FIELD_HEADERS),
        key_map=_MANUAL_FIELDS,
        identifier_label="field",
    )
    parameters = _parse_key_values(
        _rows(document, _PARAMETER_HEADERS),
        key_pattern=_PARAMETER,
        identifier_label="parameter",
        allow_common=True,
    )
    attachment_rules = _parse_attachments(
        _rows(
            document,
            _ATTACHMENT_HEADERS_V2
            if template_version == "2"
            else _ATTACHMENT_HEADERS_V1,
        ),
        source_root,
        template_version=template_version,
    )

    recipient_ids = {recipient["recipient_id"] for recipient in recipients}
    _reject_unknown_targets(manual_fields, recipient_ids, "manual_field")
    _reject_unknown_targets(parameters, recipient_ids, "parameter", allow_common=True)
    _validate_attachment_targets(
        attachment_rules, recipient_ids, set(templates)
    )
    return (
        metadata,
        templates,
        recipients,
        manual_fields,
        parameters,
        attachment_rules,
    )


def _build_requests(
    *,
    source_root: Path,
    metadata: dict[str, str],
    templates: dict[str, str],
    recipients: list[dict[str, str]],
    manual_fields: dict[str, dict[str, str]],
    parameters: dict[str, dict[str, str]],
    attachment_rules: list[tuple[str, str, Attachment]],
    lookup_dir: Path | None,
) -> tuple[DeclarationRequest, ...]:
    default_template_id = metadata.get("default_template_id", "")
    requests: list[DeclarationRequest] = []
    for recipient in recipients:
        requests.append(
            _build_request(
                source_root=source_root,
                metadata=metadata,
                templates=templates,
                recipient=recipient,
                manual_fields=manual_fields,
                parameters=parameters,
                attachment_rules=attachment_rules,
                default_template_id=default_template_id,
                lookup_dir=lookup_dir,
            )
        )
    if not requests:
        raise TemplateContractNotConfigured("recipients_missing")
    return tuple(requests)


def _build_request(
    *,
    source_root: Path,
    metadata: dict[str, str],
    templates: dict[str, str],
    recipient: dict[str, str],
    manual_fields: dict[str, dict[str, str]],
    parameters: dict[str, dict[str, str]],
    attachment_rules: list[tuple[str, str, Attachment]],
    default_template_id: str,
    lookup_dir: Path | None,
) -> DeclarationRequest:
    recipient_id = recipient["recipient_id"]
    template_id = recipient["template_id"] or default_template_id
    description = recipient["description_override"]
    if not description:
        if not template_id or template_id not in templates:
            raise TemplateContractNotConfigured(
                f"description_template_missing:{recipient_id}"
            )
        description = templates[template_id]
    if recipient["data_source"] == "notice" and not recipient["notice_number"]:
        raise TemplateContractNotConfigured(
            f"notice_number_missing:{recipient_id}"
        )

    fields = {
        "batch_title": metadata.get("batch_title", ""),
        "recipient_id": recipient_id,
        "data_source": recipient["data_source"],
        "notice_number": recipient["notice_number"],
        "related_declaration_number": recipient["related_declaration_number"],
        "description_template_id": template_id,
        "declaration_subject": metadata["declaration_subject"],
        "court_unit_name": metadata.get("court_unit_name", ""),
        "court_unit_code": metadata.get("court_unit_code", ""),
        **manual_fields.get(recipient_id, {}),
    }
    if lookup_dir is not None:
        fields = resolve_lookup_fields(fields, lookup_dir.resolve())

    request_parameters = dict(parameters.get("", {}))
    request_parameters.update(parameters.get(recipient_id, {}))
    return DeclarationRequest(
        source_dir=source_root,
        recipient_id=recipient_id,
        fields=fields,
        description_template=description,
        parameters=request_parameters,
        attachments=_select_attachments(
            attachment_rules, recipient_id, template_id
        ),
    )


def _rows(document: object, expected_headers: tuple[str, ...]) -> list[list[str]]:
    expected = tuple(normalize_label(value) for value in expected_headers)
    matches = []
    for table in document.tables:
        if not table.rows:
            continue
        headers = tuple(
            normalize_label(cell.text) for cell in table.rows[0].cells
        )
        if headers == expected:
            matches.append(table)
    if len(matches) != 1:
        joined = ",".join(expected_headers)
        raise TemplateContractNotConfigured(f"table_contract_invalid:{joined}")

    rows = []
    for row in matches[0].rows[1:]:
        values = [cell.text.strip() for cell in row.cells]
        if any(values):
            rows.append(values)
    return rows


def _parse_metadata(rows: Iterable[list[str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    normalized_map = {
        normalize_label(label): name for label, name in _METADATA_FIELDS.items()
    }
    for label, value in rows:
        name = normalized_map.get(normalize_label(label))
        if name is None:
            raise TemplateContractNotConfigured(f"metadata_field_unknown:{label}")
        if name in result:
            raise TemplateContractNotConfigured(f"metadata_field_duplicate:{label}")
        result[name] = value.strip()
    return result


def _parse_templates(rows: Iterable[list[str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for template_id, _title, description in rows:
        template_id = template_id.strip()
        if not _IDENTIFIER.fullmatch(template_id):
            raise TemplateContractNotConfigured(
                f"template_id_invalid:{template_id}"
            )
        if template_id in result:
            raise TemplateContractNotConfigured(
                f"template_id_duplicate:{template_id}"
            )
        if not description.strip():
            raise TemplateContractNotConfigured(
                f"template_description_missing:{template_id}"
            )
        result[template_id] = description.strip()
    return result


def _parse_recipients(rows: Iterable[list[str]]) -> list[dict[str, str]]:
    result = []
    seen = set()
    for values in rows:
        recipient_id, source, notice, related, template_id, override = values
        recipient_id = recipient_id.strip()
        if not _IDENTIFIER.fullmatch(recipient_id):
            raise TemplateContractNotConfigured(
                f"recipient_id_invalid:{recipient_id}"
            )
        if recipient_id in seen:
            raise TemplateContractNotConfigured(
                f"recipient_id_duplicate:{recipient_id}"
            )
        seen.add(recipient_id)
        source_type = _SOURCE_TYPES.get(normalize_label(source))
        if source_type is None:
            raise TemplateContractNotConfigured(
                f"recipient_source_invalid:{recipient_id}"
            )
        result.append(
            {
                "recipient_id": recipient_id,
                "data_source": source_type,
                "notice_number": notice.strip(),
                "related_declaration_number": related.strip(),
                "template_id": template_id.strip(),
                "description_override": override.strip(),
            }
        )
    return result


def _parse_key_values(
    rows: Iterable[list[str]],
    *,
    identifier_label: str,
    key_map: dict[str, str] | None = None,
    key_pattern: re.Pattern[str] | None = None,
    allow_common: bool = False,
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    normalized_map = {
        normalize_label(label): name for label, name in (key_map or {}).items()
    }
    for owner_id, raw_key, value in rows:
        owner_id = owner_id.strip()
        if not owner_id and not allow_common:
            raise TemplateContractNotConfigured(
                f"{identifier_label}_owner_missing"
            )
        if key_map is not None:
            key = normalized_map.get(normalize_label(raw_key))
        else:
            stripped_key = raw_key.strip()
            key = (
                stripped_key
                if key_pattern and key_pattern.fullmatch(stripped_key)
                else None
            )
        if key is None:
            raise TemplateContractNotConfigured(
                f"{identifier_label}_name_invalid:{raw_key}"
            )
        owner_values = result.setdefault(owner_id, {})
        if key in owner_values:
            raise TemplateContractNotConfigured(
                f"{identifier_label}_duplicate:{owner_id}:{key}"
            )
        owner_values[key] = value.strip()
    return result


def _parse_attachments(
    rows: Iterable[list[str]], source_root: Path, *, template_version: str
) -> list[tuple[str, str, Attachment]]:
    result = []
    seen_ids = set()
    for values in rows:
        if template_version == "2":
            (
                attachment_id,
                relative,
                title,
                kind,
                document_number,
                document_date,
                show_to_recipient,
                scope,
                target_id,
            ) = values
            normalized_visibility = normalize_label(show_to_recipient)
            if normalized_visibility not in {"بله", "خیر"}:
                raise TemplateContractNotConfigured(
                    f"attachment_visibility_invalid:{attachment_id.strip()}"
                )
            visible = normalized_visibility == "بله"
        else:
            attachment_id, relative, title, kind, scope, target_id = values
            document_number = ""
            document_date = ""
            visible = True
        attachment_id = attachment_id.strip()
        if not _IDENTIFIER.fullmatch(attachment_id):
            raise TemplateContractNotConfigured(
                f"attachment_id_invalid:{attachment_id}"
            )
        if attachment_id in seen_ids:
            raise TemplateContractNotConfigured(
                f"attachment_id_duplicate:{attachment_id}"
            )
        seen_ids.add(attachment_id)
        relative_path = Path(relative.strip())
        if not relative.strip() or relative_path.is_absolute():
            raise TemplateContractNotConfigured(
                f"attachment_path_invalid:{attachment_id}"
            )
        path = (source_root / relative_path).resolve()
        try:
            path.relative_to(source_root)
        except ValueError as exc:
            raise TemplateContractNotConfigured(
                f"attachment_outside_source:{attachment_id}"
            ) from exc
        scope_name = _ATTACHMENT_SCOPES.get(normalize_label(scope))
        if scope_name is None:
            raise TemplateContractNotConfigured(
                f"attachment_scope_invalid:{attachment_id}"
            )
        target_id = target_id.strip()
        if scope_name == "common" and target_id:
            raise TemplateContractNotConfigured(
                f"attachment_target_unexpected:{attachment_id}"
            )
        if scope_name != "common" and not target_id:
            raise TemplateContractNotConfigured(
                f"attachment_target_missing:{attachment_id}"
            )
        result.append(
            (
                scope_name,
                target_id,
                Attachment(
                    path=path,
                    title=title.strip(),
                    kind=kind.strip(),
                    document_number=document_number.strip(),
                    document_date=document_date.strip(),
                    show_to_recipient=visible,
                ),
            )
        )
    return result


def _select_attachments(
    rules: Iterable[tuple[str, str, Attachment]],
    recipient_id: str,
    template_id: str,
) -> tuple[Attachment, ...]:
    selected = []
    for scope, target_id, attachment in rules:
        if scope == "common":
            selected.append(attachment)
        elif scope == "template" and target_id == template_id:
            selected.append(attachment)
        elif scope == "recipient" and target_id == recipient_id:
            selected.append(attachment)
    return tuple(selected)


def _reject_unknown_targets(
    values: dict[str, dict[str, str]],
    recipient_ids: set[str],
    label: str,
    *,
    allow_common: bool = False,
) -> None:
    for target_id in values:
        if allow_common and target_id == "":
            continue
        if target_id not in recipient_ids:
            raise TemplateContractNotConfigured(
                f"{label}_recipient_unknown:{target_id}"
            )


def _validate_attachment_targets(
    rules: Iterable[tuple[str, str, Attachment]],
    recipient_ids: set[str],
    template_ids: set[str],
) -> None:
    for scope, target_id, attachment in rules:
        if scope == "recipient" and target_id not in recipient_ids:
            raise TemplateContractNotConfigured(
                f"attachment_recipient_unknown:{attachment.path.name}"
            )
        if scope == "template" and target_id not in template_ids:
            raise TemplateContractNotConfigured(
                f"attachment_template_unknown:{attachment.path.name}"
            )
