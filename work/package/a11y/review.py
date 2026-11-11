"""Human verification of a repaired PDF.

Automated tools cannot decide whether alt text is *accurate*, whether the reading order is *logical* or whether a
derived title is *right*. This module turns every "needs review" result of a repair into a checklist that a person
works through. It does four things:

  1. builds the checklist from the validation report (fix actions + validator findings + manual checks + external tools)
  2. records a decision per item: confirmed / edited / rejected (with a note and the reviewer's name)
  3. applies the reviewer's edits (figure alt text, document title, document language) to the repaired PDF
  4. produces a sign-off that is bound to the SHA-256 of the exact file that was reviewed

It never turns a failure into a pass. A sign-off is only possible when every item is resolved AND the independent
validator reports no failures. Any later change to the file or to a decision withdraws the sign-off.
This is a record of human review, not a PDF/UA certification.

Everything here is pure data / PDF editing; file I/O for the state lives in app.py (it has the Windows-safe helpers).
"""
import collections, datetime, hashlib, os, re, time
import pikepdf
from pikepdf import Name
import engine, structure, validate

DECISIONS = ("pending", "confirmed", "rejected", "edited")
EDIT_LIMITS = {"alt": 500, "title": 200, "lang": 35}            # editable item kinds -> max characters
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
NO_BULK = {"manual", "external"}                                # these must be ticked one by one
NO_BULK_CHECKS = {"fig.quality"}                                # image descriptions are always checked one by one
NEEDS_NOTE = {"external"}                                       # confirming an external-tool result requires a note (tool, version, result)
LOCK_PLACEHOLDER = "A placeholder description cannot be confirmed: write what the image shows, or press “Mark as decorative” if it carries no information."
MAX_NOTE = 1000
DECORATIVE = "[decorative]"                                     # alt-text edit value meaning: this image is decorative, take it out of the reading order

STATUS_TEXT = {
    "verified": "✔ Verified by a human reviewer",
    "ready": "Ready for sign-off",
    "in_progress": "Human review in progress",
}


def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
def blank(): return dict(items={}, reviewer="", signoff=None, log=[])


# ---------------------------------------------------------------- 1. the checklist
def items_from_report(rep):
    """Deterministic list of everything a person must look at. Ids are stable for the same report."""
    out, seen = [], collections.Counter()

    def add(source, group, title, detail, page=None, bbox=None, ref=None, check=None, locked=None):
        base = hashlib.sha1("|".join(str(x) for x in (source, check or group, page, detail)).encode("utf8")).hexdigest()[:10]
        n = seen[base]; seen[base] += 1                                  # identical items on the same page get a suffix
        kind = (ref or {}).get("kind")
        out.append(dict(id=base if n == 0 else f"{base}{n}", source=source, group=group, title=title, detail=detail or "",
                        page=page, bbox=bbox, ref=ref, check=check, editable=kind if kind in EDIT_LIMITS else None, locked=locked,
                        needs_note=source in NEEDS_NOTE))

    for a in rep.get("actions", []):                                    # things the fixer did that a person must confirm
        if a.get("status") == "needs_review":
            ref = a.get("ref"); add("fix", a["action"], a["action"], a.get("detail"), a.get("page"), (ref or {}).get("bbox"), ref,
                                    locked=LOCK_PLACEHOLDER if a["action"] == "Placeholder alt text inserted" else None)
    A = rep["after"]
    for c in A["checks"]:                                               # things the validator could not decide
        if c["status"] != validate.REVIEW: continue
        manual = c["id"].startswith("man.")
        for f in c["findings"]:
            if f["status"] != validate.REVIEW: continue
            add("manual" if manual else "check", "Manual checklist" if manual else c["title"], c["title"], f["msg"], f.get("page"), f.get("bbox"), None, c["id"],
                locked=LOCK_PLACEHOLDER if c["id"] == "fig.quality" and "placeholder" in f["msg"].lower() else None)
    for x in A.get("external", []):                                     # PAC etc.: a person runs them and records the result
        if x["status"] == validate.PENDING:
            add("external", "External validators", x["tool"], x["msg"], None, None, None, "ext." + x["tool"])
    return out


def merged(rep, state):
    items = items_from_report(rep); st = state.get("items", {})
    for it in items:
        s = st.get(it["id"], {})
        it.update(decision=s.get("decision", "pending"), note=s.get("note", ""), value=s.get("value"), by=s.get("by"), t=s.get("t"), applied=bool(s.get("applied")))
        it["resolved"] = it["decision"] == "confirmed" or (it["decision"] == "edited" and it["applied"])
    return items


def summary(items, rep, state):
    cnt = collections.Counter(i["decision"] for i in items)
    unapplied = sum(1 for i in items if i["decision"] == "edited" and not i["applied"])
    resolved = sum(1 for i in items if i["resolved"]); failed = rep["after"]["totals"]["failed"]
    blockers = []
    todo = cnt["pending"]
    if todo: blockers.append(f"{todo} item(s) not reviewed yet")
    if cnt["rejected"]: blockers.append(f"{cnt['rejected']} rejected item(s): correct them (edit here, or fix the source document and run the tool again) or change your decision")
    if unapplied: blockers.append(f"{unapplied} edit(s) not applied yet: press “Apply edits & re-check”")
    if failed: blockers.append(f"{failed} automated failure(s) remain in the repaired PDF")
    sig = state.get("signoff"); signed = bool(sig and sig.get("sha256") == rep["after"]["sha256"])
    status = "verified" if signed and not blockers else "ready" if not blockers else "in_progress"
    return dict(total=len(items), resolved=resolved, pending=todo, confirmed=cnt["confirmed"], edited=cnt["edited"], rejected=cnt["rejected"],
                unapplied=unapplied, failures=failed, blockers=blockers, can_signoff=not blockers, signed=signed and not blockers,
                status=status, status_text=STATUS_TEXT[status], reviewer=state.get("reviewer", ""))


def groups_of(items):
    g = collections.OrderedDict()
    for i in items:
        e = g.setdefault(i["group"], dict(group=i["group"], source=i["source"], total=0, pending=0, bulk=i["source"] not in NO_BULK))
        e["total"] += 1; e["pending"] += i["decision"] == "pending"
        if i["editable"] == "alt" or i["locked"] or i["check"] in NO_BULK_CHECKS: e["bulk"] = False   # alt text accuracy is the check most worth doing one by one
    return list(g.values())


def human_section(rep, state):
    """Block added to the downloadable JSON / HTML report."""
    items = merged(rep, state); sm = summary(items, rep, state)
    keep = ("id", "source", "group", "title", "detail", "page", "decision", "note", "value", "by", "t", "applied", "editable", "locked")
    return dict(status=sm["status"], status_text=sm["status_text"], summary=sm, signoff=state.get("signoff") if sm["signed"] else None,
                started=bool(state.get("items") or state.get("signoff")), items=[{k: i[k] for k in keep} for i in items], log=state.get("log", [])[-500:])


# ---------------------------------------------------------------- 2. validating a reviewer's edit
def clean_value(kind, value):
    v = " ".join(str(value or "").split())
    if not v: raise ValueError("The text is empty")
    if kind == "alt" and v.lower() == DECORATIVE: return DECORATIVE
    if len(v) > EDIT_LIMITS[kind]: raise ValueError(f"Too long (max {EDIT_LIMITS[kind]} characters)")
    if kind == "alt":
        if validate.GENERIC_ALT.fullmatch(v): raise ValueError(f"“{v}” is a generic word or file name: describe what the image shows")
        if validate.PLACEHOLDER.search(v): raise ValueError("That still looks like a placeholder: write the real description")
    if kind == "lang" and not LANG_RE.match(v): raise ValueError("Use a language code such as en, fr, de, pt-BR")
    return v


# ---------------------------------------------------------------- 3. applying edits to the repaired PDF
def _replace(tmp, dst):
    for k in range(8):                                                  # Windows refuses to replace a file another thread has open
        try: return os.replace(tmp, dst)
        except PermissionError: time.sleep(0.05 * (k + 1))
    os.replace(tmp, dst)


def _figure_index(pdf):
    """(1-based page, MCID) -> Figure structure element."""
    idx = {}; root = pdf.Root
    if "/StructTreeRoot" not in root: return idx
    rm = engine.rolemap(root.StructTreeRoot); pidx = {p.obj.objgen: i for i, p in enumerate(pdf.pages)}

    def visit(el, depth, pg):
        if pg is not None and pg.objgen in pidx and engine.role_of(el, rm) == "Figure":
            for m in engine.mcids_of(el): idx[(pidx[pg.objgen] + 1, m)] = el
    engine.walk(root.StructTreeRoot.get("/K"), rm, visit)
    return idx


def current_values(path, items):
    """What is in the repaired PDF right now for the editable items (so the reviewer edits the real text, not a 140-char excerpt)."""
    want = [i for i in items if i["editable"]]
    if not want: return {}
    out = {}
    with pikepdf.open(path) as pdf:
        root = pdf.Root; fig = _figure_index(pdf) if any(i["editable"] == "alt" for i in want) else {}
        for i in want:
            k = i["editable"]
            if k == "title": out[i["id"]] = str(pdf.docinfo.get("/Title", ""))
            elif k == "lang": out[i["id"]] = str(root.get("/Lang", ""))
            elif k == "alt":
                r = i.get("ref") or {}; el = fig.get((r.get("page"), r.get("mcid")))
                out[i["id"]] = str(el.get("/Alt", "")) if el is not None else None
    return out


def apply_edits(path, edits):
    """edits: [dict(id, kind, ref, value)]. Returns {id: (ok, message)}. The PDF is replaced atomically, only if something changed."""
    res, tmp, changed = {}, path + ".edit", False
    with pikepdf.open(path) as pdf:
        root = pdf.Root; fig = None
        for e in edits:
            try:
                k, v = e["kind"], e["value"]
                if k == "alt":
                    if fig is None: fig = _figure_index(pdf)
                    r = e.get("ref") or {}; el = fig.get((r.get("page"), r.get("mcid")))
                    if el is None: raise LookupError("Could not find that figure in the repaired PDF")
                    if v == DECORATIVE:
                        structure.figure_to_artifact(pdf, el, int(r["page"]) - 1); fig = None          # tree and page content changed: index is stale
                    else: el.Alt = pikepdf.String(v)
                elif k == "title":
                    pdf.docinfo["/Title"] = pikepdf.String(v)
                    _write_xmp(pdf, v, str(root.get("/Lang", "en")) or "en")
                elif k == "lang":
                    root.Lang = pikepdf.String(v)
                    _write_xmp(pdf, str(pdf.docinfo.get("/Title", "")), v)
                else: raise LookupError("Not an editable item")
                res[e["id"]] = (True, "applied"); changed = True
            except Exception as ex: res[e["id"]] = (False, f"{type(ex).__name__}: {ex}")
        if changed: pdf.save(tmp, object_stream_mode=pikepdf.ObjectStreamMode.disable)
    if changed: _replace(tmp, path)
    return res


def _write_xmp(pdf, title, lang):
    ms = pdf.make_stream(engine.xmp_packet(title, lang).encode("utf8")); ms.Type = Name.Metadata; ms.Subtype = Name.XML; pdf.Root.Metadata = ms
