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


# ---------- the newest day is shown as partial (2026-11-11: "through 9 Nov" while 10 Nov had 20 of 24 hours) ----------

def test_partial_newest_day_is_reported(cache):
    out = insights.compute(cache, insights.Thresholds())
    assert out["partial_day"] == {"day": "2026-11-05", "hours": 6, "cost": 50.0}


def test_no_partial_day_when_the_data_ends_at_midnight(tmp_path):
    path = tmp_path / "focus.duckdb"
    c = duckdb.connect(str(path))
    c.sql("CREATE TABLE focus (chargeperiodstart TIMESTAMP, chargeperiodend TIMESTAMP, billedcost DOUBLE, "
          "chargecategory VARCHAR, servicename VARCHAR, resourceid VARCHAR)")
    c.execute("INSERT INTO focus VALUES (?, ?, 4.0, 'Usage', 'Amazon EC2', 'i-1')",
              (datetime(2026, 11, 9, 23), datetime(2026, 11, 10, 0)))
    c.close()
    out = insights.compute(str(path), insights.Thresholds())
    assert out["last_full_day"] == "2026-11-09" and out["partial_day"] is None


# ---------- a new export has less than a week of history: days without data are not days of zero spend ----------

def _flat_cache(tmp_path, days, cost_per_day=100.0):
    """An account that spends the same every day, with data for `days` full days ending on LAST."""
    path = tmp_path / "focus.duckdb"
    c = duckdb.connect(str(path))
    c.sql("CREATE TABLE focus (chargeperiodstart TIMESTAMP, chargeperiodend TIMESTAMP, billedcost DOUBLE, "
          "chargecategory VARCHAR, servicename VARCHAR, resourceid VARCHAR)")
    for i in range(days):
        d = LAST - timedelta(days=i)
        for hour in range(24):
            start = datetime(d.year, d.month, d.day, hour)
            c.execute("INSERT INTO focus VALUES (?, ?, ?, 'Usage', 'Amazon EC2', 'i-1')",
                      (start, start + timedelta(hours=1), cost_per_day / 24))
    c.close()
    return str(path)


def test_flat_spend_with_three_days_of_history_is_not_an_increase(tmp_path):
    out = insights.compute(_flat_cache(tmp_path, days=4), insights.Thresholds())

    assert out["latest_day_cost"] == pytest.approx(100.0)
    assert out["avg7"] == pytest.approx(100.0)                      # 3 days of $100, not $300 / 7
    assert out["spikes"] == []


def test_the_first_day_of_data_has_no_baseline_and_no_new_resources(tmp_path):
    out = insights.compute(_flat_cache(tmp_path, days=1), insights.Thresholds())

    assert out["avg7"] == 0
    assert out["spikes"] == [] and out["stopped"] == []
    assert out["new_resources"] == {"count": 0, "cost": 0}         # nothing to compare with, so nothing is "new"


def test_a_service_that_started_mid_week_still_counts_as_an_increase(tmp_path):
    path = _flat_cache(tmp_path, days=8, cost_per_day=10.0)
    c = duckdb.connect(path)
    for i in range(3):  # Amazon S3 only in the last 3 days, $20 each
        d = LAST - timedelta(days=i)
        c.execute("INSERT INTO focus VALUES (?, ?, 20.0, 'Usage', 'Amazon S3', 'bucket-1')",
                  (datetime(d.year, d.month, d.day, 12), datetime(d.year, d.month, d.day, 13)))
    c.close()

    out = insights.compute(path, insights.Thresholds())

    s3 = [s for s in out["spikes"] if s["name"] == "Amazon S3"]
    assert s3 and s3[0]["avg7"] == pytest.approx(40.0 / 7, abs=0.01)       # the account has a full week: S3 was $0 on 5 days
