import pikepdf
from pikepdf import Array, Dictionary, Name
import pymupdf
import engine, validate


def make_tagged_page(path, instructions, children):
    with pikepdf.new() as pdf:
        page=pdf.add_blank_page(page_size=(400,400))
        page.Contents=pdf.make_stream(instructions)
        page.Resources=Dictionary(Font=Dictionary({"/F1":Dictionary(Type=Name.Font,Subtype=Name.Type1,BaseFont=Name.Helvetica)}))
        doc=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Document,K=Array()))
        st=pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot,K=Array([doc]))); doc.P=st
        for child in children: doc.K.append(child(pdf,doc,page))
        pdf.Root.StructTreeRoot=st; pdf.Root.MarkInfo=Dictionary(Marked=True); pdf.save(path)


def find_roles(node, role):
    out=[]
    def visit(n):
        if isinstance(n,pikepdf.Array):
            for x in n: visit(x)
        elif isinstance(n,pikepdf.Dictionary) and n.get("/Type") not in (Name.MCR,Name.OBJR):
            if str(n.get("/S",""))=="/"+role: out.append(n)
            visit(n.get("/K"))
    visit(node); return out


def test_mixed_word_fragment_tags_collapse_to_one_heading_without_changing_text(tmp_path):
    src=str(tmp_path/"fragmented-heading.pdf"); dst=str(tmp_path/"heading-fixed.pdf")
    content=(b"/Span <</MCID 0>> BDC BT /F1 16 Tf 30 370 Td (CHAPTER 2) Tj ET EMC\n"
             b"/Span <</MCID 1>> BDC BT /F1 16 Tf 30 330 Td (AI) Tj ET EMC\n"
             b"/Span <</MCID 2>> BDC BT /F1 16 Tf 47 330 Td ( Trust,) Tj ET EMC\n"
             b"/Span <</MCID 3>> BDC BT /F1 16 Tf 104 330 Td ( Compliance and Security) Tj ET EMC\n"
             b"/Span <</MCID 4>> BDC BT /F1 12 Tf 30 290 Td (A complete paragraph) Tj ET EMC\n"
             b"/Span <</MCID 5>> BDC BT /F1 12 Tf 30 274 Td (continues on the next line.) Tj ET EMC")
    def h1(pdf,doc,page): return pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.H1,P=doc,Pg=page.obj,K=0))
    def h2(pdf,doc,page): return pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.H2,P=doc,Pg=page.obj,K=1))
    def p1(pdf,doc,page): return pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.P,P=doc,Pg=page.obj,K=2))
    def h3(pdf,doc,page): return pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.H3,P=doc,Pg=page.obj,K=3))
    def p2(pdf,doc,page): return pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.P,P=doc,Pg=page.obj,K=4))
    def p3(pdf,doc,page): return pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.P,P=doc,Pg=page.obj,K=5))
    make_tagged_page(src,content,[h1,h2,p1,h3,p2,p3])
    before=pymupdf.open(src)[0].get_text()
    engine.fix(src,dst,do_ocr=False,sections="none")
    with pikepdf.open(dst) as pdf:
        assert len(pdf.pages)==1
        assert len(find_roles(pdf.Root.StructTreeRoot,"H1"))==1
        heads=find_roles(pdf.Root.StructTreeRoot,"H2")
        assert len(heads)==1 and list(heads[0].K)==[1,2,3]
        assert len(find_roles(pdf.Root.StructTreeRoot,"H3"))==0
        paragraphs=find_roles(pdf.Root.StructTreeRoot,"P")
        assert len(paragraphs)==1 and list(paragraphs[0].K)==[4,5]
    assert pymupdf.open(dst)[0].get_text()==before


def test_visual_list_order_repaired_without_rewriting_page_content(tmp_path):
    src=str(tmp_path/"list-order.pdf"); dst=str(tmp_path/"list-order-fixed.pdf")
    content=(b"/Span <</MCID 0>> BDC BT /F1 12 Tf 30 320 Td (Lower item) Tj ET EMC\n"
             b"/Span <</MCID 1>> BDC BT /F1 12 Tf 30 350 Td (Upper item) Tj ET EMC")
    def make_list(pdf,doc,page):
        lst=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.L,P=doc,K=Array()))
        for mcid,y in ((0,320),(1,350)):
            li=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.LI,P=lst,Pg=page.obj,K=mcid)); lst.K.append(li)
        return lst
    make_tagged_page(src,content,[make_list])
    before=pymupdf.open(src)[0].get_text()
    result=engine.fix(src,dst,do_ocr=False,sections="none")
    with pikepdf.open(dst) as pdf:
        lst=find_roles(pdf.Root.StructTreeRoot,"L")[0]
        assert [int(li.K[0].K[0]) for li in lst.K]==[1,0]
        assert all(str(li.K[0].S)=="/LBody" for li in lst.K)
        checked=validate.run(dst,external=False)
        assert next(c for c in checked["checks"] if c["id"]=="list.structure")["status"]=="passed"
        assert next(c for c in checked["checks"] if c["id"]=="struct.mcid")["status"]=="passed"
        assert next(c for c in checked["checks"] if c["id"]=="struct.parenttree")["status"]=="passed"
    assert pymupdf.open(dst)[0].get_text()==before
    assert any(a["action"]=="List items reordered to match page reading order" for a in result["actions"])


def test_only_existing_invalid_figure_bbox_is_repaired_from_mcid_geometry(tmp_path):
    src=str(tmp_path/"bbox.pdf"); dst=str(tmp_path/"bbox-fixed.pdf")
    content=(b"/Span <</MCID 0>> BDC 10 20 40 30 re f EMC\n"
             b"/Span <</MCID 1>> BDC 100 100 30 25 re f EMC")
    def figure(pdf,doc,page,mcid,box):
        attr=Dictionary(O=Name.Layout,BBox=Array(box))
        return pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Figure,P=doc,Pg=page.obj,K=mcid,Alt=pikepdf.String("Descriptive figure"),A=attr))
    def bad(pdf,doc,page): return figure(pdf,doc,page,0,[10,20,10,40])
    def good(pdf,doc,page): return figure(pdf,doc,page,1,[100,100,130,125])
    make_tagged_page(src,content,[bad,good])
    original_text=pymupdf.open(src)[0].get_text(); before=validate.run(src,external=False)
    assert next(c for c in before["checks"] if c["id"]=="fig.bbox")["status"]=="failed"
    result=engine.fix(src,dst,do_ocr=False,sections="none")
    with pikepdf.open(dst) as pdf:
        figs=find_roles(pdf.Root.StructTreeRoot,"Figure")
        by_mcid={int(x.K):[float(v) for v in x.A.BBox] for x in figs}
        assert by_mcid[0]==[10.0,20.0,50.0,50.0]
        assert by_mcid[1]==[100.0,100.0,130.0,125.0]
        after=validate.run(dst,original=src,external=False)
        assert next(c for c in after["checks"] if c["id"]=="fig.bbox")["status"]=="passed"
        assert next(c for c in after["checks"] if c["id"]=="struct.mcid")["status"]=="passed"
        assert len(pdf.pages)==1
    assert pymupdf.open(dst)[0].get_text()==original_text
    assert sum("bounding boxes aligned" in a["action"].lower() for a in result["actions"])==1
