import os, re, io, math, json, base64, urllib.request, collections
import pikepdf, pymupdf
from pikepdf import Name, Dictionary, Array, Operator, ContentStreamInstruction as CSI
from rules import RULES
import fonts, forms, validate

STD = set("Document Part Art Sect Div BlockQuote Caption TOC TOCI Index NonStruct Private P H H1 H2 H3 H4 H5 H6 L LI Lbl LBody Table TR TH TD THead TBody TFoot Span Quote Note Reference BibEntry Code Link Annot Ruby Warichu RB RT RP WT WP Figure Formula Form StructTreeRoot".split())
GUESS = {"heading1":"H1","heading2":"H2","heading3":"H3","heading4":"H4","heading5":"H5","heading6":"H6","title":"H1","paragraph":"P","para":"P","body":"P","bodytext":"P","text":"P","image":"Figure","picture":"Figure","graphic":"Figure","figure":"Figure","list":"L","listitem":"LI","listbody":"LBody","label":"Lbl","tablecell":"TD","tableheader":"TH","tablerow":"TR","table":"Table","link":"Link","section":"Sect","sect":"Sect","article":"Art","caption":"Caption","footnote":"Note","span":"Span","div":"Div"}
CONSTRUCT = {"m","l","c","v","y","re","h"}; PAINT = {"S","s","f","F","f*","B","B*","b","b*","n"}

# ---------------------------------------------------------------- geometry
def mul(a, b): return (a[0]*b[0]+a[1]*b[2], a[0]*b[1]+a[1]*b[3], a[2]*b[0]+a[3]*b[2], a[2]*b[1]+a[3]*b[3], a[4]*b[0]+a[5]*b[2]+b[4], a[4]*b[1]+a[5]*b[3]+b[5])
def apply(m, x, y): return (m[0]*x+m[2]*y+m[4], m[1]*x+m[3]*y+m[5])
def union(b, pts):
    xs=[p[0] for p in pts]+([b[0],b[2]] if b else []); ys=[p[1] for p in pts]+([b[1],b[3]] if b else [])
    return [min(xs),min(ys),max(xs),max(ys)]
def to_view(pg, bbox):
    """PDF user-space bbox -> top-left origin points in the rendered (rotated) page."""
    r = pymupdf.Rect(*bbox).normalize() * pg.transformation_matrix * pg.rotation_matrix
    r.normalize(); return [round(v,1) for v in (r.x0,r.y0,r.x1,r.y1)]

def scan_page(page, mu_page=None):
    """Mini content-stream interpreter. Returns (instructions, units, mcid_boxes)."""
    ins = list(pikepdf.parse_content_stream(page)); units=[]; mcb={}
    res = page.obj.get("/Resources", Dictionary()); fonts = res.get("/Font", Dictionary()); xo = res.get("/XObject", Dictionary())
    ctm=(1,0,0,1,0,0); st=[]; tm=tlm=(1,0,0,1,0,0); fs=12; lead=0; wide=False
    mc=[]  # stack of (tag, mcid)
    cur=None; path=None
    def ctx():
        art=any(t=="/Artifact" for t,_ in mc); mid=[m for _,m in mc if m is not None]
        return art, (mid[-1] if mid else None)
    for i, c in enumerate(ins):
        op = str(c.operator) if not isinstance(c, pikepdf.ContentStreamInlineImage) else "BI"; a = c.operands if op!="BI" else []
        try:
            if op=="q": st.append(ctm)
            elif op=="Q": ctm = st.pop() if st else ctm
            elif op=="cm": ctm = mul(tuple(float(x) for x in a), ctm)
            elif op in("BDC","BMC"):
                mid=None
                if op=="BDC" and isinstance(a[1], pikepdf.Dictionary) and "/MCID" in a[1]: mid=int(a[1]["/MCID"])
                mc.append((str(a[0]), mid))
            elif op=="EMC":
                if mc: mc.pop()
            elif op=="BT": tm=tlm=(1,0,0,1,0,0); cur=dict(kind="text", i0=i, bbox=None, size=0, n=0, ctx=ctx())
            elif op=="Tf":
                fs=float(a[1]); f=fonts.get(str(a[0])); wide = bool(f is not None and str(f.get("/Subtype"))=="/Type0")
            elif op=="Tm": tm=tlm=tuple(float(x) for x in a)
            elif op in("Td","TD"):
                tx,ty=float(a[0]),float(a[1]); tlm=mul((1,0,0,1,tx,ty),tlm); tm=tlm
                if op=="TD": lead=-ty
            elif op=="TL": lead=float(a[0])
            elif op in("T*","'",'"'):
                tlm=mul((1,0,0,1,0,-lead),tlm); tm=tlm
            if op in("Tj","TJ","'",'"') and cur:
                s=a[-1]; strs=[s] if op!="TJ" else [x for x in s if isinstance(x,pikepdf.String)]
                nb=sum(len(bytes(x)) for x in strs); w=nb*(0.5 if not wide else 0.25)*fs
                m=mul(tm,ctm); x0,y0=apply(m,0,-0.2*fs/1) if False else apply(m,0,-0.2*fs)
                sz=abs(fs*math.hypot(m[2],m[3])) or fs; ww=w*math.hypot(m[0],m[1])
                p=[apply(m,0,-0.2*fs), apply(m,w,0.8*fs)]
                cur["bbox"]=union(cur["bbox"],p); cur["size"]=max(cur["size"],round(sz,1)); cur["n"]+=nb
                tm=mul((1,0,0,1,w,0),tm)
            elif op=="ET" and cur:
                cur["i1"]=i
                if cur["n"]: units.append(cur)
                cur=None
            elif op=="Do":
                x=xo.get(str(a[0]));
                if x is not None:
                    pts=[apply(ctm,0,0),apply(ctm,1,1),apply(ctm,1,0),apply(ctm,0,1)]
                    units.append(dict(kind="image" if str(x.get("/Subtype"))=="/Image" else "form", i0=i,i1=i,bbox=union(None,pts),size=0,ctx=ctx(),xobj=x))
            elif op=="BI":
                pts=[apply(ctm,0,0),apply(ctm,1,1)]; units.append(dict(kind="image",i0=i,i1=i,bbox=union(None,pts),size=0,ctx=ctx(),xobj=None))
            elif op=="sh": units.append(dict(kind="path",i0=i,i1=i,bbox=None,size=0,ctx=ctx()))
            elif op in CONSTRUCT:
                if path is None: path=dict(kind="path",i0=i,bbox=None,size=0,ctx=ctx())
                nums=[float(x) for x in a]; pts=[apply(ctm,nums[k],nums[k+1]) for k in range(0,len(nums)-1,2)]
                if op=="re": pts=[apply(ctm,nums[0],nums[1]),apply(ctm,nums[0]+nums[2],nums[1]+nums[3])]
                path["bbox"]=union(path["bbox"],pts)
            elif op in PAINT and path:
                path["i1"]=i
                if op!="n" or True: units.append(path)
                path=None
        except Exception: pass
        if op in("EMC",) : pass
    for u in units:
        art, mid = u["ctx"]; u["art"]=art; u["mcid"]=mid; u["tagged"]=art or mid is not None
        if mid is not None and u["bbox"]:
            mcb[mid]=union(mcb.get(mid),[(u["bbox"][0],u["bbox"][1]),(u["bbox"][2],u["bbox"][3])])
    return ins, units, mcb

# ---------------------------------------------------------------- struct tree helpers
def role_of(el, rm):
    s=str(el.get("/S","")).lstrip("/"); seen=set()
    while s in rm and s not in seen: seen.add(s); s=str(rm[s]).lstrip("/")
    return s
def walk(node, rm, fn, depth=0, pg=None):
    if isinstance(node, pikepdf.Array):
        for k in node: walk(k, rm, fn, depth, pg)
    elif isinstance(node, pikepdf.Dictionary):
        if node.get("/Type")==Name.MCR or node.get("/Type")==Name.OBJR: return
        pg=node.get("/Pg", pg); fn(node, depth, pg); walk(node.get("/K"), rm, fn, depth+1, pg)
def rolemap(root): return {str(k).lstrip("/"):v for k,v in root.get("/RoleMap",{}).items()}
def mcids_of(el):
    k=el.get("/K"); out=[]
    ks = list(k) if isinstance(k, pikepdf.Array) else ([k] if k is not None else [])
    for x in ks:
        if isinstance(x,int) or (isinstance(x,pikepdf.Object) and x._type_code==pikepdf.ObjectType.integer): out.append(int(x))
        elif isinstance(x,pikepdf.Dictionary) and x.get("/Type")==Name.MCR and "/Stm" not in x: out.append(int(x["/MCID"]))
    return out

def font_embedded(f):
    try:
        if str(f.get("/Subtype"))=="/Type3": return True
        if str(f.get("/Subtype"))=="/Type0": f=f["/DescendantFonts"][0]
        d=f.get("/FontDescriptor"); return d is not None and any(k in d for k in("/FontFile","/FontFile2","/FontFile3"))
    except Exception: return True

# ---------------------------------------------------------------- analysis
def analyze(src):
    pdf=pikepdf.open(src); doc=pymupdf.open(src); issues=[]; root=pdf.Root
    def add(rule,page=None,bbox=None,msg=None,**kw):
        r=RULES[rule]; issues.append(dict(id=len(issues),rule=rule,sev=r["sev"],page=page,bbox=bbox,msg=msg or r["title"],auto=r["auto"],wcag=r["wcag"],mh=r["mh"],**kw))
    mk=root.get("/MarkInfo")
    if not(mk is not None and bool(mk.get("/Marked",False))): add("MARKED")
    has_tree="/StructTreeRoot" in root
    if not has_tree: add("NO_TAGS")
    if not str(root.get("/Lang","")).strip(): add("LANG")
    vp=root.get("/ViewerPreferences")
    if not(vp is not None and bool(vp.get("/DisplayDocTitle",False))): add("DISPLAY_TITLE")
    xmp=bytes(root.Metadata.read_bytes()).decode("utf8","ignore") if "/Metadata" in root else ""
    if not re.search(r"<dc:title>.*?<rdf:li[^>]*>\s*\S",xmp,re.S) and not str(pdf.docinfo.get("/Title","")).strip(): add("TITLE")
    if "pdfuaid:part" not in xmp: add("XMP_UA")
    if len(pdf.pages)>=20 and not doc.get_toc(): add("BOOKMARKS")
    rm=rolemap(root.StructTreeRoot) if has_tree else {}
    seen_fonts=set(); heads=[]; elems=[]
    pg_index={p.obj.objgen:i for i,p in enumerate(pdf.pages)}
    boxes={}; sizes=[]
    for i,page in enumerate(pdf.pages):
        mu=doc[i]; ins,units,mcb=scan_page(page); boxes[i]=mcb; sizes.append([round(mu.rect.width,1),round(mu.rect.height,1)])   # sizes let the UI reserve space for every page without rendering it
        if str(page.obj.get("/Tabs",""))!="/S": add("TABS",i+1)
        res=page.obj.get("/Resources",{}); 
        for n,f in (res.get("/Font",{}) or {}).items():
            if f.objgen not in seen_fonts:
                seen_fonts.add(f.objgen)
                if not font_embedded(f): add("FONT_EMBED",i+1,msg=f"Font {str(f.get('/BaseFont','?'))} is not embedded")
        words=len(mu.get_text("words")); imgs=[u for u in units if u["kind"]=="image" and u["bbox"]]
        big=[u for u in imgs if (u["bbox"][2]-u["bbox"][0])*(u["bbox"][3]-u["bbox"][1])>0.6*mu.rect.width*mu.rect.height]
        if words<3 and big: add("SCANNED",i+1,to_view(mu,big[0]["bbox"]),"Scanned page: image only, no extractable text")
        n=0
        for u in units:
            if not u["tagged"] and u["bbox"] and n<40:
                n+=1; add("CONTENT_UNTAGGED",i+1,to_view(mu,u["bbox"]),f"Untagged {u['kind']} content")
        for a in page.obj.get("/Annots",[]) or []:
            st=str(a.get("/Subtype")); 
            if "/Rect" not in a: continue
            bb=to_view(mu,[float(x) for x in a.Rect])
            if st=="/Link" and not str(a.get("/Contents","")).strip(): add("LINK_ALT",i+1,bb)
            if st in("/Link","/Widget") and "/StructParent" not in a: add("ANNOT_UNTAGGED",i+1,bb,f"{st[1:]} annotation not tagged")
            if st=="/Widget" and not str(a.get("/TU","")).strip(): add("FORM_TU",i+1,bb)
    if has_tree:
        def visit(el,depth,pg):
            role=role_of(el,rm); s=str(el.get("/S","")).lstrip("/")
            pi=pg_index.get(pg.objgen) if pg is not None else None
            bb=None
            if pi is not None:
                m=[boxes[pi][x] for x in mcids_of(el) if x in boxes[pi]]
                if m: bb=to_view(doc[pi],[min(b[0] for b in m),min(b[1] for b in m),max(b[2] for b in m),max(b[3] for b in m)])
            elems.append((el,role,pi,bb))
            if role not in STD: add("ROLEMAP",pi+1 if pi is not None else None,bb,f"Structure type /{s} resolves to non-standard '{role}'")
            if role=="Figure":
                alt=str(el.get("/Alt","")).strip() or str(el.get("/ActualText","")).strip()
                if not alt: add("FIG_ALT",pi+1 if pi is not None else None,bb)
                elif re.fullmatch(r"(?i)(image|figure|picture|graphic|img[_\-\d.]*\w*|.{0,1}|[\w\-]+\.(png|jpe?g|gif|tiff?))",alt): add("FIG_ALT_BAD",pi+1 if pi is not None else None,bb,f"Alt text '{alt}' is not descriptive")
            if role=="TH":
                at=str(el.get("/A",""))
                if "Scope" not in at: add("TH_SCOPE",pi+1 if pi is not None else None,bb)
            if re.fullmatch(r"H[1-6]?",role): heads.append((el,role,pi,bb))
            if role=="Table":
                found=[]; walk(el.get("/K"),rm,lambda e,d,p:found.append(role_of(e,rm)))
                if "TH" not in found: add("TABLE_NOTH",pi+1 if pi is not None else None,bb)
        walk(root.StructTreeRoot.get("/K"),rm,visit)
        prev=0
        for k,(el,role,pi,bb) in enumerate(heads):
            lvl=int(role[1]) if len(role)>1 else None
            if k==0 and lvl!=1: add("HEADING_FIRST",pi+1 if pi is not None else None,bb)
            elif lvl and lvl>prev+1: add("HEADING_SKIP",pi+1 if pi is not None else None,bb,f"Heading jumps from H{prev} to H{lvl}")
            prev=lvl or prev
    pdf.close(); doc.close()
    return dict(issues=issues,pages=len(sizes),page_sizes=sizes,summary=summarize(issues))
def summarize(issues):
    c=collections.Counter(i["sev"] for i in issues); r=collections.Counter(i["rule"] for i in issues)
    return dict(total=len(issues),by_sev=dict(c),by_rule=dict(r))

# ---------------------------------------------------------------- helpers: language, title, alt text
STOP={"en":"the and of to in is that it for with as was on are this be by".split(),"es":"el la de que y en los se del las por un para con una".split(),"fr":"le la les de des et en un une que pour dans est du".split(),"de":"der die und in den von zu das mit sich des auf ist".split(),"pt":"o a de que e do da em um para com uma os".split(),"it":"il la di che e un per in con non una del le".split()}
def detect_lang(text,default="en"):
    w=re.findall(r"[a-zA-Zà-ÿ]+",text.lower()); 
    if len(w)<8: return default
    sc={l:sum(x in s for x in w) for l,s in ((l,set(v)) for l,v in STOP.items())}; best=max(sc,key=sc.get)
    return best if sc[best]>=0.06*len(w) else default
def describe_image(png, lang="en"):
    key=os.environ.get("ANTHROPIC_API_KEY")
    if not key: return None
    body=json.dumps(dict(model=os.environ.get("ALT_MODEL","claude-haiku-5-5"),max_tokens=200,messages=[dict(role="user",content=[dict(type="image",source=dict(type="base64",media_type="image/png",data=base64.b64encode(png).decode())),dict(type="text",text=f"Write concise alt text (max 25 words, language: {lang}) for this image from a document, for a screen-reader user. Describe meaning, not appearance fluff. If it contains text, include it. Reply with the alt text only; reply DECORATIVE if it is purely decorative.")])])).encode()
    try:
        r=urllib.request.Request("https://api.anthropic.com/v1/messages",body,{"x-api-key":key,"anthropic-version":"2023-06-01","content-type":"application/json"})
        return json.load(urllib.request.urlopen(r,timeout=40))["content"][0]["text"].strip()
    except Exception: return None
def alt_for(mu, bbox_view, lang, log, acts=None, page=None):
    """Alt text for the figure at bbox_view. AI/OCR-produced text is always logged as needing human review."""
    r=pymupdf.Rect(*bbox_view)&mu.rect
    if r.is_empty or r.width<4: return None
    png=mu.get_pixmap(clip=r,dpi=110).tobytes("png"); a=describe_image(png,lang)
    if a:
        if acts is not None and not a.upper().startswith("DECORATIVE"): acts.append(dict(action="Alt text written by AI",status="needs_review",page=page,detail=a[:140]))
        return a
    try:
        import pytesseract; from PIL import Image
        t=" ".join(pytesseract.image_to_string(Image.open(io.BytesIO(png))).split())
        if len(t)>3:
            log.append("alt from OCR text (review)")
            if acts is not None: acts.append(dict(action="Alt text taken from OCR of the image",status="needs_review",page=page,detail=t[:140]))
            return t[:200]
    except Exception: pass
    return None

# ---------------------------------------------------------------- OCR text layer
def find_font():
    win=os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
    extra=os.environ.get("FONT_DIR", "")
    for p in (os.path.join(extra,"DejaVuSans.ttf"),os.path.join(extra,"LiberationSans-Regular.ttf"),
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf","/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
              os.path.join(win,"arial.ttf"),os.path.join(win,"calibri.ttf")):
        if os.path.exists(p): return p
def ocr_pages(doc, pages, lang="eng", log=None, acts=None):
    import pytesseract; from PIL import Image
    ff=find_font()
    for pn in pages:
        pg=doc[pn]; pix=pg.get_pixmap(dpi=300); img=Image.open(io.BytesIO(pix.tobytes("png")))
        d=pytesseract.image_to_data(img,lang=lang,output_type=pytesseract.Output.DICT); s=72/300; lines=collections.OrderedDict()
        for k in range(len(d["text"])):
            t=d["text"][k].strip()
            if t and float(d["conf"][k])>30: lines.setdefault((d["block_num"][k],d["par_num"][k],d["line_num"][k]),[]).append(k)
        for ks in lines.values():
            x0=min(d["left"][k] for k in ks)*s; x1=max(d["left"][k]+d["width"][k] for k in ks)*s
            y0=min(d["top"][k] for k in ks)*s; y1=max(d["top"][k]+d["height"][k] for k in ks)*s
            txt=" ".join(d["text"][k].strip() for k in ks); h=y1-y0; size=max(4,h*0.85)
            kw=dict(fontfile=ff,fontname="ocrf") if ff else dict(fontname="helv")
            pg.insert_text(pymupdf.Point(x0,y1-h*0.2),txt,fontsize=size,render_mode=3,**kw)
        if log is not None: log.append(f"OCR page {pn+1}: {len(lines)} lines")
        if acts is not None: acts.append(dict(action="OCR text layer created",status="needs_review",page=pn+1,detail=f"{len(lines)} lines recognised: compare with the scan"))

# ---------------------------------------------------------------- shared helpers used by fix() and by human-review edits (review.py)
def _ref(acts, n0, page, mcid, bbox):
    """Tag the actions added since index n0 with where the figure is, so a human reviewer can see it and edit its alt text."""
    if mcid is None: return
    for a in acts[n0:]: a["ref"] = dict(kind="alt", page=page, mcid=int(mcid), bbox=bbox)

def xmp_packet(ttl, lng):
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (f'<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            f'<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:pdfuaid="http://www.aiim.org/pdfua/ns/id/">'
            f'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">{esc(ttl)}</rdf:li></rdf:Alt></dc:title><dc:language><rdf:Bag><rdf:li>{esc(lng)}</rdf:li></rdf:Bag></dc:language>'
            f'<pdfuaid:part>1</pdfuaid:part></rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>')

# ---------------------------------------------------------------- the fixer
def fix(src, dst, lang=None, do_ocr=True, ocr_lang="eng", title=None):
    log=[]; acts=[]; tmp=dst+".stage.pdf"; doc=pymupdf.open(src)
    if doc.is_pdf is False or len(doc)==0: doc=pymupdf.open("pdf",doc.convert_to_pdf())
    scanned=[i for i,p in enumerate(doc) if len(p.get_text("words"))<3 and p.get_images()]
    if scanned and do_ocr:
        try: ocr_pages(doc,scanned,ocr_lang,log,acts)
        except Exception as e:
            log.append(f"OCR unavailable: {e}"); acts.append(dict(action="OCR of scanned pages",status="failed",page=None,detail=f"OCR unavailable: {type(e).__name__}. Install tesseract-ocr"))
    doc.save(tmp, garbage=0); doc.close()
    pdf=pikepdf.open(tmp); mu=pymupdf.open(tmp); root=pdf.Root; npg=len(pdf.pages)
    fonts.embed_missing(pdf,log,acts)
    forms.flatten_all(pdf,acts,log)      # Form XObjects with text/images are merged into the page so they can be tagged (not hidden as one Artifact)
    _w={}
    def tbox(i,rect):                   # words inside rect, in reading order. Words are extracted once per page: PyMuPDF's get_textbox()
        if i not in _w: _w.clear(); _w[i]=mu[i].get_text("words")   # walks every character on the page per call (O(blocks x chars))
        out=[]
        for w in _w[i]:
            ix=min(w[2],rect.x1)-max(w[0],rect.x0); iy=min(w[3],rect.y1)-max(w[1],rect.y0)
            if ix>0 and iy>0 and ix*iy>=0.4*(w[2]-w[0])*(w[3]-w[1]): out.append(w[4])
        return " ".join(out)
    alltext=" ".join(p.get_text()[:3000] for p in mu[:5])
    lng=lang or str(root.get("/Lang","")).strip()[:2] or detect_lang(alltext)
    if not lang and not str(root.get("/Lang","")).strip(): acts.append(dict(action="Document language set",status="needs_review",page=None,detail=f"Detected automatically as '{lng}' from the text: confirm",ref=dict(kind="lang",value=lng)))
    # structure root
    if "/StructTreeRoot" not in root:
        doc_el=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Document,K=Array()))
        st=pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot,K=Array([doc_el]))); doc_el.P=st; root.StructTreeRoot=st; log.append("created StructTreeRoot/Document")
    st=root.StructTreeRoot; rm=rolemap(st)
    k=st.get("/K"); 
    if not isinstance(k,pikepdf.Array): st.K=Array([k] if k is not None else [])
    top=next((e for e in st.K if isinstance(e,pikepdf.Dictionary) and str(e.get("/S"))=="/Document"),None)
    if top is None:
        top=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Document,P=st,K=Array(list(st.K)))); 
        for e in top.K:
            if isinstance(e,pikepdf.Dictionary): e.P=top
        st.K=Array([top])
    if not isinstance(top.get("/K"),pikepdf.Array): top.K=Array([top.K] if "/K" in top else [])
    # Existing MCIDs are scoped to a page content stream. Include both the
    # structure tree and the stream: malformed PDFs can contain orphan MCIDs.
    used=collections.defaultdict(int)
    content_mcids=collections.defaultdict(collections.Counter)
    page_numbers={p.obj.objgen:i+1 for i,p in enumerate(pdf.pages)}
    for page in pdf.pages:
        try:
            for c in pikepdf.parse_content_stream(page):
                if isinstance(c,pikepdf.ContentStreamInlineImage) or str(c.operator)!="BDC": continue
                a=c.operands
                if len(a)>1 and isinstance(a[1],pikepdf.Dictionary) and "/MCID" in a[1]:
                    content_mcids[page.obj.objgen][int(a[1]["/MCID"])]+=1
        except Exception as e:
            acts.append(dict(action="Content MCIDs could not be scanned",status="failed",page=None,detail=f"{type(e).__name__}: {e}"))
    def sc(el,d,pg):
        if pg is not None:
            for m in mcids_of(el): used[pg.objgen]=max(used[pg.objgen],m+1)
    walk(top.K,rm,sc)
    # Duplicate MCIDs in a page stream cannot be assigned unambiguously to
    # structure elements. Keep the first occurrence (and its existing owner),
    # give later occurrences fresh IDs, and add a matching Span owner.
    duplicate_count=0
    for page in pdf.pages:
        scope=page.obj.objgen; ins=list(pikepdf.parse_content_stream(page)); seen=set(); out=[]; page_changed=False
        fresh=max(used[scope],max(content_mcids[scope],default=-1)+1)
        for c in ins:
            if not isinstance(c,pikepdf.ContentStreamInlineImage) and str(c.operator)=="BDC":
                a=list(c.operands)
                if len(a)>1 and isinstance(a[1],pikepdf.Dictionary) and "/MCID" in a[1]:
                    mid=int(a[1]["/MCID"])
                    if mid in seen:
                        props=Dictionary({k:v for k,v in a[1].items()}); props.MCID=fresh
                        out.append(CSI([a[0],props],Operator("BDC")))
                        el=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Span,P=top,Pg=page.obj,K=fresh)); top.K.append(el)
                        fresh+=1; duplicate_count+=1; page_changed=True; continue
                    seen.add(mid)
            out.append(c)
        if page_changed: page.obj.Contents=pdf.make_stream(pikepdf.unparse_content_stream(out))
    if duplicate_count:
        acts.append(dict(action="Duplicate content MCIDs assigned unique IDs",status="needs_review",page=None,detail=f"{duplicate_count} repeated marked-content occurrence(s) received unique IDs; confirm their reading order"))
        for page in pdf.pages:
            scope=page.obj.objgen; content_mcids[scope].clear()
            for c in pikepdf.parse_content_stream(page):
                if not isinstance(c,pikepdf.ContentStreamInlineImage) and str(c.operator)=="BDC" and len(c.operands)>1 and isinstance(c.operands[1],pikepdf.Dictionary) and "/MCID" in c.operands[1]: content_mcids[scope][int(c.operands[1]["/MCID"])]+=1
    # Give orphan marked content a real owner. This preserves its existing
    # tag and MCID while making the content reachable to assistive technology.
    owners=collections.defaultdict(set)
    def own(el,d,pg):
        if pg is not None:
            for m in mcids_of(el): owners[pg.objgen,m].add(el.objgen)
    walk(top.K,rm,own)
    orphan_count=0
    for page in pdf.pages:
        scope=page.obj.objgen
        for mid,count in content_mcids[scope].items():
            used[scope]=max(used[scope],mid+1)
            if count==1 and not owners[scope,mid]:
                el=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Span,P=top,Pg=page.obj,K=mid))
                top.K.append(el); owners[scope,mid].add(el.objgen); orphan_count+=1
            elif len(owners[scope,mid])>1:
                acts.append(dict(action="Multiply claimed MCID found",status="failed",page=page_numbers[scope],detail=f"MCID {mid} is referenced by {len(owners[scope,mid])} structure elements"))
            elif count>1:
                acts.append(dict(action="Duplicate content MCID found",status="failed",page=page_numbers[scope],detail=f"MCID {mid} occurs {count} times in the page content stream"))
    if orphan_count:
        log.append(f"reconciled {orphan_count} orphan MCID(s) with Span structure elements")
        acts.append(dict(action="Orphan MCIDs linked to structure",status="succeeded",page=None,detail=f"{orphan_count} existing marked-content reference(s) linked"))
    # pass 1: per-page tagging of untagged content, collect size stats
    pageplan=[]; sizes=collections.Counter()
    for i,page in enumerate(pdf.pages):
        ins,units,_=scan_page(page); un=[u for u in units if not u["tagged"]]
        for u in un:
            if u["kind"]=="text": sizes[round(u["size"])]+=u["n"]
        pageplan.append((ins,un))
    body=sizes.most_common(1)[0][0] if sizes else 12
    tiers=sorted({s for s in sizes if s>=body*1.2},reverse=True)[:3]
    has_h=any(re.fullmatch(r"H[1-6]?",role_of(e,rm)) for e in []) 
    created=0; first_title=None; inferH=[0]
    for i,page in enumerate(pdf.pages):
        ins,un=pageplan[i]; page.obj["/Tabs"]=Name.S
        if not un: continue
        mpage=mu[i]; mcid=used[page.obj.objgen]; wraps=[]; last=None; lastbox=None
        for u in sorted(un,key=lambda u:u["i0"]):
            vb=to_view(mpage,u["bbox"]) if u["bbox"] else None
            # Image size alone does not establish that an image is decorative.
            # Keep images in the Figure/review flow; only non-semantic drawing
            # paths and graphic-only Forms are artifacts by default.
            if u["kind"] in("path","form"):
                wraps.append((u["i0"],u["i1"],"Artifact",None)); last=None; continue
            if u["kind"]=="image":
                n0=len(acts); alt=alt_for(mpage,vb,lng,log,acts,i+1)
                if not alt: acts.append(dict(action="Placeholder alt text inserted",status="needs_review",page=i+1,detail="No description could be generated: write one by hand")); alt="Image (alt text needs review)"
                if alt.upper().startswith("DECORATIVE"): wraps.append((u["i0"],u["i1"],"Artifact",None)); continue
                el=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Figure,P=top,Pg=page.obj,K=mcid,Alt=pikepdf.String(alt)))
                top.K.append(el); _ref(acts,n0,i+1,mcid,vb); wraps.append((u["i0"],u["i1"],"MCID",mcid)); mcid+=1; created+=1; last=None; continue
            sz=round(u["size"]); role="P"
            txt=" ".join(tbox(i,pymupdf.Rect(*vb)+(-1,-1,1,1)).split()) if vb else ""
            if sz in tiers and len(txt)<=150: role=f"H{tiers.index(sz)+1}"; inferH[0]+=1
            if role=="P" and last is not None and last[0]=="P" and vb and lastbox and vb[1]-lastbox[3]<u["size"]*1.2:
                last[1].K.append(mcid); lastbox=vb
            else:
                el=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name("/"+role),P=top,Pg=page.obj,K=Array([mcid]))); top.K.append(el); created+=1; last=(role,el); lastbox=vb
                if role=="H1" and first_title is None: first_title=txt
            if role!="P": last=None
            wraps.append((u["i0"],u["i1"],"MCID",mcid)); mcid+=1
        out=[]; starts={w[0]:w for w in wraps}; ends={}
        for w in wraps: ends.setdefault(w[1],[]).append(w)
        for j,c in enumerate(ins):
            if j in starts:
                w=starts[j]; out.append(CSI([Name.Artifact],Operator("BMC")) if w[2]=="Artifact" else CSI([Name.P if False else Name("/Span"),Dictionary(MCID=w[3])],Operator("BDC")))
            out.append(c)
            for w in ends.get(j,[]): out.append(CSI([],Operator("EMC")))
        page.obj.Contents=pdf.make_stream(pikepdf.unparse_content_stream(out)); used[page.obj.objgen]=mcid
    # Refresh the content inventory after newly tagged units have been added.
    for page in pdf.pages:
        scope=page.obj.objgen; content_mcids[scope].clear()
        for c in pikepdf.parse_content_stream(page):
            if not isinstance(c,pikepdf.ContentStreamInlineImage) and str(c.operator)=="BDC" and len(c.operands)>1 and isinstance(c.operands[1],pikepdf.Dictionary) and "/MCID" in c.operands[1]:
                content_mcids[scope][int(c.operands[1]["/MCID"])]+=1
    owners.clear(); walk(top.K,rm,own)
    if created: log.append(f"tagged {created} structure elements from untagged content"); acts.append(dict(action="Untagged content tagged",status="succeeded",page=None,detail=f"{created} structure elements created"))
    if inferH[0]: acts.append(dict(action="Headings inferred from font size",status="needs_review",page=None,detail=f"{inferH[0]} heading(s): confirm levels and that they are real headings"))
    # fix BDC tag names: use real struct type for the marked-content tag
    # (Span is valid; PDF/UA only requires MCID linkage)
    # pass 2: repair existing tree
    rm=rolemap(st); pg_index={p.obj.objgen:i for i,p in enumerate(pdf.pages)}
    fixed=collections.Counter(); newrm={}
    def repair(el,d,pg):
        s=str(el.get("/S","")).lstrip("/"); 
        if s and s not in STD:
            tgt=role_of(el,rm)
            if tgt not in STD:
                g=GUESS.get(re.sub(r"[^a-z]","",s.lower().rstrip("0123456789")) if not re.search(r"[1-6]$",s) else re.sub(r"[^a-z0-9]","",s.lower())) or GUESS.get(re.sub(r"[^a-z]","",s.lower())) or "Span"
                newrm[s]=Name("/"+g); fixed["rolemap"]+=1; acts.append(dict(action=f"Custom structure type /{s} mapped to /{g}",status="needs_review",page=None,detail="Guessed from the type name"))
    walk(st.K,rm,repair)
    if newrm: st.RoleMap=Dictionary({("/"+k):v for k,v in {**{k:v for k,v in rm.items() if str(v).lstrip("/") in STD},**newrm}.items()}); rm=rolemap(st)
    heads=[]; figs=[]; tables=[]; scan_cache={}
    def collect(el,d,pg):
        r=role_of(el,rm)
        if re.fullmatch(r"H[1-6]?",r): heads.append((el,r,pg))
        elif r=="Figure": figs.append((el,pg))
        elif r=="Table": tables.append(el)
    walk(st.K,rm,collect); prev=0
    for el,r,pg in heads:
        lvl=int(r[1]) if len(r)>1 else prev+1 if prev else 1
        new=1 if prev==0 else min(lvl,prev+1)
        if f"H{new}"!=str(el.S).lstrip("/") or str(el.S).lstrip("/")!=r: 
            if str(el.S).lstrip("/") in STD: el.S=Name(f"/H{new}"); fixed["heading"]+=1
        prev=new
    for el,pg in figs:
        alt=str(el.get("/Alt","")).strip() or str(el.get("/ActualText","")).strip()
        bad=re.fullmatch(r"(?i)(image|figure|picture|graphic|img[_\-\d.]*\w*|.{0,1}|[\w\-]+\.(png|jpe?g|gif|tiff?))",alt) if alt else True
        if bad:
            a=None; bb=None; n0=len(acts); pi=pg_index.get(pg.objgen) if pg is not None else None
            if pi is not None:
                if pi not in scan_cache: scan_cache[pi]=scan_page(pdf.pages[pi])[2]
                mcb=scan_cache[pi]; m=[mcb[x] for x in mcids_of(el) if x in mcb]
                if m:
                    bb=to_view(mu[pi],[min(b[0] for b in m),min(b[1] for b in m),max(b[2] for b in m),max(b[3] for b in m)])
                    a=alt_for(mu[pi],bb,lng,log,acts,pi+1)
            if not a: acts.append(dict(action="Placeholder alt text inserted",status="needs_review",page=(pi+1) if pi is not None else None,detail="Figure had no usable description: write one by hand"))
            el.Alt=pikepdf.String(a or "Figure (alt text needs review)"); fixed["alt"]+=1
            ids=mcids_of(el)
            if pi is not None and ids: _ref(acts,n0,pi+1,ids[0],bb)
    for t in tables:
        rows=[e for e in [] ]; trs=[]; ths=[]
        walk(t.get("/K"),rm,lambda e,d,p:(trs if role_of(e,rm)=="TR" else ths if role_of(e,rm)=="TH" else []).append(e))
        if not ths and trs:
            walk(trs[0].get("/K"),rm,lambda e,d,p:(setattr(e,"S",Name.TH) if role_of(e,rm)=="TD" else None)); fixed["table_th"]+=1; ths=[]
            walk(trs[0].get("/K"),rm,lambda e,d,p:ths.append(e) if role_of(e,rm)=="TH" else None)
        first=trs[0] if trs else None
        for th in ths:
            at=th.get("/A")
            if at is None or "Scope" not in str(at):
                sc_="Column" if first is not None and th.objgen in [x.objgen for x in (first.K if isinstance(first.get("/K"),pikepdf.Array) else [])] else "Row"
                th.A=Dictionary(O=Name.Table,Scope=Name("/"+sc_)); fixed["scope"]+=1
    # annotations -> Link/Annot struct elems with OBJR
    for i,page in enumerate(pdf.pages):
        for a in page.obj.get("/Annots",[]) or []:
            stp=str(a.get("/Subtype"))
            if stp not in("/Link","/Widget") or "/Rect" not in a: continue
            GEN=r"(?i)(link|click here|here|read more|more|this|page)"
            if stp=="/Link" and (not str(a.get("/Contents","")).strip() or re.fullmatch(GEN,str(a.get("/Contents","")).strip(" .:"))):
                uri=a.get("/A",{}).get("/URI") if isinstance(a.get("/A"),pikepdf.Dictionary) else None
                r=pymupdf.Rect(*[float(x) for x in a.Rect]); r=r*mu[i].transformation_matrix*mu[i].rotation_matrix; r.normalize()
                t=" ".join(tbox(i,r).split()); gen=bool(re.fullmatch(GEN,t.strip(" .:")))
                lab=f"{t} ({uri})" if (t and gen and uri) else (t or (str(uri) if uri else "Link"))
                if gen or not t: acts.append(dict(action="Link description built from its destination",status="needs_review",page=i+1,detail=lab[:140]))
                a.Contents=pikepdf.String(lab); fixed["link_alt"]+=1
            if stp=="/Widget" and not str(a.get("/TU","")).strip(): a.TU=pikepdf.String(str(a.get("/T","Form field")) or "Form field"); fixed["tu"]+=1
            if "/StructParent" not in a:
                el=pdf.make_indirect(Dictionary(Type=Name.StructElem,S=Name.Link if stp=="/Link" else Name.Form,P=top,Pg=page.obj,K=Array([Dictionary(Type=Name.OBJR,Obj=a,Pg=page.obj)])))
                top.K.append(el); a.StructParent=-1; fixed["annot_tag"]+=1
    # rebuild ParentTree
    nums={}; pidx={p.obj.objgen:i for i,p in enumerate(pdf.pages)}; per=collections.defaultdict(dict); nxt=[npg]
    def reg(el,d,pg):
        for m in mcids_of(el):
            if pg is not None and pg.objgen in pidx: per[pidx[pg.objgen]][m]=el
        ks=el.get("/K"); 
        for x in (list(ks) if isinstance(ks,pikepdf.Array) else [ks] if ks is not None else []):
            if isinstance(x,pikepdf.Dictionary) and x.get("/Type")==Name.OBJR:
                o=x.Obj; o.StructParent=nxt[0]; nums[nxt[0]]=el; nxt[0]+=1
    walk(st.K,rm,reg)
    stale={(i,mid) for i,page in enumerate(pdf.pages) for mid in per[i] if mid not in content_mcids[page.obj.objgen]}
    if stale:
        def strip_stale(el,pg=None):
            if not isinstance(el,pikepdf.Dictionary) or el.get("/Type") in (Name.MCR,Name.OBJR): return
            pg=el.get("/Pg",pg); kids=el.get("/K")
            ks=list(kids) if isinstance(kids,pikepdf.Array) else [kids] if kids is not None else []
            keep=[]
            for child in ks:
                mid=None
                if isinstance(child,int): mid=int(child)
                elif isinstance(child,pikepdf.Dictionary) and child.get("/Type")==Name.MCR and "/Stm" not in child: mid=int(child.get("/MCID",-1))
                page_i=pidx.get(pg.objgen) if pg is not None else None
                if mid is not None and page_i is not None and (page_i,mid) in stale: continue
                if isinstance(child,pikepdf.Dictionary) and child.get("/Type") not in (Name.MCR,Name.OBJR): strip_stale(child,pg)
                keep.append(child)
            if isinstance(kids,pikepdf.Array) or len(keep)!=1: el.K=Array(keep)
            elif keep: el.K=keep[0]
            else: el.K=Array()
        for child in list(st.K): strip_stale(child)
        acts.append(dict(action="Stale page MCID references removed",status="needs_review",page=None,detail=f"{len(stale)} structure reference(s) pointed to no content; they were removed and need a human check"))
        per=collections.defaultdict(dict); nums={}; nxt=[npg]
        walk(st.K,rm,reg)
    for i,page in enumerate(pdf.pages):
        scope=page.obj.objgen; present=set(content_mcids[scope]); linked=set(per[i])
        missing=sorted(present-linked); dangling=sorted(linked-present)
        multiply=sorted(m for m in present if len(owners[scope,m])!=1)
        if missing or dangling or multiply:
            raise ValueError(f"MCID reconciliation failed on page {i+1}: unowned={missing[:5]}, dangling={dangling[:5]}, multiply_claimed={multiply[:5]}")
        page.obj.StructParents=i; mx=max(per[i])+1 if per[i] else 0
        arr=Array([per[i].get(m,None) if per[i].get(m) is not None else pikepdf.Object.parse(b"null") for m in range(mx)]); nums[i]=arr
    flat=Array()
    for kk in sorted(nums): flat.append(kk); flat.append(nums[kk])
    st.ParentTree=pdf.make_indirect(Dictionary(Nums=flat)); st.ParentTreeNextKey=nxt[0]
    # document-level
    root.MarkInfo=Dictionary(Marked=True); root.Lang=pikepdf.String(lng)
    root.ViewerPreferences=Dictionary({**dict(root.get("/ViewerPreferences",{})),"/DisplayDocTitle":True}) if "/ViewerPreferences" in root else Dictionary(DisplayDocTitle=True)
    cur=str(pdf.docinfo.get("/Title","")).strip(); cur="" if cur.lower() in("untitled","","none") else cur
    ttl=title or cur or (first_title or "").strip()
    if not ttl:
        best=(0,"")
        for sp in [s for b in mu[0].get_text("dict")["blocks"] if "lines" in b for l in b["lines"] for s in l["spans"]]:
            if sp["size"]>best[0] and sp["text"].strip(): best=(sp["size"],sp["text"].strip())
        ttl=best[1] or os.path.splitext(os.path.basename(src))[0]
    pdf.docinfo["/Title"]=pikepdf.String(ttl)
    if not(title or cur): acts.append(dict(action="Document title set",status="needs_review",page=None,detail=f"Derived from the document: '{ttl[:80]}'",ref=dict(kind="title",value=ttl)))
    xmp=xmp_packet(ttl,lng)
    ms=pdf.make_stream(xmp.encode("utf8")); ms.Type=Name.Metadata; ms.Subtype=Name.XML; root.Metadata=ms
    if npg>=20 and not mu.get_toc():
        with pdf.open_outline() as ol:
            ol.root.clear()
            for el,r,pg in heads:
                if r in("H1","H2") or str(el.S) in("/H1","/H2"):
                    pi=pidx.get(pg.objgen) if pg is not None else None
                    if pi is None: continue
                    t=" ".join(mu[pi].get_text()[:80].split()) or f"Page {pi+1}"; ol.root.append(pikepdf.OutlineItem(t[:60],pi))
            log.append("built bookmarks")
    for k,(txt,st_) in dict(heading=("Heading hierarchy repaired (starts at H1, no skipped levels)","succeeded"),link_alt=("Link descriptions added","succeeded"),annot_tag=("Links/form fields enclosed in structure elements","succeeded"),
                            scope=("Table header Scope added","succeeded"),table_th=("First table row promoted to header cells","needs_review"),tu=("Form field tooltips derived from field names","needs_review")).items():
        if fixed.get(k): acts.append(dict(action=txt,status=st_,page=None,detail=f"{fixed[k]} item(s)"))
    verify_tmp=dst+".verify.pdf"
    pdf.save(verify_tmp,object_stream_mode=pikepdf.ObjectStreamMode.disable); pdf.close(); mu.close()
    try: verification=validate.run(verify_tmp,original=src,external=False)
    except Exception:
        for p in (verify_tmp,tmp):
            try: os.remove(p)
            except OSError: pass
        raise
    required={"struct.tree","struct.marked","struct.mcid","struct.parenttree","struct.roles","keep.pages","keep.text","keep.visual"}
    failed=[c for c in verification["checks"] if c["id"] in required and c["status"] in (validate.FAIL,validate.ERROR)]
    if failed:
        try: os.remove(verify_tmp)
        except OSError: pass
        try: os.remove(tmp)
        except OSError: pass
        details="; ".join(f"{c['id']}: {c['findings'][0]['msg']}" for c in failed if c.get("findings"))
        raise RuntimeError("Output verification failed; repaired file was not committed. "+details[:800])
    os.replace(verify_tmp,dst)
    acts.append(dict(action="Repaired output independently verified",status="succeeded",page=None,detail="Structure, MCID linkage, ParentTree and preservation checks passed"))
    try: os.remove(tmp)
    except OSError: pass
    return dict(log=log,fixed=dict(fixed),language=lng,title=ttl,actions=acts)
