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

    def test_removes_trailing_dots_from_extracted_location_labels(self) -> None:
        labels, issues = extract_address_labels(
            "استان تهران. - شهرستان تهران. - شهر تهران. - خیابان نمونه"
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

    def test_incomplete_location_falls_back_to_tehran(self) -> None:
        lookup_dir = Path(__file__).resolve().parents[1] / "lookups"
        candidate = RecipientCandidate(
            source_path=lookup_dir / "source.pdf",
            full_name="شخص نمونه",
            address="خیابان نمونه",
        )

        result = preprocess_candidate(candidate, lookup_dir)

        self.assertEqual(
            (result.province, result.county, result.city),
            ("استان تهران", "شهرستان تهران", "تهران"),
        )
        self.assertEqual(result.job_title, "آزاد")
        self.assertIn("location_defaulted", result.issues)

    def test_preserves_extracted_location_when_lookup_is_unresolved(self) -> None:
        lookup_dir = Path(__file__).resolve().parents[1] / "lookups"
        candidate = RecipientCandidate(
            source_path=lookup_dir / "source.pdf",
            full_name="شخص نمونه",
            address="استان نمونه - شهرستان نمونه - شهر نمونه - خیابان نمونه",
            province="استان نمونه",
            county="شهرستان نمونه",
            city="نمونه",
        )

        result = preprocess_candidate(candidate, lookup_dir)

        self.assertEqual(
            (result.province, result.county, result.city),
            ("استان نمونه", "شهرستان نمونه", "نمونه"),
        )
        self.assertIn("location_lookup_unresolved", result.issues)
        self.assertNotIn("location_defaulted", result.issues)

    def test_keeps_resolved_province_code_when_child_lookup_fails(self) -> None:
        lookup_dir = Path(__file__).resolve().parents[1] / "lookups"
        candidate = RecipientCandidate(
            source_path=lookup_dir / "source.pdf",
            full_name="شخص نمونه",
            address="نشانی کامل",
            province="استان خوزستان",
            county="شهرستان نامعتبر",
            city="شهر نامعتبر",
        )

        result = preprocess_candidate(candidate, lookup_dir)

        self.assertEqual(result.province_code, "06")
        self.assertEqual(result.county_code, "")
        self.assertEqual(result.city_code, "")
        self.assertIn("location_lookup_unresolved", result.issues)

    def test_replaces_county_name_in_province_with_verified_parent(self) -> None:
        lookup_dir = Path(__file__).resolve().parents[1] / "lookups"
        candidate = RecipientCandidate(
            source_path=lookup_dir / "source.pdf",
            full_name="شخص نمونه",
            address="نشانی کامل",
            province="شهرستان گچساران.",
            county="شهرستان گچساران.",
            city="دوگنبدان.",
        )

        result = preprocess_candidate(candidate, lookup_dir)

        self.assertEqual(result.province, "استان كهكيلويه وبويراحمد")
        self.assertEqual(result.province_code, "17")
        self.assertEqual(result.county, "شهرستان گچساران")
        self.assertEqual(result.county_code, "1703")
        self.assertEqual(result.city, "دوگنبدان")
        self.assertEqual(result.city_code, "1703021515")
        self.assertNotIn("location_lookup_unresolved", result.issues)

    def test_rey_county_resolves_its_tehran_province(self) -> None:
        lookup_dir = Path(__file__).resolve().parents[1] / "lookups"
        candidate = RecipientCandidate(
            source_path=lookup_dir / "source.docx",
            full_name="شخص نمونه",
            address="شهرستان ری - شهر ری - خیابان نمونه",
            province="شهرستان ری",
            county="شهرستان ری",
            city="ری",
        )

        result = preprocess_candidate(candidate, lookup_dir)

        self.assertEqual(result.province, "استان تهران")
        self.assertEqual(result.province_code, "23")
        self.assertEqual(result.county, "شهرستان ری")
        self.assertEqual(result.county_code, "2303")
        self.assertEqual(result.city, "ری")
        self.assertEqual(result.city_code, "2303042115")
        self.assertNotIn("location_lookup_unresolved", result.issues)

    def test_invalid_city_uses_resolved_county_name_as_city(self) -> None:
        lookup_dir = Path(__file__).resolve().parents[1] / "lookups"
        for county, invalid_city, expected_city in (
            ("شهرستان ایلام", "امتداد خ پاسداران", "ایلام"),
            ("شهرستان کرمانشاه", "تعاون", "کرمانشاه"),
        ):
            with self.subTest(county=county):
                result = preprocess_candidate(RecipientCandidate(
                    source_path=lookup_dir / "source.pdf",
                    full_name="شخص نمونه",
                    address="نشانی کامل",
                    province=(
                        "استان ایلام" if expected_city == "ایلام"
                        else "استان کرمانشاه"
                    ),
                    county=county,
                    city=invalid_city,
                ), lookup_dir)

                self.assertEqual(result.city, expected_city)
                self.assertTrue(result.city_code)
                self.assertNotIn("location_lookup_unresolved", result.issues)

    def test_gachsaran_county_always_uses_dogonbadan_city(self) -> None:
        lookup_dir = Path(__file__).resolve().parents[1] / "lookups"
        candidate = RecipientCandidate(
            source_path=lookup_dir / "source.docx",
            full_name="شخص نمونه",
            address="نشانی کامل",
            province="استان کهگیلویه و بویر احمد",
            county="شهرستان گچساران",
            city="گچساران",
        )

        result = preprocess_candidate(candidate, lookup_dir)

        self.assertEqual(result.city, "دوگنبدان")
        self.assertEqual(result.city_code, "1703021515")
        self.assertNotIn("location_lookup_unresolved", result.issues)

    def test_city_name_resolves_its_county_and_known_spelling_variants(self) -> None:
        lookup_dir = Path(__file__).resolve().parents[1] / "lookups"
        cases = (
            (
                "استان کهگیلویه و بویر احمد", "دوگنبدان",
                "شهرستان گچساران", "دوگنبدان", "1703", "1703021515",
            ),
            (
                "استان خوزستان", "رامهرز",
                "شهرستان رامهرمز", "رامهرمز", "0610", "0610021287",
            ),
            (
                "استان کرمانشاه", "اسلام اباد غرب",
                "شهرستان اسلام آبادغرب", "اسلام آبادغرب", "0501", "0501031256",
            ),
        )
        for province, city, county, expected_city, county_code, city_code in cases:
            with self.subTest(city=city):
                result = preprocess_candidate(RecipientCandidate(
                    source_path=lookup_dir / "source.docx",
                    full_name="شخص نمونه",
                    address=f"{province}-{city}",
                    province=province,
                    county=f"شهرستان {city}",
                    city=city,
                ), lookup_dir)

                self.assertEqual(result.county, county)
                self.assertEqual(result.city, expected_city)
                self.assertEqual(result.county_code, county_code)
                self.assertEqual(result.city_code, city_code)
                self.assertNotIn("location_lookup_unresolved", result.issues)

    @staticmethod
    def _write(path: Path, value: object) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
