"""The FOCUS data source for the schedule plugin (pico_plugin_schedule): report templates and saved queries on the
local DuckDB cache, and the newest full day of data ("Data through").

The cache is read in a short-lived child process (`python -m pico_plugin_focus_data.schedule_source`): in the
one-process agent Pico's own datasource keeps focus.duckdb open with other settings, and DuckDB refuses a second
in-process connection to the same file with a different configuration. The child also keeps the query limits
(1 GB, 2 threads, 60 s) to itself. Imported only when the schedule plugin is used.
"""
import calendar
import json
import os
import re
import subprocess
import sys
import threading
from datetime import date, timedelta

import duckdb

from pico_plugin_schedule import child
from pico_plugin_schedule.kinds import RenderError
from pico_plugin_schedule.tasks import TaskError

from .insights import _last_full_day

TEMPLATES = ("service_cost", "cost_by_service", "top_resources", "month_to_date", "account_cost")
QUERY_TIMEOUT_S = 60
QUERY_MEMORY = "1GB"
MAX_ROWS = 25
MAX_SQL = 4000
_SQL_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_CHILD_ENV_DROP = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")


def _money(x):
    return f"${x:,.2f}"


def _day_label(d):
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def check_sql(sql):
    """The saved SQL if it is one read-only SELECT / WITH statement, else TaskError('bad_sql')."""
    s = (sql or "").strip()
    if s.endswith(";"):
        s = s[:-1].rstrip()
    if not s or len(s) > MAX_SQL or ";" in s or not _SQL_START.match(s):
        raise TaskError("bad_sql", "A saved query must be one SELECT (or WITH ... SELECT) statement.")
    return s


def _connect(cache_path, memory=None):
    return duckdb.connect(cache_path, read_only=True,
                          config={"enable_external_access": False, "memory_limit": memory or QUERY_MEMORY, "threads": 2})


def _data_day(c):
    max_end = c.execute("SELECT max(chargeperiodend) FROM focus WHERE chargecategory = 'Usage'").fetchone()[0]
    return _last_full_day(max_end) if max_end is not None else None


def _where(flt):
    cols = {"service": "servicename", "account": "subaccountid", "region": "regionid"}
    parts, args = [], []
    for k, col in cols.items():
        if (flt or {}).get(k):
            parts.append(f"{col} = ?")
            args.append(flt[k])
    return "".join(f" AND {p}" for p in parts), args


def _total(c, a, b, flt):
    w, args = _where(flt)
    return c.execute("SELECT coalesce(sum(billedcost), 0) FROM focus WHERE chargecategory = 'Usage' "
                     f"AND CAST(chargeperiodstart AS DATE) BETWEEN ? AND ?{w}", [a, b, *args]).fetchone()[0]


def _by(c, col, day, flt, limit=10):
    """[(key, last-day cost, month-to-date cost)], biggest last day first."""
    w, args = _where(flt)
    return c.execute(f"""
        SELECT {col} AS k,
               coalesce(sum(billedcost) FILTER (WHERE CAST(chargeperiodstart AS DATE) = ?), 0) AS d,
               coalesce(sum(billedcost) FILTER (WHERE CAST(chargeperiodstart AS DATE) BETWEEN ? AND ?), 0) AS m
        FROM focus WHERE chargecategory = 'Usage' AND {col} IS NOT NULL{w}
        GROUP BY 1 HAVING m > 0 ORDER BY d DESC, m DESC LIMIT {int(limit)}""",
                     [day, day.replace(day=1), day, *args]).fetchall()


DATABRICKS_NOTE = ("_Databricks here means the charges billed through AWS Marketplace; usage billed by Databricks "
                   "directly isn't in the AWS export._")


def _template(c, payload, day):
    lines = _template_lines(c, payload, day)
    if any("databricks" in str(v).lower() for v in (payload.get("filter") or {}).values()):
        lines.append(DATABRICKS_NOTE)  # decided 2026-11-06: say what the number covers
    return lines


def _template_lines(c, payload, day):
    name, flt = payload["template"], payload.get("filter") or {}
    month = calendar.month_name[day.month]
    what = " · ".join(v for v in flt.values()) or "all services"
    if name == "service_cost":
        return [f"• {what} on {_day_label(day)}: *{_money(_total(c, day, day, flt))}*",
                f"• {month} so far: *{_money(_total(c, day.replace(day=1), day, flt))}*"]
    if name == "month_to_date":
        start = day.replace(day=1)
        prev_start = (start - timedelta(days=1)).replace(day=1)
        try:
            prev_end = prev_start.replace(day=day.day)
        except ValueError:
            prev_end = start - timedelta(days=1)
        cost, prev = _total(c, start, day, flt), _total(c, prev_start, prev_end, flt)
        line = f"• {month} so far ({what}): *{_money(cost)}*"
        if prev > 0:
            change = (cost - prev) / prev * 100
            how = "about the same as" if abs(change) < 10 else f"{abs(round(change))}% " \
                                                               f"{'higher' if change > 0 else 'lower'} than"
            line += f", {how} the same days in {calendar.month_name[prev_start.month]}"
        return [line]
    col, label = {"cost_by_service": ("servicename", "Service"), "top_resources": ("resourceid", "Resource"),
                  "account_cost": ("subaccountid", "Account")}[name]
    rows = _by(c, col, day, flt)
    if not rows:
        return [f"• No {label.lower()} costs for {what} yet this month."]
    return [f"{label}  ·  {_day_label(day)}  ·  {month} so far"] + \
        [f"• `{k}`: *{_money(dcost)}*  ·  {_money(mcost)}" for k, dcost, mcost in rows]


def _query(c, sql, timeout=None):
    timer = threading.Timer(timeout or QUERY_TIMEOUT_S, c.interrupt)  # a runaway query is stopped, not left running
    timer.start()
    try:
        cur = c.execute(check_sql(sql))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(MAX_ROWS + 1)
    finally:
        timer.cancel()
    lines = [" | ".join(cols)] + [" | ".join("" if v is None else str(v) for v in r) for r in rows[:MAX_ROWS]]
    if len(rows) > MAX_ROWS:
        lines.append(f"(first {MAX_ROWS} rows)")
    return ["```", *lines, "```"] if rows else None  # no rows: nothing to send


def _cache_part(kind, payload, cache_path, timeout=None, memory=None):
    """(newest full day, body lines or None) from the cache. Runs inside the child process."""
    c = _connect(cache_path, memory)
    try:
        day = _data_day(c)
        body = []
        if day is not None and kind == "report":
            body = _template(c, payload, day)
        elif day is not None and kind == "query":
            body = _query(c, payload["sql"], timeout)
        return day, body
    finally:
        c.close()


def _child_env():
    env = {k: v for k, v in os.environ.items() if k not in _CHILD_ENV_DROP}
    import pico_plugin_schedule

    parents = [os.path.dirname(os.path.dirname(os.path.abspath(f)))
               for f in (__file__, pico_plugin_schedule.__file__)]
    env["PYTHONPATH"] = os.pathsep.join(p for p in (*parents, env.get("PYTHONPATH")) if p)
    return env


def _read_cache(kind, payload, cache_path):
    """_cache_part in a child process, so its DuckDB connection never meets Pico's in this process."""
    req = {"kind": kind, "payload": payload, "cache_path": cache_path, "timeout": QUERY_TIMEOUT_S,
           "memory": QUERY_MEMORY}
    try:
        r = child.run([sys.executable, "-m", __name__], input=json.dumps(req), timeout=QUERY_TIMEOUT_S + 60,
                      env=_child_env())  # its own process group: a stuck read is killed with all it started
    except subprocess.TimeoutExpired as e:
        raise RenderError("reading the cost data took too long") from e
    try:
        out = json.loads((r.stdout or "").strip().splitlines()[-1])
    except (IndexError, ValueError):
        print(json.dumps({"event": "cache_read_failed", "rc": r.returncode, "stderr": (r.stderr or "")[-500:]}),
              flush=True)
        raise RenderError(f"reading the cost data failed (exit {r.returncode}; details in the agent log)") from None
    if out.get("error"):
        raise RenderError(out["error"])
    return (date.fromisoformat(out["day"]) if out.get("day") else None), out.get("body")


def _child_main():
    """`python -m pico_plugin_focus_data.schedule_source`: one cache read; request JSON on stdin, result JSON on stdout."""
    req = json.loads(sys.stdin.read())
    try:
        day, body = _cache_part(req["kind"], req["payload"], req["cache_path"], req.get("timeout"), req.get("memory"))
        out = {"day": day.isoformat() if day else None, "body": body}
    except Exception as e:  # noqa: BLE001 - reported to the parent as a RenderError
        out = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    print(json.dumps(out), flush=True)


class FocusSource:
    """pico_plugin_schedule's Source on the FOCUS cache. `config`: the schedule plugin's settings (cache_path, or
    data_dir with focus.duckdb in it)."""

    templates = TEMPLATES
    supports_query = True

    def __init__(self, config=None):
        config = config or {}
        data_dir = config.get("data_dir") or os.environ.get("FOCUS_DATA_DIR") or "/data"
        self.cache_path = config.get("cache_path") or os.path.join(data_dir, "focus.duckdb")

    def check_report(self, payload):
        """The checked report payload: a known template; service_cost needs the service."""
        payload = payload or {}
        if payload.get("template") not in TEMPLATES:
            raise TaskError("bad_payload", f"A report needs one of: {', '.join(TEMPLATES)}.")
        flt = {k: v for k, v in (payload.get("filter") or {}).items() if k in ("service", "account", "region") and v}
        if payload["template"] == "service_cost" and not flt.get("service"):
            raise TaskError("bad_payload", "The service_cost report needs the service name (--service).")
        return {"template": payload["template"], **({"filter": flt} if flt else {})}

    def check_query(self, sql):
        return check_sql(sql)

    def read(self, kind, payload):
        """(newest full day or None, body lines or None = nothing to send); [] for kinds without a body."""
        if not os.path.exists(self.cache_path):
            return None, []
        return _read_cache(kind, payload, self.cache_path)


if __name__ == "__main__":
    _child_main()
