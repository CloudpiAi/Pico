"""Slack access checks (PRD R3, R8): plain codes the agent turns into a plain answer; the token is never logged."""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "schedule"))
from pico_plugin_schedule import slack_api  # noqa: E402

ALL_SCOPES = "chat:write,channels:read,groups:read,im:write,users:read"


class FakeSlack:
    """Answers by method name; records calls."""

    def __init__(self, scopes=ALL_SCOPES, **answers):
        self.scopes, self.answers, self.calls = scopes, answers, []

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "auth.test":
            return {"ok": True, "user_id": "U0BOT00001"}, {"x-oauth-scopes": self.scopes}
        ans = self.answers.get(method.replace(".", "_"), {"ok": True})
        return (ans(params) if callable(ans) else ans), {}


def client(fake):
    return slack_api.Client("xoxb-test-token", transport=fake)


def test_channel_ok_when_member():
    fake = FakeSlack(conversations_info={"ok": True, "channel": {"id": "C0TEAM0001", "is_member": True}})
    assert client(fake).check_target({"type": "channel", "id": "C0TEAM0001"}) == "ok"


@pytest.mark.parametrize("answer,code", [
    ({"ok": True, "channel": {"id": "C0TEAM0001", "is_member": False}}, "not_in_channel"),
    ({"ok": False, "error": "channel_not_found"}, "channel_not_found"),
    ({"ok": False, "error": "missing_scope"}, "missing_scope"),
    ({"ok": False, "error": "ratelimited"}, "slack_error"),
])
def test_channel_problems(answer, code):
    fake = FakeSlack(conversations_info=answer)
    assert client(fake).check_target({"type": "channel", "id": "C0TEAM0001"}) == code


def test_no_chat_write_scope():
    fake = FakeSlack(scopes="channels:read,im:write")
    assert client(fake).check_target({"type": "channel", "id": "C0TEAM0001"}) == "missing_scope"


def test_dm_ok_and_cannot_dm():
    ok = FakeSlack(conversations_open={"ok": True, "channel": {"id": "D0RAVIDM01"}})
    assert client(ok).check_target({"type": "user", "id": "U0RAVI0001"}) == "ok"
    no = FakeSlack(scopes="chat:write", conversations_open={"ok": False, "error": "missing_scope"})
    assert client(no).check_target({"type": "user", "id": "U0RAVI0001"}) == "cannot_dm"


def test_no_token():
    assert slack_api.Client("", transport=FakeSlack()).check_target({"type": "user", "id": "U0RAVI0001"}) == "no_token"


def test_post_to_user_opens_dm_first():
    fake = FakeSlack(conversations_open={"ok": True, "channel": {"id": "D0RAVIDM01"}},
                     chat_postMessage={"ok": True, "ts": "1.2"})
    client(fake).post({"type": "user", "id": "U0RAVI0001"}, "hello")
    methods = [m for m, _ in fake.calls]
    assert methods == ["conversations.open", "chat.postMessage"]
    assert fake.calls[-1][1]["channel"] == "D0RAVIDM01" and fake.calls[-1][1]["text"] == "hello"


def test_post_failure_raises_with_code():
    fake = FakeSlack(chat_postMessage={"ok": False, "error": "not_in_channel"})
    with pytest.raises(slack_api.SlackError) as e:
        client(fake).post({"type": "channel", "id": "C0TEAM0001"}, "hello")
    assert e.value.code == "not_in_channel"
    assert "xoxb-test-token" not in str(e.value)


def test_user_time_zone():
    fake = FakeSlack(users_info={"ok": True, "user": {"tz": "Asia/Kolkata"}})
    assert client(fake).user_tz("U0RAVI0001") == "Asia/Kolkata"
    assert client(FakeSlack(users_info={"ok": False, "error": "missing_scope"})).user_tz("U0RAVI0001") is None


def test_resolve_channel_by_name():
    pages = iter([
        {"ok": True, "channels": [{"id": "C0AAA00001", "name": "general"}],
         "response_metadata": {"next_cursor": "n1"}},
        {"ok": True, "channels": [{"id": "C0FIN00001", "name": "finance"}], "response_metadata": {"next_cursor": ""}},
    ])
    fake = FakeSlack(conversations_list=lambda params: next(pages))
    c = client(fake)
    assert c.resolve_channel("#finance") == "C0FIN00001"
    assert c.resolve_channel("C0XYZ00001") == "C0XYZ00001"  # already an ID


def test_every_code_has_a_plain_message():
    for code in ("ok", "missing_scope", "not_in_channel", "channel_not_found", "cannot_dm", "channel_not_allowed",
                 "no_token", "slack_error"):
        assert slack_api.MESSAGES[code]



# --- Slack sends a channel as an id link, never as the name (live 2026-11-12: "to #cost-reports" arrived as
# "to <#C0123ABC>"; the model passed it on in forms the lookup didn't read -> "I can't see that channel").

def _no_list(params):
    raise AssertionError("an id-shaped value must not be looked up by name")


@pytest.mark.parametrize("value", [
    "<#C0FIN00001>", "<#C0FIN00001|finance>", "<#C0FIN00001|>", "#C0FIN00001", "C0FIN00001", " <#C0FIN00001> ",
    "'<#C0FIN00001>'",
])
def test_every_channel_link_form_resolves_to_the_id(value):
    c = client(FakeSlack(conversations_list=_no_list))
    assert c.resolve_channel(value) == "C0FIN00001"


def test_a_private_channel_link_resolves_too():
    c = client(FakeSlack(conversations_list=_no_list))
    assert c.resolve_channel("<#G0PRIV0001|secret>") == "G0PRIV0001"


def test_a_user_mention_is_not_a_channel():
    c = client(FakeSlack(conversations_list=_no_list))
    assert c.resolve_channel("<@U0SOMEONE1>") is None


def test_a_lowercase_name_that_looks_like_an_id_is_still_a_name():
    fake = FakeSlack(conversations_list=lambda params: {"ok": True, "channels": [{"id": "C0REAL00001",
                     "name": "c0fin00001"}], "response_metadata": {"next_cursor": ""}})
    assert client(fake).resolve_channel("#c0fin00001") == "C0REAL00001"


def test_the_skill_says_to_pass_the_channel_id_not_the_link():
    skill = (Path(slack_api.__file__).parent / "skills" / "schedule" / "SKILL.md").read_text(encoding="utf-8")
    assert "<#C" in skill and "--to C" in skill and "never the brackets" in skill
