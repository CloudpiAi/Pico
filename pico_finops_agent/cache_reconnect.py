"""Make the agent see a new cache file without restarting the agent.

Pico keeps one DBManager per process with an open DuckDB connection, and DuckDB shares one database
instance per file path inside a process: after the loader swaps /data/focus.duckdb (os.replace), every
query keeps reading the old file until all connections to it close. This manager closes a datasource's
connectors when the file behind it changed (inode), so the next query opens the new file.

Installed through Pico's public hook `pico.tools.db_tools.db_manager.set_db_manager_factory` (no core
change) by pico_finops_agent/agent.py, before Pico's Slack gateway starts.
"""
import os
import threading

from pico.tools.db_tools import db_manager as dbm


def _file_of(cfg):
    if getattr(cfg, "type", "") != "duckdb":
        return None
    uri = cfg.uri or cfg.database or ""
    return uri[len("duckdb:///"):] if uri.startswith("duckdb:///") else uri or None


def _inode(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


class ReconnectingDBManager(dbm.DBManager):
    def __init__(self, db_configs):
        super().__init__(db_configs)
        self._seen = {}
        self._check_lock = threading.Lock()

    def _close_datasource(self, datasource):
        group = self._conn_dict.get(datasource)
        if not isinstance(group, dict):
            return
        for name, conn in list(group.items()):
            try:
                if conn is not None:
                    conn.close()
            finally:
                group.pop(name, None)

    def _check(self, datasource):
        path = _file_of(self._db_configs.get(datasource))
        if not path:
            return
        ino = _inode(path)
        with self._check_lock:
            if datasource in self._seen and self._seen[datasource] != ino:
                self._close_datasource(datasource)
            self._seen[datasource] = ino

    def get_conn(self, datasource, database=""):
        self._check(datasource)
        return super().get_conn(datasource, database)

    def get_connections(self, datasource=""):
        if datasource:
            self._check(datasource)
        return super().get_connections(datasource)


def install():
    """One manager per datasource set (like Pico's own CLI cache), reconnecting on a file swap."""
    cache = {}
    lock = threading.Lock()

    def factory(db_configs):
        key = frozenset((db_configs or {}).keys())
        with lock:
            m = cache.get(key)
            if m is None:
                m = cache[key] = ReconnectingDBManager(db_configs or {})
            return m

    dbm.set_db_manager_factory(factory)
    return factory
