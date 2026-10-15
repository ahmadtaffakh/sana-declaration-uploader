"""Isolated client for the observed temporary petition-registration API."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import json
import re
from pathlib import Path
from typing import Protocol

from .models import DeclarationDraft


SET_PETITION_DATA_URL = (
    "https://person.adliran.ir/Petitions/Home/SetPetitionData"
)
_TRACKING_CODE = re.compile(r"^\d{16}$")
_PETITION_NUMBER = re.compile(r"^\d+$")
_PETITION_ID = re.compile(r"^(?:[0-9a-fA-F]{32}|"
                          r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}"
                          r"[0-9a-fA-F]{12})$")


class _RequestContext(Protocol):
    def post(self, url: str, **kwargs: object) -> object: ...


@dataclass(frozen=True)
class SetPetitionDataResult:
    tracking_code: str
    petition_number: str
    petition_id: str


class SetPetitionDataError(RuntimeError):
    """Sanitized failure from a single SetPetitionData write attempt."""


class SetPetitionDataClient:
    """Call SetPetitionData once using a caller-provided authenticated context.

    Pass the request context belonging to the logged-in browser context so its
    session cookies are reused. The payload is the portal's inner petition
    model; this class wraps it in the observed ``data``/``isAuthenticate``
    envelope and deliberately does not retry ambiguous writes.
    """

    def __init__(self, request_context: _RequestContext) -> None:
        self.request_context = request_context

    def submit(
        self, petition_data: Mapping[str, object]
    ) -> SetPetitionDataResult:
        envelope = {
            "data": json.dumps(
                petition_data,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "isAuthenticate": True,
        }
        return self.submit_envelope(envelope)

    def submit_envelope(
        self, envelope: Mapping[str, object]
    ) -> SetPetitionDataResult:
        if envelope.get("isAuthenticate") is not True:
            raise SetPetitionDataError("set_petition_data_envelope_invalid")
        inner_data = envelope.get("data")
        if not isinstance(inner_data, str):
            raise SetPetitionDataError("set_petition_data_envelope_invalid")
        try:
            parsed_data = json.loads(inner_data)
        except (TypeError, ValueError):
            raise SetPetitionDataError(
                "set_petition_data_envelope_invalid"
            ) from None
        if not isinstance(parsed_data, dict):
            raise SetPetitionDataError("set_petition_data_envelope_invalid")

        body = json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self._post_body(body)

    def _post_body(self, body: str) -> SetPetitionDataResult:
        try:
            response = self.request_context.post(
                SET_PETITION_DATA_URL,
                data=body,
                headers={
                    "content-type": "application/json;charset=UTF-8",
                    "accept": "application/json, text/plain, */*",
                    "x-requested-with": "XMLHttpRequest",
                    "origin": "https://person.adliran.ir",
                    "referer": "https://person.adliran.ir/LegalPerson/Index",
                },
            )
        except Exception:
            raise SetPetitionDataError(
                "set_petition_data_transport_ambiguous"
            ) from None

        status = getattr(response, "status", None)
        if not isinstance(status, int) or not 200 <= status < 300:
            raise SetPetitionDataError("set_petition_data_http_failed")
        try:
            raw_result = response.text().strip()
        except Exception:
            raise SetPetitionDataError(
                "set_petition_data_response_unreadable"
            ) from None
        return parse_set_petition_data_result(raw_result)


def submit_one_petition_data(
    page: object, petition_data: Mapping[str, object]
) -> SetPetitionDataResult:
    """Submit one petition through the authenticated page's cookie context.

    This intentionally accepts one portal model, not a batch or draft, and
    makes exactly one write attempt. The page must already be authenticated.
    """

    try:
        request_context = page.context.request
    except Exception:
        raise SetPetitionDataError(
            "set_petition_data_authenticated_context_unavailable"
        ) from None
    return SetPetitionDataClient(request_context).submit(petition_data)


def load_setpetitiondata_template(path: Path) -> dict[str, object]:
    """Load the locally stored request captured from the portal."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict) and isinstance(raw.get("data"), str):
            raw = json.loads(raw["data"])
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise SetPetitionDataError("set_petition_data_template_invalid") from None
    if not isinstance(raw, dict) or not isinstance(
        raw.get("theJSSPetitionPerson"), list
    ):
        raise SetPetitionDataError("set_petition_data_template_invalid")
    recipients = [
        person for person in raw["theJSSPetitionPerson"]
        if isinstance(person, dict) and str(person.get("RoleType")) == "2"
    ]
    if len(recipients) != 1 or not isinstance(raw.get("Description"), str):
        raise SetPetitionDataError("set_petition_data_template_invalid")
    return raw


def save_setpetitiondata_debug_text(
    petition_data: Mapping[str, object], path: Path
) -> tuple[Path, Path]:
    """Save the exact request body and a PowerShell curl command for debugging."""

    envelope = {
        "data": json.dumps(
            petition_data,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "isAuthenticate": True,
    }
    body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    command_path = path.with_name(f"{path.stem}.curl.txt")
    console_path = path.with_name(f"{path.stem}.console.txt")
    console_script = "\n".join(
        (
            "(async () => {",
            "  const response = await fetch(",
            f"    {json.dumps(SET_PETITION_DATA_URL)},",
            "    {",
            "      method: 'POST',",
            "      credentials: 'include',",
            "      headers: {",
            "        'accept': 'application/json, text/plain, */*',",
            "        'content-type': 'application/json;charset=UTF-8',",
            "        'x-requested-with': 'XMLHttpRequest',",
            "      },",
            f"      body: {json.dumps(body, ensure_ascii=False)},",
            "    }",
            "  );",
            "  const responseText = await response.text();",
            "  console.log('HTTP status:', response.status);",
            "  console.log('Response body:', responseText);",
            "})().catch((error) => console.error('Request failed:', error));",
            "",
        )
    )
    curl_command = "\n".join(
        (
            "curl.exe --url 'https://person.adliran.ir/Petitions/Home/SetPetitionData' `",
            "  -H 'accept: application/json, text/plain, */*' `",
            "  -H 'content-type: application/json;charset=UTF-8' `",
            "  -H 'x-requested-with: XMLHttpRequest' `",
            "  -H 'origin: https://person.adliran.ir' `",
            "  -H 'referer: https://person.adliran.ir/LegalPerson/Index' `",
            "  -b '<PASTE_CURRENT_SESSION_COOKIE_HERE>' `",
            f"  --data-binary '@{path.resolve()}'",
            "",
        )
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body + "\n", encoding="utf-8")
        command_path.write_text(curl_command, encoding="utf-8")
        console_path.write_text(console_script, encoding="utf-8")
    except OSError:
        raise SetPetitionDataError(
            "set_petition_data_debug_text_write_failed"
        ) from None
    return path, command_path


def build_setpetitiondata_payload(
    template: Mapping[str, object], draft: DeclarationDraft
) -> dict[str, object]:
    """Build a payload using the field mapping observed in a successful portal request.

    ``RelatedNo`` uses the manually verified ``12345678`` fallback when the
    template is blank or contains its known ``123456`` placeholder; callers
    should supply the actual related number when one is available.
    """

    payload = deepcopy(dict(template))
    people = payload.get("theJSSPetitionPerson")
    if not isinstance(people, list):
        raise SetPetitionDataError("set_petition_data_template_invalid")
    recipients = [
        person for person in people
        if isinstance(person, dict) and str(person.get("RoleType")) == "2"
    ]
    if len(recipients) != 1:
        raise SetPetitionDataError("set_petition_data_recipient_not_unique")

    fields = draft.fields
    required = (
        "province_code", "county_code", "city_code",
        "province", "county", "city",
    )
    if any(not fields.get(name, "").strip() for name in required):
        raise SetPetitionDataError("set_petition_data_location_code_missing")
    recipient = recipients[0]
    replacements = {
        "Name": fields.get("recipient_first_name", ""),
        "Family": fields.get("recipient_last_name", ""),
        "FatherName": fields.get("recipient_father_name", ""),
        "IdentityNo": fields.get("recipient_national_id", ""),
        "BasicAvenue": fields.get("recipient_main_street", "")
        or fields["city"],
        "HomeAddress": fields.get("recipient_address", ""),
        "PostallCode": fields.get("recipient_postal_code", ""),
        # Portal payload uses State for province and Province for county.
        "StateId": fields["province_code"],
        "StateTitle": fields["province"],
        "ProvinceId": fields["county_code"],
        "ProvinceTitle": fields["county"],
        "CityId": fields["city_code"],
        "CityTitle": fields["city"],
    }
    required_fields = (
        "Name", "Family", "BasicAvenue", "HomeAddress",
        "ProvinceId", "ProvinceTitle", "StateId", "StateTitle",
        "CityId", "CityTitle",
    )
    if any(name not in recipient for name in required_fields):
        raise SetPetitionDataError("set_petition_data_template_invalid")
    recipient.update(replacements)

    # Match the observed HTML wrapper expected by the endpoint.
    payload["Description"] = f"<p>{fields.get('declaration_text', '')}</p>"
    payload["HstCaseSubjectCode"] = payload.get("HstCaseSubjectCode") or "0024"
    for name in ("MasterJudgeUnitId", "MasterJudgeUnitName"):
        if payload.get(name) == "":
            payload[name] = None

    # These fields are reset by the portal for the initial relief row.
    reliefs = payload.get("theJSSPetitionRelief")
    if isinstance(reliefs, list):
        for relief in reliefs:
            if isinstance(relief, dict):
                relief["RowNumber"] = 0
                relief["PersonNumber"] = 0

    related_no = payload.get("RelatedNo")
    if related_no in (None, "", "123456"):
        payload["RelatedNo"] = "12345678"
    return payload


def parse_set_petition_data_result(raw: str) -> SetPetitionDataResult:
    """Parse the observed tracking-code;petition-number;petition-id result."""

    parts = raw.strip().split(";")
    if len(parts) != 3:
        raise SetPetitionDataError("set_petition_data_response_invalid")
    tracking_code, petition_number, petition_id = (
        part.strip() for part in parts
    )
    if not (
        _TRACKING_CODE.fullmatch(tracking_code)
        and _PETITION_NUMBER.fullmatch(petition_number)
        and _PETITION_ID.fullmatch(petition_id)
    ):
        raise SetPetitionDataError("set_petition_data_response_invalid")
    return SetPetitionDataResult(
        tracking_code=tracking_code,
        petition_number=petition_number,
        petition_id=petition_id,
    )
