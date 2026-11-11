import json
import report
import validate


def test_report_includes_internal_category_scorecard(fixed):
    result = validate.run(fixed, external=False)
    rep = report.build("sample.pdf", result, result, {}, [])
    html = report.to_html(rep)
    payload = json.loads(report.to_json(rep))

    assert "Automated check summary by category" in html
    assert "not PAC checkpoint totals" in html
    assert "Structure" in html and "Manual review" in html
    assert len(payload["after"]["category_summary"]) > 1
