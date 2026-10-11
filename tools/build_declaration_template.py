"""Build the versioned RTL Word input template."""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "templates" / "declaration-batch-template.docx"
FONT_NAME = "Arial"
HEADER_FILL = "1F4E78"
INPUT_FILL = "EAF2F8"
BORDER = "D9D9D9"


def _set_rtl(paragraph) -> None:
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    properties = paragraph._p.get_or_add_pPr()
    if properties.find(qn("w:bidi")) is None:
        properties.append(OxmlElement("w:bidi"))


def _set_font(run, *, size: float = 11, bold: bool = False, white=False) -> None:
    run.font.name = FONT_NAME
    run.font.size = Pt(size)
    run.bold = bold
    run.font.color.rgb = RGBColor(255, 255, 255) if white else RGBColor(0, 0, 0)
    fonts = run._element.get_or_add_rPr().get_or_add_rFonts()
    for name in ("w:ascii", "w:hAnsi", "w:cs"):
        fonts.set(qn(name), FONT_NAME)


def _shade(cell, fill: str) -> None:
    properties = cell._tc.get_or_add_tcPr()
    shading = properties.find(qn("w:shd"))
    if shading is None:
        shading = OxmlElement("w:shd")
        properties.append(shading)
    shading.set(qn("w:fill"), fill)


def _set_cell_border(cell) -> None:
    properties = cell._tc.get_or_add_tcPr()
    borders = properties.first_child_found_in("w:tcBorders")
    if borders is None:
        borders = OxmlElement("w:tcBorders")
        properties.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = borders.find(qn(f"w:{edge}"))
        if element is None:
            element = OxmlElement(f"w:{edge}")
            borders.append(element)
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), "4")
        element.set(qn("w:color"), BORDER)


def _set_cell_text(cell, value: str, *, header: bool = False) -> None:
    cell.text = ""
    paragraph = cell.paragraphs[0]
    _set_rtl(paragraph)
    run = paragraph.add_run(value)
    _set_font(run, bold=header, white=header)
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    _shade(cell, HEADER_FILL if header else INPUT_FILL)
    _set_cell_border(cell)


def _add_table(document, headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...]):
    table = document.add_table(rows=1, cols=len(headers))
    table.autofit = True
    properties = table._tbl.tblPr
    if properties.find(qn("w:bidiVisual")) is None:
        properties.append(OxmlElement("w:bidiVisual"))
    for cell, value in zip(table.rows[0].cells, headers, strict=True):
        _set_cell_text(cell, value, header=True)
    header_properties = table.rows[0]._tr.get_or_add_trPr()
    header_properties.append(OxmlElement("w:tblHeader"))
    for values in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, values, strict=True):
            _set_cell_text(cell, value)
    document.add_paragraph()
    return table


def _add_heading(document, text: str, level: int = 1) -> None:
    paragraph = document.add_heading(text, level=level)
    _set_rtl(paragraph)
    for run in paragraph.runs:
        _set_font(run, size=14 if level == 1 else 12, bold=True)


def _add_text(document, text: str, *, bold_lead: str = "") -> None:
    paragraph = document.add_paragraph()
    _set_rtl(paragraph)
    if bold_lead:
        run = paragraph.add_run(bold_lead)
        _set_font(run, bold=True)
    run = paragraph.add_run(text)
    _set_font(run)


def build() -> Path:
    document = Document()
    section = document.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(0.65)
    section.bottom_margin = Inches(0.65)
    section.left_margin = Inches(0.65)
    section.right_margin = Inches(0.65)

    normal = document.styles["Normal"]
    normal.font.name = FONT_NAME
    normal.font.size = Pt(11)

    title = document.add_paragraph(style="Title")
    _set_rtl(title)
    run = title.add_run("قالب ثبت گروهی اظهارنامه")
    _set_font(run, size=18, bold=True)
    _add_text(
        document,
        "این فرم برای یک یا چند مخاطب است. سلول‌های آبی روشن را تکمیل کنید و عنوان جدول‌ها یا ستون‌ها را تغییر ندهید.",
    )
    _add_text(
        document,
        "پیش از ثبت، اطلاعات ابلاغیه، شناسه‌های سایت، شرح نهایی و پیوست‌های هر مخاطب جداگانه نمایش داده می‌شوند.",
    )

    _add_heading(document, "اطلاعات بسته")
    _add_table(
        document,
        ("نام فیلد", "مقدار"),
        (
            ("نسخه قالب", "2"),
            ("عنوان بسته", ""),
            ("موضوع اظهارنامه", ""),
            ("شناسه الگوی پیش فرض", "default"),
            ("نام واحد قضایی", ""),
            ("کد واحد قضایی", ""),
        ),
    )
    _add_text(
        document,
        "اگر نام واحد قضایی وارد شود، کد آن باید از فهرست تأییدشدهٔ سایت resolve شود. واردکردن هم‌زمان نام و کد متناقض باعث توقف می‌شود.",
        bold_lead="واحد قضایی: ",
    )

    _add_heading(document, "الگوهای شرح")
    _add_text(
        document,
        "برای شرح‌های متفاوت، ردیف جدید با شناسه انگلیسی یکتا اضافه کنید. شرح پیش‌فرض باید در ردیف default نوشته شود.",
    )
    _add_table(
        document,
        ("شناسه الگو", "عنوان الگو", "شرح"),
        (("default", "شرح پیش فرض", ""),),
    )

    _add_heading(document, "فهرست مخاطبان")
    _add_text(
        document,
        "منبع اطلاعات فقط ابلاغیه یا دستی است. برای ابلاغیه، شماره ابلاغیه الزامی است. شرح اختصاصی بر الگوی انتخاب‌شده اولویت دارد.",
    )
    _add_table(
        document,
        (
            "شناسه مخاطب",
            "منبع اطلاعات",
            "شماره ابلاغیه",
            "شماره اظهارنامه مرتبط",
            "شناسه الگو",
            "شرح اختصاصی",
        ),
        (("r1", "ابلاغیه", "", "", "", ""),),
    )

    _add_heading(document, "اطلاعات دستی مخاطبان")
    _add_text(
        document,
        "برای مخاطب دستی، هر مقدار را در یک ردیف وارد کنید. نام‌های مجاز شامل کد ملی، تاریخ تولد، جنسیت، نام، نام خانوادگی، نام پدر، شغل، استان، شهرستان، شهر، روستا، خیابان اصلی و نشانی است.",
    )
    _add_table(
        document,
        ("شناسه مخاطب", "نام فیلد", "مقدار"),
        (("", "", ""),),
    )

    _add_heading(document, "پارامترهای شرح")
    _add_text(
        document,
        "نام پارامتر باید انگلیسی باشد. خالی‌بودن شناسه مخاطب یعنی مقدار برای همه مشترک است؛ مقدار اختصاصی همان مخاطب بر مقدار مشترک اولویت دارد.",
    )
    _add_table(
        document,
        ("شناسه مخاطب", "نام پارامتر", "مقدار"),
        (("", "", ""),),
    )

    _add_heading(document, "پیوست‌ها")
    _add_text(
        document,
        "دامنه مشترک برای همه، الگو برای یک گروه شرح و مخاطب برای یک شخص است. شماره و تاریخ مدرک را عیناً از همان پیوست وارد کنید. نمایش برای مخاطب فقط بله یا خیر است. فایل‌ها باید داخل پوشه attachments باشند و داخل Word قرار نگیرند.",
    )
    _add_table(
        document,
        (
            "شناسه پیوست",
            "مسیر نسبی",
            "عنوان پیوست",
            "نوع پیوست",
            "شماره مدرک",
            "تاریخ مدرک",
            "نمایش برای مخاطب",
            "دامنه",
            "شناسه هدف",
        ),
        (("", "", "", "", "", "", "", "", ""),),
    )

    _add_heading(document, "فیلدهای قابل استفاده در شرح")
    _add_table(
        document,
        ("گروه", "نمونه"),
        (
            ("مخاطب", "{{recipient.full_name}}  {{recipient.address}}"),
            ("ابلاغیه", "{{notice.number}}  {{notice.date}}"),
            ("پارامتر", "{{param.amount}}  {{param.deadline_days}}"),
        ),
    )
    _add_text(
        document,
        "هر placeholder حل‌نشده، نامعتبر یا دارای املای اشتباه باعث توقف می‌شود. مقدار پارامتر به‌عنوان متن خام قرار می‌گیرد و اجرا نمی‌شود.",
    )

    document.core_properties.author = ""
    document.core_properties.last_modified_by = ""
    document.core_properties.title = "قالب ثبت گروهی اظهارنامه"
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    document.save(OUTPUT)
    return OUTPUT


if __name__ == "__main__":
    print(build())
