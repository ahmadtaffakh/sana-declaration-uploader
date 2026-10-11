from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from docx import Document

from sana_declaration_uploader.input_parser import (
    TemplateContractNotConfigured,
    parse_declaration_folder,
)
from sana_declaration_uploader.templating import render_request


class InputParserTests(unittest.TestCase):
    def test_parses_templates_parameters_and_attachment_scopes(self) -> None:
        with TemporaryDirectory() as directory:
            source_dir = Path(directory)
            attachments_dir = source_dir / "attachments"
            attachments_dir.mkdir()
            for name in ("agency.pdf", "group.pdf", "person.pdf"):
                (attachments_dir / name).write_bytes(b"test")

            document = self._base_document()
            self._add_table(
                document,
                ("شناسه الگو", "عنوان الگو", "شرح"),
                (
                    (
                        "default",
                        "پیش فرض",
                        "مخاطب {{recipient.full_name}} مبلغ {{param.amount}}",
                    ),
                    ("special", "خاص", "شرح دوم"),
                ),
            )
            self._add_table(
                document,
                (
                    "شناسه مخاطب",
                    "منبع اطلاعات",
                    "شماره ابلاغیه",
                    "شماره اظهارنامه مرتبط",
                    "شناسه الگو",
                    "شرح اختصاصی",
                ),
                (
                    ("r1", "دستی", "", "", "default", ""),
                    (
                        "r2",
                        "دستی",
                        "",
                        "",
                        "special",
                        "اختصاصی {{recipient.full_name}}",
                    ),
                ),
            )
            self._add_table(
                document,
                ("شناسه مخاطب", "نام فیلد", "مقدار"),
                (
                    ("r1", "نام", "علی"),
                    ("r1", "نام خانوادگی", "نمونه"),
                    ("r2", "نام", "سارا"),
                    ("r2", "نام خانوادگی", "آزمایشی"),
                ),
            )
            self._add_table(
                document,
                ("شناسه مخاطب", "نام پارامتر", "مقدار"),
                (("", "amount", "100"), ("r1", "amount", "200")),
            )
            self._add_table(
                document,
                (
                    "شناسه پیوست",
                    "مسیر نسبی",
                    "عنوان پیوست",
                    "نوع پیوست",
                    "دامنه",
                    "شناسه هدف",
                ),
                (
                    (
                        "agency",
                        "attachments/agency.pdf",
                        "نمایندگی",
                        "document",
                        "مشترک",
                        "",
                    ),
                    (
                        "group",
                        "attachments/group.pdf",
                        "پیوست گروه",
                        "document",
                        "الگو",
                        "default",
                    ),
                    (
                        "person",
                        "attachments/person.pdf",
                        "پیوست فرد",
                        "document",
                        "مخاطب",
                        "r2",
                    ),
                ),
            )
            document.save(source_dir / "declaration.docx")

            batch = parse_declaration_folder(source_dir)
            first, second = batch.requests

            self.assertEqual(batch.title, "بسته نمونه")
            self.assertEqual(
                [item.path.name for item in first.attachments],
                ["agency.pdf", "group.pdf"],
            )
            self.assertEqual(
                [item.path.name for item in second.attachments],
                ["agency.pdf", "person.pdf"],
            )
            self.assertEqual(first.parameters["amount"], "200")
            self.assertEqual(
                render_request(first).fields["declaration_text"],
                "مخاطب علی نمونه مبلغ 200",
            )
            self.assertEqual(
                render_request(second).fields["declaration_text"],
                "اختصاصی سارا آزمایشی",
            )

    def test_version_two_parses_site_attachment_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            source_dir = Path(directory)
            attachments_dir = source_dir / "attachments"
            attachments_dir.mkdir()
            (attachments_dir / "agency.pdf").write_bytes(b"test")

            document = self._base_document(version="2")
            self._add_table(
                document,
                ("شناسه الگو", "عنوان الگو", "شرح"),
                (("default", "پیش فرض", "شرح"),),
            )
            self._add_table(
                document,
                (
                    "شناسه مخاطب", "منبع اطلاعات", "شماره ابلاغیه",
                    "شماره اظهارنامه مرتبط", "شناسه الگو", "شرح اختصاصی",
                ),
                (("r1", "دستی", "", "", "default", ""),),
            )
            self._add_table(
                document, ("شناسه مخاطب", "نام فیلد", "مقدار"), ()
            )
            self._add_table(
                document, ("شناسه مخاطب", "نام پارامتر", "مقدار"), ()
            )
            self._add_table(
                document,
                (
                    "شناسه پیوست", "مسیر نسبی", "عنوان پیوست", "نوع پیوست",
                    "شماره مدرک", "تاریخ مدرک", "نمایش برای مخاطب",
                    "دامنه", "شناسه هدف",
                ),
                ((
                    "agency", "attachments/agency.pdf", "نامه نمایندگی",
                    "تصویر مدرک نمایندگی", "100", "1405/07/18", "بله",
                    "مشترک", "",
                ),),
            )
            document.save(source_dir / "declaration.docx")

            attachment = parse_declaration_folder(source_dir).requests[0].attachments[0]

            self.assertEqual(attachment.document_number, "100")
            self.assertEqual(attachment.document_date, "1405/07/18")
            self.assertTrue(attachment.show_to_recipient)

    def test_attachment_path_traversal_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            source_dir = Path(directory)
            document = self._base_document()
            self._add_table(
                document,
                ("شناسه الگو", "عنوان الگو", "شرح"),
                (("default", "پیش فرض", "شرح"),),
            )
            self._add_table(
                document,
                (
                    "شناسه مخاطب",
                    "منبع اطلاعات",
                    "شماره ابلاغیه",
                    "شماره اظهارنامه مرتبط",
                    "شناسه الگو",
                    "شرح اختصاصی",
                ),
                (("r1", "دستی", "", "", "default", ""),),
            )
            self._add_table(
                document,
                ("شناسه مخاطب", "نام فیلد", "مقدار"),
                (),
            )
            self._add_table(
                document,
                ("شناسه مخاطب", "نام پارامتر", "مقدار"),
                (),
            )
            self._add_table(
                document,
                (
                    "شناسه پیوست",
                    "مسیر نسبی",
                    "عنوان پیوست",
                    "نوع پیوست",
                    "دامنه",
                    "شناسه هدف",
                ),
                (("bad", "../outside.pdf", "خارج", "document", "مشترک", ""),),
            )
            document.save(source_dir / "declaration.docx")

            with self.assertRaisesRegex(
                TemplateContractNotConfigured, "attachment_outside_source"
            ):
                parse_declaration_folder(source_dir)

    @staticmethod
    def _base_document(*, version: str = "1") -> Document:
        document = Document()
        InputParserTests._add_table(
            document,
            ("نام فیلد", "مقدار"),
            (
                ("نسخه قالب", version),
                ("عنوان بسته", "بسته نمونه"),
                ("موضوع اظهارنامه", "موضوع نمونه"),
                ("شناسه الگوی پیش فرض", "default"),
                ("نام واحد قضایی", ""),
                ("کد واحد قضایی", ""),
            ),
        )
        return document

    @staticmethod
    def _add_table(
        document: Document,
        headers: tuple[str, ...],
        rows: tuple[tuple[str, ...], ...],
    ) -> None:
        table = document.add_table(rows=1, cols=len(headers))
        for cell, value in zip(table.rows[0].cells, headers, strict=True):
            cell.text = value
        for values in rows:
            cells = table.add_row().cells
            for cell, value in zip(cells, values, strict=True):
                cell.text = value


if __name__ == "__main__":
    unittest.main()
