"""Run:  pytest -q      (from the a11y/ folder)"""
import pikepdf, pytest
from pikepdf import Name, Dictionary, Array
import engine, validate

def kinds(v, status=("failed", "error")): return {c["id"] for c in v["checks"] if c["status"] in status}
def elems(pdf):
    out = []
    def w(n):
        if isinstance(n, pikepdf.Array):
            for k in n: w(k)
        elif isinstance(n, pikepdf.Dictionary) and "/S" in n: out.append(n); w(n.get("/K"))
    w(pdf.Root.StructTreeRoot.K); return out

# ---- happy path -------------------------------------------------------------------------------
def test_untagged_source_fails_before(src):
    v = validate.run(src, external=False)
    assert v["verdict"] == "failed" and {"struct.tree", "struct.marked", "meta.lang"} <= kinds(v)

def test_repaired_file_has_no_failures_but_is_never_called_passed(src, fixed):
    v = validate.run(fixed, original=src, external=False)
    assert kinds(v) == set(), kinds(v)
    assert v["verdict"] == "needs_review"                       # manual checks always remain
    assert {"man.order", "man.at", "fig.quality"} <= kinds(v, ("needs_review",))

def test_external_pending_is_reported_not_hidden(src, fixed, monkeypatch):
    monkeypatch.delenv("VERAPDF", raising=False); monkeypatch.setattr("shutil.which", lambda x: None)
    v = validate.run(fixed, original=src)
    assert {e["status"] for e in v["external"]} == {"pending"}

def test_before_after_comparison(src, fixed):
    b = validate.run(src, external=False); a = validate.run(fixed, original=src, external=False)
    out = {o["id"]: o["outcome"] for o in validate.compare(b, a)}
    assert out["struct.tree"] == "fixed" and out["meta.lang"] == "fixed" and a["totals"]["failed"] < b["totals"]["failed"]

def test_before_after_comparison_marks_new_failure_as_regression(fixed):
    before = validate.run(fixed, external=False)
    after = validate.run(fixed, external=False)
    target = next(c for c in after["checks"] if c["id"] == "meta.lang")
    target["status"] = "failed"
    outcomes = {x["id"]: x["outcome"] for x in validate.compare(before, after)}
    assert outcomes["meta.lang"] == "regressed"

# ---- the repaired file is damaged afterwards: validator must notice (engine.analyze mostly does not) ---
def m_orphan(p):
    # Remove an element that actually owns marked content. Fixed output may
    # group adjacent fragments, so a positional child index is not stable.
    target = next(e for e in elems(p) if any(isinstance(k, int) for k in (list(e.K) if isinstance(e.get("/K"), pikepdf.Array) else [e.get("/K")]) if k is not None))
    parent = target.P; kids = list(parent.K) if isinstance(parent.K, pikepdf.Array) else [parent.K]
    parent.K = Array([k for k in kids if not (isinstance(k, pikepdf.Dictionary) and validate.og(k) == validate.og(target))])
def m_dangling(p): next(e for e in elems(p) if str(e.S) == "/P").K = Array([999])
def m_stray_emc(p): pg = p.pages[0]; pg.Contents = p.make_stream(pg.Contents.read_bytes() + b"\nEMC\n")
def m_parenttree(p): p.Root.StructTreeRoot.ParentTree = p.make_indirect(Dictionary(Nums=Array()))
def m_swap_parent(p): arr = p.Root.StructTreeRoot.ParentTree.Nums[1]; arr[0], arr[1] = arr[1], arr[0]
def m_no_alt(p): del next(e for e in elems(p) if str(e.S) == "/Figure")["/Alt"]
def m_untagged_path(p): pg = p.pages[1]; pg.Contents = p.make_stream(pg.Contents.read_bytes() + b"\n0 0 50 50 re f\n")
def m_heading_skip(p): [e for e in elems(p) if str(e.S).startswith("/H")][-1].S = Name.H4
def m_no_lang(p): del p.Root["/Lang"]
def m_bad_xmp(p): p.Root.Metadata = p.make_stream(b"<x:xmpmeta><unclosed>")
def m_lost_text(p):
    pg = p.pages[0]; first = [True]; out = []
    for c in pikepdf.parse_content_stream(pg):
        if str(c.operator) == "Tj" and first[0]: first[0] = False; continue
        out.append(c)
    pg.Contents = p.make_stream(pikepdf.unparse_content_stream(out))

@pytest.mark.parametrize("mut,expect", [
    (m_orphan, "struct.mcid"), (m_dangling, "struct.mcid"), (m_stray_emc, "struct.marked"), (m_parenttree, "struct.parenttree"),
    (m_swap_parent, "struct.parenttree"), (m_no_alt, "fig.alt"), (m_untagged_path, "struct.marked"), (m_heading_skip, "head.hierarchy"),
    (m_no_lang, "meta.lang"), (m_bad_xmp, "meta.xmp"), (m_lost_text, "keep.text")])
def test_validator_catches_damage(src, fixed, tmp_path, mut, expect):
    p = pikepdf.open(fixed); mut(p); out = str(tmp_path / "m.pdf"); p.save(out)
    v = validate.run(out, original=src, external=False)
    assert expect in kinds(v) and v["verdict"] == "failed"

def test_placeholder_alt_and_vague_link_need_review_not_pass(src, fixed, tmp_path):
    p = pikepdf.open(fixed)
    next(e for e in elems(p) if str(e.S) == "/Figure").Alt = pikepdf.String("Figure (alt text needs review)")
    for a in p.pages[0].Annots: a.Contents = pikepdf.String("click here")
    out = str(tmp_path / "m.pdf"); p.save(out); v = validate.run(out, original=src, external=False)
    assert kinds(v) == set() and {"link.text", "fig.quality"} <= kinds(v, ("needs_review",))

def test_garbage_input_is_error_not_pass(tmp_path):
    f = tmp_path / "x.pdf"; f.write_bytes(b"%PDF-1.4 not really")
    with pytest.raises(Exception): validate.run(str(f), external=False)

def test_unused_declared_font_is_not_a_failure(fixed):
    assert "font.embed" not in kinds(validate.run(fixed, external=False))      # ReportLab declares Helvetica but never uses it


def test_category_summary_counts_checks_and_findings_separately(fixed):
    result = validate.run(fixed, external=False)
    summary = {row["group"]: row for row in result["category_summary"]}
    assert sum(row["checks"] for row in summary.values()) == result["totals"]["checks"]
    assert summary["Structure"]["passed"] > 0
    assert summary["Manual review"]["warned"] == 5
    assert sum(row["failed"] + row["errors"] for row in summary.values()) == result["totals"]["checks_failed"]
    assert "PAC" not in summary
