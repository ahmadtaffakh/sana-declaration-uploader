import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sana_declaration_uploader.catalog_capture import (
    capture_catalogs_from_page,
    normalize_capture_payload,
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
        "courts": [{"code": "00159", "title": "واحد تهران", "locationId": "t1"}],
    }

    def test_normalizes_hierarchy_and_matching_court(self) -> None:
        locations, courts = normalize_capture_payload(self.PAYLOAD)

        self.assertEqual(locations[0]["label"], "استان تهران")
        self.assertEqual(locations[1]["label"], "شهرستان تهران")
        self.assertEqual(locations[2]["parent_key"], "c1")
        self.assertEqual(courts[0]["location_id"], "t1")
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
                "court_units": 1,
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
            self.assertEqual(locations[2]["id"], "t1")
            self.assertEqual(courts[0]["location_id"], "t1")
            self.assertEqual(report["counts"]["unresolved_branches"], 0)


if __name__ == "__main__":
    unittest.main()
