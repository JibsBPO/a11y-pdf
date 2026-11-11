"""Builds the before/after validation report (JSON for the UI, standalone accessible HTML for download)."""
import html, json
import validate

LABEL = {"passed": "✔ Passed", "needs_review": "⚠ Needs review", "failed": "✖ Failed", "pending": "… Pending", "error": "‼ Error"}
ACTION = {"succeeded": "✔ Succeeded", "needs_review": "⚠ Needs review", "failed": "✖ Failed"}
OUTCOME = {"fixed": "✔ Fixed", "needs_review": "⚠ Fixed, needs review", "failed": "✖ Still failing", "regressed": "✖ New problem introduced"}


def build(name, before, after, fix_result, audit, engine_before=None, engine_after=None):
    return dict(name=name, before=before, after=after, outcomes=validate.compare(before, after),
                fix_log=fix_result, actions=(fix_result or {}).get("actions", []), audit=audit,
                builtin=dict(before=(engine_before or {}).get("summary"), after=(engine_after or {}).get("summary")))


def rebuild(old, after, engine_after, audit_extra):
    """Report after a re-check (human edits applied): keeps the original 'before' and the fixer's actions, replaces 'after'."""
    rep = dict(old); rep["after"] = after; rep["outcomes"] = validate.compare(old["before"], after)
    rep["builtin"] = dict(old.get("builtin") or {}, after=(engine_after or {}).get("summary")); rep["audit"] = list(old.get("audit", [])) + list(audit_extra)
    return rep


def to_json(rep): return json.dumps(rep, indent=1, ensure_ascii=False)


def to_html(rep):
    e = lambda s: html.escape(str(s if s is not None else ""))
    B, A = rep["before"], rep["after"]
    def rows(cells): return "".join(f"<tr>{''.join(cells_)}</tr>" for cells_ in cells)
    th = lambda s: f'<th scope="col">{e(s)}</th>'
    out = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>Accessibility validation report: {e(rep["name"])}</title><style>'
           ':root{color-scheme:light dark;font:16px/1.5 system-ui,sans-serif}body{max-width:60rem;margin:2rem auto;padding:0 1rem}'
           'table{border-collapse:collapse;width:100%;margin:.5rem 0 1.5rem}th,td{border:1px solid #8886;padding:.4rem .6rem;text-align:left;vertical-align:top}'
           '.v{font-size:1.4rem;font-weight:700;border:3px solid currentColor;padding:.5rem 1rem;display:inline-block}code{word-break:break-all}'
           '</style></head><body><main>',
           f'<h1>Accessibility validation report</h1><p><strong>File:</strong> {e(rep["name"])} · <strong>Generated:</strong> {e(A["generated"])} · '
           f'<strong>SHA-256 of repaired PDF:</strong> <code>{e(A["sha256"])}</code></p>',
           f'<p class="v">{e(LABEL.get(A["verdict"], A["verdict"]))}</p><p>{e(A["verdict_text"])}</p>',
           '<h2>Before and after</h2><p>Both files were checked by the same independent validator.</p><table><caption>Result counts</caption><thead><tr>'
           + "".join(th(x) for x in ("Measure", "Original", "Repaired")) + "</tr></thead><tbody>"
           + rows([[f"<th scope='row'>{m}</th>", f"<td>{e(B['totals'][k])}</td>", f"<td>{e(A['totals'][k])}</td>"] for m, k in
                   (("Failures", "failed"), ("Items needing human review", "needs_review"), ("Checks passed", "checks_passed"), ("Checks failed", "checks_failed"))])
           + f"</tbody></table><p>Total checks run: {e(A['totals']['checks'])}.</p>"]
    cats = A.get("category_summary") or validate.category_summary(A.get("checks", []))
    out.append('<h2>Automated check summary by category</h2><p>Workbench check counts; these are not PAC checkpoint totals. PAC results are shown separately below.</p>'
               '<table><caption>Results by category</caption><thead><tr>' + "".join(th(x) for x in ("Category", "Checks", "Passed", "Warned / review", "Failed", "Errors")) + "</tr></thead><tbody>"
               + rows([[f"<th scope='row'>{e(c['group'])}</th>", f"<td>{c['checks']}</td>", f"<td>{c['passed']}</td>", f"<td>{c['warned']}</td>", f"<td>{c['failed']}</td>", f"<td>{c['errors']}</td>"] for c in cats])
               + "</tbody></table>")
    out.append('<h2>What the repair did</h2><table><thead><tr>' + "".join(th(x) for x in ("Check", "Group", "Before", "After", "Outcome")) + "</tr></thead><tbody>"
               + (rows([[f"<td>{e(o['title'])}</td>", f"<td>{e(o['group'])}</td>", f"<td>{e(LABEL[o['before']])}</td>", f"<td>{e(LABEL[o['after']])}</td>", f"<td>{e(OUTCOME[o['outcome']])}</td>"] for o in rep["outcomes"]])
                  or '<tr><td colspan="5">No check changed status.</td></tr>') + "</tbody></table>")
    acts = rep.get("actions", []); cnt = {k: sum(a["status"] == k for a in acts) for k in ACTION}
    out.append(f'<h2>Fix actions</h2><p>{cnt["succeeded"]} succeeded · {cnt["needs_review"]} need review · {cnt["failed"]} failed.</p>'
               + ('<table><thead><tr>' + "".join(th(x) for x in ("Result", "Action", "Page", "Detail")) + "</tr></thead><tbody>"
                  + rows([[f"<td>{e(ACTION.get(a['status'], a['status']))}</td>", f"<td>{e(a['action'])}</td>", f"<td>{e(a.get('page') or '')}</td>", f"<td>{e(a.get('detail'))}</td>"]
                          for a in sorted(acts, key=lambda a: ("failed", "needs_review", "succeeded").index(a["status"]))]) + "</tbody></table>" if acts else "<p>The fixer made no changes.</p>"))
    for title, st in (("Remaining failures", ("failed", "error")), ("Items that need human review", ("needs_review",))):
        items = sorted(((c, f) for c in A["checks"] if c["status"] in st for f in c["findings"] if f["status"] in st), key=lambda cf: (validate.SEV_ORDER.get(cf[1].get("sev"), 9), cf[1]["page"] or 0))
        out.append(f"<h2>{title}</h2>")
        if not items: out.append("<p>None.</p>"); continue
        out.append('<table><thead><tr>' + "".join(th(x) for x in ("Severity", "Page", "Check", "WCAG", "Detail")) + "</tr></thead><tbody>"
                   + rows([[f"<td>{e(f.get('sev'))}</td>", f"<td>{e(f['page'] or 'Document')}</td>", f"<td>{e(c['title'])}</td>", f"<td>{e(c['wcag'])}</td>", f"<td>{e(f['msg'])}</td>"] for c, f in items]) + "</tbody></table>")
    hv = rep.get("human")
    if hv:
        sm = hv["summary"]; sg = hv.get("signoff")
        out.append(f'<h2>Human verification</h2><p class="v">{e(hv["status_text"])}</p>')
        if sg: out.append(f'<p>Signed off by <strong>{e(sg["reviewer"])}</strong> on {e(sg["t"])}. The sign-off applies only to the file with SHA-256 <code>{e(sg["sha256"])}</code>. '
                          f'Statement: {e(sg["statement"])}</p>')
        else: out.append(f'<p>{e(sm["resolved"])} of {e(sm["total"])} review items are resolved.' + (" Open points: " + e("; ".join(sm["blockers"])) + "." if sm["blockers"] else "") + "</p>")
        out.append("<p>This records that a person looked at the items below; it is not a PDF/UA certification.</p>")
        its = hv["items"]; LB = {"confirmed": "✔ Confirmed", "edited": "✎ Edited", "rejected": "✖ Rejected", "pending": "… Not reviewed"}
        if its:
            out.append('<table><caption>Review items (first 500)</caption><thead><tr>' + "".join(th(x) for x in ("Decision", "Item", "Page", "Detail", "Reviewer note", "Reviewer / time")) + "</tr></thead><tbody>"
                       + rows([[f"<td>{e(LB.get(i['decision'], i['decision']))}{'' if i['decision'] != 'edited' else (' (applied)' if i['applied'] else ' (not applied)')}</td>", f"<td>{e(i['title'])}</td>", f"<td>{e(i['page'] or '')}</td>",
                                f"<td>{e(i['detail'])}" + (("<br><strong>Marked as decorative</strong> (removed from the reading order)" if i["value"] == "[decorative]" else f"<br><strong>Corrected to:</strong> {e(i['value'])}") if i["decision"] == "edited" and i.get("value") else "") + "</td>",
                                f"<td>{e(i['note'])}</td>", f"<td>{e(i['by'] or '')} {e(i['t'] or '')}</td>"] for i in its[:500]]) + "</tbody></table>")
            if len(its) > 500: out.append(f"<p>{len(its) - 500} more items are in the JSON report.</p>")
        if hv.get("log"): out.append("<h3>Review log</h3><ol>" + "".join(f"<li><time>{e(x['t'])}</time> — {e(x.get('who') or '')}: {e(x['what'])}</li>" for x in hv["log"][-100:]) + "</ol>")
    out.append('<h2>Page by page</h2><table><thead><tr>' + "".join(th(x) for x in ("Page", "Failures", "Needs review")) + "</tr></thead><tbody>"
               + rows([[f"<th scope='row'>{p['page']}</th>", f"<td>{p['failed']}</td>", f"<td>{p['needs_review']}</td>"] for p in A["page_results"]]) + "</tbody></table>")
    out.append('<h2>External validation</h2><table><thead><tr>' + "".join(th(x) for x in ("Tool", "Status", "Detail")) + "</tr></thead><tbody>"
               + rows([[f"<td>{e(x['tool'])}</td>", f"<td>{e(LABEL.get(x['status'], x['status']))}</td>", f"<td>{e(x['msg'])}"
                        + ("<ul>" + "".join(f"<li>Clause {e(d.get('clause'))}-{e(d.get('test'))}: {e(d.get('msg'))}</li>" for d in x['details'][:30]) + "</ul>" if x["details"] else "") + "</td>"] for x in A["external"]] or [["<td colspan='3'>Not run.</td>"]])
               + "</tbody></table>")
    out.append("<h2>Audit log</h2><ol>" + "".join(f"<li><time>{e(a['t'])}</time> — {e(a['step'])}: {e(a['detail'])}</li>" for a in rep["audit"]) + "</ol>")
    out.append("<h2>Limitations</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in A["limitations"]) + "</ul></main></body></html>")
    return "".join(out)
