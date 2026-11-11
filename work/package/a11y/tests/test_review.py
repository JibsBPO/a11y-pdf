"""Human verification workflow + large uploads."""
import os, time, pytest, pikepdf
from fastapi.testclient import TestClient
import app as appmod

c = TestClient(appmod.app)

@pytest.fixture(autouse=True)
def _fresh_rate_limit(): appmod._hits.clear()


def upload(src):
    return c.post("/api/analyze", files={"file": ("a.pdf", open(src, "rb"), "application/pdf")}).json()["sid"]

def wait(sid, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get(f"/api/job/{sid}").json()
        if j["state"] in ("done", "error"): return j
        time.sleep(0.2)
    raise AssertionError("job timed out")

def repaired(src):
    sid = upload(src); assert c.post(f"/api/fix/{sid}").status_code == 202; assert wait(sid)["state"] == "done"; return sid

def items(sid, status="all"): return c.get(f"/api/review/{sid}", params=dict(status=status, limit=200)).json()

def decide(sid, iid, decision, **kw): return c.post(f"/api/review/{sid}/item", data=dict(id=iid, decision=decision, reviewer="Dana Reviewer", **kw))

def by(p, **kw): return [i for i in p["items"] if all(i.get(k) == v for k, v in kw.items())]


# ---------------------------------------------------------------- uploads: the 413 / 50 MB problem
def big_pdf(path, mb):
    pdf = pikepdf.new(); pdf.add_blank_page()
    pdf.pages[0].obj.Junk = pdf.make_stream(os.urandom(mb * 1024 * 1024))          # random data does not compress: the file really is this big
    pdf.save(path)

def test_config_reports_limits():
    j = c.get("/api/config").json()
    assert j["max_upload_mb"] >= 50 and j["max_pages"] >= 300 and "ocr" in j

def test_pdf_above_50_mb_is_accepted(tmp_path):
    f = tmp_path / "big.pdf"; big_pdf(str(f), 52); assert os.path.getsize(f) > 50 * 1024 * 1024
    r = c.post("/api/analyze", files={"file": ("big.pdf", open(f, "rb"), "application/pdf")})
    assert r.status_code == 200, r.text
    j = r.json(); assert j["pages"] == 1 and j["bytes"] > 50 * 1024 * 1024 and j["page_sizes"] == [[612.0, 792.0]]
    assert c.get(f"/api/page/{j['sid']}/orig/1.png").status_code == 200
    assert c.delete(f"/api/session/{j['sid']}").json()["deleted"]
    assert not [x for x in os.listdir(appmod.T) if x.startswith(j["sid"])]            # nothing left behind

def test_oversize_is_refused_early_with_a_clear_message(monkeypatch):
    monkeypatch.setattr(appmod, "MAX_BYTES", 1024 * 1024)
    r = c.post("/api/analyze", files={"file": ("a.pdf", b"%PDF-" + b"0" * (3 * 1024 * 1024))})   # > limit + framing allowance: refused by the guard
    assert r.status_code == 413 and "limit is 1 MB" in r.json()["detail"] and r.json()["limit_mb"] == 1
    r = c.post("/api/analyze", files={"file": ("a.pdf", b"%PDF-" + b"0" * int(1.5 * 1024 * 1024))})  # inside the framing allowance: refused while copying
    assert r.status_code == 413 and "limit 1 MB" in r.json()["detail"]
    assert not [x for x in os.listdir(appmod.T) if x.endswith("_upload.bin")]          # partial upload removed

def test_too_many_pages_is_413_with_the_page_limit(src, monkeypatch):
    monkeypatch.setattr(appmod, "MAX_PAGES", 1)
    r = c.post("/api/analyze", files={"file": ("a.pdf", open(src, "rb"))})
    assert r.status_code == 413 and "limit 1" in r.json()["detail"]

def test_upload_without_declared_size_is_refused():
    r = c.post("/api/analyze", content=iter([b"x" * 10]), headers={"content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 411

def test_headers_are_present_on_early_responses(monkeypatch):
    monkeypatch.setattr(appmod, "PASSWORD", "pw")
    r = c.get("/"); assert r.status_code == 401 and r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"

def test_analyze_returns_page_sizes(src):
    sid = c.post("/api/analyze", files={"file": ("a.pdf", open(src, "rb"))}).json(); assert sid["page_sizes"] == [[612.0, 792.0]] * 2


# ---------------------------------------------------------------- crop
def test_crop_endpoint(src):
    sid = upload(src)
    r = c.get(f"/api/crop/{sid}/orig/1.png", params=dict(bbox="72,242,272,342")); assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert c.get(f"/api/crop/{sid}/orig/1.png", params=dict(bbox="nope")).status_code == 400
    assert c.get(f"/api/crop/{sid}/orig/9.png", params=dict(bbox="1,1,50,50")).status_code == 404
    assert c.get(f"/api/crop/{sid}/orig/1.png", params=dict(bbox="900,900,950,950")).status_code == 400    # outside the page


# ---------------------------------------------------------------- the review checklist
def test_checklist_is_built_from_the_report(src):
    sid = repaired(src); p = items(sid)
    s = p["summary"]; assert s["total"] == len(p["items"]) > 5 and s["pending"] == s["total"] and not s["can_signoff"] and s["status"] == "in_progress"
    assert len(by(p, source="manual")) == 4 and by(p, group="External validators")
    alt = by(p, editable="alt"); assert alt and alt[0]["ref"]["page"] == 1 and alt[0]["bbox"] and alt[0]["current"]
    assert by(p, editable="title")[0]["current"] == "Quarterly Report" and by(p, editable="lang")[0]["current"] == "en"
    assert {i["id"] for i in p["items"]} == {i["id"] for i in items(sid)["items"]}      # ids are stable between calls

def test_filters_and_paging(src):
    sid = repaired(src); n = items(sid)["total"]
    assert c.get(f"/api/review/{sid}", params=dict(status="all", limit=3)).json()["items"].__len__() == 3
    assert c.get(f"/api/review/{sid}", params=dict(status="all", offset=n)).json()["items"] == []
    assert c.get(f"/api/review/{sid}", params=dict(status="confirmed")).json()["total"] == 0
    assert c.get(f"/api/review/{sid}", params=dict(limit=0)).json()["items"] == []     # summary only

def test_decisions_notes_and_rejection(src):
    sid = repaired(src); p = items(sid); manual = by(p, source="manual")[0]
    r = decide(sid, manual["id"], "confirmed", note="Stepped through the tags in Acrobat"); assert r.status_code == 200
    got = r.json()["items"][0]; assert got["decision"] == "confirmed" and got["note"].startswith("Stepped") and got["by"] == "Dana Reviewer" and got["resolved"]
    r = decide(sid, manual["id"], "rejected", note="Order is wrong on page 2"); s = r.json()["summary"]; assert s["rejected"] == 1 and any("rejected" in b for b in s["blockers"])
    assert decide(sid, manual["id"], "pending").json()["items"][0]["decision"] == "pending"
    assert decide(sid, manual["id"], "maybe").status_code == 400 and decide(sid, "nonexistent", "confirmed").status_code == 404

def test_placeholder_alt_text_cannot_be_confirmed_only_fixed(src):
    sid = repaired(src); p = items(sid); ph = by(p, group="Placeholder alt text inserted")[0]
    assert ph["locked"] and decide(sid, ph["id"], "confirmed").status_code == 400
    locked = [i for i in p["items"] if i["locked"]]; assert len(locked) >= 2                       # the fixer's placeholder AND the validator's placeholder finding
    assert all(decide(sid, i["id"], "confirmed").status_code == 400 for i in locked)
    assert [x for x in p["groups"] if x["group"] == "Placeholder alt text inserted"][0]["bulk"] is False

def test_external_results_need_a_note(src):
    sid = repaired(src); pac = [i for i in items(sid)["items"] if i["source"] == "external"][0]
    assert decide(sid, pac["id"], "confirmed").status_code == 400
    assert decide(sid, pac["id"], "confirmed", note="PAC 2024: 0 errors").status_code == 200

def test_edit_validation(src):
    sid = repaired(src); p = items(sid); alt = by(p, editable="alt")[0]; ttl = by(p, editable="title")[0]; lng = by(p, editable="lang")[0]; other = by(p, source="manual")[0]
    assert decide(sid, other["id"], "edited", value="x").status_code == 400                   # not editable
    for bad in ("", "image", "photo", "needs review", "x" * 600): assert decide(sid, alt["id"], "edited", value=bad).status_code == 422, bad
    assert decide(sid, lng["id"], "edited", value="english please").status_code == 422
    assert decide(sid, ttl["id"], "edited", value="  ").status_code == 422

def test_bulk_confirm_rules(src):
    sid = repaired(src); p = items(sid)
    assert c.post(f"/api/review/{sid}/bulk", data=dict(group="Manual checklist")).status_code == 400
    assert c.post(f"/api/review/{sid}/bulk", data=dict(group="Placeholder alt text inserted")).status_code == 400
    g = [x for x in p["groups"] if x["bulk"]][0]; n = g["pending"]
    r = c.post(f"/api/review/{sid}/bulk", data=dict(group=g["group"], reviewer="Dana Reviewer"))
    assert r.status_code == 200 and r.json()["summary"]["confirmed"] == n
    assert any("bulk-confirmed" in x["what"] for x in c.get(f"/api/report/{sid}.json").json()["human"]["log"])

def test_full_review_edit_recheck_signoff_and_report(src):
    sid = repaired(src); p = items(sid)
    alt, ttl = by(p, editable="alt")[0], by(p, editable="title")[0]
    # 1. edits are recorded but NOT in the file until applied
    assert decide(sid, alt["id"], "edited", value="A blue rectangle used as a sample picture", note="checked against the page").status_code == 200
    assert decide(sid, ttl["id"], "edited", value="Quarterly Report 2026").status_code == 200
    s = items(sid)["summary"]; assert s["unapplied"] == 2 and any("not applied" in b for b in s["blockers"])
    # 2. sign-off is refused while anything is open
    assert c.post(f"/api/review/{sid}/signoff", data=dict(reviewer="Dana Reviewer", agree="yes")).status_code == 409
    # 3. apply + re-check (background job, same polling as a repair)
    assert c.post(f"/api/review/{sid}/apply").status_code == 202; j = wait(sid); assert j["state"] == "done" and [x["name"] for x in j["steps"]][0].startswith("Applying")
    with pikepdf.open(os.path.join(appmod.T, f"{sid}_fixed.pdf")) as pdf:
        assert str(pdf.docinfo["/Title"]) == "Quarterly Report 2026" and b"Quarterly Report 2026" in bytes(pdf.Root.Metadata.read_bytes())
    p = items(sid); assert p["summary"]["unapplied"] == 0 and by(p, id=alt["id"])[0]["resolved"] and by(p, id=alt["id"])[0]["current"] == "A blue rectangle used as a sample picture"
    assert not by(p, check="fig.quality", detail="Alt text is a placeholder: 'Image (alt text needs review)'")        # the validator no longer sees the placeholder
    # 4. confirm everything else
    for i in p["items"]:
        if not i["resolved"]:
            assert decide(sid, i["id"], "confirmed", note="PAC and veraPDF not run, accepted" if i["needs_note"] else "ok").status_code == 200, i
    s = items(sid)["summary"]; assert s["can_signoff"] and s["status"] == "ready" and not s["blockers"], s
    # 5. sign-off needs a name and the tick box
    assert c.post(f"/api/review/{sid}/signoff", data=dict(reviewer="D", agree="yes")).status_code == 400
    assert c.post(f"/api/review/{sid}/signoff", data=dict(reviewer="Dana Reviewer", agree="")).status_code == 400
    r = c.post(f"/api/review/{sid}/signoff", data=dict(reviewer="Dana Reviewer", agree="yes")); assert r.status_code == 200 and r.json()["summary"]["status"] == "verified" and r.json()["signoff"]["reviewer"] == "Dana Reviewer"
    # 6. the downloadable reports carry the human record, bound to the file's hash
    rep = c.get(f"/api/report/{sid}.json").json(); assert rep["human"]["status"] == "verified" and rep["human"]["signoff"]["sha256"] == rep["after"]["sha256"]
    html = c.get(f"/api/report/{sid}.html").text; assert "Human verification" in html and "Dana Reviewer" in html and "Quarterly Report 2026" in html and "not a PDF/UA certification" in html
    # 7. changing any decision withdraws the sign-off
    one = by(items(sid), source="manual")[0]; r = decide(sid, one["id"], "rejected", note="found a problem"); assert r.json()["signoff"] is None and r.json()["summary"]["status"] == "in_progress"
    assert c.post(f"/api/review/{sid}/signoff", data=dict(reviewer="Dana Reviewer", agree="yes")).status_code == 409

def test_signoff_can_be_withdrawn_and_new_repair_resets_review(src):
    sid = repaired(src)
    for i in items(sid)["items"]:
        if i["editable"] == "alt": decide(sid, i["id"], "edited", value="A sample picture for the test document")
    c.post(f"/api/review/{sid}/apply"); wait(sid)
    for i in items(sid)["items"]:
        if not i["resolved"]: decide(sid, i["id"], "confirmed", note="checked, fine")
    assert c.post(f"/api/review/{sid}/signoff", data=dict(reviewer="Dana Reviewer", agree="yes")).json()["summary"]["signed"]
    assert c.delete(f"/api/review/{sid}/signoff").json()["signoff"] is None
    c.post(f"/api/fix/{sid}"); wait(sid)
    assert items(sid)["summary"]["confirmed"] == 0                                    # a new repair starts a new review

def test_reviewing_many_items_is_not_rate_limited(src, monkeypatch):
    monkeypatch.setattr(appmod, "RATE_PER_MIN", 3)                        # uploads/repairs: 3 per minute...
    sid = repaired_unlimited(src, monkeypatch); m = by(items(sid), source="manual")[0]
    assert all(decide(sid, m["id"], d).status_code == 200 for d in ["confirmed", "pending"] * 10)     # ...but 20 review clicks in a row are fine

def repaired_unlimited(src, monkeypatch):
    monkeypatch.setattr(appmod, "RATE_PER_MIN", 1000); sid = repaired(src); monkeypatch.setattr(appmod, "RATE_PER_MIN", 3); appmod._hits.clear(); return sid

def test_review_requires_a_repair_first(src):
    sid = upload(src); assert c.get(f"/api/review/{sid}").status_code == 404

def test_review_html_report_escapes_reviewer_text(src):
    sid = repaired(src); m = by(items(sid), source="manual")[0]
    decide(sid, m["id"], "confirmed", note="<script>alert(1)</script>")
    html = c.get(f"/api/report/{sid}.html").text; assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


# ---------------------------------------------------------------- decorative figures -> Artifact
def test_figure_can_be_marked_decorative_and_leaves_the_reading_order(src):
    sid = repaired(src); p = items(sid); alt = by(p, editable="alt")[0]
    def figures():
        with pikepdf.open(os.path.join(appmod.T, f"{sid}_fixed.pdf")) as pdf:
            out = []; import engine
            engine.walk(pdf.Root.StructTreeRoot.K, engine.rolemap(pdf.Root.StructTreeRoot), lambda el, d, pg: out.append(el) if str(el.get("/S")) == "/Figure" else None); return len(out)
    n0 = figures(); assert n0 >= 1
    r = decide(sid, alt["id"], "edited", value="[decorative]"); assert r.status_code == 200
    assert c.post(f"/api/review/{sid}/apply").status_code == 202 and wait(sid)["state"] == "done"
    assert figures() == n0 - 1                                                            # the Figure element is gone ...
    rep = c.get(f"/api/report/{sid}.json").json()
    bad = {x["id"] for x in rep["after"]["checks"] if x["status"] in ("failed", "error")}
    assert bad == set(), bad                                                              # ... and the tree, ParentTree and page content are still consistent
    got = by(items(sid), id=alt["id"])[0]; assert got["resolved"] and got["value"] == "[decorative]"
    assert not by(items(sid), check="fig.quality", detail="Alt text is a placeholder: 'Image (alt text needs review)'")
    assert "Marked as decorative" in c.get(f"/api/report/{sid}.html").text
