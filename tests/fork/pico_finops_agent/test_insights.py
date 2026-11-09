"""Post-load insights (plugins/focus insights.py): computed with SQL from the cache, never by the LLM.

Planted data: 8 days of baseline, then a last full day with a service spike, a resource spike, a stopped
service and a new resource; a tax row and a partial newest day that must not count.
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "focus"))
from pico_plugin_focus_data import insights  # noqa: E402

LAST = date(2026, 11, 4)


@pytest.fixture
def cache(tmp_path):
    path = tmp_path / "focus.duckdb"
    c = duckdb.connect(str(path))
    c.sql("CREATE TABLE focus (chargeperiodstart TIMESTAMP, chargeperiodend TIMESTAMP, billedcost DOUBLE, "
          "chargecategory VARCHAR, servicename VARCHAR, resourceid VARCHAR)")
    rows = []

    def day_row(d, svc, res, cost, cat="Usage"):
        start = datetime(d.year, d.month, d.day, 12)
        rows.append((start, start + timedelta(hours=1), cost, cat, svc, res))

    for i in range(1, 9):  # baseline: Oct 27 .. Nov 3
        d = LAST - timedelta(days=i)
        day_row(d, "Amazon EC2", "i-steady", 10.0)
        day_row(d, "Amazon EC2", "i-jumpy", 5.0)
        day_row(d, "Databricks", "db-1", 2.0)
    day_row(LAST, "Amazon EC2", "i-steady", 10.0)
    day_row(LAST, "Amazon EC2", "i-jumpy", 12.0)      # resource spike: +7 (+140%)
    day_row(LAST, "Amazon S3", "bucket-new", 0.7)     # new resource
    day_row(LAST, "Amazon EC2", "i-steady", 3.0, "Tax")  # tax: ignored
    c.executemany("INSERT INTO focus VALUES (?, ?, ?, ?, ?, ?)", rows)
    # partial newest day: data through Nov 5 06:00, so Nov 4 is the last full day
    c.execute("INSERT INTO focus VALUES (?, ?, ?, 'Usage', 'Amazon EC2', 'i-steady')",
              (datetime(2026, 11, 5, 5), datetime(2026, 11, 5, 6), 50.0))
    c.close()
    return str(path)


def test_last_full_day_and_totals(cache):
    out = insights.compute(cache, insights.Thresholds())
    assert out["last_full_day"] == "2026-11-04"
    assert out["latest_day_cost"] == pytest.approx(22.7)          # 10 + 12 + 0.7, no tax, no partial day
    assert out["avg7"] == pytest.approx(17.0)                      # 10 + 5 + 2 per day before


def test_spikes_stopped_and_new(cache):
    out = insights.compute(cache, insights.Thresholds())
    names = [(s["kind"], s["name"]) for s in out["spikes"]]
    assert ("resource", "i-jumpy") in names                       # +7 >= 0.5 and +140% >= 50%
    assert ("service", "Amazon EC2") not in names                 # 22 vs 15: +$7 but only +47% (< 50%)
    lower = insights.compute(cache, insights.Thresholds(increase_pct=40))
    assert ("service", "Amazon EC2") in [(s["kind"], s["name"]) for s in lower["spikes"]]
    assert [s["name"] for s in out["stopped"]] == ["Databricks"]  # 0 vs 2/day
    assert out["new_resources"] == {"count": 1, "cost": pytest.approx(0.7)}


def test_thresholds_decide(cache):
    strict = insights.compute(cache, insights.Thresholds(increase_pct=200, min_service_usd=1, min_resource_usd=0.5))
    assert strict["spikes"] == []                                  # nothing rose 200%
    big_floor = insights.compute(cache, insights.Thresholds(increase_pct=50, min_service_usd=100, min_resource_usd=100))
    assert big_floor["spikes"] == []                               # no rise of $100


def test_month_to_date(cache):
    out = insights.compute(cache, insights.Thresholds())
    assert out["mtd"]["cost"] == pytest.approx(22.7 + 3 * 17.0)    # Nov 1..4
    assert out["mtd"]["prev_same_days"] == pytest.approx(0.0)      # no Oct 1..4 rows planted


@pytest.mark.parametrize("env,expected", [
    ({}, (50.0, 1.0, 0.5)),
    ({"FOCUS_ALERT_INCREASE_PCT": "80", "FOCUS_ALERT_MIN_SERVICE_USD": "5", "FOCUS_ALERT_MIN_RESOURCE_USD": "2"}, (80.0, 5.0, 2.0)),
    ({"FOCUS_ALERT_INCREASE_PCT": "", "FOCUS_ALERT_MIN_SERVICE_USD": "abc"}, (50.0, 1.0, 0.5)),  # empty / bad -> default
])
def test_thresholds_from_env_with_defaults(monkeypatch, env, expected):
    for k in ("FOCUS_ALERT_INCREASE_PCT", "FOCUS_ALERT_MIN_SERVICE_USD", "FOCUS_ALERT_MIN_RESOURCE_USD"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    t = insights.Thresholds.from_env()
    assert (t.increase_pct, t.min_service_usd, t.min_resource_usd) == expected
