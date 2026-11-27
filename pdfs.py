"""PDF builders for The Hungry Games: a printable recipe per dish, and the QR code sheet."""
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import (Image as RLImage, KeepTogether, ListFlowable, ListItem,
                                Paragraph, SimpleDocTemplate, Spacer)

EMBER = colors.HexColor("#e8650a")   # a shade darker than the site's orange, so it prints well
INK = colors.HexColor("#1b1617")
MUTE = colors.HexColor("#6b6062")
RULE = colors.HexColor("#e6dcd8")
EVENT = "The Hungry Games · Thanksgiving 2026"

_kick = ParagraphStyle("kick", fontName="Helvetica-Bold", fontSize=10, leading=13, textColor=EMBER)
_title = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=28, leading=31, textColor=INK, spaceAfter=6)
_meta = ParagraphStyle("meta", fontName="Helvetica", fontSize=10.5, leading=14.5, textColor=MUTE, spaceAfter=4)
_h2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=INK, spaceBefore=10, spaceAfter=3)
_h3 = ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=11.5, leading=15, textColor=EMBER, spaceBefore=8, spaceAfter=2)
_body = ParagraphStyle("body", fontName="Helvetica", fontSize=11, leading=15, textColor=INK)
_note = ParagraphStyle("note", parent=_body, fontSize=10.5, leading=14, textColor=MUTE, spaceBefore=10)


def _p(text, style):
    # Helvetica covers the characters the recipes use (° – ’ “ ” è ñ); anything else becomes "?".
    safe = str(text).encode("cp1252", "replace").decode("cp1252")
    return Paragraph(escape(safe), style)


def _photo(image_bytes, max_w, max_h):
    if not image_bytes:
        return None
    try:
        reader = ImageReader(BytesIO(image_bytes))
        w, h = reader.getSize()
    except Exception:
        return None
    scale = min(max_w / w, max_h / h)
    img = RLImage(BytesIO(image_bytes), width=w * scale, height=h * scale)
    img.hAlign = "CENTER"
    return img


def _footer(c, doc):
    c.saveState()
    c.setFont("Helvetica", 8.5)
    c.setFillColor(MUTE)
    c.drawString(doc.leftMargin, 0.45 * inch, EVENT)
    c.drawRightString(letter[0] - doc.rightMargin, 0.45 * inch, f"Page {doc.page}")
    c.restoreState()


def recipe_pdf(dish: dict, number: int, image_bytes: bytes | None) -> bytes:
    """A printable recipe: title, main photo (if any), ingredients, steps and notes."""
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            topMargin=0.7 * inch, bottomMargin=0.8 * inch,
                            title=dish["name"], author=EVENT, subject="Recipe")
    width = letter[0] - doc.leftMargin - doc.rightMargin
    label = "GUEST CONTENDER" if dish.get("guest") else f"CONTENDER NO. {number:02d}"
    story = [_p(f"THE HUNGRY GAMES · {label} · {dish['course'].upper()}", _kick),
             _p(dish["name"], _title)]
    photo = _photo(image_bytes, width, 3.0 * inch)
    if photo:
        story += [Spacer(1, 4), photo, Spacer(1, 10)]
    story.append(_p(dish["meta"], _meta))

    if dish["ing"]:
        story.append(_p("Ingredients", _h2))
    for heading, items in dish["ing"]:
        block = []
        if len(dish["ing"]) > 1 or heading != "Ingredients":
            block.append(_p(heading, _h3))
        block.append(ListFlowable([ListItem(_p(x, _body), leftIndent=14, value="•") for x in items],
                                  bulletType="bullet", bulletFontName="Helvetica", bulletFontSize=9,
                                  bulletColor=EMBER, leftIndent=14, spaceBefore=2))
        story.append(KeepTogether(block))

    first = True
    for heading, items in dish["steps"]:
        lead = [_p("Steps", _h2)] if first else []
        first = False
        if len(dish["steps"]) > 1 or heading != "Steps":
            lead.append(_p(heading, _h3))
        lead.append(ListFlowable([ListItem(_p(items[0], _body), leftIndent=20, spaceAfter=3)],
                                 bulletType="1", bulletFontName="Helvetica-Bold", bulletFontSize=10.5,
                                 bulletColor=EMBER, leftIndent=20, bulletFormat="%s."))
        story.append(KeepTogether(lead))
        if len(items) == 1:
            continue
        story.append(ListFlowable([ListItem(_p(x, _body), leftIndent=20, spaceAfter=3, value=n)
                                   for n, x in enumerate(items[1:], 2)],
                                  bulletType="1", bulletFontName="Helvetica-Bold", bulletFontSize=10.5,
                                  bulletColor=EMBER, leftIndent=20, bulletFormat="%s."))
    if not dish["ing"] and not dish["steps"]:
        story.append(_p("No recipe was shared for this one. Ask the cook.", _note))
    if dish.get("note"):
        story.append(_p(dish["note"], _note))
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def _qr(c, value, x, y, size):
    w = QrCodeWidget(value, barLevel="M", barBorder=0)
    x0, y0, x1, y1 = w.getBounds()
    d = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    d.add(w)
    renderPDF.draw(d, c, x, y)


def qr_sheet(dishes: list, base: str) -> bytes:
    """Cut-out cards, six per letter page: one for the home page (standings), then one per dish."""
    base = base.rstrip("/")
    cards = [("THE STANDINGS", "All fifteen contenders", base + "/",
              "Scan for every dish, the live rankings and the photo wall.")]
    for i, d in enumerate(dishes, 1):
        label = "GUEST CONTENDER" if d.get("guest") else f"CONTENDER NO. {i:02d}"
        cards.append((label, d["name"], f"{base}/#{d['id']}",
                      "Scan for the backstory, the recipe, and to cast your vote."))

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setTitle("The Hungry Games QR codes")
    pw, ph = letter
    margin = 0.4 * inch
    cols, rows = 2, 3
    cw, ch = (pw - 2 * margin) / cols, (ph - 2 * margin) / rows
    name_style = ParagraphStyle("n", fontName="Helvetica-Bold", fontSize=17, leading=19.5,
                                textColor=INK, alignment=TA_CENTER)
    for k, (kicker, name, url, hint) in enumerate(cards):
        slot = k % (cols * rows)
        if k and slot == 0:
            c.showPage()
        col, row = slot % cols, slot // cols
        x, y = margin + col * cw, ph - margin - (row + 1) * ch
        # dashed cut line
        c.setStrokeColor(RULE)
        c.setDash(4, 4)
        c.setLineWidth(0.8)
        c.rect(x, y, cw, ch)
        c.setDash()
        cx = x + cw / 2
        top = y + ch - 0.36 * inch
        c.setFillColor(EMBER)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawCentredString(cx, top, "THE HUNGRY GAMES  ·  THANKSGIVING 2026")
        c.setFillColor(MUTE)
        c.setFont("Helvetica-Bold", 9)
        c.drawCentredString(cx, top - 15, kicker)
        para = _p(name, name_style)
        _, nh = para.wrap(cw - 0.5 * inch, 1.2 * inch)
        name_bottom = top - 21 - nh
        para.drawOn(c, x + 0.25 * inch, name_bottom)
        # QR fills the space between the name and the hint text, never overlapping either
        q_top, q_floor = name_bottom - 8, y + 0.58 * inch
        qsize = min(1.8 * inch, q_top - q_floor)
        _qr(c, url, cx - qsize / 2, q_floor + (q_top - q_floor - qsize) / 2, qsize)
        c.setFillColor(INK)
        c.setFont("Helvetica", 9)
        c.drawCentredString(cx, y + 0.4 * inch, hint)
        c.setFillColor(MUTE)
        c.setFont("Helvetica", 7.5)
        c.drawCentredString(cx, y + 0.22 * inch, url)
    c.save()
    return buf.getvalue()
