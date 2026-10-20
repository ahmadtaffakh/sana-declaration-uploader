from pathlib import Path
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import patch
import unittest

from sana_declaration_uploader.cli import build_parser, main


class CliTests(unittest.TestCase):
    def test_inspect_parses_requested_source_directory(self) -> None:
        source_dir = Path("sample-input")

        with patch(
            "sana_declaration_uploader.cli.parse_declaration_folder"
        ) as parse_declaration_folder:
            exit_code = main(["inspect", str(source_dir)])

        self.assertEqual(exit_code, 0)
        parse_declaration_folder.assert_called_once_with(source_dir)

    def test_scraping_commands_are_not_available(self) -> None:
        parser = build_parser()

        for command in ("capture-lookups", "promote-lookups"):
            with self.subTest(command=command), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as error:
                    parser.parse_args([command])

            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
