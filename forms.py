"""Form XObjects that contain real content.

A Form XObject is a reusable block of page content that a page runs with `Do`. Some producers put the whole body of a page in
one (headers and logos are the harmless case). v3 wrapped every Form in an Artifact, which hid any text or images inside it
from assistive technology.

What this module does: a Form that contains text or images (directly or via nested Forms) is *flattened*: the `Do` operator is
replaced by the form's own content, inside `q ... Q`, with the form's /Matrix applied, its /BBox as clip, and its resources
renamed (Fm<n>_<name>) and merged into the page's resources so names can never collide. The page then looks the same, and the
normal tagging pass can tag that text and those images. Forms with only vector graphics are left alone (they become Artifacts,
which is right for them). Forms that already carry their own marked content (/MCID), forms with optional content (/OC) and
forms deeper than 3 levels are left alone and reported.
"""
import pikepdf
from pikepdf import Name, Dictionary

TEXT = {"Tj", "TJ", "'", '"'}
DEVICE = {"/DeviceGray", "/DeviceRGB", "/DeviceCMYK", "/Pattern"}
MAX_DEPTH = 3
CSI = pikepdf.ContentStreamInstruction
OP = pikepdf.Operator


def inherited(obj, key):
    """Value of a page attribute that may be inherited from /Parent nodes (e.g. /Resources)."""
    seen = 0
    while obj is not None and seen < 50:
        if key in obj: return obj[key]
        obj = obj.get("/Parent"); seen += 1
    return None


def content_kinds(form, fallback_res=None, cache=None, depth=0):
    """What a Form XObject contains: a subset of {'text', 'image', 'marked'} (nested Forms included)."""
    cache = {} if cache is None else cache
    key = form.objgen
    if key != (0, 0) and key in cache: return cache[key]
    out = set()
    if depth <= MAX_DEPTH:
        res = form.get("/Resources", fallback_res); xo = (res.get("/XObject") if res is not None else None) or {}
        try: ins = list(pikepdf.parse_content_stream(form))
        except Exception: ins = []
        for c in ins:
            if isinstance(c, pikepdf.ContentStreamInlineImage): out.add("image"); continue
            op = str(c.operator); a = c.operands
            if op in TEXT: out.add("text")
            elif op == "BDC" and len(a) > 1 and isinstance(a[1], Dictionary) and "/MCID" in a[1]: out.add("marked")
            elif op == "Do" and a:
                x = xo.get(str(a[0]))
                if x is None: continue
                st = str(x.get("/Subtype"))
                if st == "/Image": out.add("image")
                elif st == "/Form": out |= content_kinds(x, res, cache, depth + 1)
    if key != (0, 0): cache[key] = out
    return out


def is_flattenable(x, fallback_res, cache):
    if str(x.get("/Subtype")) != "/Form" or "/OC" in x: return False
    k = content_kinds(x, fallback_res, cache)
    return bool(k & {"text", "image"}) and "marked" not in k


def _expand(page_res, x, counter):
    counter[0] += 1; pre = f"Fm{counter[0]}_"
    fres = x.get("/Resources"); rename = {}
    def ren(cat, name):
        if fres is None: return name                            # the form uses the page's own resources: nothing to rename
        old = str(name); d = fres.get(cat)
        if d is None or old not in d: return name               # refers to an inherited page resource
        new = pre + old.lstrip("/"); rename[(cat, old)] = new
        return Name("/" + new)
    out = [CSI([], OP("q"))]
    m = x.get("/Matrix")
    if m is not None and [float(v) for v in m] != [1, 0, 0, 1, 0, 0]: out.append(CSI([float(v) for v in m], OP("cm")))
    b = x.get("/BBox")
    if b is not None:
        x0, y0, x1, y1 = (float(v) for v in b)
        out += [CSI([x0, y0, x1 - x0, y1 - y0], OP("re")), CSI([], OP("W")), CSI([], OP("n"))]
    for c in pikepdf.parse_content_stream(x):
        if isinstance(c, pikepdf.ContentStreamInlineImage): out.append(c); continue
        op = str(c.operator); a = list(c.operands)
        if a:
            if op == "Tf": a[0] = ren("/Font", a[0])
            elif op == "Do": a[0] = ren("/XObject", a[0])
            elif op == "gs": a[0] = ren("/ExtGState", a[0])
            elif op in ("cs", "CS") and str(a[0]) not in DEVICE: a[0] = ren("/ColorSpace", a[0])
            elif op in ("scn", "SCN") and isinstance(a[-1], Name): a[-1] = ren("/Pattern", a[-1])
            elif op == "sh": a[0] = ren("/Shading", a[0])
            elif op in ("BDC", "DP") and len(a) > 1 and isinstance(a[1], Name): a[1] = ren("/Properties", a[1])
        out.append(CSI(a, OP(op)))
    out.append(CSI([], OP("Q")))
    for (cat, old), new in rename.items():
        if cat not in page_res: page_res[cat] = Dictionary()
        page_res[cat]["/" + new] = fres[cat][old]
    return out


def flatten_page(pdf, page, cache, counter):
    """Flatten the Forms of one page. Returns how many Do operators were replaced."""
    res = inherited(page.obj, "/Resources")
    if res is None or "/XObject" not in res: return 0
    if not any(is_flattenable(x, res, cache) for x in res["/XObject"].values()): return 0     # cheap exit: no content stream is parsed
    if "/Resources" not in page.obj:                                                           # inherited: give the page its own copy before adding names
        page.obj.Resources = pdf.make_indirect(Dictionary({k: v for k, v in res.items()})); res = page.obj.Resources
    total = 0
    for _ in range(MAX_DEPTH):
        xo = res.get("/XObject") or {}; out = []; n = 0; stack = []
        for c in pikepdf.parse_content_stream(page):
            if isinstance(c, pikepdf.ContentStreamInlineImage): out.append(c); continue
            op = str(c.operator); a = c.operands
            if op in ("BDC", "BMC"): stack.append(bool((op == "BDC" and len(a) > 1 and isinstance(a[1], Dictionary) and "/MCID" in a[1]) or (a and str(a[0]) == "/Artifact")))
            elif op == "EMC" and stack: stack.pop()
            if op == "Do" and a and not any(stack):                   # only untagged content: never inside an MCID or an Artifact
                x = xo.get(str(a[0]))
                if x is not None and is_flattenable(x, res, cache):
                    out += _expand(res, x, counter); n += 1; continue
            out.append(c)
        if not n: break
        page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out)); total += n
    return total


def flatten_all(pdf, acts, log):
    cache, counter, tot, pages = {}, [0], 0, 0
    for i, page in enumerate(pdf.pages):
        try: n = flatten_page(pdf, page, cache, counter)
        except Exception as ex:
            acts.append(dict(action="Form XObject could not be flattened", status="failed", page=i + 1, detail=f"{type(ex).__name__}: {ex}: its text/images stay hidden from assistive technology")); continue
        if n:
            tot += n; pages += 1
            acts.append(dict(action="Form XObject flattened into the page", status="needs_review", page=i + 1,
                             detail=f"{n} form(s) with text or images were merged into the page content so they can be tagged; compare the page with the original"))
    if tot: log.append(f"flattened {tot} form XObject(s) on {pages} page(s)")
    return tot
