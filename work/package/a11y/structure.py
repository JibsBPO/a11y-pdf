"""Edits to an existing structure tree that must keep the page content and the tree consistent."""
import collections
import pikepdf
from pikepdf import Name, Dictionary, Array
import engine

CSI = pikepdf.ContentStreamInstruction


def rebuild_parent_tree(pdf):
    """Recreate /ParentTree (and the /StructParents keys of pages and link/widget annotations) from the structure tree."""
    st = pdf.Root.StructTreeRoot; rm = engine.rolemap(st); npg = len(pdf.pages)
    nums = {}; pidx = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}; per = collections.defaultdict(dict); nxt = [npg]

    def reg(el, d, pg):
        for m in engine.mcids_of(el):
            if pg is not None and pg.objgen in pidx: per[pidx[pg.objgen]][m] = el
        ks = el.get("/K")
        for x in (list(ks) if isinstance(ks, pikepdf.Array) else [ks] if ks is not None else []):
            if isinstance(x, pikepdf.Dictionary) and x.get("/Type") == Name.OBJR:
                o = x.Obj; o.StructParent = nxt[0]; nums[nxt[0]] = el; nxt[0] += 1
    engine.walk(st.get("/K"), rm, reg)
    null = pikepdf.Object.parse(b"null")
    for i, page in enumerate(pdf.pages):
        content=set()
        for c in pikepdf.parse_content_stream(page):
            if isinstance(c,pikepdf.ContentStreamInlineImage) or str(c.operator)!="BDC": continue
            a=c.operands
            if len(a)>1 and isinstance(a[1],Dictionary) and "/MCID" in a[1]: content.add(int(a[1]["/MCID"]))
        linked=set(per[i]); missing=sorted(content-linked); dangling=sorted(linked-content)
        if missing or dangling:
            raise ValueError(f"Cannot rebuild ParentTree for page {i+1}: unowned MCIDs {missing[:5]}, dangling references {dangling[:5]}")
        page.obj.StructParents = i; mx = max(per[i]) + 1 if per[i] else 0
        nums[i] = Array([per[i].get(m) if per[i].get(m) is not None else null for m in range(mx)])
    flat = Array()
    for kk in sorted(nums): flat.append(kk); flat.append(nums[kk])
    st.ParentTree = pdf.make_indirect(Dictionary(Nums=flat)); st.ParentTreeNextKey = nxt[0]


def figure_to_artifact(pdf, el, page_index):
    """Turn a Figure that is purely decorative into an Artifact: its marked content becomes /Artifact BMC, the Figure element
    leaves the structure tree, and the ParentTree is rebuilt. Raises LookupError (nothing changed) if the content cannot be found."""
    mc = set(engine.mcids_of(el))
    if not mc: raise LookupError("This figure has no marked content of its own")
    page = pdf.pages[page_index]; out = []; found = set()
    for c in pikepdf.parse_content_stream(page):
        if not isinstance(c, pikepdf.ContentStreamInlineImage) and str(c.operator) == "BDC" and len(c.operands) > 1 \
                and isinstance(c.operands[1], Dictionary) and "/MCID" in c.operands[1] and int(c.operands[1]["/MCID"]) in mc:
            found.add(int(c.operands[1]["/MCID"])); out.append(CSI([Name.Artifact], pikepdf.Operator("BMC"))); continue
        out.append(c)
    if found != mc: raise LookupError("The figure's content was not found in the page content (it may be inside a Form XObject)")
    parent = el.get("/P")
    if parent is None: raise LookupError("The figure has no parent element")
    k = parent.get("/K")
    if isinstance(k, pikepdf.Array):
        keep = [x for x in k if not (isinstance(x, pikepdf.Dictionary) and x.objgen == el.objgen)]
        if len(keep) == len(k): raise LookupError("The figure is not listed in its parent")
        parent.K = Array(keep)
    elif isinstance(k, pikepdf.Dictionary) and k.objgen == el.objgen: parent.K = Array()
    else: raise LookupError("The figure is not listed in its parent")
    page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out))
    rebuild_parent_tree(pdf)
