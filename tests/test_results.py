from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from sana_declaration_uploader.models import DeclarationDraft, UploadResult
from sana_declaration_uploader.results import (
    ResultPersistenceError,
    existing_results,
    read_saved_result,
    save_verified_result,
)


class ResultPersistenceTests(unittest.TestCase):
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

    def test_saves_only_verified_result_beside_its_input(self):
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

            self.assertEqual(path.parent, source)
            self.assertEqual(path.name, "results.json")
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

            manifest = json.loads((source / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(
                len(manifest["declarations"]["sample.docx"]["registrations"]), 2
            )
            self.assertEqual(
                read_saved_result(source, source_path)["tracking_code"], "T-NEW"
            )

    def test_rejects_unverified_result_without_writing(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            draft = DeclarationDraft(
                source_dir=source, fields={}, source_path=source / "sample.docx"
            )

            with self.assertRaisesRegex(ResultPersistenceError, "unverified_result"):
                save_verified_result(draft, UploadResult(status="failed"))

            self.assertFalse((source / "results.json").exists())
