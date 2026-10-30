import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackagingConfigurationTests(unittest.TestCase):
    def test_spec_is_portable_and_rejects_codex_runtime_dlls(self):
        text = (ROOT / "SanaDeclarationUploader.spec").read_text(encoding="utf-8")

        self.assertIn("PROJECT_ROOT = Path(SPECPATH)", text)
        self.assertIn('"codex-runtimes"', text)
        self.assertNotIn(r"F:\sana-declaration-uploader", text)

    def test_windows_build_uses_clean_path_and_staging_output(self):
        text = (ROOT / "tools" / "build_windows.ps1").read_text(encoding="utf-8")

        self.assertIn("$env:PATH = @(", text)
        self.assertIn("build\\dist-release", text)
        self.assertIn('"codex-runtimes"', text)
        self.assertNotIn("dist\\SanaDeclarationUploader", text)


if __name__ == "__main__":
    unittest.main()
