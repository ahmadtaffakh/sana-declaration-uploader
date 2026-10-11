"""Safe one-pass rendering for declaration descriptions."""

from __future__ import annotations

import re
from typing import Mapping

from .models import DeclarationDraft, DeclarationRequest


class DescriptionRenderError(ValueError):
    """Raised when a description placeholder cannot be resolved exactly."""


_PLACEHOLDER = re.compile(
    r"\{\{\s*(recipient|notice|param)\.([A-Za-z][A-Za-z0-9_]*)\s*\}\}"
)
_ANY_PLACEHOLDER = re.compile(r"\{\{.*?\}\}", re.DOTALL)


def render_request(
    request: DeclarationRequest,
    resolved_fields: Mapping[str, str] | None = None,
) -> DeclarationDraft:
    fields = _merge_fields(request.fields, resolved_fields or {})
    contexts = {
        "recipient": _prefixed_fields(fields, "recipient_"),
        "notice": _prefixed_fields(fields, "notice_"),
        "param": dict(request.parameters),
    }
    first_name = contexts["recipient"].get("first_name", "").strip()
    last_name = contexts["recipient"].get("last_name", "").strip()
    contexts["recipient"]["full_name"] = " ".join(
        value for value in (first_name, last_name) if value
    )

    source = request.description_template
    for raw_placeholder in _ANY_PLACEHOLDER.findall(source):
        if _PLACEHOLDER.fullmatch(raw_placeholder) is None:
            raise DescriptionRenderError(
                f"placeholder_invalid:{raw_placeholder}"
            )

    def replace(match: re.Match[str]) -> str:
        namespace, name = match.groups()
        value = contexts[namespace].get(name, "")
        if not str(value).strip():
            raise DescriptionRenderError(
                f"placeholder_unresolved:{namespace}.{name}"
            )
        return str(value)

    rendered = _PLACEHOLDER.sub(replace, source).strip()
    if not rendered:
        raise DescriptionRenderError("description_empty")

    fields["declaration_text"] = rendered
    return DeclarationDraft(
        source_dir=request.source_dir,
        fields=fields,
        attachments=request.attachments,
    )


def _merge_fields(
    original: Mapping[str, str], resolved: Mapping[str, str]
) -> dict[str, str]:
    result = dict(original)
    for key, value in resolved.items():
        old_value = result.get(key, "").strip()
        new_value = str(value).strip()
        if old_value and new_value and old_value != new_value:
            raise DescriptionRenderError(f"resolved_field_conflict:{key}")
        if new_value:
            result[key] = new_value
    return result


def _prefixed_fields(fields: Mapping[str, str], prefix: str) -> dict[str, str]:
    return {
        key.removeprefix(prefix): value
        for key, value in fields.items()
        if key.startswith(prefix)
    }
