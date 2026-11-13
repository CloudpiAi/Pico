"""The EC2 side of the load: keep the `pico-finops-agent-loader` Lambda current, build its payload, invoke it.

ensure_lambda:
  layer  `pico-finops-agent-duckdb`: reuse the version whose Description is "sha256:<layer zip SHA>", else publish one
  function `pico-finops-agent-loader`: create when missing (retry while a new role propagates; a create race counts as
  found), else update the code when CodeSha256 differs and the configuration when the layer, memory,
  timeout, handler or role drifted; reserved concurrency 1. No environment variables: customer details
  travel in the payload, so a .env change applies on the next run without touching the function.
Credentials: the default chain (the EC2 instance role in the container); `aws_profile` only for developers.
"""
import base64
import hashlib
import json
import os
import time

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, NoCredentialsError

FUNCTION = "pico-finops-agent-loader"
LAYER = "pico-finops-agent-duckdb"
HANDLER = "pico_finops_agent_loader.lambda_handler"
RUNTIME = "python3.12"
MEMORY_MB = 3072
TIMEOUT_S = 900
EPHEMERAL_MB = 2048
DESCRIPTION = "Pico FinOps Agent: loads the AWS FOCUS export into the S3 Table"
ROLE_RETRIES = 6
NO_CREDENTIALS_MSG = ("no AWS credentials: attach the EC2 instance role (IMDSv2 hop limit 2 for Docker), "
                      "or set the AWS_* settings in .env")
# botocore Credentials.method -> where the credentials come from (for the problem text, problems.credentials)
_SOURCE_OF_METHOD = {
    "env": "env", "explicit": "env",
    "shared-credentials-file": "profile", "config-file": "profile", "sso": "profile", "assume-role": "profile",
    "assume-role-with-web-identity": "profile", "custom-process": "profile",
    "iam-role": "instance_role", "container-role": "instance_role",
}


def credential_source(cfg, session=None):
    """env | profile | instance_role | none | unknown: where boto3 finds the credentials (never raises)."""
    try:
        creds = (session or _session)(cfg).get_credentials()
    except Exception:  # noqa: BLE001 - only used to word a problem; a lookup failure must not hide it
        return "unknown"
    if creds is None:
        return "none"
    return _SOURCE_OF_METHOD.get(getattr(creds, "method", None), "unknown")


class LoadConfigError(Exception):
    """A setting is missing or wrong: no AWS call is made, and retrying won't help."""


class NoCredentials(Exception):
    pass


def _sleep(seconds):
    time.sleep(seconds)


def sha256_b64_bytes(data):
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def _session(cfg):
    return boto3.Session(profile_name=cfg.aws_profile or None, region_name=cfg.region)


def lambda_client(cfg):
    # One sync invoke can take the whole 15 minutes; no client retries (a retry would be a second load).
    return _session(cfg).client("lambda", config=BotoConfig(read_timeout=TIMEOUT_S + 30, connect_timeout=10,
                                                            retries={"max_attempts": 0}))


def _code(e):
    return e.response.get("Error", {}).get("Code", "") if hasattr(e, "response") else ""


def is_not_found(e):
    """True for Lambda's ResourceNotFoundException (the function was deleted)."""
    return isinstance(e, ClientError) and _code(e) == "ResourceNotFoundException"


def _read_zip(cfg, name):
    path = os.path.join(cfg.zip_dir, name)
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as e:
        raise LoadConfigError(f"Lambda package {path} is missing ({e.strerror}); set zip_dir") from e


def _ensure_layer(lam, data):
    """Reuse a version only when its real content hash matches; the Description is just a hint
    (anyone allowed to publish to the layer could set it)."""
    sha = sha256_b64_bytes(data)
    tag = "sha256:" + sha
    for v in lam.list_layer_versions(LayerName=LAYER).get("LayerVersions", []):
        if v.get("Description") != tag:
            continue
        real = lam.get_layer_version(LayerName=LAYER, VersionNumber=v["Version"])["Content"]["CodeSha256"]
        if real == sha:
            return v["LayerVersionArn"], False
    arn = lam.publish_layer_version(LayerName=LAYER, Description=tag, Content={"ZipFile": data},
                                    CompatibleRuntimes=[RUNTIME], CompatibleArchitectures=["x86_64"])["LayerVersionArn"]
    return arn, True


def _get(lam):
    try:
        return lam.get_function(FunctionName=FUNCTION)["Configuration"]
    except ClientError as e:
        if _code(e) == "ResourceNotFoundException":
            return None
        raise


def _create(lam, cfg, code, layer_arn):
    for attempt in range(ROLE_RETRIES + 1):
        try:
            lam.create_function(FunctionName=FUNCTION, Runtime=RUNTIME, Architectures=["x86_64"], Handler=HANDLER,
                                Role=cfg.lambda_role_arn, Code={"ZipFile": code}, Layers=[layer_arn],
                                MemorySize=MEMORY_MB, Timeout=TIMEOUT_S, EphemeralStorage={"Size": EPHEMERAL_MB},
                                Description=DESCRIPTION)
            return True
        except ClientError as e:
            if _code(e) == "ResourceConflictException":
                return False  # created by a concurrent caller
            if _code(e) == "InvalidParameterValueException" and "assume" in str(e) and attempt < ROLE_RETRIES:
                _sleep(5 * (attempt + 1))  # a new IAM role takes a few seconds to become assumable
                continue
            raise
    return False


def ensure_lambda(cfg, lam, refresh_role=False):
    """Create or update `pico-finops-agent-loader` from the local zips. Returns {"action", "layer", "code_sha256"}.

    refresh_role saves the role again even when the ARN is unchanged: a role deleted and recreated under the
    same name is a new role, and the function keeps failing with the old one's credentials until then."""
    if not cfg.lambda_role_arn or "<MISSING:" in cfg.lambda_role_arn:
        raise LoadConfigError("lambda_role_arn is not set (FOCUS_LAMBDA_ROLE_ARN)")
    layer_zip, code = _read_zip(cfg, "duckdb-layer.zip"), _read_zip(cfg, "loader.zip")
    code_sha = sha256_b64_bytes(code)
    layer_arn, published = _ensure_layer(lam, layer_zip)
    fn = _get(lam)
    action = "unchanged"
    if fn is None:
        action = "created" if _create(lam, cfg, code, layer_arn) else "unchanged"
        lam.get_waiter("function_active_v2").wait(FunctionName=FUNCTION)
        fn = _get(lam)
    if fn["CodeSha256"] != code_sha:
        lam.update_function_code(FunctionName=FUNCTION, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
        action = "updated"
    want = {"Layers": [layer_arn], "MemorySize": MEMORY_MB, "Timeout": TIMEOUT_S, "Handler": HANDLER,
            "Role": cfg.lambda_role_arn, "Description": DESCRIPTION}
    have = {"Layers": [layer["Arn"] for layer in fn.get("Layers", [])], "MemorySize": fn.get("MemorySize"),
            "Timeout": fn.get("Timeout"), "Handler": fn.get("Handler"), "Role": fn.get("Role"),
            "Description": fn.get("Description")}
    drift = {k: v for k, v in want.items() if have.get(k) != v}
    if refresh_role and action != "created":
        drift["Role"] = cfg.lambda_role_arn
    if drift:
        lam.update_function_configuration(FunctionName=FUNCTION, **drift)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
        action = "updated" if action == "unchanged" else action
    lam.put_function_concurrency(FunctionName=FUNCTION, ReservedConcurrentExecutions=1)
    return {"action": action, "layer": layer_arn, "layer_published": published, "code_sha256": code_sha}


def build_payload(cfg, force=False, periods=None):
    """The invoke payload from the profile; refuses empty values and Pico's `<MISSING:VAR>` text."""
    fields = {"export_bucket": cfg.export_bucket, "export_prefix": cfg.export_prefix,
              "table_bucket_arn": cfg.table_bucket_arn, "namespace": cfg.namespace, "table": cfg.table,
              "state_bucket": cfg.state_bucket, "state_key": cfg.state_key, "lambda_role_arn": cfg.lambda_role_arn}
    bad = [k for k, v in fields.items() if not v or "<MISSING:" in str(v)]
    if bad:
        raise LoadConfigError(f"not set in .env: {', '.join(bad)}")
    months = str(cfg.months).strip()
    if not months.isdigit() or not 1 <= int(months) <= 120:
        raise LoadConfigError(f"FOCUS_MONTHS must be a whole number from 1 to 120, not {cfg.months!r}")
    return {"force": bool(force), "periods": periods, "months": int(months),
            "source": {"export_bucket": cfg.export_bucket, "export_prefix": cfg.export_prefix},
            "target": {"table_bucket_arn": cfg.table_bucket_arn, "namespace": cfg.namespace, "table": cfg.table},
            "state": {"bucket": cfg.state_bucket, "key": cfg.state_key}}


def invoke(lam, payload):
    """Sync invoke. A FunctionError (an unhandled exception in the Lambda) is returned as status `error`."""
    r = lam.invoke(FunctionName=FUNCTION, InvocationType="RequestResponse", Payload=json.dumps(payload).encode())
    body = r["Payload"].read()
    try:
        out = json.loads(body or b"{}")
    except ValueError:
        out = {"errorMessage": body[:300].decode(errors="replace")}
    if r.get("FunctionError") or "status" not in out:
        return {"status": "error", "months_loaded": [], "table_created": False,
                "error": f"Lambda {r.get('FunctionError', 'error')}: {str(out.get('errorMessage', out))[:300]}"}
    return out


def identity(cfg):
    try:
        return _session(cfg).client("sts").get_caller_identity()["Arn"]
    except NoCredentialsError as e:
        raise NoCredentials(NO_CREDENTIALS_MSG) from e


def remote_state(cfg):
    """state.json the Lambda writes (None when it doesn't exist yet)."""
    try:
        obj = _session(cfg).client("s3").get_object(Bucket=cfg.state_bucket, Key=cfg.state_key)
    except ClientError as e:
        if _code(e) in ("NoSuchKey", "404"):
            return None
        raise
    return json.loads(obj["Body"].read())
