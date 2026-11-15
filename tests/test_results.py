from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sqlite3
import unittest
from unittest.mock import patch

from sana_declaration_uploader.models import DeclarationDraft, UploadResult
from sana_declaration_uploader.results import (
    backfill_registration_report_fields,
    ResultPersistenceError,
    existing_results,
    import_sana_registration,
    migrate_legacy_registration,
    read_legacy_tracking_codes,
    read_saved_attachment_context,
    read_saved_result,
    registration_history,
    save_attachment_count,
    save_verified_result,
)


class ResultPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.database_dir = TemporaryDirectory()
        self.database_path = Path(self.database_dir.name) / "sana-history.sqlite3"
        self.database_patch = patch(
            "sana_declaration_uploader.results.history_database_path",
            return_value=self.database_path,
        )
        self.database_patch.start()

    def tearDown(self):
        self.database_patch.stop()
        self.database_dir.cleanup()

    def test_imports_unknown_sana_registration_once_and_marks_origin(self):
        petition = {
            "No": "2" * 16,
            "CreateDateTime": "1405/07/01 10:20:30",
            "RelatedNo": "R-2",
            "theJSSPetitionPerson": [{
                "RoleType": 2,
                "NationalityCode": "001",
                "Name": "نام",
                "Family": "خانوادگی",
            }],
        }

        self.assertEqual(import_sana_registration(petition), 1)
        self.assertEqual(import_sana_registration(petition), 0)

        row = registration_history()[0]
        self.assertEqual(row["tracking_code"], "2" * 16)
        self.assertEqual(row["origin"], "sana")
        self.assertEqual(row["source_file"], "دریافت‌شده از ثنا")
        self.assertEqual(row["national_id"], "001")

    def test_does_not_guess_identity_for_ambiguous_sana_recipients(self):
        petition = {
            "No": "3" * 16,
            "CreateDateTime": "1405/07/01 10:20:30",
            "theJSSPetitionPerson": [
                {"RoleType": 2, "Name": "اول", "Family": "نمونه"},
                {"RoleType": 2, "Name": "دوم", "Family": "نمونه"},
            ],
        }

        self.assertEqual(import_sana_registration(petition), 1)

        row = registration_history()[0]
        self.assertEqual(row["first_name"], "")
        self.assertEqual(row["last_name"], "")

    def test_migrates_existing_database_origin_to_local(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                """CREATE TABLE registrations (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       source_path TEXT NOT NULL,
                       source_file TEXT NOT NULL,
                       source_recipient_ordinal INTEGER NOT NULL,
                       tracking_code TEXT NOT NULL,
                       registered_at TEXT NOT NULL,
                       related_declaration_number TEXT NOT NULL DEFAULT '',
                       national_id TEXT NOT NULL DEFAULT '',
                       first_name TEXT NOT NULL DEFAULT '',
                       last_name TEXT NOT NULL DEFAULT '',
                       attachment_count INTEGER,
                       attachment_count_checked_at TEXT NOT NULL DEFAULT '',
                       attachment_context TEXT,
                       UNIQUE(source_path, source_recipient_ordinal,
                              tracking_code, registered_at)
                   )"""
            )
            connection.execute(
                """INSERT INTO registrations (
                       source_path, source_file, source_recipient_ordinal,
                       tracking_code, registered_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                ("old.docx", "old.docx", 1, "T-OLD", "old"),
            )
            connection.commit()
        finally:
            connection.close()

        self.assertEqual(registration_history()[0]["origin"], "local")

    def test_migrates_legacy_codes_to_each_recipient_key(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            source_path = source / "shared.docx"
            (source / "results.json").write_text(json.dumps({
                "schema_version": 1,
                "declarations": {
                    "shared.docx": {"registrations": [
                        {"tracking_code": "T-FIRST", "registered_at": "one"},
                        {"tracking_code": "T-SECOND", "registered_at": "two"},
                    ]},
                },
            }), encoding="utf-8")
            first = DeclarationDraft(
                source, {}, source_path=source_path,
                source_recipient_ordinal=1,
            )
            second = DeclarationDraft(
                source, {}, source_path=source_path,
                source_recipient_ordinal=2,
            )

            self.assertEqual(
                read_legacy_tracking_codes(source, source_path),
                ("T-FIRST", "T-SECOND"),
            )
            migrate_legacy_registration(second, "T-SECOND")
            migrate_legacy_registration(first, "T-FIRST")

            self.assertEqual(read_legacy_tracking_codes(source, source_path), ())
            self.assertEqual(
                read_saved_result(
                    source, source_path, source_recipient_ordinal=2,
                    source_recipient_count=2,
                )["tracking_code"],
                "T-SECOND",
            )

    def test_finds_existing_result_only_for_matching_word_file(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            first_word = source / "first.docx"
            second_word = source / "second.docx"
            draft = DeclarationDraft(
                source_dir=source,
                fields={},
                source_path=first_word,
            )
            save_verified_result(
                draft, UploadResult(status="success", tracking_code="T-0001")
            )
            drafts = (
                draft,
                DeclarationDraft(source_dir=source, fields={}, source_path=second_word),
            )

            prior = existing_results(drafts)
            self.assertEqual(len(prior), 1)
            self.assertEqual(prior[0][0], "first.docx")
            self.assertEqual(prior[0][1]["tracking_code"], "T-0001")

    def test_saves_only_verified_result_in_central_database(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            source_path = source / "sample.docx"
            draft = DeclarationDraft(
                source_dir=source,
                fields={"sample": "value"},
                source_path=source_path,
            )

            path = save_verified_result(
                draft,
                UploadResult(status="success", tracking_code="T-0001"),
            )

            self.assertEqual(path, self.database_path)
            saved = read_saved_result(source, source_path)
            self.assertEqual(saved["tracking_code"], "T-0001")
            self.assertTrue(saved["registered_at"])

    def test_preserves_registration_history_and_returns_latest_result(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            source_path = source / "sample.docx"
            draft = DeclarationDraft(source_dir=source, fields={}, source_path=source_path)
            save_verified_result(
                draft, UploadResult(status="success", tracking_code="T-OLD")
            )
            save_verified_result(
                draft, UploadResult(status="success", tracking_code="T-NEW")
            )

            self.assertEqual(len(registration_history()), 2)
            self.assertEqual(
                read_saved_result(source, source_path)["tracking_code"], "T-NEW"
            )

    def test_saves_attachment_count_on_matching_registration(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            source_path = source / "sample.docx"
            draft = DeclarationDraft(source, {}, source_path=source_path)
            save_verified_result(draft, UploadResult("success", "T-0001"))

            save_attachment_count(draft, "T-0001", 3)

            saved = read_saved_result(source, source_path)
            self.assertEqual(saved["attachment_count"], 3)
            self.assertTrue(saved["attachment_count_checked_at"])

    def test_attachment_count_does_not_create_unverified_registration(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            draft = DeclarationDraft(
                source, {}, source_path=source / "sample.docx"
            )

            with self.assertRaisesRegex(
                ResultPersistenceError, "verified_registration_not_found"
            ):
                save_attachment_count(draft, "T-UNKNOWN", 1)

    def test_rejects_unverified_result_without_writing(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            draft = DeclarationDraft(
                source_dir=source, fields={}, source_path=source / "sample.docx"
            )

            with self.assertRaisesRegex(ResultPersistenceError, "unverified_result"):
                save_verified_result(draft, UploadResult(status="failed"))

            self.assertFalse(self.database_path.exists())

    def test_history_contains_search_fields_and_is_newest_first(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            first = DeclarationDraft(
                source,
                {
                    "related_declaration_number": "R-1",
                    "recipient_national_id": "001",
                    "recipient_first_name": "علی",
                    "recipient_last_name": "نمونه",
                },
                source_path=source / "first.docx",
            )
            second = DeclarationDraft(
                source, {}, source_path=source / "second.docx"
            )
            save_verified_result(first, UploadResult("success", "T-FIRST"))
            save_verified_result(second, UploadResult("success", "T-SECOND"))

            history = registration_history()

            self.assertEqual(history[0]["tracking_code"], "T-SECOND")
            self.assertEqual(history[1]["related_declaration_number"], "R-1")
            self.assertEqual(history[1]["national_id"], "001")
            self.assertEqual(history[1]["first_name"], "علی")
            self.assertEqual(history[1]["last_name"], "نمونه")

    def test_backfill_only_fills_missing_report_fields(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            source_path = source / "sample.docx"
            draft = DeclarationDraft(
                source,
                {"recipient_first_name": "نام بازبینی‌شده"},
                source_path=source_path,
            )
            save_verified_result(draft, UploadResult("success", "T-1"))

            backfill_registration_report_fields(
                source_path,
                1,
                {
                    "recipient_first_name": "نام استخراجی",
                    "recipient_last_name": "خانوادگی استخراجی",
                    "recipient_national_id": "001",
                },
            )
            row = registration_history()[0]

            self.assertEqual(row["first_name"], "نام بازبینی‌شده")
            self.assertEqual(row["last_name"], "خانوادگی استخراجی")
            self.assertEqual(row["national_id"], "001")

    def test_backfill_can_target_one_exact_tracking_code(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            source_path = source / "sample.docx"
            draft = DeclarationDraft(source, {}, source_path=source_path)
            save_verified_result(draft, UploadResult("success", "T-OLD"))
            save_verified_result(draft, UploadResult("success", "T-NEW"))

            updated = backfill_registration_report_fields(
                source_path,
                1,
                {"recipient_first_name": "نام ثنا"},
                tracking_code="T-OLD",
            )

            rows = {row["tracking_code"]: row for row in registration_history()}
            self.assertEqual(updated, 1)
            self.assertEqual(rows["T-OLD"]["first_name"], "نام ثنا")
            self.assertEqual(rows["T-NEW"]["first_name"], "")

    def test_reads_attachment_context_for_saved_tracking_code(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            source_path = source / "sample.docx"
            draft = DeclarationDraft(source, {}, source_path=source_path)
            context = {
                "petition_number": "1405000000000001",
                "petition_id": "petition-id",
                "petition_type_code": "110",
                "reliefs": [{"Id": "relief-id"}],
            }
            save_verified_result(
                draft, UploadResult("success", "1405000000000001"),
                attachment_context=context,
            )

            self.assertEqual(
                read_saved_attachment_context(source, source_path),
                {"tracking_code": "1405000000000001", **context},
            )

    def test_keeps_two_recipients_from_one_source_separate(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            source_path = source / "sample.docx"
            first = DeclarationDraft(
                source, {}, source_path=source_path,
                source_recipient_ordinal=1,
            )
            second = DeclarationDraft(
                source, {}, source_path=source_path,
                source_recipient_ordinal=2,
            )

            first_context = {
                "petition_number": "T-FIRST",
                "petition_id": "petition-first",
                "petition_type_code": "110",
                "reliefs": [{"Id": "relief-first"}],
            }
            second_context = {
                "petition_number": "T-SECOND",
                "petition_id": "petition-second",
                "petition_type_code": "110",
                "reliefs": [{"Id": "relief-second"}],
            }
            save_verified_result(
                first, UploadResult("success", "T-FIRST"),
                attachment_context=first_context,
            )
            save_verified_result(
                second, UploadResult("success", "T-SECOND"),
                attachment_context=second_context,
            )

            self.assertEqual(
                read_saved_result(
                    source, source_path,
                    source_recipient_ordinal=1,
                    source_recipient_count=2,
                )["tracking_code"],
                "T-FIRST",
            )
            self.assertEqual(
                read_saved_result(
                    source, source_path,
                    source_recipient_ordinal=2,
                    source_recipient_count=2,
                )["tracking_code"],
                "T-SECOND",
            )
            self.assertEqual(
                read_saved_attachment_context(
                    source, source_path,
                    source_recipient_ordinal=1,
                    source_recipient_count=2,
                ),
                {"tracking_code": "T-FIRST", **first_context},
            )
            self.assertEqual(
                read_saved_attachment_context(
                    source, source_path,
                    source_recipient_ordinal=2,
                    source_recipient_count=2,
                ),
                {"tracking_code": "T-SECOND", **second_context},
            )

    def test_reads_unambiguous_legacy_multi_recipient_history_by_ordinal(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            source_path = source / "sample.docx"
            (source / "results.json").write_text(json.dumps({
                "schema_version": 1,
                "declarations": {
                    "sample.docx": {"registrations": [
                        {
                            "tracking_code": "T-FIRST",
                            "registered_at": "one",
                            "attachment_context": {
                                "petition_number": "T-FIRST",
                                "petition_id": "petition-first",
                                "petition_type_code": "110",
                                "reliefs": [{"Id": "relief-first"}],
                            },
                        },
                        {
                            "tracking_code": "T-SECOND",
                            "registered_at": "two",
                            "attachment_context": {
                                "petition_number": "T-SECOND",
                                "petition_id": "petition-second",
                                "petition_type_code": "110",
                                "reliefs": [{"Id": "relief-second"}],
                            },
                        },
                    ]}
                },
            }), encoding="utf-8")

            first = read_saved_result(
                source, source_path,
                source_recipient_ordinal=1,
                source_recipient_count=2,
            )
            second = read_saved_result(
                source, source_path,
                source_recipient_ordinal=2,
                source_recipient_count=2,
            )

            self.assertEqual(first["tracking_code"], "T-FIRST")
            self.assertEqual(second["tracking_code"], "T-SECOND")
            self.assertEqual(
                read_saved_attachment_context(
                    source, source_path,
                    source_recipient_ordinal=2,
                    source_recipient_count=2,
                )["petition_id"],
                "petition-second",
            )

    def test_rejects_ambiguous_legacy_multi_recipient_history(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            source_path = source / "sample.docx"
            (source / "results.json").write_text(json.dumps({
                "schema_version": 1,
                "declarations": {
                    "sample.docx": {"registrations": [
                        {"tracking_code": "T-OLD", "registered_at": "old"},
                        {"tracking_code": "T-FIRST", "registered_at": "one"},
                        {"tracking_code": "T-SECOND", "registered_at": "two"},
                    ]}
                },
            }), encoding="utf-8")

            with self.assertRaisesRegex(
                ResultPersistenceError,
                "legacy_results_ambiguous_for_multiple_recipients",
            ):
                read_saved_result(
                    source, source_path,
                    source_recipient_ordinal=1,
                    source_recipient_count=2,
                )
