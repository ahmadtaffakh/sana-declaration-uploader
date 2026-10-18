import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from sana_declaration_uploader.attachment_api import (
    ADD_DOCUMENT_FILE_URL,
    ADD_DOCUMENT_URL,
    GET_VALID_ATTACHMENT_URL,
    SET_ACCEPT_STATE_URL,
    AttachmentApiError,
    AttachmentClient,
    AttachmentType,
    _build_document,
    save_attachment_console_text,
)


class AttachmentClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.file_path = Path(self.directory.name) / "attachment.pdf"
        self.file_path.write_bytes(b"%PDF-test")
        self.page = Mock()
        self.client = AttachmentClient(
            self.page,
            petition_no="1405000000000001",
            jss_petition_id="petition-id",
        )

    @staticmethod
    def attachment_type():
        return AttachmentType(
            id="099",
            code="099",
            title="سایر ضمائم",
            description_template="شرح",
            state=1,
            other_data_definition={
                "TheDocumentList": [
                    {
                        "Name": "IssueDate", "Type": "Date",
                        "IsOptional": True,
                    },
                    {
                        "Name": "No", "Type": "String",
                        "IsOptional": False,
                    },
                    {
                        "Name": "Name", "Type": "String",
                        "IsOptional": False,
                    },
                    {
                        "Name": "IsShowDocument", "Type": "YesNo",
                        "IsOptional": True,
                    },
                ],
                "TheInputFileList": [
                    {
                        "Name": "001", "Type": "FileUpload",
                        "FarsiName": "تصویر سایر ضمائم",
                        "FileValid": "jpg;jpeg;pdf",
                        "IsOptional": False,
                    }
                ],
            },
        )

    def test_saves_four_request_chrome_console_script_without_file_content(self):
        output_path = Path(self.directory.name) / "attachment.console.txt"

        actual = save_attachment_console_text(
            self.attachment_type(),
            "1405000000000001",
            "petition-id",
            [{"Id": "relief-id", "JSSPetitionId": "petition-id"}],
            "110",
            "۱۲۳٤",
            "۱۴۰۵/۰۱/۰۱",
            self.file_path,
            output_path,
            document_title="عنوان پیوست",
        )

        script = output_path.read_text(encoding="utf-8")
        self.assertEqual(actual, output_path)
        self.assertIn(GET_VALID_ATTACHMENT_URL, script)
        self.assertIn(ADD_DOCUMENT_URL, script)
        self.assertIn(ADD_DOCUMENT_FILE_URL, script)
        self.assertIn(SET_ACCEPT_STATE_URL, script)
        self.assertIn("const RUN_WRITE_REQUESTS = false", script)
        self.assertIn("showOpenFilePicker", script)
        self.assertIn("window.__sanaAttachmentTestOutput", script)
        self.assertNotIn("%PDF-test", script)

    def test_saves_console_script_before_attachment_type_lookup(self):
        output_path = Path(self.directory.name) / "pre-lookup.console.txt"

        save_attachment_console_text(
            None,
            "1405000000000001",
            "petition-id",
            [{"Id": "relief-id", "JSSPetitionId": "petition-id"}],
            "110",
            "۱۲۳٤",
            "۱۴۰۵/۰۱/۰۱",
            self.file_path,
            output_path,
            document_title="عنوان پیوست",
            requested_title="سایر ضمائم",
        )

        script = output_path.read_text(encoding="utf-8")
        self.assertIn('"attachmentTypeId":null', script)
        self.assertIn('"requestedTitle":"سایر ضمائم"', script)
        self.assertIn('"documentNumber":"1234"', script)
        self.assertIn('"documentDate":"1405/01/01"', script)
        self.assertIn("const buildDocument = (type)", script)
        self.assertIn("1.GetValidAttachment", script)
        self.assertIn("4.AcceptFile", script)
        upload_request = script.split(
            "const uploadResponse = await fetch", 1
        )[1].split("const uploadText", 1)[0]
        self.assertNotIn("x-requested-with", upload_request)

    def test_rejects_short_petition_number_before_writing_console_file(self):
        output_path = Path(self.directory.name) / "invalid.console.txt"

        with self.assertRaisesRegex(
            AttachmentApiError, "attachment_petition_number_invalid"
        ):
            save_attachment_console_text(
                None,
                "12345",
                "petition-id",
                [{"Id": "relief-id", "JSSPetitionId": "petition-id"}],
                "110",
                "document-number",
                "1405/01/01",
                self.file_path,
                output_path,
                requested_title="سایر ضمائم",
            )

        self.assertFalse(output_path.exists())

    def test_rejects_short_petition_number_before_api_client_is_created(self):
        with self.assertRaisesRegex(
            AttachmentApiError, "attachment_petition_number_invalid"
        ):
            AttachmentClient(
                self.page, petition_no="12345", jss_petition_id="petition-id"
            )

        self.page.evaluate.assert_not_called()

    def test_builds_representation_document_like_live_second_request(self):
        attachment_type = AttachmentType(
            id="021",
            code="005",
            title="تصوير مدرک نمايندگي",
            description_template="شرح مدرک نمایندگی",
            state=1,
            other_data_definition={
                "Code": None,
                "ID": None,
                "Title": None,
                "TheDocumentList": [
                    {"Name": "No", "Type": "String", "IsOptional": False},
                    {"Name": "IssueDate", "Type": "Date", "IsOptional": False},
                    {
                        "Name": "IsShowDocument",
                        "Type": "YesNo",
                        "IsOptional": True,
                    },
                ],
                "TheInputFileList": [{
                    "Name": "001",
                    "Type": "FileUpload",
                    "FileValid": "jpg;jpeg",
                    "IsOptional": False,
                }],
            },
        )

        document, file_field = _build_document(
            attachment_type,
            "petition-id",
            "document-number",
            "1405/01/01",
            True,
            "مدرک نمایندگی",
            1,
        )

        self.assertEqual(file_field, "001")
        self.assertEqual(document["AttachmentTypeId"], "021")
        self.assertEqual(document["TheAttachmentType"]["Code"], "005")
        self.assertEqual(document["OrderNumber"], 1)
        self.assertEqual(document["CountFileList"], "0")
        self.assertEqual(
            [item["Name"] for item in json.loads(document["OtherData"])],
            ["No", "IssueDate", "IsShowDocument", "001"],
        )
        self.assertEqual(
            set(document["TheJSSPetitionDocumentFileList"][0]),
            {"AttachmentName", "AttachmentTitle", "IsEditMode", "OrderNumber"},
        )

    def test_normalizes_document_number_and_date_digits_for_second_request(self):
        document, _ = _build_document(
            self.attachment_type(),
            "petition-id",
            "۱۲۳٤",
            "۱۴۰۵/۰۷/۲۰",
            True,
            "عنوان پیوست",
            1,
        )

        values = {
            item["Name"]: item["Value"]
            for item in json.loads(document["OtherData"])
        }
        self.assertEqual(values["No"], "1234")
        self.assertEqual(values["IssueDate"], "1405/07/20")

    def test_get_valid_attachment_types_parses_definition(self):
        raw_type = {
            "Id": "099",
            "State": 1,
            "Code": "099",
            "Title": "سایر ضمائم",
            "DescriptionTemplate": "شرح",
            "OtherDataDefinition": json.dumps(
                self.attachment_type().other_data_definition
            ),
        }
        self.page.evaluate.return_value = {
            "status": 200,
            "text": json.dumps({
                "AttachmentTypeList": {"$values": [raw_type]}
            }),
        }

        result = self.client.get_valid_attachment_types(
            [{"ValidAttachmentTypes": ";099;"}]
        )

        self.assertEqual(result, [self.attachment_type()])
        self.page.evaluate.assert_called_once()
        expression, argument = self.page.evaluate.call_args.args
        self.assertIn("fetch(url", expression)
        self.assertEqual(argument["url"], GET_VALID_ATTACHMENT_URL)
        self.assertEqual(
            json.loads(argument["body"]),
            {
                "theJSSPetitionRelief": [
                    {"ValidAttachmentTypes": ";099;"}
                ],
                "petitionTypeCode": "110",
                "petitionNo": "1405000000000001",
                "pageSize": 10,
            },
        )

    def test_registers_metadata_upload_and_accepts_in_order(self):
        progress = []
        client = AttachmentClient(
            self.page,
            petition_no="1405000000000001",
            jss_petition_id="petition-id",
            progress=progress.append,
        )
        self.page.evaluate.side_effect = [
            {"status": 200, "text": json.dumps({"Id": "document-id"})},
            {"status": 200, "text": json.dumps(["document-file-id"])},
            {"status": 200, "text": "true"},
        ]

        result = client.register_attachment(
            self.attachment_type(),
            document_number="17",
            document_date="1405/05/05",
            file_path=self.file_path,
            document_title="مدرک آزمایشی",
            show_original=False,
        )

        self.assertEqual(
            result.document_id, "document-id"
        )
        self.assertEqual(result.document_file_id, "document-file-id")
        self.assertEqual(self.page.evaluate.call_count, 3)
        first_expr, first_arg = self.page.evaluate.call_args_list[0].args
        self.assertEqual(first_arg["url"], ADD_DOCUMENT_URL)
        self.assertIn("fetch(url", first_expr)
        outer = json.loads(first_arg["body"])
        document = json.loads(outer["document"])
        self.assertEqual(document["JSSPetitionId"], "petition-id")
        self.assertEqual(document["AttachmentTypeId"], "099")
        other_data = json.loads(document["OtherData"])
        self.assertEqual(
            other_data,
            [
                {"Name": "IssueDate", "Value": "1405/05/05", "Type": "Date"},
                {"Name": "No", "Value": "17", "Type": "String"},
                {"Name": "Name", "Value": "مدرک آزمایشی", "Type": "String"},
                {"Name": "IsShowDocument", "Value": "0", "Type": "YesNo"},
                {"Name": "001", "Value": "1", "Type": "FileUpload"},
            ],
        )
        upload_expr, upload_arg = self.page.evaluate.call_args_list[1].args
        self.assertIn("new FormData()", upload_expr)
        self.assertIn("credentials: 'include'", upload_expr)
        self.assertEqual(upload_arg["url"], ADD_DOCUMENT_FILE_URL)
        self.assertEqual(
            upload_arg["params"],
            {
                "orderNumber": "1",
                "documentId": "document-id",
                "documentFileID": "",
                "attachmentTypeID": "099",
                "checkQuality": "true",
            },
        )
        self.assertEqual(upload_arg["fileField"], "001")
        self.assertEqual(upload_arg["fileName"], self.file_path.name)
        self.assertEqual(upload_arg["mimeType"], "application/pdf")
        self.assertEqual(upload_arg["fileBase64"], "JVBERi10ZXN0")
        _, accept_arg = self.page.evaluate.call_args_list[2].args
        self.assertEqual(accept_arg["url"], SET_ACCEPT_STATE_URL)
        self.assertEqual(
            json.loads(accept_arg["body"]),
            {
                "petitionNo": "1405000000000001",
                "documentFileId": "document-file-id",
            },
        )
        self.assertTrue(any("شروع ثبت مشخصات پیوست" in m for m in progress))
        self.assertTrue(any("شروع بارگذاری فایل" in m for m in progress))
        self.assertTrue(any("شروع تأیید پیوست" in m for m in progress))
        self.assertTrue(any("تأیید پیوست موفق" in m for m in progress))

    def test_stops_on_ambiguous_upload_without_accept_or_retry(self):
        self.page.evaluate.side_effect = [
            {"status": 200, "text": json.dumps({"Id": "document-id"})},
            TimeoutError("transport details"),
        ]

        with self.assertRaisesRegex(
            AttachmentApiError,
            "attachment_file_upload_transport_ambiguous",
        ):
            self.client.register_attachment(
                self.attachment_type(), "17", "1405/05/05", self.file_path,
                document_title="مدرک آزمایشی",
            )

        self.assertEqual(self.page.evaluate.call_count, 2)

    def test_rejects_disallowed_extension_before_any_write(self):
        with TemporaryDirectory() as directory:
            unsupported = Path(directory) / "attachment.docx"
            unsupported.write_bytes(b"content")

            with self.assertRaisesRegex(
                AttachmentApiError, "attachment_file_type_not_allowed"
            ):
                self.client.register_attachment(
                    self.attachment_type(), "17", "1405/05/05", unsupported
                )

        self.page.evaluate.assert_not_called()
        self.page.context.request.post.assert_not_called()

    def test_rejects_missing_required_document_title_before_write(self):
        with self.assertRaisesRegex(
            AttachmentApiError, "attachment_document_title_missing"
        ):
            self.client.register_attachment(
                self.attachment_type(), "17", "1405/05/05", self.file_path,
            )

        self.page.evaluate.assert_not_called()
        self.page.context.request.post.assert_not_called()

    def test_accept_false_is_not_reported_as_success(self):
        self.page.evaluate.side_effect = [
            {"status": 200, "text": json.dumps({"Id": "document-id"})},
            {"status": 200, "text": json.dumps(["document-file-id"])},
            {"status": 200, "text": "false"},
        ]

        with self.assertRaisesRegex(
            AttachmentApiError, "attachment_accept_rejected"
        ):
            self.client.register_attachment(
                self.attachment_type(), "17", "1405/05/05", self.file_path,
                document_title="مدرک آزمایشی",
            )

        self.page.evaluate.assert_has_calls(
            [unittest.mock.call(unittest.mock.ANY, unittest.mock.ANY)] * 3
        )


if __name__ == "__main__":
    unittest.main()
