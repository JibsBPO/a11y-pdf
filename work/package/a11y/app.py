import os, io, time, uuid, json, hmac, base64, shutil, asyncio, datetime, threading, collections, contextlib
import pymupdf, pikepdf
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, Response, JSONResponse
import engine, validate, report, review
from rules import RULES

HERE = os.path.dirname(os.path.abspath(__file__))
T = os.environ.get("DATA_DIR") or os.path.join(HERE, "tmp"); os.makedirs(T, exist_ok=True)
env = lambda k, d: float(os.environ.get(k, d))
MAX_BYTES = int(env("MAX_UPLOAD_MB", 200) * 1024 * 1024)      # upload size limit (v3 default was 25 MB)
MAX_PAGES = int(env("MAX_PAGES", 1000))                         # v3 default was 300; both limits answer HTTP 413 when exceeded
TTL_SECONDS = int(env("RETENTION_MINUTES", 60) * 60)           # files are deleted after this
MAX_JOBS = int(env("MAX_JOBS", 2))                              # repairs running at once (OCR/rendering are CPU heavy)
RATE_PER_MIN = int(env("RATE_LIMIT_PER_MIN", 20))               # uploads + repairs per client IP per minute
PASSWORD = os.environ.get("APP_PASSWORD", "")                   # set to require HTTP Basic auth (any username)
TRUST_PROXY = os.environ.get("TRUST_PROXY", "") == "1"          # read client IP from X-Forwarded-For (only behind your own proxy)

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
_slots = threading.BoundedSemaphore(MAX_JOBS); _waiting = [0]; _lock = threading.Lock()
_hits = collections.defaultdict(collections.deque)
_review_lock = threading.Lock()
CHUNK = 1 << 20
OVERHEAD = 1 << 20                                              # multipart framing allowed on top of MAX_BYTES
DRAIN_MAX = min(4 * MAX_BYTES, 2 << 30)                         # never read more than this from a client we are refusing


async def _janitor():
    while True:
        await asyncio.sleep(300)
        with contextlib.suppress(Exception): await asyncio.get_running_loop().run_in_executor(None, purge)

@contextlib.asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(_janitor()); yield; task.cancel()

app = FastAPI(title="a11y workbench", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def client_ip(request):
    if TRUST_PROXY and request.headers.get("x-forwarded-for"): return request.headers["x-forwarded-for"].split(",")[0].strip()
    return request.client.host if request.client else "?"

@app.middleware("http")
async def guard(request: Request, call_next):
    early = None
    if request.url.path != "/healthz":
        if PASSWORD:
            ok = False
            try: ok = hmac.compare_digest(base64.b64decode(request.headers.get("authorization", "")[6:]).decode().partition(":")[2], PASSWORD)
            except Exception: pass
            if not ok: early = Response("Authentication required", 401, headers={"WWW-Authenticate": 'Basic realm="a11y workbench"'})
        if early is None and request.method == "POST" and request.url.path.startswith("/api/") and RATE_PER_MIN > 0:
            # uploads, repairs and re-checks are expensive -> RATE_LIMIT_PER_MIN. Review clicks (confirm/reject/edit) are cheap -> 20x that, so a reviewer is never throttled.
            pth = request.url.path; heavy = pth == "/api/analyze" or pth.startswith("/api/fix/") or pth.endswith("/apply")
            q = _hits[(client_ip(request), heavy)]; now = time.time(); cap = RATE_PER_MIN if heavy else RATE_PER_MIN * 20
            while q and now - q[0] > 60: q.popleft()
            if len(q) >= cap: early = JSONResponse({"detail": "Too many requests: slow down"}, 429, headers={"Retry-After": "30"})
            else: q.append(now)
        if early is None and request.method == "POST" and request.url.path == "/api/analyze":
            cl = request.headers.get("content-length", "")
            if not cl.isdigit(): early = JSONResponse({"detail": "Upload must declare its size (Content-Length)"}, 411)
            elif int(cl) > MAX_BYTES + OVERHEAD:
                # Refuse BEFORE the server spools the whole file to disk. Drain (discard) the body first: answering while the browser is still
                # sending makes browsers report "Failed to fetch" / connection reset instead of showing this message.
                drained = 0
                async for chunk in request.stream():
                    drained += len(chunk)
                    if drained > DRAIN_MAX: break
                early = JSONResponse({"detail": f"File too large: {int(cl) / 1048576:.0f} MB sent, limit is {MAX_BYTES // 1048576} MB", "limit_mb": MAX_BYTES // 1048576}, 413)
    r = early if early is not None else await call_next(request)
    r.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY", "Content-Security-Policy": CSP})
    return r


# ---------------------------------------------------------------- files / sessions
def path(sid, name, ext="pdf"):
    if not sid.isalnum() or len(sid) > 32: raise HTTPException(400, "bad session id")
    return os.path.join(T, f"{sid}_{name}.{ext}")

def need(sid, name="orig", ext="pdf"):
    p = path(sid, name, ext)
    if not os.path.exists(p): raise HTTPException(404, "session expired or not found; upload the file again")
    return p

def purge(force_sid=None):
    """Delete expired session files (and everything for force_sid)."""
    now = time.time()
    for f in os.listdir(T):
        fp = os.path.join(T, f)
        try:
            if (force_sid and f.startswith(force_sid + "_")) or now - os.path.getmtime(fp) > TTL_SECONDS: os.remove(fp)
        except OSError: pass

def stamp(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
def jwrite(p, obj):
    """Atomic write: pollers never see half a file. Retries because Windows refuses to replace a file another thread has open."""
    tmp = p + ".w"
    with open(tmp, "w", encoding="utf8") as f: json.dump(obj, f)
    for k in range(8):
        try: return os.replace(tmp, p)
        except PermissionError: time.sleep(0.05 * (k + 1))
    os.replace(tmp, p)
def jread(p):
    for k in range(8):
        try:
            with open(p, encoding="utf8") as f: return json.load(f)
        except (PermissionError, json.JSONDecodeError): time.sleep(0.05 * (k + 1))
    with open(p, encoding="utf8") as f: return json.load(f)


@app.get("/healthz")
def healthz(): return dict(ok=True)


@app.get("/api/config")
def config():
    return dict(max_upload_mb=MAX_BYTES // 1048576, max_pages=MAX_PAGES, retention_minutes=TTL_SECONDS // 60, ocr=bool(shutil.which("tesseract")),
                ai_alt=bool(os.environ.get("ANTHROPIC_API_KEY")), verapdf=bool(os.environ.get("VERAPDF") or shutil.which("verapdf")))


def _drop(*paths):
    for p in paths:
        with contextlib.suppress(OSError): os.remove(p)


def _analyze_upload(file):
    """Runs in a worker thread: the event loop must stay free to answer other requests while a big file is processed."""
    purge()
    sid = uuid.uuid4().hex[:12]; up, orig = path(sid, "upload", "bin"), path(sid, "orig")
    try:
        n = 0
        with open(up, "wb") as out:                                        # copy in 1 MB pieces: a 200 MB PDF is never held in RAM
            while True:
                chunk = file.file.read(CHUNK)
                if not chunk: break
                n += len(chunk)
                if n > MAX_BYTES: raise HTTPException(413, f"File too large (limit {MAX_BYTES // 1048576} MB)")
                out.write(chunk)
        if n == 0: raise HTTPException(400, "Empty file")
        with open(up, "rb") as f: head = f.read(1024)
        if b"%PDF-" in head:                                               # decide by content, not by file name
            try:
                with pikepdf.open(up) as p: pages = len(p.pages)
            except pikepdf.PasswordError: raise HTTPException(422, "Password-protected PDFs are not supported; remove the password first")
            except Exception: raise HTTPException(422, "This does not look like a valid PDF")
            if pages > MAX_PAGES: raise HTTPException(413, f"Too many pages ({pages}; limit {MAX_PAGES})")
            os.replace(up, orig)
        else:                                                               # page image / scan -> single-page PDF
            try:
                from PIL import Image
                Image.MAX_IMAGE_PIXELS = 60_000_000
                with Image.open(up) as im:
                    im.load(); buf = io.BytesIO(); im.convert("RGB").save(buf, "PNG")
                pdf_bytes = pymupdf.open(stream=buf.getvalue(), filetype="png").convert_to_pdf()
            except Exception: raise HTTPException(415, "Unsupported file: upload a PDF, PNG or JPG")
            with open(orig, "wb") as f: f.write(pdf_bytes)
            _drop(up)
        jwrite(path(sid, "meta", "json"), dict(name=os.path.basename(file.filename or "document.pdf"), bytes=os.path.getsize(orig), t=stamp()))
        try: res = engine.analyze(orig)
        except Exception: raise HTTPException(422, "The PDF could not be analysed (it may be damaged)")
        return dict(sid=sid, name=file.filename, bytes=os.path.getsize(orig), **res)
    except BaseException:
        purge(sid); _drop(up, orig); raise


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    try: return await run_in_threadpool(_analyze_upload, file)
    finally: await file.close()


# ---------------------------------------------------------------- repair job (runs in a thread; the browser polls /api/job)
STEPS = ["Built-in analysis (original)", "Independent validation (original)", "Auto-fix", "Built-in analysis (repaired)", "Independent validation (repaired file re-opened from disk)", "Writing report"]
STEPS_RECHECK = ["Applying your edits to the repaired PDF", "Built-in analysis (repaired)", "Independent validation (repaired file re-opened from disk)", "Writing report"]


class Stepper:
    """Runs named steps, publishing progress to the job file the browser polls, and keeps the audit log."""
    def __init__(self, jp, job, names, audit): self.jp, self.job, self.names, self.audit = jp, job, names, audit
    def __call__(self, i, fn):
        job = self.job; job["steps"][i]["state"] = "running"; job["pct"] = int(100 * i / len(self.names)); jwrite(self.jp, job); t0 = time.perf_counter()
        try: r = fn()
        except Exception: job["steps"][i]["state"] = "failed"; raise
        dt = time.perf_counter() - t0; job["steps"][i].update(state="done", seconds=round(dt, 1)); self.audit.append(dict(t=stamp(), step=self.names[i], detail=f"{dt:.1f}s")); return r


def _queue():
    with _lock: _waiting[0] += 1
    _slots.acquire()
    with _lock: _waiting[0] -= 1


def run_job(sid, lang, ocr_lang, title):
    jp = path(sid, "job", "json"); orig, fixed = path(sid, "orig"), path(sid, "fixed"); meta = jread(path(sid, "meta", "json"))
    job = jread(jp)                                          # created by fix() before this thread started
    audit = [dict(t=meta["t"], step="Upload", detail=f"{meta['name']} ({meta['bytes']} bytes)")]
    _queue(); step = Stepper(jp, job, STEPS, audit)
    try:
        job["state"] = "running"
        eb = step(0, lambda: engine.analyze(orig))
        before = step(1, lambda: validate.run(orig, external=False))
        r = step(2, lambda: engine.fix(orig, fixed, lang or None, True, ocr_lang, title or None))
        ea = step(3, lambda: engine.analyze(fixed))
        after = step(4, lambda: validate.run(fixed, original=orig))
        def write():
            rep = report.build(meta["name"], before, after, r, audit, eb, ea)
            jwrite(path(sid, "report", "json"), rep); jwrite(path(sid, "result", "json"), dict(sid=sid, result=r, after=ea)); return rep
        step(5, write); job.update(state="done", pct=100)
    except Exception as ex:
        detail=" ".join(str(ex).split())[:500]
        job.update(state="error", error=f"Repair or validation failed ({type(ex).__name__}). {detail or 'No further details were provided.'}")
    finally:
        _slots.release(); jwrite(jp, job)


def run_recheck(sid):
    """Apply the reviewer's edits to the repaired PDF, then validate it again from scratch."""
    jp = path(sid, "job", "json"); orig, fixed = path(sid, "orig"), path(sid, "fixed"); job = jread(jp); audit = []
    _queue(); step = Stepper(jp, job, STEPS_RECHECK, audit)
    try:
        job["state"] = "running"
        def apply():
            with _review_lock:
                state = review_state(sid); rep = jread(path(sid, "report", "json")); items = review.merged(rep, state)
                todo = [dict(id=i["id"], kind=i["editable"], ref=i["ref"], value=i["value"]) for i in items if i["decision"] == "edited" and not i["applied"] and i["value"]]
                res = review.apply_edits(fixed, todo) if todo else {}
                for iid, (ok, msg) in res.items():
                    e = state["items"][iid]
                    if ok: e["applied"] = True
                    else: e["decision"] = "pending"; e["value"] = None; e["note"] = (e.get("note", "") + f" [edit could not be applied: {msg}]").strip()
                    audit.append(dict(t=stamp(), step="Edit " + ("applied" if ok else "FAILED"), detail=f"{iid}: {msg}"))
                state["signoff"] = None; state["log"].append(dict(t=stamp(), who=state.get("reviewer", ""), what=f"{sum(1 for v in res.values() if v[0])} edit(s) applied; sign-off withdrawn until the re-check is reviewed"))
                review_save(sid, state)
        step(0, apply)
        ea = step(1, lambda: engine.analyze(fixed))
        after = step(2, lambda: validate.run(fixed, original=orig))
        def write():
            rep = report.rebuild(jread(path(sid, "report", "json")), after, ea, audit)
            jwrite(path(sid, "report", "json"), rep); old = jread(path(sid, "result", "json")); jwrite(path(sid, "result", "json"), dict(sid=sid, result=old["result"], after=ea)); return rep
        step(3, write); job.update(state="done", pct=100)
    except Exception as ex:
        job.update(state="error", error=f"Re-check failed ({type(ex).__name__}). Your decisions are saved; try again.")
    finally:
        _slots.release(); jwrite(jp, job)


@app.post("/api/fix/{sid}", status_code=202)
def fix(sid: str, lang: str = Form(None), ocr_lang: str = Form("eng"), title: str = Form(None)):
    need(sid); jp = path(sid, "job", "json")
    if os.path.exists(jp) and jread(jp)["state"] in ("queued", "running"): raise HTTPException(409, "A repair is already running for this file")
    if _waiting[0] >= MAX_JOBS * 4: raise HTTPException(429, "Server busy: try again in a minute")
    if not (ocr_lang.replace("+", "").isalpha() and len(ocr_lang) <= 20): raise HTTPException(400, "bad OCR language")
    _drop(path(sid, "review", "json"))                       # a new repair makes any earlier human review meaningless
    jwrite(jp, dict(state="queued", pct=0, steps=[dict(name=n, state="waiting", seconds=None) for n in STEPS], started=stamp(), error=None))   # exists BEFORE we answer, so the first poll can't 404
    threading.Thread(target=run_job, args=(sid, lang, ocr_lang, title), daemon=True).start()
    return dict(sid=sid, state="queued")

@app.get("/api/job/{sid}")
def job(sid: str): return jread(need(sid, "job", "json"))

@app.get("/api/result/{sid}")
def result(sid: str):
    res = jread(need(sid, "result", "json")); res["report"] = jread(need(sid, "report", "json")); return res


@app.get("/api/validate/{sid}/{ver}")
def revalidate(sid: str, ver: str):
    """Re-run validation on demand (e.g. after installing veraPDF)."""
    return validate.run(need(sid, "fixed" if ver == "fixed" else "orig"), original=need(sid) if ver == "fixed" else None)

def _full_report(sid):
    rep = jread(need(sid, "report", "json")); rep["human"] = review.human_section(rep, review_state(sid)); return rep

@app.get("/api/report/{sid}.json")
def report_json(sid: str):
    return Response(report.to_json(_full_report(sid)), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="accessibility-report.json"'})

@app.get("/api/report/{sid}.html")
def report_html(sid: str):
    return Response(report.to_html(_full_report(sid)), media_type="text/html", headers={"Content-Disposition": 'attachment; filename="accessibility-report.html"'})

@app.get("/api/page/{sid}/{ver}/{n}.png")
def page(sid: str, ver: str, n: int, zoom: float = 1.5):
    with pymupdf.open(need(sid, "fixed" if ver == "fixed" else "orig")) as d:
        if not 1 <= n <= len(d): raise HTTPException(404, "no such page")
        zoom = min(max(zoom, 0.5), 3.0)
        return Response(d[n - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png"), media_type="image/png")


@app.get("/api/crop/{sid}/{ver}/{n}.png")
def crop(sid: str, ver: str, n: int, bbox: str, zoom: float = 2.0):
    """A close-up of one region of a page (used by the human review to show the image next to its alt text)."""
    try: x0, y0, x1, y1 = (float(v) for v in bbox.split(","))
    except ValueError: raise HTTPException(400, "bbox must be x0,y0,x1,y1")
    with pymupdf.open(need(sid, "fixed" if ver == "fixed" else "orig")) as d:
        if not 1 <= n <= len(d): raise HTTPException(404, "no such page")
        pg = d[n - 1]; r = (pymupdf.Rect(x0, y0, x1, y1).normalize() + (-6, -6, 6, 6)) & pg.rect
        if r.is_empty or r.width < 2 or r.height < 2: raise HTTPException(400, "empty region")
        zoom = min(max(zoom, 0.5), 4.0, 1600 / max(r.width, r.height))
        return Response(pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=r).tobytes("png"), media_type="image/png")

@app.get("/api/download/{sid}")
def dl(sid: str): return FileResponse(need(sid, "fixed"), filename="accessible.pdf", media_type="application/pdf")

@app.delete("/api/session/{sid}")
def delete_session(sid: str):
    path(sid, "x"); purge(sid); return dict(deleted=True)

# ---------------------------------------------------------------- human review
def review_state(sid):
    p = path(sid, "review", "json")
    if os.path.exists(p):
        with contextlib.suppress(Exception): return jread(p)
    return review.blank()

def review_save(sid, state): jwrite(path(sid, "review", "json"), state)

def _who(state, reviewer):
    r = " ".join((reviewer or "").split())[:80]
    if r: state["reviewer"] = r
    return state.get("reviewer", "")

FILTERS = dict(all=lambda i: True, todo=lambda i: not i["resolved"], pending=lambda i: i["decision"] == "pending", confirmed=lambda i: i["decision"] == "confirmed",
               rejected=lambda i: i["decision"] == "rejected", edited=lambda i: i["decision"] == "edited")

def _payload(sid, status="all", offset=0, limit=40, only=None):
    rep = jread(need(sid, "report", "json")); state = review_state(sid); items = review.merged(rep, state)
    sm = review.summary(items, rep, state); groups = review.groups_of(items)
    sel = [i for i in items if i["id"] in only] if only else [i for i in items if FILTERS.get(status, FILTERS["all"])(i)]
    chunk = sel[offset:offset + limit]
    cur = review.current_values(need(sid, "fixed"), chunk) if chunk else {}
    for i in chunk: i["current"] = cur.get(i["id"])
    return dict(items=chunk, total=len(sel), summary=sm, groups=groups, reviewer=state.get("reviewer", ""), signoff=state.get("signoff") if sm["signed"] else None)

def _log(state, who, what): state["log"].append(dict(t=stamp(), who=who, what=what))


@app.get("/api/review/{sid}")
def review_get(sid: str, status: str = "todo", offset: int = 0, limit: int = 40):
    need(sid, "report", "json")
    return _payload(sid, status, max(offset, 0), min(max(limit, 0), 200))


@app.post("/api/review/{sid}/item")
def review_item(sid: str, id: str = Form(...), decision: str = Form(...), note: str = Form(""), value: str = Form(None), reviewer: str = Form(None)):
    need(sid, "report", "json")
    if decision not in review.DECISIONS: raise HTTPException(400, "Unknown decision")
    with _review_lock:
        rep = jread(path(sid, "report", "json")); state = review_state(sid); it = next((i for i in review.merged(rep, state) if i["id"] == id), None)
        if it is None: raise HTTPException(404, "No such review item (the report changed: reload the list)")
        who = _who(state, reviewer); e = state["items"].setdefault(id, {})
        if decision == "confirmed" and it["locked"]: raise HTTPException(400, it["locked"])
        if decision == "confirmed" and it["needs_note"] and len(" ".join(note.split())) < 3: raise HTTPException(400, "Write a short note first: which tool and version you used, and what it reported (or why it was not run)")
        if decision == "edited":
            if not it["editable"]: raise HTTPException(400, "This item cannot be edited here: correct it in the source document or in Acrobat")
            try: v = review.clean_value(it["editable"], value)
            except ValueError as ex: raise HTTPException(422, str(ex))
            e.update(decision="edited", value=v, applied=False)
        else:
            if e.get("decision") == "edited" and not e.get("applied"): e["value"] = None      # an edit that was never applied is dropped
            e["decision"] = decision
        e.update(note=" ".join(note.split())[:review.MAX_NOTE], by=who, t=stamp())
        withdrew = state.get("signoff") is not None; state["signoff"] = None
        _log(state, who, f"{decision}: {it['title']}" + (f" (page {it['page']})" if it["page"] else "") + (" — sign-off withdrawn" if withdrew else ""))
        review_save(sid, state)
    return _payload(sid, only={id})


@app.post("/api/review/{sid}/bulk")
def review_bulk(sid: str, group: str = Form(...), note: str = Form(""), reviewer: str = Form(None)):
    """Confirm every not-yet-reviewed item of one group (e.g. 'Fonts substituted'). Never for alt text or the manual checklist."""
    need(sid, "report", "json")
    with _review_lock:
        rep = jread(path(sid, "report", "json")); state = review_state(sid)
        todo = [i for i in review.merged(rep, state) if i["group"] == group and i["decision"] == "pending"]
        if any(i["source"] in review.NO_BULK or i["editable"] == "alt" or i["locked"] or i["check"] in review.NO_BULK_CHECKS for i in todo): raise HTTPException(400, "Items in this group must be reviewed one by one")
        who = _who(state, reviewer)
        for i in todo: state["items"].setdefault(i["id"], {}).update(decision="confirmed", note=" ".join(note.split())[:review.MAX_NOTE] or "Confirmed in bulk", by=who, t=stamp())
        if todo: state["signoff"] = None; _log(state, who, f"bulk-confirmed {len(todo)} item(s) in “{group}”"); review_save(sid, state)
    return _payload(sid, "todo", 0, 40)


@app.post("/api/review/{sid}/apply", status_code=202)
def review_apply(sid: str):
    """Write the reviewer's edits into the repaired PDF and validate it again (a background job, polled like a repair)."""
    need(sid, "report", "json"); need(sid, "fixed"); jp = path(sid, "job", "json")
    if os.path.exists(jp) and jread(jp)["state"] in ("queued", "running"): raise HTTPException(409, "A job is already running for this file")
    if _waiting[0] >= MAX_JOBS * 4: raise HTTPException(429, "Server busy: try again in a minute")
    jwrite(jp, dict(state="queued", pct=0, steps=[dict(name=n, state="waiting", seconds=None) for n in STEPS_RECHECK], started=stamp(), error=None))
    threading.Thread(target=run_recheck, args=(sid,), daemon=True).start()
    return dict(sid=sid, state="queued")


@app.post("/api/review/{sid}/signoff")
def review_signoff(sid: str, reviewer: str = Form(...), agree: str = Form("")):
    need(sid, "report", "json")
    with _review_lock:
        rep = jread(path(sid, "report", "json")); state = review_state(sid); sm = review.summary(review.merged(rep, state), rep, state)
        name = " ".join(reviewer.split())[:80]
        if len(name) < 2: raise HTTPException(400, "Enter your name to sign off")
        if agree != "yes": raise HTTPException(400, "Tick the confirmation box to sign off")
        if not sm["can_signoff"]: raise HTTPException(409, "Cannot sign off yet: " + "; ".join(sm["blockers"]))
        state["reviewer"] = name
        state["signoff"] = dict(reviewer=name, t=stamp(), sha256=rep["after"]["sha256"], items=sm["total"],
                                statement="I reviewed every item in this checklist for this exact file. This is a record of my review, not a PDF/UA certification.")
        _log(state, name, f"signed off ({sm['total']} items, file {rep['after']['sha256'][:12]}…)"); review_save(sid, state)
    return _payload(sid, "all", 0, 0)


@app.delete("/api/review/{sid}/signoff")
def review_unsign(sid: str):
    need(sid, "report", "json")
    with _review_lock:
        state = review_state(sid)
        if state.get("signoff"): state["signoff"] = None; _log(state, state.get("reviewer", ""), "sign-off withdrawn"); review_save(sid, state)
    return _payload(sid, "all", 0, 0)


@app.get("/api/rules")
def rules(): return RULES

@app.get("/", response_class=HTMLResponse)
def index(): return open(os.path.join(HERE, "static", "index.html"), encoding="utf8").read()
