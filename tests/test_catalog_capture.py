import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sana_declaration_uploader.catalog_capture import (
    capture_catalogs_from_page,
    normalize_capture_payload,
    promote_captured_catalogs,
)
from sana_declaration_uploader.live_browser import LiveBrowserError


class _Page:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def evaluate(self, script, payload):
        self.calls.append((script, payload))
        return self.result


class CatalogCaptureTests(unittest.TestCase):
    PAYLOAD = {
        "locations": [
            {
                "Id": "p1", "LocationCode": "23", "LocationName": "تهران",
                "LocationType": 2, "ParentId": "",
            },
            {
                "Id": "c1", "LocationCode": "2301", "LocationName": "تهران",
                "LocationType": 3, "ParentId": "p1",
            },
            {
                "Id": "t1", "LocationCode": "2301000001", "LocationName": "تهران",
                "LocationType": 5, "ParentId": "c1",
            },
        ],
        "courts": [
            {"code": "00159", "title": "واحد تهران", "locationId": key}
            for key in ("p1", "c1", "t1")
        ],
        "jurisdictions": [
            {
                "locationId": key,
                "locationType": location_type,
                "locationCode": location_code,
                "locationName": location_name,
                "parentLocationId": parent,
                "getLocationForAddressId": "address1",
                "organizationId": "org1",
                "organizationCode": "00159",
                "organizationTitle": "واحد تهران",
            }
            for key, location_type, location_code, location_name, parent in (
                ("p1", 2, "23", "تهران", ""),
                ("c1", 3, "2301", "تهران", "p1"),
                ("t1", 5, "2301000001", "تهران", "c1"),
            )
        ],
    }

    def test_normalizes_hierarchy_and_matching_court(self) -> None:
        locations, courts = normalize_capture_payload(self.PAYLOAD)

        self.assertEqual(locations[0]["label"], "استان تهران")
        self.assertEqual(locations[1]["label"], "شهرستان تهران")
        self.assertEqual(locations[2]["parent_key"], "c1")
        self.assertEqual(courts[-1]["location_id"], "t1")
        self.assertEqual(courts[0]["code"], "00159")

    def test_missing_parent_fails_before_writing(self) -> None:
        payload = {**self.PAYLOAD, "locations": self.PAYLOAD["locations"][1:]}

        with self.assertRaisesRegex(LiveBrowserError, "parent_missing"):
            normalize_capture_payload(payload)

    def test_missing_court_capture_fails_before_writing(self) -> None:
        payload = {**self.PAYLOAD, "courts": []}

        with self.assertRaisesRegex(LiveBrowserError, "capture_incomplete"):
            normalize_capture_payload(payload)

    def test_writes_separate_capture_files(self) -> None:
        with TemporaryDirectory() as directory:
            output_dir = Path(directory)
            counts = capture_catalogs_from_page(_Page(self.PAYLOAD), output_dir)

            self.assertEqual(counts, {
                "locations": 3,
                "court_units": 3,
                "jurisdictions": 3,
                "unresolved_branches": 0,
            })
            locations = json.loads(
                (output_dir / "locations.captured.json").read_text(encoding="utf-8")
            )
            courts = json.loads(
                (output_dir / "court_units.captured.json").read_text(encoding="utf-8")
            )
            report = json.loads(
                (output_dir / "capture_report.json").read_text(encoding="utf-8")
            )
            jurisdictions = json.loads(
                (output_dir / "jurisdictions.captured.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(locations[2]["id"], "t1")
            self.assertEqual(courts[-1]["location_id"], "t1")
            self.assertEqual(report["counts"]["unresolved_branches"], 0)
            self.assertEqual(len(jurisdictions), 3)
            self.assertEqual(jurisdictions[0]["organizationId"], "org1")
            self.assertEqual(
                [entry["organizationCode"] for entry in jurisdictions],
                ["00159", "00159", "00159"],
            )

    def test_capture_observes_cascading_recipient_model_without_api_enumeration(self) -> None:
        with TemporaryDirectory() as directory:
            page = _Page(self.PAYLOAD)
            capture_catalogs_from_page(page, Path(directory))

        script = page.calls[0][0]
        for observed_contract in (
            "NotifyOwnerOrganizationId",
            "NotifyOwnerOrganizationCode",
            "NotifyOwnerOrganizationTitle",
            "GetLocationForAddressId",
            "collectCurrentCourt({...state, ParentId: ''})",
            "collectCurrentCourt({...county, ParentId: state.Id})",
            "collectCurrentCourt({...city, ParentId: county.Id})",
        ):
            self.assertIn(observed_contract, script)
        self.assertNotIn("postJson(", script)
        self.assertNotIn("fetch(`/Offices/", script)

    def test_promotes_valid_rows_and_excludes_reported_branch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            capture_dir = root / "capture"
            lookup_dir = root / "lookups"
            capture_dir.mkdir()
            lookup_dir.mkdir()
            locations, courts = normalize_capture_payload(self.PAYLOAD)
            excluded = locations[-1]
            courts.append({
                "key": "00160:c1",
                "label": "واحد شهرستان",
                "code": "00160",
                "location_id": "c1",
                "source": "authenticated SANA address selection",
            })
            (capture_dir / "locations.captured.json").write_text(
                json.dumps(locations, ensure_ascii=False), encoding="utf-8"
            )
            (capture_dir / "court_units.captured.json").write_text(
                json.dumps(courts, ensure_ascii=False), encoding="utf-8"
            )
            (capture_dir / "capture_report.json").write_text(
                json.dumps({
                    "counts": {"locations": 3, "court_units": 4},
                    "issues": [{"parentId": excluded["id"]}],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            existing_courts = [{"key": "old", "label": "واحد قبلی", "code": "00001"}]
            (lookup_dir / "court_units.json").write_text(
                json.dumps(existing_courts, ensure_ascii=False), encoding="utf-8"
            )

            counts = promote_captured_catalogs(capture_dir, lookup_dir)

            self.assertEqual(counts, {
                "locations": 2,
                "court_units": 1,
                "excluded_branches": 1,
            })
            self.assertEqual(
                json.loads((lookup_dir / "court_units.json").read_text(encoding="utf-8")),
                existing_courts,
            )


if __name__ == "__main__":
    unittest.main()
