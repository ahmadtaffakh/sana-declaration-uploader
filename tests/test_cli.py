from pathlib import Path
from unittest.mock import patch
import unittest

from sana_declaration_uploader.cli import main


class CliTests(unittest.TestCase):
    def test_inspect_parses_requested_source_directory(self) -> None:
        source_dir = Path("sample-input")

        with patch(
            "sana_declaration_uploader.cli.parse_declaration_folder"
        ) as parse_declaration_folder:
            exit_code = main(["inspect", str(source_dir)])

        self.assertEqual(exit_code, 0)
        parse_declaration_folder.assert_called_once_with(source_dir)


if __name__ == "__main__":
    unittest.main()
