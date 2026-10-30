"""Generate the architecture PDF from the canonical Markdown document.

The exporter intentionally uses a conservative A4 layout: Chinese-capable fonts,
generous margins, repeating table headers, wrapped cells, and page-aware code
blocks.  The Markdown remains the source of truth; this script only controls the
print presentation.
"""

from __future__ import annotations

import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "architecture-overview.md"
OUTPUT = ROOT / "output" / "pdf" / "ChipFlow_总体架构说明.pdf"


def register_fonts() -> tuple[str, str]:
    candidates = [
        (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/msyhbd.ttc")),
        (Path("C:/Windows/Fonts/simhei.ttf"), Path("C:/Windows/Fonts/simhei.ttf")),
    ]
    for regular, bold in candidates:
        if regular.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont("ChipFlowSans", str(regular)))
            pdfmetrics.registerFont(TTFont("ChipFlowSansBold", str(bold)))
            return "ChipFlowSans", "ChipFlowSansBold"
    raise RuntimeError("No Chinese-capable Windows font was found")


def inline_markup(text: str) -> str:
    text = escape(text.strip())
    text = re.sub(r"`([^`]+)`", r'<font name="ChipFlowSans">\1</font>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", text)
    return text


def styles(regular: str, bold: str) -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("TitleCN", parent=base["Title"], fontName=bold, fontSize=22, leading=30, textColor=colors.HexColor("#153A5B"), spaceAfter=14 * mm, alignment=TA_CENTER),
        "h1": ParagraphStyle("H1CN", parent=base["Heading1"], fontName=bold, fontSize=16, leading=22, textColor=colors.HexColor("#153A5B"), spaceBefore=8 * mm, spaceAfter=4 * mm, keepWithNext=True),
        "h2": ParagraphStyle("H2CN", parent=base["Heading2"], fontName=bold, fontSize=13, leading=18, textColor=colors.HexColor("#21618C"), spaceBefore=6 * mm, spaceAfter=3 * mm, keepWithNext=True),
        "h3": ParagraphStyle("H3CN", parent=base["Heading3"], fontName=bold, fontSize=11, leading=16, textColor=colors.HexColor("#2874A6"), spaceBefore=4 * mm, spaceAfter=2 * mm, keepWithNext=True),
        "body": ParagraphStyle("BodyCN", parent=base["BodyText"], fontName=regular, fontSize=9.5, leading=15, textColor=colors.HexColor("#202830"), spaceAfter=2.4 * mm, alignment=TA_LEFT),
        "bullet": ParagraphStyle("BulletCN", parent=base["BodyText"], fontName=regular, fontSize=9.3, leading=14.5, leftIndent=6 * mm, firstLineIndent=-3.5 * mm, bulletIndent=1.5 * mm, spaceAfter=1.2 * mm),
        "code": ParagraphStyle("CodeCN", parent=base["Code"], fontName=regular, fontSize=7.4, leading=10.5, leftIndent=3 * mm, rightIndent=3 * mm, borderColor=colors.HexColor("#CBD5E1"), borderWidth=0.5, borderPadding=5, backColor=colors.HexColor("#F6F8FA"), spaceBefore=2 * mm, spaceAfter=3 * mm, wordWrap="CJK"),
        "cell": ParagraphStyle("CellCN", parent=base["BodyText"], fontName=regular, fontSize=7.7, leading=11, textColor=colors.HexColor("#202830"), wordWrap="CJK"),
        "cell_head": ParagraphStyle("CellHeadCN", parent=base["BodyText"], fontName=bold, fontSize=7.8, leading=11, textColor=colors.white, wordWrap="CJK"),
    }


def page_decoration(canvas, doc) -> None:
    canvas.saveState()
    width, height = A4
    canvas.setStrokeColor(colors.HexColor("#D9E2EC"))
    canvas.line(18 * mm, height - 16 * mm, width - 18 * mm, height - 16 * mm)
    canvas.setFont("ChipFlowSans", 7.5)
    canvas.setFillColor(colors.HexColor("#64748B"))
    canvas.drawString(18 * mm, 10 * mm, "ChipFlow 总体架构说明")
    canvas.drawRightString(width - 18 * mm, 10 * mm, f"第 {doc.page} 页")
    canvas.restoreState()


def table_from(lines: list[str], style_map: dict[str, ParagraphStyle]) -> Table:
    rows: list[list[Paragraph]] = []
    for row_index, line in enumerate(lines):
        values = [part.strip() for part in line.strip().strip("|").split("|")]
        if row_index == 1 and all(re.fullmatch(r":?-{3,}:?", value) for value in values):
            continue
        style = style_map["cell_head"] if not rows else style_map["cell"]
        rows.append([Paragraph(inline_markup(value), style) for value in values])
    available = A4[0] - 36 * mm
    col_widths = [available / max(1, len(rows[0]))] * len(rows[0])
    table = Table(rows, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#21618C")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B8C5D1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7FAFC")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def build_story(markdown: str, style_map: dict[str, ParagraphStyle]):
    story = []
    lines = markdown.splitlines()
    index = 0
    first_heading = True
    while index < len(lines):
        line = lines[index].rstrip()
        if line.startswith("```"):
            code_lines = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                code_lines.append(lines[index])
                index += 1
            code_html = "<br/>".join(escape(item) if item else "&nbsp;" for item in code_lines)
            story.append(Paragraph(code_html, style_map["code"]))
        elif line.startswith("|") and index + 1 < len(lines) and lines[index + 1].startswith("|"):
            table_lines = []
            while index < len(lines) and lines[index].startswith("|"):
                table_lines.append(lines[index])
                index += 1
            story.append(table_from(table_lines, style_map))
            story.append(Spacer(1, 3 * mm))
            continue
        elif line.startswith("# "):
            if not first_heading:
                story.append(PageBreak())
            story.append(Paragraph(inline_markup(line[2:]), style_map["title"]))
            first_heading = False
        elif line.startswith("## "):
            story.append(Paragraph(inline_markup(line[3:]), style_map["h1"]))
        elif line.startswith("### "):
            story.append(Paragraph(inline_markup(line[4:]), style_map["h2"]))
        elif line.startswith("#### "):
            story.append(Paragraph(inline_markup(line[5:]), style_map["h3"]))
        elif re.match(r"^[-*] ", line):
            story.append(Paragraph("• " + inline_markup(line[2:]), style_map["bullet"]))
        elif re.match(r"^\d+\. ", line):
            number, body = line.split(". ", 1)
            story.append(Paragraph(f"{number}. " + inline_markup(body), style_map["bullet"]))
        elif line.strip():
            story.append(Paragraph(inline_markup(line), style_map["body"]))
        else:
            story.append(Spacer(1, 1.2 * mm))
        index += 1
    return story


def main() -> None:
    regular, bold = register_fonts()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(OUTPUT),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=21 * mm,
        bottomMargin=17 * mm,
        title="ChipFlow 总体架构说明",
        author="ChipFlow",
        subject="芯片设计 Multi-Agent 系统总体架构",
    )
    document.build(
        build_story(SOURCE.read_text(encoding="utf-8"), styles(regular, bold)),
        onFirstPage=page_decoration,
        onLaterPages=page_decoration,
    )
    print(OUTPUT)


if __name__ == "__main__":
    main()
