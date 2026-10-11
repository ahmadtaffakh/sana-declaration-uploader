import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sana_declaration_uploader.lookups import (
    LookupResolutionError,
    resolve_lookup_fields,
)


class LookupTests(unittest.TestCase):
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

    @staticmethod
    def _write(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8"
        )


if __name__ == "__main__":
    unittest.main()
