from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings
from django.http import FileResponse, Http404

SIGNATURES = ("Người lập biểu", "Người kiểm tra", "Hiệu trưởng")
MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def document(request, title, subtitle, headers, rows, filename, format, landscape=False):
    if format not in MIME:
        raise Http404
    place = request.school.location
    stream = BytesIO()
    args = (stream, request.school.name, title, subtitle, headers, rows, landscape, place)
    {"pdf": pdf, "docx": docx, "xlsx": xlsx}[format](*args)
    stream.seek(0)
    inline = format == "pdf" and request.GET.get("download") != "1"
    return FileResponse(stream, as_attachment=not inline, filename=f"{filename}.{format}", content_type=MIME[format])


def column_weights(headers):
    if len(headers) == 12:
        return [2.55, 1.65, 1.05, 1.05, 0.8, 0.75, 0.6, 0.6, 0.85, 0.9, 1.0, 0.75]
    return [1] * len(headers)


def pdf(stream, school, title, subtitle, headers, rows, wide, place):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import KeepTogether, LongTable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font_dir = Path(getattr(settings, "PDF_FONT_DIR", "/usr/share/fonts/truetype/dejavu"))
    pdfmetrics.registerFont(TTFont("DejaVu", str(font_dir / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("DejaVuBold", str(font_dir / "DejaVuSans-Bold.ttf")))
    size = landscape(A4) if wide else A4
    doc = SimpleDocTemplate(stream, pagesize=size, leftMargin=31, rightMargin=31, topMargin=34, bottomMargin=38,
                            title=title, author=school)
    cell = ParagraphStyle("cell", fontName="DejaVu", fontSize=8, leading=11, splitLongWords=True)
    numeric = ParagraphStyle("numeric", parent=cell, alignment=TA_CENTER)
    header = ParagraphStyle("header", parent=cell, fontName="DejaVuBold", alignment=TA_CENTER)
    heading = ParagraphStyle("heading", parent=header, fontSize=14, leading=19)
    right = ParagraphStyle("right", parent=cell, alignment=TA_RIGHT)
    grid = [[Paragraph(escape(str(text)), header) for text in headers]]
    grid += [[Paragraph(escape(str(value)), cell if isinstance(value, str) else numeric)
              for value in row] for row in rows]
    width = size[0] - 74
    weights = column_weights(headers)
    table = LongTable(grid, colWidths=[width * weight / sum(weights) for weight in weights], repeatRows=1, splitInRow=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#efefef")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#8e8e8e")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    signatures = Table([[Paragraph(text, header) for text in SIGNATURES]], colWidths=[width / 3] * 3)
    closing = KeepTogether([Spacer(1, 12), Paragraph(f"{escape(place)}, ngày …… tháng …… năm ……", right),
                            Spacer(1, 12), signatures, Spacer(1, 45)])
    flow = [Paragraph(escape(school), header), Spacer(1, 8), Paragraph(escape(title), heading),
            Paragraph(escape(subtitle), header), Spacer(1, 12), table, closing]

    def footer(canvas, _doc):
        canvas.setFont("DejaVu", 8)
        canvas.drawRightString(size[0] - 36, 22, f"Trang {canvas.getPageNumber()}")

    doc.build(flow, onFirstPage=footer, onLaterPages=footer)


def docx(stream, school, title, subtitle, headers, rows, wide, place):
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.shared import Mm, Pt

    doc = Document()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE if wide else WD_ORIENT.PORTRAIT
    section.page_width, section.page_height = (Mm(297), Mm(210)) if wide else (Mm(210), Mm(297))
    section.top_margin = section.bottom_margin = section.left_margin = section.right_margin = Mm(13)
    normal = doc.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(9)
    normal.paragraph_format.space_after = Pt(3)
    for text, bold, size in [(school, True, 11), (title, True, 15), (subtitle, False, 10)]:
        paragraph = doc.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = paragraph.add_run(text)
        run.bold, run.font.size = bold, Pt(size)
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.autofit = False
    available = section.page_width - section.left_margin - section.right_margin
    weights = column_weights(headers)
    for column, weight in zip(table.columns, weights):
        column.width = round(available * weight / sum(weights))
    for cell, column in zip(table.rows[0].cells, table.columns):
        cell.width = column.width
    table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
    for cell, value in zip(table.rows[0].cells, headers):
        cell.paragraphs[0].add_run(value).bold = True
    for row in rows:
        new = table.add_row()
        new._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
        for cell, value, column in zip(new.cells, row, table.columns):
            cell.width = column.width
            cell.text = str(value)
            if not isinstance(value, str):
                cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph = doc.add_paragraph(f"{place}, ngày …… tháng …… năm ……")
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    paragraph.paragraph_format.keep_with_next = True
    signatures = doc.add_table(rows=1, cols=3)
    signatures.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
    for cell, text in zip(signatures.rows[0].cells, SIGNATURES):
        cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        cell.paragraphs[0].add_run(text + "\n\n\n").bold = True
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.add_run("Trang ")
    field = OxmlElement("w:fldSimple")
    from docx.oxml.ns import qn
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    doc.save(stream)


def xlsx(stream, school, title, subtitle, headers, rows, wide, place):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    book = Workbook()
    sheet = book.active
    sheet.title = "Báo cáo"
    for row in [(school,), (title,), (subtitle,), tuple(headers), *rows]:
        sheet.append(row)
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = "s"
            cell.alignment = Alignment(wrap_text=True, vertical="center")
    for row in range(1, 4):
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(headers))
        sheet.cell(row, 1).font = Font(bold=True)
    for cell in sheet[4]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="E6EDF7")
    for column, header in enumerate(headers, start=1):
        sheet.column_dimensions[get_column_letter(column)].width = 29 if column == 1 else max(14, min(len(header) + 3, 22))
    sheet.freeze_panes = "B5"
    sheet.print_title_rows = "1:4"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.orientation = "landscape" if wide else "portrait"
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.oddFooter.right.text = "Trang &P"
    sheet.append([])
    sheet.append([f"{place}, ngày …… tháng …… năm ……"])
    sheet.append(list(SIGNATURES))
    book.save(stream)
