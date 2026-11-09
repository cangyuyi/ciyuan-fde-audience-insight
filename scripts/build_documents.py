#!/usr/bin/env python3
"""Build polished Chinese DOCX deliverables from reviewed Markdown content."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

NAVY = "183B56"
TEAL = "0E7C7B"
LIGHT = "EAF4F4"
PALE = "F4F7FA"
GRAY = "566573"
WHITE = "FFFFFF"
FONT = "Hiragino Sans GB"


def set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_cell_margins(cell, top=90, start=100, bottom=90, end=100) -> None:
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for margin, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{margin}"))
        if node is None:
            node = OxmlElement(f"w:{margin}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_run_font(run, size: float | None = None, bold: bool | None = None, color: str | None = None) -> None:
    run.font.name = FONT
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), FONT)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)


def add_page_field(paragraph) -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend([begin, instr, end])
    set_run_font(run, 8, color=GRAY)


def add_inline(paragraph, text: str, size: float = 10.0, color: str | None = None) -> None:
    """Render bold, inline code, line breaks and Markdown links without losing text."""
    text = text.replace("<br><br>", "\n\n").replace("<br/>", "\n").replace("<br>", "\n")
    token = re.compile(r"(\*\*.+?\*\*|`.+?`|\[[^]]+\]\([^)]+\))")
    for part in token.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            set_run_font(run, size, bold=True, color=color)
        elif part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1])
            set_run_font(run, size - 0.3, color=TEAL)
            run.font.name = "Menlo"
        elif part.startswith("["):
            match = re.match(r"\[([^]]+)\]\(([^)]+)\)", part)
            label, url = match.groups() if match else (part, "")
            run = paragraph.add_run(label)
            set_run_font(run, size, color=TEAL)
            run.underline = True
            if url:
                suffix = paragraph.add_run(f"（{url}）")
                set_run_font(suffix, max(size - 1.5, 7), color=GRAY)
        else:
            run = paragraph.add_run(part)
            set_run_font(run, size, color=color)


def setup_document(title: str, landscape: bool, compact: bool, dense: bool = False) -> Document:
    doc = Document()
    section = doc.sections[0]
    if landscape:
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width, section.page_height = Cm(29.7), Cm(21.0)
    else:
        section.page_width, section.page_height = Cm(21.0), Cm(29.7)
    margin = 0.75 if dense else (1.25 if compact else 1.65)
    section.top_margin = Cm(margin)
    section.bottom_margin = Cm(margin)
    section.left_margin = Cm(margin + 0.15)
    section.right_margin = Cm(margin + 0.15)
    section.header_distance = Cm(0.55)
    section.footer_distance = Cm(0.55)

    normal = doc.styles["Normal"]
    normal.font.name = FONT
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    normal.font.size = Pt(8 if dense else (9 if compact else 10))
    normal.paragraph_format.space_after = Pt(1 if dense else (3 if compact else 5))
    normal.paragraph_format.line_spacing = 1.0 if dense else (1.08 if compact else 1.18)

    for name, size, color in (("Title", 25, NAVY), ("Heading 1", 16, NAVY), ("Heading 2", 12, TEAL), ("Heading 3", 10.5, NAVY)):
        style = doc.styles[name]
        style.font.name = FONT
        style._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
        style.font.size = Pt(size - (3 if dense and name != "Title" else (2 if compact and name != "Title" else 0)))
        style.font.bold = True
        style.font.color.rgb = RGBColor.from_string(color)
        style.paragraph_format.space_before = Pt(4 if dense else (9 if compact else 14))
        style.paragraph_format.space_after = Pt(2 if dense else (4 if compact else 6))
        style.paragraph_format.keep_with_next = True

    header = section.header.paragraphs[0]
    header.text = "词元引擎 · FDE 实战题  /  王泽栋"
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for run in header.runs:
        set_run_font(run, 8, color=GRAY)
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer.add_run("脱敏虚构材料 · 招聘评估使用   |   ")
    set_run_font(run, 8, color=GRAY)
    add_page_field(footer)
    doc.core_properties.title = title
    doc.core_properties.author = "王泽栋"
    doc.core_properties.subject = "词元引擎 FDE 48 小时实战题优化交付"
    return doc


def add_cover(doc: Document, title: str, subtitle: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(70)
    run = p.add_run("词元引擎 · FDE 实战交付")
    set_run_font(run, 11, bold=True, color=TEAL)
    title_p = doc.add_paragraph(style="Title")
    title_p.paragraph_format.space_before = Pt(14)
    title_p.add_run(title)
    for run in title_p.runs:
        set_run_font(run, 25, bold=True, color=NAVY)
    sub = doc.add_paragraph()
    add_inline(sub, subtitle, 12, GRAY)
    line = doc.add_table(rows=1, cols=1)
    line.alignment = WD_TABLE_ALIGNMENT.LEFT
    line.columns[0].width = Cm(4)
    set_cell_shading(line.cell(0, 0), TEAL)
    line.cell(0, 0).text = ""
    line.rows[0].height = Cm(0.12)
    doc.add_paragraph()
    meta = doc.add_table(rows=5, cols=2)
    meta.alignment = WD_TABLE_ALIGNMENT.LEFT
    entries = [
        ("候选人", "王泽栋"),
        ("交付版本", "V2.0 · 全面优化版"),
        ("交付日期", "2026-11-05 · Asia/Shanghai"),
        ("数据范围", "题目包及脱敏虚构 M1–M6；未采集外部人群数据"),
        ("核心原则", "有证据 · 可追溯 · 能落地 · 不确定处明确待验证"),
    ]
    for row, (key, value) in zip(meta.rows, entries):
        row.cells[0].text = key
        row.cells[1].text = value
        set_cell_shading(row.cells[0], PALE)
        for i, cell in enumerate(row.cells):
            set_cell_margins(cell, 100, 120, 100, 120)
            for p in cell.paragraphs:
                for r in p.runs:
                    set_run_font(r, 9.5, bold=(i == 0), color=NAVY if i == 0 else GRAY)
    doc.add_page_break()


def parse_table(lines: list[str]) -> list[list[str]]:
    rows = []
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(cells)
    if len(rows) >= 2 and all(re.fullmatch(r":?-{3,}:?", c.replace(" ", "")) for c in rows[1]):
        rows.pop(1)
    return rows


def add_table(doc: Document, rows: list[list[str]], compact: bool) -> None:
    if not rows:
        return
    cols = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    table.style = "Table Grid"
    for ri, values in enumerate(rows):
        for ci in range(cols):
            cell = table.cell(ri, ci)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            set_cell_margins(cell, 65 if compact else 90, 75, 65 if compact else 90, 75)
            if ri == 0:
                set_cell_shading(cell, NAVY)
            elif ri % 2 == 0:
                set_cell_shading(cell, PALE)
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            add_inline(p, values[ci] if ci < len(values) else "", 7.4 if compact else 8.2, WHITE if ri == 0 else None)
            for run in p.runs:
                if ri == 0:
                    run.bold = True
        tr_pr = table.rows[ri]._tr.get_or_add_trPr()
        cant_split = OxmlElement("w:cantSplit")
        tr_pr.append(cant_split)
        if ri == 0:
            repeat = OxmlElement("w:tblHeader")
            repeat.set(qn("w:val"), "true")
            tr_pr.append(repeat)
    doc.add_paragraph().paragraph_format.space_after = Pt(0)


def render_markdown(doc: Document, text: str, compact: bool) -> None:
    lines = text.splitlines()
    i = 0
    first_title = True
    in_appendix = False
    while i < len(lines):
        raw = lines[i].rstrip()
        stripped = raw.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("<!--"):
            i += 1
            continue
        if stripped.startswith("```"):
            i += 1
            block = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            for block_line in block:
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Cm(0.25)
                p.paragraph_format.space_after = Pt(0)
                p_pr = p._p.get_or_add_pPr()
                shd = OxmlElement("w:shd")
                shd.set(qn("w:fill"), PALE)
                p_pr.append(shd)
                add_inline(p, block_line if block_line else " ", 8.3 if compact else 9.0, GRAY)
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            block = []
            while i < len(lines) and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                block.append(lines[i].strip())
                i += 1
            add_table(doc, parse_table(block), compact)
            continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            if "附录A" in title:
                in_appendix = True
            if level == 1 and first_title:
                first_title = False
                p = doc.add_paragraph(style="Title" if not compact else "Heading 1")
                add_inline(p, title, 22 if not compact else 15, NAVY)
            else:
                p = doc.add_heading(level=min(level, 4))
                add_inline(p, title, {1: 16, 2: 14, 3: 11, 4: 10}[level], NAVY if level != 3 else TEAL)
            i += 1
            continue
        if stripped in {"---", "***"}:
            p = doc.add_paragraph()
            p_pr = p._p.get_or_add_pPr()
            borders = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), "6")
            bottom.set(qn("w:color"), TEAL)
            borders.append(bottom)
            p_pr.append(borders)
            i += 1
            continue
        if stripped.startswith(">"):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.35)
            p.paragraph_format.right_indent = Cm(0.2)
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(5)
            p_pr = p._p.get_or_add_pPr()
            shd = OxmlElement("w:shd")
            shd.set(qn("w:fill"), LIGHT)
            p_pr.append(shd)
            add_inline(p, stripped.lstrip("> "), 9 if compact else 9.5, NAVY)
            i += 1
            continue
        bullet = re.match(r"^[-*+]\s+(.+)$", stripped)
        numbered = re.match(r"^(\d+)[.)]\s+(.+)$", stripped)
        if bullet or numbered:
            p = doc.add_paragraph(style="List Bullet" if bullet else None)
            p.paragraph_format.left_indent = Cm(0.55)
            p.paragraph_format.first_line_indent = Cm(-0.25)
            p.paragraph_format.space_after = Pt(0 if in_appendix else (1 if compact else 2))
            if in_appendix:
                p.paragraph_format.line_spacing = 1.0
            content = bullet.group(1) if bullet else f"{numbered.group(1)}. {numbered.group(2)}"
            add_inline(p, content, 8.6 if in_appendix else (8.8 if compact else 9.8))
            i += 1
            continue
        p = doc.add_paragraph()
        add_inline(p, stripped, 8.8 if compact else 9.8)
        i += 1


def build(input_path: Path, output_path: Path, title: str, subtitle: str, landscape: bool, compact: bool, cover: bool, dense: bool = False) -> None:
    doc = setup_document(title, landscape, compact, dense)
    if cover:
        add_cover(doc, title, subtitle)
    render_markdown(doc, input_path.read_text(encoding="utf-8"), compact)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path)
    # Structural validation: reopen and require content plus stable tables.
    check = Document(output_path)
    if not check.paragraphs or not any(p.text.strip() for p in check.paragraphs):
        raise RuntimeError(f"empty document generated: {output_path}")
    print(f"built {output_path} paragraphs={len(check.paragraphs)} tables={len(check.tables)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--title", required=True)
    parser.add_argument("--subtitle", default="有证据、可追溯、能落地的人群洞察与方案")
    parser.add_argument("--landscape", action="store_true")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--cover", action="store_true")
    parser.add_argument("--dense", action="store_true")
    args = parser.parse_args()
    build(args.input, args.output, args.title, args.subtitle, args.landscape, args.compact, args.cover, args.dense)


if __name__ == "__main__":
    main()
