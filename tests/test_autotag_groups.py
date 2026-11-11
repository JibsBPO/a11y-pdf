import pikepdf, pymupdf
from pikepdf import Array, Dictionary, Name
import engine


def role_tree(node, role):
    found=[]
    if isinstance(node,pikepdf.Dictionary):
        if str(node.get("/S"))=="/"+role: found.append(node)
        k=node.get("/K"); values=list(k) if isinstance(k,pikepdf.Array) else ([k] if k is not None else [])
        for value in values: found.extend(role_tree(value,role))
    elif isinstance(node,pikepdf.Array):
        for value in node: found.extend(role_tree(value,role))
    return found


def fragmented_text_pdf(path):
    with pikepdf.new() as pdf:
        page=pdf.add_blank_page(page_size=(612,792))
        font=Dictionary(Type=Name.Font,Subtype=Name.Type1,BaseFont=Name.Helvetica,Encoding=Name.WinAnsiEncoding)
        page.Resources=Dictionary(Font=Dictionary({"/F1":font}))
        page.Contents=pdf.make_stream(b"""BT /F1 18 Tf 1 0 0 1 50 730 Tm (Ways to Make) Tj ET
BT /F1 18 Tf 1 0 0 1 170 730 Tm (Numbers to 8) Tj ET
BT /F1 12 Tf 1 0 0 1 50 680 Tm (This is one) Tj ET
BT /F1 12 Tf 1 0 0 1 122 680 Tm (complete sentence.) Tj ET
BT /F1 12 Tf 1 0 0 1 50 650 Tm (Another paragraph.) Tj ET""")
        pdf.save(path)


def test_fragments_become_sentence_level_paragraphs_and_one_heading(tmp_path):
    src=str(tmp_path/"fragments.pdf"); dst=str(tmp_path/"grouped.pdf"); fragmented_text_pdf(src)
    engine.fix(src,dst,do_ocr=False,sections="none")
    with pikepdf.open(dst) as pdf:
        doc=pdf.Root.StructTreeRoot.K[0]
        h1=role_tree(doc,"H1"); paragraphs=role_tree(doc,"P")
        assert len(h1)==1 and len(h1[0].K)==2
        assert len(paragraphs)==2 and sorted(len(p.K) for p in paragraphs)==[1,2]
        assert len(pdf.pages)==1
    with pymupdf.open(src) as before,pymupdf.open(dst) as after:
        assert before[0].get_text()==after[0].get_text()
        assert "Ways to Make Numbers to 8" in after[0].get_text()
        assert "This is one complete sentence." in " ".join(after[0].get_text().split())


def test_existing_adjacent_word_paragraph_tags_are_merged(tmp_path):
    src=str(tmp_path/"word-tags.pdf"); dst=str(tmp_path/"word-tags-fixed.pdf")
    with pikepdf.new() as pdf:
        page=pdf.add_blank_page(page_size=(300,400)); font=Dictionary(Type=Name.Font,Subtype=Name.Type1,BaseFont=Name.Helvetica,Encoding=Name.WinAnsiEncoding)
        page.Resources=Dictionary(Font=Dictionary({"/F1":font}))
        page.Contents=pdf.make_stream(b"/Span <</MCID 0>> BDC BT /F1 12 Tf 1 0 0 1 20 350 Tm (Make) Tj ET EMC\n/Span <</MCID 1>> BDC BT /F1 12 Tf 1 0 0 1 55 350 Tm (groups) Tj ET EMC")
        doc=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Document,K=Array()))
        st=pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot,K=Array([doc]))); doc.P=st
        for mid in range(2):
            p=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.P,P=doc,Pg=page.obj,K=mid)); doc.K.append(p)
        pdf.Root.StructTreeRoot=st; pdf.Root.MarkInfo=Dictionary(Marked=True); pdf.save(src)
    result=engine.fix(src,dst,do_ocr=False,sections="none")
    with pikepdf.open(dst) as pdf:
        ps=role_tree(pdf.Root.StructTreeRoot,"P")
        assert len(ps)==1 and list(ps[0].K)==[0,1]
        assert len(pdf.pages)==1
    assert any("word-level tags grouped" in a["action"] for a in result["actions"])
    with pymupdf.open(src) as before,pymupdf.open(dst) as after:
        assert before[0].get_text()==after[0].get_text()


def tagged_table_pdf(path):
    with pikepdf.new() as pdf:
        page=pdf.add_blank_page(page_size=(300,400)); font=Dictionary(Type=Name.Font,Subtype=Name.Type1,BaseFont=Name.Helvetica,Encoding=Name.WinAnsiEncoding)
        page.Resources=Dictionary(Font=Dictionary({"/F1":font}))
        page.Contents=pdf.make_stream(b"/Span <</MCID 0>> BDC BT /F1 12 Tf 20 350 Td (Name) Tj ET EMC\n/Span <</MCID 1>> BDC BT /F1 12 Tf 20 330 Td (Value) Tj ET EMC")
        doc=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Document,K=Array()))
        st=pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot,K=Array([doc]))); doc.P=st
        table=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Table,P=doc,Pg=page.obj,K=Array()))
        for mid in range(2):
            row=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.TR,P=table,Pg=page.obj,K=Array()))
            cell=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.TD,P=row,Pg=page.obj,K=mid)); row.K.append(cell); table.K.append(row)
        doc.K.append(table); pdf.Root.StructTreeRoot=st; pdf.Root.MarkInfo=Dictionary(Marked=True); pdf.save(path)


def test_existing_table_rows_are_wrapped_in_tbody_without_guessing_headers(tmp_path):
    src=str(tmp_path/"table.pdf"); dst=str(tmp_path/"table-fixed.pdf"); tagged_table_pdf(src)
    engine.fix(src,dst,do_ocr=False,sections="none")
    with pikepdf.open(dst) as pdf:
        bodies=role_tree(pdf.Root.StructTreeRoot,"TBody"); headers=role_tree(pdf.Root.StructTreeRoot,"TH")
        assert len(pdf.pages)==1 and len(bodies)==1 and len(bodies[0].K)==2
        assert headers==[]
    with pymupdf.open(src) as before,pymupdf.open(dst) as after:
        assert before[0].get_text()==after[0].get_text()


def test_widget_annotation_gets_a_form_structure_element(tmp_path):
    src=str(tmp_path/"widget.pdf"); dst=str(tmp_path/"widget-fixed.pdf")
    with pikepdf.new() as pdf:
        page=pdf.add_blank_page(page_size=(300,400))
        widget=pdf.make_indirect(Dictionary(Type=Name.Annot,Subtype=Name.Widget,Rect=Array([20,20,120,40]),FT=Name.Tx,T=pikepdf.String("Student name"),F=4,P=page.obj))
        page.Annots=Array([widget]); pdf.Root.AcroForm=Dictionary(Fields=Array([widget]),NeedAppearances=True)
        pdf.Root.MarkInfo=Dictionary(Marked=True); pdf.save(src)
    engine.fix(src,dst,do_ocr=False,sections="none")
    with pikepdf.open(dst) as pdf:
        forms=role_tree(pdf.Root.StructTreeRoot,"Form")
        assert len(forms)==1 and forms[0].K[0].get("/Type")==Name.OBJR
        assert len(pdf.pages)==1
    with pymupdf.open(src) as before,pymupdf.open(dst) as after:
        assert before[0].get_text()==after[0].get_text() and len(before)==len(after)
