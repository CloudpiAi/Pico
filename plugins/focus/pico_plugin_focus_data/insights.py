"""Insights for the post-load Slack notice, computed with SQL from the local cache (never by the LLM).

Only usage rows count (tax rows would distort a day), and only full days are compared: the newest day in
an export is usually incomplete (AWS delivers hours late). That partial day is reported on its own (`partial_day`:
its date, hours in, cost so far) so the notice can say it is partly in, never as a day to compare. "Sudden increase" = the last full day is at least FOCUS_ALERT_INCREASE_PCT
above the 7-day average before it AND at least FOCUS_ALERT_MIN_SERVICE_USD (per service) or
FOCUS_ALERT_MIN_RESOURCE_USD (per resource) above it. Unset, empty or invalid settings use the defaults.
"""
import os
from dataclasses import dataclass
from datetime import timedelta

import duckdb

DEFAULTS = {"FOCUS_ALERT_INCREASE_PCT": 50.0, "FOCUS_ALERT_MIN_SERVICE_USD": 1.0, "FOCUS_ALERT_MIN_RESOURCE_USD": 0.5}
TOP = 3


def _env_number(name):
    try:
        value = float(os.environ.get(name, "").strip())
    except ValueError:
        return DEFAULTS[name]
    return value if value >= 0 else DEFAULTS[name]


@dataclass
class Thresholds:
    increase_pct: float = DEFAULTS["FOCUS_ALERT_INCREASE_PCT"]
    min_service_usd: float = DEFAULTS["FOCUS_ALERT_MIN_SERVICE_USD"]
    min_resource_usd: float = DEFAULTS["FOCUS_ALERT_MIN_RESOURCE_USD"]

    @classmethod
    def from_env(cls):
        return cls(_env_number("FOCUS_ALERT_INCREASE_PCT"), _env_number("FOCUS_ALERT_MIN_SERVICE_USD"),
                   _env_number("FOCUS_ALERT_MIN_RESOURCE_USD"))


def _last_full_day(max_end):
    """The newest day whose 24 hours are all in the data."""
    if (max_end.hour, max_end.minute, max_end.second, max_end.microsecond) == (0, 0, 0, 0):
        return (max_end - timedelta(days=1)).date()
    return max_end.date() - timedelta(days=1)


# One fixed query for both groupings: the column is chosen by a bound parameter, so no SQL text is ever built.
_MOVER_KEYS = ("servicename", "resourceid")
_MOVERS_SQL = """
    WITH s AS (SELECT CASE ? WHEN 'servicename' THEN servicename WHEN 'resourceid' THEN resourceid END AS k,
                      CAST(chargeperiodstart AS DATE) AS dt, sum(billedcost) AS cost
               FROM focus WHERE chargecategory = 'Usage' GROUP BY 1, 2)
    SELECT k,
           coalesce(sum(cost) FILTER (WHERE dt = ?), 0) AS today,
           coalesce(sum(cost) FILTER (WHERE dt BETWEEN ? AND ?), 0) / 7 AS avg7
    FROM s WHERE k IS NOT NULL GROUP BY 1"""


def _movers(c, key, day, minimum, pct):
    if key not in _MOVER_KEYS:
        raise ValueError(f"unknown grouping {key!r}; expected one of {_MOVER_KEYS}")
    rows = c.execute(_MOVERS_SQL, [key, day, day - timedelta(days=7), day - timedelta(days=1)]).fetchall()
    spikes = [(k, t, a) for k, t, a in rows
              if a > 0 and t - a >= minimum and (t - a) / a * 100 >= pct]
    stopped = [(k, t, a) for k, t, a in rows if t == 0 and a >= minimum]
    spikes.sort(key=lambda r: r[1] - r[2], reverse=True)
    stopped.sort(key=lambda r: r[2], reverse=True)
    return spikes[:TOP], stopped[:TOP]


def compute(cache_path, thresholds):
    c = duckdb.connect(cache_path, read_only=True)
    try:
        max_end = c.execute("SELECT max(chargeperiodend) FROM focus WHERE chargecategory = 'Usage'").fetchone()[0]
        if max_end is None:
            return None
        day = _last_full_day(max_end)

        def total(a, b):
            return c.execute("SELECT coalesce(sum(billedcost), 0) FROM focus WHERE chargecategory = 'Usage' "
                             "AND CAST(chargeperiodstart AS DATE) BETWEEN ? AND ?", [a, b]).fetchone()[0]

        month_start = day.replace(day=1)
        prev_month_start = (month_start - timedelta(days=1)).replace(day=1)
        try:
            prev_same_end = prev_month_start.replace(day=day.day)
        except ValueError:  # e.g. Mar 31 -> Feb has no 31st
            prev_same_end = month_start - timedelta(days=1)

        svc_spikes, svc_stopped = _movers(c, "servicename", day, thresholds.min_service_usd, thresholds.increase_pct)
        res_spikes, _ = _movers(c, "resourceid", day, thresholds.min_resource_usd, thresholds.increase_pct)
        new = c.execute("""
            WITH s AS (SELECT resourceid, CAST(chargeperiodstart AS DATE) AS dt, sum(billedcost) AS cost
                       FROM focus WHERE chargecategory = 'Usage' AND resourceid IS NOT NULL GROUP BY 1, 2)
            SELECT count(*), coalesce(sum(cost), 0) FROM s
            WHERE dt = ? AND resourceid NOT IN (SELECT resourceid FROM s WHERE dt BETWEEN ? AND ?)""",
                        [day, day - timedelta(days=30), day - timedelta(days=1)]).fetchone()
        partial = None
        if (max_end.hour, max_end.minute, max_end.second) != (0, 0, 0):  # the data stops inside a day
            pday = max_end.date()
            partial = {"day": pday.isoformat(), "hours": max_end.hour, "cost": round(total(pday, pday), 2)}
        return {
            "last_full_day": day.isoformat(),
            "partial_day": partial,
            "latest_day_cost": round(total(day, day), 2),
            "avg7": round(total(day - timedelta(days=7), day - timedelta(days=1)) / 7, 2),
            "mtd": {"cost": round(total(month_start, day), 2),
                    "prev_same_days": round(total(prev_month_start, prev_same_end), 2)},
            "spikes": [{"kind": "service", "name": k, "today": round(t, 2), "avg7": round(a, 2)} for k, t, a in svc_spikes]
            + [{"kind": "resource", "name": k, "today": round(t, 2), "avg7": round(a, 2)} for k, t, a in res_spikes],
            "stopped": [{"name": k, "avg7": round(a, 2)} for k, _, a in svc_stopped],
            "new_resources": {"count": int(new[0]), "cost": round(new[1], 2)},
            "thresholds": {"increase_pct": thresholds.increase_pct, "min_service_usd": thresholds.min_service_usd,
                           "min_resource_usd": thresholds.min_resource_usd},
        }
    finally:
        c.close()
