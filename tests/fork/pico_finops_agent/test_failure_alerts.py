"""Failure handling: classify what broke, tell the channel once per problem, say when it recovers,
warn when the data gets old. (User decisions 2026-11-05: same notify channel, once per problem + recovery,
stale after 3 days.)"""
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("boto3", reason="Needs the pico-finops-agent extra")
pytest.importorskip("apscheduler", reason="Needs the pico-finops-agent extra")

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "focus"))
from pico_finops_agent import scheduler as agent_api  # noqa: E402
from pico_plugin_focus_data import problems  # noqa: E402


class _ClientError(Exception):
    def __init__(self, code, msg=""):
        super().__init__(f"An error occurred ({code}) when calling the X operation: {msg}")
        self.response = {"Error": {"Code": code, "Message": msg}}


# ---------- classification ----------

def test_ec2_permission_names_the_action():
    e = _ClientError("AccessDeniedException", "User: arn:aws:sts::1:assumed-role/ec2/i-1 is not authorized to "
                                              "perform: lambda:InvokeFunction on resource: arn:aws:lambda:...")
    p = problems.from_exception(e)
    assert p["code"] == "ec2_permission" and p["aws_action"] == "lambda:InvokeFunction" and p["permanent"]
    assert "ec2-role-policy.json" in p["action"]


@pytest.mark.parametrize("error,code,permanent", [
    ("ClientError: An error occurred (AccessDenied) when calling the ListObjectsV2 operation: User: x is not "
     "authorized to perform: s3:ListBucket on resource", "lambda_permission", True),
    ("ClientError: An error occurred (NoSuchBucket) when calling the GetObject operation: The specified bucket "
     "does not exist", "state_bucket_missing", True),
    # the Lambda role was deleted while the function still exists (seen live 2026-11-05)
    ("ClientError: An error occurred (InvalidAccessKeyId) when calling the GetObject operation: The AWS Access Key "
     "Id you provided does not exist in our records.", "lambda_role_missing", True),
    ("ClientError: An error occurred (UnrecognizedClientException) when calling the GetTable operation: The "
     "security token included in the request is invalid.", "lambda_role_missing", True),
    ("Lambda Unhandled: Task timed out after 900.00 seconds", "timeout", False),
    ("ValueError: something odd", "unknown", False),
])
def test_lambda_result_errors(error, code, permanent):
    p = problems.from_lambda_result("error", error)
    assert (p["code"], p["permanent"]) == (code, permanent)


def test_other_statuses():
    assert problems.from_lambda_result("schema_changed", "x")["code"] == "schema_changed"
    assert problems.from_lambda_result("conflict", "x")["permanent"] is False
    assert problems.from_exception(_ClientError("TooManyRequestsException"))["code"] == "timeout"
    assert problems.from_exception(_ClientError("ExpiredTokenException"))["code"] == "credentials"
    assert problems.from_exception(OSError("No space left on device"), stage="cache")["code"] == "cache_rebuild"


def test_cache_forbidden_is_an_ec2_permission():
    """DuckDB reports a denied S3 Tables read as a plain 403 (seen live 2026-11-05)."""
    e = RuntimeError("Request to 's3tables.us-east-1.amazonaws.com/iceberg/v1/config?warehouse=arn%3Aaws' returned a "
                     "non-200 status code (Forbidden_403), with reason: Forbidden")
    p = problems.from_exception(e, stage="cache")
    assert p["code"] == "ec2_permission" and p["permanent"]
    assert "read the S3 table for the local copy" in p["summary"] and "ec2-role-policy.json" in p["action"]


def test_cache_without_credentials_is_a_credentials_problem():
    e = RuntimeError("Secret Validation Failure: during `create` using the following:\nCredential Chain: 'config'")
    assert problems.from_exception(e, stage="cache")["code"] == "credentials"


def test_role_missing_on_invoke():
    e = _ClientError("InvalidParameterValueException", "The role defined for the function cannot be assumed by Lambda.")
    assert problems.from_exception(e)["code"] == "lambda_role_missing"


# ---------- scheduler alerts ----------

@pytest.fixture
def chan(monkeypatch, tmp_path):
    monkeypatch.setenv("FOCUS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SLACK_NOTIFY_CHANNEL", "pico-finops-agent")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("FOCUS_ALERT_STALE_DAYS", raising=False)
    monkeypatch.setattr(agent_api, "_LAST_ANNOUNCED", None)
    posts = []
    monkeypatch.setattr(agent_api, "_post_slack", lambda token, channel, text: posts.append(text))

    def state(**kw):
        (tmp_path / "state.json").write_text(json.dumps({"last_result": {"months_loaded": []}, **kw}))

    return posts, state


EC2 = {"code": "ec2_permission", "summary": "the EC2 role isn't allowed to lambda:InvokeFunction",
       "action": "Add it from pico_finops_agent/iam/ec2-role-policy.json.", "permanent": True}


def test_alert_once_per_problem_then_recovery(chan, monkeypatch):
    posts, state = chan
    monkeypatch.setattr(agent_api, "_today", lambda: __import__("datetime").date(2026, 11, 5))
    state(problem=EC2, data_through="2026-11-04")
    agent_api._notify_after_load(2)
    agent_api._notify_after_load(2)  # same problem: not again
    assert len(posts) == 1
    assert ":warning:" in posts[0] and "lambda:InvokeFunction" in posts[0] and "ec2-role-policy.json" in posts[0]
    assert "data through Wed 4 Nov" in posts[0]
    state(problem=None, data_through="2026-11-04")
    agent_api._notify_after_load(0)
    assert len(posts) == 2 and "refreshing again" in posts[1]
    agent_api._notify_after_load(0)
    assert len(posts) == 2  # recovery only once


def test_transient_failure_waits_for_the_retry(chan):
    posts, state = chan
    state(problem={**EC2, "code": "timeout", "permanent": False}, data_through="2026-11-04")
    agent_api._notify_after_load(1, final=False)
    assert posts == []
    agent_api._notify_after_load(1, final=True)
    assert len(posts) == 1


def test_stale_data_alert_once(chan, monkeypatch):
    posts, state = chan
    monkeypatch.setattr(agent_api, "_today", lambda: __import__("datetime").date(2026, 11, 9))
    state(problem=None, data_through="2026-11-04")
    agent_api._notify_after_load(0)
    agent_api._notify_after_load(0)
    assert len(posts) == 1 and "5 days" in posts[0] and "Data Exports" in posts[0]
    monkeypatch.setenv("FOCUS_ALERT_STALE_DAYS", "7")
    state(problem=None, data_through="2026-11-04")
    posts.clear()
    agent_api._notify_after_load(0)
    assert posts == []


def test_retry_run_is_final(monkeypatch, chan):
    posts, state = chan
    state(problem={**EC2, "code": "timeout", "permanent": False}, data_through="2026-11-04")
    monkeypatch.setattr(agent_api.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1})())
    from apscheduler.schedulers.background import BackgroundScheduler

    monkeypatch.setattr(agent_api, "_SCHEDULER", BackgroundScheduler())  # so a retry can be scheduled
    agent_api._run_job(retry=True)
    assert posts == []          # a retry is coming
    agent_api._run_job(retry=False)
    assert len(posts) == 1      # the retry failed too


# ---------- notices for loads started from Slack (user decision 2026-11-05) ----------

def _loaded(tmp_dir, at, months=("2026-10",)):
    (Path(tmp_dir) / "state.json").write_text(json.dumps(
        {"last_result": {"status": "ok", "months_loaded": list(months), "at": at}, "problem": None}))


def test_slack_started_load_is_announced_once_by_the_watch(chan, tmp_path):
    posts, _ = chan
    _loaded(tmp_path, "2026-11-05T10:57:06+00:00")
    agent_api.announce_new_data()
    agent_api.announce_new_data()
    assert len(posts) == 1 and "Fresh cost data" in posts[0]
    _loaded(tmp_path, "2026-11-05T11:30:00+00:00")  # a later load
    agent_api.announce_new_data()
    assert len(posts) == 2


def test_scheduled_load_is_not_announced_twice(chan, tmp_path):
    posts, _ = chan
    _loaded(tmp_path, "2026-11-05T18:00:40+00:00")
    agent_api._notify_after_load(0)
    agent_api.announce_new_data()
    assert len(posts) == 1


def test_load_without_new_months_is_not_announced(chan, tmp_path):
    posts, _ = chan
    _loaded(tmp_path, "2026-11-05T10:57:06+00:00", months=())
    agent_api.announce_new_data()
    assert posts == []


def test_the_last_load_before_an_upgrade_is_not_posted_again(chan, tmp_path):
    posts, _ = chan
    _loaded(tmp_path, "2026-11-05T06:00:40+00:00")  # alerts.json has no "announced" yet
    agent_api.mark_current_load_announced()
    agent_api.announce_new_data()
    assert posts == []


def test_scheduler_has_the_watch_job(monkeypatch):
    sched = agent_api.build_scheduler()
    assert {j.id for j in sched.get_jobs()} == {agent_api.JOB_ID, agent_api.WATCH_JOB_ID, "tasks_sync"}  # + scheduled Slack tasks


def test_slack_load_after_a_failure_posts_the_recovery_too(chan, tmp_path):
    posts, _ = chan
    (tmp_path / "alerts.json").write_text(json.dumps({"failure": "ec2_permission", "stale": False}))
    _loaded(tmp_path, "2026-11-05T12:00:00+00:00")
    agent_api.announce_new_data()
    assert len(posts) == 2 and "refreshing again" in posts[0] and "Fresh cost data" in posts[1]
    agent_api.announce_new_data()
    assert len(posts) == 2


def test_notice_is_not_repeated_when_alerts_json_cannot_be_saved(chan, tmp_path, monkeypatch):
    posts, _ = chan
    _loaded(tmp_path, "2026-11-05T12:00:00+00:00")

    def cannot_save(alerts):
        raise OSError("read-only file system")

    monkeypatch.setattr(agent_api, "_write_alerts", cannot_save)
    agent_api.announce_new_data()
    agent_api.announce_new_data()
    assert len(posts) == 1


def test_recovery_and_notice_never_overlap(chan, tmp_path):
    """The whole after-load step holds the lock, so the watch can't interleave with the load job."""
    posts, _ = chan
    _loaded(tmp_path, "2026-11-05T18:00:40+00:00")
    assert agent_api._ANNOUNCE_LOCK.acquire(blocking=False)
    try:
        import threading
        t = threading.Thread(target=agent_api.announce_new_data)
        t.start()
        t.join(0.3)
        assert t.is_alive() and posts == []  # waits for the lock
    finally:
        agent_api._ANNOUNCE_LOCK.release()
    t.join(2)
    assert len(posts) == 1


# --- the credentials alert names the real cause (2026-11-12: an expired .env session token on a test server was
# reported as "the EC2 has no working AWS credentials. Attach the instance role (IMDSv2 hop limit 2 for Docker).")


def test_an_expired_token_from_env_says_expired_and_how_to_renew():
    p = problems.from_exception(_ClientError("ExpiredTokenException", "The security token included in the request "
                                             "is expired"), source_of=lambda: "env")
    assert (p["code"], p["cause"], p["source"], p["aws_code"]) == ("credentials", "expired", "env",
                                                                    "ExpiredTokenException")
    assert p["summary"] == "the AWS credentials have expired"
    assert "AWS_SESSION_TOKEN" in p["action"] and ".env" in p["action"]
    assert "EC2" not in p["summary"] + p["action"] and "hop limit" not in p["action"]
    assert p["permanent"] is True


def test_an_expired_profile_says_sign_in_again():
    p = problems.from_exception(_ClientError("ExpiredToken"), source_of=lambda: "profile")
    assert p["cause"] == "expired" and "aws sso login" in p["action"] and "hop limit" not in p["action"]


def test_expired_instance_role_credentials_point_at_the_role():
    p = problems.from_exception(_ClientError("ExpiredTokenException"), source_of=lambda: "instance_role")
    assert "instance role" in p["summary"] + p["action"] and "hop limit 2" in p["action"]


def test_an_invalid_key_from_env_says_fix_the_key():
    p = problems.from_exception(_ClientError("InvalidClientTokenId"), source_of=lambda: "env")
    assert (p["cause"], p["aws_code"]) == ("invalid", "InvalidClientTokenId")
    assert p["summary"] == "the AWS access key isn't valid"
    assert "AWS_ACCESS_KEY_ID" in p["action"] and "hop limit" not in p["action"]


def test_no_credentials_at_all_offers_both_ways():
    e = RuntimeError("Secret Validation Failure: during `create` using the following:\nCredential Chain: 'config'")
    p = problems.from_exception(e, stage="cache", source_of=lambda: "none")
    assert (p["code"], p["cause"]) == ("credentials", "missing")
    assert p["summary"] == "no AWS credentials were found"
    assert "instance role" in p["action"] and ".env" in p["action"]


def test_an_unknown_source_gives_the_generic_hint():
    p = problems.from_exception(_ClientError("ExpiredTokenException"))
    assert p["source"] == "unknown" and ".env" in p["action"] and "instance role" in p["action"]


def test_the_source_is_only_looked_up_for_a_credentials_problem():
    def boom():
        raise AssertionError("looked up for a non-credentials problem")
    assert problems.from_exception(_ClientError("TooManyRequestsException"), source_of=boom)["code"] == "timeout"


def test_a_failing_source_lookup_does_not_break_the_classification():
    def broken():
        raise RuntimeError("no metadata")
    p = problems.from_exception(_ClientError("ExpiredTokenException"), source_of=broken)
    assert p["cause"] == "expired" and p["source"] == "unknown"


def test_the_slack_alert_shows_the_cause_and_the_aws_code():
    p = problems.from_exception(_ClientError("ExpiredTokenException"), source_of=lambda: "env")
    text = agent_api.failure_text(p, "2026-11-09")
    assert "the AWS credentials have expired (ExpiredTokenException)." in text
    assert "EC2" not in text and "hop limit" not in text and "data through Mon 9 Nov" in text


def test_problems_without_an_aws_code_keep_their_text():
    p = problems.config("FOCUS_REGION is not set")
    assert agent_api.failure_text(p, None) == f":warning: I couldn't refresh the cost data: {p['summary']}. {p['action']}"
