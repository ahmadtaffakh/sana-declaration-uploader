import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from sana_declaration_uploader.models import DeclarationDraft
from sana_declaration_uploader.petition_api import (
    SET_PETITION_DATA_URL,
    SetPetitionDataClient,
    SetPetitionDataError,
    build_setpetitiondata_payload,
    load_setpetitiondata_template,
    parse_set_petition_data_result,
    save_setpetitiondata_debug_text,
    submit_one_petition_data,
)


class SetPetitionDataClientTests(unittest.TestCase):
    @staticmethod
    def _success_response():
        return Mock(
            status=200,
            text=Mock(return_value=(
                "1405220409964381;74469;"
                "E421AC18584E486B8EFA9B1CA98C8389"
            )),
        )

    def test_posts_wrapped_payload_once_and_parses_result(self):
        request = Mock()
        request.post.return_value = Mock(
            status=200,
            text=Mock(return_value=(
                "1405220409964381;74469;"
                "E421AC18584E486B8EFA9B1CA98C8389"
            )),
        )

        result = SetPetitionDataClient(request).submit(
            {"PetitionTypeCode": "110", "Description": "متن"}
        )

        self.assertEqual(result.tracking_code, "1405220409964381")
        self.assertEqual(result.petition_number, "74469")
        self.assertEqual(
            result.petition_id, "E421AC18584E486B8EFA9B1CA98C8389"
        )
        request.post.assert_called_once()
        args, kwargs = request.post.call_args
        self.assertEqual(args[0], SET_PETITION_DATA_URL)
        envelope = json.loads(kwargs["data"])
        self.assertEqual(
            json.loads(envelope["data"]),
            {"PetitionTypeCode": "110", "Description": "متن"},
        )
        self.assertIs(envelope["isAuthenticate"], True)

    def test_saves_console_script_with_exact_body_and_response_logging(self):
        petition = {"PetitionTypeCode": "110", "Description": "متن فارسی"}
        with TemporaryDirectory() as directory:
            body_path = Path(directory) / "request.txt"
            save_setpetitiondata_debug_text(petition, body_path)

            script = body_path.with_name("request.console.txt").read_text(
                encoding="utf-8"
            )
            body = body_path.read_text(encoding="utf-8").strip()

        self.assertIn("method: 'POST'", script)
        self.assertIn("credentials: 'include'", script)
        self.assertIn(json.dumps(body, ensure_ascii=False), script)
        self.assertIn("response.status", script)
        self.assertIn("responseText", script)

    def test_does_not_retry_transport_errors(self):
        request = Mock()
        request.post.side_effect = TimeoutError("sensitive transport detail")

        with self.assertRaisesRegex(
            SetPetitionDataError, "set_petition_data_transport_ambiguous"
        ):
            SetPetitionDataClient(request).submit({"Description": "متن"})

        request.post.assert_called_once()

    def test_one_item_helper_uses_authenticated_page_context(self):
        request = Mock()
        request.post.return_value = self._success_response()
        page = Mock(context=Mock(request=request))

        result = submit_one_petition_data(page, {"PetitionTypeCode": "110"})

        self.assertEqual(result.petition_number, "74469")
        request.post.assert_called_once()

    def test_rejects_http_failure_without_exposing_response_body(self):
        request = Mock()
        request.post.return_value = Mock(status=500, text=Mock(
            return_value="private response"
        ))

        with self.assertRaisesRegex(
            SetPetitionDataError, "set_petition_data_http_failed"
        ):
            SetPetitionDataClient(request).submit({"Description": "متن"})

        request.post.assert_called_once()

    def test_rejects_malformed_result(self):
        with self.assertRaisesRegex(
            SetPetitionDataError, "set_petition_data_response_invalid"
        ):
            parse_set_petition_data_result("tracking;not-a-number;bad-id")


class SetPetitionDataPayloadTests(unittest.TestCase):
    @staticmethod
    def _template():
        return {
            "Description": "old description",
            "HstCaseSubjectCode": "",
            "MasterJudgeUnitId": "",
            "MasterJudgeUnitName": "",
            "RelatedNo": "123456",
            "theJSSPetitionRelief": [
                {"RowNumber": 1, "PersonNumber": 2},
                "ignore non-object rows",
            ],
            "theJSSPetitionPerson": [
                {"RoleType": 1, "Name": "fixed"},
                {
                    "RoleType": 2,
                    "Name": "old",
                    "Family": "old",
                    "BasicAvenue": "old",
                    "HomeAddress": "old",
                    "ProvinceId": "old",
                    "ProvinceTitle": "old",
                    "StateId": "old",
                    "StateTitle": "old",
                    "CityId": "old",
                    "CityTitle": "old",
                },
            ],
        }

    @staticmethod
    def _draft():
        return DeclarationDraft(
            source_dir=Path("."),
            fields={
                "recipient_first_name": "نام",
                "recipient_last_name": "نام‌خانوادگی",
                "recipient_father_name": "پدر",
                "recipient_national_id": "0012345678",
                "recipient_main_street": "خیابان",
                "recipient_address": "نشانی",
                "recipient_postal_code": "1234567890",
                "province_code": "P1",
                "province": "استان",
                "county_code": "C1",
                "county": "شهرستان",
                "city_code": "T1",
                "city": "شهر",
                "declaration_text": "شرح جدید",
            },
        )

    def test_replaces_recipient_description_and_region_codes_only(self):
        template = self._template()
        result = build_setpetitiondata_payload(template, self._draft())

        self.assertEqual(result["Description"], "<p>شرح جدید</p>")
        self.assertEqual(result["HstCaseSubjectCode"], "0024")
        self.assertIsNone(result["MasterJudgeUnitId"])
        self.assertIsNone(result["MasterJudgeUnitName"])
        self.assertEqual(result["RelatedNo"], "12345678")
        self.assertEqual(
            result["theJSSPetitionRelief"][0],
            {"RowNumber": 0, "PersonNumber": 0},
        )
        self.assertEqual(
            result["theJSSPetitionRelief"][1], "ignore non-object rows"
        )
        self.assertEqual(
            result["theJSSPetitionPerson"][0],
            template["theJSSPetitionPerson"][0],
        )
        recipient = result["theJSSPetitionPerson"][1]
        self.assertEqual(recipient["IdentityNo"], "0012345678")
        self.assertEqual(recipient["FatherName"], "پدر")
        self.assertEqual(recipient["PostallCode"], "1234567890")
        self.assertEqual(recipient["StateId"], "P1")
        self.assertEqual(recipient["StateTitle"], "استان")
        self.assertEqual(recipient["ProvinceId"], "C1")
        self.assertEqual(recipient["ProvinceTitle"], "شهرستان")
        self.assertEqual(recipient["CityId"], "T1")
        self.assertEqual(template["Description"], "old description")

    def test_requires_one_recipient_role(self):
        template = self._template()
        template["theJSSPetitionPerson"].append(
            dict(template["theJSSPetitionPerson"][1])
        )
        with self.assertRaisesRegex(
            SetPetitionDataError, "set_petition_data_recipient_not_unique"
        ):
            build_setpetitiondata_payload(template, self._draft())

    def test_loads_wrapped_request_as_local_template(self):
        template = self._template()
        envelope = {
            "data": json.dumps(template, ensure_ascii=False),
            "isAuthenticate": True,
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "payload.json"
            path.write_text(json.dumps(envelope), encoding="utf-8")
            self.assertEqual(load_setpetitiondata_template(path), template)

    def test_requires_all_region_codes(self):
        fields = dict(self._draft().fields)
        fields["city_code"] = ""
        draft = DeclarationDraft(source_dir=Path("."), fields=fields)
        with self.assertRaisesRegex(
            SetPetitionDataError, "set_petition_data_location_code_missing"
        ):
            build_setpetitiondata_payload(self._template(), draft)


if __name__ == "__main__":
    unittest.main()
