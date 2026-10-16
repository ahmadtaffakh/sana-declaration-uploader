import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sana_declaration_uploader.lookups import (
    LookupResolutionError,
    resolve_city_hierarchy,
    resolve_captured_jurisdiction_fields,
    resolve_lookup_fields,
    resolve_province_from_county,
    suggest_court_unit,
)


class LookupTests(unittest.TestCase):
    def test_captured_city_jurisdiction_overrides_parent_mapping(self) -> None:
        with TemporaryDirectory() as directory:
            lookup_dir = Path(directory)
            capture_dir = lookup_dir / "capture"
            capture_dir.mkdir()
            self._write(capture_dir / "jurisdictions.captured.json", [
                self._jurisdiction("province1", 2, "court-province", "00100"),
                self._jurisdiction("county1", 3, "court-county", "00200"),
                self._jurisdiction("city1", 5, "court-city", "00300"),
            ])

            result = resolve_captured_jurisdiction_fields(
                {
                    "province_id": "province1",
                    "county_id": "county1",
                    "city_id": "city1",
                },
                lookup_dir,
            )

        self.assertEqual(result["notify_owner_organization_id"], "court-city")
        self.assertEqual(result["notify_owner_organization_code"], "00300")

    def test_missing_city_jurisdiction_inherits_county_mapping(self) -> None:
        with TemporaryDirectory() as directory:
            lookup_dir = Path(directory)
            capture_dir = lookup_dir / "capture"
            capture_dir.mkdir()
            self._write(capture_dir / "jurisdictions.captured.json", [
                self._jurisdiction("province1", 2, "court-province", "00100"),
                self._jurisdiction("county1", 3, "court-county", "00200"),
            ])

            result = resolve_captured_jurisdiction_fields(
                {
                    "province_id": "province1",
                    "county_id": "county1",
                    "city_id": "city1",
                },
                lookup_dir,
            )

        self.assertEqual(result["notify_owner_organization_id"], "court-county")
        self.assertEqual(result["notify_owner_organization_code"], "00200")

    def test_duplicate_city_mappings_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            lookup_dir = Path(directory)
            capture_dir = lookup_dir / "capture"
            capture_dir.mkdir()
            first = self._jurisdiction("city1", 5, "court-a", "00300")
            second = self._jurisdiction("city1", 5, "court-b", "00400")
            self._write(
                capture_dir / "jurisdictions.captured.json", [first, second]
            )

            with self.assertRaisesRegex(
                LookupResolutionError, "jurisdiction_ambiguous:city_id"
            ):
                resolve_captured_jurisdiction_fields(
                    {"city_id": "city1"}, lookup_dir
                )

    def test_resolves_common_kohgiluyeh_spelling_variants(self) -> None:
        lookup_dir = Path(__file__).resolve().parents[1] / "lookups"

        for label in (
            "استان کهکیلویه و بویر احمد",
            "استان کهگیلویه و بویراحمد",
            "استان کهگلیویه و بویراحمد",
        ):
            with self.subTest(label=label):
                result = resolve_lookup_fields({"province": label}, lookup_dir)
                self.assertEqual(result["province_code"], "17")

    def test_location_matching_ignores_extra_spaces(self) -> None:
        lookup_dir = Path(__file__).resolve().parents[1] / "lookups"

        result = resolve_lookup_fields({
            "province": "استان خوزستان",
            "county": "شهرستان بندر ماهشهر",
        }, lookup_dir)

        self.assertEqual(result["county_code"], "0605")

    def test_resolves_city_and_its_unique_county_inside_province(self) -> None:
        lookup_dir = Path(__file__).resolve().parents[1] / "lookups"

        result = resolve_city_hierarchy(
            "استان کهگیلویه و بویر احمد", "دوگنبدان", lookup_dir
        )

        self.assertEqual(result["county"], "شهرستان گچساران")
        self.assertEqual(result["county_code"], "1703")
        self.assertEqual(result["city_code"], "1703021515")

    def test_resolves_county_and_its_verified_parent_province(self) -> None:
        lookup_dir = Path(__file__).resolve().parents[1] / "lookups"

        result = resolve_province_from_county(
            "استان گچساران.", lookup_dir
        )

        self.assertEqual(result["province"], "استان كهكيلويه وبويراحمد")
        self.assertEqual(result["province_code"], "17")
        self.assertEqual(result["county"], "شهرستان گچساران")
        self.assertEqual(result["county_code"], "1703")

    def test_ambiguous_county_parent_resolution_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            lookup_dir = Path(directory)
            self._write(
                lookup_dir / "locations.json",
                [
                    {
                        "key": "p1", "label": "استان یک", "type": "province",
                        "parent_key": "", "id": "1", "code": "P1",
                    },
                    {
                        "key": "p2", "label": "استان دو", "type": "province",
                        "parent_key": "", "id": "2", "code": "P2",
                    },
                    {
                        "key": "c1", "label": "شهرستان نمونه", "type": "county",
                        "parent_key": "p1", "id": "3", "code": "C1",
                    },
                    {
                        "key": "c2", "label": "شهرستان نمونه", "type": "county",
                        "parent_key": "p2", "id": "4", "code": "C2",
                    },
                ],
            )

            with self.assertRaisesRegex(
                LookupResolutionError, "lookup_ambiguous:county"
            ):
                resolve_province_from_county("نمونه", lookup_dir)

    def test_resolves_exact_hierarchical_values(self) -> None:
        with TemporaryDirectory() as directory:
            lookup_dir = Path(directory)
            self._write(
                lookup_dir / "jobs.json",
                [{"key": "lawyer", "label": "وكيل", "id": "11", "code": "J1"}],
            )
            self._write(
                lookup_dir / "locations.json",
                [
                    {
                        "key": "p1",
                        "label": "استان نمونه",
                        "type": "province",
                        "parent_key": "",
                        "id": "21",
                        "code": "P1",
                    },
                    {
                        "key": "c1",
                        "label": "شهرستان نمونه",
                        "type": "county",
                        "parent_key": "p1",
                        "id": "22",
                        "code": "C1",
                    },
                    {
                        "key": "t1",
                        "label": "شهر نمونه",
                        "type": "city",
                        "parent_key": "c1",
                        "id": "23",
                        "code": "T1",
                    },
                ],
            )
            self._write(
                lookup_dir / "court_units.json",
                [{"key": "court1", "label": "واحد نمونه", "code": "00001"}],
            )

            result = resolve_lookup_fields(
                {
                    "job_title": "وکیل",
                    "province": "استان نمونه",
                    "county": "شهرستان نمونه",
                    "city": "شهر نمونه",
                    "court_unit_name": "واحد نمونه",
                },
                lookup_dir,
            )

            self.assertEqual(result["job_code"], "J1")
            self.assertEqual(result["province_code"], "P1")
            self.assertEqual(result["county_code"], "C1")
            self.assertEqual(result["city_code"], "T1")
            self.assertEqual(result["court_unit_code"], "00001")

    def test_ambiguous_label_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            lookup_dir = Path(directory)
            self._write(
                lookup_dir / "jobs.json",
                [
                    {"key": "one", "label": "شغل نمونه", "id": "1", "code": "A"},
                    {"key": "two", "label": "شغل نمونه", "id": "2", "code": "B"},
                ],
            )

            with self.assertRaisesRegex(
                LookupResolutionError, "lookup_ambiguous:job"
            ):
                resolve_lookup_fields({"job_title": "شغل نمونه"}, lookup_dir)

    def test_court_code_is_verified_and_resolved(self) -> None:
        with TemporaryDirectory() as directory:
            lookup_dir = Path(directory)
            self._write(
                lookup_dir / "court_units.json",
                [{"key": "court1", "label": "واحد نمونه", "code": "00001"}],
            )

            result = resolve_lookup_fields(
                {"court_unit_code": "00001"}, lookup_dir
            )

            self.assertEqual(result["court_unit_name"], "واحد نمونه")
            self.assertEqual(result["court_unit_key"], "court1")

    def test_suggests_court_only_for_one_exact_location_match(self) -> None:
        with TemporaryDirectory() as directory:
            lookup_dir = Path(directory)
            self._write(
                lookup_dir / "court_units.json",
                [{
                    "key": "court1",
                    "label": "واحد نمونه",
                    "code": "00001",
                    "location_id": "city1",
                }],
            )

            result = suggest_court_unit({"city_id": "city1"}, lookup_dir)

            self.assertEqual(result["court_unit_code"], "00001")

    def test_jurisdiction_wording_selects_the_general_verified_unit(self) -> None:
        lookup_dir = Path(__file__).resolve().parents[1] / "lookups"

        result = resolve_lookup_fields(
            {
                "province": "استان تهران",
                "county": "شهرستان تهران",
                "city": "تهران",
                "court_unit_name": "حوزه قضايي شهرستان تهران",
            },
            lookup_dir,
        )

        self.assertEqual(result["court_unit_name"], "دادگستري كل استان تهران")
        self.assertEqual(result["court_unit_code"], "00159")

    def test_unknown_court_wording_without_location_still_fails(self) -> None:
        with TemporaryDirectory() as directory:
            lookup_dir = Path(directory)
            self._write(
                lookup_dir / "court_units.json",
                [{"key": "court1", "label": "واحد نمونه", "code": "00001"}],
            )

            with self.assertRaisesRegex(
                LookupResolutionError, "lookup_not_found:court_unit"
            ):
                resolve_lookup_fields(
                    {"court_unit_name": "حوزه نامشخص"}, lookup_dir
                )

    @staticmethod
    def _write(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def _jurisdiction(
        location_id: str, location_type: int, organization_id: str, code: str
    ) -> dict[str, object]:
        return {
            "locationId": location_id,
            "locationType": location_type,
            "locationCode": "01",
            "locationName": "مکان نمونه",
            "parentLocationId": "",
            "getLocationForAddressId": f"address-{location_id}",
            "organizationId": organization_id,
            "organizationCode": code,
            "organizationTitle": "حوزه نمونه",
        }


if __name__ == "__main__":
    unittest.main()
