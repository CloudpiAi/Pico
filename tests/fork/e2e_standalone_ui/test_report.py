"""The run log and the reports (tests/e2e/standalone_ui/report.py)."""

import json
import os
import re

from tests.e2e.standalone_ui.checks import Check
from tests.e2e.standalone_ui.report import RunLog

TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _log(tmp_path, **kw):
    return RunLog(str(tmp_path / "run"), clock=lambda: 1794383046.0, **kw)


def _lines(log):
    with open(os.path.join(log.dir, "run.log"), encoding="utf-8") as fh:
        return fh.read().splitlines()


def test_run_log_is_written_live_with_a_timestamp_per_event(tmp_path):
    log = _log(tmp_path)
    log.case_start("qa_help", " /help")
    lines = _lines(log)  # readable before the case ends
    assert any("START qa_help" in ln and "' /help'" in ln for ln in lines)
    log.add(Check("PASS", "qa_help finished", "done"))
    log.add(Check("FAIL", "qa_help reply", "(No response generated)"))
    log.case_end("qa_help", 12.34)
    lines = _lines(log)
    assert all(TS.match(ln) for ln in lines)
    assert any("PASS qa_help finished" in ln and "done" in ln for ln in lines)
    assert any("FAIL qa_help reply" in ln for ln in lines)
    assert any("END qa_help FAIL" in ln and "12.3s" in ln for ln in lines)


def test_echo_gets_every_line(tmp_path):
    seen = []
    log = _log(tmp_path, echo=seen.append)
    log.case_start("a", "hi")
    log.note("waiting for the late answer")
    assert len(seen) == 2 and "waiting for the late answer" in seen[1]


def test_case_status_and_counts(tmp_path):
    log = _log(tmp_path)
    for cid, statuses in (("ok", ["PASS", "PASS"]), ("bad", ["PASS", "FAIL"]), ("skipped", ["SKIP"]),
                          ("partial", ["PASS", "SKIP"])):
        log.case_start(cid, "x")
        for s in statuses:
            log.add(Check(s, f"{cid} c", ""))
        log.case_end(cid, 1.0)
    assert [log.cases[c].status for c in ("ok", "bad", "skipped", "partial")] == ["PASS", "FAIL", "SKIP", "PASS"]
    assert log.counts() == {"PASS": 2, "FAIL": 1, "SKIP": 1}
    assert log.exit_code() == 1
    assert log.exit_code(setup_failed=True) == 2


def test_no_fail_is_exit_0(tmp_path):
    log = _log(tmp_path)
    log.case_start("a", "x")
    log.add(Check("PASS", "a reply", "ok"))
    log.case_end("a", 1.0)
    assert log.exit_code() == 0


def test_a_check_outside_a_case_goes_to_the_run_entry(tmp_path):
    log = _log(tmp_path)
    log.add(Check("FAIL", "run", "stopped with Ctrl-C"))
    assert log.cases["_run"].status == "FAIL"
    assert log.exit_code() == 1


def test_report_json_has_summary_checks_timing_screenshots_and_meta(tmp_path):
    log = _log(tmp_path)
    log.set_meta(vars={"prev_month_name": "October 2026"}, config={"team_id": "T1", "bot_name": "Pico FinOps Agent"},
                 agent_image="pico-finops-agent:nightly sha256:abc")
    log.case_start("qa_x", "How much?", group="qa", where="dm")
    log.reply("qa_x", ["Your cost was $1.00"])
    log.add(Check("FAIL", "qa_x figures", "total expected 2.00"))
    shot = os.path.join(log.dir, "shots", "qa_x.png")
    os.makedirs(os.path.dirname(shot))
    open(shot, "wb").write(b"png")
    log.screenshot("qa_x", shot)
    log.case_end("qa_x", 4.5)
    json_path, html_path = log.write()
    rep = json.load(open(json_path, encoding="utf-8"))
    assert rep["summary"]["cases"] == {"PASS": 0, "FAIL": 1, "SKIP": 0}
    assert rep["summary"]["checks"]["FAIL"] == 1
    assert rep["meta"]["agent_image"] == "pico-finops-agent:nightly sha256:abc"
    assert rep["meta"]["config"]["team_id"] == "T1"
    assert rep["meta"]["vars"]["prev_month_name"] == "October 2026"
    (case,) = rep["cases"]
    assert case["id"] == "qa_x" and case["status"] == "FAIL" and case["seconds"] == 4.5
    assert case["sent"] == "How much?" and case["group"] == "qa" and case["where"] == "dm"
    assert case["reply"] == "Your cost was $1.00"
    assert case["checks"] == [{"status": "FAIL", "name": "qa_x figures", "detail": "total expected 2.00"}]
    assert case["screenshot"] == "shots/qa_x.png"  # relative to the report
    assert os.path.isfile(html_path)


def test_report_html_is_self_contained_escaped_and_filterable(tmp_path):
    log = _log(tmp_path)
    log.case_start("qa_a", "<b>hi</b>")
    log.reply("qa_a", ["<script>alert(1)</script> " + "x" * 5000])
    log.add(Check("PASS", "qa_a reply", "ok"))
    log.case_end("qa_a", 2.0)
    log.case_start("qa_b", "second")
    log.add(Check("FAIL", "qa_b reply", "(No response generated)"))
    log.screenshot("qa_b", os.path.join(log.dir, "shots", "qa_b.png"))
    log.case_end("qa_b", 3.0)
    log.case_start("qa_c", "third")
    log.add(Check("SKIP", "qa_c", "needs mtd_month_name"))
    log.case_end("qa_c", 0.0)
    _, html_path = log.write()
    page = open(html_path, encoding="utf-8").read()
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&lt;b&gt;hi&lt;/b&gt;" in page
    assert "x" * 5000 not in page  # the reply is truncated
    assert not re.search(r"<script[^>]+src=|<link\b|@import", page)  # no external assets
    assert 'href="shots/qa_b.png"' in page
    assert 'data-status="PASS"' in page and 'data-status="FAIL"' in page and 'data-status="SKIP"' in page
    assert "<select" in page  # the status filter
    for label in ("PASS", "FAIL", "SKIP"):
        assert re.search(rf"{label}\D{{0,40}}1", page)


def test_reports_are_rewritten_after_each_case(tmp_path):
    log = _log(tmp_path)
    log.case_start("a", "x")
    log.add(Check("PASS", "a reply", "ok"))
    log.case_end("a", 1.0)
    rep = json.load(open(os.path.join(log.dir, "report.json"), encoding="utf-8"))
    assert rep["cases"][0]["id"] == "a"
