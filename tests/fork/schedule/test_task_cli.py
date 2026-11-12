"""`pico schedule ...` (PRD R1, R3, R4, R11, R13, R14, R16): what the agent runs, end to end with a fake Slack.

Output is one JSON object; `message` is the plain sentence the agent passes on."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("apscheduler", reason="Needs the pico-finops-agent extra")

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "schedule"))
from pico_plugin_schedule import cli, slack_ctx, task_cli, tasks  # noqa: E402

from .test_slack_api import ALL_SCOPES, FakeSlack  # noqa: E402

RAVI, MEENA = "U0RAVI0001", "U0MEENA001"
NOW = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("FOCUS_REPORT_CHANNELS", "FOCUS_REPORT_ADMINS", "FOCUS_REPORT_HISTORY_MAX"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FOCUS_REPORT_CREATORS", f"{RAVI},{MEENA}")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    fake = FakeSlack(
        users_info={"ok": True, "user": {"tz": "Asia/Kolkata"}},
        conversations_open=lambda p: {"ok": True, "channel": {"id": "D0" + p["users"][2:]}},
        conversations_info=lambda p: {"ok": True, "channel": {"id": p["channel"],
                                                              "is_member": p["channel"] != "C0NOTIN001"}},
        conversations_list={"ok": True, "channels": [{"id": "C0FIN00001", "name": "finance"}],
                            "response_metadata": {"next_cursor": ""}},
        conversations_members=lambda p: {"ok": True, "members": [RAVI] if p["channel"] == "C0FIN00001"
                                         else [RAVI, MEENA], "response_metadata": {"next_cursor": ""}},
        chat_postMessage={"ok": True, "ts": "1.1"},
    )
    monkeypatch.setattr(task_cli, "slack_client", lambda: __import__("pico_plugin_schedule.slack_api",
                                                                       fromlist=["Client"]).Client("xoxb-test-token",
                                                                                                   transport=fake))
    monkeypatch.setattr(task_cli, "_now", lambda: NOW)
    profile = {"table_bucket_arn": "arn:aws:s3tables:us-east-1:111122223333:bucket/acme-focus",
               "export_bucket": "acme-focus-export", "export_prefix": "focus/acme-focus/data/",
               "cache_path": str(tmp_path / "focus.duckdb"), "data_dir": str(tmp_path),
               "source": "pico_plugin_focus_data.schedule_source:FocusSource"}
    key = slack_ctx.load_or_create_key(str(tmp_path))

    counter = iter(range(1, 10_000))

    def ctx(channel, user):
        """The ctx of a new message from `user` in `channel` (the newest one there)."""
        msg = f"1730800000.{next(counter):06d}"
        slack_ctx.remember_latest(str(tmp_path), channel, "", msg, user)
        return slack_ctx.TAG_RE.search(slack_ctx.make_tag(key, channel, user, "", msg)).group("ctx")

    def run(*argv):
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main([*argv], profile)
        return rc, json.loads(buf.getvalue())

    return run, ctx, fake, str(tmp_path)


def test_add_reminder_to_me_from_dm(env):
    run, ctx, fake, d = env
    rc, out = run("add", "--ctx", ctx("D0RAVIDM01", RAVI), "--kind", "reminder", "--to", "me",
                  "--name", "RI renewal", "--at", "2026-11-06 10:00", "--text", "check the RI renewal")
    assert rc == 0 and out["ok"] and out["task"]["target"] == {"type": "user", "id": RAVI}
    assert out["task"]["schedule"]["tz"] == "Asia/Kolkata"           # from the person's Slack profile
    assert "Fri 6 Nov" in out["next_run"] and "10:00 AM" in out["next_run"]
    assert tasks.history(d, out["task"]["id"])[0]["by"] == RAVI


@pytest.mark.parametrize("delay,local", [("2m", "2026-11-05 14:32"), ("15 minutes", "2026-11-05 14:45"),
                                         ("1h", "2026-11-05 15:30"), ("2 hours", "2026-11-05 16:30")])
def test_in_n_minutes_needs_no_clock(env, delay, local):
    """"In 2 minutes" (live 2026-11-12): the model has today's date but not the time, so it ran `date`, which is
    refused and stopped the answer. --in works the time out from now in the person's time zone."""
    run, ctx, fake, d = env
    rc, out = run("add", "--ctx", ctx("D0RAVIDM01", RAVI), "--kind", "reminder", "--to", "me",
                  "--name", "Check", "--in", delay, "--text", "check the run")
    assert rc == 0 and out["ok"], out
    assert out["task"]["schedule"] == {"at": local, "tz": "Asia/Kolkata"}


@pytest.mark.parametrize("delay", ["soon", "0m", "-5m", "3d", "2"])
def test_a_bad_in_is_refused_with_the_format(env, delay):
    run, ctx, fake, d = env
    rc, out = run("add", "--ctx", ctx("D0RAVIDM01", RAVI), "--kind", "reminder", "--to", "me",
                  "--name", "Check", "--in", delay, "--text", "x")
    assert rc == 1 and out["code"] == "bad_in" and "--in '10m'" in out["message"]


def test_in_with_cron_or_at_is_refused(env):
    run, ctx, fake, d = env
    rc, out = run("add", "--ctx", ctx("D0RAVIDM01", RAVI), "--kind", "reminder", "--to", "me", "--name", "C",
                  "--in", "2m", "--at", "2026-11-06 10:00", "--text", "x")
    assert rc == 1 and out["code"] == "bad_in"


def test_edit_with_in_moves_a_task(env):
    run, ctx, fake, d = env
    rc, out = run("add", "--ctx", ctx("D0RAVIDM01", RAVI), "--kind", "reminder", "--to", "me",
                  "--name", "RI renewal", "--at", "2026-11-06 10:00", "--text", "check the RI renewal")
    tid = out["task"]["id"]
    rc, out = run("edit", tid, "--ctx", ctx("D0RAVIDM01", RAVI), "--in", "30m")
    assert rc == 0 and out["task"]["schedule"]["at"] == "2026-11-05 15:00", out


def test_here_in_a_channel_and_named_channel(env):
    run, ctx, *_ = env
    _, out = run("add", "--ctx", ctx("C0TEAM0001", RAVI), "--kind", "reminder", "--to", "here", "--name", "n",
                 "--cron", "0 17 * * 5", "--text", "stop the dev instances")
    assert out["task"]["target"] == {"type": "channel", "id": "C0TEAM0001"}
    _, out = run("add", "--ctx", ctx("C0TEAM0001", RAVI), "--kind", "reminder", "--to", "#finance", "--name", "n",
                 "--cron", "0 17 * * 5", "--text", "x")
    assert out["task"]["target"] == {"type": "channel", "id": "C0FIN00001"}


def test_here_in_a_dm_means_me(env):
    run, ctx, *_ = env
    _, out = run("add", "--ctx", ctx("D0RAVIDM01", RAVI), "--kind", "reminder", "--to", "here", "--name", "n",
                 "--cron", "0 9 * * *", "--text", "x")
    assert out["task"]["target"] == {"type": "user", "id": RAVI}


def test_forged_context_refused(env):
    run, *_ = env
    rc, out = run("add", "--ctx", "C0TEAM0001.U0MEENA001.deadbeefdeadbeefdeadbeefdeadbeef", "--kind", "reminder",
                  "--to", "here", "--name", "n", "--cron", "0 9 * * *", "--text", "x")
    assert rc == 1 and out["code"] == "bad_context"


def test_bot_not_in_channel_says_so_and_saves_nothing(env):
    run, ctx, _, d = env
    rc, out = run("add", "--ctx", ctx("C0TEAM0001", RAVI), "--kind", "reminder", "--to", "C0NOTIN001", "--name", "n",
                  "--cron", "0 9 * * *", "--text", "x")
    assert rc == 1 and out["code"] == "not_in_channel" and "/invite" in out["message"]
    assert not Path(d, "tasks.json").exists() or json.loads(Path(d, "tasks.json").read_text())["tasks"] == {}


def test_report_add_returns_a_preview(env, monkeypatch):
    run, ctx, *_ = env
    monkeypatch.setattr(task_cli.task_kinds, "render", lambda t, *a, **k: ("PREVIEW TEXT", "2026-11-04"))
    _, out = run("add", "--ctx", ctx("D0RAVIDM01", RAVI), "--kind", "report", "--to", "me", "--name", "EC2 daily",
                 "--cron", "0 15 * * *", "--template", "service_cost", "--service", "Amazon Elastic Compute Cloud")
    assert out["ok"] and out["preview"] == "PREVIEW TEXT"
    assert out["task"]["payload"] == {"template": "service_cost", "filter": {"service": "Amazon Elastic Compute Cloud"}}


def test_someone_else_asks_to_stop_owner_gets_a_dm_then_confirms(env):
    run, ctx, fake, d = env
    _, out = run("add", "--ctx", ctx("C0TEAM0001", RAVI), "--kind", "reminder", "--to", "here", "--name", "Dev stop",
                 "--cron", "0 17 * * 5", "--text", "x")
    tid = out["task"]["id"]
    fake.calls.clear()
    rc, out = run("remove", tid, "--ctx", ctx("C0TEAM0001", MEENA))
    assert rc == 0 and out["code"] == "confirmation_requested" and "Ravi" not in out["message"]  # IDs, not guesses
    dm = [p for m, p in fake.calls if m == "chat.postMessage"]
    assert dm and dm[0]["channel"] == "D0RAVI0001" and f"<@{MEENA}>" in dm[0]["text"]
    rid = out["request"]["id"]
    assert rid in dm[0]["text"]
    fake.calls.clear()
    rc, out = run("confirm", rid, "--ctx", ctx("D0RAVIDM01", RAVI))
    assert rc == 0 and tasks.get(d, tid)["status"] == "removed"
    told = [p["channel"] for m, p in fake.calls if m == "chat.postMessage"]
    assert "C0TEAM0001" in told  # the channel where Meena asked hears the result


def test_who_and_history_for_anyone(env):
    run, ctx, *_ = env
    _, out = run("add", "--ctx", ctx("C0TEAM0001", RAVI), "--kind", "reminder", "--to", "here", "--name", "n",
                 "--cron", "0 17 * * 5", "--text", "x")
    tid = out["task"]["id"]
    rc, out = run("show", tid, "--ctx", ctx("C0OTHER001", MEENA))
    assert rc == 0 and out["task"]["owner"] == RAVI and out["task"]["created_at"]
    rc, out = run("history", tid, "--ctx", ctx("D0MEENADM1", MEENA))
    assert rc == 0 and [e["action"] for e in out["history"]] == ["created"]


def test_list_mine_and_this_channel(env):
    run, ctx, *_ = env
    run("add", "--ctx", ctx("D0RAVIDM01", RAVI), "--kind", "reminder", "--to", "me", "--name", "a",
        "--cron", "0 9 * * *", "--text", "x")
    run("add", "--ctx", ctx("C0TEAM0001", MEENA), "--kind", "reminder", "--to", "here", "--name", "b",
        "--cron", "0 9 * * *", "--text", "x")
    _, out = run("list", "--ctx", ctx("C0TEAM0001", RAVI))
    assert [t["name"] for t in out["tasks"]] == ["a", "b"]


def test_edit_time_by_owner(env):
    run, ctx, _, d = env
    _, out = run("add", "--ctx", ctx("D0RAVIDM01", RAVI), "--kind", "reminder", "--to", "me", "--name", "a",
                 "--cron", "0 15 * * *", "--text", "x")
    tid = out["task"]["id"]
    rc, out = run("edit", tid, "--ctx", ctx("D0RAVIDM01", RAVI), "--cron", "0 9 * * *")
    assert rc == 0 and tasks.get(d, tid)["schedule"]["cron"] == "0 9 * * *"
    assert tasks.history(d, tid)[-1]["action"] == "time_changed"


def test_manifest_allows_task_commands():
    import yaml

    m = yaml.safe_load((REPO_ROOT / "plugins/schedule/pico_plugin_schedule/pico-plugin.yml").read_text())
    actions = ("add", "list", "show", "history", "remove", "pause", "resume", "edit", "confirm", "decline")
    for profile in ("normal", "auto"):  # every action, and any form of it: "*:*" (live 2026-11-12)
        assert m["permissions"][profile]["allow"] == ["*:*"]
        assert {"--config:*", "--ctx * --config:*"} <= set(m["permissions"][profile]["deny"])
    assert [c["name"] for c in m["commands"]] == list(actions)
    assert ALL_SCOPES  # scope list documented in test_slack_api


def test_ctx_of_an_older_message_refused(env):
    run, ctx, *_ = env
    old = ctx("C0TEAM0001", RAVI)
    ctx("C0TEAM0001", MEENA)  # someone wrote after it
    rc, out = run("add", "--ctx", old, "--kind", "reminder", "--to", "me", "--name", "n", "--cron", "0 9 * * *",
                  "--text", "x")
    assert rc == 1 and out["code"] == "stale_context"


def test_named_channel_needs_the_requester_in_it(env):
    run, ctx, *_ = env
    rc, out = run("add", "--ctx", ctx("C0TEAM0001", MEENA), "--kind", "reminder", "--to", "#finance", "--name", "n",
                  "--cron", "0 9 * * *", "--text", "x")
    assert rc == 1 and out["code"] == "not_a_member"


def test_edit_query_sql_and_report_template(env, monkeypatch):
    run, ctx, _, d = env
    monkeypatch.setattr(task_cli.task_kinds, "render", lambda t, *a, **k: ("PREVIEW", "2026-11-04"))
    _, out = run("add", "--ctx", ctx("D0RAVIDM01", RAVI), "--kind", "query", "--to", "me", "--name", "q",
                 "--cron", "0 9 * * *", "--sql", "SELECT 1")
    q = out["task"]["id"]
    rc, out = run("edit", q, "--ctx", ctx("D0RAVIDM01", RAVI), "--sql", "SELECT 2")
    assert rc == 0 and tasks.get(d, q)["payload"] == {"sql": "SELECT 2"}
    rc, out = run("edit", q, "--ctx", ctx("D0RAVIDM01", RAVI), "--sql", "DELETE FROM focus")
    assert rc == 1 and out["code"] == "bad_sql"
    _, out = run("add", "--ctx", ctx("D0RAVIDM01", RAVI), "--kind", "report", "--to", "me", "--name", "r",
                 "--cron", "0 9 * * *", "--template", "month_to_date")
    r = out["task"]["id"]
    rc, _ = run("edit", r, "--ctx", ctx("D0RAVIDM01", RAVI), "--template", "service_cost", "--service", "Amazon S3")
    assert rc == 0 and tasks.get(d, r)["payload"] == {"template": "service_cost", "filter": {"service": "Amazon S3"}}


def test_change_request_dm_says_what_would_change(env):
    run, ctx, fake, _ = env
    _, out = run("add", "--ctx", ctx("C0TEAM0001", RAVI), "--kind", "reminder", "--to", "here", "--name", "Dev stop",
                 "--cron", "0 17 * * 5", "--text", "stop the dev instances")
    fake.calls.clear()
    run("edit", out["task"]["id"], "--ctx", ctx("C0TEAM0001", MEENA), "--text", "go home", "--cron", "0 8 * * 1")
    dm = [p for m, p in fake.calls if m == "chat.postMessage"][0]["text"]
    assert "go home" in dm and "0 8 * * 1" in dm


def test_unattended_runs_cannot_manage_tasks(env, monkeypatch):
    run, ctx, *_ = env
    monkeypatch.setenv("PICO_FINOPS_AGENT_UNATTENDED", "1")  # set for the `ask` child process
    rc, out = run("add", "--ctx", ctx("D0RAVIDM01", RAVI), "--kind", "reminder", "--to", "me", "--name", "n",
                  "--cron", "0 9 * * *", "--text", "x")
    assert rc == 1 and out["code"] == "not_from_a_scheduled_run"


def test_unattended_runs_cannot_start_a_load(env, monkeypatch):
    monkeypatch.setenv("PICO_FINOPS_AGENT_UNATTENDED", "1")
    profile = {"table_bucket_arn": "arn:aws:s3tables:us-east-1:111122223333:bucket/acme-focus",
               "export_bucket": "acme-focus-export", "export_prefix": "focus/acme-focus/data/",
               "cache_path": env[3] + "/focus.duckdb"}
    from pico_plugin_focus_data import cli as focus_cli

    assert focus_cli.main(["load"], profile) == 2



def test_unlisted_person_can_only_schedule_for_their_own_dm(env, monkeypatch):
    run, ctx, *_ = env
    monkeypatch.setenv("FOCUS_REPORT_CREATORS", RAVI)
    rc, out = run("add", "--ctx", ctx("C0TEAM0001", MEENA), "--kind", "reminder", "--to", "here", "--name", "n",
                  "--cron", "0 9 * * *", "--text", "x")
    assert rc == 1 and out["code"] == "not_a_creator" and "DM" in out["message"]
    rc, out = run("add", "--ctx", ctx("C0TEAM0001", MEENA), "--kind", "reminder", "--to", "me", "--name", "n",
                  "--cron", "0 9 * * *", "--text", "x")
    assert rc == 0
