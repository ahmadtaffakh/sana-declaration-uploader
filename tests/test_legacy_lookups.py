import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.import_legacy_lookups import import_legacy_lookups


class LegacyLookupImportTests(unittest.TestCase):
    def test_imports_only_supported_records_from_mixed_capture(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "capture.json"
            source.write_text(
                "<div>legacy page</div>\n"
                '[{"Id":"p1","LocationCode":"23","LocationName":"استان نمونه",'
                '"LocationType":2},{"Id":"c1","LocationCode":"2301",'
                '"LocationName":"شهرستان نمونه","LocationType":3},'
                '{"Id":"t1","LocationCode":"2301000001",'
                '"LocationName":"شهر نمونه","LocationType":5}]\n'
                "network log\n"
                '[{"Id":"u1","Code":"00123","UnitName":"واحد نمونه",'
                '"GeoLocationId":"t1"}]\n'
                '[{"Id":"j1","Code":"00004","Title":"بازنشسته","State":0},'
                '{"Id":"r1","Code":"1033","Title":"عنوان غیرشغلی","State":0}]\n',
                encoding="utf-8",
            )
            target = root / "lookups"

            counts = import_legacy_lookups(source, target)

            self.assertEqual(counts, {"locations": 3, "court_units": 1, "jobs": 1})
            locations = json.loads((target / "locations.json").read_text(encoding="utf-8"))
            county = next(item for item in locations if item["type"] == "county")
            city = next(item for item in locations if item["type"] == "city")
            self.assertEqual(county["parent_key"], "p1")
            self.assertEqual(city["parent_key"], "c1")
            jobs = json.loads((target / "jobs.json").read_text(encoding="utf-8"))
            self.assertEqual(jobs[0]["label"], "بازنشسته")
            self.assertEqual(jobs[0]["code"], "00004")

    def test_rejects_capture_without_reusable_catalog_records(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "empty.json"
            source.write_text('{"unrelated": true}', encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "legacy_lookup_records_missing"):
                import_legacy_lookups(source, root / "lookups")


if __name__ == "__main__":
    unittest.main()
