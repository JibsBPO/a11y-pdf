"""Fonts that v3 could not fix: symbolic (Symbol, ZapfDingbats, ToUnicode-mapped) and composite (CID) fonts, unknown families."""
import io, pikepdf, pymupdf, pytest, pytesseract
from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import engine, fonts, validate


def text_pdf(path, font, text, size=24):
    c = canvas.Canvas(path, pagesize=letter); c.setFont(font, size); c.drawString(72, 700, text); c.showPage(); c.save()

def ocr(pdf_path, page=0):
    pix = pymupdf.open(pdf_path)[page].get_pixmap(matrix=pymupdf.Matrix(3, 3)); return pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes("png")))).strip()

def failed_ids(r): return {c["id"] for c in r["checks"] if c["status"] in ("failed", "error")}


# ---------------------------------------------------------------- tables / parsers
def test_builtin_encodings_are_complete():
    s, z = fonts._builtin("symbol"), fonts._builtin("zapf")
    assert s[97] == "α" and s[98] == "β" and s[0x53] == "Σ" and len(s) >= 185          # a = alpha, b = beta, S = Sigma
    assert z[0x34] == "✔" and len(z) >= 185                                               # '4' = check mark

def test_tounicode_parser_handles_char_range_and_array():
    cm = b"1 beginbfchar <0003> <0020> endbfchar 2 beginbfrange <0010> <0012> <0041> <0020> <0021> [<0061> <0062>] endbfrange"
    assert fonts.parse_tounicode(cm) == {3: " ", 16: "A", 17: "B", 18: "C", 32: "a", 33: "b"}


# ---------------------------------------------------------------- symbolic simple fonts
def test_symbol_font_is_embedded_with_correct_characters(tmp_path):
    s, o = str(tmp_path / "s.pdf"), str(tmp_path / "o.pdf"); text_pdf(s, "Symbol", "αβγδΣΔ")
    assert "font.embed" in failed_ids(validate.run(s, external=False))
    r = engine.fix(s, o); after = validate.run(o, original=s, external=False)
    assert "font.embed" not in failed_ids(after) and "font.unicode" not in failed_ids(after)
    assert any("symbolic font" in a["action"] and a["status"] == "needs_review" for a in r["actions"])
    assert pymupdf.open(o)[0].get_text().strip() == "αβγδΣΔ"   # a ToUnicode map now exists (Delta is the Greek letter U+0394, not the math increment MuPDF guessed)
    pix = pymupdf.open(o)[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=pymupdf.Rect(60, 60, 300, 110)); assert min(pix.samples) < 100   # glyphs are drawn, not blank
    assert all(f[1] == "ttf" for f in pymupdf.open(o)[0].get_fonts())

def test_zapfdingbats_is_embedded(tmp_path):
    s, o = str(tmp_path / "s.pdf"), str(tmp_path / "o.pdf"); text_pdf(s, "ZapfDingbats", "✔ ✖ ■")
    r = engine.fix(s, o)
    assert "font.embed" not in failed_ids(validate.run(o, original=s, external=False))
    assert pymupdf.open(o)[0].get_text().replace(" ", "").strip() == "✔✖■"   # (MuPDF could not even extract the original: raw codes)

def test_symbolic_font_with_no_information_still_fails_honestly(tmp_path):
    s, o = str(tmp_path / "s.pdf"), str(tmp_path / "o.pdf"); text_pdf(s, "Helvetica", "abc")
    with pikepdf.open(s, allow_overwriting_input=True) as pdf:
        f = [x for x in pdf.objects if isinstance(x, pikepdf.Dictionary) and x.get("/Type") == pikepdf.Name.Font][0]
        f.BaseFont = pikepdf.Name("/CustomSymbols"); f.FontDescriptor = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.FontDescriptor, FontName=pikepdf.Name("/CustomSymbols"), Flags=4))
        pdf.save(s)
    r = engine.fix(s, o)
    a = [a for a in r["actions"] if a["status"] == "failed" and "not embedded" in a["action"]]
    assert a and "nothing says which glyph" in a[0]["detail"]
    assert "font.embed" in failed_ids(validate.run(o, original=s, external=False))


# ---------------------------------------------------------------- composite (CID) fonts
def cid_pdf(path, with_tounicode=True, encoding="/Identity-H"):
    """'Hello World' set in a non-embedded Arial-like CID font: CID = glyph id = ASCII - 29 (as in Arial)."""
    pdf = pikepdf.new(); pdf.add_blank_page(page_size=(612, 792)); txt = "Hello World"
    cids = [ord(ch) - 29 for ch in txt]; hexs = "".join(f"{c:04X}" for c in cids)
    tu = "/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /X def /CMapType 2 def 1 begincodespacerange <0000> <FFFF> endcodespacerange " \
         + f"{len(cids)} beginbfchar " + " ".join(f"<{ord(ch) - 29:04X}> <{ord(ch):04X}>" for ch in txt) + " endbfchar endcmap end end"
    dfont = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.CIDFontType2, BaseFont=pikepdf.Name("/ArialMT"),
        CIDSystemInfo=pikepdf.Dictionary(Registry=pikepdf.String("Adobe"), Ordering=pikepdf.String("Identity"), Supplement=0), CIDToGIDMap=pikepdf.Name.Identity, DW=600,
        FontDescriptor=pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.FontDescriptor, FontName=pikepdf.Name("/ArialMT"), Flags=32, FontBBox=[-665, -325, 2000, 1006], ItalicAngle=0, Ascent=905, Descent=-212, CapHeight=716, StemV=80))))
    f0 = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type0, BaseFont=pikepdf.Name("/ArialMT"), Encoding=pikepdf.Name(encoding), DescendantFonts=[dfont]))
    if with_tounicode: f0.ToUnicode = pdf.make_stream(tu.encode())
    pg = pdf.pages[0]; pg.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=f0))
    pg.Contents = pdf.make_stream(f"BT /F1 28 Tf 72 700 Td <{hexs}> Tj ET".encode()); pdf.save(path)

def test_cid_font_with_tounicode_is_embedded_and_renders_correctly(tmp_path):
    s, o = str(tmp_path / "s.pdf"), str(tmp_path / "o.pdf"); cid_pdf(s)
    assert "font.embed" in failed_ids(validate.run(s, external=False))
    r = engine.fix(s, o); after = validate.run(o, original=s, external=False)
    assert "font.embed" not in failed_ids(after) and "font.unicode" not in failed_ids(after), failed_ids(after)
    assert any("composite font" in a["action"] and a["status"] == "needs_review" for a in r["actions"])
    assert pymupdf.open(o)[0].get_text().strip() == "Hello World"
    assert "Hello World" in ocr(o)                                                                # the glyphs on the page really are 'Hello World'

def test_cid_font_without_tounicode_fails_honestly(tmp_path):
    s, o = str(tmp_path / "s.pdf"), str(tmp_path / "o.pdf"); cid_pdf(s, with_tounicode=False)
    r = engine.fix(s, o); a = [a for a in r["actions"] if a["status"] == "failed" and "not embedded" in a["action"]]
    assert a and "ToUnicode" in a[0]["detail"]

def test_cid_font_with_cjk_cmap_fails_honestly(tmp_path):
    s, o = str(tmp_path / "s.pdf"), str(tmp_path / "o.pdf"); cid_pdf(s, encoding="/UniJIS-UCS2-H")
    r = engine.fix(s, o); assert any(a["status"] == "failed" and "UniJIS-UCS2-H" in a["detail"] for a in r["actions"])


# ---------------------------------------------------------------- choosing the family: the one used most
def test_unknown_font_follows_the_documents_dominant_family(tmp_path):
    """Three lines in Times, one in an unknown font: the unknown font is replaced by a SERIF face, not the default sans."""
    s, o = str(tmp_path / "s.pdf"), str(tmp_path / "o.pdf")
    c = canvas.Canvas(s, pagesize=letter); c.setFont("Times-Roman", 14)
    for k in range(3): c.drawString(72, 700 - 20 * k, "A line of body text set in Times, repeated for weight.")
    c.setFont("Helvetica", 14); c.drawString(72, 600, "Mystery"); c.showPage(); c.save()
    with pikepdf.open(s, allow_overwriting_input=True) as pdf:
        f = [x for x in pdf.objects if isinstance(x, pikepdf.Dictionary) and x.get("/Type") == pikepdf.Name.Font and str(x.BaseFont) == "/Helvetica"][0]
        f.BaseFont = pikepdf.Name("/QwertyFont"); pdf.save(s)
    r = engine.fix(s, o); a = [a for a in r["actions"] if "QwertyFont" in a["action"]][0]
    assert "Serif" in a["action"] and "used most" in a["detail"], a

def test_classify_uses_name_then_flags_then_default():
    assert fonts.classify("/ABCDEF+Courier-Bold", "serif") == ("mono", 1, 0)
    assert fonts.classify("/Xyz", "serif", flags=1) == ("mono", 0, 0)
    assert fonts.classify("/Xyz", "sans", flags=2) == ("serif", 0, 0)
    assert fonts.classify("/Xyz", "serif") == ("serif", 0, 0)
    assert fonts.classify("/Xyz-BoldItalic", "sans") == ("sans", 1, 1)
