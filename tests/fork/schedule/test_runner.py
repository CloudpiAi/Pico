"""The task runner on its own: Another host needs the report scheduler without the FOCUS
plugin or the standalone agent. With NoLoads data tasks are never held; with a host gate they wait for its load."""
import builtins
import sys
from datetime import datetime, timedelta, timezone

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from pico_plugin_schedule import runner, slack_api, tasks

from .test_slack_api import FakeSlack

RAVI = "U0RAVI0001"
NOW = datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc)
ME = {"type": "user", "id": RAVI}


class FakeGate:
    def __init__(self):
        self.is_running, self.next_at, self.fresh = False, None, False

    def running(self):
        return self.is_running

    def next_load_at(self):
        return self.next_at

    def fresh_since_last_slot(self, now):
        return self.fresh


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    for k in ("FOCUS_REPORT_CHANNELS", "FOCUS_ASK_MAX_PER_DAY", "FOCUS_REPORT_CREATORS"):
        monkeypatch.delenv(k, raising=False)
    fake = FakeSlack(conversations_open=lambda p: {"ok": True, "channel": {"id": "D0" + p["users"][2:]}},
                     chat_postMessage={"ok": True, "ts": "1.1"})
    monkeypatch.setattr(runner, "_utcnow", lambda: NOW)
    monkeypatch.setattr(runner, "_HELD", {})
    monkeypatch.setattr(runner.kinds, "render", lambda t, *a, **k: (f"TEXT {t['id']}", "2026-11-08"))
    d = str(tmp_path)

    def setup(gate=None):
        runner.configure(data_dir=d, gate=gate,
                         slack_client_factory=lambda: slack_api.Client("xoxb-test-token", transport=fake))
        return gate

    def add(kind="report", payload=None):
        return tasks.add(d, owner=RAVI, where="D0RAVI0001", kind=kind, name="n", target=ME,
                         schedule={"cron": "0 9 * * *", "tz": "UTC"},
                         payload=payload or {"template": "anything"}, now=NOW - timedelta(hours=1),
                         settings=tasks.Settings())

    def posts():
        return [p["text"] for m, p in fake.calls if m == "chat.postMessage"]

    return setup, add, posts


def test_no_loads_never_holds_a_data_task(host):
    setup, add, posts = host
    setup()  # NoLoads: a host without a data load
    t = add()
    runner.run_task(t["id"])
    assert posts() == [f"TEXT {t['id']}"] and runner._HELD == {}


def test_host_gate_holds_until_on_load_done(host):
    setup, add, posts = host
    gate = setup(FakeGate())
    gate.next_at = NOW + timedelta(minutes=15)
    t = add()
    runner.run_task(t["id"])
    assert posts() == [] and t["id"] in runner._HELD
    runner.on_load_done(0)
    assert posts() == [f"TEXT {t['id']}"]


def test_fresh_host_data_is_not_held(host):
    setup, add, posts = host
    gate = setup(FakeGate())
    gate.next_at, gate.fresh = NOW + timedelta(minutes=15), True
    t = add()
    runner.run_task(t["id"])
    assert posts() == [f"TEXT {t['id']}"]


def test_install_adds_the_sync_job():
    sched = BackgroundScheduler()
    runner.install(sched, data_dir="/tmp/x")
    assert [j.id for j in sched.get_jobs()] == ["tasks_sync"]


def test_runner_alone_without_focus_or_agent(host, monkeypatch):
    """Only pico_plugin_schedule: the FOCUS plugin and the agent can't be imported, a reminder and an ask still go out."""
    for name in [m for m in sys.modules if m.startswith(("pico_plugin_focus_data", "pico_finops_agent"))]:
        monkeypatch.delitem(sys.modules, name)
    real = builtins.__import__

    def blocked(name, *a, **k):
        if name.startswith(("pico_plugin_focus_data", "pico_finops_agent")):
            raise ImportError(f"not installed here: {name}")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    setup, add, posts = host
    setup()
    r, q = add(kind="reminder", payload={"text": "stand-up"}), add(kind="ask", payload={"question": "anything new?"})
    sched = BackgroundScheduler()
    runner.install(sched, data_dir=runner.data_dir(), slack_client_factory=runner._CFG["client"])
    runner.tasks_sync(sched)
    assert {f"task_{r['id']}", f"task_{q['id']}"} <= {j.id for j in sched.get_jobs()}
    runner.run_task(r["id"])
    runner.run_task(q["id"])
    assert posts() == [f"TEXT {r['id']}", f"TEXT {q['id']}"]
