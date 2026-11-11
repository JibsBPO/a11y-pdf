import uuid
import pikepdf
import pymupdf
from pikepdf import Array, Dictionary, Name
import app as appmod
import validate


def make_pdf(path):
    with pikepdf.new() as pdf:
        page=pdf.add_blank_page(page_size=(300,400))
        font=Dictionary(Type=Name.Font,Subtype=Name.Type1,BaseFont=Name.Helvetica,Encoding=Name.WinAnsiEncoding)
        page.Resources=Dictionary(Font=Dictionary({"/F1":font}))
        page.Contents=pdf.make_stream(b"/Span <</MCID 0>> BDC BT /F1 12 Tf 20 350 Td (Editor preserves this text) Tj ET EMC")
        doc=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Document,K=Array()))
        # Match real-world PDFs that store the sole Document as a scalar /K.
        st=pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot,K=doc)); doc.P=st
        sec=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Sect,P=doc,Pg=page.obj,K=Array()))
        p=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.P,P=sec,Pg=page.obj,K=Array([0])))
        span=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Span,P=sec,Pg=page.obj,K=Array()))
        sec.K=Array([p,span]); doc.K.append(sec)
        pdf.Root.StructTreeRoot=st; pdf.Root.MarkInfo=Dictionary(Marked=True); pdf.save(path)

def doc_elem(pdf):
    k=pdf.Root.StructTreeRoot.K
    return k[0] if isinstance(k,pikepdf.Array) else k

class FakeRequest:
    def __init__(self,body): self.body=body
    async def json(self): return self.body

def edit(sid,op):
    coro=appmod.tags_edit(sid,FakeRequest({"ops":[op]}))
    try: coro.send(None)
    except StopIteration as done: return done.value
    raise AssertionError("tag editor unexpectedly awaited external work")


def test_tag_editor_operations_preserve_page_text_and_structure(tmp_path,monkeypatch):
    monkeypatch.setattr(appmod,"T",str(tmp_path))
    sid=uuid.uuid4().hex[:12]; orig=appmod.path(sid,"orig"); fixed=appmod.path(sid,"fixed"); make_pdf(orig); make_pdf(fixed)
    appmod.review_save(sid,appmod.review.blank())
    nodes=appmod.tags_get(sid)["nodes"]; assert nodes[0]["type"]=="Document" and nodes[0]["children"][0]["type"]=="Sect"
    sect=nodes[0]["children"][0]; p=sect["children"][0]; span=sect["children"][1]
    for op in [
        {"op":"rename","path":p["id"],"type":"P"},
        {"op":"alt","path":p["id"],"value":"Paragraph label"},
        {"op":"lang","path":p["id"],"value":"en-US"},
        {"op":"rename","path":p["id"],"type":"P","custom":"WorkbenchParagraph"},
        {"op":"group","paths":[p["id"],span["id"]]},
    ]:
        edit(sid,op)
    grouped=appmod.tags_get(sid)["nodes"][0]["children"][0]["children"][0]
    edited_p=grouped["children"][0]
    assert edited_p["type"]=="WorkbenchParagraph" and edited_p["role"]=="P"
    updated=grouped
    assert updated["role"]=="Sect"
    assert updated["type"]=="Sect"
    edit(sid,{"op":"unwrap","path":updated["id"]})
    result=validate.run(fixed,original=orig,external=False)
    checks={x["id"]:x for x in result["checks"]}
    assert checks["struct.mcid"]["status"]=="passed" and checks["struct.parenttree"]["status"]=="passed"
    with pikepdf.open(orig) as a,pikepdf.open(fixed) as b:
        assert len(a.pages)==len(b.pages)==1
        p=doc_elem(b).K[0].K[0]
        assert p.Alt=="Paragraph label"
        assert str(p.Lang)=="en-US"
    with pymupdf.open(orig) as a,pymupdf.open(fixed) as b:
        assert a[0].get_text()==b[0].get_text() and "Editor preserves this text" in b[0].get_text()
    assert appmod.review_state(sid)["signoff"] is None


def test_tag_editor_refuses_delete_of_content_owner(tmp_path,monkeypatch):
    monkeypatch.setattr(appmod,"T",str(tmp_path)); sid=uuid.uuid4().hex[:12]
    make_pdf(appmod.path(sid,"orig")); make_pdf(appmod.path(sid,"fixed"))
    tree=appmod.tags_get(sid)["nodes"][0]["children"][0]["children"][0]
    from fastapi import HTTPException
    try: edit(sid,{"op":"delete","path":tree["id"]})
    except HTTPException as ex: assert ex.status_code==409 and "owns marked content" in ex.detail
    else: raise AssertionError("content-owning tag deletion was not refused")
