from decimal import Decimal
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings
from django.http import FileResponse, Http404

SIGNATURES = ("Người lập biểu", "Người kiểm tra", "Hiệu trưởng")
DAILY_SIGNATURES = ("Người chấm công", "Phụ trách bộ phận", "Thủ trưởng đơn vị")
DAILY_LEGEND = "CXN = chưa xác nhận · Ô trống = không có dữ liệu công"
MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def document(request, title, subtitle, headers, rows, filename, format, landscape=False, *, daily=False, school_name=None):
    if format not in MIME:
        raise Http404
    place = request.school.location
    stream = BytesIO()
    args = (stream, school_name or request.school.name, title, subtitle, headers, rows, landscape, place)
    {"pdf": pdf, "docx": docx, "xlsx": xlsx}[format](*args, daily=daily)
    stream.seek(0)
    inline = format == "pdf" and request.GET.get("download") != "1"
    return FileResponse(stream, as_attachment=not inline, filename=f"{filename}.{format}", content_type=MIME[format])


def column_weights(headers, daily=False):
    if daily:
        return [1.55, 8, *([1] * (len(headers) - 3)), 2.3]
    if len(headers) == 12:
        return [2.55, 1.65, 1.05, 1.05, 0.8, 0.75, 0.6, 0.6, 0.85, 0.9, 1.0, 0.75]
    return [1] * len(headers)


def display_value(value):
    return format(value.normalize(), "f") if isinstance(value, Decimal) else str(value)


def pdf(stream, school, title, subtitle, headers, rows, wide, place, *, daily=False):
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
    cell = ParagraphStyle("cell", fontName="DejaVu", fontSize=7 if daily else 8, leading=10 if daily else 11,
                          splitLongWords=True)
    numeric = ParagraphStyle("numeric", parent=cell, alignment=TA_CENTER)
    header = ParagraphStyle("header", parent=cell, fontName="DejaVuBold", alignment=TA_CENTER)
    heading = ParagraphStyle("heading", parent=header, fontSize=14, leading=19)
    right = ParagraphStyle("right", parent=cell, alignment=TA_RIGHT)
    grid = [[Paragraph(escape(str(text)), header) for text in headers]]
    grid += [[Paragraph(escape(display_value(value)), cell if isinstance(value, str) and not daily else
                        cell if daily and index == 1 else numeric) for index, value in enumerate(row)] for row in rows]
    width = size[0] - 74
    weights = column_weights(headers, daily)
    table = LongTable(grid, colWidths=[width * weight / sum(weights) for weight in weights], repeatRows=1, splitInRow=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#efefef")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#8e8e8e")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4 if daily else 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4 if daily else 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 2 if daily else 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2 if daily else 6),
    ]))
    signatures = Table([[Paragraph(text, header) for text in (DAILY_SIGNATURES if daily else SIGNATURES)]],
                       colWidths=[width / 3] * 3)
    closing = KeepTogether([Spacer(1, 12), Paragraph(f"{escape(place)}, ngày …… tháng …… năm ……", right),
                            Spacer(1, 12), signatures, Spacer(1, 45)])
    flow = [Paragraph(escape(school), header), Spacer(1, 8), Paragraph(escape(title), heading),
            Paragraph(escape(subtitle), header), Spacer(1, 12), table]
    if daily:
        flow.extend([Spacer(1, 6), Paragraph(DAILY_LEGEND, cell)])
    flow.append(closing)

    def footer(canvas, _doc):
        canvas.setFont("DejaVu", 8)
        canvas.drawRightString(size[0] - 36, 22, f"Trang {canvas.getPageNumber()}")

    doc.build(flow, onFirstPage=footer, onLaterPages=footer)


def docx(stream, school, title, subtitle, headers, rows, wide, place, *, daily=False):
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt

    doc = Document()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE if wide else WD_ORIENT.PORTRAIT
    section.page_width, section.page_height = (Mm(297), Mm(210)) if wide else (Mm(210), Mm(297))
    section.top_margin = section.bottom_margin = section.left_margin = section.right_margin = Mm(13)
    normal = doc.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(7 if daily else 9)
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
    weights = column_weights(headers, daily)
    if daily:
        margins = OxmlElement("w:tblCellMar")
        for edge in ("top", "left", "bottom", "right"):
            margin = OxmlElement(f"w:{edge}")
            margin.set(qn("w:w"), "30")
            margin.set(qn("w:type"), "dxa")
            margins.append(margin)
        table._tbl.tblPr.append(margins)
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
        for index, (cell, value, column) in enumerate(zip(new.cells, row, table.columns)):
            cell.width = column.width
            cell.text = display_value(value)
            if (daily and index != 1) or not isinstance(value, str):
                cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    if daily:
        doc.add_paragraph(DAILY_LEGEND)
    paragraph = doc.add_paragraph(f"{place}, ngày …… tháng …… năm ……")
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    paragraph.paragraph_format.keep_with_next = True
    signatures = doc.add_table(rows=1, cols=3)
    signatures.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
    for cell, text in zip(signatures.rows[0].cells, DAILY_SIGNATURES if daily else SIGNATURES):
        cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        cell.paragraphs[0].add_run(text + "\n\n\n").bold = True
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.add_run("Trang ")
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    doc.save(stream)


def xlsx(stream, school, title, subtitle, headers, rows, wide, place, *, daily=False):
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
        if daily:
            width = 28 if column == 2 else 10 if column == len(headers) else 4.5
        else:
            width = 29 if column == 1 else max(14, min(len(header) + 3, 22))
        sheet.column_dimensions[get_column_letter(column)].width = width
    sheet.freeze_panes = "C5" if daily else "B5"
    sheet.print_title_rows = "1:4"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.orientation = "landscape" if wide else "portrait"
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.oddFooter.right.text = "Trang &P"
    if daily:
        total_row = sheet.max_row
        last_day = get_column_letter(len(headers) - 1)
        if total_row > 5:
            for row in range(5, total_row):
                ref = f"C{row}:{last_day}{row}"
                sheet.cell(row, len(headers), f'=IF(COUNT({ref}),SUM({ref}),"")')
            for column in range(3, len(headers) + 1):
                letter = get_column_letter(column)
                ref = f"{letter}5:{letter}{total_row - 1}"
                sheet.cell(total_row, column, f'=IF(COUNT({ref}),SUM({ref}),"")')
        sheet.append([DAILY_LEGEND])
        sheet.merge_cells(start_row=sheet.max_row, start_column=1, end_row=sheet.max_row, end_column=len(headers))
    sheet.append([])
    sheet.append([f"{place}, ngày …… tháng …… năm ……"])
    if daily:
        signature_row = sheet.max_row + 1
        for index, text in enumerate(DAILY_SIGNATURES):
            start = (index * len(headers)) // 3 + 1
            end = ((index + 1) * len(headers)) // 3
            sheet.merge_cells(start_row=signature_row, start_column=start, end_row=signature_row, end_column=end)
            cell = sheet.cell(signature_row, start, text)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center")
        sheet.row_dimensions[signature_row + 1].height = 43
        sheet.print_area = f"A1:{get_column_letter(len(headers))}{signature_row + 1}"
    else:
        sheet.append(list(SIGNATURES))
    book.save(stream)
