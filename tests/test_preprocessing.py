import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sana_declaration_uploader.models import RecipientCandidate
from sana_declaration_uploader.preprocessing import (
    extract_address_labels,
    preprocess_candidate,
)


class RecipientPreprocessingTests(unittest.TestCase):
    def test_extracts_hierarchical_address_labels(self) -> None:
        labels, issues = extract_address_labels(
            "استان تهران - شهرستان تهران - شهر تهران - خیابان نمونه"
        )

        self.assertEqual(labels, {
            "province": "استان تهران",
            "county": "شهرستان تهران",
            "city": "تهران",
        })
        self.assertEqual(issues, ())

    def test_marks_different_group_locations_ambiguous(self) -> None:
        labels, issues = extract_address_labels(
            "استان تهران - شهرستان تهران - تهران | "
            "استان فارس - شهرستان شیراز - شیراز"
        )

        self.assertEqual(labels, {})
        self.assertEqual(set(issues), {
            "province_address_ambiguous",
            "county_address_ambiguous",
            "city_address_ambiguous",
        })

    def test_forces_free_job_and_does_not_suggest_court(self) -> None:
        with TemporaryDirectory() as directory:
            lookup_dir = Path(directory)
            self._write(lookup_dir / "jobs.json", [{
                "key": "j1", "label": "آزاد", "id": "j1", "code": "J1",
            }])
            self._write(lookup_dir / "locations.json", [
                {"key": "p1", "label": "استان تهران", "type": "province", "parent_key": "", "id": "p1", "code": "23"},
                {"key": "c1", "label": "شهرستان تهران", "type": "county", "parent_key": "p1", "id": "c1", "code": "2301"},
                {"key": "t1", "label": "تهران", "type": "city", "parent_key": "c1", "id": "t1", "code": "2301000001"},
            ])
            self._write(lookup_dir / "court_units.json", [{
                "key": "u1", "label": "واحد تهران", "code": "00123", "location_id": "t1",
            }])
            candidate = RecipientCandidate(
                source_path=lookup_dir / "source.pdf",
                full_name="شخص نمونه",
                job_title="کارمند",
                address="استان تهران - شهرستان تهران - تهران - خیابان نمونه",
            )

            result = preprocess_candidate(candidate, lookup_dir)

            self.assertEqual(result.job_title, "آزاد")
            self.assertEqual(result.job_code, "J1")
            self.assertEqual(result.province_code, "23")
            self.assertEqual(result.county_code, "2301")
            self.assertEqual(result.city_code, "2301000001")
            self.assertEqual(result.court_unit_code, "")
            self.assertEqual(result.issues, ())

    def test_unknown_location_falls_back_to_tehran(self) -> None:
        lookup_dir = Path(__file__).resolve().parents[1] / "lookups"
        candidate = RecipientCandidate(
            source_path=lookup_dir / "source.pdf",
            full_name="شخص نمونه",
            address="استان نمونه - شهرستان نمونه - شهر نمونه - خیابان نمونه",
        )

        result = preprocess_candidate(candidate, lookup_dir)

        self.assertEqual(
            (result.province, result.county, result.city),
            ("استان تهران", "شهرستان تهران", "تهران"),
        )
        self.assertEqual(result.job_title, "آزاد")
        self.assertIn("location_defaulted", result.issues)

    @staticmethod
    def _write(path: Path, value: object) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
