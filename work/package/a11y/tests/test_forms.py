"""Form XObjects that hold real content (v3 hid them as one Artifact)."""
import pikepdf, pymupdf, pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import engine, validate


def forms_pdf(path, graphics_only=False):
    c = canvas.Canvas(path, pagesize=letter)
    c.beginForm("body")
    if graphics_only: c.rect(72, 600, 200, 80, fill=1)
    else:
        c.setFont("Helvetica-Bold", 22); c.drawString(72, 700, "Annual Report 2026")
        c.setFont("Helvetica", 12); c.drawString(72, 670, "Body text that lives inside a Form XObject.")
    c.endForm()
    c.doForm("body"); c.showPage(); c.save()


def test_form_with_text_is_flattened_and_tagged(tmp_path):
    s, o = str(tmp_path / "s.pdf"), str(tmp_path / "o.pdf"); forms_pdf(s)
    before = validate.run(s, external=False)
    r = engine.fix(s, o); after = validate.run(o, original=s, external=False)
    failed = {c["id"] for c in after["checks"] if c["status"] in ("failed", "error")}
    assert failed == set(), failed                                        # incl. keep.text / keep.visual / form.hidden
    assert any(a["action"].startswith("Form XObject flattened") and a["status"] == "needs_review" for a in r["actions"])
    d = pymupdf.open(o); t = d[0].get_text()
    assert "Annual Report 2026" in t and "Body text that lives inside" in t   # the text is still there ...
    pdf = pikepdf.open(o); roles = []
    engine.walk(pdf.Root.StructTreeRoot.K, engine.rolemap(pdf.Root.StructTreeRoot), lambda el, d_, pg: roles.append(str(el.get("/S"))))
    assert "/H1" in roles and "/P" in roles                               # ... and now it is a heading and a paragraph, not an Artifact

def test_old_behaviour_is_detected_by_the_validator(tmp_path):
    """A Form with text wrapped in /Artifact (what v3 produced) is reported as hidden content."""
    s = str(tmp_path / "s.pdf"); forms_pdf(s)
    with pikepdf.open(s) as pdf:
        pg = pdf.pages[0]; ins = list(pikepdf.parse_content_stream(pg))
        out = [pikepdf.ContentStreamInstruction([pikepdf.Name.Artifact], pikepdf.Operator("BMC"))] + ins + [pikepdf.ContentStreamInstruction([], pikepdf.Operator("EMC"))]
        pg.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out)); pdf.save(str(tmp_path / "hid.pdf"))
    ids = {c["id"] for c in validate.run(str(tmp_path / "hid.pdf"), external=False)["checks"] if c["status"] == "failed"}
    assert "form.hidden" in ids

def test_graphics_only_form_stays_an_artifact(tmp_path):
    s, o = str(tmp_path / "s.pdf"), str(tmp_path / "o.pdf"); forms_pdf(s, graphics_only=True)
    r = engine.fix(s, o)
    assert not any("flattened" in a["action"] for a in r["actions"])
    assert not {c["id"] for c in validate.run(o, original=s, external=False)["checks"] if c["status"] in ("failed", "error")}

def test_form_resources_are_renamed_without_collisions(tmp_path):
    """Page and Form both call their font /F1 but they are different fonts: after flattening each text keeps its own font."""
    s, o = str(tmp_path / "s.pdf"), str(tmp_path / "o.pdf")
    c = canvas.Canvas(s, pagesize=letter)
    c.beginForm("f"); c.setFont("Courier", 14); c.drawString(72, 650, "Typed inside the form"); c.endForm()
    c.setFont("Times-Roman", 14); c.drawString(72, 700, "Typed on the page"); c.doForm("f"); c.showPage(); c.save()
    engine.fix(s, o); d = pymupdf.open(o)
    fonts = {w[4]: None for w in d[0].get_text("words")}
    spans = {sp["text"].strip(): sp["font"] for b in d[0].get_text("dict")["blocks"] for l in b.get("lines", []) for sp in l["spans"]}
    assert "Courier" in " ".join(f for t, f in spans.items() if "form" in t) or "Mono" in " ".join(f for t, f in spans.items() if "form" in t)
    assert "Typed on the page" in d[0].get_text() and "Typed inside the form" in d[0].get_text()
