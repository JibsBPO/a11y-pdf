import io, pikepdf, pymupdf, pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import engine, validate

def helvetica_pdf(path, font="Helvetica"):
    c = canvas.Canvas(path, pagesize=letter)
    c.setFont(font, 22); c.drawString(72, 700, "Annual Report 2026")
    c.setFont(font, 12); c.drawString(72, 670, "Caf\u00e9 r\u00e9sum\u00e9: numbers 1,234.56 and punctuation (ok)!")
    c.showPage(); c.save()

@pytest.mark.parametrize("font", ["Helvetica", "Times-Roman", "Courier-Bold", "Helvetica-Oblique"])
def test_nonembedded_standard_fonts_get_embedded(tmp_path, font):
    s, o = str(tmp_path / "s.pdf"), str(tmp_path / "o.pdf"); helvetica_pdf(s, font)
    before = validate.run(s, external=False); assert "font.embed" in {c["id"] for c in before["checks"] if c["status"] == "failed"}
    r = engine.fix(s, o)
    after = validate.run(o, original=s, external=False)
    failed = {c["id"] for c in after["checks"] if c["status"] in ("failed", "error")}
    assert failed == set(), failed                                   # incl. keep.text and keep.visual: nothing lost, same look
    assert any("embedded as Liberation" in a["action"] and a["status"] == "needs_review" for a in r["actions"])
    assert pymupdf.open(o)[0].get_text() == pymupdf.open(s)[0].get_text()
    assert all(f[1] == "ttf" for f in pymupdf.open(o)[0].get_fonts())
