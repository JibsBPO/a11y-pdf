"""Independent re-validation of a PDF (normally the *repaired* PDF).

Why this file exists
--------------------
engine.analyze() is written by the same code that does the repair, so "0 issues" from it only means
"the fixer agrees with itself". This module re-opens the saved file from disk and re-checks it from
scratch (it does NOT call engine.analyze), including cross-checks the engine never made:
content-stream <-> structure-tree MCID linkage, ParentTree integrity, balanced marked content,
text/visual preservation against the original, and (optionally) an external veraPDF run.

Every check ends in exactly one of three outcomes (plus two non-results):
  passed        the automated check ran and found nothing wrong
  needs_review  a person must verify (cannot be decided automatically)
  failed        the check found a concrete, unresolved problem
  pending       an external validator is not configured / not run yet
  error         the check itself crashed - never treated as a pass
"""
import collections, datetime, hashlib, io, json, os, re, shutil, subprocess
import xml.etree.ElementTree as ET
import pikepdf, pymupdf
import forms
from forms import content_kinds
from pikepdf import Name
from rules import RULES

PASS, REVIEW, FAIL, PENDING, ERROR = "passed", "needs_review", "failed", "pending", "error"
STD = set("Document Part Art Sect Div BlockQuote Caption TOC TOCI Index NonStruct Private P H H1 H2 H3 H4 H5 H6 L LI Lbl LBody Table TR TH TD THead TBody TFoot Span Quote Note Reference BibEntry Code Link Annot Ruby Warichu RB RT RP WT WP Figure Formula Form".split())
PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}
TEXTOPS = {"Tj", "TJ", "'", '"'}
PLACEHOLDER = re.compile(r"needs review|todo|tbd|placeholder", re.I)
GENERIC_ALT = re.compile(r"(?i)(image|figure|picture|graphic|photo|img[_\-\d.]*\w*|.{0,1}|[\w\-]+\.(png|jpe?g|gif|tiff?))")
GENERIC_LINK = re.compile(r"(?i)(link|click here|here|read more|more|this|page|https?://\S+|www\.\S+)")

# id -> (group, title, WCAG, Matterhorn). Matterhorn codes are reused from rules.py where one exists.
CHECKS = {
    "struct.tree":        ("Structure", "Document is tagged (StructTreeRoot, MarkInfo)", "1.3.1", "01-004"),
    "struct.marked":      ("Structure", "All page content is tagged or marked as Artifact; marked content is balanced", "1.3.1", "01-005"),
    "form.hidden":        ("Structure", "Text and images inside Form XObjects are not hidden as Artifacts", "1.3.1", "01-005"),
    "struct.mcid":        ("Structure", "Every content MCID is in the structure tree, and every tree MCID exists on its page", "1.3.1", "09-001"),
    "struct.parenttree":  ("Structure", "ParentTree / StructParent(s) agree with the structure tree", "1.3.1", "09-004"),
    "struct.roles":       ("Structure", "All structure types are standard or map to a standard type", "4.1.2", "02-001"),
    "meta.lang":          ("Metadata", "Document language is set and well-formed", "3.1.1", "11-006"),
    "meta.title":         ("Metadata", "Document title is present and shown in the title bar", "2.4.2", "07-001"),
    "meta.xmp":           ("Metadata", "XMP metadata is well-formed and declares PDF/UA-1", "4.1.2", "05-003"),
    "page.tabs":          ("Metadata", "Every page uses structure tab order (/Tabs /S)", "2.4.3", "28-004"),
    "head.hierarchy":     ("Headings", "Headings start at H1 and do not skip levels", "1.3.1", "14-003"),
    "link.contents":      ("Links & annotations", "Links have a description and a destination", "2.4.4", "28-011"),
    "annot.tagged":       ("Links & annotations", "Annotations are enclosed in structure elements", "1.3.1", "28-002"),
    "link.text":          ("Links & annotations", "Link descriptions are meaningful", "2.4.4", "—"),
    "fig.alt":            ("Images", "Every Figure has alternative text that is not a placeholder or file name", "1.1.1", "13-004"),
    "fig.quality":        ("Images", "Alternative text accurately describes each image", "1.1.1", "—"),
    "table.headers":      ("Tables", "Tables have header cells with a Scope", "1.3.1", "15-001"),
    "form.tu":            ("Forms", "Form fields have a tooltip/label (/TU)", "3.3.2", "28-005"),
    "font.embed":         ("Fonts & text", "All fonts are embedded", "1.3.1", "31-001"),
    "font.unicode":       ("Fonts & text", "Text can be extracted (composite fonts have ToUnicode)", "1.3.1", "31-008"),
    "ocr.layer":          ("Fonts & text", "Scanned pages have a real text layer", "1.1.1", "01-005"),
    "ocr.quality":        ("Fonts & text", "OCR text layer is accurate", "1.1.1", "—"),
    "keep.pages":         ("Preservation", "Page count, size and rotation unchanged", "—", "—"),
    "keep.text":          ("Preservation", "No original text lost", "—", "—"),
    "keep.visual":        ("Preservation", "Pages render identically to the original", "—", "—"),
    "man.order":          ("Manual review", "Reading order is logical", "1.3.2", "—"),
    "man.contrast":       ("Manual review", "Text and graphics have sufficient colour contrast", "1.4.3", "—"),
    "man.parts":          ("Manual review", "Language changes within the text are marked", "3.1.2", "—"),
    "man.at":             ("Manual review", "Tested with a screen reader (NVDA / JAWS / VoiceOver)", "—", "—"),
}
# Severity of a *failure* (review items are always severity 'review'). Anything not listed is 'major'.
SEV = {"form.hidden": "critical", "struct.tree": "critical", "struct.marked": "critical", "struct.mcid": "critical", "fig.alt": "critical", "ocr.layer": "critical",
       "keep.text": "critical", "keep.pages": "critical", "link.contents": "major", "form.tu": "major", "keep.visual": "major",
       "page.tabs": "minor", "meta.title": "minor", "meta.xmp": "minor"}
SEV_ORDER = {"critical": 0, "major": 1, "minor": 2, "review": 3}

LIMITATIONS = [
    "Forms that carry their own marked content, tiling patterns and annotation appearance streams are not inspected for tagging; Forms holding text or images are checked (form.hidden) and flattened by the repair.",
    "Reading order and alternative-text accuracy cannot be proven automatically; they are always reported as needs review.",
    "PAC has no command-line or API; run it manually on the downloaded PDF. veraPDF only runs if installed (see README).",
    "PDF/UA conformance by tools does not guarantee a good screen-reader experience; test with assistive technology.",
]


# ---------------------------------------------------------------- small helpers
def is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)

def kids(node):
    k = node.get("/K")
    return [] if k is None else (list(k) if isinstance(k, pikepdf.Array) else [k])

def og(o):
    try: return tuple(o.objgen)
    except Exception: return (0, 0)

def read_numtree(node, out, depth=0):
    if node is None or depth > 25: return
    nums = node.get("/Nums")
    if nums is not None:
        for i in range(0, len(nums) - 1, 2): out[int(nums[i])] = nums[i + 1]
    for k in (node.get("/Kids") or []): read_numtree(k, out, depth + 1)

def resolve_role(s, rm):
    """-> (resolved role, is_cyclic)"""
    seen = set(); s = s.lstrip("/")
    while s in rm:
        if s in seen: return s, True
        seen.add(s); s = str(rm[s]).lstrip("/")
    return s, False

def to_view(pg, bbox):
    r = pymupdf.Rect(*bbox).normalize() * pg.transformation_matrix * pg.rotation_matrix
    r.normalize(); return [round(v, 1) for v in (r.x0, r.y0, r.x1, r.y1)]


class Collector:
    def __init__(self): self.f = collections.defaultdict(list)
    def add(self, check, status, msg, page=None, count=1, bbox=None):
        self.f[check].append(dict(status=status, page=page, msg=msg, count=count, bbox=bbox))
    fail = lambda s, c, m, **k: s.add(c, FAIL, m, **k)
    review = lambda s, c, m, **k: s.add(c, REVIEW, m, **k)


# ---------------------------------------------------------------- content-stream scan (independent of engine.scan_page)
def scan_content(page, cache=None):
    out = dict(fonts=set(), mcids=collections.Counter(), unbalanced=0, untagged=collections.Counter(), invisible_text=False, hidden_forms=0, error=None)
    stack = []; curfont = None; cache = {} if cache is None else cache
    res = forms.inherited(page.obj, "/Resources"); xo = (res.get("/XObject") if res is not None else None) or {}
    try: ins = list(pikepdf.parse_content_stream(page))
    except Exception as e:
        out["error"] = f"content stream could not be parsed: {e}"; return out
    for c in ins:
        if isinstance(c, pikepdf.ContentStreamInlineImage): op, a = "BI", []
        else: op, a = str(c.operator), c.operands
        if op in ("BDC", "BMC"):
            mid = None
            if op == "BDC" and len(a) > 1 and isinstance(a[1], pikepdf.Dictionary) and "/MCID" in a[1]: mid = int(a[1]["/MCID"])
            stack.append((str(a[0]) if a else "", mid))
            if mid is not None: out["mcids"][mid] += 1
        elif op == "EMC":
            if stack: stack.pop()
            else: out["unbalanced"] += 1
        else:
            if op == "Tr" and a and is_int(a[0]) and int(a[0]) == 3: out["invisible_text"] = True
            if op == "Tf" and a: curfont = str(a[0])
            if op in TEXTOPS and curfont: out["fonts"].add(curfont)   # font counts only once text is shown with it
            kind = "text" if op in TEXTOPS else "graphics" if (op in PAINT or op == "sh") else "XObject" if op == "Do" else "inline image" if op == "BI" else None
            if kind and not any(t == "/Artifact" or m is not None for t, m in stack): out["untagged"][kind] += 1
            if op == "Do" and a and any(t == "/Artifact" for t, m in stack):          # a Form wrapped in an Artifact that really holds text/images
                x = xo.get(str(a[0]))
                if x is not None and str(x.get("/Subtype")) == "/Form" and content_kinds(x, res, cache) & {"text", "image"}: out["hidden_forms"] += 1
    out["unbalanced"] += len(stack)
    return out


def walk_struct(pdf, pidx):
    """Document-order walk. Returns elems, refs {(page, mcid): [elem objgen]}, objrs {annot objgen: elem}, problems."""
    st = pdf.Root.StructTreeRoot; rm = {str(k).lstrip("/"): v for k, v in st.get("/RoleMap", {}).items()}
    elems, refs, objrs, problems, seen = [], collections.defaultdict(list), {}, [], set()
    stack = [(k, st, None, 1) for k in reversed(kids(st))]
    while stack:
        n, par, pg, d = stack.pop()
        if d > 200: problems.append("structure tree deeper than 200 levels"); continue
        if is_int(n):
            if pg is None: problems.append("MCID reference with no /Pg in scope")
            else: refs[(pidx.get(og(pg)), int(n))].append(og(par))
            continue
        if not isinstance(n, pikepdf.Dictionary): continue
        t = n.get("/Type")
        if t == Name.MCR:
            p = n.get("/Pg", pg)
            refs[(pidx.get(og(p)) if p is not None else None, int(n.get("/MCID", -1)))].append(og(par))
        elif t == Name.OBJR:
            if n.get("/Obj") is not None: objrs[og(n.Obj)] = (par, pidx.get(og(n.get("/Pg", pg))) if n.get("/Pg", pg) is not None else None)
        else:
            if og(n) != (0, 0):
                if og(n) in seen: problems.append("structure element reachable twice (cycle or shared child)"); continue
                seen.add(og(n))
            p = n.get("/Pg", pg); s = str(n.get("/S", "")); role, cyc = resolve_role(s, rm)
            elems.append(dict(el=n, og=og(n), s=s.lstrip("/"), role=role, cyclic=cyc, page=pidx.get(og(p)) if p is not None else None, depth=d))
            for k in reversed(kids(n)): stack.append((k, n, p, d + 1))
    return elems, refs, objrs, problems, rm


def iter_fonts(res, seen, depth=0, used=None):
    """Fonts in a resource dict, descending into Form XObjects. At page level only fonts actually selected
    with Tf are returned (a font merely declared in /Resources is never rendered, so it is not a violation)."""
    if res is None or depth > 6: return
    for name, f in (res.get("/Font") or {}).items():
        if used is not None and str(name) not in used: continue
        if og(f) not in seen or og(f) == (0, 0): seen.add(og(f)); yield f
    for _, x in (res.get("/XObject") or {}).items():
        if str(x.get("/Subtype")) == "/Form" and og(x) not in seen:
            seen.add(og(x)); yield from iter_fonts(x.get("/Resources"), seen, depth + 1)


def font_is_embedded(f):
    if str(f.get("/Subtype")) == "/Type3": return True
    if str(f.get("/Subtype")) == "/Type0": f = f["/DescendantFonts"][0]
    d = f.get("/FontDescriptor")
    return d is not None and any(k in d for k in ("/FontFile", "/FontFile2", "/FontFile3"))


# ---------------------------------------------------------------- the checks
def run_checks(src, original=None):
    pdf = pikepdf.open(src); doc = pymupdf.open(src); root = pdf.Root; C = Collector()
    pages = list(pdf.pages); pidx = {og(p.obj): i for i, p in enumerate(pages)}
    ctx = {}   # shared results between checks

    def safe(fn):
        try: fn()
        except Exception as e: C.add(fn.__name__.replace("_", "."), ERROR, f"check crashed: {type(e).__name__}: {e}")

    scans = {}
    fcache = {}
    for i, p in enumerate(pages): scans[i] = scan_content(p, fcache)

    def tree():
        mk = root.get("/MarkInfo")
        if not (mk is not None and bool(mk.get("/Marked", False))): C.fail("struct.tree", "MarkInfo /Marked is not true")
        if "/StructTreeRoot" not in root:
            C.fail("struct.tree", "No StructTreeRoot: the document is untagged"); return
        if not kids(root.StructTreeRoot): C.fail("struct.tree", "StructTreeRoot is empty")
    safe(tree)
    has_tree = "/StructTreeRoot" in root
    elems, refs, objrs, problems, rm = ([], {}, {}, [], {})
    if has_tree:
        try: elems, refs, objrs, problems, rm = walk_struct(pdf, pidx)
        except Exception as e: C.add("struct.tree", ERROR, f"structure tree could not be walked: {e}")
    for pr in problems: C.fail("struct.tree", pr)
    by_page = collections.defaultdict(dict)                      # page -> {mcid: [owner objgens]}  (indexed once: avoids O(pages x refs) scans)
    for (p_, m_), o_ in refs.items(): by_page[p_][m_] = o_

    def marked():
        for i, s in scans.items():
            if s["error"]: C.add("struct.marked", ERROR, s["error"], page=i + 1); continue
            if s["unbalanced"]: C.fail("struct.marked", f"{s['unbalanced']} unbalanced BDC/BMC/EMC operator(s)", page=i + 1, count=s["unbalanced"])
            for kind, n in s["untagged"].items(): C.fail("struct.marked", f"{n} untagged {kind} operation(s): not tagged and not an Artifact", page=i + 1, count=n)
    safe(marked)

    def form_hidden():
        for i, s in scans.items():
            if s["hidden_forms"]: C.fail("form.hidden", f"{s['hidden_forms']} Form XObject(s) containing text or images are marked as Artifact: that content is hidden from assistive technology", page=i + 1, count=s["hidden_forms"])
    safe(form_hidden)

    def mcid():
        if not has_tree: return
        for i, s in scans.items():
            if s["error"]: continue
            for m, n in s["mcids"].items():
                if n > 1: C.fail("struct.mcid", f"MCID {m} is used {n} times on the page", page=i + 1)
            inpage = set(by_page.get(i, {}))
            orphan = sorted(set(s["mcids"]) - inpage); dangling = sorted(inpage - set(s["mcids"]))
            if orphan: C.fail("struct.mcid", f"{len(orphan)} marked-content MCID(s) not referenced by any structure element (e.g. {orphan[:5]}): content invisible to assistive technology", page=i + 1, count=len(orphan))
            if dangling: C.fail("struct.mcid", f"{len(dangling)} structure reference(s) to MCIDs missing from the page (e.g. {dangling[:5]})", page=i + 1, count=len(dangling))
        for (p, m), owners in refs.items():
            if p is None: C.fail("struct.mcid", f"MCID {m} has no resolvable page")
            elif len(set(owners)) > 1: C.fail("struct.mcid", f"MCID {m} is claimed by {len(set(owners))} structure elements", page=p + 1)
    safe(mcid)

    def parenttree():
        if not has_tree: return
        st = root.StructTreeRoot; pt = {}
        if "/ParentTree" not in st: C.fail("struct.parenttree", "StructTreeRoot has no ParentTree"); return
        read_numtree(st.ParentTree, pt)
        for i, p in enumerate(pages):
            need = list(by_page.get(i, {}))
            if need and "/StructParents" not in p.obj: C.fail("struct.parenttree", "Page has tagged content but no /StructParents", page=i + 1); continue
            if not need: continue
            arr = pt.get(int(p.obj.StructParents))
            if arr is None or not isinstance(arr, pikepdf.Array): C.fail("struct.parenttree", "ParentTree has no array for this page's StructParents key", page=i + 1); continue
            bad = 0
            for m in need:
                owner = {o for o in refs[(i, m)]}
                got = og(arr[m]) if m < len(arr) and arr[m] is not None else None
                if got not in owner: bad += 1
            if bad: C.fail("struct.parenttree", f"{bad} ParentTree entr{'y' if bad == 1 else 'ies'} point to the wrong or a missing structure element", page=i + 1, count=bad)
        annot_sp = {og(a): a.get("/StructParent") for p in pages for a in (p.obj.get("/Annots") or [])}
        for obj, (el, pg) in objrs.items():
            sp = annot_sp.get(obj)
            if sp is None: C.fail("struct.parenttree", "Annotation referenced by an OBJR has no /StructParent", page=(pg + 1) if pg is not None else None)
            elif og(pt.get(int(sp)) if pt.get(int(sp)) is not None else None) != og(el): C.fail("struct.parenttree", "Annotation /StructParent does not map back to its structure element", page=(pg + 1) if pg is not None else None)
        nk = st.get("/ParentTreeNextKey")
        if nk is not None and pt and int(nk) <= max(pt): C.fail("struct.parenttree", "ParentTreeNextKey is not greater than the highest key in use")
    safe(parenttree)

    def roles():
        for k, v in rm.items():
            if k in STD and str(v).lstrip("/") != k: C.fail("struct.roles", f"Standard type /{k} is re-mapped to /{str(v).lstrip('/')}")
        bad = collections.Counter((e["s"], e["role"]) for e in elems if e["cyclic"] or e["role"] not in STD)
        for (s, r), n in bad.items(): C.fail("struct.roles", f"Structure type /{s} does not resolve to a standard type (ends at '{r}')", count=n)
    safe(roles)

    def lang():
        l = str(root.get("/Lang", "")).strip()
        if not l: C.fail("meta.lang", "Catalog /Lang is missing")
        elif not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*", l): C.fail("meta.lang", f"Language tag '{l}' is not a valid BCP 47 tag")
        else: ctx["lang"] = l
    safe(lang)

    def xmp_and_title():
        info = str(pdf.docinfo.get("/Title", "")).strip(); xmp = ""
        if "/Metadata" in root:
            raw = bytes(root.Metadata.read_bytes()).decode("utf8", "ignore")
            try:
                ET.fromstring(re.sub(r"<\?xpacket[^>]*\?>", "", raw).strip()); xmp = raw
            except ET.ParseError as e: C.fail("meta.xmp", f"XMP packet is not well-formed XML: {e}")
        else: C.fail("meta.xmp", "No XMP metadata stream")
        if xmp:
            m = re.search(r"pdfuaid:part>\s*(\d+)", xmp) or re.search(r'pdfuaid:part="(\d+)"', xmp)
            if not m: C.fail("meta.xmp", "XMP does not declare pdfuaid:part")
            elif m.group(1) != "1": C.fail("meta.xmp", f"XMP declares pdfuaid:part={m.group(1)}, expected 1")
        t = re.search(r"<dc:title>.*?<rdf:li[^>]*>([^<]*)<", xmp, re.S); xt = t.group(1).strip() if t else ""
        if not (info or xt) or (info or xt).lower() in ("untitled", "none"): C.fail("meta.title", "No meaningful document title")
        elif info and xt and info != xt: C.review("meta.title", f"Info Title and XMP dc:title differ ('{info[:40]}' vs '{xt[:40]}')")
        vp = root.get("/ViewerPreferences")
        if not (vp is not None and bool(vp.get("/DisplayDocTitle", False))): C.fail("meta.title", "ViewerPreferences /DisplayDocTitle is not true")
    safe(xmp_and_title)

    def tabs():
        for i, p in enumerate(pages):
            if str(p.obj.get("/Tabs", "")) != "/S": C.fail("page.tabs", "/Tabs is not /S", page=i + 1)
    safe(tabs)

    def headings():
        hs = [e for e in elems if re.fullmatch(r"H[1-6]?", e["role"])]
        if not hs:
            if len(pages) > 1: C.review("head.hierarchy", "No headings found in a multi-page document: confirm that it really has none")
            return
        prev = 0
        if any(e["role"] == "H" for e in hs) and any(len(e["role"]) == 2 for e in hs): C.fail("head.hierarchy", "Mixes plain /H with numbered H1-H6 headings")
        for k, e in enumerate(hs):
            lvl = int(e["role"][1]) if len(e["role"]) == 2 else None
            pg = (e["page"] + 1) if e["page"] is not None else None
            if k == 0 and lvl != 1: C.fail("head.hierarchy", f"First heading is {e['role']}, not H1", page=pg)
            elif lvl and lvl > prev + 1: C.fail("head.hierarchy", f"Heading jumps from H{prev} to H{lvl}", page=pg)
            prev = lvl or prev
        if sum(e["role"] == "H1" for e in hs) > 1: C.review("head.hierarchy", f"{sum(e['role'] == 'H1' for e in hs)} H1 headings: fine for some documents, confirm it is intended")
    safe(headings)

    def annots():
        for i, p in enumerate(pages):
            for a in (p.obj.get("/Annots") or []):
                st = str(a.get("/Subtype")); flags = int(a.get("/F", 0))
                if st in ("/Popup", "/PrinterMark", "/TrapNet") or flags & 2 or "/Rect" not in a: continue
                bb = to_view(doc[i], [float(x) for x in a.Rect])
                if og(a) not in objrs: C.fail("annot.tagged", f"{st[1:]} annotation is not enclosed in a structure element (no OBJR)", page=i + 1, bbox=bb)
                c = str(a.get("/Contents", "")).strip()
                if st == "/Link":
                    act = a.get("/A"); has_dest = "/Dest" in a or (isinstance(act, pikepdf.Dictionary) and ("/URI" in act or "/D" in act or "/S" in act))
                    if not c: C.fail("link.contents", "Link has no /Contents description", page=i + 1, bbox=bb)
                    if not has_dest: C.fail("link.contents", "Link has no destination or action", page=i + 1, bbox=bb)
                    uri = str(act.get("/URI", "")) if isinstance(act, pikepdf.Dictionary) else ""
                    if c and (GENERIC_LINK.fullmatch(c) or (uri and c == uri)): C.review("link.text", f"Link description '{c[:50]}' may not be meaningful out of context", page=i + 1, bbox=bb)
                elif st == "/Widget":
                    if not str(a.get("/TU", "")).strip(): C.fail("form.tu", "Form field has no /TU tooltip", page=i + 1, bbox=bb)
                elif not c: C.review("link.contents", f"{st[1:]} annotation has no /Contents", page=i + 1, bbox=bb)
    safe(annots)

    def figures():
        figs = [e for e in elems if e["role"] == "Figure"]; ctx["figs"] = len(figs)
        for e in figs:
            alt = str(e["el"].get("/Alt", "")).strip() or str(e["el"].get("/ActualText", "")).strip(); pg = (e["page"] + 1) if e["page"] is not None else None
            if not alt: C.fail("fig.alt", "Figure has no /Alt or /ActualText", page=pg)
            elif GENERIC_ALT.fullmatch(alt): C.fail("fig.alt", f"Alt text '{alt}' is a generic word or file name", page=pg)
            elif PLACEHOLDER.search(alt): C.review("fig.quality", f"Alt text is a placeholder: '{alt[:60]}'", page=pg)
            elif len(alt) > 250: C.review("fig.quality", "Alt text is very long (>250 chars): consider a shorter alt plus a text description", page=pg)
        if figs: C.review("fig.quality", f"{len(figs)} figure(s): a person must confirm each description matches the image (AI/OCR-generated text is never auto-approved)")
    safe(figures)

    def tables():
        for e in [e for e in elems if e["role"] == "Table"]:
            pg = (e["page"] + 1) if e["page"] is not None else None; found = []; stack = list(kids(e["el"]))
            while stack:
                n = stack.pop()
                if isinstance(n, pikepdf.Dictionary) and n.get("/Type") not in (Name.MCR, Name.OBJR):
                    r, _ = resolve_role(str(n.get("/S", "")), rm)
                    if r == "TH": found.append(n)
                    stack.extend(kids(n))
            if not found: C.fail("table.headers", "Table has no TH header cells", page=pg)
            else:
                ns = sum(1 for th in found if "Scope" not in str(th.get("/A", "")) and "/Headers" not in th)
                if ns: C.review("table.headers", f"{ns} header cell(s) without Scope (or Headers): fine for simple tables, confirm", page=pg, count=ns)
    safe(tables)

    def fonts():
        seen = set()
        for i, p in enumerate(pages):
            for f in iter_fonts(p.obj.get("/Resources"), seen, used=scans[i]["fonts"]):
                try:
                    if not font_is_embedded(f): C.fail("font.embed", f"Font {str(f.get('/BaseFont', '?'))} is not embedded", page=i + 1)
                    elif str(f.get("/Subtype")) == "/Type0" and "/ToUnicode" not in f: C.fail("font.unicode", f"Composite font {str(f.get('/BaseFont', '?'))} has no ToUnicode map", page=i + 1)
                except Exception: C.review("font.embed", "A font dictionary could not be inspected", page=i + 1)
    safe(fonts)

    def ocr():
        for i, p in enumerate(doc):
            words = len(p.get_text("words")); big = any(pymupdf.Rect(im["bbox"]).get_area() > 0.6 * p.rect.get_area() for im in p.get_image_info())
            if words < 3 and big: C.fail("ocr.layer", "Page is a scanned image with no real text", page=i + 1)
            elif scans[i]["invisible_text"]: C.review("ocr.quality", "Invisible OCR text layer: compare the recognised text with the page image", page=i + 1)
    safe(ocr)

    def preservation():
        if original is None: return
        o = pymupdf.open(original)
        if len(o) != len(doc): C.fail("keep.pages", f"Page count changed: {len(o)} -> {len(doc)}"); return
        for i in range(len(doc)):
            if o[i].rect != doc[i].rect or o[i].rotation != doc[i].rotation: C.fail("keep.pages", "Page size or rotation changed", page=i + 1)
            a = collections.Counter(w[4] for w in o[i].get_text("words")); b = collections.Counter(w[4] for w in doc[i].get_text("words"))
            lost = a - b
            if lost: C.fail("keep.text", f"{sum(lost.values())} original word(s) no longer extractable (e.g. {list(lost)[:4]})", page=i + 1, count=sum(lost.values()))
            try:
                from PIL import Image, ImageChops
                px = lambda d: Image.open(io.BytesIO(d[i].get_pixmap(dpi=60).tobytes("png"))).convert("L")
                x, y = px(o), px(doc); diff = ImageChops.difference(x, y).point(lambda v: 255 if v > 40 else 0)
                frac = sum(diff.histogram()[255:]) / (x.width * x.height)
                if frac > 0.03: C.fail("keep.visual", f"{frac * 100:.1f}% of pixels differ from the original: content may have moved or disappeared", page=i + 1)
                elif frac > 0.003: C.review("keep.visual", f"{frac * 100:.2f}% of pixels differ (typically a substituted font's glyph shapes): compare with the original", page=i + 1)
            except ImportError: C.review("keep.visual", "Pillow not installed: visual comparison skipped")
    safe(preservation)

    # Reading order: PyMuPDF exposes no per-MCID geometry, so we do not guess. Always a human check.
    C.review("man.order", "Reading order cannot be verified automatically: step through the structure tree (e.g. Acrobat Reading Order tool, PAC logical structure view)")
    C.review("man.contrast", "Colour contrast is not tested automatically; check text/background and graphics against WCAG 1.4.3 / 1.4.11")
    C.review("man.parts", "Phrases in another language need their own /Lang: not detectable automatically")
    C.review("man.at", "Open the downloaded PDF in a screen reader and read through it once")

    # ---- assemble
    checks = []
    for cid, (group, title, wcag, mh) in CHECKS.items():
        fs = C.f.get(cid, []); order_ = {ERROR: 4, FAIL: 3, REVIEW: 2}
        status = max((f["status"] for f in fs), key=lambda s: order_[s]) if fs else PASS
        for f in fs: f["sev"] = "review" if f["status"] == REVIEW else SEV.get(cid, "major")
        checks.append(dict(id=cid, group=group, title=title, wcag=wcag, mh=mh, status=status, findings=fs))
    pg_stats = [dict(page=i + 1, failed=0, needs_review=0) for i in range(len(pages))]
    for c in checks:
        for f in c["findings"]:
            if f["page"] and 1 <= f["page"] <= len(pages):
                if f["status"] in (FAIL, ERROR): pg_stats[f["page"] - 1]["failed"] += f["count"]
                elif f["status"] == REVIEW: pg_stats[f["page"] - 1]["needs_review"] += f["count"]
    pdf.close(); n = len(pages); doc.close()
    return checks, pg_stats, n


# ---------------------------------------------------------------- external validators
def run_verapdf(path, timeout=180):
    """Runs veraPDF's PDF/UA-1 profile if the CLI is available. Parser follows veraPDF's JSON report format;
    verify against your installed version (see README)."""
    exe = os.environ.get("VERAPDF") or shutil.which("verapdf")
    if not exe: return dict(tool="veraPDF (PDF/UA-1)", status=PENDING, msg="Not installed. Install veraPDF and set VERAPDF=/path/to/verapdf, or run it manually.", details=[])
    try:
        p = subprocess.run([exe, "--flavour", "ua1", "--format", "json", path], capture_output=True, text=True, timeout=timeout)
        j = json.loads(p.stdout)
        vr = j["report"]["jobs"][0]["validationResult"]; vr = vr[0] if isinstance(vr, list) else vr
        compliant = vr.get("compliant", vr.get("isCompliant"))
        rules = [r for r in vr.get("details", {}).get("ruleSummaries", []) if str(r.get("status", "")).lower() == "failed"]
        details = [dict(clause=r.get("clause"), test=r.get("testNumber"), failed=r.get("failedChecks"), msg=(r.get("description") or "")[:240]) for r in rules]
        if compliant is None: return dict(tool="veraPDF (PDF/UA-1)", status=ERROR, msg="veraPDF ran but the report had no compliance flag", details=[])
        return dict(tool="veraPDF (PDF/UA-1)", status=PASS if compliant else FAIL, msg="Compliant with PDF/UA-1" if compliant else f"{len(rules)} rule(s) failed", details=details)
    except subprocess.TimeoutExpired: return dict(tool="veraPDF (PDF/UA-1)", status=ERROR, msg=f"Timed out after {timeout}s", details=[])
    except Exception as e: return dict(tool="veraPDF (PDF/UA-1)", status=ERROR, msg=f"Could not run/parse veraPDF: {type(e).__name__}: {e}", details=[])


PAC_NOTE = dict(tool="PAC (PDF Accessibility Checker)", status=PENDING, details=[],
                msg="PAC is a Windows desktop app with no command-line interface or API. Open the downloaded PDF in PAC and record the result here manually.")


# ---------------------------------------------------------------- public API
def verdict(checks, external):
    st = {c["status"] for c in checks} | {e["status"] for e in external if e["status"] != PENDING}
    if ERROR in st: return "error", "Validation incomplete: at least one check crashed. Do not treat this file as verified."
    if FAIL in st: return "failed", "Unresolved accessibility failures remain."
    if REVIEW in st or any(e["status"] == PENDING for e in external):
        return "needs_review", "No automated check failed, but items need human review and/or external validation is pending. This is not a certification."
    return "passed", "All automated checks passed. Manual / assistive-technology testing is still recommended."


def run(src, original=None, external=True):
    """Validate the PDF at `src`. Pass `original` to also verify nothing was lost during repair."""
    checks, pg, n = run_checks(src, original)
    ext = [run_verapdf(src), PAC_NOTE] if external else []
    v, text = verdict(checks, ext)
    tot = lambda s: sum(f["count"] for c in checks for f in c["findings"] if f["status"] == s)
    by_sev = {k: sum(f["count"] for c in checks for f in c["findings"] if f["sev"] == k and f["status"] in (FAIL, ERROR)) for k in ("critical", "major", "minor")}
    h = hashlib.sha256(open(src, "rb").read()).hexdigest()
    return dict(verdict=v, verdict_text=text, pages=n, sha256=h, bytes=os.path.getsize(src),
                totals=dict(failed=tot(FAIL) + tot(ERROR), by_sev=by_sev, needs_review=tot(REVIEW), checks=len(checks),
                            checks_passed=sum(c["status"] == PASS for c in checks), checks_failed=sum(c["status"] in (FAIL, ERROR) for c in checks),
                            checks_review=sum(c["status"] == REVIEW for c in checks)),
                checks=checks, page_results=pg, external=ext, limitations=LIMITATIONS,
                generated=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))


def compare(before, after):
    """Per-check outcome of the repair, judged by the same validator on both files."""
    b = {c["id"]: c for c in before["checks"]}; out = []
    for c in after["checks"]:
        was = b[c["id"]]["status"]; now = c["status"]
        if was == FAIL and now == PASS: res = "fixed"
        elif was == FAIL and now == REVIEW: res = "needs_review"
        elif was in (FAIL, ERROR) and now in (FAIL, ERROR): res = "failed"
        elif was != FAIL and now in (FAIL, ERROR): res = "regressed"
        else: continue
        out.append(dict(id=c["id"], title=c["title"], group=c["group"], before=was, after=now, outcome=res))
    return out
