"""V6: after the cache file is swapped (os.replace), the agent's next query sees the new file.

DuckDB shares one database instance per path inside a process, so every connection to the file must
close; pico_finops_agent.cache_reconnect's DB manager does that when the file's inode changes."""
import os

import pytest

duckdb = pytest.importorskip("duckdb")

from pico.configuration.agent_config import DbConfig  # noqa: E402
from pico.tools.db_tools import db_manager as dbm  # noqa: E402

from pico_finops_agent import cache_reconnect  # noqa: E402


def _build(path, rows):
    c = duckdb.connect(str(path))
    c.sql(f"CREATE TABLE focus AS SELECT range AS v FROM range({rows})")
    c.close()


def _count(conn):
    res = conn.execute_query("SELECT count(*) AS n FROM focus", result_format="csv")
    assert res.success, res
    return int(res.sql_return.splitlines()[1])


def _manager(path):
    return cache_reconnect.ReconnectingDBManager(
        {"focus": DbConfig(type="duckdb", uri=f"duckdb:///{path}", database="focus", extra={"read_only": True})})


def test_swapped_file_is_seen_on_next_query(tmp_path):
    cache, new = tmp_path / "focus.duckdb", tmp_path / "focus.new.duckdb"
    _build(cache, 1)
    m = _manager(cache)
    assert _count(m.get_conn("focus")) == 1
    _build(new, 5)
    os.replace(new, cache)
    assert _count(m.get_conn("focus")) == 5
    _build(new, 9)
    os.replace(new, cache)
    assert _count(m.get_conn("focus")) == 9
    m.close()


def test_same_file_keeps_the_connection(tmp_path):
    cache = tmp_path / "focus.duckdb"
    _build(cache, 2)
    m = _manager(cache)
    first = m.get_conn("focus")
    _count(first)
    assert m.get_conn("focus") is first
    m.close()


def test_missing_file_then_created(tmp_path):
    cache = tmp_path / "focus.duckdb"
    m = _manager(cache)
    m._check("focus")  # no file yet: nothing to open, no error
    _build(cache, 3)
    assert _count(m.get_conn("focus")) == 3
    m.close()


def test_swap_during_a_running_query_does_not_break_it(tmp_path):
    """close() and every query hold the connector's lock: a swap seen by another thread waits for the
    running query, and the old connector reconnects on its next call."""
    import threading

    cache, new = tmp_path / "focus.duckdb", tmp_path / "focus.new.duckdb"
    _build(cache, 1)
    m = _manager(cache)
    old = m.get_conn("focus")
    _count(old)
    result = {}

    def long_query():
        r = old.execute_query("SELECT count(*) AS n FROM range(300000000) t(x) WHERE x % 7 = 0", result_format="csv")
        result["ok"] = r.success

    t = threading.Thread(target=long_query)
    t.start()
    _build(new, 4)
    os.replace(new, cache)
    assert _count(m.get_conn("focus")) == 4  # other thread: new file
    t.join(timeout=120)
    assert result.get("ok") is True  # the running query finished normally
    assert _count(old) == 4  # the old connector object reconnected to the new file
    m.close()


def test_install_sets_a_caching_factory(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr(dbm, "set_db_manager_factory", lambda f: calls.setdefault("f", f))
    cache_reconnect.install()
    f = calls["f"]
    cfgs = {"focus": DbConfig(type="duckdb", uri=f"duckdb:///{tmp_path}/x.duckdb")}
    assert f(cfgs) is f(cfgs)
    assert isinstance(f(cfgs), cache_reconnect.ReconnectingDBManager)
