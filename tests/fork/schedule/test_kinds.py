"""What each task kind posts, with any data source: the plugin renders, the host's Source gives the
figures. Another host can use the plugin with its own Source, or NoData (reminders and asks only)."""
from datetime import date, datetime, timezone

import pytest

from pico_plugin_schedule import kinds, source, tasks

NOW = datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc)
S = tasks.Settings()


class FakeSource:
    templates = ("anything",)
    supports_query = True

    def __init__(self, day=date(2026, 11, 8), body=("• X",)):
        self.day, self.body, self.calls = day, body, []

    def check_report(self, payload):
        return payload

    def check_query(self, sql):
        return sql

    def read(self, kind, payload):
        self.calls.append(kind)
        return self.day, (list(self.body) if self.body is not None else None)


def task(kind, payload, **kw):
    return {"id": "t9", "kind": kind, "name": "Daily", "owner": "U0RAVI0001", "payload": payload,
            "last_data_through": None, **kw}


def test_render_uses_the_source():
    src = FakeSource()
    text, through = kinds.render(task("report", {"template": "anything"}), src, S, NOW)
    assert "_Data through Sun 8 Nov_" in text and "• X" in text and through == "2026-11-08"
    assert src.calls == ["report"]


def test_source_with_nothing_to_send_posts_nothing():
    assert kinds.render(task("query", {"sql": "q"}), FakeSource(body=None), S, NOW)[0] is None


def test_reminder_never_reads_the_source():
    src = FakeSource()
    text, _ = kinds.render(task("reminder", {"text": "stand-up"}), src, S, NOW)
    assert "Reminder: stand-up" in text and src.calls == []


def test_no_data_source_allows_reminders_and_asks_only():
    nd = source.load("", {})
    assert isinstance(nd, source.NoData) and nd.templates == () and nd.supports_query is False
    for check, arg in ((nd.check_report, {"template": "x"}), (nd.check_query, "SELECT 1")):
        with pytest.raises(tasks.TaskError) as e:
            check(arg)
        assert e.value.code == "bad_kind"


def test_load_builds_the_named_source():
    src = source.load("pico_plugin_schedule.source:NoData", {"data_dir": "/tmp"})
    assert isinstance(src, source.NoData)
