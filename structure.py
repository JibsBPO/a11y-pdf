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


def group_sections(pdf, mode="single"):
    """Group Document children around H1 sections without dropping tree nodes.

    Returns a result record for the repair log. Direct marked-content children
    are left untouched because moving them can change ownership semantics.
    """
    if mode not in ("single", "page", "heading", "none"):
        raise ValueError("sections must be 'single', 'page', 'heading', or 'none'")
    if mode == "none": return dict(changed=False, reason="section grouping disabled", count=0)
    st=pdf.Root.get("/StructTreeRoot")
    if st is None: return dict(changed=False, reason="no StructTreeRoot", count=0)
    rm=engine.rolemap(st)
    docs=[]
    engine.walk(st.get("/K"),rm,lambda el,d,pg: docs.append(el) if engine.role_of(el,rm)=="Document" else None)
    if not docs: return dict(changed=False, reason="no Document structure element", count=0)
    doc=docs[0]; kids=doc.get("/K"); original=list(kids) if isinstance(kids,pikepdf.Array) else ([kids] if kids is not None else [])
    if any(isinstance(x,int) or (isinstance(x,pikepdf.Dictionary) and x.get("/Type")==Name.MCR) for x in original):
        return dict(changed=False, reason="Document has direct marked-content children; grouping was skipped to preserve their ownership", count=0)
    pages=list(pdf.pages); page_index={p.obj.objgen:i for i,p in enumerate(pages)}

    def page_refs(node,inherited=None,out=None):
        if out is None: out=set()
        if isinstance(node,pikepdf.Array):
            for x in node: page_refs(x,inherited,out)
        elif isinstance(node,pikepdf.Dictionary):
            typ=node.get("/Type")
            pg=node.get("/Pg",inherited)
            if pg is not None and pg.objgen in page_index: out.add(page_index[pg.objgen])
            if typ not in (Name.MCR,Name.OBJR): page_refs(node.get("/K"),pg,out)
        elif isinstance(node,int) and inherited is not None and inherited.objgen in page_index:
            out.add(page_index[inherited.objgen])
        return out

    def is_elem(x):
        return isinstance(x,pikepdf.Dictionary) and "/S" in x and x.get("/Type") not in (Name.MCR,Name.OBJR)
    def make_section(children,index=None):
        sec=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Sect,P=doc,K=Array(children)))
        if index is not None and pages: sec.Pg=pages[index].obj
        for child in children:
            if is_elem(child): child.P=sec
        return sec

    if mode=="single":
        # Keep one document-wide Sect and unwrap additional Sect nodes without
        # dropping or reordering their descendants.
        def without_sections(node,parent):
            if not is_elem(node): return [node]
            if engine.role_of(node,rm)=="Sect":
                result=[]
                for child in (list(node.get("/K")) if isinstance(node.get("/K"),pikepdf.Array) else [node.get("/K")] if node.get("/K") is not None else []):
                    result.extend(without_sections(child,parent))
                return result
            kids=node.get("/K")
            vals=list(kids) if isinstance(kids,pikepdf.Array) else ([kids] if kids is not None else [])
            if vals:
                replaced=[]
                for child in vals: replaced.extend(without_sections(child,node))
                node.K=Array(replaced)
            if parent is not None: node.P=parent
            return [node]
        if len(original)==1 and is_elem(original[0]) and engine.role_of(original[0],rm)=="Sect":
            def count_sections(node):
                found=0
                if is_elem(node) and engine.role_of(node,rm)=="Sect": found+=1
                if is_elem(node):
                    for child in (list(node.get("/K")) if isinstance(node.get("/K"),pikepdf.Array) else [node.get("/K")] if node.get("/K") is not None else []): found+=count_sections(child)
                return found
            if count_sections(original[0])==1:
                return dict(changed=False,reason="already has one document-wide Sect",count=1)
        children=[]
        for child in original: children.extend(without_sections(child,None))
        if not children: return dict(changed=False,reason="Document has no structure children",count=0)
        doc.K=Array([make_section(children)])
        return dict(changed=True,reason="grouped the whole document under one Sect and unwrapped extra sections",count=1)

    if mode=="page":
        refs_per_child=[page_refs(child) for child in original]
        if any(len(refs)>1 for refs in refs_per_child) or any(not refs for refs in refs_per_child):
            return dict(changed=False,reason="some Document children do not map to exactly one page; grouping was skipped",count=0)
        # Idempotence: the Document already has one section whose descendant
        # page references belong to each page in order.
        if len(original)==len(pages) and all(is_elem(x) and engine.role_of(x,rm)=="Sect" for x in original):
            if all((page_refs(x)=={i} or (not page_refs(x) and i==0)) for i,x in enumerate(original)):
                return dict(changed=False,reason="already grouped one section per page",count=len(original))
        by_page=[[] for _ in pages]
        for child,refs in zip(original,refs_per_child):
            target=min(refs)
            if by_page: by_page[target].append(child)
        doc.K=Array([make_section(group,i) for i,group in enumerate(by_page)])
        return dict(changed=True,reason="grouped Document children by page",count=len(by_page))

    headings=[i for i,x in enumerate(original) if is_elem(x) and engine.role_of(x,rm)=="H1"]
    if not headings: return dict(changed=False,reason="no direct H1 children of Document",count=0)
    # The existing one-section-per-H1 form is already complete, including
    # sections with a single child.
    if len(original)==len(headings) and all(is_elem(x) and engine.role_of(x,rm)=="Sect" and
            sum(1 for y in (list(x.get("/K")) if isinstance(x.get("/K"),pikepdf.Array) else [x.get("/K")]) if is_elem(y) and engine.role_of(y,rm)=="H1")==1 for x in original):
        return dict(changed=False,reason="already grouped one section per H1",count=len(original))
    # The first H1 begins the first section. If there is a preamble, retain it
    # in the first section with that heading rather than inventing a section.
    starts=[0]+headings[1:]
    groups=[]
    for j,start in enumerate(starts):
        end=headings[j+1] if j+1<len(headings) else len(original)
        # If there is preamble content before the first H1, retain it in the
        # first section rather than dropping it or creating an extra section.
        groups.append(original[start:end])
    doc.K=Array([make_section(group) for group in groups])
    return dict(changed=True,reason="grouped Document children around H1 headings",count=len(groups))


def repair_list_hierarchy(pdf, render_doc=None):
    """Apply only clear SOP list fixes: provide LBody wrappers and nest trailing sublists.

    Does not invent labels, split list text, or infer where one list item ends.
    Returns counts so the caller can send changed or ambiguous cases to review.
    """
    st=pdf.Root.get("/StructTreeRoot")
    if st is None: return dict(repaired=0,needs_review=0)
    rm=engine.rolemap(st); items=[]
    engine.walk(st.get("/K"),rm,lambda el,d,pg: items.append((el,engine.role_of(el,rm),pg)))
    repaired=0; needs_review=0; reordered=0

    def values(el):
        k=el.get("/K")
        return list(k) if isinstance(k,pikepdf.Array) else ([k] if k is not None else [])
    def role(child):
        if isinstance(child,pikepdf.Dictionary) and child.get("/Type") not in (Name.MCR,Name.OBJR) and "/S" in child:
            return engine.role_of(child,rm)
        return None
    def set_parent(child,parent):
        if role(child) is not None: child.P=parent

    for li,r,_pg in items:
        if r!="LI": continue
        kids=values(li); bodies=[x for x in kids if role(x)=="LBody"]
        nested=[x for x in kids if role(x)=="L"]
        if not bodies:
            content=[x for x in kids if role(x)!="Lbl"]
            # A list-only child has no list-item text to wrap. Keep it intact
            # and report it for a person to supply/confirm the missing content.
            if content and any(role(x)!="L" for x in content):
                body=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.LBody,P=li,K=Array(content)))
                if "/Pg" in li: body.Pg=li.Pg
                for x in content: set_parent(x,body)
                li.K=Array([x for x in kids if role(x)=="Lbl"]+[body]); repaired+=1
                bodies=[body]; nested=[]
            elif nested:
                needs_review+=1
        if len(bodies)==1 and nested:
            # Only move a sublist if it follows the sole LBody in the list-item
            # order. Earlier/interleaved children are ambiguous and left alone.
            body=bodies[0]; body_pos=kids.index(body); nested_pos=[kids.index(x) for x in nested]
            other=[i for i,x in enumerate(kids) if i>body_pos and role(x) not in ("L","Lbl")]
            if all(i>body_pos for i in nested_pos) and not other:
                body_kids=values(body)
                body.K=Array(body_kids+nested)
                for child in nested: child.P=body
                li.K=Array([x for x in kids if role(x)!="L"]); repaired+=len(nested)
            else: needs_review+=1

    # Sort direct LI siblings only when every item has page-bound marked-content
    # geometry. This corrects an obvious tag-order mismatch without moving PDF
    # content or guessing the order of ambiguous items.
    pages=list(pdf.pages); pindex={p.obj.objgen:i for i,p in enumerate(pages)}; box_cache={}
    def references(node,inherited_pg,out):
        if isinstance(node,pikepdf.Array):
            for child in node: references(child,inherited_pg,out)
        elif isinstance(node,int):
            if inherited_pg is not None: out.append((inherited_pg.objgen,int(node)))
        elif isinstance(node,pikepdf.Dictionary):
            typ=node.get("/Type")
            if typ==Name.OBJR: return
            if typ==Name.MCR:
                if node.get("/Stm") is not None: return
                page=node.get("/Pg",inherited_pg)
                if page is not None: out.append((page.objgen,int(node.get("/MCID",-1))))
                return
            page=node.get("/Pg",inherited_pg); references(node.get("/K"),page,out)
    def item_box(item,inherited_pg):
        refs=[]; references(item,inherited_pg,refs)
        page_ids={key for key,_ in refs}
        if not refs or len(page_ids)!=1: return None
        key=refs[0][0]; pi=pindex.get(key)
        if pi is None: return None
        if pi not in box_cache: box_cache[pi]=engine.scan_page(pages[pi])[2]
        boxes=[box_cache[pi].get(mid) for _,mid in refs]
        if any(box is None for box in boxes): return None
        raw=[min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)]
        view=engine.to_view(render_doc[pi],raw) if render_doc is not None else [raw[0],-raw[3],raw[2],-raw[1]]
        return (pi,view)
    for lst,role_,pg in items:
        if role_!="L": continue
        lis=values(lst)
        if len(lis)<2 or any(role(x)!="LI" for x in lis): continue
        measured=[item_box(li,pg) for li in lis]
        if any(x is None for x in measured): continue
        ordered=sorted(zip(lis,measured,range(len(lis))),key=lambda x:(x[1][0],x[1][1][1],x[1][1][0],x[2]))
        if [x[2] for x in ordered]!=list(range(len(lis))):
            lst.K=Array([x[0] for x in ordered])
            for li in lis: li.P=lst
            reordered+=1

    return dict(repaired=repaired,needs_review=needs_review,reordered=reordered)


def repair_figure_bboxes(pdf):
    """Replace only existing malformed/misaligned Figure BBoxes with MCID geometry."""
    st=pdf.Root.get("/StructTreeRoot")
    if st is None: return dict(repaired=0,needs_review=0)
    rm=engine.rolemap(st); page_index={p.obj.objgen:i for i,p in enumerate(pdf.pages)}; cache={}; figures=[]
    engine.walk(st.get("/K"),rm,lambda el,d,pg: figures.append((el,pg)) if engine.role_of(el,rm)=="Figure" else None)
    repaired=0; needs_review=0
    def bbox_slot(el):
        if "/BBox" in el: return el,"/BBox"
        attrs=el.get("/A"); attrs=list(attrs) if isinstance(attrs,pikepdf.Array) else ([attrs] if attrs is not None else [])
        for attr in attrs:
            if isinstance(attr,pikepdf.Dictionary) and "/BBox" in attr: return attr,"/BBox"
        return None,None
    def refs(node,pg,out):
        if isinstance(node,pikepdf.Array):
            for child in node: refs(child,pg,out)
        elif isinstance(node,int):
            if pg is not None: out.append((pg.objgen,int(node)))
        elif isinstance(node,pikepdf.Dictionary):
            typ=node.get("/Type")
            if typ in (Name.MCR,Name.OBJR):
                if typ==Name.MCR and node.get("/Stm") is None:
                    page=node.get("/Pg",pg)
                    if page is not None: out.append((page.objgen,int(node.get("/MCID",-1))))
                return
            page=node.get("/Pg",pg); refs(node.get("/K"),page,out)
    for el,inherited_pg in figures:
        target,key=bbox_slot(el)
        if target is None: continue
        pg=el.get("/Pg",inherited_pg); refs_out=[]; refs(el.get("/K"),pg,refs_out)
        page_ids={p for p,_ in refs_out}
        if pg is None or len(page_ids)>1:
            needs_review+=1; continue
        pi=page_index.get(pg.objgen)
        if pi is None:
            needs_review+=1; continue
        if pi not in cache:
            try: cache[pi]=engine.scan_page(pdf.pages[pi])[2]
            except Exception: cache[pi]={}
        boxes=[cache[pi].get(mid) for _page,mid in refs_out]
        if not boxes or any(b is None for b in boxes):
            needs_review+=1; continue
        actual=[min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)]
        if actual[2]<=actual[0] or actual[3]<=actual[1]:
            needs_review+=1; continue
        try: old=[float(x) for x in target[key]]
        except Exception: old=[]
        if len(old)!=4 or old[2]<=old[0] or old[3]<=old[1] or max(abs(a-b) for a,b in zip(old,actual))>2.0:
            target[key]=Array([round(x,3) for x in actual]); repaired+=1
    return dict(repaired=repaired,needs_review=needs_review)


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
