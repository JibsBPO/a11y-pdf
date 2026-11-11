import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pikepdf
from pikepdf import Array, Dictionary, Name

import engine
import validate


def make_mcid_collision_source(path, duplicate=False, dangling=False):
    """Tagged MCIDs 0-6, orphan 7, and one genuinely untagged text run."""
    with pikepdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(612, 792))
        font = Name.F1
        page.Resources = Dictionary(Font=Dictionary({"/F1": Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica, Encoding=Name.WinAnsiEncoding)}))
        chunks = []
        for mid in range(8):
            chunks.append(f"/Span <</MCID {mid}>> BDC BT {font} 12 Tf 40 {730-mid*18} Td (Tagged {mid}) Tj ET EMC")
        if duplicate:
            chunks.append(f"/Span <</MCID 7>> BDC BT {font} 12 Tf 40 575 Td (Repeated orphan) Tj ET EMC")
        chunks.append(f"BT {font} 12 Tf 40 560 Td (Untagged text) Tj ET")
        page.Contents = pdf.make_stream("\n".join(chunks).encode("ascii"))

        doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, K=Array()))
        st = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot, K=Array([doc])))
        doc.P = st
        for mid in range(7):
            el = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Span, P=doc, Pg=page.obj, K=mid))
            doc.K.append(el)
        if dangling:
            el = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Span, P=doc, Pg=page.obj, K=999))
            doc.K.append(el)
        pdf.Root.StructTreeRoot = st
        pdf.Root.MarkInfo = Dictionary(Marked=True)
        pdf.save(path)


def _mcids(pdf, page):
    return [int(c.operands[1]["/MCID"]) for c in pikepdf.parse_content_stream(page)
            if not isinstance(c, pikepdf.ContentStreamInlineImage)
            and str(c.operator) == "BDC" and len(c.operands) > 1
            and isinstance(c.operands[1], pikepdf.Dictionary) and "/MCID" in c.operands[1]]


def test_form_stream_mcids_do_not_pollute_page_parent_tree_scope():
    with pikepdf.new() as pdf:
        form = pdf.make_stream(b"/Span <</MCID 4>> BDC EMC")
        form.Type = Name.XObject
        form.Subtype = Name.Form
        form_mcr = Dictionary(Type=Name.MCR, MCID=4, Stm=form)
        page_mcr = Dictionary(Type=Name.MCR, MCID=2)
        el = Dictionary(K=Array([page_mcr, form_mcr]))
        assert engine.mcids_of(el) == [2]


def test_fix_allocates_above_orphans_and_links_them(tmp_path):
    src = str(tmp_path / "orphan.pdf")
    dst = str(tmp_path / "fixed.pdf")
    make_mcid_collision_source(src)

    engine.fix(src, dst, do_ocr=False)

    with pikepdf.open(dst) as pdf:
        ids = _mcids(pdf, pdf.pages[0])
        assert len(ids) == len(set(ids))
        assert 7 in ids
        assert max(ids) > 7  # the new untagged text must not reuse orphan MCID 7
    result = validate.run(dst, original=src, external=False)
    mcid_check = next(c for c in result["checks"] if c["id"] == "struct.mcid")
    assert mcid_check["status"] == "passed"
    assert "Untagged text" in __import__("pymupdf").open(dst)[0].get_text()

    second = str(tmp_path / "fixed-twice.pdf")
    engine.fix(dst, second, do_ocr=False)
    with pikepdf.open(second) as pdf:
        ids_twice = _mcids(pdf, pdf.pages[0])
        assert ids_twice == ids
    twice_check = next(c for c in validate.run(second, external=False)["checks"] if c["id"] == "struct.mcid")
    assert twice_check["status"] == "passed"


def test_fix_renumbers_duplicate_content_mcids(tmp_path):
    src = str(tmp_path / "duplicate.pdf")
    dst = str(tmp_path / "duplicate-fixed.pdf")
    make_mcid_collision_source(src, duplicate=True)
    engine.fix(src, dst, do_ocr=False)
    with pikepdf.open(dst) as pdf:
        ids = _mcids(pdf, pdf.pages[0])
        assert len(ids) == len(set(ids))
        assert 7 in ids
        assert max(ids) >= 9
    check = next(c for c in validate.run(dst, external=False)["checks"] if c["id"] == "struct.mcid")
    assert check["status"] == "passed"


def test_fix_reconciles_stale_structure_references(tmp_path):
    src = str(tmp_path / "stale.pdf")
    dst = str(tmp_path / "stale-fixed.pdf")
    make_mcid_collision_source(src, dangling=True)
    result = engine.fix(src, dst, do_ocr=False)
    assert any(a["action"] == "Stale page MCID references removed" for a in result["actions"])
    check = next(c for c in validate.run(dst, external=False)["checks"] if c["id"] == "struct.mcid")
    assert check["status"] == "passed"
