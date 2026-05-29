"""Convert USER_MANUAL.md to a clean PDF using fpdf2."""
import re
import warnings
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos

warnings.filterwarnings("ignore", category=DeprecationWarning)

SRC  = Path(__file__).parent / "USER_MANUAL.md"
DEST = Path(__file__).parent / "DiGiCo_LV1_Overlay_User_Manual.pdf"
FONT_DIR = Path(__file__).parent

C_BLACK   = (30,  30,  30)
C_TITLE   = (20,  60, 120)
C_H2      = (40,  90, 160)
C_H3      = (60, 110, 180)
C_CELL_BG = (245, 247, 250)
C_BORDER  = (180, 190, 210)
C_CODE_BG = (240, 242, 246)


class ManualPDF(FPDF):
    def header(self):
        self.set_font("Sans", "", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, "DiGiCo Preamp Overlay for Waves eMotion LV1  -  User Manual")
        self.ln(2)
        self.set_draw_color(*C_BORDER)
        self.set_line_width(0.3)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("Sans", "", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"Page {self.page_no()}", align="C")

    def heading(self, level: int, text: str):
        self.ln(4)
        if level == 1:
            self.set_font("Sans", "B", 20)
            self.set_text_color(*C_TITLE)
            self.multi_cell(0, 12, text)
            self.set_draw_color(*C_H2)
            self.set_line_width(0.5)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.ln(4)
        elif level == 2:
            self.set_font("Sans", "B", 14)
            self.set_text_color(*C_H2)
            self.ln(2)
            self.multi_cell(0, 9, text)
            self.set_draw_color(*C_BORDER)
            self.set_line_width(0.3)
            self.line(self.l_margin, self.get_y(), self.l_margin + 80, self.get_y())
            self.ln(3)
        elif level == 3:
            self.set_font("Sans", "B", 11)
            self.set_text_color(*C_H3)
            self.ln(1)
            self.multi_cell(0, 7, text)
            self.ln(1)

    def body(self, text: str):
        self.set_font("Sans", "", 10)
        self.set_text_color(*C_BLACK)
        self.multi_cell(0, 6, text)
        self.ln(1)

    def bullet(self, text: str, indent: int = 0):
        self.set_font("Sans", "", 10)
        self.set_text_color(*C_BLACK)
        indent_mm = self.l_margin + 4 + indent * 4
        text_w = self.w - self.r_margin - indent_mm - 5
        y0 = self.get_y()
        self.set_xy(indent_mm, y0)
        self.cell(5, 6, "-")
        self.set_xy(indent_mm + 5, y0)
        self.multi_cell(text_w, 6, text)

    def code(self, text: str):
        self.set_fill_color(*C_CODE_BG)
        self.set_font("Mono", "", 9)
        self.set_text_color(60, 60, 60)
        self.set_x(self.l_margin)
        self.multi_cell(0, 5.5, text, fill=True)
        self.ln(2)

    def table(self, headers: list, rows: list):
        usable = self.w - self.l_margin - self.r_margin
        col_w  = usable / max(len(headers), 1)

        self.set_fill_color(*C_H2)
        self.set_text_color(255, 255, 255)
        self.set_font("Sans", "B", 9)
        for h in headers:
            self.cell(col_w, 7, h, border=1, fill=True)
        self.ln()

        self.set_font("Sans", "", 9)
        fill = False
        for row in rows:
            self.set_fill_color(*(C_CELL_BG if fill else (255, 255, 255)))
            self.set_text_color(*C_BLACK)
            x0, y0 = self.get_x(), self.get_y()
            for j, cell in enumerate(row):
                self.set_xy(x0 + j * col_w, y0)
                self.multi_cell(col_w, 6, cell, border=1, fill=fill)
            # advance past row
            row_h = max(
                self.get_string_width(c) / col_w * 6 + 6
                for c in row
            ) if row else 6
            self.set_xy(x0, y0 + max(row_h, 6))
            fill = not fill
        self.ln(3)

    def hr(self):
        self.set_draw_color(*C_BORDER)
        self.set_line_width(0.3)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(4)


def strip_inline(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*',     r'\1', text)
    text = re.sub(r'`(.+?)`',       r'\1', text)
    return text


def render(pdf: ManualPDF, lines: list[str]):
    i = 0
    while i < len(lines):
        raw = lines[i]

        # Heading
        m = re.match(r'^(#{1,3})\s+(.+)', raw)
        if m:
            pdf.heading(len(m.group(1)), strip_inline(m.group(2)))
            i += 1
            continue

        # HR
        if re.match(r'^---+\s*$', raw):
            pdf.hr()
            i += 1
            continue

        # Fenced code block
        if raw.startswith('```'):
            code_lines, i = [], i + 1
            while i < len(lines) and not lines[i].startswith('```'):
                code_lines.append(lines[i])
                i += 1
            i += 1
            pdf.code('\n'.join(code_lines))
            continue

        # Table
        if '|' in raw and i + 1 < len(lines) and re.match(r'^\|[-| :]+\|', lines[i + 1]):
            headers = [c.strip() for c in raw.strip('|').split('|')]
            i += 2
            rows = []
            while i < len(lines) and '|' in lines[i]:
                rows.append([strip_inline(c.strip()) for c in lines[i].strip('|').split('|')])
                i += 1
            pdf.table(headers, rows)
            continue

        # Indented bullet (2 spaces)
        m = re.match(r'^  [-*]\s+(.+)', raw)
        if m:
            pdf.bullet(strip_inline(m.group(1)), indent=1)
            i += 1
            continue

        # Bullet / numbered list
        m = re.match(r'^[-*]\s+(.+)', raw) or re.match(r'^\d+\.\s+(.+)', raw)
        if m:
            pdf.bullet(strip_inline(m.group(1)))
            i += 1
            continue

        # Blank line
        if not raw.strip():
            pdf.ln(2)
            i += 1
            continue

        # Normal text
        pdf.body(strip_inline(raw.strip()))
        i += 1


def main():
    text  = SRC.read_text(encoding="utf-8")
    lines = text.splitlines()

    pdf = ManualPDF(orientation="P", unit="mm", format="A4")
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(auto=True, margin=20)

    pdf.add_font("Sans", "",  str(FONT_DIR / "DejaVuSans.ttf"))
    pdf.add_font("Sans", "B", str(FONT_DIR / "DejaVuSans-Bold.ttf"))
    pdf.add_font("Mono", "",  str(FONT_DIR / "DejaVuSansMono.ttf"))

    pdf.add_page()

    # Cover block
    pdf.set_font("Sans", "B", 26)
    pdf.set_text_color(*C_TITLE)
    pdf.ln(10)
    pdf.multi_cell(0, 14, "DiGiCo Preamp Overlay\nfor Waves eMotion LV1", align="C")
    pdf.ln(4)
    pdf.set_font("Sans", "", 13)
    pdf.set_text_color(80, 80, 80)
    pdf.multi_cell(0, 8, "User Manual  -  Version 0.1.1", align="C")
    pdf.ln(6)
    pdf.set_draw_color(*C_BORDER)
    pdf.set_line_width(0.5)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(8)

    render(pdf, lines)

    pdf.output(str(DEST))
    print(f"PDF written: {DEST}")


if __name__ == "__main__":
    main()
