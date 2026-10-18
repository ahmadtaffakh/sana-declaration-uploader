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
    _sanitize_address,
    build_setpetitiondata_payload,
    load_setpetitiondata_template,
    parse_set_petition_data_result,
    save_setpetitiondata_debug_text,
    submit_one_petition_data,
)


class SetPetitionDataClientTests(unittest.TestCase):
    def test_posts_wrapped_payload_once_and_parses_result(self):
        page = Mock()
        page.evaluate.return_value = {
            "status": 200,
            "text": (
                "1405220409964381;74469;"
                "E421AC18584E486B8EFA9B1CA98C8389"
            ),
        }

        result = SetPetitionDataClient(page).submit(
            {"PetitionTypeCode": "110", "Description": "متن"}
        )

        self.assertEqual(result.tracking_code, "1405220409964381")
        self.assertEqual(result.petition_number, "74469")
        self.assertEqual(
            result.petition_id, "E421AC18584E486B8EFA9B1CA98C8389"
        )
        page.evaluate.assert_called_once()
        expression, argument = page.evaluate.call_args.args
        self.assertIn("fetch(url", expression)
        self.assertIn("credentials: 'include'", expression)
        self.assertEqual(argument["url"], SET_PETITION_DATA_URL)
        envelope = json.loads(argument["body"])
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
        page = Mock()
        page.evaluate.side_effect = TimeoutError("sensitive transport detail")

        with self.assertRaisesRegex(
            SetPetitionDataError, "set_petition_data_transport_ambiguous"
        ):
            SetPetitionDataClient(page).submit({"Description": "متن"})

        page.evaluate.assert_called_once()

    def test_one_item_helper_uses_authenticated_page_context(self):
        page = Mock()
        page.evaluate.return_value = {
            "status": 200,
            "text": (
                "1405220409964381;74469;"
                "E421AC18584E486B8EFA9B1CA98C8389"
            ),
        }

        result = submit_one_petition_data(page, {"PetitionTypeCode": "110"})

        self.assertEqual(result.petition_number, "74469")
        page.evaluate.assert_called_once()

    def test_rejects_http_failure_without_exposing_response_body(self):
        page = Mock()
        page.evaluate.return_value = {
            "status": 500,
            "text": "private response",
        }

        with self.assertRaisesRegex(
            SetPetitionDataError, "set_petition_data_http_failed"
        ):
            SetPetitionDataClient(page).submit({"Description": "متن"})

        page.evaluate.assert_called_once()

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
            "RelatedNo": "template-related-number",
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
                    "NationalityCode": "old",
                    "BasicAvenue": "old",
                    "HomeAddress": "old",
                    "ManucipalityRegion": "تهران",
                    "ProvinceId": "old",
                    "ProvinceTitle": "old",
                    "StateId": "old",
                    "StateTitle": "old",
                    "CityId": "old",
                    "CityTitle": "old",
                    "NotifyOwnerOrganizationId": "old",
                    "NotifyOwnerOrganizationCode": "old",
                    "NotifyOwnerOrganizationTitle": "old",
                    "GetLocationForAddressId": "old",
                    "MobileNumber4SMS": "template-mobile",
                    "JobId": "template-job",
                    "JobTitle": "template-title",
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
                "recipient_identity_no": "98765",
                "recipient_main_street": "خیابان",
                "recipient_address": "نشانی",
                "recipient_postal_code": "1234567890",
                "job_code": "FREE-CODE",
                "job_id": "d2e420be1bf14c2088a14f04e7c71bb9",
                "related_declaration_number": "1405220897083662",
                "province_id": "province-id-from-lookup",
                "province_code": "01",
                "province": "استان",
                "county_id": "county-id-from-lookup",
                "county_code": "002",
                "county": "شهرستان",
                "city_id": "city-id-from-lookup",
                "city_code": "0003",
                "city": "شهر",
                "notify_owner_organization_id": "court-id",
                "notify_owner_organization_code": "01005",
                "notify_owner_organization_title": "حوزه قضایی شهرستان",
                "get_location_for_address_id": "address-id",
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
        self.assertEqual(result["RelatedNo"], "1405220897083662")
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
        self.assertEqual(recipient["NationalityCode"], "0012345678")
        self.assertEqual(recipient["IdentityNo"], "98765")
        self.assertEqual(recipient["ManucipalityRegion"], "")
        self.assertEqual(recipient["MobileNumber4SMS"], "")
        self.assertEqual(
            recipient["JobId"], "d2e420be1bf14c2088a14f04e7c71bb9"
        )
        self.assertEqual(recipient["JobTitle"], "آزاد")
        self.assertEqual(recipient["FatherName"], "پدر")
        self.assertEqual(recipient["PostallCode"], "1234567890")
        self.assertEqual(recipient["StateId"], "01")
        self.assertEqual(recipient["StateTitle"], "استان")
        self.assertEqual(recipient["ProvinceId"], "002")
        self.assertEqual(recipient["ProvinceTitle"], "شهرستان")
        self.assertEqual(recipient["CityId"], "0003")
        self.assertEqual(recipient["NotifyOwnerOrganizationId"], "court-id")
        self.assertEqual(recipient["NotifyOwnerOrganizationCode"], "01005")
        self.assertEqual(
            recipient["NotifyOwnerOrganizationTitle"], "حوزه قضایی شهرستان"
        )
        self.assertEqual(recipient["GetLocationForAddressId"], "address-id")
        self.assertEqual(template["Description"], "old description")

    def test_related_number_is_blank_when_not_extracted(self):
        draft = self._draft()
        draft.fields["related_declaration_number"] = ""

        payload = build_setpetitiondata_payload(self._template(), draft)

        self.assertEqual(payload["RelatedNo"], "")

    def test_national_code_is_not_used_as_identity_number(self):
        draft = self._draft()
        draft.fields.pop("recipient_identity_no")

        payload = build_setpetitiondata_payload(self._template(), draft)

        self.assertEqual(
            payload["theJSSPetitionPerson"][1]["IdentityNo"], ""
        )
        self.assertEqual(
            payload["theJSSPetitionPerson"][1]["NationalityCode"],
            "0012345678",
        )

    def test_sanitizes_both_address_fields(self):
        draft = self._draft()
        draft.fields.update({
            "recipient_main_street": "خيابان الف. Tel: 021123456",
            "recipient_address": (
                "استان تهران - شهرستان تهران - تهران - "
                "کوچه ب کد پستی: 1234567890"
            ),
        })

        recipient = build_setpetitiondata_payload(
            self._template(), draft
        )["theJSSPetitionPerson"][1]

        self.assertEqual(recipient["BasicAvenue"], "خيابان الف")
        self.assertEqual(
            recipient["HomeAddress"],
            "استان تهران - شهرستان تهران - تهران - "
            "کوچه ب کد پستی: 1234567890",
        )
        self.assertLessEqual(len(recipient["BasicAvenue"]), 29)
        self.assertGreater(len(recipient["HomeAddress"]), 10)
        self.assertNotIn("Tel", recipient["BasicAvenue"])
        self.assertIn("کد پستی: 1234567890", recipient["HomeAddress"])

    def test_main_street_is_limited_to_twenty_nine_characters(self):
        draft = self._draft()
        main_street = "الف" * 35
        draft.fields["recipient_main_street"] = main_street

        recipient = build_setpetitiondata_payload(
            self._template(), draft
        )["theJSSPetitionPerson"][1]

        self.assertEqual(recipient["BasicAvenue"], main_street[:29])
        self.assertEqual(len(recipient["BasicAvenue"]), 29)

    def test_sanitizer_removes_phone_postal_code_and_trailing_period(self):
        self.assertEqual(
            _sanitize_address("نشانی اصلی. تلفن تماس: 021123456"),
            "نشانی اصلی",
        )
        self.assertEqual(
            _sanitize_address("خیابان فرعی. کدپستی 1234567890"),
            "خیابان فرع",
        )
        self.assertEqual(_sanitize_address("123456789."), "123456789")

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
