"""Authenticated API client for registering SANA petition attachments."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import mimetypes
from pathlib import Path
from typing import Callable, Mapping, Protocol


GET_VALID_ATTACHMENT_URL = (
    "https://person.adliran.ir/Offices/GetValidAttachment"
)
ADD_DOCUMENT_URL = (
    "https://person.adliran.ir/Offices/AddDocumentToJSSPetitionDocument"
)
ADD_DOCUMENT_FILE_URL = (
    "https://person.adliran.ir/Offices/AddDocumentFileToJSSPetitionDocumentFile"
)
SET_ACCEPT_STATE_URL = (
    "https://person.adliran.ir/Offices/SetJssPetitionDocumentToAcceptState"
)


@dataclass(frozen=True)
class AttachmentType:
    """Attachment type returned by ``GetValidAttachment``."""

    id: str
    code: str
    title: str
    description_template: str
    other_data_definition: Mapping[str, object]
    state: int


@dataclass(frozen=True)
class AttachmentResult:
    """Identifiers returned after the attachment is accepted."""

    document_id: str
    document_file_id: str


class AttachmentApiError(RuntimeError):
    """Sanitized failure from an attachment API call."""


class _BrowserPage(Protocol):
    """Browser surface needed by the API client."""

    context: object

    def evaluate(self, expression: str, argument: object) -> object: ...


class AttachmentClient:
    """Register attachments for an already-created petition, without retries."""

    def __init__(
        self,
        page: _BrowserPage,
        petition_no: str,
        jss_petition_id: str,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.page = page
        self.petition_no = petition_no.strip()
        if len(self.petition_no) != 16 or not self.petition_no.isdigit():
            raise AttachmentApiError("attachment_petition_number_invalid")
        self.jss_petition_id = jss_petition_id.strip()
        self.progress = progress

    # Step 0: fetch the portal-approved types for this petition's reliefs.
    def get_valid_attachment_types(
        self,
        reliefs: list[Mapping[str, object]],
        petition_type_code: str = "110",
        page_size: int = 10,
    ) -> list[AttachmentType]:
        """Return attachment types permitted for the supplied reliefs."""

        if not reliefs or not petition_type_code.strip() or page_size < 1:
            raise AttachmentApiError("attachment_lookup_request_invalid")
        self._report("شروع دریافت نوع‌های مجاز پیوست")
        payload = json.dumps(
            {
                "theJSSPetitionRelief": reliefs,
                "petitionTypeCode": petition_type_code,
                "petitionNo": self.petition_no,
                "pageSize": page_size,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        response = self._fetch_json(
            GET_VALID_ATTACHMENT_URL,
            payload,
            "attachment_lookup",
        )
        if not isinstance(response, dict):
            raise AttachmentApiError("attachment_lookup_response_invalid")
        envelope = response.get("AttachmentTypeList")
        values = envelope.get("$values") if isinstance(envelope, dict) else None
        if not isinstance(values, list):
            raise AttachmentApiError("attachment_lookup_response_invalid")
        attachment_types = [_parse_attachment_type(value) for value in values]
        codes = ", ".join(item.code for item in attachment_types) or "هیچ‌کدام"
        self._report(
            f"دریافت نوع‌های مجاز موفق؛ تعداد: {len(attachment_types)}؛ "
            f"کدها: {codes}"
        )
        return attachment_types

    # Steps 1-3: create metadata, upload one file, then accept it.
    def register_attachment(
        self,
        attachment_type: AttachmentType,
        document_number: str,
        document_date: str,
        file_path: Path,
        order_number: int = 1,
        show_original: bool = True,
        document_title: str = "",
    ) -> AttachmentResult:
        """Run the three write steps once for a single attachment."""

        if not self.petition_no or not self.jss_petition_id:
            raise AttachmentApiError("attachment_petition_context_missing")
        if order_number < 1:
            raise AttachmentApiError("attachment_order_number_invalid")
        file_bytes, mime_type = _read_attachment_file(
            file_path, attachment_type.other_data_definition
        )
        document, file_field = _build_document(
            attachment_type,
            self.jss_petition_id,
            document_number,
            document_date,
            show_original,
            document_title,
            order_number,
        )

        # Step 1: create the portal's metadata record and capture its Id.
        self._report(
            f"شروع ثبت مشخصات پیوست؛ نوع: {attachment_type.title} "
            f"(کد {attachment_type.code})"
        )
        document_response = self._fetch_json(
            ADD_DOCUMENT_URL,
            json.dumps(
                {"document": json.dumps(
                    document, ensure_ascii=False, separators=(",", ":")
                )},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "attachment_add_document",
        )
        if not isinstance(document_response, dict):
            raise AttachmentApiError("attachment_document_response_invalid")
        document_id = document_response.get("Id")
        if not isinstance(document_id, str) or not document_id.strip():
            raise AttachmentApiError("attachment_document_id_missing")
        document_id = document_id.strip()
        self._report(
            f"ثبت مشخصات پیوست موفق؛ شناسه سند: {document_id}"
        )

        # Step 2: upload the bytes under the file field defined by the type.
        self._report(
            f"شروع بارگذاری فایل؛ پسوند: {file_path.suffix or 'نامشخص'}؛ "
            f"حجم: {len(file_bytes)} بایت؛ نوع محتوا: {mime_type}"
        )
        try:
            context = self.page.context
            request = context.request  # type: ignore[attr-defined]
            response = request.post(
                ADD_DOCUMENT_FILE_URL,
                params={
                    "orderNumber": order_number,
                    "documentId": document_id,
                    "documentFileID": "",
                    "attachmentTypeID": attachment_type.id,
                    "checkQuality": "true",
                },
                multipart={
                    file_field: {
                        "name": file_path.name,
                        "mimeType": mime_type,
                        "buffer": file_bytes,
                    }
                },
            )
        except Exception:
            raise AttachmentApiError(
                "attachment_file_upload_transport_ambiguous"
            ) from None
        upload_status = getattr(response, "status", None)
        if not _is_success_status(upload_status):
            status_detail = (
                f":http_status={upload_status}"
                if isinstance(upload_status, int)
                else ""
            )
            raise AttachmentApiError(
                f"attachment_file_upload_http_failed{status_detail}"
            )
        try:
            uploaded = response.json()
        except Exception:
            raise AttachmentApiError(
                "attachment_file_upload_response_invalid"
            ) from None
        if (
            not isinstance(uploaded, list)
            or len(uploaded) != 1
            or not isinstance(uploaded[0], str)
            or not uploaded[0].strip()
        ):
            raise AttachmentApiError("attachment_file_upload_response_invalid")
        document_file_id = uploaded[0].strip()
        self._report(
            f"بارگذاری فایل موفق؛ شناسه فایل: {document_file_id}"
        )

        # Step 3: mark the uploaded file accepted for this petition number.
        self._report(
            f"شروع تأیید پیوست برای شماره اظهارنامه {self.petition_no}"
        )
        accepted = self._fetch_json(
            SET_ACCEPT_STATE_URL,
            json.dumps(
                {
                    "petitionNo": self.petition_no,
                    "documentFileId": document_file_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "attachment_accept",
        )
        if accepted is not True:
            raise AttachmentApiError("attachment_accept_rejected")
        self._report("تأیید پیوست موفق")
        return AttachmentResult(document_id, document_file_id)

    def _report(self, message: str) -> None:
        """Send a sanitized progress message when a callback is configured."""

        if self.progress is not None:
            self.progress(message)

    def _fetch_json(
        self, url: str, body: str, operation: str
    ) -> object:
        """Post JSON through the authenticated browser session once."""

        try:
            result = self.page.evaluate(
                """async ({ url, body }) => {
                    const response = await fetch(url, {
                        method: 'POST',
                        credentials: 'include',
                        headers: {
                            'accept': 'application/json, text/plain, */*',
                            'content-type': 'application/json;charset=UTF-8',
                            'x-requested-with': 'XMLHttpRequest',
                        },
                        body,
                    });
                    return {
                        status: response.status,
                        text: await response.text(),
                    };
                }""",
                {"url": url, "body": body},
            )
        except Exception:
            raise AttachmentApiError(
                f"{operation}_transport_ambiguous"
            ) from None
        if not isinstance(result, dict):
            raise AttachmentApiError(f"{operation}_response_unreadable")
        status = result.get("status")
        if not _is_success_status(status):
            status_detail = (
                f":http_status={status}" if isinstance(status, int) else ""
            )
            raise AttachmentApiError(
                f"{operation}_http_failed{status_detail}"
            )
        response_text = result.get("text")
        if not isinstance(response_text, str):
            raise AttachmentApiError(f"{operation}_response_unreadable")
        try:
            return json.loads(response_text)
        except (TypeError, ValueError):
            raise AttachmentApiError(f"{operation}_response_invalid") from None


def _parse_attachment_type(value: object) -> AttachmentType:
    if not isinstance(value, dict):
        raise AttachmentApiError("attachment_lookup_response_invalid")
    identifier = value.get("Id")
    code = value.get("Code")
    title = value.get("Title")
    description = value.get("DescriptionTemplate")
    state = value.get("State")
    definition = value.get("OtherDataDefinition")
    if isinstance(definition, str):
        try:
            definition = json.loads(definition)
        except (TypeError, ValueError):
            raise AttachmentApiError(
                "attachment_lookup_response_invalid"
            ) from None
    if not (
        isinstance(identifier, str)
        and identifier
        and isinstance(code, str)
        and isinstance(title, str)
        and isinstance(description, str)
        and isinstance(state, int)
        and isinstance(definition, dict)
    ):
        raise AttachmentApiError("attachment_lookup_response_invalid")
    return AttachmentType(
        id=identifier,
        code=code,
        title=title,
        description_template=description,
        other_data_definition=definition,
        state=state,
    )


def _build_document(
    attachment_type: AttachmentType,
    jss_petition_id: str,
    document_number: str,
    document_date: str,
    show_original: bool,
    document_title: str,
    order_number: int,
) -> tuple[dict[str, object], str]:
    definition = attachment_type.other_data_definition
    document_fields = _definition_list(definition, "TheDocumentList")
    file_fields = _definition_list(definition, "TheInputFileList")
    required_files = [field for field in file_fields if not _is_optional(field)]
    if len(required_files) != 1:
        raise AttachmentApiError("attachment_file_definition_unsupported")
    file_field = required_files[0].get("Name")
    if not isinstance(file_field, str) or not file_field:
        raise AttachmentApiError("attachment_file_definition_unsupported")

    values: dict[str, str] = {
        "No": _ascii_digits(document_number.strip()),
        "IssueDate": _ascii_digits(document_date.strip()),
        "IsShowDocument": "1" if show_original else "0",
        "Name": document_title.strip(),
    }
    type_copy = {
        "Id": attachment_type.id,
        "State": attachment_type.state,
        "Code": attachment_type.code,
        "DescriptionTemplate": attachment_type.description_template,
        "OtherDataDefinition": deepcopy(definition),
        "Title": attachment_type.title,
    }
    other_data: list[dict[str, str]] = []
    copied_definition = type_copy["OtherDataDefinition"]
    assert isinstance(copied_definition, dict)
    copied_documents = _definition_list(copied_definition, "TheDocumentList")
    copied_files = _definition_list(copied_definition, "TheInputFileList")

    for source_fields, copied_fields in (
        (document_fields, copied_documents),
        (file_fields, copied_files),
    ):
        for source, target in zip(source_fields, copied_fields):
            name = source.get("Name")
            kind = source.get("Type")
            if not isinstance(name, str) or not isinstance(kind, str):
                raise AttachmentApiError(
                    "attachment_metadata_definition_unsupported"
                )
            if kind == "FileUpload":
                value = "1" if name == file_field else ""
            else:
                value = values.get(name, "")
            if not value and not _is_optional(source):
                if name in values:
                    raise AttachmentApiError(
                        f"attachment_{_field_error_name(name)}_missing"
                    )
                raise AttachmentApiError(
                    "attachment_metadata_field_unsupported"
                )
            target["Value"] = value
            other_data.append({"Name": name, "Value": value, "Type": kind})

    document = {
        "TheJSSPetitionDocumentFileList": [
            {
                "AttachmentName": file_field,
                "AttachmentTitle": attachment_type.title,
                "IsEditMode": 1,
                "OrderNumber": order_number,
            }
        ],
        "OtherData": json.dumps(
            other_data, ensure_ascii=False, separators=(",", ":")
        ),
        "TheAttachmentType": type_copy,
        "IsShowDescription": True,
        "CountFileList": "0",
        "OrderNumber": order_number,
        "Index": 0,
        "AttachmentTypeId": attachment_type.id,
        "AttachmentTypeTitle": attachment_type.title,
        "AttachmentDescription": attachment_type.description_template,
        "Id": "",
        "JSSPetitionId": jss_petition_id,
    }
    return document, file_field


def _definition_list(
    definition: Mapping[str, object], name: str
) -> list[dict[str, object]]:
    fields = definition.get(name)
    if not isinstance(fields, list) or not all(
        isinstance(field, dict) for field in fields
    ):
        raise AttachmentApiError("attachment_metadata_definition_invalid")
    return fields


def _is_optional(field: Mapping[str, object]) -> bool:
    return field.get("IsOptional") is True or field.get("eIsOptional") == 1


def _field_error_name(name: str) -> str:
    return {
        "No": "document_number",
        "IssueDate": "document_date",
        "Name": "document_title",
    }.get(name, "metadata_value")


def _read_attachment_file(
    file_path: Path, definition: Mapping[str, object]
) -> tuple[bytes, str]:
    if not file_path.is_file():
        raise AttachmentApiError("attachment_file_missing")
    try:
        content = file_path.read_bytes()
    except OSError:
        raise AttachmentApiError("attachment_file_unreadable") from None
    if not content:
        raise AttachmentApiError("attachment_file_empty")
    file_fields = _definition_list(definition, "TheInputFileList")
    required = [field for field in file_fields if not _is_optional(field)]
    if len(required) != 1:
        raise AttachmentApiError("attachment_file_definition_unsupported")
    allowed = required[0].get("FileValid")
    if isinstance(allowed, str) and allowed.strip():
        extensions = {
            value.strip().lstrip(".").casefold()
            for value in allowed.split(";")
            if value.strip()
        }
        if file_path.suffix.lstrip(".").casefold() not in extensions:
            raise AttachmentApiError("attachment_file_type_not_allowed")
    return content, mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"


def _is_success_status(value: object) -> bool:
    return isinstance(value, int) and 200 <= value < 300


def _ascii_digits(value: str) -> str:
    return value.translate(
        str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    )


def save_attachment_console_text(
    attachment_type: AttachmentType | None,
    petition_no: str,
    jss_petition_id: str,
    reliefs: list[Mapping[str, object]],
    petition_type_code: str,
    document_number: str,
    document_date: str,
    file_path: Path,
    output_path: Path,
    *,
    order_number: int = 1,
    show_original: bool = True,
    document_title: str = "",
    requested_title: str = "",
) -> Path:
    """Save the four attachment requests as a Chrome-console script."""

    petition_no = petition_no.strip()
    if len(petition_no) != 16 or not petition_no.isdigit():
        raise AttachmentApiError("attachment_petition_number_invalid")
    if attachment_type is None:
        if not requested_title.strip():
            raise AttachmentApiError("attachment_title_missing")
        if not file_path.is_file():
            raise AttachmentApiError("attachment_file_missing")
        try:
            file_size = file_path.stat().st_size
        except OSError:
            raise AttachmentApiError("attachment_file_unreadable") from None
        if file_size < 1:
            raise AttachmentApiError("attachment_file_empty")
        mime_type = (
            mimetypes.guess_type(file_path.name)[0]
            or "application/octet-stream"
        )
        document = None
        file_field = None
    else:
        file_bytes, mime_type = _read_attachment_file(
            file_path, attachment_type.other_data_definition
        )
        file_size = len(file_bytes)
        document, file_field = _build_document(
            attachment_type,
            jss_petition_id,
            document_number,
            document_date,
            show_original,
            document_title,
            order_number,
        )
    config = {
        "petitionNo": petition_no,
        "jssPetitionId": jss_petition_id,
        "attachmentTypeId": attachment_type.id if attachment_type else None,
        "attachmentTypeCode": attachment_type.code if attachment_type else None,
        "requestedTitle": (
            attachment_type.title if attachment_type else requested_title.strip()
        ),
        "lookupBody": {
            "theJSSPetitionRelief": reliefs,
            "petitionTypeCode": petition_type_code,
            "petitionNo": petition_no,
            "pageSize": 10,
        },
        "documentBody": (
            {"document": json.dumps(
                document, ensure_ascii=False, separators=(",", ":")
            )}
            if document is not None else None
        ),
        "documentNumber": _ascii_digits(document_number.strip()),
        "documentDate": _ascii_digits(document_date.strip()),
        "documentTitle": document_title.strip(),
        "showOriginal": show_original,
        "fileField": file_field,
        "expectedFileName": file_path.name,
        "expectedFileSize": file_size,
        "mimeType": mime_type,
        "orderNumber": order_number,
    }
    script = f"""// Generated by Sana Declaration Uploader.
// Run this only on https://person.adliran.ir while the intended session is active.
// Request 1 is read-only. Requests 2-4 write once and must never be retried blindly.
(async () => {{
  const RUN_WRITE_REQUESTS = false; // Change to true only when ready for one live attempt.
  const CONFIG = {json.dumps(config, ensure_ascii=False, separators=(",", ":"))};
  const URLS = {{
    validTypes: {json.dumps(GET_VALID_ATTACHMENT_URL)},
    addDocument: {json.dumps(ADD_DOCUMENT_URL)},
    uploadFile: {json.dumps(ADD_DOCUMENT_FILE_URL)},
    acceptFile: {json.dumps(SET_ACCEPT_STATE_URL)},
  }};
  const outputs = [];
  window.__sanaAttachmentTestOutput = outputs;

  const normalizeTitle = (value) => String(value || '')
    .normalize('NFKC').replaceAll('ي', 'ی').replaceAll('ك', 'ک')
    .replaceAll('\u200c', ' ').replace(/\\s+/g, ' ').trim();

  const parseDefinition = (value) => {{
    const definition = typeof value === 'string' ? JSON.parse(value) : value;
    if (!definition || typeof definition !== 'object') {{
      throw new Error('Attachment type has no usable OtherDataDefinition');
    }}
    return definition;
  }};

  const buildDocument = (type) => {{
    const definition = parseDefinition(type.OtherDataDefinition);
    const documents = definition.TheDocumentList;
    const files = definition.TheInputFileList;
    if (!Array.isArray(documents) || !Array.isArray(files)) {{
      throw new Error('Attachment metadata definition is invalid');
    }}
    const requiredFiles = files.filter((field) => !(field?.IsOptional === true || field?.eIsOptional === 1));
    if (requiredFiles.length !== 1 || !requiredFiles[0]?.Name) {{
      throw new Error('Attachment file definition is unsupported');
    }}
    const fileField = String(requiredFiles[0].Name);
    const allowed = String(requiredFiles[0].FileValid || '').split(';')
      .map((item) => item.trim().replace(/^\\./, '').toLocaleLowerCase())
      .filter(Boolean);
    const extension = CONFIG.expectedFileName.includes('.')
      ? CONFIG.expectedFileName.split('.').pop().toLocaleLowerCase() : '';
    if (allowed.length && !allowed.includes(extension)) {{
      throw new Error(`File extension is not allowed; allowed: ${{allowed.join(', ')}}`);
    }}
    const values = {{
      No: CONFIG.documentNumber,
      IssueDate: CONFIG.documentDate,
      IsShowDocument: CONFIG.showOriginal ? '1' : '0',
      Name: CONFIG.documentTitle,
    }};
    const definitionCopy = structuredClone(definition);
    const copiedFields = [
      ...(definitionCopy.TheDocumentList || []),
      ...(definitionCopy.TheInputFileList || []),
    ];
    const otherData = [];
    for (const field of copiedFields) {{
      const optional = field?.IsOptional === true || field?.eIsOptional === 1;
      const name = String(field?.Name || '');
      const kind = String(field?.Type || '');
      const value = kind === 'FileUpload'
        ? (name === fileField ? '1' : '')
        : String(values[name] || '');
      if (!value && !optional) throw new Error(`Required attachment field is empty: ${{name}}`);
      field.Value = value;
      otherData.push({{ Name: name, Value: value, Type: kind }});
    }}
    const typeCopy = structuredClone(type);
    typeCopy.OtherDataDefinition = definitionCopy;
    return {{
      fileField,
      body: {{ document: JSON.stringify({{
        TheJSSPetitionDocumentFileList: [{{
          AttachmentName: fileField,
          AttachmentTitle: String(type.Title || ''),
          IsEditMode: 1,
          OrderNumber: CONFIG.orderNumber,
        }}],
        OtherData: JSON.stringify(otherData),
        TheAttachmentType: typeCopy,
        IsShowDescription: true,
        CountFileList: '0',
        OrderNumber: CONFIG.orderNumber,
        Index: 0,
        AttachmentTypeId: String(type.Id || ''),
        AttachmentTypeTitle: String(type.Title || ''),
        AttachmentDescription: String(type.DescriptionTemplate || ''),
        Id: '',
        JSSPetitionId: CONFIG.jssPetitionId,
      }}) }},
    }};
  }};

  const chooseFile = async () => {{
    if (typeof window.showOpenFilePicker !== 'function') {{
      throw new Error('showOpenFilePicker is unavailable in this Chrome context');
    }}
    const [handle] = await window.showOpenFilePicker({{ multiple: false }});
    const file = await handle.getFile();
    if (file.name !== CONFIG.expectedFileName || file.size !== CONFIG.expectedFileSize) {{
      throw new Error(`Wrong file selected; expected ${{CONFIG.expectedFileName}} (${{CONFIG.expectedFileSize}} bytes)`);
    }}
    return file;
  }};

  const postJson = async (step, url, body) => {{
    const response = await fetch(url, {{
      method: 'POST',
      credentials: 'include',
      headers: {{
        'accept': 'application/json, text/plain, */*',
        'content-type': 'application/json;charset=UTF-8',
        'x-requested-with': 'XMLHttpRequest',
      }},
      body: JSON.stringify(body),
    }});
    const text = await response.text();
    const result = {{ step, status: response.status, body: text }};
    outputs.push(result);
    console.log(result);
    if (!response.ok) throw new Error(`${{step}} failed with HTTP ${{response.status}}`);
    try {{ return JSON.parse(text); }}
    catch {{ throw new Error(`${{step}} returned non-JSON data`); }}
  }};

  const file = await chooseFile();
  const validTypes = await postJson('1.GetValidAttachment', URLS.validTypes, CONFIG.lookupBody);
  const values = validTypes?.AttachmentTypeList?.$values;
  const matched = Array.isArray(values)
    ? values.filter((item) => CONFIG.attachmentTypeId
      ? String(item?.Id) === CONFIG.attachmentTypeId
      : normalizeTitle(item?.Title) === normalizeTitle(CONFIG.requestedTitle))
    : [];
  if (matched.length !== 1 || (CONFIG.attachmentTypeCode && String(matched[0]?.Code) !== CONFIG.attachmentTypeCode)) {{
    throw new Error('The expected attachment type is no longer uniquely valid');
  }}
  const selectedType = matched[0];
  const generated = CONFIG.documentBody
    ? {{ body: CONFIG.documentBody, fileField: CONFIG.fileField }}
    : buildDocument(selectedType);
  const attachmentTypeId = String(selectedType.Id || '').trim();
  if (!attachmentTypeId) throw new Error('Selected attachment type has no Id');
  if (!RUN_WRITE_REQUESTS) {{
    console.warn('Request 1 completed. Requests 2-4 were not sent. Set RUN_WRITE_REQUESTS=true for one live attempt.');
    return outputs;
  }}

  const documentResult = await postJson('2.AddDocument', URLS.addDocument, generated.body);
  const documentId = String(documentResult?.Id || '').trim();
  if (!documentId) throw new Error('2.AddDocument returned no document Id');

  const uploadUrl = new URL(URLS.uploadFile);
  uploadUrl.search = new URLSearchParams({{
    orderNumber: String(CONFIG.orderNumber),
    documentId,
    documentFileID: '',
    attachmentTypeID: attachmentTypeId,
    checkQuality: 'true',
  }}).toString();
  const form = new FormData();
  const uploadFile = file.type === CONFIG.mimeType
    ? file
    : new File([file], file.name, {{ type: CONFIG.mimeType, lastModified: file.lastModified }});
  form.append(generated.fileField, uploadFile, uploadFile.name);
  const uploadResponse = await fetch(uploadUrl, {{
    method: 'POST', credentials: 'include',
    headers: {{
      'accept': 'application/json, text/plain, */*',
    }},
    body: form,
  }});
  const uploadText = await uploadResponse.text();
  const uploadOutput = {{ step: '3.UploadFile', status: uploadResponse.status, body: uploadText }};
  outputs.push(uploadOutput);
  console.log(uploadOutput);
  if (!uploadResponse.ok) throw new Error(`3.UploadFile failed with HTTP ${{uploadResponse.status}}`);
  let uploaded;
  try {{ uploaded = JSON.parse(uploadText); }}
  catch {{ throw new Error('3.UploadFile returned non-JSON data'); }}
  if (!Array.isArray(uploaded) || uploaded.length !== 1 || !String(uploaded[0] || '').trim()) {{
    throw new Error('3.UploadFile returned no unique document file Id');
  }}

  await postJson('4.AcceptFile', URLS.acceptFile, {{
    petitionNo: CONFIG.petitionNo,
    documentFileId: String(uploaded[0]).trim(),
  }});
  console.log('All four requests completed once.', outputs);
  return outputs;
}})().catch((error) => {{
  console.error('Attachment request sequence stopped:', error);
  console.log('Completed outputs:', window.__sanaAttachmentTestOutput);
}});
"""
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(script, encoding="utf-8")
    except OSError:
        raise AttachmentApiError("attachment_console_text_write_failed") from None
    return output_path
