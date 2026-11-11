import io, os, time, pytest
from fastapi.testclient import TestClient
import app as appmod

c = TestClient(appmod.app)

def upload(src):
    return c.post("/api/analyze", files={"file": ("a.pdf", open(src, "rb"), "application/pdf")}).json()["sid"]

def fix_and_wait(sid, timeout=120):
    r = c.post(f"/api/fix/{sid}"); assert r.status_code == 202, r.text
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get(f"/api/job/{sid}").json()
        if j["state"] in ("done", "error"): return j
        time.sleep(0.2)
    raise AssertionError("job timed out")

def test_full_flow_and_reports(src):
    sid = upload(src); job = fix_and_wait(sid)
    assert job["state"] == "done" and job["pct"] == 100 and all(x["state"] == "done" for x in job["steps"])
    rep = c.get(f"/api/result/{sid}").json()["report"]
    assert rep["before"]["totals"]["failed"] > rep["after"]["totals"]["failed"] == 0
    assert rep["after"]["verdict"] in ("needs_review", "passed") and rep["audit"] and rep["outcomes"]
    h = c.get(f"/api/report/{sid}.html"); assert h.status_code == 200 and "<html lang=\"en\">" in h.text and "Before and after" in h.text
    assert c.get(f"/api/report/{sid}.json").json()["name"] == "a.pdf"
    assert c.get(f"/api/download/{sid}").content[:5] == b"%PDF-"
    assert c.get(f"/api/validate/{sid}/fixed").json()["pages"] == 2
    assert c.get(f"/api/page/{sid}/fixed/1.png").headers["cache-control"] == "no-store"
    assert c.delete(f"/api/session/{sid}").json()["deleted"]; assert c.get(f"/api/download/{sid}").status_code == 404

def test_report_html_escapes_pdf_controlled_text():
    import report, validate
    f = {"status": "failed", "page": 1, "msg": "<script>alert(1)</script>", "count": 1, "bbox": None}
    chk = [dict(id="x", group="g", title="t<b>", wcag="1.1.1", mh="-", status="failed", findings=[f])]
    A = dict(verdict="failed", verdict_text="v", generated="now", sha256="0", totals=dict(failed=1, needs_review=0, checks_passed=0, checks_failed=1, checks=1),
             checks=chk, page_results=[dict(page=1, failed=1, needs_review=0)], external=[], limitations=[])
    out = report.to_html(dict(name="<img src=x onerror=1>", before=A, after=A, outcomes=[], audit=[]))
    assert "<script>" not in out and "<img src=x" not in out

@pytest.mark.parametrize("name,data,code", [("e.pdf", b"", 400), ("x.pdf", b"hello world", 415), ("x.pdf", b"%PDF-1.4 junk", 422)])
def test_rejects_bad_uploads(name, data, code):
    assert c.post("/api/analyze", files={"file": (name, data)}).status_code == code

def test_size_limit(monkeypatch):
    monkeypatch.setattr(appmod, "MAX_BYTES", 100)
    assert c.post("/api/analyze", files={"file": ("a.pdf", b"%PDF-" + b"0" * 500)}).status_code == 413

def test_rejects_encrypted_pdf(tmp_path):
    import pikepdf
    p = pikepdf.new(); p.add_blank_page(); f = tmp_path / "e.pdf"
    p.save(str(f), encryption=pikepdf.Encryption(user="u", owner="o"))
    assert c.post("/api/analyze", files={"file": ("e.pdf", open(f, "rb"))}).status_code == 422

def test_bad_session_ids_and_page_bounds(src):
    assert c.get("/api/download/..%2f..%2fetc").status_code in (400, 404)
    sid = c.post("/api/analyze", files={"file": ("a.pdf", open(src, "rb"))}).json()["sid"]
    assert c.get(f"/api/page/{sid}/orig/0.png").status_code == 404 and c.get(f"/api/page/{sid}/orig/99.png").status_code == 404

def test_expired_files_are_purged(src, monkeypatch):
    sid = c.post("/api/analyze", files={"file": ("a.pdf", open(src, "rb"))}).json()["sid"]
    monkeypatch.setattr(appmod, "TTL_SECONDS", -1); appmod.purge()
    assert c.get(f"/api/download/{sid}").status_code == 404


def test_job_progress_is_reported_and_double_start_is_refused(src):
    sid = upload(src); assert c.post(f"/api/fix/{sid}").status_code == 202
    assert c.post(f"/api/fix/{sid}").status_code in (202, 409)       # 409 while running; may already be finished on a fast machine
    t0 = time.time()
    while c.get(f"/api/job/{sid}").json()["state"] not in ("done", "error") and time.time() - t0 < 120: time.sleep(0.2)
    assert c.get(f"/api/job/{sid}").json()["state"] == "done"

def test_report_has_severity_actions_and_audit(src):
    sid = upload(src); fix_and_wait(sid); rep = c.get(f"/api/result/{sid}").json()["report"]
    assert set(rep["before"]["totals"]["by_sev"]) == {"critical", "major", "minor"} and rep["before"]["totals"]["by_sev"]["critical"] > 0
    assert any(a["status"] == "succeeded" for a in rep["actions"]) and any(a["status"] == "needs_review" for a in rep["actions"])
    assert [x["step"] for x in rep["audit"]][0] == "Upload" and len(rep["audit"]) >= 6
    assert "Fix actions" in c.get(f"/api/report/{sid}.html").text

def test_security_headers_and_health():
    r = c.get("/healthz"); assert r.json() == {"ok": True}
    r = c.get("/"); h = r.headers
    assert h["cache-control"] == "no-store" and h["x-frame-options"] == "DENY" and "default-src 'self'" in h["content-security-policy"] and r.text.startswith("<!doctype html><html lang=en>")
    assert c.get("/docs").status_code == 404                # no public API explorer

def test_password_protection(monkeypatch):
    monkeypatch.setattr(appmod, "PASSWORD", "s3cret")
    assert c.get("/").status_code == 401 and c.get("/api/rules").status_code == 401
    assert c.get("/healthz").status_code == 200
    assert c.get("/", auth=("anyone", "wrong")).status_code == 401
    assert c.get("/", auth=("anyone", "s3cret")).status_code == 200

def test_rate_limit(monkeypatch, src):
    monkeypatch.setattr(appmod, "RATE_PER_MIN", 3); appmod._hits.clear()
    codes = [c.post("/api/analyze", files={"file": ("e.pdf", b"")}).status_code for _ in range(5)]
    assert codes[:3] == [400] * 3 and codes[3:] == [429, 429]; appmod._hits.clear()
