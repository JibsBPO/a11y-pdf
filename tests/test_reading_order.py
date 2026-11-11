import pikepdf
import pymupdf
from pikepdf import Array, Dictionary, Name
import engine


def test_clear_two_column_units_are_ordered_down_then_across():
    # Source order deliberately alternates columns; the visible page is two-column.
    units = [
        {"i0": 0, "bbox": [320, 10, 500, 30], "label": "right top"},
        {"i0": 1, "bbox": [40, 50, 220, 70], "label": "left lower"},
        {"i0": 2, "bbox": [320, 50, 500, 70], "label": "right lower"},
        {"i0": 3, "bbox": [40, 10, 220, 30], "label": "left top"},
    ]
    ordered = engine.reading_order_units(units, type("Rect", (), {"width": 600, "x0": 0})())
    assert [u["label"] for u in ordered] == ["left top", "left lower", "right top", "right lower"]


def test_ambiguous_single_column_keeps_content_stream_order():
    units = [
        {"i0": 4, "bbox": [40, 50, 500, 70], "label": "second"},
        {"i0": 1, "bbox": [40, 10, 500, 30], "label": "first"},
    ]
    ordered = engine.reading_order_units(units, type("Rect", (), {"width": 600, "x0": 0})())
    assert [u["label"] for u in ordered] == ["first", "second"]


def test_custom_role_names_are_mapped_only_from_explicit_semantics():
    assert engine.guess_role("GK_Paragraph", True) == "P"
    assert engine.guess_role("GK_P", False) == "Span"
    assert engine.guess_role("GK_Heading1", True) == "H1"
    assert engine.guess_role("Figure_1", False) == "Figure"
    assert engine.guess_role("Title", True) == "H1"
    assert engine.guess_role("GK_Head_A", True) == "P"


def test_fix_does_not_guess_heading_from_ambiguous_custom_role(tmp_path):
    src=str(tmp_path/"custom-heading.pdf"); dst=str(tmp_path/"custom-heading-fixed.pdf")
    with pikepdf.new() as pdf:
        page=pdf.add_blank_page(page_size=(612,792))
        font=pdf.make_indirect(Dictionary(Type=Name.Font,Subtype=Name.Type1,BaseFont=Name.Helvetica,Encoding=Name.WinAnsiEncoding))
        page.Resources=Dictionary(Font=Dictionary({"/F1":font}))
        page.Contents=pdf.make_stream(b"/Span <</MCID 0>> BDC BT /F1 20 Tf 40 700 Td (Section heading) Tj ET EMC")
        doc=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Document,K=Array()))
        st=pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot,K=Array([doc]),RoleMap=Dictionary({"/GK_Head_A":Name.P})))
        doc.P=st
        heading=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name("/GK_Head_A"),P=doc,Pg=page.obj,K=0))
        doc.K.append(heading); pdf.Root.StructTreeRoot=st; pdf.Root.MarkInfo=Dictionary(Marked=True); pdf.save(src)
    engine.fix(src,dst,do_ocr=False)
    with pikepdf.open(dst) as pdf:
        def find(node,role):
            if isinstance(node,pikepdf.Dictionary):
                if str(node.get("/S"))==role:return node
                k=node.get("/K"); vals=list(k) if isinstance(k,pikepdf.Array) else [k]
                for x in vals:
                    out=find(x,role)
                    if out:return out
        tagged=find(pdf.Root.StructTreeRoot,"/P")
        assert str(tagged.S) == "/P"


def test_fix_reorders_mixed_paragraph_kids_by_visible_lines(tmp_path):
    src=str(tmp_path/"mixed-order.pdf"); dst=str(tmp_path/"mixed-order-fixed.pdf")
    with pikepdf.new() as pdf:
        page=pdf.add_blank_page(page_size=(612,792))
        font=pdf.make_indirect(Dictionary(Type=Name.Font,Subtype=Name.Type1,BaseFont=Name.Helvetica,Encoding=Name.WinAnsiEncoding))
        page.Resources=Dictionary(Font=Dictionary({"/F1":font}))
        page.Contents=pdf.make_stream(b"/Span <</MCID 0>> BDC BT /F1 12 Tf 40 700 Td (First) Tj ET EMC\n/Span <</MCID 1>> BDC BT /F1 12 Tf 130 700 Td (inline figure label) Tj ET EMC\n/Span <</MCID 2>> BDC BT /F1 12 Tf 40 680 Td (Next line) Tj ET EMC")
        doc=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Document,K=Array()))
        st=pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot,K=Array([doc]))); doc.P=st
        para=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.P,P=doc,Pg=page.obj,K=Array()))
        nested=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Span,P=para,Pg=page.obj,K=0))
        para.K=Array([1,2,nested]); doc.K.append(para)
        pdf.Root.StructTreeRoot=st; pdf.Root.MarkInfo=Dictionary(Marked=True); pdf.save(src)
    result=engine.fix(src,dst,do_ocr=False)
    assert any(a["action"]=="Paragraph reading order aligned to marked-content geometry" for a in result["actions"])
    with pikepdf.open(dst) as pdf:
        def find(node):
            if isinstance(node,pikepdf.Dictionary):
                if str(node.get("/S"))=="/P":return node
                k=node.get("/K"); vals=list(k) if isinstance(k,pikepdf.Array) else [k]
                for x in vals:
                    got=find(x)
                    if got:return got
        para=find(pdf.Root.StructTreeRoot)
        kids=list(para.K)
        def first_mcid(kid):
            if isinstance(kid,int): return kid
            if isinstance(kid,pikepdf.Dictionary):
                value=kid.get("/K")
                if isinstance(value,pikepdf.Array): value=value[0]
                return int(value)
            raise AssertionError(f"unexpected kid {kid!r}")
        assert [first_mcid(k) for k in kids]==[0,1,2]


def test_fix_reorders_existing_paragraph_tags_by_page_geometry(tmp_path):
    src=str(tmp_path/"tag-order.pdf"); dst=str(tmp_path/"tag-order-fixed.pdf")
    with pikepdf.new() as pdf:
        page=pdf.add_blank_page(page_size=(612,792))
        font=pdf.make_indirect(Dictionary(Type=Name.Font,Subtype=Name.Type1,BaseFont=Name.Helvetica,Encoding=Name.WinAnsiEncoding))
        page.Resources=Dictionary(Font=Dictionary({"/F1":font}))
        page.Contents=pdf.make_stream(b"/Span <</MCID 0>> BDC BT /F1 12 Tf 40 720 Td (First paragraph.) Tj ET EMC\n/Span <</MCID 1>> BDC BT /F1 12 Tf 40 680 Td (Second paragraph.) Tj ET EMC")
        doc=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Document,K=Array()))
        st=pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot,K=Array([doc]))); doc.P=st
        for mid in (1,0): doc.K.append(pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.P,P=doc,Pg=page.obj,K=mid)))
        pdf.Root.StructTreeRoot=st; pdf.Root.MarkInfo=Dictionary(Marked=True); pdf.save(src)
    before=pymupdf.open(src)[0].get_text()
    result=engine.fix(src,dst,do_ocr=False)
    with pikepdf.open(dst) as pdf:
        def content_mcid(el):
            k=el.get("/K")
            return int(k) if isinstance(k,int) else int(list(k)[0])
        paragraphs=[]
        def walk(node):
            if isinstance(node,pikepdf.Array):
                for child in node: walk(child)
            elif isinstance(node,pikepdf.Dictionary):
                if str(node.get("/S"))=="/P": paragraphs.append(node)
                walk(node.get("/K"))
        walk(pdf.Root.StructTreeRoot)
        assert [content_mcid(p) for p in paragraphs]==[0,1]
        assert len(pdf.pages)==1
    assert pymupdf.open(dst)[0].get_text()==before
    assert any(a["action"]=="Tagged structure reading order aligned to page geometry" for a in result["actions"])
