from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from sana_declaration_uploader.models import DeclarationDraft, UploadResult
from sana_declaration_uploader.results import (
    ResultPersistenceError,
    save_verified_result,
)


class ResultPersistenceTests(unittest.TestCase):
    def test_saves_only_verified_result_beside_its_input(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            draft = DeclarationDraft(source_dir=source, fields={"sample": "value"})

            path = save_verified_result(
                draft,
                UploadResult(status="success", tracking_code="T-0001"),
            )

            self.assertEqual(path.parent, source)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"status": "success", "tracking_code": "T-0001"},
            )

    def test_rejects_unverified_result_without_writing(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)
            draft = DeclarationDraft(source_dir=source, fields={})

            with self.assertRaisesRegex(ResultPersistenceError, "unverified_result"):
                save_verified_result(draft, UploadResult(status="failed"))

            self.assertFalse((source / "result.json").exists())
