import os, sys, io, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pikepdf, pymupdf
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from PIL import Image
import engine

FONT = engine.find_font() or "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

def make_source(path):
    """Untagged 2-page PDF with EMBEDDED fonts: title, headings, paragraphs, an image, a link."""
    pdfmetrics.registerFont(TTFont("DV", FONT))
    im = Image.new("RGB", (200, 100), (30, 90, 200)); im.save(path + ".png")
    c = canvas.Canvas(path, pagesize=letter)
    c.setFont("DV", 24); c.drawString(72, 720, "Quarterly Report")
    c.setFont("DV", 12); c.drawString(72, 690, "This is the first paragraph of body text for the report.")
    c.drawString(72, 675, "It continues on a second line with more words in it today.")
    c.setFont("DV", 16); c.drawString(72, 630, "Results")
    c.setFont("DV", 12); c.drawString(72, 610, "The results were good and the team is happy with them.")
    c.drawImage(path + ".png", 72, 450, 200, 100)
    c.linkURL("https://example.com", (72, 400, 200, 415), relative=0); c.drawString(72, 403, "Visit example")
    c.showPage()
    c.setFont("DV", 16); c.drawString(72, 720, "Details")
    c.setFont("DV", 12); c.drawString(72, 700, "More body text appears here on the second page of the report.")
    c.showPage(); c.save()

@pytest.fixture(scope="session")
def src(tmp_path_factory):
    p = str(tmp_path_factory.mktemp("pdf") / "src.pdf"); make_source(p); return p

@pytest.fixture(scope="session")
def fixed(src, tmp_path_factory):
    p = str(tmp_path_factory.mktemp("pdf") / "fixed.pdf"); engine.fix(src, p); return p
