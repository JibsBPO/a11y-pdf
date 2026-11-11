import pikepdf
from pikepdf import Array, Dictionary, Name
import engine, validate


def make_list_pdf(path, nested=False, nested_only=False):
    with pikepdf.new() as pdf:
        page=pdf.add_blank_page(page_size=(300,400))
        page.Contents=pdf.make_stream(b"/Span <</MCID 0>> BDC BT /F1 12 Tf 20 350 Td (Item one) Tj ET EMC")
        page.Resources=Dictionary(Font=Dictionary({"/F1":Dictionary(Type=Name.Font,Subtype=Name.Type1,BaseFont=Name.Helvetica)}))
        doc=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Document,K=Array()))
        st=pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot,K=Array([doc]))); doc.P=st
        lst=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.L,P=doc,K=Array())); doc.K.append(lst)
        li=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.LI,P=lst,Pg=page.obj,K=Array([0])))
        lst.K.append(li)
        if nested_only:
            sublist=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.L,P=li,K=Array()))
            child=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.LI,P=sublist,Pg=page.obj,K=Array([0])))
            sublist.K.append(child); li.K=Array([sublist])
        if nested:
            page.Contents=pdf.make_stream(b"/Span <</MCID 0>> BDC BT /F1 12 Tf 20 350 Td (Item one) Tj ET EMC\n/Span <</MCID 1>> BDC BT /F1 12 Tf 20 330 Td (Nested item) Tj ET EMC")
            body=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.LBody,P=li,K=Array([0])))
            sublist=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.L,P=li,K=Array()))
            child=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.LI,P=sublist,Pg=page.obj,K=Array()))
            childbody=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.LBody,P=child,K=Array([1])))
            child.K.append(childbody); sublist.K.append(child); li.K=Array([body,sublist])
        pdf.Root.StructTreeRoot=st; pdf.Root.MarkInfo=Dictionary(Marked=True); pdf.save(path)


def statuses(pdf_path):
    result=validate.run(pdf_path,external=False)
    return result,{c["id"]:c["status"] for c in result["checks"]}


def test_direct_list_item_content_is_wrapped_in_lbody_without_losing_text(tmp_path):
    src=str(tmp_path/"list-direct.pdf"); dst=str(tmp_path/"list-fixed.pdf"); make_list_pdf(src)
    before,_=statuses(src); assert next(c for c in before["checks"] if c["id"]=="list.structure")["status"]=="failed"
    engine.fix(src,dst,do_ocr=False)
    after,st=statuses(dst)
    with pikepdf.open(dst) as pdf:
        assert len(pdf.pages)==1
        lst=pdf.Root.StructTreeRoot.K[0].K[0]; li=lst.K[0]
        assert str(li.K[0].S)=="/LBody" and len(li.K[0].K)==1 and int(li.K[0].K[0])==0
    import pymupdf
    assert pymupdf.open(src)[0].get_text()==pymupdf.open(dst)[0].get_text()
    assert st["list.structure"]=="passed" and st["struct.mcid"]=="passed" and st["struct.parenttree"]=="passed"


def test_nested_list_is_moved_under_lbody_in_reading_order(tmp_path):
    src=str(tmp_path/"nested-list.pdf"); dst=str(tmp_path/"nested-list-fixed.pdf"); make_list_pdf(src,nested=True)
    engine.fix(src,dst,do_ocr=False)
    result,st=statuses(dst)
    with pikepdf.open(dst) as pdf:
        li=pdf.Root.StructTreeRoot.K[0].K[0].K[0]
        body=li.K[0]
        assert str(body.S)=="/LBody" and str(body.K[1].S)=="/L"
        assert body.K[1].P.objgen==body.objgen and len(pdf.pages)==1
    import pymupdf
    assert pymupdf.open(src)[0].get_text()==pymupdf.open(dst)[0].get_text()
    assert st["list.structure"]=="passed" and st["struct.mcid"]=="passed" and st["struct.parenttree"]=="passed"


def test_list_only_parent_is_not_given_invented_empty_lbody(tmp_path):
    src=str(tmp_path/"ambiguous-list.pdf"); dst=str(tmp_path/"ambiguous-list-fixed.pdf")
    make_list_pdf(src,nested_only=True)
    result=engine.fix(src,dst,do_ocr=False)
    with pikepdf.open(dst) as pdf:
        parent_li=pdf.Root.StructTreeRoot.K[0].K[0].K[0]
        assert str(parent_li.K[0].S)=="/L" and len(parent_li.K)==1 and len(pdf.pages)==1
    import pymupdf
    assert pymupdf.open(src)[0].get_text()==pymupdf.open(dst)[0].get_text()
    assert any(a["action"]=="List hierarchy needs human review" for a in result["actions"])
    _,st=statuses(dst)
    assert st["list.structure"]=="failed" and st["struct.mcid"]=="passed" and st["struct.parenttree"]=="passed"
