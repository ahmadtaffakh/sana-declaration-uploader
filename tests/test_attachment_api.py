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
)


class AttachmentClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.file_path = Path(self.directory.name) / "attachment.pdf"
        self.file_path.write_bytes(b"%PDF-test")
        self.page = Mock()
        self.client = AttachmentClient(
            self.page, petition_no="12345", jss_petition_id="petition-id"
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
                "petitionNo": "12345",
                "pageSize": 10,
            },
        )

    def test_registers_metadata_upload_and_accepts_in_order(self):
        progress = []
        client = AttachmentClient(
            self.page, petition_no="12345", jss_petition_id="petition-id",
            progress=progress.append,
        )
        self.page.evaluate.side_effect = [
            {"status": 200, "text": json.dumps({"Id": "document-id"})},
            {"status": 200, "text": "true"},
        ]
        upload_response = Mock(status=200)
        upload_response.json.return_value = ["document-file-id"]
        self.page.context.request.post.return_value = upload_response

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
        self.assertEqual(self.page.evaluate.call_count, 2)
        self.assertEqual(self.page.context.request.post.call_count, 1)
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
        upload_args, upload_kwargs = self.page.context.request.post.call_args
        self.assertEqual(upload_args, (ADD_DOCUMENT_FILE_URL,))
        self.assertEqual(
            upload_kwargs["params"],
            {
                "orderNumber": 1,
                "documentId": "document-id",
                "documentFileID": "",
                "attachmentTypeID": "099",
                "checkQuality": "true",
            },
        )
        self.assertEqual(
            upload_kwargs["multipart"]["001"]["buffer"], b"%PDF-test"
        )
        self.assertEqual(
            upload_kwargs["multipart"]["001"]["mimeType"], "application/pdf"
        )
        _, accept_arg = self.page.evaluate.call_args_list[1].args
        self.assertEqual(accept_arg["url"], SET_ACCEPT_STATE_URL)
        self.assertEqual(
            json.loads(accept_arg["body"]),
            {"petitionNo": "12345", "documentFileId": "document-file-id"},
        )
        self.assertTrue(any("شروع ثبت مشخصات پیوست" in m for m in progress))
        self.assertTrue(any("شروع بارگذاری فایل" in m for m in progress))
        self.assertTrue(any("شروع تأیید پیوست" in m for m in progress))
        self.assertTrue(any("تأیید پیوست موفق" in m for m in progress))

    def test_stops_on_ambiguous_upload_without_accept_or_retry(self):
        self.page.evaluate.return_value = {
            "status": 200, "text": json.dumps({"Id": "document-id"})
        }
        self.page.context.request.post.side_effect = TimeoutError(
            "transport details"
        )

        with self.assertRaisesRegex(
            AttachmentApiError,
            "attachment_file_upload_transport_ambiguous",
        ):
            self.client.register_attachment(
                self.attachment_type(), "17", "1405/05/05", self.file_path,
                document_title="مدرک آزمایشی",
            )

        self.page.evaluate.assert_called_once()
        self.page.context.request.post.assert_called_once()

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
            {"status": 200, "text": "false"},
        ]
        upload_response = Mock(status=200)
        upload_response.json.return_value = ["document-file-id"]
        self.page.context.request.post.return_value = upload_response

        with self.assertRaisesRegex(
            AttachmentApiError, "attachment_accept_rejected"
        ):
            self.client.register_attachment(
                self.attachment_type(), "17", "1405/05/05", self.file_path,
                document_title="مدرک آزمایشی",
            )

        self.page.evaluate.assert_has_calls(
            [unittest.mock.call(unittest.mock.ANY, unittest.mock.ANY)] * 2
        )
        self.page.context.request.post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
