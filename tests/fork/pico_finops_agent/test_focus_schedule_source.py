"""FocusSource (the standalone agent's data source for scheduled tasks) and what each task kind posts on it (PRD R9, R10, R18): reminder text, report templates and saved SELECTs run on the
read-only cache (no LLM), `ask` runs `pico -p` and posts its final answer. Data tasks say how fresh the data is."""
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("apscheduler", reason="Needs the pico-finops-agent extra")

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "schedule"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "focus"))
from pico_plugin_focus_data import schedule_source  # noqa: E402
from pico_plugin_schedule import kinds as task_kinds  # noqa: E402
from pico_plugin_schedule import tasks  # noqa: E402


def render(task, cache, *args, **kw):
    """Render a task on the FOCUS cache at `cache` (the standalone agent's data source)."""
    return task_kinds.render(task, schedule_source.FocusSource({"cache_path": cache}), *args, **kw)

LAST = date(2026, 11, 4)
NOW = datetime(2026, 11, 5, 9, 30, tzinfo=timezone.utc)
EC2 = "Amazon Elastic Compute Cloud"


@pytest.fixture
def cache(tmp_path):
    path = tmp_path / "focus.duckdb"
    c = duckdb.connect(str(path))
    c.sql("CREATE TABLE focus (chargeperiodstart TIMESTAMP, chargeperiodend TIMESTAMP, billedcost DOUBLE, "
          "chargecategory VARCHAR, servicename VARCHAR, resourceid VARCHAR, subaccountid VARCHAR, regionid VARCHAR)")
    rows = []
    for i in range(0, 5):  # Oct 31 .. Nov 4
        d = LAST - timedelta(days=i)
        s = datetime(d.year, d.month, d.day, 12)
        rows += [(s, s + timedelta(hours=1), 10.0, "Usage", EC2, "i-1", "111122223333", "us-east-1"),
                 (s, s + timedelta(hours=1), 4.0, "Usage", "Amazon Simple Storage Service", "b-1", "111122223333",
                  "us-east-1"),
                 (s, s + timedelta(hours=1), 1.0, "Usage", EC2, "i-2", "444455556666", "eu-west-1")]
    rows.append((datetime(2026, 11, 1), datetime(2026, 12, 1), 3.0, "Tax", EC2, None, "111122223333", None))
    # data reaches into Nov 5, so Nov 4 is the last full day
    rows.append((datetime(2026, 11, 5, 0), datetime(2026, 11, 5, 1), 0.0, "Usage", EC2, "i-1", "111122223333",
                 "us-east-1"))
    c.executemany("INSERT INTO focus VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    c.close()
    return str(path)


def task(kind, payload, **kw):
    t = {"id": "t3", "kind": kind, "name": "EC2 cost daily", "owner": "U0RAVI0001", "payload": payload,
         "last_data_through": None}
    t.update(kw)
    return t


S = tasks.Settings()


# ---------- reminder ----------

def test_reminder_posts_text_with_footer(cache):
    text, through = render(task("reminder", {"text": "stop the dev instances"}), cache, S, NOW)
    assert "Reminder: stop the dev instances" in text and through is None
    assert "Set up by <@U0RAVI0001>" in text and "stop task t3" in text
    assert "Data through" not in text


def test_reminder_mentions_off_by_default():
    out = task_kinds.sanitize_mentions("<!channel> stand-up @here <!everyone> <!subteam^S1>", allow=False)
    assert "<!" not in out and "@here" not in out
    assert task_kinds.sanitize_mentions("@here stand-up", allow=True) == "<!here> stand-up"


# ---------- report templates ----------

def test_service_cost_template(cache):
    text, through = render(task("report", {"template": "service_cost", "filter": {"service": EC2}}),
                                      cache, S, NOW)
    assert through == "2026-11-04"
    assert "Data through Wed 4 Nov" in text
    assert "$11.00" in text            # Nov 4: 10 + 1 for EC2, tax excluded
    assert "$44.00" in text            # Nov 1..4 month to date for EC2
    assert EC2 in text


def test_cost_by_service_and_accounts(cache):
    text, _ = render(task("report", {"template": "cost_by_service"}), cache, S, NOW)
    assert text.index(EC2) < text.index("Amazon Simple Storage Service")  # biggest first
    text, _ = render(task("report", {"template": "account_cost"}), cache, S, NOW)
    assert "111122223333" in text and "444455556666" in text


def test_top_resources_and_month_to_date(cache):
    text, _ = render(task("report", {"template": "top_resources", "filter": {"service": EC2}}), cache, S, NOW)
    assert "i-1" in text and "b-1" not in text
    text, _ = render(task("report", {"template": "month_to_date"}), cache, S, NOW)
    assert "$60.00" in text  # Nov 1..4: 15/day


def test_no_new_data_line(cache):
    t = task("report", {"template": "month_to_date"}, last_data_through="2026-11-04")
    text, _ = render(t, cache, S, NOW)
    assert "No new data since the last report" in text and "Data through Wed 4 Nov" in text


def test_no_cache_yet(tmp_path):
    text, through = render(task("report", {"template": "month_to_date"}), str(tmp_path / "none.duckdb"),
                                      S, NOW)
    assert "not loaded yet" in text and through is None


# ---------- saved query ----------

@pytest.mark.parametrize("sql", [
    "SELECT servicename, sum(billedcost) FROM focus GROUP BY 1",
    "with x as (select 1 as a) select * from x;",
    "  SELECT 1  ",
])
def test_select_allowed(sql):
    assert schedule_source.check_sql(sql)


@pytest.mark.parametrize("sql", [
    "DELETE FROM focus",
    "SELECT 1; DROP TABLE focus",
    "ATTACH 'x.db' AS x",
    "COPY focus TO '/tmp/x.csv'",
    "",
    "SELECT " + "x" * 5000,
])
def test_other_sql_refused(sql):
    with pytest.raises(tasks.TaskError) as e:
        schedule_source.check_sql(sql)
    assert e.value.code == "bad_sql"


def test_query_runs_read_only(cache):
    t = task("query", {"sql": "SELECT servicename, round(sum(billedcost), 2) AS cost FROM focus "
                              "WHERE chargecategory = 'Usage' GROUP BY 1 ORDER BY 2 DESC"})
    text, through = render(t, cache, S, NOW)
    assert EC2 in text and "55.0" in text and through == "2026-11-04"


def test_query_cannot_read_other_files(cache, tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("a\n1\n")
    t = task("query", {"sql": f"SELECT * FROM read_csv('{secret}')"})
    with pytest.raises(task_kinds.RenderError, match="IO Error|Permission|disabled"):  # external access is off
        render(t, cache, S, NOW)


# ---------- ask ----------

def _payload(text, depth=0, typ="markdown"):
    return json.dumps({"message_id": "m", "role": "assistant", "depth": depth,
                       "content": [{"type": typ, "payload": {"content": text}}]})


def test_ask_parses_final_answer():
    lines = [_payload("thinking...", typ="thinking-delta"), _payload("sub step", depth=1),
             _payload("EC2 went up 12% because of i-1."), "not json"]
    assert task_kinds.final_answer(lines) == "EC2 went up 12% because of i-1."


def test_ask_runs_pico_print(cache, monkeypatch):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": _payload("S3 changed most.") + "\n", "stderr": ""})()

    text, through = render(task("ask", {"question": "what changed most last week?"}), cache, S, NOW,
                                      runner=runner)
    assert calls[0][1:4] == ["-m", "pico_plugin_schedule.hooks.ask_child", "-p"]  # pico -p behind the bash guard
    assert calls[0][4].startswith("what changed most last week?")
    assert task_kinds.SKIP_TOKEN in calls[0][4]  # the agent is told how to say "nothing to send"
    assert "S3 changed most." in text and "Data through Wed 4 Nov" in text and through == "2026-11-04"


def test_ask_failure_raises(cache):
    def runner(cmd, **kw):
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()

    with pytest.raises(task_kinds.RenderError):
        render(task("ask", {"question": "q"}), cache, S, NOW, runner=runner)


def test_mentions_removed_from_names_and_results(cache):
    t = task("report", {"template": "month_to_date"}, name="<!channel> EC2 @here")
    text, _ = render(t, cache, tasks.Settings(allow_mentions=True), NOW)
    assert "<!channel>" not in text and "@here" not in text and "<!here>" not in text
    q = task("query", {"sql": "SELECT '<!everyone> hi' AS x"})
    text, _ = render(q, cache, S, NOW)
    assert "<!everyone>" not in text
    assert "<@U0RAVI0001>" in text  # the footer's user mention stays


def test_ask_child_gets_no_slack_tokens_and_is_marked_unattended(cache, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-secret")
    seen = {}

    def runner(cmd, **kw):
        seen.update(kw["env"])
        return type("R", (), {"returncode": 0, "stdout": _payload("ok") + "\n", "stderr": ""})()

    render(task("ask", {"question": "q"}), cache, S, NOW, runner=runner)
    assert "SLACK_BOT_TOKEN" not in seen and "SLACK_APP_TOKEN" not in seen
    assert seen["PICO_FINOPS_AGENT_UNATTENDED"] == "1"


def test_ask_error_text_is_not_posted(cache):
    def runner(cmd, **kw):
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "Traceback ... key=sk-live-abc"})()

    with pytest.raises(task_kinds.RenderError) as e:
        render(task("ask", {"question": "q"}), cache, S, NOW, runner=runner)
    assert "sk-live" not in str(e.value)


# pico -p prints a failed run as a depth-0 "thinking" part: "chat interaction failed: <error>" (agentic_node error
# action -> build_thinking_content). t57 (2026-11-12) failed this way and was reported as "the agent gave no answer".
@pytest.mark.parametrize("error", [
    "chat interaction failed: Maximum turns (15) exceeded - agent execution stopped",
    "chat interaction failed: error_code=300022, error_message=Maximum turns (15) exceeded - agent execution stopped",
    "chat interaction failed: Max turns (15) exceeded",
    "chat interaction failed: the run stopped\nMaximum turns (15) exceeded - agent execution stopped",
])
def test_ask_that_hits_the_step_limit_says_so(cache, error):
    def runner(cmd, **kw):
        out = _payload("Let me check the buckets.", typ="thinking") + "\n" + _payload(error, typ="thinking") + "\n"
        return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()

    with pytest.raises(task_kinds.RenderError) as e:
        render(task("ask", {"question": "S3 cost by bucket"}), cache, S, NOW, runner=runner)
    assert "needed more than 15 steps" in str(e.value) and "narrow" in str(e.value)
    assert e.value.spent


def test_ask_with_no_answer_and_no_step_limit_keeps_the_old_reason(cache):
    def runner(cmd, **kw):
        return type("R", (), {"returncode": 0, "stdout": _payload("hmm", typ="thinking") + "\n", "stderr": ""})()

    with pytest.raises(task_kinds.RenderError, match="the agent gave no answer"):
        render(task("ask", {"question": "q"}), cache, S, NOW, runner=runner)


@pytest.mark.parametrize("line", [
    json.dumps({"depth": 0, "content": {"type": "thinking"}}),
    json.dumps({"depth": 0, "content": ["Maximum turns (15) exceeded"]}),
    json.dumps({"depth": 0, "content": [{"type": "thinking", "payload": "interaction failed: Maximum turns (15) exceeded"}]}),
    json.dumps(["not", "a", "message"]),
])
def test_odd_output_lines_never_break_the_turn_limit_check(line):
    assert task_kinds.turn_limit([line]) is None


def test_step_limit_text_in_a_sub_agent_is_not_the_run_failing(cache):
    """Only the top-level run (depth 0) failing counts; a sub-step quoting the words doesn't."""
    def runner(cmd, **kw):
        out = _payload("Maximum turns (15) exceeded", depth=1, typ="thinking") + "\n" + _payload("") + "\n"
        return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()

    with pytest.raises(task_kinds.RenderError, match="the agent gave no answer"):
        render(task("ask", {"question": "q"}), cache, S, NOW, runner=runner)


def test_runaway_query_is_stopped(cache, monkeypatch):
    monkeypatch.setattr(schedule_source, "QUERY_TIMEOUT_S", 0.5)
    t = task("query", {"sql": "SELECT count(*) FROM range(100000000000) a"})
    with pytest.raises(Exception):  # noqa: B017 - DuckDB raises InterruptException
        render(t, cache, S, NOW)



def test_databricks_report_says_it_is_the_aws_marketplace_part(tmp_path):
    """D2: Databricks cost in the AWS export is only what AWS Marketplace bills; the report says so."""
    path = tmp_path / "focus.duckdb"
    c = duckdb.connect(str(path))
    c.sql("CREATE TABLE focus (chargeperiodstart TIMESTAMP, chargeperiodend TIMESTAMP, billedcost DOUBLE, "
          "chargecategory VARCHAR, servicename VARCHAR, resourceid VARCHAR, subaccountid VARCHAR, regionid VARCHAR)")
    c.execute("INSERT INTO focus VALUES (?, ?, 7.5, 'Usage', 'Databricks Data Intelligence Platform', 'db', 'a', 'r')",
              (datetime(2026, 11, 4, 12), datetime(2026, 11, 5, 1)))
    c.close()
    t = task("report", {"template": "service_cost", "filter": {"service": "Databricks Data Intelligence Platform"}})
    text, _ = render(t, str(path), S, NOW)
    assert "$7.50" in text and "billed through AWS Marketplace" in text
    t = task("report", {"template": "service_cost", "filter": {"service": EC2}})
    text, _ = render(t, str(path), S, NOW)
    assert "AWS Marketplace" not in text



# ---------- conditional tasks: "only if ..." (decided 2026-11-06) ----------

@pytest.mark.parametrize("answer", ["NOTHING_TO_SEND", "  NOTHING_TO_SEND.  ", "`NOTHING_TO_SEND`", "**NOTHING_TO_SEND**"])
def test_ask_that_finds_nothing_sends_nothing(cache, answer):
    def runner(cmd, **kw):
        return type("R", (), {"returncode": 0, "stdout": _payload(answer) + "\n", "stderr": ""})()

    text, through = render(task("ask", {"question": "if EC2 > $200 list resources"}), cache, S, NOW,
                                      runner=runner)
    assert text is None and through == "2026-11-04"


def test_ask_answer_that_only_mentions_the_word_is_still_sent(cache):
    long = "EC2 was $246 (over $200). Top: i-1 $88. (Not NOTHING_TO_SEND because the limit was passed.)"

    def runner(cmd, **kw):
        return type("R", (), {"returncode": 0, "stdout": _payload(long) + "\n", "stderr": ""})()

    text, _ = render(task("ask", {"question": "q"}), cache, S, NOW, runner=runner)
    assert text is not None and "$246" in text


def test_query_without_rows_sends_nothing(cache):
    t = task("query", {"sql": "SELECT resourceid, sum(billedcost) AS c FROM focus WHERE servicename = "
                              "'Amazon Elastic Compute Cloud' GROUP BY 1 HAVING sum(billedcost) > 200"})
    text, through = render(t, cache, S, NOW)
    assert text is None and through == "2026-11-04"


# ---------- live check 2026-11-10: one process, Pico already has the cache open ----------

def test_report_runs_while_the_agent_has_the_cache_open(cache):
    """In the one-process agent Pico's own datasource keeps focus.duckdb open (read_only, external access off).
    DuckDB refuses a second in-process connection with other settings ("Can't open a connection to same database
    file with a different configuration"), so t2 was paused. The report must still render."""
    pico_conn = duckdb.connect(cache, read_only=True, config={"enable_external_access": False})
    try:
        text, through = render(task("report", {"template": "service_cost", "filter": {"service": EC2}}),
                                          cache, S, NOW)
    finally:
        pico_conn.close()
    assert "EC2 cost daily" in text and through == "2026-11-04"


def test_saved_query_runs_while_the_agent_has_the_cache_open(cache):
    pico_conn = duckdb.connect(cache, read_only=True, config={"enable_external_access": False})
    try:
        text, _ = render(task("query", {"sql": "SELECT count(*) AS n FROM focus"}), cache, S, NOW)
    finally:
        pico_conn.close()
    assert "n" in text and "17" in text


# ---------- FocusSource checks (moved from the task store with the split, 2026-11-10) ----------

def test_report_templates_are_the_sources():
    src = schedule_source.FocusSource({"cache_path": "/nowhere.duckdb"})
    assert src.templates == ("service_cost", "cost_by_service", "top_resources", "month_to_date", "account_cost")
    with pytest.raises(tasks.TaskError) as e:
        src.check_report({"template": "nope"})
    assert e.value.code == "bad_payload"


def test_service_cost_needs_a_service():
    with pytest.raises(tasks.TaskError) as e:
        schedule_source.FocusSource({}).check_report({"template": "service_cost"})
    assert e.value.code == "bad_payload"


def test_no_cache_yet_reads_nothing(tmp_path):
    assert schedule_source.FocusSource({"cache_path": str(tmp_path / "none.duckdb")}).read("report", {}) == (None, [])
