"""loader/pico_finops_agent_loader.py: the pico-finops-agent-loader Lambda: the invoke contract, what runs inside the Lambda, and the table lifecycle.

Pure functions are tested directly; the handler runs against fake S3 / S3 Tables clients and a local
DuckDB database standing in for the Iceberg catalog (``s3t``), with local Parquet files as deliveries.
"""
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("boto3", reason="Needs the pico-finops-agent extra: uv sync --extra pico-finops-agent")

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "loader"))
import pico_finops_agent_loader as fl  # noqa: E402

GOOD = {
    "force": False,
    "periods": None,
    "source": {"export_bucket": "acme-focus-export", "export_prefix": "focus/acme/data/"},
    "target": {"table_bucket_arn": "arn:aws:s3tables:us-east-1:111122223333:bucket/acme-focus",
               "namespace": "focus", "table": "focus"},
    "state": {"bucket": "acme-focus-state", "key": "pico-finops-agent/state.json"},
}


def _event(**over):
    ev = json.loads(json.dumps(GOOD))
    for path, value in over.items():
        cur = ev
        keys = path.split(".")
        for k in keys[:-1]:
            cur = cur[k]
        if value is _DROP:
            cur.pop(keys[-1])
        else:
            cur[keys[-1]] = value
    return ev


_DROP = object()


# ---------- payload check ----------

def test_good_payload_is_accepted():
    ev = fl.validate(_event(), region="us-east-1")
    assert ev["target"]["table"] == "focus" and ev["force"] is False and ev["periods"] is None
    assert ev["months"] == 4  # the default when the payload doesn't say


def test_months_from_payload():
    assert fl.validate(_event(months=12), region="us-east-1")["months"] == 12


@pytest.mark.parametrize("value", [0, -1, 121, "4", 2.5, True, None])
def test_bad_months_refused(value):
    with pytest.raises(fl.BadRequest, match="months"):
        fl.validate(_event(months=value), region="us-east-1")


@pytest.mark.parametrize(
    "path,value",
    [
        ("source.export_bucket", "Bad_Bucket"),
        ("source.export_bucket", _DROP),
        ("source.export_prefix", "/abs/"),
        ("source.export_prefix", "no/slash"),
        ("source.export_prefix", "a/../b/"),
        ("source.export_prefix", "a/b'c/"),
        ("target.table_bucket_arn", "arn:aws:s3:::bucket"),
        ("target.table_bucket_arn", "arn:aws:s3tables:eu-west-1:111122223333:bucket/acme-focus"),
        ("target.namespace", "focus;drop"),
        ("target.namespace", "focus\n"),
        ("source.export_bucket", "acme-focus-export\n"),
        ("target.table", "Focus"),
        ("state.bucket", "x"),
        ("state.key", "focus/state.txt"),
        ("state.key", "../state.json"),
        ("periods", ["2026-13"]),
        ("periods", "2026-10"),
        ("force", "yes"),
    ],
)
def test_bad_payload_names_the_field(path, value):
    with pytest.raises(fl.BadRequest) as e:
        fl.validate(_event(**{path: value}), region="us-east-1")
    assert path.split(".")[-1] in str(e.value)


def test_handler_bad_request_touches_nothing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no AWS client may be created for a bad payload")

    monkeypatch.setattr(fl, "_client", boom)
    out = fl.lambda_handler(_event(**{"target.table": "DROP"}), None)
    assert out["status"] == "bad_request" and "table" in out["error"]


# ---------- month selection ----------

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
AVAIL = {"2026-09": "u9", "2026-10": "u10", "2026-11": "u11"}


def test_first_run_backfills_everything_oldest_first():
    state = fl.empty_state()
    assert list(fl.select_periods(state, AVAIL, NOW, force=False, periods=None)) == ["2026-09", "2026-10", "2026-11"]


LONG = {f"2026-{m:02d}": f"u{m}" for m in range(2, 12)}  # Feb..Nov 2026, like the real export


def test_month_window_counts_the_current_month():
    assert fl.month_window(NOW, 4) == ["2026-08", "2026-09", "2026-10", "2026-11"]
    assert fl.month_window(NOW, 1) == ["2026-11"]
    assert fl.month_window(datetime(2026, 2, 3, tzinfo=timezone.utc), 4) == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_first_run_loads_only_the_last_n_months():
    assert list(fl.select_periods(fl.empty_state(), LONG, NOW, force=False, periods=None)) == [
        "2026-08", "2026-09", "2026-10", "2026-11"]
    assert list(fl.select_periods(fl.empty_state(), LONG, NOW, force=False, periods=None, months=2)) == [
        "2026-10", "2026-11"]


def test_a_larger_limit_fills_in_the_months_never_loaded():
    state = {**fl.empty_state(), "backfill_done": True,
             "loaded": {p: LONG[p] for p in ("2026-08", "2026-09", "2026-10", "2026-11")}}
    assert fl.select_periods(state, LONG, NOW, force=False, periods=None, months=6) == {
        "2026-06": "u6", "2026-07": "u7"}


def test_explicit_periods_outside_the_window_are_ignored():
    assert fl.select_periods(fl.empty_state(), LONG, NOW, force=True, periods=["2026-03", "2026-10"]) == {
        "2026-10": "u10"}


def test_after_backfill_only_due_months_with_new_deliveries():
    state = {**fl.empty_state(), "backfill_done": True,
             "loaded": {"2026-09": "old9", "2026-10": "old10", "2026-11": "old"}}
    assert fl.select_periods(state, AVAIL, NOW, force=False, periods=None) == {"2026-11": "u11"}
    state["loaded"]["2026-11"] = "u11"
    assert fl.select_periods(state, AVAIL, NOW, force=False, periods=None) == {}


def test_force_and_explicit_periods():
    state = {**fl.empty_state(), "backfill_done": True, "loaded": dict(AVAIL)}
    assert fl.select_periods(state, AVAIL, NOW, force=True, periods=None) == {"2026-11": "u11"}
    assert fl.select_periods(state, AVAIL, NOW, force=True, periods=["2026-09"]) == {"2026-09": "u9"}
    assert fl.select_periods(state, AVAIL, NOW, force=False, periods=["2026-09"]) == {}


def test_early_month_includes_previous_month():
    early = datetime(2026, 11, 3, tzinfo=timezone.utc)
    state = {**fl.empty_state(), "backfill_done": True, "loaded": {"2026-09": "old9"}}
    assert list(fl.select_periods(state, AVAIL, early, force=False, periods=None)) == ["2026-10", "2026-11"]


def test_newest_folder_ignores_names_with_sql_characters():
    base = "focus/acme/data/billing_period=2026-10/"
    keys = [base + "2026-11-01T23:09:48.098Z-c079028e-d969-3a86-a07d-bf59f6c526d1/p.parquet",
            base + "2026-11-02T15:07:11.328Z-e2891bd6-5d2b-3e19-83a2-6eda07f5202e/p.parquet",
            base + "zzz']); DROP TABLE x; --/p.parquet",
            base + "2026-11-03T00:00:00.000Z-x/notes.txt"]

    class S3:
        def get_paginator(self, n):
            return types.SimpleNamespace(paginate=lambda **kw: [{"Contents": [{"Key": k} for k in keys]}])

    assert fl.newest_folder(S3(), "b", "focus/acme/data/", "2026-10") == (
        "2026-11-02T15:07:11.328Z-e2891bd6-5d2b-3e19-83a2-6eda07f5202e")


# ---------- schema check + insert SQL ----------

def test_schema_diff():
    table = [("billedcost", "DOUBLE"), ("servicename", "VARCHAR"), ("tags", "VARCHAR")]
    assert fl.schema_diff(table, [("BilledCost", "DOUBLE"), ("ServiceName", "VARCHAR")]) is None  # missing → NULL, ok
    assert fl.schema_diff(table, [("BilledCost", "DOUBLE"), ("NewCol", "BIGINT")]) == {
        "added": ["newcol BIGINT"], "type_changed": []}
    assert fl.schema_diff(table, [("BilledCost", "VARCHAR")]) is None  # a changed type is cast at insert, not a diff


TABLE = [("billedcost", "DOUBLE"), ("servicename", "VARCHAR"), ("tags", "VARCHAR")]


def test_select_exprs_use_table_column_order_and_null_for_missing():
    exprs, casts = fl.select_exprs(TABLE, [("ServiceName", "VARCHAR"), ("BilledCost", "DOUBLE")])
    assert exprs == ['"BilledCost"', '"ServiceName"', "NULL"] and casts == []
    assert fl.insert_sql('s3t."focus"."focus"', TABLE, exprs, "SRC") == (
        'INSERT INTO s3t."focus"."focus" ("billedcost", "servicename", "tags") '
        'SELECT "BilledCost", "ServiceName", NULL FROM SRC')


def test_select_exprs_cast_a_changed_type_to_the_table_type():
    exprs, casts = fl.select_exprs(TABLE, [("BilledCost", "FLOAT"), ("ServiceName", "VARCHAR")])
    assert exprs == ['CAST("BilledCost" AS DOUBLE)', '"ServiceName"', "NULL"]
    assert casts == ["billedcost FLOAT -> DOUBLE"]


# ---------- month replace against a local DuckDB table ----------

def _parquet(path, rows, month="2026-10-01 00:00:00+00", extra_col=False, drop_tags=False, cost=None,
             month_type="TIMESTAMPTZ", drop_month=False):
    """cost: SQL for BilledCost (default a DOUBLE); month_type: the type BillingPeriodStart is written with."""
    c = duckdb.connect()
    c.sql("SET TimeZone = 'UTC'")
    cols = [] if drop_month else [f"CAST(TIMESTAMPTZ '{month}' AS {month_type}) AS BillingPeriodStart"]
    cols += [f"{cost or 'CAST(range * 1.5 AS DOUBLE)'} AS BilledCost", "'svc' AS ServiceName"]
    if not drop_tags:
        cols.append("'t' AS Tags")
    if extra_col:
        cols.append("42 AS NewCol")
    c.sql(f"COPY (SELECT {', '.join(cols)} FROM range({rows})) TO '{path}' (FORMAT parquet)")
    c.close()
    return str(path)


@pytest.fixture
def catalog(tmp_path):
    c = duckdb.connect(str(tmp_path / "catalog.duckdb"))
    c.sql("SET TimeZone = 'UTC'")
    c.sql("ATTACH ':memory:' AS s3t")
    c.sql("CREATE SCHEMA s3t.focus")
    return c


def test_create_table_from_delivery_lowercases_columns(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "PARTITION_CLAUSE", "")  # local DuckDB has no Iceberg partitioning
    src = fl.source_sql(_parquet(tmp_path / "a.parquet", 3))
    fl.create_table(catalog, 's3t."focus"."focus"', src)
    names = [r[0] for r in catalog.sql('DESCRIBE s3t."focus"."focus"').fetchall()]
    assert names == ["billingperiodstart", "billedcost", "servicename", "tags"]


def test_replace_month_is_delete_plus_insert(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "PARTITION_CLAUSE", "")
    fq = 's3t."focus"."focus"'
    fl.create_table(catalog, fq, fl.source_sql(_parquet(tmp_path / "a.parquet", 3)))
    assert fl.replace_month(catalog, fq, _parquet(tmp_path / "a.parquet", 3)) == (3, [])
    assert fl.replace_month(catalog, fq, _parquet(tmp_path / "b.parquet", 5)) == (5, [])  # same month replaced
    assert catalog.sql(f"SELECT count(*) FROM {fq}").fetchone()[0] == 5
    fl.replace_month(catalog, fq, _parquet(tmp_path / "c.parquet", 2, month="2026-09-01 00:00:00+00"))
    assert catalog.sql(f"SELECT count(*) FROM {fq}").fetchone()[0] == 7


def test_missing_column_loads_as_null(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "PARTITION_CLAUSE", "")
    fq = 's3t."focus"."focus"'
    fl.create_table(catalog, fq, fl.source_sql(_parquet(tmp_path / "a.parquet", 1)))
    fl.replace_month(catalog, fq, _parquet(tmp_path / "b.parquet", 2, drop_tags=True))
    assert catalog.sql(f"SELECT count(*) FROM {fq} WHERE tags IS NULL").fetchone()[0] == 2


def test_new_column_raises_schema_changed_and_writes_nothing(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "PARTITION_CLAUSE", "")
    fq = 's3t."focus"."focus"'
    fl.create_table(catalog, fq, fl.source_sql(_parquet(tmp_path / "a.parquet", 1)))
    fl.replace_month(catalog, fq, _parquet(tmp_path / "a.parquet", 1))
    with pytest.raises(fl.SchemaChanged) as e:
        fl.replace_month(catalog, fq, _parquet(tmp_path / "b.parquet", 4, extra_col=True))
    assert e.value.diff == {"added": ["newcol INTEGER"], "type_changed": []}
    assert catalog.sql(f"SELECT count(*) FROM {fq}").fetchone()[0] == 1


def _types(catalog, fq):
    return dict(catalog.sql(f"SELECT column_name, column_type FROM (DESCRIBE {fq})").fetchall())


@pytest.mark.parametrize("cost,delivery_type", [("CAST(range * 1.5 AS FLOAT)", "FLOAT"),
                                                ("CAST(range * 1.5 AS VARCHAR)", "VARCHAR"),
                                                ("CAST(range * 1.5 AS DECIMAL(18, 4))", "DECIMAL(18,4)")])
def test_changed_type_is_cast_to_the_table_type(catalog, tmp_path, monkeypatch, cost, delivery_type):
    """The table keeps the type it was created with; a delivery with another type is cast and loaded."""
    monkeypatch.setattr(fl, "PARTITION_CLAUSE", "")
    fq = 's3t."focus"."focus"'
    fl.create_table(catalog, fq, fl.source_sql(_parquet(tmp_path / "a.parquet", 1)))
    rows, casts = fl.replace_month(catalog, fq, _parquet(tmp_path / "b.parquet", 4, cost=cost))
    assert rows == 4 and casts == [f"billedcost {delivery_type} -> DOUBLE"]
    assert _types(catalog, fq)["billedcost"] == "DOUBLE"
    assert catalog.sql(f"SELECT sum(billedcost) FROM {fq}").fetchone()[0] == pytest.approx(0 + 1.5 + 3.0 + 4.5)


def test_month_column_with_another_type_is_cast_and_still_replaces_its_month(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "PARTITION_CLAUSE", "")
    fq = 's3t."focus"."focus"'
    fl.create_table(catalog, fq, fl.source_sql(_parquet(tmp_path / "a.parquet", 3)))
    fl.replace_month(catalog, fq, _parquet(tmp_path / "a.parquet", 3))
    rows, casts = fl.replace_month(catalog, fq, _parquet(tmp_path / "b.parquet", 2, month_type="TIMESTAMP"))
    assert rows == 2 and casts == ["billingperiodstart TIMESTAMP -> TIMESTAMP WITH TIME ZONE"]
    assert catalog.sql(f"SELECT count(*) FROM {fq}").fetchone()[0] == 2  # the old 3 rows were replaced, not kept


def test_value_that_cannot_be_cast_stops_with_schema_changed_and_writes_nothing(catalog, tmp_path, monkeypatch):
    """A strict CAST: a value that doesn't fit the table's type is never turned into NULL or a wrong number."""
    monkeypatch.setattr(fl, "PARTITION_CLAUSE", "")
    fq = 's3t."focus"."focus"'
    fl.create_table(catalog, fq, fl.source_sql(_parquet(tmp_path / "a.parquet", 1)))
    fl.replace_month(catalog, fq, _parquet(tmp_path / "a.parquet", 1))
    with pytest.raises(fl.SchemaChanged) as e:
        fl.replace_month(catalog, fq, _parquet(tmp_path / "b.parquet", 3, cost="'abc'"))
    assert e.value.diff["added"] == [] and e.value.diff["type_changed"] == ["billedcost VARCHAR -> DOUBLE"]
    assert "abc" in e.value.diff["cannot_convert"]
    assert catalog.sql(f"SELECT count(*) FROM {fq}").fetchone()[0] == 1  # the month already there is untouched


def test_remove_old_months_deletes_only_months_before_the_window(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "PARTITION_CLAUSE", "")
    fq = 's3t."focus"."focus"'
    fl.create_table(catalog, fq, fl.source_sql(_parquet(tmp_path / "a.parquet", 1)))
    for i, month in enumerate(("2026-06", "2026-07", "2026-08", "2026-11")):
        fl.replace_month(catalog, fq, _parquet(tmp_path / f"m{i}.parquet", 2, month=f"{month}-01 00:00:00+00"))
    assert fl.remove_old_months(catalog, fq, "2026-08") == ["2026-06", "2026-07"]
    left = catalog.sql(f"SELECT DISTINCT strftime(billingperiodstart, '%Y-%m') FROM {fq} ORDER BY 1").fetchall()
    assert [r[0] for r in left] == ["2026-08", "2026-11"]
    assert fl.remove_old_months(catalog, fq, "2026-08") == []  # nothing older: no delete


def test_empty_delivery_changes_nothing_and_is_not_a_schema_change(catalog, tmp_path, monkeypatch):
    """0 rows = no data for that month: skipped, the rows already loaded stay, even if its columns differ."""
    monkeypatch.setattr(fl, "PARTITION_CLAUSE", "")
    fq = 's3t."focus"."focus"'
    fl.create_table(catalog, fq, fl.source_sql(_parquet(tmp_path / "a.parquet", 3)))
    fl.replace_month(catalog, fq, _parquet(tmp_path / "a.parquet", 3))
    assert fl.replace_month(catalog, fq, _parquet(tmp_path / "b.parquet", 0)) == (0, [])
    assert fl.replace_month(catalog, fq, _parquet(tmp_path / "c.parquet", 0, extra_col=True)) == (0, [])
    assert catalog.sql(f"SELECT count(*) FROM {fq}").fetchone()[0] == 3


def test_rows_without_a_month_column_are_an_error_not_a_schema_change(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "PARTITION_CLAUSE", "")
    fq = 's3t."focus"."focus"'
    fl.create_table(catalog, fq, fl.source_sql(_parquet(tmp_path / "a.parquet", 1)))
    with pytest.raises(fl.MonthUnknown, match="BillingPeriodStart"):
        fl.replace_month(catalog, fq, _parquet(tmp_path / "b.parquet", 2, drop_month=True), "2026-10")


def test_failed_commit_raises_the_commit_error_not_the_rollback_error():
    class Conn:
        """Stands in for DuckDB when COMMIT fails and the transaction is already gone."""
        def __init__(self):
            self.calls = []

        def sql(self, q):
            self.calls.append(q.split()[0])
            if q == "COMMIT":
                raise RuntimeError("commit failed: Iceberg catalog conflict")
            if q == "ROLLBACK":
                raise RuntimeError("no transaction is active")
            if q.startswith("DESCRIBE"):
                return types.SimpleNamespace(fetchall=lambda: [("billingperiodstart", "TIMESTAMP WITH TIME ZONE")])
            return types.SimpleNamespace(fetchone=lambda: (1,) if "count(*)" in q else ("2026-10-01 00:00:00+00",))

    c = Conn()
    with pytest.raises(RuntimeError, match="commit failed"):
        fl.replace_month(c, "T", "s3://b/p.parquet")
    assert c.calls[-1] == "ROLLBACK"


# ---------- handler with fake AWS clients ----------

class _ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self, deliveries, export_prefix="focus/acme/data/"):
        self.deliveries = deliveries  # {period: [folder, ...]}
        self.prefix = export_prefix
        self.objects = {}
        self.etag_n = 0
        self.puts = 0

    def get_paginator(self, name):
        s3 = self

        class P:
            def paginate(self, Bucket, Prefix, Delimiter=None):
                if Delimiter:
                    return [{"CommonPrefixes": [{"Prefix": f"{s3.prefix}billing_period={p}/"} for p in s3.deliveries]}]
                period = Prefix[len(s3.prefix):].split("=")[1].rstrip("/")
                return [{"Contents": [{"Key": f"{Prefix}{f}/part-0.parquet"} for f in s3.deliveries.get(period, [])]}]

        return P()

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise _ClientError("NoSuchKey")
        body, etag = self.objects[(Bucket, Key)]
        return {"Body": types.SimpleNamespace(read=lambda: body), "ETag": etag}

    def put_object(self, Bucket, Key, Body, ContentType=None, IfMatch=None, IfNoneMatch=None):
        cur = self.objects.get((Bucket, Key))
        if IfNoneMatch == "*" and cur is not None:
            raise _ClientError("PreconditionFailed")
        if IfMatch is not None and (cur is None or cur[1] != IfMatch):
            raise _ClientError("PreconditionFailed")
        self.etag_n += 1
        self.puts += 1
        self.objects[(Bucket, Key)] = (Body, f'"e{self.etag_n}"')
        return {"ETag": f'"e{self.etag_n}"'}


class FakeTables:
    def __init__(self, exists=False):
        self.namespaces, self.exists, self.created_ns = set(), exists, []

    def get_namespace(self, tableBucketARN, namespace):
        if namespace not in self.namespaces:
            raise _ClientError("NotFoundException")

    def create_namespace(self, tableBucketARN, namespace):
        self.namespaces.add(namespace[0])
        self.created_ns.append(namespace[0])

    def get_table(self, tableBucketARN, namespace, name):
        if not self.exists:
            raise _ClientError("NotFoundException")


class Ctx:
    def __init__(self, ms=900_000):
        self.ms = ms

    def get_remaining_time_in_millis(self):
        return self.ms


@pytest.fixture
def wired(tmp_path, monkeypatch, catalog):
    """Handler wired to fakes: deliveries are local Parquet files, the catalog is local DuckDB."""
    monkeypatch.setattr(fl, "PARTITION_CLAUSE", "")
    monkeypatch.setattr(fl, "ClientError", _ClientError)
    files = {}

    def make(period, folder, rows, **kw):
        p = tmp_path / f"{period}-{folder}.parquet".replace(":", "_")
        files[(period, folder)] = _parquet(p, rows, month=f"{period}-01 00:00:00+00", **kw)

    s3 = FakeS3({})
    tables = FakeTables()
    monkeypatch.setattr(fl, "_client", lambda name: {"s3": s3, "s3tables": tables}[name])
    monkeypatch.setattr(fl, "delivery_source", lambda bucket, prefix, period, folder: files[(period, folder)])

    def fake_connect(ev):
        catalog.sql("CREATE SCHEMA IF NOT EXISTS s3t.focus")
        return catalog

    monkeypatch.setattr(fl, "connect", fake_connect)

    def table_created(*a, **k):
        tables.exists = True

    orig_create = fl.create_table
    monkeypatch.setattr(fl, "create_table", lambda c, fq, src: (orig_create(c, fq, src), table_created()))
    return types.SimpleNamespace(s3=s3, tables=tables, make=make, catalog=catalog)


def _state(w):
    body, _ = w.s3.objects[("acme-focus-state", "pico-finops-agent/state.json")]
    return json.loads(body)


def test_first_run_creates_table_and_backfills(wired, monkeypatch):
    monkeypatch.setattr(fl, "_now", lambda: NOW)
    for period, rows in (("2026-09", 2), ("2026-10", 3), ("2026-11", 4)):
        wired.make(period, "2026-11-02T15:07:11.328Z-aaaa", rows)
        wired.s3.deliveries[period] = ["2026-11-02T15:07:11.328Z-aaaa"]
    out = fl.lambda_handler(_event(), Ctx())
    assert out["status"] == "ok", out
    assert out["table_created"] is True
    assert [m["period"] for m in out["months_loaded"]] == ["2026-09", "2026-10", "2026-11"]
    assert wired.tables.created_ns == ["focus"]
    assert wired.catalog.sql('SELECT count(*) FROM s3t."focus"."focus"').fetchone()[0] == 9
    st = _state(wired)
    assert st["backfill_done"] is True and set(st["loaded"]) == {"2026-09", "2026-10", "2026-11"}
    assert wired.s3.puts == 4  # state.json after each month, plus the final record

    again = fl.lambda_handler(_event(), Ctx())
    assert again["status"] == "ok" and again["months_loaded"] == [] and again["new_deliveries"] == []


def test_time_check_returns_partial_and_keeps_committed_months(wired, monkeypatch):
    monkeypatch.setattr(fl, "_now", lambda: NOW)
    for period in ("2026-09", "2026-10"):
        wired.make(period, "f1", 1)
        wired.s3.deliveries[period] = ["f1"]
    calls = iter([900_000, 60_000])  # checked before each month

    class C:
        def get_remaining_time_in_millis(self):
            return next(calls, 60_000)

    out = fl.lambda_handler(_event(), C())
    assert out["status"] == "partial", out
    assert [m["period"] for m in out["months_loaded"]] == ["2026-09"]
    assert out["backfill_pending"] == ["2026-10"]
    assert _state(wired)["backfill_done"] is False


def test_schema_changed_stops_and_reports_diff(wired, monkeypatch):
    """A column that appears after the table exists stops the load (the table keeps its schema)."""
    monkeypatch.setattr(fl, "_now", lambda: NOW)
    wired.make("2026-11", "f1", 2)
    wired.s3.deliveries["2026-11"] = ["f1"]
    assert fl.lambda_handler(_event(), Ctx())["status"] == "ok"
    wired.make("2026-11", "f2", 3, extra_col=True)
    wired.s3.deliveries["2026-11"] = ["f1", "f2"]
    out = fl.lambda_handler(_event(), Ctx())
    assert out["status"] == "schema_changed"
    assert out["schema_diff"] == {"added": ["newcol INTEGER"], "type_changed": []}
    assert out["months_loaded"] == []
    assert _state(wired)["loaded"]["2026-11"].endswith("f1.parquet")
    assert "newcol" in _state(wired)["last_error"]
    assert wired.catalog.sql('SELECT count(*) FROM s3t."focus"."focus"').fetchone()[0] == 2  # untouched


def test_changed_type_loads_and_reports_the_cast(wired, monkeypatch):
    monkeypatch.setattr(fl, "_now", lambda: NOW)
    wired.make("2026-11", "f1", 2)
    wired.s3.deliveries["2026-11"] = ["f1"]
    assert fl.lambda_handler(_event(), Ctx())["status"] == "ok"
    wired.make("2026-11", "f2", 3, cost="CAST(range AS VARCHAR)")
    wired.s3.deliveries["2026-11"] = ["f1", "f2"]
    out = fl.lambda_handler(_event(), Ctx())
    assert out["status"] == "ok" and out["schema_diff"] is None
    assert out["months_loaded"][0]["rows"] == 3
    assert out["months_loaded"][0]["cast"] == ["billedcost VARCHAR -> DOUBLE"]


def test_months_older_than_the_limit_are_removed_from_the_table_and_state(wired, monkeypatch):
    """Keep only the last N months: a run with months=2 in November drops everything before October."""
    monkeypatch.setattr(fl, "_now", lambda: NOW)
    for period in ("2026-09", "2026-10", "2026-11"):
        wired.make(period, "f1", 2)
        wired.s3.deliveries[period] = ["f1"]
    assert fl.lambda_handler(_event(), Ctx())["status"] == "ok"
    out = fl.lambda_handler(_event(months=2), Ctx())
    assert out["status"] == "ok" and out["months_removed"] == ["2026-09"]
    assert sorted(_state(wired)["loaded"]) == ["2026-10", "2026-11"]
    left = wired.catalog.sql('SELECT DISTINCT strftime(billingperiodstart, \'%Y-%m\') FROM s3t."focus"."focus" ORDER BY 1')
    assert [r[0] for r in left.fetchall()] == ["2026-10", "2026-11"]
    assert fl.lambda_handler(_event(months=2), Ctx())["months_removed"] == []


def test_empty_delivery_is_skipped_as_no_new_data(wired, monkeypatch):
    """A new delivery with 0 rows: status ok, not announced as a loaded month, the month's rows stay, and it is
    remembered so the next run doesn't look at it again."""
    monkeypatch.setattr(fl, "_now", lambda: NOW)
    wired.make("2026-11", "f1", 2)
    wired.s3.deliveries["2026-11"] = ["f1"]
    assert fl.lambda_handler(_event(), Ctx())["status"] == "ok"
    wired.make("2026-11", "f2", 0)
    wired.s3.deliveries["2026-11"] = ["f1", "f2"]
    out = fl.lambda_handler(_event(), Ctx())
    assert out["status"] == "ok" and out["schema_diff"] is None and out["error"] is None
    assert out["months_loaded"] == [] and out["empty_deliveries"] == ["2026-11"]
    assert _state(wired)["loaded"]["2026-11"].endswith("f2.parquet")
    assert wired.catalog.sql('SELECT count(*) FROM s3t."focus"."focus"').fetchone()[0] == 2
    assert fl.lambda_handler(_event(), Ctx())["new_deliveries"] == []


def test_table_is_created_from_the_newest_schema(wired, monkeypatch):
    """At creation the newest delivery sets the columns; an older month without one gets NULL."""
    monkeypatch.setattr(fl, "_now", lambda: NOW)
    wired.make("2026-10", "f1", 1)
    wired.make("2026-11", "f1", 2, extra_col=True)
    wired.s3.deliveries.update({"2026-10": ["f1"], "2026-11": ["f1"]})
    out = fl.lambda_handler(_event(), Ctx())
    assert out["status"] == "ok" and len(out["months_loaded"]) == 2
    assert wired.catalog.sql('SELECT count(*) FROM s3t."focus"."focus" WHERE newcol IS NULL').fetchone()[0] == 1


def test_deleted_table_is_recreated_and_backfilled(wired, monkeypatch):
    monkeypatch.setattr(fl, "_now", lambda: NOW)
    wired.make("2026-11", "f1", 2)
    wired.s3.deliveries["2026-11"] = ["f1"]
    wired.s3.objects[("acme-focus-state", "pico-finops-agent/state.json")] = (
        json.dumps({**fl.empty_state(), "backfill_done": True, "loaded": {"2026-11": "x"}}).encode(), '"e0"')
    out = fl.lambda_handler(_event(), Ctx())
    assert out["status"] == "ok" and out["table_created"] is True
    assert [m["period"] for m in out["months_loaded"]] == ["2026-11"]


def test_empty_export_reports_no_table(wired, monkeypatch):
    """Nothing delivered yet and no table: ok, nothing created, and the result says the table is missing."""
    monkeypatch.setattr(fl, "_now", lambda: NOW)
    out = fl.lambda_handler(_event(), Ctx())
    assert out["status"] == "ok" and out["table_created"] is False and out["table_exists"] is False


def test_result_says_table_exists_after_a_load(wired, monkeypatch):
    monkeypatch.setattr(fl, "_now", lambda: NOW)
    wired.make("2026-11", "f1", 1)
    wired.s3.deliveries["2026-11"] = ["f1"]
    assert fl.lambda_handler(_event(), Ctx())["table_exists"] is True


def test_state_conflict(wired, monkeypatch):
    monkeypatch.setattr(fl, "_now", lambda: NOW)
    wired.make("2026-11", "f1", 1)
    wired.s3.deliveries["2026-11"] = ["f1"]
    orig = wired.s3.put_object

    def racing_put(**kw):
        wired.s3.objects[("acme-focus-state", "pico-finops-agent/state.json")] = (b"{}", '"other"')
        return orig(**kw)

    wired.s3.put_object = racing_put
    out = fl.lambda_handler(_event(), Ctx())
    assert out["status"] == "conflict"


def test_no_deliveries_is_ok_noop(wired, monkeypatch):
    monkeypatch.setattr(fl, "_now", lambda: NOW)
    wired.tables.exists = True
    wired.tables.namespaces.add("focus")
    wired.catalog.sql('CREATE TABLE s3t."focus"."focus" (billingperiodstart TIMESTAMPTZ)')  # exists, no rows
    out = fl.lambda_handler(_event(), Ctx())
    assert out["status"] == "ok" and out["months_loaded"] == [] and out["months_removed"] == []


def test_state_bucket_missing_is_an_error_result_not_a_crash(wired, monkeypatch):
    monkeypatch.setattr(fl, "_now", lambda: NOW)

    def missing(**kw):
        raise _ClientError("NoSuchBucket")

    wired.s3.get_object = missing
    out = fl.lambda_handler(_event(), Ctx())
    assert out["status"] == "error" and "NoSuchBucket" in out["error"]


def test_final_state_write_failure_keeps_the_loaded_months(wired, monkeypatch):
    monkeypatch.setattr(fl, "_now", lambda: NOW)
    wired.make("2026-11", "f1", 2)
    wired.s3.deliveries["2026-11"] = ["f1"]
    orig, calls = wired.s3.put_object, []

    def flaky_put(**kw):
        calls.append(1)
        if len(calls) > 1:  # the per-month write works, the final one is denied
            raise _ClientError("AccessDenied")
        return orig(**kw)

    wired.s3.put_object = flaky_put
    out = fl.lambda_handler(_event(), Ctx())
    assert out["status"] == "error" and "AccessDenied" in out["error"]
    assert [m["period"] for m in out["months_loaded"]] == ["2026-11"]
