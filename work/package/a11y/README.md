# A11y Workbench: PDF/UA + WCAG testing and remediation

Upload a PDF (or scan / PNG / JPG) → **Auto-fix** → the repaired PDF is re-opened from disk and checked by a separate
validator → a person works through the **Human review** checklist (confirm / edit / reject, then sign off) → download the repaired PDF and a before/after report that includes the human review.

Before Auto-fix, optionally enter a document title. If left blank, the repair uses the title already in the PDF, then a detected heading or the filename. The title is written to both the PDF Info dictionary and XMP metadata.

## 1. Run it on your computer

**Linux / macOS**
```bash
# one-time system packages
sudo apt install tesseract-ocr fonts-liberation      # Ubuntu/Debian.  macOS: brew install tesseract && brew install --cask font-liberation
./run.sh                                              # creates .venv, installs requirements, starts the server
```
Open **http://localhost:8000**.

**Windows (easiest):** double-click `run.bat` (needs Python 3.10+; it creates `.venv` and installs everything the first time), then open http://localhost:8000.

**Windows (manual, PowerShell)**
```powershell
# install Python 3.10+ and Tesseract (https://github.com/UB-Mannheim/tesseract/wiki), then:
py -m venv .venv ; .venv\Scripts\activate
pip install -r requirements.txt
$env:FONT_DIR="C:\path\to\liberation-fonts"           # Liberation fonts: https://github.com/liberationfonts/liberation-fonts/releases
uvicorn app:app
```
Without Tesseract, scanned PDFs cannot be OCRed. Without Liberation fonts, non-embedded fonts cannot be embedded
(both are reported as *Failed* actions, never hidden). Optional: `export ANTHROPIC_API_KEY=...` for AI-written alt text (always marked *needs review*).

## 2. Deploy it on a server (Docker + automatic HTTPS)
Needs a Linux server with Docker and a domain name pointing at it.
```bash
cp .env.example .env && nano .env          # set DOMAIN and a long random APP_PASSWORD
docker compose up -d --build
```
Open `https://<your domain>`: the browser asks for the password (any username). Caddy obtains the HTTPS certificate itself.
Uploads live only in RAM (`tmpfs`), the app container is read-only and not reachable except through Caddy.
Update: `git pull && docker compose up -d --build`. Logs: `docker compose logs -f app`.
> Docker files were written and syntax-checked but **not run** in the environment they were built in. Do a first test deploy before relying on them.

Without Docker: `APP_PASSWORD=... TRUST_PROXY=1 uvicorn app:app --host 127.0.0.1 --port 8000` behind nginx/Caddy that terminates HTTPS and limits bodies to ~30 MB.
Use **one** worker per process (job state is per process); scale by running more instances.

## 3. Large files (50 MB and above)
Default limits are **200 MB** and **1000 pages** per file (v3 was 25 MB / 300). Both answer HTTP **413** when exceeded, and the page now says which one.
Change them with `MAX_UPLOAD_MB` / `MAX_PAGES` (`.env`, or `set MAX_UPLOAD_MB=500` before starting on Windows). Every layer in front of the app has its own limit and they must agree:

| Layer | Setting |
|---|---|
| This app | `MAX_UPLOAD_MB`, `MAX_PAGES` |
| Caddy (docker-compose) | `MAX_BODY` in `.env` (a little above `MAX_UPLOAD_MB`) |
| nginx | `client_max_body_size 210m; proxy_read_timeout 900s; proxy_send_timeout 900s;` |
| Browser → server | none; the page reads the limits from `/api/config` and tells you *before* uploading if a file is too big |

How big files are handled: the upload is streamed to disk in 1 MB pieces (never held in RAM), refused early with a readable message if it is over the limit, analysed in a worker thread (the server keeps answering other requests), and pages are drawn only when you scroll near them. Rough resource needs: disk/RAM about 4x the file size per file in progress; repair time grows with page count (see section 8).
If the browser shows *"Cannot reach the server"* the app is not running (stopped, crashed or restarting): start it again and press **Retry upload**.

## 4. Human review (the Human review tab)
After Auto-fix the tab lists everything a machine cannot decide: AI/OCR alt text, placeholder alt text, derived title and language, inferred headings, substituted fonts, link/field descriptions, visual differences, the manual checklist (reading order, contrast, language changes, screen-reader test) and the external validators (PAC / veraPDF).
* **Confirm** (you checked it) · **Save my edit** (alt text, title, language: shown next to a close-up of the image) · **Reject** (it is wrong; blocks sign-off) · **Reset**. Notes and your name are recorded.
* Placeholder alt text can **not** be confirmed, only replaced. Alt text and the manual checklist can not be bulk-confirmed; other groups have *Confirm all N* (asks for confirmation, recorded in the log). PAC / veraPDF results need a note (tool, version, result, or why not run).
* **Apply edits & re-check** writes your edits into the repaired PDF and validates it again from scratch (background job with progress).
* **Sign off** needs every item resolved *and* zero automated failures. It is bound to the SHA-256 of the exact PDF; any later decision or edit withdraws it. The HTML/JSON reports contain the full review, reviewer, times and log.
* It is a record that a person looked, **not a PDF/UA certification**. Starting Auto-fix again discards the review for that file.

## 5. What the results mean
Every check ends as **Passed** (automated check ran, nothing wrong) · **Needs review** (a person must verify) · **Failed** (a concrete unresolved problem, with severity and page) ·
**Pending / Error** (external validator not run / a check crashed; never counted as a pass).
The tool never shows a bare "0 issues". Best verdict: *Passed*, meaning only "all automated checks passed". Reading order, alt-text accuracy,
contrast and screen-reader behaviour always remain manual.

* `engine.py` analyses and repairs. `validate.py` is separate: it checks the **saved** file from scratch, and the original with the same code, so before/after are comparable.
* **Fix actions** list everything the repair did as ✔ succeeded / ⚠ needs review (AI/OCR alt text, inferred headings, derived title/language, substituted fonts, OCR text) / ✖ failed (e.g. a font whose codes carry no character information).
* **Preservation checks** compare the repaired file with the original: page count/size, every original word still extractable, rendering similarity (small shape differences → *needs review*, large → *failed*).
* **Fonts**: every non-embedded font is replaced by an embedded substitute (always listed as *needs review*; the validator compares the rendering with the original).
  * *Standard fonts* (Helvetica, Arial, Times, Courier …): metric-compatible Liberation (on Windows Arial / Times New Roman / Courier New, DejaVu as a last resort). An unknown font name follows the font's own flags (fixed-pitch / serif) and otherwise **the family used most in the document**.
  * *Symbolic simple fonts*: Symbol and ZapfDingbats use the built-in Adobe encodings; any other symbolic font needs a `/ToUnicode` map. A substitute that really has those characters is embedded with a symbolic cmap and a ToUnicode map is written.
  * *Composite (CID) fonts* with Identity-H/V and a `/ToUnicode` map: the descendant font is replaced so that every CID selects the glyph of the character its ToUnicode entry names; page content is untouched.
  * *Still failed, by design*: fonts whose codes say nothing about the characters (no ToUnicode and no standard encoding) and CID fonts with a CJK CMap other than Identity. Without the original font nobody can know what those codes mean: re-export from the source application.
* **Form XObjects**: a Form that contains text or images is *flattened* into the page (its resources are renamed so nothing collides) and then tagged like any other content; v3 hid such forms as one Artifact. Forms with only graphics stay Artifacts. The validator has a new check `form.hidden` for the old behaviour.
* **Decorative images**: in the Human review tab, *Mark as decorative* turns a Figure into an Artifact (page content, structure tree and ParentTree are all updated, then the file is validated again).

## 6. External validators
* **veraPDF** (PDF/UA-1): install it and `export VERAPDF=/path/to/verapdf`; it then runs after every repair. The report parser follows veraPDF's documented JSON; confirm on your version once with `verapdf --flavour ua1 --format json some.pdf`.
* **PAC**: Windows desktop app with no CLI/API. Open the downloaded PDF in PAC by hand; the report shows it as *Pending*.

## 7. Settings (environment variables, see `.env.example`)
`APP_PASSWORD` · `MAX_UPLOAD_MB` (200) · `MAX_PAGES` (1000) · `RETENTION_MINUTES` (60) · `MAX_JOBS` (2) · `RATE_LIMIT_PER_MIN` (20 uploads/repairs/re-checks per minute per IP; review clicks allow 20x that) · `TRUST_PROXY` · `DATA_DIR` · `FONT_DIR` · `VERAPDF` · `ANTHROPIC_API_KEY`

Privacy: files are deleted after `RETENTION_MINUTES`, on the **Delete my files** button, and when the container restarts. Responses are `no-store`; encrypted and non-PDF/image files are rejected.

## 8. Performance (measured, 1 CPU core, synthetic untagged documents)
| Document | Repair | Validate repaired | Peak memory |
|---|---|---|---|
| 50 pages, 5 links + 2 images per page | 12 s | 2 s | 149 MB |
| 200 pages, 20 links + 1 image per page | 32 s | 8 s | 223 MB |
| 300 pages (default limit), 10 links + 1 image per page | 63 s | 14 s | 246 MB |

Repeat on your own files with `python bench.py <pages> <links per page> <images per page>`. Real scanned documents are slower (OCR ≈ 1–3 s/page).

## 9. Tests
```bash
pip install -r requirements-dev.txt && pytest -q
```
71 tests: Form XObject flattening, symbolic and CID font replacement (checked by OCR of the rendering), the most-used-family choice, decorative figures, large uploads (52 MB accepted, oversize refused early, page limit), the human-review workflow (decisions, locked placeholders, edits applied and re-validated, sign-off bound to the file hash, reports), a damage rubric (11 ways a repaired PDF can be broken, each must be reported), font embedding, the full upload→repair→report flow, password, rate limit, upload limits, XSS escaping.

## 10. Known gaps
Forms that carry their own marked content, tiling patterns and annotation appearance streams aren't inspected for tagging · fonts with no character information and CJK-CMap CID fonts · decorative conversion only for Figures that have their own marked content on the page · no automatic reading-order or contrast testing · rate limit and job queue are per process (in memory) ·
no per-job time limit (a pathological PDF can occupy a worker until it finishes) · no user accounts (one shared password) · the workbench UI itself has had only basic accessibility fixes (language, labels, live status, alt text), not a full audit.
