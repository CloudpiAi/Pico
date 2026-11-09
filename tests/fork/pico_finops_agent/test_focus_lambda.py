"""focus-data plugin v2 (Phase 3): ensure the Lambda, build the payload, invoke, handle the result,
refresh the cache, report status. Fake AWS clients; no network."""
import io
import json
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("boto3", reason="Needs the pico-finops-agent extra: uv sync --extra pico-finops-agent")

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "focus"))
from pico_plugin_focus_data import cli, lambda_ops, loader  # noqa: E402

PROFILE = {
    "region": "us-east-1",
    "export_bucket": "acme-focus-export",
    "export_prefix": "focus/acme-focus/data/",
    "table_bucket_arn": "arn:aws:s3tables:us-east-1:111122223333:bucket/acme-focus",
    "namespace": "focus",
    "table": "focus",
    "state_bucket": "acme-focus-state",
    "state_key": "pico-finops-agent/state.json",
    "lambda_role_arn": "arn:aws:iam::111122223333:role/pico-finops-agent-loader-role",
}


class _ClientError(Exception):
    def __init__(self, code, msg=""):
        super().__init__(f"{code}: {msg}")
        self.response = {"Error": {"Code": code, "Message": msg}}


class FakeLambda:
    def __init__(self, results=()):
        self.fn = None
        self.layers = []
        self.calls = []
        self.results = list(results)
        self.concurrency = None
        self.fail_create_assume = 0

    def _c(self, name, **kw):
        self.calls.append(name)

    def list_layer_versions(self, LayerName):
        self._c("list_layer_versions")
        return {"LayerVersions": list(reversed(self.layers))}

    def publish_layer_version(self, LayerName, Description, Content, CompatibleRuntimes, CompatibleArchitectures):
        self._c("publish_layer_version")
        arn = f"arn:aws:lambda:us-east-1:111122223333:layer:{LayerName}:{len(self.layers) + 1}"
        self.layers.append({"LayerVersionArn": arn, "Description": Description, "Version": len(self.layers) + 1,
                            "_sha": lambda_ops.sha256_b64_bytes(Content["ZipFile"])})
        return {"LayerVersionArn": arn}

    def get_layer_version(self, LayerName, VersionNumber):
        self._c("get_layer_version")
        v = next(v for v in self.layers if v["Version"] == VersionNumber)
        return {"LayerVersionArn": v["LayerVersionArn"], "Content": {"CodeSha256": v["_sha"]}}

    def get_function(self, FunctionName):
        self._c("get_function")
        if self.fn is None:
            raise _ClientError("ResourceNotFoundException")
        return {"Configuration": dict(self.fn)}

    def create_function(self, **kw):
        self._c("create_function")
        if self.fail_create_assume:
            self.fail_create_assume -= 1
            raise _ClientError("InvalidParameterValueException", "The role defined for the function cannot be assumed by Lambda.")
        self.fn = {"CodeSha256": lambda_ops.sha256_b64_bytes(kw["Code"]["ZipFile"]), "Layers": [{"Arn": kw["Layers"][0]}],
                   "MemorySize": kw["MemorySize"], "Timeout": kw["Timeout"], "Handler": kw["Handler"],
                   "Role": kw["Role"], "Runtime": kw["Runtime"], "State": "Active", "Description": kw["Description"]}
        self.created_with = kw

    def update_function_code(self, FunctionName, ZipFile):
        self._c("update_function_code")
        self.fn["CodeSha256"] = lambda_ops.sha256_b64_bytes(ZipFile)

    def update_function_configuration(self, FunctionName, **kw):
        self._c("update_function_configuration")
        if "Layers" in kw:
            self.fn["Layers"] = [{"Arn": a} for a in kw["Layers"]]
        for k in ("MemorySize", "Timeout", "Handler", "Role", "Description"):
            if k in kw:
                self.fn[k] = kw[k]

    def put_function_concurrency(self, FunctionName, ReservedConcurrentExecutions):
        self._c("put_function_concurrency")
        self.concurrency = ReservedConcurrentExecutions

    def get_waiter(self, name):
        self._c(f"wait:{name}")
        return types.SimpleNamespace(wait=lambda **kw: None)

    def invoke(self, FunctionName, InvocationType, Payload, LogType=None):
        self._c("invoke")
        self.last_payload = json.loads(Payload)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        out = {"Payload": io.BytesIO(json.dumps(r).encode())}
        if r.get("_function_error"):
            out["FunctionError"] = "Unhandled"
        return out


@pytest.fixture
def zips(tmp_path):
    d = tmp_path / "zips"
    d.mkdir()
    (d / "duckdb-layer.zip").write_bytes(b"layer-v1")
    (d / "loader.zip").write_bytes(b"code-v1")
    return d


@pytest.fixture
def cfg(tmp_path, zips):
    return loader.Config.from_profile({**PROFILE, "cache_path": str(tmp_path / "data" / "focus.duckdb"),
                                       "zip_dir": str(zips)})


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    monkeypatch.setattr(lambda_ops, "ClientError", _ClientError)
    monkeypatch.setattr(lambda_ops, "_sleep", lambda s: None)


# ---------- ensure_lambda ----------

def test_creates_layer_function_and_concurrency_when_missing(cfg):
    lam = FakeLambda()
    info = lambda_ops.ensure_lambda(cfg, lam)
    assert info["action"] == "created"
    assert lam.created_with["FunctionName"] == "pico-finops-agent-loader"
    assert lam.created_with["Role"] == PROFILE["lambda_role_arn"]
    assert lam.created_with["Handler"] == "pico_finops_agent_loader.lambda_handler"
    assert lam.created_with["Runtime"] == "python3.12" and lam.created_with["Architectures"] == ["x86_64"]
    assert "Environment" not in lam.created_with  # customer details come in the payload
    assert lam.created_with["Description"] == "Pico FinOps Agent: loads the AWS FOCUS export into the S3 Table"
    assert lam.concurrency == 1
    assert lam.layers[0]["Description"] == "sha256:" + lambda_ops.sha256_b64_bytes(b"layer-v1")


def test_same_zips_change_nothing(cfg):
    lam = FakeLambda()
    lambda_ops.ensure_lambda(cfg, lam)
    lam.calls.clear()
    info = lambda_ops.ensure_lambda(cfg, lam)
    assert info["action"] == "unchanged"
    assert not {"publish_layer_version", "create_function", "update_function_code",
                "update_function_configuration"} & set(lam.calls)


def test_changed_code_updates_function(cfg, zips):
    lam = FakeLambda()
    lambda_ops.ensure_lambda(cfg, lam)
    (zips / "loader.zip").write_bytes(b"code-v2")
    info = lambda_ops.ensure_lambda(cfg, lam)
    assert info["action"] == "updated" and "update_function_code" in lam.calls
    assert lam.fn["CodeSha256"] == lambda_ops.sha256_b64_bytes(b"code-v2")


def test_changed_layer_publishes_and_repoints(cfg, zips):
    lam = FakeLambda()
    lambda_ops.ensure_lambda(cfg, lam)
    (zips / "duckdb-layer.zip").write_bytes(b"layer-v2")
    lambda_ops.ensure_lambda(cfg, lam)
    assert len(lam.layers) == 2 and lam.fn["Layers"] == [{"Arn": lam.layers[1]["LayerVersionArn"]}]


def test_layer_with_a_forged_description_is_not_trusted(cfg):
    """Description is settable by anyone who may publish to the layer: the content hash decides."""
    lam = FakeLambda()
    tag = "sha256:" + lambda_ops.sha256_b64_bytes(b"layer-v1")
    lam.layers.append({"LayerVersionArn": "arn:aws:lambda:us-east-1:111122223333:layer:pico-finops-agent-duckdb:1",
                       "Description": tag, "Version": 1, "_sha": lambda_ops.sha256_b64_bytes(b"evil")})
    info = lambda_ops.ensure_lambda(cfg, lam)
    assert info["layer_published"] is True
    assert lam.fn["Layers"] == [{"Arn": lam.layers[-1]["LayerVersionArn"]}]
    assert lam.layers[-1]["Version"] == 2


def test_config_drift_is_corrected(cfg):
    lam = FakeLambda()
    lambda_ops.ensure_lambda(cfg, lam)
    lam.fn["MemorySize"] = 128
    lambda_ops.ensure_lambda(cfg, lam)
    assert lam.fn["MemorySize"] == lambda_ops.MEMORY_MB


def test_old_description_is_updated(cfg):
    lam = FakeLambda()
    lambda_ops.ensure_lambda(cfg, lam)
    lam.fn["Description"] = "FOCUS export -> S3 Table loader (pico focus-data)"
    lambda_ops.ensure_lambda(cfg, lam)
    assert lam.fn["Description"] == lambda_ops.DESCRIPTION


def test_role_propagation_is_retried(cfg):
    lam = FakeLambda()
    lam.fail_create_assume = 2
    assert lambda_ops.ensure_lambda(cfg, lam)["action"] == "created"
    assert lam.calls.count("create_function") == 3


def test_create_race_treats_existing_as_found(cfg):
    lam = FakeLambda()
    orig = lam.create_function

    def racing(**kw):
        orig(**kw)
        raise _ClientError("ResourceConflictException")

    lam.create_function = racing
    assert lambda_ops.ensure_lambda(cfg, lam)["action"] in ("created", "unchanged")


def test_missing_zip_is_a_clear_error(cfg, zips):
    (zips / "loader.zip").unlink()
    with pytest.raises(lambda_ops.LoadConfigError, match="loader.zip"):
        lambda_ops.ensure_lambda(cfg, FakeLambda())


# ---------- payload ----------

def test_payload_from_profile(cfg):
    p = lambda_ops.build_payload(cfg, force=True)
    assert p == {"force": True, "periods": None, "months": 4,
                 "source": {"export_bucket": "acme-focus-export", "export_prefix": "focus/acme-focus/data/"},
                 "target": {"table_bucket_arn": PROFILE["table_bucket_arn"], "namespace": "focus", "table": "focus"},
                 "state": {"bucket": "acme-focus-state", "key": "pico-finops-agent/state.json"}}


@pytest.mark.parametrize("key", ["export_bucket", "state_bucket", "lambda_role_arn", "table_bucket_arn"])
def test_payload_refuses_missing_values(tmp_path, zips, key):
    bad = loader.Config.from_profile({**PROFILE, key: f"<MISSING:FOCUS_{key.upper()}>",
                                      "cache_path": str(tmp_path / "f.duckdb"), "zip_dir": str(zips)})
    with pytest.raises(lambda_ops.LoadConfigError, match=key):
        lambda_ops.build_payload(bad, force=False)


@pytest.mark.parametrize("value,want", [("6", 6), (12, 12), ("1", 1)])
def test_payload_months_from_profile(tmp_path, zips, value, want):
    c = loader.Config.from_profile({**PROFILE, "months": value, "cache_path": str(tmp_path / "f.duckdb"),
                                    "zip_dir": str(zips)})
    assert lambda_ops.build_payload(c)["months"] == want


@pytest.mark.parametrize("value", ["0", "121", "four", "", "<MISSING:FOCUS_MONTHS>"])
def test_payload_refuses_bad_months(tmp_path, zips, value):
    c = loader.Config.from_profile({**PROFILE, "months": value, "cache_path": str(tmp_path / "f.duckdb"),
                                    "zip_dir": str(zips)})
    with pytest.raises(lambda_ops.LoadConfigError, match="FOCUS_MONTHS"):
        lambda_ops.build_payload(c)


# ---------- load: invoke + result handling ----------

OK_NEW = {"status": "ok", "table_created": True, "backfill_pending": [], "new_deliveries": ["2026-10"],
          "months_loaded": [{"period": "2026-10", "rows": 5, "delivery": "d", "seconds": 1.0}],
          "schema_diff": None, "state_etag": '"e"', "error": None}
OK_NOOP = {**OK_NEW, "table_created": False, "new_deliveries": [], "months_loaded": []}
PARTIAL = {**OK_NEW, "status": "partial", "backfill_pending": ["2026-11"]}


@pytest.fixture
def run(cfg, monkeypatch):
    refreshed = []

    def go(results, cache=False, refresh_error=None, force=False):
        lam = FakeLambda(results)
        monkeypatch.setattr(lambda_ops, "lambda_client", lambda c: lam)

        def fake_refresh(c):
            refreshed.append(1)
            if refresh_error:
                raise refresh_error
            Path(c.cache_path).write_bytes(b"cache")
            return 5

        monkeypatch.setattr(loader, "refresh_cache", fake_refresh)
        if cache:
            Path(cfg.cache_path).parent.mkdir(parents=True, exist_ok=True)
            Path(cfg.cache_path).write_bytes(b"old")
        rc = loader.run_load(cfg, force=force)
        return rc, lam, loader.read_state(cfg)

    go.refreshed = refreshed
    return go


def test_first_load_creates_lambda_invokes_and_refreshes(run):
    rc, lam, st = run([OK_NEW])
    assert rc == 0
    assert "create_function" in lam.calls and lam.calls.count("invoke") == 1
    assert run.refreshed == [1]
    assert st["last_result"]["status"] == "ok" and st["last_error"] is None and st["last_success"]


def test_insights_saved_after_refresh(run, monkeypatch):
    from pico_plugin_focus_data import insights

    monkeypatch.setattr(insights, "compute", lambda path, t: {"last_full_day": "2026-11-04", "spikes": []})
    rc, _, st = run([OK_NEW])
    assert rc == 0 and st["last_result"]["insights"] == {"last_full_day": "2026-11-04", "spikes": []}


def test_insights_error_does_not_fail_the_load(run, monkeypatch):
    from pico_plugin_focus_data import insights

    def boom(path, t):
        raise RuntimeError("bad sql")

    monkeypatch.setattr(insights, "compute", boom)
    rc, _, st = run([OK_NEW])
    assert rc == 0 and st["last_result"].get("insights") is None and st["last_error"] is None


def test_noop_with_cache_does_not_refresh(run):
    rc, _, _ = run([OK_NOOP], cache=True)
    assert rc == 0 and run.refreshed == []


def test_removed_months_rebuild_the_cache(run):
    """Months dropped from the S3 Table (older than FOCUS_MONTHS) must leave the local copy too."""
    rc, _, st = run([{**OK_NOOP, "months_removed": ["2026-06"]}], cache=True)
    assert rc == 0 and run.refreshed == [1]
    assert st["last_result"]["months_removed"] == ["2026-06"]


def test_noop_without_cache_refreshes(run):
    rc, _, _ = run([OK_NOOP], cache=False)
    assert rc == 0 and run.refreshed == [1]


def test_no_table_yet_is_ok_without_refresh(run):
    """An empty export: the Lambda creates no table; don't rebuild a cache from a missing table."""
    rc, _, st = run([{**OK_NOOP, "table_exists": False}], cache=False)
    assert rc == 0 and run.refreshed == []
    assert st["cache_needs_rebuild"] is False and st["no_data_yet"] is True and st["last_error"] is None


def test_partial_is_reinvoked_up_to_three_times(run):
    rc, lam, st = run([PARTIAL, PARTIAL, PARTIAL, PARTIAL])
    assert lam.calls.count("invoke") == 4  # first + 3 more
    assert rc == 0 and st["last_result"]["status"] == "partial"
    rc, lam, _ = run([PARTIAL, OK_NEW])
    assert lam.calls.count("invoke") == 2


@pytest.mark.parametrize("status,rc", [("error", 1), ("conflict", 1), ("bad_request", 2), ("schema_changed", 2)])
def test_failure_statuses_and_exit_codes(run, status, rc):
    res = {**OK_NOOP, "status": status, "error": f"{status} detail",
           "schema_diff": {"added": ["x VARCHAR"], "type_changed": []} if status == "schema_changed" else None}
    got, _, st = run([res], cache=True)
    assert got == rc
    assert status in st["last_error"]
    if status == "schema_changed":
        assert st["schema_diff"] == {"added": ["x VARCHAR"], "type_changed": []}


def test_function_error_is_error(run):
    rc, _, st = run([{"errorMessage": "boom", "_function_error": True}], cache=True)
    assert rc == 1 and "boom" in st["last_error"]


def test_deleted_function_is_recreated_and_invoked_again(run, cfg):
    rc, lam, _ = run([_ClientError("ResourceNotFoundException"), OK_NEW])
    assert rc == 0 and lam.calls.count("invoke") == 2 and lam.calls.count("create_function") >= 1


def test_missing_value_refused_before_any_aws_call(tmp_path, zips, monkeypatch):
    bad = loader.Config.from_profile({**PROFILE, "export_bucket": "<MISSING:FOCUS_EXPORT_BUCKET>",
                                      "cache_path": str(tmp_path / "d" / "f.duckdb"), "zip_dir": str(zips)})
    monkeypatch.setattr(lambda_ops, "lambda_client", lambda c: (_ for _ in ()).throw(AssertionError("no AWS")))
    assert loader.run_load(bad) == 2
    assert "export_bucket" in loader.read_state(bad)["last_error"]


def test_failed_refresh_marks_cache_needs_rebuild_and_next_run_refreshes(run):
    rc, _, st = run([OK_NEW], refresh_error=OSError("disk full"))
    assert rc == 1 and st["cache_needs_rebuild"] is True and "disk full" in st["last_error"]
    rc, _, st = run([OK_NOOP], cache=True)
    assert rc == 0 and run.refreshed == [1, 1] and st["cache_needs_rebuild"] is False


def test_concurrent_load_is_skipped(cfg, monkeypatch):
    import fcntl

    Path(cfg.cache_path).parent.mkdir(parents=True, exist_ok=True)
    held = open(Path(cfg.work_dir) / "load.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(lambda_ops, "lambda_client", lambda c: (_ for _ in ()).throw(AssertionError("no AWS")))
    try:
        assert loader.run_load(cfg) == 0
    finally:
        held.close()
    log = (Path(cfg.work_dir) / "load.jsonl").read_text(encoding="utf-8")
    assert '"skipped"' in log


# ---------- status ----------

def test_status_shows_identity_remote_state_and_local(cfg, monkeypatch, capsys):
    monkeypatch.setattr(lambda_ops, "identity", lambda c: "arn:aws:sts::111122223333:assumed-role/ec2-role/i-1")
    monkeypatch.setattr(lambda_ops, "remote_state", lambda c: {"loaded": {"2026-10": "u"}, "backfill_done": True})
    assert cli.main(["status"], {**PROFILE, "cache_path": cfg.cache_path, "zip_dir": cfg.zip_dir}) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["aws_identity"].endswith("/i-1")
    assert out["s3_table_state"]["backfill_done"] is True
    assert "last_load" in out


def test_status_without_credentials_says_so(cfg, monkeypatch, capsys):
    def no_creds(c):
        raise lambda_ops.NoCredentials("no AWS credentials: attach the instance role, and set the IMDSv2 hop limit to 2 for Docker")

    monkeypatch.setattr(lambda_ops, "identity", no_creds)
    monkeypatch.setattr(lambda_ops, "remote_state", lambda c: (_ for _ in ()).throw(AssertionError("skipped")))
    assert cli.main(["status"], {**PROFILE, "cache_path": cfg.cache_path, "zip_dir": cfg.zip_dir}) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["aws_identity"].startswith("no AWS credentials")


def test_load_without_credentials_needs_a_person(cfg, monkeypatch):
    """A retry can't fix missing credentials: exit 2 with the same hint as status."""
    from botocore.exceptions import NoCredentialsError

    lam = FakeLambda()

    def no_creds(**kw):
        raise NoCredentialsError()

    lam.list_layer_versions = no_creds
    monkeypatch.setattr(lambda_ops, "lambda_client", lambda c: lam)
    assert loader.run_load(cfg) == 2
    assert loader.read_state(cfg)["last_error"] == lambda_ops.NO_CREDENTIALS_MSG


def test_permission_error_is_classified_and_needs_a_person(run, monkeypatch):
    def denied(cfg, lam, refresh_role=False):
        raise _ClientError("AccessDeniedException", "User: x is not authorized to perform: lambda:GetFunction on resource: y")

    monkeypatch.setattr(lambda_ops, "ensure_lambda", denied)
    rc, _, st = run([])
    assert rc == 2  # a retry can't fix a missing permission
    assert st["problem"]["code"] == "ec2_permission" and st["problem"]["aws_action"] == "lambda:GetFunction"


def test_lambda_error_result_is_classified(run):
    res = {**OK_NOOP, "status": "error",
           "error": "ClientError: An error occurred (AccessDenied) when calling the ListObjectsV2 operation: not authorized to perform: s3:ListBucket"}
    rc, _, st = run([res], cache=True)
    assert rc == 2 and st["problem"]["code"] == "lambda_permission"


def test_success_clears_the_problem_and_keeps_data_through(run, monkeypatch):
    from pico_plugin_focus_data import insights

    monkeypatch.setattr(insights, "compute", lambda path, t: {"last_full_day": "2026-11-04"})
    rc, _, st = run([OK_NEW])
    assert rc == 0 and st["problem"] is None and st["data_through"] == "2026-11-04"


def test_months_from_a_failed_rebuild_are_announced_later(run):
    rc, _, st = run([OK_NEW], refresh_error=OSError("No space left on device"))
    assert rc == 1 and st["problem"]["code"] == "cache_rebuild" and st["unannounced_months"] == ["2026-10"]
    rc, _, st = run([OK_NOOP], cache=True)
    assert rc == 0 and st["last_result"]["months_loaded"] == ["2026-10"] and not st.get("unannounced_months")


# ---------- a recreated Lambda role (seen live 2026-11-05) ----------

def test_refresh_role_resaves_the_same_role(cfg):
    """A deleted and recreated role keeps its ARN but the function keeps the old role's credentials
    until its configuration is saved again."""
    lam = FakeLambda()
    lambda_ops.ensure_lambda(cfg, lam)
    lam.calls.clear()
    lambda_ops.ensure_lambda(cfg, lam, refresh_role=True)
    assert "update_function_configuration" in lam.calls and lam.fn["Role"] == PROFILE["lambda_role_arn"]


ROLE_GONE = {**OK_NOOP, "status": "error", "error": "ClientError: An error occurred (InvalidAccessKeyId) when "
             "calling the GetObject operation: The AWS Access Key Id you provided does not exist in our records."}


def test_role_is_resaved_on_the_run_after_role_missing(run, monkeypatch):
    seen = []
    real = lambda_ops.ensure_lambda
    monkeypatch.setattr(lambda_ops, "ensure_lambda",
                        lambda c, lam, refresh_role=False: seen.append(refresh_role) or real(c, lam, refresh_role))
    rc, _, st = run([ROLE_GONE], cache=True)
    assert rc == 2 and st["problem"]["code"] == "lambda_role_missing"
    rc, _, _ = run([OK_NOOP], cache=True)
    assert rc == 0
    run([OK_NOOP], cache=True)
    assert seen == [False, True, False]



# ---------- an interrupted load (the agent restarted mid-load; PR review 2026-11-06) ----------

def test_a_finished_load_leaves_no_in_progress_mark(run):
    rc, _, st = run([OK_NOOP], cache=True)
    assert rc == 0 and "load_in_progress" not in st


def test_failed_load_clears_the_mark_too(run):
    rc, _, st = run([{**OK_NOOP, "status": "error", "error": "x"}], cache=True)
    assert "load_in_progress" not in st


def test_load_after_an_interrupted_one_rebuilds_the_local_copy(run, cfg, capsys):
    """The killed load may have put new months in the S3 table without rebuilding the local copy."""
    import os as _os

    _os.makedirs(cfg.work_dir, exist_ok=True)
    loader._write_state(cfg, {**loader.read_state(cfg), "load_in_progress": "2026-11-06T06:30:00+00:00"})
    rc, _, st = run([OK_NOOP], cache=True)  # nothing new delivered
    assert rc == 0 and run.refreshed == [1] and "load_in_progress" not in st
    assert "previous_load_interrupted" in capsys.readouterr().out + open(cfg.log_path).read()
