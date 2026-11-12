"""The expected answers, from a copy of the agent's focus.duckdb.

The agent opens /data/focus.duckdb read-only and the loader swaps it with os.replace, so the test never opens the live
file: `snapshot` copies it out of the container (`docker cp`, no shell) and `Truth` reads the copy read-only.

`Truth.vars()` gives the placeholders the catalog uses. The previous month is the newest *complete* month, so most
questions ask about it; the newest month is month-to-date. A value that the data can't give (an empty table, a single
month) is left out, and a case that needs it is SKIP.
"""

import re
import subprocess
from datetime import datetime
from typing import Callable, Optional

import duckdb

CACHE_IN_CONTAINER = "/data/focus.duckdb"
_CONTAINER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class SnapshotError(Exception):
    pass


def snapshot(container: str, dest: str, run: Callable = subprocess.run) -> str:
    """Copy the container's focus.duckdb to `dest` and return `dest`."""
    if not _CONTAINER.match(container or ""):
        raise SnapshotError(f"not a container name: {container!r}")
    proc = run(["docker", "cp", f"{container}:{CACHE_IN_CONTAINER}", dest], capture_output=True, text=True,
               timeout=120)
    if proc.returncode != 0:
        raise SnapshotError(f"docker cp failed: {(proc.stderr or '').strip()[:300]}")
    return dest


def _month(ts: datetime) -> tuple[str, str, str]:
    return ts.strftime("%Y-%m"), ts.strftime("%B %Y"), ts.strftime("%Y-%m-01 00:00:00")


class Truth:
    def __init__(self, path: str):
        self._c = duckdb.connect(path, read_only=True, config={"enable_external_access": False})

    def close(self) -> None:
        self._c.close()

    def _row(self, sql: str, params=()) -> Optional[tuple]:
        return self._c.execute(sql, params).fetchone()

    def value(self, sql: str) -> Optional[float]:
        """The first cell of the first row as a number; None when there is no row or it is NULL."""
        row = self._row(sql)
        if not row or row[0] is None:
            return None
        return float(row[0])

    def text(self, sql: str) -> Optional[str]:
        """The first cell of the first row as text (a bucket, an account, a region); None when there is no row,
        or it is NULL or empty."""
        row = self._row(sql)
        if not row or row[0] is None or str(row[0]).strip() == "":
            return None
        return str(row[0]).strip()

    def vars(self) -> dict[str, str]:
        """newest_*: the newest month in the data; mtd_*: the same month, only when it is still partial;
        prev_*: the newest COMPLETE month (the newest month itself once its usage runs to the month's end)."""
        v: dict[str, str] = {}
        months = [r[0] for r in self._c.execute(
            "SELECT DISTINCT billingperiodstart FROM focus WHERE billingperiodstart IS NOT NULL "
            "ORDER BY 1 DESC LIMIT 2").fetchall()]
        if not months:
            return v
        v["newest_month"], v["newest_month_name"], v["newest_month_start"] = _month(months[0])
        through = self._row("SELECT max(chargeperiodend) FROM focus WHERE chargecategory = 'Usage'")
        through_ts = through[0] if through and through[0] is not None else None
        if through_ts is not None:
            v["data_through"] = through_ts.strftime("%Y-%m-%d")
        newest = months[0]
        next_month = newest.replace(year=newest.year + (newest.month == 12), month=newest.month % 12 + 1)
        partial = through_ts is None or through_ts < next_month
        if partial:
            v["mtd_month_name"], v["mtd_month_start"] = v["newest_month_name"], v["newest_month_start"]
            if len(months) < 2:
                return v
            prev = months[1]
        else:
            prev = newest
        v["prev_month"], v["prev_month_name"], v["prev_month_start"] = _month(prev)
        # ranked on every charge, like the expected cost the catalog's SQL sums
        top = self._c.execute(
            "SELECT servicename FROM focus WHERE billingperiodstart = ? AND servicename IS NOT NULL "
            "GROUP BY 1 ORDER BY sum(billedcost) DESC LIMIT 2", [prev]).fetchall()
        if top:
            v["top_service"] = str(top[0][0])
        if len(top) > 1:
            v["second_service"] = str(top[1][0])
        for key, col in (("top_account", "subaccountid"), ("top_region", "regionid")):
            row = self._row(
                f"SELECT {col} FROM focus WHERE billingperiodstart = ? AND {col} IS NOT NULL "
                f"GROUP BY 1 ORDER BY sum(billedcost) DESC LIMIT 1", [prev])
            if row:
                v[key] = str(row[0])
        return v
