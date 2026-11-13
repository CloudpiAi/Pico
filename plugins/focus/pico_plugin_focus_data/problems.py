"""Turn a failed load into one plain problem: what broke, what to do, and whether a retry can help.

Saved as state.json `problem` by the load; the scheduler posts it in Slack once per problem.
A permanent problem (a missing permission, credentials, settings, a schema change) needs a person, so the
load exits 2 and is not retried; anything else exits 1 and is retried once.
"""
import re

EC2_POLICY = "pico_finops_agent/iam/ec2-role-policy.json"
LOADER_POLICY = "pico_finops_agent/iam/loader-role-policy.json"
_ACTION_RE = re.compile(r"perform: ([a-z0-9-]+:[A-Za-z0-9*]+)")
_DENIED = ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnauthorizedException")
_EXPIRED = ("ExpiredToken", "ExpiredTokenException")
_INVALID = ("UnrecognizedClientException", "InvalidClientTokenId")
_CREDENTIALS = _EXPIRED + _INVALID


def _make(code, summary, action, permanent, detail="", aws_action=None):
    return {"code": code, "summary": summary, "action": action, "permanent": permanent,
            "aws_action": aws_action, "detail": str(detail)[:300]}


def _code_of(e):
    return (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "")


def _action_in(text):
    m = _ACTION_RE.search(text or "")
    return m.group(1) if m else None


def from_exception(e, stage="aws", source_of=None):
    """An exception raised on the EC2 side (ensure / invoke / cache rebuild). `source_of()` tells where the
    credentials come from (see lambda_ops.credential_source); it is only called for a credentials problem."""
    code, text = _code_of(e), str(e)
    if stage == "cache" and "Secret Validation Failure" in text:
        return credentials(text, cause="missing", source=_source(source_of))  # DuckDB found no usable credentials
    if code in _DENIED or "not authorized to perform" in text or (stage == "cache" and "Forbidden_403" in text):
        act = _action_in(text)
        what = act or ("read the S3 table for the local copy" if stage == "cache" else "call AWS")
        return _make("ec2_permission", f"the EC2 role isn't allowed to {what}", f"Add it from {EC2_POLICY}.",
                     True, text, act)
    if code in _CREDENTIALS:
        return credentials(text, cause="expired" if code in _EXPIRED else "invalid", source=_source(source_of),
                           aws_code=code)
    if code == "InvalidParameterValueException" and "assume" in text:
        return _make("lambda_role_missing", "the loader Lambda's role is missing or can't be used",
                     f"Recreate pico-finops-agent-loader-role from {LOADER_POLICY} (and the trust policy).", True, text)
    if code in ("TooManyRequestsException", "ThrottlingException"):
        return _make("timeout", "the loader Lambda was busy", "It retries automatically.", False, text)
    if stage == "cache":
        return _make("cache_rebuild", "the local copy of the data couldn't be rebuilt",
                     "Check disk space and the logs on the EC2.", False, text)
    return _make("unknown", "an unexpected error stopped the load", "Check the logs: docker compose logs api.",
                 False, f"{type(e).__name__}: {text}")


def from_lambda_result(status, error):
    """A loader Lambda result that isn't ok."""
    text = str(error or "")
    if status == "schema_changed":
        # a changed column type is cast to the table's type; only a new column, or a value that can't be
        # converted to the table's type, gets here
        return _make("schema_changed", "AWS added a column to the export, or sent a value the table can't hold",
                     "Recreate the table so it picks up the new columns or types (see the setup guide).", True, text)
    if status == "bad_request":
        return config(text)
    if status == "conflict":
        return _make("conflict", "two loads overlapped", "It retries automatically.", False, text)
    if "NoSuchBucket" in text:
        return _make("state_bucket_missing", "the state bucket doesn't exist",
                     "Create it, or fix FOCUS_STATE_BUCKET in .env.", True, text)
    if "InvalidAccessKeyId" in text or "UnrecognizedClientException" in text:
        # the Lambda's own credentials stopped working: its role was deleted while the function still runs
        return _make("lambda_role_missing", "the loader Lambda's role is missing or can't be used",
                     f"Recreate pico-finops-agent-loader-role from {LOADER_POLICY} (and the trust policy).", True, text)
    if any(d in text for d in _DENIED) or "not authorized to perform" in text:
        act = _action_in(text)
        return _make("lambda_permission", f"the loader Lambda's role isn't allowed to {act or 'reach the data'}",
                     f"Add it from {LOADER_POLICY}.", True, text, act)
    if "Task timed out" in text or "TooManyRequests" in text:
        return _make("timeout", "the loader Lambda timed out or was busy",
                     "It retries automatically; if it keeps happening, check its CloudWatch logs.", False, text)
    if "assume" in text and "role" in text.lower():
        return _make("lambda_role_missing", "the loader Lambda's role is missing or can't be used",
                     f"Recreate pico-finops-agent-loader-role from {LOADER_POLICY} (and the trust policy).", True, text)
    return _make("unknown", "the loader Lambda reported an error", "Check its CloudWatch logs.", False, text)


def config(detail):
    return _make("config", "a setting is missing or wrong", "Fix it in .env and restart.", True, detail)


_RESTART = "then restart the agent"
_ROLE_HINT = "attach the EC2 instance role (IMDSv2 hop limit 1 with host networking)"
_ENV_KEYS = "AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN in .env"
# (cause, source) -> (summary, action). Seen 2026-11-12: an expired .env session token on a non-EC2 host was reported
# as "the EC2 has no working AWS credentials. Attach the instance role…", which sent the reader the wrong way.
_CREDENTIAL_TEXT = {
    ("expired", "env"): ("the AWS credentials have expired",
                         f"Renew the temporary AWS credentials ({_ENV_KEYS}), {_RESTART}."),
    ("expired", "profile"): ("the AWS credentials have expired",
                             f"Sign in again for the AWS profile (for SSO: aws sso login), {_RESTART}."),
    ("expired", "instance_role"): ("the EC2 instance role's credentials have expired",
                                   "They normally renew by themselves: check the instance role is still attached "
                                   "(IMDSv2 hop limit 1 with host networking)."),
    ("expired", None): ("the AWS credentials have expired",
                        f"Renew them (the AWS_* settings in .env or the AWS profile) or {_ROLE_HINT}, {_RESTART}."),
    ("invalid", "env"): ("the AWS access key isn't valid",
                         f"Fix AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in .env, {_RESTART}."),
    ("invalid", "profile"): ("the AWS access key isn't valid", f"Fix the AWS profile's keys, {_RESTART}."),
    ("invalid", None): ("the AWS access key isn't valid",
                        f"Fix the AWS keys (the AWS_* settings in .env or the AWS profile) or {_ROLE_HINT}, {_RESTART}."),
    ("missing", None): ("no AWS credentials were found",
                        f"{_ROLE_HINT[0].upper()}{_ROLE_HINT[1:]}, or set the AWS_* settings in .env, {_RESTART}."),
}


def _source(source_of):
    """Where the credentials come from: env | profile | instance_role | none | unknown (never raises)."""
    if source_of is None:
        return "unknown"
    try:
        return source_of() or "unknown"
    except Exception:  # noqa: BLE001 - a failed lookup must not hide the real problem
        return "unknown"


def credentials(detail="", cause="missing", source="unknown", aws_code=None):
    """A credentials problem worded for its cause and for where the credentials come from."""
    summary, action = _CREDENTIAL_TEXT.get((cause, source)) or _CREDENTIAL_TEXT[(cause, None)]
    p = _make("credentials", summary, action, True, detail)
    p.update(cause=cause, source=source, aws_code=aws_code)
    return p
