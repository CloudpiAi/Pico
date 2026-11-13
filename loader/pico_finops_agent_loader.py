"""Lambda `pico-finops-agent-loader`: FOCUS export (S3) -> S3 Table (Iceberg), one month per Iceberg commit.

Handler: pico_finops_agent_loader.lambda_handler. Python 3.12, DuckDB from the `pico-finops-agent-duckdb` layer, extensions
bundled in the function zip (no INSTALL at run time). Credentials: the Lambda execution role only.

Event (every field checked first; a bad one returns `bad_request` without touching S3):
  {"force": false, "periods": null | ["YYYY-MM", ...], "months": 4 (1-120, default 4),
   "source": {"export_bucket": "...", "export_prefix": ".../data/"},
   "target": {"table_bucket_arn": "arn:aws:s3tables:<region>:<acct>:bucket/<name>", "namespace": "focus", "table": "focus"},
   "state":  {"bucket": "...", "key": "pico-finops-agent/state.json"}}
Result:
  {"status": "ok"|"partial"|"conflict"|"bad_request"|"schema_changed"|"error", "table_created": bool,
   "table_exists": bool (false: nothing delivered yet, so no table to read),
   "backfill_pending": [...], "new_deliveries": [...],
   "months_loaded": [{period, rows, delivery, seconds, cast?: ["col FROM -> TO", ...]}],
   "empty_deliveries": [...] (newest delivery had 0 rows: nothing changed for that month),
   "months_removed": [...] (older than the last `months` months: deleted from the table),
   "schema_diff": null | {"added": [...], "type_changed": [the casts tried], "cannot_convert"?: "..."},
   "state_etag": "...", "error": null | "..."}

Export modes (worked out per month from what is in S3, no setting): "Create new" puts each delivery in its own folder,
billing_period=X/<delivery>/*.parquet; the newest folder is loaded and its URL is the "has it changed" token in
state.json `loaded`. "Overwrite" puts the files straight in the month folder, billing_period=X/*.parquet; the files
the month's manifest lists (<export root>/metadata/billing_period=X/<export>-Manifest.json `dataFiles`, only files
really in that folder) are loaded and the token is overwrite:<executionId>; without a usable manifest every parquet
in the folder is loaded and the token is overwrite:etag:<sha256 of the keys and ETags>. A month with both layouts
(the export changed mode) loads the newer set. After an overwrite month is loaded, its token is read again: when AWS
rewrote the month meanwhile it isn't saved, so the next run loads the month again. state.json `export_modes` says
which mode each month had (create_new | overwrite | mixed).

Months kept: only the last `months` months, counting the current month (4 in November = Aug..Nov). Older
exported months are never loaded, and months older than that are deleted from the table on every run.

Table lifecycle: a missing namespace/table is created from the newest delivery's schema (columns lowercased,
partitioned by month); a missing table also resets `loaded` and backfills every month kept, oldest first.
The table keeps the types it was created with: a delivery column of another type is CAST to the table's type at
insert (strict, never TRY_CAST). A value that can't be converted stops with `schema_changed` and writes nothing
for that month, as does a new column. A missing column is loaded as NULL. A delivery with 0 rows is no new data:
the month's rows stay as they are. Before each month the remaining Lambda time is checked (120 s); when short,
the run stops with `partial` and the next run continues. state.json is written after every month with an ETag
condition.
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

EXTENSIONS = ("httpfs", "aws", "avro", "iceberg")  # iceberg needs avro; Lambda cannot auto-install it
CONFLICT_CODES = ("PreconditionFailed", "ConditionalRequestConflict")
NOT_FOUND_CODES = ("NotFoundException", "NoSuchKey", "404")
# A manifest that is missing or not ours to read: use the ETag fallback. Any other S3 error (throttling, 5xx) fails
# the run, so the month's token never flips between overwrite:<executionId> and overwrite:etag:... on a passing error.
NO_MANIFEST_CODES = NOT_FOUND_CODES + ("AccessDenied", "403")
PARTITION_CLAUSE = " PARTITIONED BY (month(billingperiodstart))"
MIN_REMAINING_MS = 120_000
DEFAULT_MONTHS = 4  # months kept, counting the current month (FOCUS_MONTHS on the EC2)
MAX_MONTHS = 120

# Same rules as pico_finops_agent/entrypoint.sh (customer .env check).
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
NAME_RE = re.compile(r"^[a-z0-9_]{1,255}\Z")
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])\Z")
# Real AWS folders: 2026-11-02T15:07:11.328Z-e2891bd6-5d2b-3e19-83a2-6eda07f5202e
DELIVERY_RE = re.compile(r"^[0-9A-Za-z_.:=-]+\Z")
PERIOD_PREFIX_RE = re.compile(r"billing_period=(\d{4}-\d{2})/$")
# Real AWS overwrite files: focus2-00001.snappy.parquet (no quotes or SQL characters allowed)
FILE_RE = re.compile(r"^[0-9A-Za-z_.=-]+\.parquet\Z")
_OLDEST = datetime(1970, 1, 1, tzinfo=timezone.utc)


class BadRequest(Exception):
    pass


class SchemaChanged(Exception):
    def __init__(self, period, diff):
        super().__init__(f"{period}: delivery doesn't fit the table: {diff}")
        self.period = period
        self.diff = diff


class MonthUnknown(Exception):
    """A delivery has rows but no BillingPeriodStart, so the month to replace is unknown (an `error`, not a schema
    change: the table is fine)."""


def _log(event, **kw):
    print(json.dumps({"at": _now().isoformat(timespec="seconds"), "event": event, **kw}), flush=True)


def _now():
    return datetime.now(timezone.utc)


def _client(name):
    return boto3.client(name)


def q_str(value):
    return "'" + str(value).replace("'", "''") + "'"


def q_ident(name):
    return '"' + str(name).replace('"', '""') + '"'


# ---------------- payload check ----------------

def _need(d, key, where):
    if not isinstance(d, dict) or not isinstance(d.get(key), str) or not d.get(key):
        raise BadRequest(f"{where}.{key}: missing or not a string")
    return d[key]


def validate(event, region):
    """Return the checked event or raise BadRequest naming the field."""
    if not isinstance(event, dict):
        raise BadRequest("event: must be an object")
    src, tgt, st = event.get("source"), event.get("target"), event.get("state")
    bucket = _need(src, "export_bucket", "source")
    if not BUCKET_RE.match(bucket):
        raise BadRequest("source.export_bucket: not a valid S3 bucket name")
    prefix = _need(src, "export_prefix", "source")
    if prefix.startswith("/") or not prefix.endswith("/") or ".." in prefix or re.search(r"['\";]", prefix):
        raise BadRequest("source.export_prefix: must end with /, not start with /, no .., quotes or ;")
    arn = _need(tgt, "table_bucket_arn", "target")
    arn_re = rf"^arn:aws[a-z-]*:s3tables:{re.escape(region)}:\d{{12}}:bucket/[a-z0-9-]{{3,63}}\Z"
    if not re.match(arn_re, arn):
        raise BadRequest(f"target.table_bucket_arn: not an S3 Tables bucket ARN in {region}")
    for key in ("namespace", "table"):
        if not NAME_RE.match(_need(tgt, key, "target")):
            raise BadRequest(f"target.{key}: only a-z, 0-9 and _")
    if not BUCKET_RE.match(_need(st, "bucket", "state")):
        raise BadRequest("state.bucket: not a valid S3 bucket name")
    key = _need(st, "key", "state")
    if not key.endswith(".json") or ".." in key or key.startswith("/"):
        raise BadRequest("state.key: must end with .json, no ..")
    periods = event.get("periods")
    if periods is not None and (not isinstance(periods, list) or not all(
            isinstance(p, str) and PERIOD_RE.match(p) for p in periods)):
        raise BadRequest("periods: null or a list of YYYY-MM")
    force = event.get("force", False)
    if not isinstance(force, bool):
        raise BadRequest("force: must be true or false")
    months = event.get("months", DEFAULT_MONTHS)
    if isinstance(months, bool) or not isinstance(months, int) or not 1 <= months <= MAX_MONTHS:
        raise BadRequest(f"months: a whole number from 1 to {MAX_MONTHS}")
    return {"force": force, "periods": periods, "months": months, "source": {"export_bucket": bucket, "export_prefix": prefix},
            "target": {"table_bucket_arn": arn, "namespace": tgt["namespace"], "table": tgt["table"]},
            "state": {"bucket": st["bucket"], "key": key}}


# ---------------- months ----------------

def empty_state():
    return {"loaded": {}, "backfill_done": False, "table_created_at": None, "last_success": None, "last_error": None}


def periods_due(now):
    """Current month, plus the previous month for the first 14 days (AWS restatement window)."""
    cur = now.strftime("%Y-%m")
    if now.day > 14:
        return [cur]
    prev = datetime(now.year - (now.month == 1), (now.month - 2) % 12 + 1, 1).strftime("%Y-%m")
    return [prev, cur]


def month_window(now, months):
    """The last `months` months, current month included, oldest first: (now=2026-11, 4) -> 2026-08..2026-11."""
    year, month = now.year, now.month
    out = []
    for _ in range(months):
        out.append(f"{year:04d}-{month:02d}")
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return out[::-1]


def select_periods(state, available, now, force, periods, months=DEFAULT_MONTHS):
    """{period: delivery} to load, oldest first. available = {period: its newest Delivery (or a token string)};
    a month is due when its token differs from the one in state.json `loaded`.

    Only months inside month_window count. After the backfill: the due months, plus any month in the window
    never loaded (the window grew, e.g. FOCUS_MONTHS went from 4 to 6)."""
    window = set(month_window(now, months))
    available = {p: u for p, u in available.items() if p in window}
    if periods:
        wanted = periods
    elif not state.get("backfill_done"):
        wanted = sorted(available)
    else:
        wanted = periods_due(now) + [p for p in available if p not in state["loaded"]]
    return {p: available[p] for p in sorted(set(wanted)) if p in available
            and (force or state["loaded"].get(p) != getattr(available[p], "token", available[p]))}


def list_periods(s3, bucket, prefix):
    out = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            m = PERIOD_PREFIX_RE.search(cp["Prefix"])
            if m:
                out.add(m.group(1))
    return sorted(out)


@dataclass
class Delivery:
    """A month's newest data in the export: the files to read and the token that says whether it changed."""
    mode: str              # create_new | overwrite (the layout loaded)
    files: list            # URLs for read_parquet
    token: str             # saved in state.json `loaded`
    ident: str             # the delivery folder, the executionId, or etag:<first 12 of the digest>
    written_at: datetime = None
    mixed: bool = False    # the month folder had both layouts

    @property
    def export_mode(self):
        return "mixed" if self.mixed else self.mode


def delivery_source(bucket, prefix, period, folder):
    return f"s3://{bucket}/{prefix}billing_period={period}/{folder}/*.parquet"


def _object_url(bucket, key):
    return f"s3://{bucket}/{key}"


def _export_root(prefix):
    """The export root for a .../data/ prefix (its manifests are under <root>metadata/); None otherwise."""
    if prefix == "data/":
        return ""
    return prefix[:-len("data/")] if prefix.endswith("/data/") else None


def read_manifest(s3, bucket, prefix, period):
    """(executionId, dataFiles) from the month's <export>-Manifest.json, or None when there is none to use (no
    data/ prefix, no manifest, no access, not JSON, or missing fields). The -Manifest-FOCUS.json next to it has no
    dataFiles and is skipped."""
    root = _export_root(prefix)
    if root is None:
        return None
    base = f"{root}metadata/billing_period={period}/"
    try:
        keys = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=base,
                                                                                   Delimiter="/")
                for o in page.get("Contents", [])]
        for key in sorted((k for k in keys if k.endswith("-Manifest.json")), reverse=True):
            try:
                m = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
            except ValueError as e:
                _log("manifest_unreadable", period=period, error=f"not JSON: {str(e)[:120]}")
                continue
            eid = m.get("executionId") if isinstance(m, dict) else None
            files = m.get("dataFiles") if isinstance(m, dict) else None
            if isinstance(eid, str) and eid and isinstance(files, list) and files and all(
                    isinstance(f, str) for f in files):
                return eid, list(dict.fromkeys(files))  # a file listed twice is read once
    except ClientError as e:
        if _code(e) not in NO_MANIFEST_CODES:
            raise
        _log("manifest_unreadable", period=period, error=_code(e))
    return None


def _overwrite_delivery(s3, bucket, prefix, period, flat):
    listed = {_object_url(bucket, o["Key"]): o for o in flat}
    written = max((o["LastModified"] for o in flat if o.get("LastModified")), default=None)
    man = read_manifest(s3, bucket, prefix, period)
    if man:
        eid, files = man
        if all(f in listed for f in files):  # only files really in this month's folder are read
            return Delivery("overwrite", list(files), f"overwrite:{eid}", eid, written)
        _log("manifest_ignored", period=period, reason="it lists files that are not in this month's folder")
    digest = hashlib.sha256("\n".join(f"{o['Key']} {o.get('ETag', '')}" for o in
                                       sorted(flat, key=lambda o: o["Key"])).encode()).hexdigest()
    return Delivery("overwrite", sorted(listed), f"overwrite:etag:{digest}", f"etag:{digest[:12]}", written)


def find_delivery(s3, bucket, prefix, period):
    """The month's newest delivery (create new or overwrite layout, see the module notes), or None."""
    base = f"{prefix}billing_period={period}/"
    folders, flat, skipped = {}, [], []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=base):
        for o in page.get("Contents", []):
            parts = o["Key"][len(base):].split("/")
            if len(parts) == 2 and parts[1].endswith(".parquet") and DELIVERY_RE.match(parts[0]):
                folders[parts[0]] = max(folders.get(parts[0]) or _OLDEST, o.get("LastModified") or _OLDEST)
            elif len(parts) == 1 and FILE_RE.match(parts[0]):
                flat.append(o)
            elif o["Key"].endswith(".parquet"):
                skipped.append(o["Key"][len(base):])
    if skipped:  # a name with characters outside FILE_RE / DELIVERY_RE is never read (it would go into SQL)
        _log("files_skipped", period=period, count=len(skipped), first=skipped[0][:120])
    new = None
    if folders:
        folder = max(folders)
        url = delivery_source(bucket, prefix, period, folder)
        new = Delivery("create_new", [url], url, folder, folders[folder])
    over = _overwrite_delivery(s3, bucket, prefix, period, flat) if flat else None
    if new and over:  # the export changed mode: the newer set is the month's data
        pick = new if (new.written_at or _OLDEST) >= (over.written_at or _OLDEST) else over
        pick.mixed = True
        return pick
    return new or over


def _still_current(s3, bucket, prefix, period, d):
    """After loading an overwrite month: is it still the same data? False (load it again next run) when AWS rewrote
    the month meanwhile, or when that can't be checked now. Create new: a delivery folder never changes."""
    if d.mode != "overwrite":
        return True
    try:
        again = find_delivery(s3, bucket, prefix, period)
    except Exception as e:  # noqa: BLE001 - the month is loaded; not saving its token only costs a reload
        _log("month_recheck_failed", period=period, error=f"{type(e).__name__}: {str(e)[:200]}")
        return False
    if again is not None and again.token != d.token:
        _log("month_changed_during_load", period=period, loaded=d.ident, now=again.ident)
        return False
    return True


# ---------------- schema + SQL ----------------

def source_sql(files):
    """read_parquet over one URL (a create-new delivery glob) or a list of files (an overwrite month)."""
    files = [files] if isinstance(files, str) else list(files)
    return f"read_parquet([{', '.join(q_str(f) for f in files)}], hive_partitioning = false)"


def _columns(c, from_sql):
    return [(r[0], r[1]) for r in c.sql(f"DESCRIBE SELECT * FROM {from_sql}").fetchall()]


def schema_diff(table_cols, delivery_cols):
    """None when every delivery column is in the table. A missing column is fine (NULL), and a column of
    another type is fine too: insert_sql casts it to the table's type."""
    table = {n.lower() for n, _ in table_cols}
    added = [f"{name.lower()} {typ.upper()}" for name, typ in delivery_cols if name.lower() not in table]
    return {"added": added, "type_changed": []} if added else None


def select_exprs(table_cols, delivery_cols):
    """One SELECT expression per table column, in table order: the delivery's column, CAST to the table's type
    when the types differ, or NULL when the delivery doesn't have it. Also returns the casts ("col FROM -> TO")."""
    by_lower = {name.lower(): (name, typ) for name, typ in delivery_cols}
    exprs, casts = [], []
    for name, typ in table_cols:
        found = by_lower.get(name.lower())
        if found is None:
            exprs.append("NULL")
        elif found[1].upper() == typ.upper():
            exprs.append(q_ident(found[0]))
        else:
            exprs.append(f"CAST({q_ident(found[0])} AS {typ})")
            casts.append(f"{name} {found[1]} -> {typ}")
    return exprs, casts


def insert_sql(fq, table_cols, exprs, src):
    cols = ", ".join(q_ident(n) for n, _ in table_cols)
    return f"INSERT INTO {fq} ({cols}) SELECT {', '.join(exprs)} FROM {src}"


def create_table(c, fq, src):
    cols = _columns(c, src)
    ddl = ", ".join(f"{q_ident(n.lower())} {t}" for n, t in cols)
    c.sql(f"CREATE TABLE {fq} ({ddl}){PARTITION_CLAUSE}")


def remove_old_months(c, fq, oldest):
    """Delete every month before `oldest` (YYYY-MM) in one transaction (one Iceberg commit). Returns the months
    removed; when there are none, nothing is written."""
    cut = f"TIMESTAMPTZ {q_str(oldest + '-01 00:00:00+00')}"
    found = c.sql(f"SELECT DISTINCT strftime(billingperiodstart, '%Y-%m') FROM {fq} "
                  f"WHERE billingperiodstart < {cut}").fetchall()
    gone = sorted(r[0] for r in found)
    if gone:
        c.sql("BEGIN")
        try:
            c.sql(f"DELETE FROM {fq} WHERE billingperiodstart < {cut}")
            c.sql("COMMIT")
        except Exception:
            _rollback(c)
            raise
    return gone


def _rollback(c):
    """Best effort: after a failed COMMIT the transaction may already be gone, and that error must not hide the
    one that caused it."""
    try:
        c.sql("ROLLBACK")
    except Exception:  # noqa: BLE001
        pass


def replace_month(c, fq, files, period=None):
    """Replace a month with a delivery: DELETE + INSERT in one transaction (one Iceberg commit).

    Returns (rows, casts). rows == 0: the delivery is empty, nothing is checked or changed. The month comes from
    billingperiodstart as the table types it, so a delivery that changed that column's type still replaces the
    right month. A strict CAST that fails (ConversionException) rolls back and raises SchemaChanged."""
    import duckdb

    src = source_sql(files)
    rows = c.sql(f"SELECT count(*) FROM {src}").fetchone()[0]
    if rows == 0:
        return 0, []
    delivery = _columns(c, src)
    table = _columns(c, fq)
    diff = schema_diff(table, delivery)
    if diff:
        raise SchemaChanged(period, diff)
    if "billingperiodstart" not in {name.lower() for name, _ in delivery}:
        raise MonthUnknown(f"{period}: the delivery has rows but no BillingPeriodStart, so its month is unknown")
    exprs, casts = select_exprs(table, delivery)
    month_expr = exprs[[name for name, _ in table].index("billingperiodstart")]
    started = False
    try:
        month = c.sql(f"SELECT CAST(min({month_expr}) AS VARCHAR) FROM {src}").fetchone()[0]
        if month is None:
            raise MonthUnknown(f"{period}: BillingPeriodStart is empty in every row, so its month is unknown")
        c.sql("BEGIN")
        started = True
        c.sql(f"DELETE FROM {fq} WHERE billingperiodstart = TIMESTAMPTZ {q_str(month)}")
        c.sql(insert_sql(fq, table, exprs, src))
        c.sql("COMMIT")
    except duckdb.ConversionException as e:  # a value the table's column type can't hold
        if started:
            _rollback(c)
        raise SchemaChanged(period, {"added": [], "type_changed": casts,
                                     "cannot_convert": str(e).splitlines()[0][:300]}) from e
    except Exception:
        if started:
            _rollback(c)
        raise
    return rows, casts


# ---------------- AWS: state, namespace, table, DuckDB ----------------

def _code(e):
    return e.response.get("Error", {}).get("Code", "") if hasattr(e, "response") else ""


def read_state(s3, bucket, key):
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if _code(e) in NOT_FOUND_CODES:
            return empty_state(), None
        raise
    state = {**empty_state(), **json.loads(obj["Body"].read())}
    return state, obj["ETag"]


def write_state(s3, bucket, key, state, etag):
    """Conditional write; returns the new ETag, or None when someone else changed state.json."""
    cond = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
    try:
        out = s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(state, indent=2).encode(),
                            ContentType="application/json", **cond)
    except ClientError as e:
        if _code(e) in CONFLICT_CODES:
            return None
        raise
    return out.get("ETag", etag)


def ensure_namespace(tables, arn, namespace):
    try:
        tables.get_namespace(tableBucketARN=arn, namespace=namespace)
    except ClientError as e:
        if _code(e) not in NOT_FOUND_CODES:
            raise
        tables.create_namespace(tableBucketARN=arn, namespace=[namespace])
        _log("namespace_created", namespace=namespace)


def table_exists(tables, arn, namespace, name):
    try:
        tables.get_table(tableBucketARN=arn, namespace=namespace, name=name)
        return True
    except ClientError as e:
        if _code(e) in NOT_FOUND_CODES:
            return False
        raise


def _load_extensions(c):
    """Load the bundled extensions; /var/task is read-only, so fall back to a copy in the temp folder (Lambda's
    /tmp is private to this function's sandbox)."""
    root = os.environ.get("LAMBDA_TASK_ROOT", os.path.dirname(os.path.abspath(__file__)))
    bundled = os.path.join(root, "duckdb_ext")
    copy = os.path.join(tempfile.gettempdir(), "duckdb_ext")
    for ext_dir in (bundled, copy):
        try:
            if ext_dir == copy and not os.path.isdir(copy):
                shutil.copytree(bundled, copy)
            c.sql(f"SET extension_directory = {q_str(ext_dir)}")
            for ext in EXTENSIONS:
                c.sql(f"LOAD {ext}")
            return
        except Exception as e:  # noqa: BLE001 - try the temp-folder copy before giving up
            _log("extensions_load_retry", dir=ext_dir, error=f"{type(e).__name__}: {str(e)[:200]}")
    raise RuntimeError("could not load DuckDB extensions from the bundle or the temp folder")


def connect(ev):
    import duckdb

    c = duckdb.connect()
    c.sql("SET TimeZone = 'UTC'")  # a delivery TIMESTAMP without a zone is cast to the table's TIMESTAMPTZ as UTC
    tmp = tempfile.gettempdir()  # the only writable folder in Lambda
    c.sql(f"SET home_directory = {q_str(tmp)}")
    c.sql(f"SET temp_directory = {q_str(os.path.join(tmp, 'duck'))}")
    c.sql("SET autoinstall_known_extensions = false")
    c.sql("SET autoload_known_extensions = false")
    _load_extensions(c)
    c.sql(f"CREATE SECRET (TYPE s3, PROVIDER credential_chain, REGION {q_str(os.environ.get('AWS_REGION', 'us-east-1'))})")
    c.sql(f"ATTACH {q_str(ev['target']['table_bucket_arn'])} AS s3t (TYPE iceberg, ENDPOINT_TYPE s3_tables)")
    return c


# ---------------- handler ----------------

def _result():
    return {"status": "ok", "table_created": False, "table_exists": False, "backfill_pending": [], "months_loaded": [],
            "empty_deliveries": [], "months_removed": [], "new_deliveries": [], "schema_diff": None, "state_etag": None, "error": None}


def lambda_handler(event, context):
    t0 = time.perf_counter()
    result = _result()
    try:
        ev = validate(event, region=os.environ.get("AWS_REGION", "us-east-1"))
    except BadRequest as e:
        result.update(status="bad_request", error=str(e))
        _log("bad_request", error=str(e))
        return result

    s3, tables = _client("s3"), _client("s3tables")
    sb, sk = ev["state"]["bucket"], ev["state"]["key"]
    eb, ep = ev["source"]["export_bucket"], ev["source"]["export_prefix"]
    arn, ns, name = ev["target"]["table_bucket_arn"], ev["target"]["namespace"], ev["target"]["table"]
    fq = f"s3t.{q_ident(ns)}.{q_ident(name)}"
    try:
        state, etag = read_state(s3, sb, sk)
    except Exception as e:  # noqa: BLE001 - e.g. the state bucket was deleted or access was removed
        result.update(status="error", error=f"{type(e).__name__}: {str(e)[:300]}")
        _log("error", error=result["error"])
        return result
    try:
        available = {}
        for period in list_periods(s3, eb, ep):
            found = find_delivery(s3, eb, ep, period)
            if found:
                available[period] = found
        exists = table_exists(tables, arn, ns, name)
        if not exists:  # new or deleted table: its data is gone, so start over and backfill
            state.update(loaded={}, export_modes={}, backfill_done=False)
        todo = select_periods(state, available, _now(), ev["force"], ev["periods"], ev["months"])
        result["new_deliveries"] = sorted(todo)
        _log("start", table_exists=exists, available=sorted(available), todo=result["new_deliveries"],
             modes=sorted({d.export_mode for d in available.values()}))

        c = None
        if not exists and available:
            ensure_namespace(tables, arn, ns)
            c = connect(ev)
            create_table(c, fq, source_sql(available[max(available)].files))
            state["table_created_at"] = _now().isoformat(timespec="seconds")
            result["table_created"] = True
            _log("table_created", table=f"{ns}.{name}")
        result["table_exists"] = exists or result["table_created"]

        pending = list(todo)
        for period in list(todo):
            if context is not None and context.get_remaining_time_in_millis() < MIN_REMAINING_MS:
                result.update(status="partial", backfill_pending=pending)
                break
            c = c or connect(ev)
            s = time.perf_counter()
            d = todo[period]
            rows, casts = replace_month(c, fq, d.files, period)
            if _still_current(s3, eb, ep, period, d):
                state["loaded"][period] = d.token  # an empty delivery too, so the next run doesn't look again
                state.setdefault("export_modes", {})[period] = d.export_mode
            pending.remove(period)
            rec = {"period": period, "rows": rows, "delivery": d.ident, "mode": d.export_mode,
                   "seconds": round(time.perf_counter() - s, 2)}
            if rows == 0:  # no new data for this month: the rows already loaded stay
                result["empty_deliveries"].append(period)
                _log("month_empty", **rec)
            else:
                if casts:
                    rec["cast"] = casts  # columns whose type differed from the table's and were converted
                result["months_loaded"].append(rec)
                _log("month_loaded", **rec)
            etag = write_state(s3, sb, sk, state, etag)
            if etag is None:
                result.update(status="conflict", error="state.json changed during the run")
                return result
        if result["table_exists"] and result["status"] in ("ok", "partial"):
            oldest = month_window(_now(), ev["months"])[0]
            c = c or connect(ev)
            result["months_removed"] = remove_old_months(c, fq, oldest)
            for period in [p for p in state["loaded"] if p < oldest]:
                del state["loaded"][period]
            for period in [p for p in state.get("export_modes", {}) if p < oldest]:
                del state["export_modes"][period]
            if result["months_removed"]:
                _log("months_removed", months=result["months_removed"], keep_from=oldest)
        if result["status"] == "ok":
            state["backfill_done"] = True
            state.update(last_success=_now().isoformat(timespec="seconds"), last_error=None)
    except SchemaChanged as e:
        result.update(status="schema_changed", schema_diff=e.diff, error=str(e)[:500])
        state.update(last_error=result["error"], last_error_at=_now().isoformat(timespec="seconds"))
    except Exception as e:  # noqa: BLE001 - reported in the result and in state.json
        result.update(status="error", error=f"{type(e).__name__}: {str(e)[:300]}")
        state.update(last_error=result["error"], last_error_at=_now().isoformat(timespec="seconds"))
    try:
        new_etag = write_state(s3, sb, sk, state, etag)
    except Exception as e:  # noqa: BLE001 - keep months_loaded so the EC2 still rebuilds its cache
        result.update(status="error", error=f"{type(e).__name__}: {str(e)[:300]}")
        _log("error", error=result["error"])
        return result
    if new_etag is None:
        result.update(status="conflict", error="state.json changed during the run")
    result["state_etag"] = new_etag
    _log(result["status"], seconds=round(time.perf_counter() - t0, 2), error=result["error"])
    return result
