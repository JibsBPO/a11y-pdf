"""Embed fonts that a PDF uses but does not contain (PDF/UA 7.21.4.1, Matterhorn 31-001).

Three cases, from easiest to hardest:

1. Standard fonts (Helvetica, Arial, Times, Courier ...): replaced by a metric-compatible font (Liberation; on Windows Arial / Times New
   Roman / Courier New; DejaVu as a last resort), subset to the characters the encoding can address and embedded. Character codes in the
   page are unchanged. When the font's name says nothing about its style (an unknown family), the substitute follows the font's own
   descriptor flags and otherwise the family that is USED MOST in the document, so the result looks consistent.
2. Symbolic simple fonts (Symbol, ZapfDingbats, and any font that ships a /ToUnicode map): the code -> character table comes from the
   built-in Adobe encodings (Symbol, ZapfDingbats) or from the font's own ToUnicode map. A substitute that has those characters is
   embedded with a (3,0) symbolic cmap, so the page's codes still select the right glyphs, and a ToUnicode map is written.
3. Composite (Type0 / CID) fonts with Identity-H/V encoding and a ToUnicode map: the descendant font is replaced by an embedded
   CIDFontType2 whose CIDToGIDMap sends every CID to the glyph of the character its ToUnicode entry names. Page content is unchanged.

What stays impossible (reported as failed, never hidden): a font whose codes carry no information about the characters (no ToUnicode,
no standard encoding), and composite fonts with a CJK CMap other than Identity. Without the original font file nobody can know what
those codes mean.

Glyph shapes of a substitute differ from the lost original, so every substitution is "needs review" and validate.py's keep.visual
check compares the rendering with the original.
"""
import io, os, re, collections, functools
import pikepdf
from pikepdf import Name, Dictionary, Array

try:
    from fontTools import subset as ftsubset
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.tables._c_m_a_p import CmapSubtable
    from fontTools.agl import toUnicode
except ImportError:                      # pragma: no cover
    ftsubset = None
import fonts_data

WIN = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
DIRS = [os.environ.get("FONT_DIR", ""), "/usr/share/fonts/truetype/liberation", "/usr/share/fonts/truetype/liberation2", "/usr/share/fonts/liberation",
        "/usr/share/fonts/TTF", "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/truetype/noto", "/usr/share/fonts/noto",
        WIN, "/System/Library/Fonts/Supplemental", os.path.expanduser("~/Library/Fonts"), "/Library/Fonts", os.path.expanduser("~/.fonts")]
STYLE = {(0, 0): "", (1, 0): "-Bold", (0, 1): "-Italic", (1, 1): "-BoldItalic"}
FILES = {(f, b, i): f"Liberation{n}{'-Regular' if not (b or i) else STYLE[(b, i)]}"
         for f, n in (("sans", "Sans"), ("serif", "Serif"), ("mono", "Mono")) for b in (0, 1) for i in (0, 1)}
MSFILES = {(f, b, i): stem + ["", "bd", "i", "bi"][b + 2 * i] for f, stem in (("sans", "arial"), ("serif", "times"), ("mono", "cour")) for b in (0, 1) for i in (0, 1)}
DEJAVU = {(f, b, i): f"DejaVu{n}{'' if not (b or i) else '-' + ('Bold' if b else '') + ('Oblique' if i else '')}"
          for f, n in (("sans", "Sans"), ("serif", "Serif"), ("mono", "SansMono")) for b in (0, 1) for i in (0, 1)}
ANYCODE = ("NotoSans-Regular", "NotoSansSymbols2-Regular", "NotoSansMath-Regular", "NotoSansSymbols-Regular", "ArialUnicodeMS", "arialuni", "seguisym", "segoeui")
SYMBOLIC = re.compile(r"symbol|zapf|dingbat|wingding|webding", re.I)


def _find(stem):
    for d in DIRS:
        p = os.path.join(d, stem + ".ttf") if d else ""
        if p and os.path.exists(p): return p


def find_file(key): return _find(FILES[key])


@functools.lru_cache(maxsize=256)
def _license_ok(path):
    try: return not (TTFont(path)["OS/2"].fsType & 0x0002)          # bit 1 = restricted licence: embedding not allowed
    except Exception: return False


@functools.lru_cache(maxsize=64)
def _cmap(path):
    f = TTFont(path); cm = f.getBestCmap() or {}
    return {u: f.getGlyphID(n) for u, n in cm.items()}              # unicode -> glyph id


def candidates(key):
    """Substitute font files in order of preference: metric-compatible first, then broader coverage."""
    out = []
    for stem in (FILES[key], MSFILES[key], DEJAVU[key], *ANYCODE):
        p = _find(stem)
        if p and p not in out and _license_ok(p): out.append(p)
    return out


def pick_font(key, needed):
    """(path, missing unicodes). The first candidate that has every needed character; otherwise the one that has the most."""
    best = (None, set(needed))
    for p in candidates(key):
        have = _cmap(p); miss = {u for u in needed if u not in have}
        if not miss: return p, set()
        if best[0] is None or len(miss) < len(best[1]): best = (p, miss)
    return best


# ---------------------------------------------------------------- choosing the family
_KW_MONO = r"courier|mono|consolas|menlo|lucidaconsole|typewriter"
_KW_SERIF = r"times|serif|georgia|garamond|cambria|palatino|minion|bookman|century|baskerville|roman"
_KW_SANS = r"arial|helvet|sans|calibri|verdana|tahoma|segoe|roboto|open|gothic|myriad|frutiger|univers|lucida|trebuchet"


def classify(basefont, default="sans", flags=None):
    """(family, bold, italic). Order: the name -> the descriptor flags (fixed-pitch / serif bits) -> `default`, which fix() sets to the
    family that is used most in the document."""
    n = re.sub(r"^[A-Z]{6}\+", "", str(basefont).lstrip("/")).lower().replace(" ", "")
    if re.search(_KW_MONO, n): fam = "mono"
    elif re.search(_KW_SERIF, n) and "sans" not in n: fam = "serif"
    elif re.search(_KW_SANS, n): fam = "sans"
    elif flags is not None and flags & 1: fam = "mono"
    elif flags is not None and flags & 2: fam = "serif"
    elif flags is not None and flags & 32 and not flags & 4: fam = default
    else: fam = default
    return fam, int(bool(re.search(r"bold|black|heavy|demi", n)) or bool(flags is not None and flags & 262144)), int(bool(re.search(r"italic|oblique|slanted", n)) or bool(flags is not None and flags & 64))


def _recognised_family(font):
    """Family of a font if its name or flags say so, else None (used to count what the document mostly uses)."""
    d = font.get("/FontDescriptor"); flags = int(d.get("/Flags", 0)) if d is not None else None
    n = re.sub(r"^[A-Z]{6}\+", "", str(font.get("/BaseFont", "")).lstrip("/")).lower()
    if re.search(_KW_MONO, n) or (flags is not None and flags & 1): return "mono"
    if (re.search(_KW_SERIF, n) and "sans" not in n) or (flags is not None and flags & 2): return "serif"
    if re.search(_KW_SANS, n) or (flags is not None and flags & 32): return "sans"
    return None


def dominant_family(pdf, max_pages=60):
    """The font family (sans / serif / mono) that most characters of the document are set in (sample: first pages)."""
    cnt = collections.Counter()
    for page in list(pdf.pages)[:max_pages]:
        res = page.obj.get("/Resources"); fonts = res.get("/Font") if res is not None else None
        if not fonts: continue
        try: ins = list(pikepdf.parse_content_stream(page))
        except Exception: continue
        cur = None
        for c in ins:
            if isinstance(c, pikepdf.ContentStreamInlineImage): continue
            op = str(c.operator); a = c.operands
            if op == "Tf" and a: cur = fonts.get(str(a[0]))
            elif op in ("Tj", "TJ", "'", '"') and cur is not None and a:
                s = a[-1]; n = sum(len(bytes(x)) for x in s if isinstance(x, pikepdf.String)) if op == "TJ" else len(bytes(s))
                try: fam = _recognised_family(cur)
                except Exception: fam = None
                if fam: cnt[fam] += n
    return cnt.most_common(1)[0][0] if cnt else "sans"


# ---------------------------------------------------------------- code -> character tables
def code_to_unicode(font):
    """(code -> unicode char, encoding name to write, is_exact). Standard/unknown base encodings are approximated by WinAnsi."""
    enc = font.get("/Encoding"); base = "WinAnsiEncoding"; diffs = {}; exact = True
    if isinstance(enc, pikepdf.Name): base = str(enc).lstrip("/")
    elif isinstance(enc, pikepdf.Dictionary):
        base = str(enc.get("/BaseEncoding", "/WinAnsiEncoding")).lstrip("/"); cur = 0
        for it in (enc.get("/Differences") or []):
            if isinstance(it, int): cur = it
            else:
                diffs[cur] = str(it).lstrip("/"); cur += 1
    if base not in ("WinAnsiEncoding", "MacRomanEncoding"): base, exact = "WinAnsiEncoding", False
    codec = "cp1252" if base == "WinAnsiEncoding" else "mac_roman"; m = {}
    for c in range(32, 256):
        try: m[c] = bytes([c]).decode(codec)
        except UnicodeDecodeError: pass
    for c, nm in diffs.items():
        u = toUnicode(nm)
        if u: m[c] = u[0]
    return m, base, exact


GREEK_TEXT = {"Delta": "\u0394", "Omega": "\u03a9", "mu": "\u03bc"}


def _builtin(which):
    zapf = which == "zapf"; out = {}
    for item in (fonts_data.ZAPFDINGBATS if zapf else fonts_data.SYMBOL).split():
        c, n = item.split(":"); u = toUnicode(n, isZapfDingbats=zapf)
        if not zapf: u = GREEK_TEXT.get(n, u)                    # the AGL maps these to math symbols (increment, ohm, micro); in Symbol they are the Greek letters
        if u: out[int(c)] = u
    return out


def _u16(h):
    b = bytes.fromhex(h if len(h) % 4 == 0 else h.zfill(len(h) + (4 - len(h) % 4)))
    return b.decode("utf-16-be", "replace")


def parse_tounicode(data):
    """{code: string} from a ToUnicode CMap (bfchar and bfrange)."""
    t = data.decode("latin-1"); m = {}
    for blk in re.findall(r"beginbfchar(.*?)endbfchar", t, re.S):
        for a, b in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", blk):
            if b: m[int(a, 16)] = _u16(b)
    for blk in re.findall(r"beginbfrange(.*?)endbfrange", t, re.S):
        for lo, hi, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", blk):
            lo, hi = int(lo, 16), int(hi, 16)
            if hi < lo or hi - lo > 65535: continue
            if dst.startswith("["):
                for k, v in enumerate(re.findall(r"<([0-9A-Fa-f]*)>", dst)):
                    if v and lo + k <= hi: m[lo + k] = _u16(v)
            else:
                base = _u16(dst.strip("<>"))
                if base:
                    for k in range(hi - lo + 1): m[lo + k] = base[:-1] + chr(min(ord(base[-1]) + k, 0x10FFFF))
    return m


def tounicode_stream(pdf, code2uni, nbytes):
    lines = []; items = sorted(code2uni.items()); w = 2 * nbytes
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]; lines.append(f"{len(chunk)} beginbfchar")
        lines += [f"<{c:0{w}X}> <{u.encode('utf-16-be').hex().upper()}>" for c, u in chunk]; lines.append("endbfchar")
    cm = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
          "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n1 begincodespacerange\n<" + "0" * w + "> <" + "F" * w + ">\nendcodespacerange\n"
          + "\n".join(lines) + "\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n")
    return pdf.make_stream(cm.encode("ascii"))


# ---------------------------------------------------------------- building the embedded font
@functools.lru_cache(maxsize=64)
def _subset(path, unicodes, retain_gids=False):
    opt = ftsubset.Options(); opt.glyph_names = False; opt.notdef_outline = True; opt.layout_features = []
    opt.name_IDs = ["*"]; opt.name_legacy = True; opt.hinting = True; opt.legacy_kern = False; opt.retain_gids = retain_gids
    f = TTFont(path); s = ftsubset.Subsetter(opt); s.populate(unicodes=list(unicodes)); s.subset(f)
    buf = io.BytesIO(); f.save(buf); return buf.getvalue()


def _subset_symbolic(path, code2uni):
    """Subset and add a (3,0) symbolic cmap so that code c selects the glyph of its character (viewers use U+F000+c)."""
    f = TTFont(io.BytesIO(_subset(path, frozenset(ord(u[0]) for u in code2uni.values()))))
    cm = f.getBestCmap(); st = CmapSubtable.newSubtable(4); st.platformID = 3; st.platEncID = 0; st.language = 0
    st.cmap = {0xF000 + c: cm[ord(u[0])] for c, u in code2uni.items() if ord(u[0]) in cm}
    f["cmap"].tables.append(st); buf = io.BytesIO(); f.save(buf); return buf.getvalue()


@functools.lru_cache(maxsize=64)
def _metrics(path):
    f = TTFont(path); upm = f["head"].unitsPerEm; sc = 1000 / upm; cmap = f.getBestCmap(); hm = f["hmtx"]
    adv = {u: round(hm[g][0] * sc) for u, g in cmap.items() if g in hm.metrics}
    bb = f["head"]; os2 = f["OS/2"]
    return dict(adv=adv, bbox=[round(bb.xMin * sc), round(bb.yMin * sc), round(bb.xMax * sc), round(bb.yMax * sc)],
                asc=round(f["hhea"].ascent * sc), desc=round(f["hhea"].descent * sc), cap=round(getattr(os2, "sCapHeight", 0) * sc) or round(0.7 * 1000),
                angle=float(f["post"].italicAngle), ps=f["name"].getDebugName(6) or "Substitute")


def _descriptor(pdf, met, data, tag, flags, bold):
    ff = pdf.make_stream(data); ff.Length1 = len(data); ps = f"{tag}+{re.sub(r'[^A-Za-z0-9-]', '', met['ps'])}"
    return ps, pdf.make_indirect(Dictionary(Type=Name.FontDescriptor, FontName=Name("/" + ps), Flags=flags, FontBBox=Array(met["bbox"]), ItalicAngle=met["angle"],
                                            Ascent=met["asc"], Descent=met["desc"], CapHeight=met["cap"], StemV=140 if bold else 80, FontFile2=ff))


def _label(path): return os.path.splitext(os.path.basename(path))[0]
def _fail(acts, name, why): acts.append(dict(action=f"Font {name} not embedded", status="failed", page=None, detail=why))


def _missing_note(missing):
    if not missing: return ""
    return f"; {len(missing)} character(s) have no glyph in the substitute (e.g. U+{min(missing):04X}) and will show as an empty box"


# ---------------------------------------------------------------- case 3: composite (CID) fonts
def _fix_type0(pdf, obj, name, base, dom, tag, acts):
    enc = obj.get("/Encoding"); en = str(enc).lstrip("/") if isinstance(enc, pikepdf.Name) else None
    if en not in ("Identity-H", "Identity-V"):
        return _fail(acts, name, f"Composite font with {'the ' + en + ' CMap' if en else 'an embedded CMap'}: its codes cannot be mapped without the font file. Re-export from the source application")
    if "/ToUnicode" not in obj:
        return _fail(acts, name, "Composite font without a ToUnicode map: nothing says which character each code is. Re-export from the source application")
    cid2u = {c: u for c, u in parse_tounicode(obj.ToUnicode.read_bytes()).items() if u and c <= 0xFFFF}
    if not cid2u: return _fail(acts, name, "The font's ToUnicode map is empty or unreadable")
    dd = obj["/DescendantFonts"][0]; d = dd.get("/FontDescriptor"); flags = int(d.get("/Flags", 0)) if d is not None else None
    fam, bold, ital = classify(base, dom, flags); needed = {ord(u[0]) for u in cid2u.values()}
    path, missing = pick_font((fam, bold, ital), needed)
    if not path: return _fail(acts, name, "No substitute font found on this server (install fonts-liberation / fonts-dejavu or set FONT_DIR)")
    met = _metrics(path); gids = _cmap(path); data = _subset(path, frozenset(needed & set(gids)), True)
    maxc = max(cid2u); m2 = bytearray(2 * (maxc + 1))
    for c, u in cid2u.items():
        g = gids.get(ord(u[0]), 0); m2[2 * c:2 * c + 2] = g.to_bytes(2, "big")
    fl = 32 | (1 if fam == "mono" else 0) | (2 if fam == "serif" else 0) | (64 if ital else 0) | (262144 if bold else 0)
    ps, desc = _descriptor(pdf, met, data, tag, fl, bold)
    new = Dictionary(Type=Name.Font, Subtype=Name.CIDFontType2, BaseFont=Name("/" + ps), FontDescriptor=desc, CIDToGIDMap=pdf.make_stream(bytes(m2)),
                     CIDSystemInfo=dd.get("/CIDSystemInfo") if "/CIDSystemInfo" in dd else Dictionary(Registry=pikepdf.String("Adobe"), Ordering=pikepdf.String("Identity"), Supplement=0))
    for k in ("/DW", "/W", "/DW2", "/W2"):
        if k in dd: new[k] = dd[k]
    if "/W" not in dd:                                              # no widths in the file: use the substitute's, so text does not overlap
        w = Array()
        for c in sorted(cid2u): w.append(c); w.append(Array([met["adv"].get(ord(cid2u[c][0]), 1000)]))
        new.W = w
    obj.DescendantFonts = Array([pdf.make_indirect(new)]); obj.BaseFont = Name("/" + ps)
    acts.append(dict(action=f"Font {name} replaced by {_label(path)} (composite font)", status="needs_review", page=None,
                     detail=f"Characters were taken from the font's ToUnicode map; the original glyph shapes are lost and the substitute is the {fam} family"
                            + (" (the document's most used)" if fam == dom else "") + _missing_note(missing)))
    return True


# ---------------------------------------------------------------- case 2: symbolic simple fonts
def _fix_symbolic(pdf, obj, name, base, dom, tag, acts):
    code2u, src = {}, ""
    if "/ToUnicode" in obj:
        code2u = {c: u for c, u in parse_tounicode(obj.ToUnicode.read_bytes()).items() if u and c < 256}; src = "its ToUnicode map"
    if not code2u:
        if re.search(r"zapf|dingbat", name, re.I): code2u = _builtin("zapf"); src = "the Adobe ZapfDingbats encoding"
        elif re.search(r"^symbol|symbol$|symbolmt", name, re.I): code2u = _builtin("symbol"); src = "the Adobe Symbol encoding"
    if not code2u:
        return _fail(acts, name, "Symbolic font without a ToUnicode map or a known standard encoding: nothing says which glyph each code is. Re-export from the source application")
    d = obj.get("/FontDescriptor"); flags = int(d.get("/Flags", 0)) if d is not None else None
    fam, bold, ital = classify(base, dom, flags); needed = {ord(u[0]) for u in code2u.values()}
    path, missing = pick_font((fam, bold, ital), needed)
    if not path: return _fail(acts, name, "No substitute font found on this server (install fonts-dejavu or set FONT_DIR)")
    if len(missing) == len(needed): return _fail(acts, name, "No installed font has the characters this font needs (install fonts-dejavu or set FONT_DIR)")
    met = _metrics(path); data = _subset_symbolic(path, {c: u for c, u in code2u.items() if ord(u[0]) in _cmap(path)})
    first, last = (int(obj["/FirstChar"]), int(obj["/LastChar"])) if "/FirstChar" in obj and "/LastChar" in obj and "/Widths" in obj else (min(code2u), max(code2u))
    widths = obj["/Widths"] if "/Widths" in obj and first == int(obj.get("/FirstChar", -1)) else Array([met["adv"].get(ord(code2u[c][0]), 0) if c in code2u else 0 for c in range(first, last + 1)])
    ps, desc = _descriptor(pdf, met, data, tag, 4, bold)           # flag 4 = symbolic: the viewer uses the (3,0) cmap
    obj.Subtype = Name.TrueType; obj.BaseFont = Name("/" + ps); obj.FontDescriptor = desc
    obj.FirstChar = first; obj.LastChar = last; obj.Widths = widths
    if "/Encoding" in obj: del obj["/Encoding"]
    obj.ToUnicode = tounicode_stream(pdf, code2u, 1)
    acts.append(dict(action=f"Font {name} replaced by {_label(path)} (symbolic font)", status="needs_review", page=None,
                     detail=f"Characters were taken from {src}; glyph shapes differ from the original" + _missing_note(missing)))
    return True


# ---------------------------------------------------------------- the entry point
def _embedded_cids(desc, cidfont):
    """Return CIDs represented by a CID font's embedded program, or None if unknown."""
    subtype=str(cidfont.get("/Subtype"))
    key=("/FontFile2" if subtype=="/CIDFontType2" else "/FontFile3" if subtype=="/CIDFontType0" else None)
    if key not in desc: return None
    data=desc[key].read_bytes()
    font=None
    try:
        font=TTFont(io.BytesIO(data),recalcBBoxes=False,recalcTimestamp=False)
        if subtype=="/CIDFontType2":
            count=int(font["maxp"].numGlyphs)
            mapping=cidfont.get("/CIDToGIDMap",Name.Identity)
            if isinstance(mapping,pikepdf.Name):
                if str(mapping)!="/Identity": return None
                return set(range(count))
            raw=mapping.read_bytes()
            cids={0} if count else set()
            for cid in range(len(raw)//2):
                gid=int.from_bytes(raw[2*cid:2*cid+2],"big")
                if gid and gid<count: cids.add(cid)
            return cids
        if subtype=="/CIDFontType0" and "CFF " in font:
            top=font["CFF "].cff.topDictIndex[0]
            if not hasattr(top,"ROS"): return None
            cids=set()
            for glyph_name in top.charset:
                if glyph_name==".notdef": cids.add(0)
                elif isinstance(glyph_name,int): cids.add(glyph_name)
                else:
                    match=re.fullmatch(r"cid(\d+)",str(glyph_name),re.I)
                    if match: cids.add(int(match.group(1)))
                    else: return None
            return cids
        return None
    except Exception:
        return None
    finally:
        if font is not None: font.close()


def _cidset_bytes(cids):
    if not cids: return b"\x00"
    out=bytearray(max(cids)//8+1)
    for cid in cids: out[cid//8] |= 1 << (7-cid%8)
    return bytes(out)


def repair_cidsets(pdf, acts):
    """Correct existing CIDSet bitmaps using the embedded font and CID mapping.

    CIDSet is optional in PDF. This deliberately does not add one where absent;
    it only repairs a present bitmap when the font program can be inspected.
    """
    repaired=0; review=0
    for obj in list(pdf.objects):
        if not isinstance(obj,pikepdf.Dictionary) or obj.get("/Type")!=Name.Font: continue
        cidfont=obj
        if str(obj.get("/Subtype"))=="/Type0":
            descendants=obj.get("/DescendantFonts")
            if not descendants: continue
            cidfont=descendants[0]
        if str(cidfont.get("/Subtype")) not in ("/CIDFontType0","/CIDFontType2"): continue
        desc=cidfont.get("/FontDescriptor")
        if desc is None or "/CIDSet" not in desc: continue
        cids=_embedded_cids(desc,cidfont)
        if cids is None:
            review+=1
            acts.append(dict(action="Embedded CID font CIDSet needs review",status="needs_review",page=None,detail="Could not reliably enumerate CIDs from the embedded font program; existing CIDSet was preserved"))
            continue
        expected=_cidset_bytes(cids)
        actual=desc["/CIDSet"].read_bytes()
        if actual!=expected:
            desc.CIDSet=pdf.make_stream(expected)
            repaired+=1
    if repaired:
        acts.append(dict(action="Embedded CID font CIDSet rebuilt",status="succeeded",page=None,detail=f"Rebuilt {repaired} CIDSet bitmap(s) from embedded font glyphs and CID-to-glyph mappings"))
    return dict(repaired=repaired,needs_review=review)


def embed_missing(pdf, log, acts):
    """Replace every non-embedded font in `pdf` (in place). Returns the number of fonts embedded."""
    repair_cidsets(pdf,acts)
    if ftsubset is None:
        log.append("fontTools not installed: fonts not embedded"); return 0
    done = 0; tags = [0]; dom = []

    def D():
        if not dom: dom.append(dominant_family(pdf))              # only computed when a font actually needs a substitute
        return dom[0]

    def tag():
        tags[0] += 1; return "".join(chr(65 + (tags[0] // 26 ** k) % 26) for k in (5, 4, 3, 2, 1, 0))

    for obj in list(pdf.objects):
        try:
            if not isinstance(obj, pikepdf.Dictionary) or obj.get("/Type") != Name.Font: continue
            sub = str(obj.get("/Subtype")); d = obj.get("/FontDescriptor")
            if sub == "/Type3" or (d is not None and any(k in d for k in ("/FontFile", "/FontFile2", "/FontFile3"))): continue
            base = str(obj.get("/BaseFont", "/Unknown")); name = re.sub(r"^[A-Z]{6}\+", "", base.lstrip("/"))
            if sub == "/Type0":
                dd = obj["/DescendantFonts"][0]; df = dd.get("/FontDescriptor")
                if df is not None and any(k in df for k in ("/FontFile", "/FontFile2", "/FontFile3")): continue
                done += bool(_fix_type0(pdf, obj, name, base, D(), tag(), acts)); continue
            if sub not in ("/Type1", "/TrueType", "/MMType1"): continue
            flags = int(d.get("/Flags", 0)) if d is not None else 0
            if SYMBOLIC.search(name) or (flags & 4 and not flags & 32):
                done += bool(_fix_symbolic(pdf, obj, name, base, D(), tag(), acts)); continue
            fam, bold, ital = classify(base, D(), flags if d is not None else None); path, _ = pick_font((fam, bold, ital), {ord(c) for c in "AaEe .,0"})
            if not path:
                _fail(acts, name, "No substitute font found on this server (install fonts-liberation or set FONT_DIR)"); continue
            cmap, encname, exact = code_to_unicode(obj)
            path, missing = pick_font((fam, bold, ital), {ord(u) for u in cmap.values()})      # prefer a font that has every character the encoding addresses
            met = _metrics(path)
            data = _subset(path, frozenset(ord(u) for u in cmap.values()))
            first, last = (int(obj["/FirstChar"]), int(obj["/LastChar"])) if "/FirstChar" in obj and "/LastChar" in obj and "/Widths" in obj else (32, 255)
            if "/Widths" in obj and first == int(obj.get("/FirstChar", -1)): widths = obj["/Widths"]
            else: widths = Array([met["adv"].get(ord(cmap[c]), 0) if c in cmap else 0 for c in range(first, last + 1)])
            fl = 32 | (1 if fam == "mono" else 0) | (2 if fam == "serif" else 0) | (64 if ital else 0) | (262144 if bold else 0)
            ps, desc = _descriptor(pdf, met, data, tag(), fl, bold)
            obj.Subtype = Name.TrueType; obj.BaseFont = Name("/" + ps); obj.FontDescriptor = desc
            obj.FirstChar = first; obj.LastChar = last; obj.Widths = widths
            e = obj.get("/Encoding")
            if isinstance(e, pikepdf.Dictionary): e.BaseEncoding = Name("/" + encname)
            else: obj.Encoding = Name("/" + encname)
            done += 1
            label = _label(path); known = bool(re.search(_KW_MONO + "|" + _KW_SERIF + "|" + _KW_SANS, name.lower()))
            acts.append(dict(action=f"Font {name} embedded as {label}", status="needs_review", page=None,
                             detail=("Metric-compatible substitute; glyph shapes may differ slightly" if known else f"Unknown font name: the {fam} family was chosen from the font's flags or because it is the family used most in this document")
                                    + ("" if exact else " (non-standard encoding approximated as WinAnsi)") + _missing_note(missing)))
        except Exception as ex:
            acts.append(dict(action="A non-embedded font could not be processed", status="failed", page=None, detail=f"{type(ex).__name__}: {ex}"))
    if done: log.append(f"embedded {done} font(s)")
    return done
