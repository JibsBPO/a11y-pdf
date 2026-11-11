import pikepdf
from pikepdf import Array, Dictionary, Name
import engine, structure


def make_tagged(path, pages=3, headings=False, direct_mcr=False):
    with pikepdf.new() as pdf:
        doc=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Document,K=Array()))
        st=pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot,K=Array([doc]))); doc.P=st
        for i in range(pages):
            page=pdf.add_blank_page(page_size=(300,400))
            page.Contents=pdf.make_stream(f"/Span <</MCID 0>> BDC BT /F1 12 Tf 20 350 Td (Page {i+1}) Tj ET EMC".encode())
            page.Resources=Dictionary(Font=Dictionary({"/F1":Dictionary(Type=Name.Font,Subtype=Name.Type1,BaseFont=Name.Helvetica)}))
            role="H1" if headings else "P"
            el=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name("/"+role),P=doc,Pg=page.obj,K=0)); doc.K.append(el)
        if direct_mcr:
            doc.K.append(Dictionary(Type=Name.MCR,MCID=77,Pg=pdf.pages[0].obj))
        pdf.Root.StructTreeRoot=st; pdf.Root.MarkInfo=Dictionary(Marked=True); pdf.save(path)


def elements(node, role):
    out=[]
    if isinstance(node,pikepdf.Dictionary):
        if str(node.get("/S"))=="/"+role: out.append(node)
        k=node.get("/K"); vals=list(k) if isinstance(k,pikepdf.Array) else ([k] if k is not None else [])
        for x in vals: out.extend(elements(x,role))
    elif isinstance(node,pikepdf.Array):
        for x in node: out.extend(elements(x,role))
    return out


def test_page_mode_creates_one_section_per_page_and_preserves_content(tmp_path):
    src=str(tmp_path/"pages.pdf"); out=str(tmp_path/"pages-fixed.pdf"); make_tagged(src)
    engine.fix(src,out,do_ocr=False,sections="page")
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages)==3 and len(elements(pdf.Root.StructTreeRoot,"Sect"))==3
        assert "Page 1" in str(pdf.pages[0].Contents.read_bytes())
        doc=pdf.Root.StructTreeRoot.K[0]
        assert len(doc.K)==3


def test_heading_mode_one_section_per_h1_and_idempotent(tmp_path):
    src=str(tmp_path/"heads.pdf"); out=str(tmp_path/"heads-fixed.pdf"); out2=str(tmp_path/"heads-fixed2.pdf"); make_tagged(src,headings=True)
    engine.fix(src,out,do_ocr=False,sections="heading")
    engine.fix(out,out2,do_ocr=False,sections="heading")
    with pikepdf.open(out) as a,pikepdf.open(out2) as b:
        assert len(elements(a.Root.StructTreeRoot,"Sect"))==len(elements(a.Root.StructTreeRoot,"H1"))==3
        assert len(elements(b.Root.StructTreeRoot,"Sect"))==3 and len(b.pages)==3


def test_default_mode_uses_exactly_one_section_for_entire_document(tmp_path):
    src=str(tmp_path/"multi-section-input.pdf"); out=str(tmp_path/"single-section.pdf"); out2=str(tmp_path/"single-section-twice.pdf")
    make_tagged(src,pages=3,headings=True)
    engine.fix(src,out,do_ocr=False)
    engine.fix(out,out2,do_ocr=False)
    with pikepdf.open(out2) as pdf:
        assert len(elements(pdf.Root.StructTreeRoot,"Sect"))==1
        assert len(pdf.pages)==3
        assert all(f"Page {i+1}" in str(page.Contents.read_bytes()) for i,page in enumerate(pdf.pages))


def test_grouping_skips_document_direct_mcr_without_dropping_content(tmp_path):
    src=str(tmp_path/"direct-mcr.pdf"); make_tagged(src,direct_mcr=True)
    with pikepdf.open(src) as pdf:
        doc=pdf.Root.StructTreeRoot.K[0]; before=len(doc.K)
        res=structure.group_sections(pdf,"page")
        assert not res["changed"] and "skipped" in res["reason"] and len(doc.K)==before


def test_none_mode_does_not_group(tmp_path):
    src=str(tmp_path/"none.pdf"); make_tagged(src)
    with pikepdf.open(src) as pdf:
        assert structure.group_sections(pdf,"none")["changed"] is False


def test_default_groups_all_pages_under_one_section(tmp_path):
    src=str(tmp_path/"default-sections.pdf"); out=str(tmp_path/"default-fixed.pdf")
    make_tagged(src,pages=3,headings=False)
    engine.fix(src,out,do_ocr=False)
    with pikepdf.open(out) as pdf:
        assert len(pdf.pages)==3
        assert len(elements(pdf.Root.StructTreeRoot,"Sect"))==1
        assert len(elements(pdf.Root.StructTreeRoot,"P"))==3
        assert all(f"Page {i+1}" in str(page.Contents.read_bytes()) for i,page in enumerate(pdf.pages))
