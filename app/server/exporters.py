"""Markdown -> PDF export via fpdf2 (core Helvetica font, minimal parsing)."""
from __future__ import annotations

import re
import time
from pathlib import Path

from fpdf import FPDF

_WRAP_WIDTH = 90


class _ReportPDF(FPDF):
    def footer(self) -> None:
        self.set_y(-15)
        self.set_font("helvetica", "I", 8)
        self.cell(0, 10,
                  f"Deep Researcher - generato il {time.strftime('%Y-%m-%d %H:%M')}",
                  align="C")


def _clean(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = text.replace("`", "")
    return text


def _wrap(line: str, width: int = _WRAP_WIDTH) -> list[str]:
    out: list[str] = []
    while len(line) > width:
        cut = line.rfind(" ", 0, width + 1)
        if cut <= 0:
            cut = width
        out.append(line[:cut])
        line = line[cut + 1:]
    out.append(line)
    return out


def export_pdf(markdown_text: str, out_path: Path) -> Path:
    """Render a markdown report to PDF; raises ValueError on empty input."""
    if not markdown_text or not markdown_text.strip():
        raise ValueError("markdown vuoto: niente da esportare in PDF")

    pdf = _ReportPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    pdf.set_font("helvetica", size=11)

    for raw in markdown_text.splitlines():
        line = _clean(raw).rstrip()
        if not line.strip():
            pdf.ln(3)
            continue
        if line.lstrip().startswith("##"):
            title = line.lstrip().lstrip("#").strip()
            if not title:
                continue
            pdf.ln(2)
            pdf.set_font("helvetica", "B", 14)
            for piece in _wrap(title):
                pdf.cell(0, 9, piece, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
            pdf.set_font("helvetica", size=11)
            continue
        pdf.set_font("helvetica", "", 11)
        for piece in _wrap(line):
            pdf.multi_cell(0, 5.5, piece, new_x="LMARGIN", new_y="NEXT")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_path))
    return out_path
