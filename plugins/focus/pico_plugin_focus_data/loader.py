"""`pico focus-data load`: ensure the Lambda -> invoke it -> rebuild the local DuckDB cache. No LLM involved.

One load cycle:
  lock /data/load.lock (a second caller logs `skipped`)
  -> payload from the profile (refuses empty / <MISSING:VAR> values before any AWS call)
  -> ensure_lambda (create / update `pico-finops-agent-loader` from the zips)
  -> sync invoke; `partial` is invoked again at once, up to 3 more times
  -> rebuild /data/focus.duckdb from the S3 Table when months were loaded, the table was created,
     the cache is missing or the last rebuild failed; temp file + atomic rename
  -> local state.json (last_success / last_error / last_result) + load.jsonl -> unlock
Exit codes: 0 ok / partial / skipped; 1 error or conflict (the scheduler retries once);
2 bad_request, schema_changed or a config error (a person must act; no retry).
"""
import fcntl
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import duckdb

from . import lambda_ops, problems

REQUIRED = ("table_bucket_arn", "export_bucket", "export_prefix", "cache_path")
MAX_PARTIAL_REINVOKES = 3
RC_OK, RC_RETRY, RC_PERSON = 0, 1, 2
_RC = {"ok": RC_OK, "partial": RC_OK, "error": RC_RETRY, "conflict": RC_RETRY,
       "bad_request": RC_PERSON, "schema_changed": RC_PERSON}


def _q_str(value):
    """SQL string literal (DuckDB): single quotes doubled."""
    return "'" + str(value).replace("'", "''") + "'"


def _q_ident(name):
    """SQL identifier (DuckDB): double quotes doubled."""
    return '"' + str(name).replace('"', '""') + '"'


@dataclass
class Config:
    table_bucket_arn: str
    export_bucket: str
    export_prefix: str
    cache_path: str
    # Unset in the container: boto3 and DuckDB use the default credential chain (EC2 instance role,
    # Lambda execution role). Developers may set it for runs outside AWS.
    aws_profile: Optional[str] = None
    region: str = "us-east-1"
    namespace: str = "pico_finops_agent"
    table: str = "focus"
    duckdb_threads: int = 3
    months: str = "4"  # FOCUS_MONTHS: months kept, counting the current one (the profile gives text)
    state_bucket: Optional[str] = None
    state_key: str = "pico-finops-agent/state.json"
    lambda_role_arn: Optional[str] = None
    zip_dir: str = "/opt/pico-finops-agent-loader"

    @classmethod
    def from_profile(cls, profile):
        known = cls.__dataclass_fields__
        missing = [k for k in REQUIRED if not profile.get(k)]
        if missing:
            raise SystemExit(f"focus-data: set {', '.join(missing)} under agent.plugins.focus-data in agent.yml")
        return cls(**{k: v for k, v in profile.items() if k in known})

    @property
    def work_dir(self):
        return os.path.dirname(os.path.abspath(self.cache_path))

    @property
    def state_path(self):
        return os.path.join(self.work_dir, "state.json")

    @property
    def log_path(self):
        return os.path.join(self.work_dir, "load.jsonl")

    @property
    def fq_table(self):
        return f"s3t.{_q_ident(self.namespace)}.{_q_ident(self.table)}"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_state(cfg):
    base = {"last_success": None, "last_error": None, "last_result": None, "cache_needs_rebuild": False}
    if os.path.exists(cfg.state_path):
        with open(cfg.state_path, encoding="utf-8") as fh:
            return {**base, **json.load(fh)}
    return base


def _write_state(cfg, state):
    tmp = cfg.state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, cfg.state_path)


def _log(cfg, event, **kw):
    rec = {"at": _now(), "event": event, **kw}
    print(json.dumps(rec), flush=True)
    with open(cfg.log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def secret_sql(cfg):
    """DuckDB S3 secret from the default credential chain; PROFILE only when a developer set one."""
    profile = f", PROFILE {_q_str(cfg.aws_profile)}" if cfg.aws_profile else ""
    return f"CREATE SECRET (TYPE s3, PROVIDER credential_chain{profile}, REGION {_q_str(cfg.region)})"


def _connect(cfg):
    c = duckdb.connect()
    c.sql(f"SET threads = {int(cfg.duckdb_threads)}")
    for ext in ("httpfs", "aws", "iceberg"):
        c.sql(f"INSTALL {ext}")
        c.sql(f"LOAD {ext}")
    c.sql(secret_sql(cfg))
    c.sql(f"ATTACH {_q_str(cfg.table_bucket_arn)} AS s3t (TYPE iceberg, ENDPOINT_TYPE s3_tables)")
    return c


def refresh_cache(cfg):
    """Copy the S3 Table into a local file; timestamptz columns become plain UTC TIMESTAMPs."""
    c = _connect(cfg)
    try:
        c.sql("SET TimeZone = 'UTC'")
        cols = c.sql(f"DESCRIBE SELECT * FROM {cfg.fq_table}").fetchall()
        sel = ", ".join(f"({_q_ident(n)} AT TIME ZONE 'UTC') AS {_q_ident(n)}" if typ.startswith("TIMESTAMP WITH TIME ZONE")
                        else _q_ident(n) for n, typ, *_ in cols)
        tmp = cfg.cache_path.replace(".duckdb", ".new.duckdb")
        if os.path.exists(tmp):
            os.remove(tmp)
        c.sql(f"ATTACH {_q_str(tmp)} AS cache")
        c.sql(f"CREATE TABLE cache.focus AS SELECT {sel} FROM {cfg.fq_table}")
        rows = c.sql("SELECT count(*) FROM cache.focus").fetchone()[0]
        c.sql("DETACH cache")
    finally:
        c.close()
    os.replace(tmp, cfg.cache_path)  # readers see the old file or the new one, never a half-written one
    return rows


def _invoke_until_done(cfg, lam, payload):
    """Invoke; on `partial` invoke again at once (up to 3 more). A deleted function is re-ensured once."""
    results = []
    for _ in range(1 + MAX_PARTIAL_REINVOKES):
        try:
            res = lambda_ops.invoke(lam, payload)
        except Exception as e:  # noqa: BLE001 - only "function not found" is handled here
            if not lambda_ops.is_not_found(e):
                raise
            _log(cfg, "loader_function_missing", action="ensure again")
            lambda_ops.ensure_lambda(cfg, lam)
            res = lambda_ops.invoke(lam, payload)
        _log(cfg, "loader_result", status=res.get("status"), months=[m["period"] for m in res.get("months_loaded", [])],
             table_created=res.get("table_created"), pending=res.get("backfill_pending"),
             empty=res.get("empty_deliveries") or [], error=res.get("error"))
        results.append(res)
        if res.get("status") != "partial":
            break
    return results


def run_load(cfg, force=False):
    os.makedirs(cfg.work_dir, exist_ok=True)
    lock = open(os.path.join(cfg.work_dir, "load.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _log(cfg, "skipped", reason="another load is running")
        lock.close()
        return RC_OK

    t0 = time.perf_counter()
    state = read_state(cfg)
    if state.get("load_in_progress"):
        # the previous load was cut short (the agent stopped or restarted): the S3 table may hold months the local
        # copy doesn't, so rebuild the copy this time even if nothing new was delivered
        state["cache_needs_rebuild"] = True
        _log(cfg, "previous_load_interrupted", started=state["load_in_progress"])
    state["load_in_progress"] = _now()
    _write_state(cfg, state)
    rc = RC_RETRY
    stage = "aws"
    try:
        payload = lambda_ops.build_payload(cfg, force=force)
        lam = lambda_ops.lambda_client(cfg)
        role_was_missing = (state.get("problem") or {}).get("code") == "lambda_role_missing"
        ensured = lambda_ops.ensure_lambda(cfg, lam, refresh_role=role_was_missing)
        _log(cfg, "loader_function", **ensured)
        results = _invoke_until_done(cfg, lam, payload)
        last = results[-1]
        loaded = [m["period"] for r in results for m in r.get("months_loaded", [])]
        removed = sorted({p for r in results for p in r.get("months_removed", [])})
        created = any(r.get("table_created") for r in results)
        status = last.get("status", "error")
        state["last_result"] = {"status": status, "months_loaded": loaded, "months_removed": removed,
                                "table_created": created,
                                "backfill_pending": last.get("backfill_pending", []), "at": _now()}
        state["schema_diff"] = last.get("schema_diff")
        state["no_data_yet"] = last.get("table_exists") is False and not created
        if state["no_data_yet"]:
            _log(cfg, "no_data_yet", reason="nothing delivered to the export yet, so there is no table to read")
        elif loaded or removed or created or state.get("cache_needs_rebuild") or (
                not os.path.exists(cfg.cache_path) and status in ("ok", "partial", "schema_changed")):
            s = time.perf_counter()
            stage = "cache"
            try:
                rows = refresh_cache(cfg)
            except Exception:
                state["cache_needs_rebuild"] = True
                # announce these months when a later rebuild succeeds
                state["unannounced_months"] = sorted(set(state.get("unannounced_months") or []) | set(loaded))
                raise
            state["cache_needs_rebuild"] = False
            if state.get("unannounced_months"):
                state["last_result"]["months_loaded"] = sorted(set(loaded) | set(state["unannounced_months"]))
                state["unannounced_months"] = []
            _log(cfg, "cache_rebuilt", rows=rows, seconds=round(time.perf_counter() - s, 2),
                 mb=round(os.path.getsize(cfg.cache_path) / 1e6, 1))
            try:  # numbers for the post-load Slack notice; never fails the load
                from . import insights

                state["last_result"]["insights"] = insights.compute(cfg.cache_path, insights.Thresholds.from_env())
                state["data_through"] = (state["last_result"]["insights"] or {}).get("last_full_day")
            except Exception as e:  # noqa: BLE001
                _log(cfg, "insights_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        rc = _RC.get(status, RC_RETRY)
        if rc == RC_OK:
            state.update(last_success=_now(), last_error=None, problem=None)
        else:
            state["problem"] = problems.from_lambda_result(status, last.get("error"))
            rc = RC_PERSON if state["problem"]["permanent"] else RC_RETRY
            state.update(last_error=f"{status}: {last.get('error')}", last_error_at=_now())
        _log(cfg, "done" if rc == RC_OK else "failed", status=status, seconds=round(time.perf_counter() - t0, 2))
        return rc
    except lambda_ops.LoadConfigError as e:
        state.update(last_error=f"config: {e}", last_error_at=_now(), problem=problems.config(e))
        _log(cfg, "failed", error=state["last_error"])
        return RC_PERSON
    except lambda_ops.NoCredentialsError:  # a retry can't fix this
        state.update(last_error=lambda_ops.NO_CREDENTIALS_MSG, last_error_at=_now(),
                     problem=problems.credentials(cause="missing", source=lambda_ops.credential_source(cfg)))
        _log(cfg, "failed", error=state["last_error"])
        return RC_PERSON
    except Exception as e:  # kept in state.json so `status` (and the chat skill) can report it
        state["problem"] = problems.from_exception(e, stage=stage,
                                                   source_of=lambda: lambda_ops.credential_source(cfg))
        state.update(last_error=f"{type(e).__name__}: {str(e)[:300]}", last_error_at=_now())
        _log(cfg, "failed", error=state["last_error"], problem=state["problem"]["code"],
             seconds=round(time.perf_counter() - t0, 2))
        return RC_PERSON if state["problem"]["permanent"] else RC_RETRY
    finally:
        state.pop("load_in_progress", None)
        _write_state(cfg, state)
        lock.close()


def status(cfg):
    """Local state + the Lambda's state.json + who we are in AWS (credential check)."""
    out = {"last_load": read_state(cfg), "cache_exists": os.path.exists(cfg.cache_path)}
    try:
        out["aws_identity"] = lambda_ops.identity(cfg)
    except lambda_ops.NoCredentials as e:
        out["aws_identity"] = str(e)
        return out
    except Exception as e:  # noqa: BLE001 - status must always print something
        out["aws_identity"] = f"unknown: {type(e).__name__}: {str(e)[:200]}"
        return out
    try:
        out["s3_table_state"] = lambda_ops.remote_state(cfg)
    except Exception as e:  # noqa: BLE001
        out["s3_table_state"] = f"unreadable: {type(e).__name__}: {str(e)[:200]}"
    table = out["s3_table_state"]
    # each month's AWS export mode as the Lambda found it: create_new | overwrite | mixed (both layouts)
    out["export_modes"] = dict(table.get("export_modes") or {}) if isinstance(table, dict) else {}
    return out
