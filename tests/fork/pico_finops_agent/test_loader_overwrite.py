"""The loader reads overwrite-mode FOCUS exports as well as create-new ones, the mode worked out per month from S3.

Create new: billing_period=X/<delivery>/*.parquet; the newest delivery folder is loaded and the URL is the "has it
changed" token, exactly as before (old state.json files stay valid). Overwrite: billing_period=X/*.parquet straight
in the month folder; the files listed in the month's manifest (<export root>/metadata/billing_period=X/
<export>-Manifest.json, `dataFiles`) are loaded and the token is overwrite:<executionId>; without a usable manifest,
every parquet in the folder and an ETag token. Both layouts (the export changed mode): the newer set wins. Seen in
a test account: focus2 (overwrite, 13 months) loaded nothing, with no error."""
import io
import json
import types
from datetime import datetime, timedelta, timezone

import pytest

from .test_loader_app import NOW, FakeTables, _ClientError, _parquet, duckdb, fl

B = "acme-focus-export"
PREFIX = "focus2/focus2/data/"
META = "focus2/focus2/metadata/"
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


class FakeS3X:
    """list_objects_v2 (with and without Delimiter, LastModified, ETag), get_object, conditional put_object."""

    def __init__(self):
        self.objects = {}  # (bucket, key) -> {"Body", "ETag", "LastModified"}
        self.n = 0

    def put(self, key, body=b"x", at=T0, bucket=B, etag=None):
        self.n += 1
        self.objects[(bucket, key)] = {"Body": body, "ETag": etag or f'"e{self.n}"', "LastModified": at}

    def get_paginator(self, name):
        s3 = self

        class P:
            def paginate(self, Bucket, Prefix, Delimiter=None):
                keys = sorted(k for b, k in s3.objects if b == Bucket and k.startswith(Prefix))
                if not Delimiter:
                    return [{"Contents": [{"Key": k, "ETag": s3.objects[(Bucket, k)]["ETag"],
                                           "LastModified": s3.objects[(Bucket, k)]["LastModified"]} for k in keys]}]
                prefixes = sorted({Prefix + k[len(Prefix):].split("/")[0] + "/" for k in keys
                                   if "/" in k[len(Prefix):]})
                direct = [{"Key": k, "ETag": s3.objects[(Bucket, k)]["ETag"],
                           "LastModified": s3.objects[(Bucket, k)]["LastModified"]}
                          for k in keys if "/" not in k[len(Prefix):]]
                return [{"CommonPrefixes": [{"Prefix": p} for p in prefixes], "Contents": direct}]

        return P()

    def get_object(self, Bucket, Key):
        o = self.objects.get((Bucket, Key))
        if o is None:
            raise _ClientError("NoSuchKey")
        return {"Body": io.BytesIO(o["Body"]), "ETag": o["ETag"]}

    def put_object(self, Bucket, Key, Body, ContentType=None, IfMatch=None, IfNoneMatch=None):
        cur = self.objects.get((Bucket, Key))
        if IfNoneMatch == "*" and cur is not None:
            raise _ClientError("PreconditionFailed")
        if IfMatch is not None and (cur is None or cur["ETag"] != IfMatch):
            raise _ClientError("PreconditionFailed")
        self.put(Key, Body, bucket=Bucket)
        return {"ETag": self.objects[(Bucket, Key)]["ETag"]}


def _manifest(s3, period, execution, files, export="focus2", at=T0):
    s3.put(f"{META}billing_period={period}/{export}-Manifest.json",
           json.dumps({"executionId": execution, "dataFiles": files, "columns": []}).encode(), at=at)
    s3.put(f"{META}billing_period={period}/{export}-Manifest-FOCUS.json", b'{"Schema": {}}', at=at)  # no dataFiles


def _overwrite_month(s3, period, execution="exec-1", at=T0, files=1, manifest=True):
    keys = [f"{PREFIX}billing_period={period}/focus2-{i:05d}.snappy.parquet" for i in range(1, files + 1)]
    for k in keys:
        s3.put(k, at=at)
    if manifest:
        _manifest(s3, period, execution, [f"s3://{B}/{k}" for k in keys], at=at)
    return keys


# ---------- detection ----------

def test_create_new_month_is_the_newest_folder_with_the_url_token_as_before():
    s3 = FakeS3X()
    base = f"{PREFIX}billing_period=2026-10/"
    s3.put(base + "2026-11-01T23:09:48.098Z-c079028e/part-0.parquet")
    s3.put(base + "2026-11-02T15:07:11.328Z-e2891bd6/part-0.parquet")
    d = fl.find_delivery(s3, B, PREFIX, "2026-10")
    url = fl.delivery_source(B, PREFIX, "2026-10", "2026-11-02T15:07:11.328Z-e2891bd6")
    assert d.mode == "create_new" and d.token == url and d.files == [url] and not d.mixed
    assert d.ident == "2026-11-02T15:07:11.328Z-e2891bd6"


def test_overwrite_month_loads_the_manifest_files_with_an_execution_token():
    s3 = FakeS3X()
    keys = _overwrite_month(s3, "2026-11", execution="f18b057b-998e", files=2)
    d = fl.find_delivery(s3, B, PREFIX, "2026-11")
    assert d.mode == "overwrite" and d.token == "overwrite:f18b057b-998e"
    assert d.files == [f"s3://{B}/{k}" for k in keys] and d.ident == "f18b057b-998e"


def test_manifest_files_outside_the_month_folder_are_not_trusted():
    s3 = FakeS3X()
    _overwrite_month(s3, "2026-11", manifest=False)
    _manifest(s3, "2026-11", "exec-x", [f"s3://other-bucket/{PREFIX}billing_period=2026-11/focus2-00001.snappy.parquet"])
    d = fl.find_delivery(s3, B, PREFIX, "2026-11")
    assert d.token.startswith("overwrite:etag:")                 # fell back to the files really in the folder
    assert d.files == [f"s3://{B}/{PREFIX}billing_period=2026-11/focus2-00001.snappy.parquet"]


def test_overwrite_without_a_manifest_uses_every_parquet_and_an_etag_token():
    s3 = FakeS3X()
    keys = _overwrite_month(s3, "2026-11", files=2, manifest=False)
    first = fl.find_delivery(s3, B, PREFIX, "2026-11")
    assert first.mode == "overwrite" and first.token.startswith("overwrite:etag:") and len(first.token) == 15 + 64
    assert fl.find_delivery(s3, B, PREFIX, "2026-11").token == first.token  # same files: same token
    s3.put(keys[0], at=T0 + timedelta(hours=6))                             # AWS rewrote a file: new ETag
    assert fl.find_delivery(s3, B, PREFIX, "2026-11").token != first.token


def test_a_prefix_without_data_has_no_manifest_and_uses_etags():
    s3 = FakeS3X()
    prefix = "focus2/flat/"
    s3.put(f"{prefix}billing_period=2026-11/focus2-00001.snappy.parquet")
    d = fl.find_delivery(s3, B, prefix, "2026-11")
    assert d.mode == "overwrite" and d.token.startswith("overwrite:etag:")


@pytest.mark.parametrize("newer", ["create_new", "overwrite"])
def test_both_layouts_the_newer_set_wins(newer):
    s3 = FakeS3X()
    old, new = T0, T0 + timedelta(days=1)
    s3.put(f"{PREFIX}billing_period=2026-11/2026-11-01T00:00:00.000Z-aaaa/part-0.parquet",
           at=new if newer == "create_new" else old)
    _overwrite_month(s3, "2026-11", at=old if newer == "create_new" else new)
    d = fl.find_delivery(s3, B, PREFIX, "2026-11")
    assert d.mixed and d.mode == newer


def test_unsafe_file_names_are_ignored():
    s3 = FakeS3X()
    s3.put(f"{PREFIX}billing_period=2026-11/x']); DROP TABLE t; --.parquet")
    assert fl.find_delivery(s3, B, PREFIX, "2026-11") is None


def test_an_empty_month_has_no_delivery():
    s3 = FakeS3X()
    s3.put(f"{PREFIX}billing_period=2026-11/readme.txt")
    assert fl.find_delivery(s3, B, PREFIX, "2026-11") is None


def test_the_upper_case_billing_period_folder_is_ignored():
    s3 = FakeS3X()
    s3.put(f"{PREFIX}BILLING_PERIOD=2026-05/focus2-00001.snappy.parquet")
    _overwrite_month(s3, "2026-11")
    assert fl.list_periods(s3, B, PREFIX) == ["2026-11"]


def test_select_periods_compares_tokens():
    s3 = FakeS3X()
    _overwrite_month(s3, "2026-11", execution="e1")
    avail = {"2026-11": fl.find_delivery(s3, B, PREFIX, "2026-11")}
    state = {**fl.empty_state(), "backfill_done": True, "loaded": {"2026-11": "overwrite:e1"}}
    assert fl.select_periods(state, avail, NOW, force=False, periods=None) == {}
    state["loaded"]["2026-11"] = "overwrite:e0"
    assert list(fl.select_periods(state, avail, NOW, force=False, periods=None)) == ["2026-11"]


# ---------- the handler ----------

@pytest.fixture
def catalog(tmp_path):
    c = duckdb.connect(str(tmp_path / "catalog.duckdb"))
    c.sql("SET TimeZone = 'UTC'")
    c.sql("ATTACH ':memory:' AS s3t")
    c.sql("CREATE SCHEMA s3t.focus")
    return c


@pytest.fixture
def ow(tmp_path, monkeypatch, catalog):
    """The handler on an overwrite export: parquet files are local, the catalog is local DuckDB."""
    monkeypatch.setattr(fl, "PARTITION_CLAUSE", "")
    monkeypatch.setattr(fl, "ClientError", _ClientError)
    monkeypatch.setattr(fl, "_now", lambda: NOW)
    s3, tables, local = FakeS3X(), FakeTables(), {}
    monkeypatch.setattr(fl, "_client", lambda name: {"s3": s3, "s3tables": tables}[name])

    def make(period, rows, execution="exec-1"):
        keys = _overwrite_month(s3, period, execution=execution)
        local[f"s3://{B}/{keys[0]}"] = _parquet(tmp_path / f"{period}-{execution}.parquet", rows,
                                                  month=f"{period}-01 00:00:00+00")

    real_source = fl.source_sql
    monkeypatch.setattr(fl, "source_sql", lambda files: real_source([local.get(f, f) for f in
                                                                     ([files] if isinstance(files, str) else files)]))

    def fake_connect(ev):
        catalog.sql("CREATE SCHEMA IF NOT EXISTS s3t.focus")
        return catalog

    monkeypatch.setattr(fl, "connect", fake_connect)
    orig_create = fl.create_table
    monkeypatch.setattr(fl, "create_table", lambda c, fq, src: (orig_create(c, fq, src),
                                                                 setattr(tables, "exists", True)))
    return types.SimpleNamespace(s3=s3, make=make, catalog=catalog, local=local)


def _event():
    return {"force": False, "periods": None,
            "source": {"export_bucket": B, "export_prefix": PREFIX},
            "target": {"table_bucket_arn": "arn:aws:s3tables:us-east-1:111122223333:bucket/acme-focus",
                       "namespace": "focus", "table": "focus"},
            "state": {"bucket": "acme-focus-state", "key": "pico-finops-agent/state.json"}}


def _state(w):
    return json.loads(w.s3.objects[("acme-focus-state", "pico-finops-agent/state.json")]["Body"])


def test_an_overwrite_export_loads_and_a_second_run_loads_nothing(ow):
    for period, rows in (("2026-09", 2), ("2026-10", 3), ("2026-11", 4)):
        ow.make(period, rows)
    out = fl.lambda_handler(_event(), None)
    assert out["status"] == "ok" and [m["period"] for m in out["months_loaded"]] == ["2026-09", "2026-10", "2026-11"]
    assert {m["mode"] for m in out["months_loaded"]} == {"overwrite"}
    assert ow.catalog.sql('SELECT count(*) FROM s3t."focus"."focus"').fetchone()[0] == 9
    st = _state(ow)
    assert st["loaded"]["2026-11"] == "overwrite:exec-1" and st["export_modes"]["2026-11"] == "overwrite"
    again = fl.lambda_handler(_event(), None)
    assert again["status"] == "ok" and again["months_loaded"] == [] and again["new_deliveries"] == []


def test_a_new_execution_reloads_the_month(ow):
    ow.make("2026-11", 4, execution="exec-1")
    fl.lambda_handler(_event(), None)
    ow.make("2026-11", 6, execution="exec-2")                     # AWS rewrote the month
    out = fl.lambda_handler(_event(), None)
    assert [m["period"] for m in out["months_loaded"]] == ["2026-11"] and out["months_loaded"][0]["rows"] == 6
    assert _state(ow)["loaded"]["2026-11"] == "overwrite:exec-2"


def test_a_month_rewritten_during_the_load_is_loaded_again_next_run(ow, monkeypatch):
    ow.make("2026-11", 4, execution="exec-1")
    real = fl.replace_month

    def rewritten_meanwhile(c, fq, files, period=None):
        out = real(c, fq, files, period)
        _manifest(ow.s3, "2026-11", "exec-2", [f"s3://{B}/{PREFIX}billing_period=2026-11/focus2-00001.snappy.parquet"])
        return out

    monkeypatch.setattr(fl, "replace_month", rewritten_meanwhile)
    out = fl.lambda_handler(_event(), None)
    assert [m["period"] for m in out["months_loaded"]] == ["2026-11"]
    assert "2026-11" not in _state(ow)["loaded"]                   # not marked loaded: the next run loads it again
    monkeypatch.setattr(fl, "replace_month", real)
    again = fl.lambda_handler(_event(), None)
    assert [m["period"] for m in again["months_loaded"]] == ["2026-11"]
    assert _state(ow)["loaded"]["2026-11"] == "overwrite:exec-2"


def test_an_old_state_with_create_new_urls_does_not_reload(ow):
    """state.json written before this change holds the create-new URL; the same delivery must not load again."""
    base = f"{PREFIX}billing_period=2026-11/"
    folder = "2026-11-02T15:07:11.328Z-e2891bd6"
    ow.s3.put(base + folder + "/part-0.parquet")
    url = fl.delivery_source(B, PREFIX, "2026-11", folder)
    old = {**fl.empty_state(), "backfill_done": True, "loaded": {"2026-11": url}}
    ow.s3.put("pico-finops-agent/state.json", json.dumps(old).encode(), bucket="acme-focus-state")
    ow.catalog.sql('CREATE TABLE s3t."focus"."focus" (billingperiodstart TIMESTAMPTZ, billedcost DOUBLE)')
    fl._client("s3tables").exists = True
    out = fl.lambda_handler(_event(), None)
    assert out["status"] == "ok" and out["months_loaded"] == [] and out["new_deliveries"] == []


# ---------- the loader role may read the manifests (pico_finops_agent/iam/loader-role-policy.json) ----------

def test_loader_role_policy_lets_the_lambda_list_and_read_the_manifests():
    from pathlib import Path

    doc = json.loads((Path(__file__).resolve().parents[3] / "pico_finops_agent" / "iam" / "loader-role-policy.json")
                     .read_text(encoding="utf-8"))
    by_sid = {s["Sid"]: s for s in doc["Statement"]}
    prefixes = by_sid["ListExportPrefix"]["Condition"]["StringLike"]["s3:prefix"]
    assert "<export-prefix>*" in prefixes and "<export-root>metadata/*" in prefixes
    assert all(p.endswith("*") for p in prefixes)                      # a prefix without * lists nothing below it
    reads = by_sid["ReadExportFiles"]["Resource"]
    reads = [reads] if isinstance(reads, str) else reads
    assert sorted(reads) == ["arn:aws:s3:::<export-bucket>/<export-prefix>*",
                             "arn:aws:s3:::<export-bucket>/<export-root>metadata/*"]
    assert by_sid["ReadExportFiles"]["Action"] == "s3:GetObject"     # read only, nothing else in the export


# ---------- review (2026-11-11): manifest errors, duplicates, re-check, export_modes ----------

class Raising(FakeS3X):
    def __init__(self, code):
        super().__init__()
        self.code = code

    def get_object(self, Bucket, Key):
        if Key.endswith("-Manifest.json"):
            raise _ClientError(self.code)
        return super().get_object(Bucket, Key)


@pytest.fixture(autouse=True)
def _client_error(monkeypatch):
    monkeypatch.setattr(fl, "ClientError", _ClientError)


def test_no_access_to_the_manifest_falls_back_to_etags():
    s3 = Raising("AccessDenied")
    _overwrite_month(s3, "2026-11")
    assert fl.find_delivery(s3, B, PREFIX, "2026-11").token.startswith("overwrite:etag:")


@pytest.mark.parametrize("code", ["SlowDown", "InternalError", "ServiceUnavailable"])
def test_a_passing_s3_error_on_the_manifest_fails_the_run_instead_of_flipping_the_token(code):
    s3 = Raising(code)
    _overwrite_month(s3, "2026-11")
    with pytest.raises(_ClientError):
        fl.find_delivery(s3, B, PREFIX, "2026-11")


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'{"executionId": "e1"}', b'{"dataFiles": ["x"]}'])
def test_an_unusable_manifest_falls_back_to_etags(body):
    s3 = FakeS3X()
    _overwrite_month(s3, "2026-11", manifest=False)
    s3.put(f"{META}billing_period=2026-11/focus2-Manifest.json", body)
    assert fl.find_delivery(s3, B, PREFIX, "2026-11").token.startswith("overwrite:etag:")


def test_repeated_manifest_files_are_read_once():
    s3 = FakeS3X()
    keys = _overwrite_month(s3, "2026-11", manifest=False)
    url = f"s3://{B}/{keys[0]}"
    _manifest(s3, "2026-11", "e1", [url, url])
    assert fl.find_delivery(s3, B, PREFIX, "2026-11").files == [url]


def test_a_manifest_naming_another_months_file_is_not_trusted():
    s3 = FakeS3X()
    _overwrite_month(s3, "2026-10")
    _overwrite_month(s3, "2026-11", manifest=False)
    _manifest(s3, "2026-11", "e1", [f"s3://{B}/{PREFIX}billing_period=2026-10/focus2-00001.snappy.parquet"])
    assert fl.find_delivery(s3, B, PREFIX, "2026-11").token.startswith("overwrite:etag:")


def test_a_failing_re_check_does_not_fail_the_run(ow, monkeypatch):
    ow.make("2026-11", 4)
    real, calls = fl.find_delivery, []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) > 1:  # the re-check after the load
            raise _ClientError("InternalError")
        return real(*a, **k)

    monkeypatch.setattr(fl, "find_delivery", flaky)
    out = fl.lambda_handler(_event(), None)
    assert out["status"] == "ok" and [m["period"] for m in out["months_loaded"]] == ["2026-11"]
    assert "2026-11" not in _state(ow)["loaded"]          # not sure it's current: loaded again next run


def test_export_modes_are_saved_with_the_token_only(ow, monkeypatch):
    ow.make("2026-11", 4, execution="exec-1")
    real = fl.replace_month

    def rewritten_meanwhile(c, fq, files, period=None):
        out = real(c, fq, files, period)
        _manifest(ow.s3, "2026-11", "exec-2", [f"s3://{B}/{PREFIX}billing_period=2026-11/focus2-00001.snappy.parquet"])
        return out

    monkeypatch.setattr(fl, "replace_month", rewritten_meanwhile)
    fl.lambda_handler(_event(), None)
    assert "2026-11" not in _state(ow).get("export_modes", {})


def test_a_recreated_table_clears_export_modes(ow):
    ow.make("2026-11", 4)
    old = {**fl.empty_state(), "backfill_done": True, "loaded": {"2026-09": "s3://old"},
           "export_modes": {"2026-09": "create_new"}}
    ow.s3.put("pico-finops-agent/state.json", json.dumps(old).encode(), bucket="acme-focus-state")
    fl.lambda_handler(_event(), None)                       # the table doesn't exist: start over
    assert _state(ow)["export_modes"] == {"2026-11": "overwrite"}


def test_skipped_unsafe_parquet_names_are_logged(capsys):
    s3 = FakeS3X()
    _overwrite_month(s3, "2026-11")
    s3.put(f"{PREFIX}billing_period=2026-11/focus2 copy.parquet")
    fl.find_delivery(s3, B, PREFIX, "2026-11")
    assert '"event": "files_skipped"' in capsys.readouterr().out
