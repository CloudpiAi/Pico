"""Small Slack Web API client for scheduled tasks: can the bot post there, post, DM, time zones (PRD R3, R4, R8).

Every check returns a plain code, and MESSAGES[code] is the sentence the agent says. The token is sent only in the
Authorization header and never logged or put in an error. Scopes used: chat:write (post), channels:read and
groups:read (is the bot in that channel), im:write (DMs), users:read (time zone).

Temporary trouble (network, timeout, HTTP 429 / 5xx, `ratelimited`, `internal_error`, ...) is tried again in the call,
TRIES times in all, waiting Retry-After (at most RETRY_AFTER_MAX_S); what is still failing then is `slack_error`, which
callers treat as temporary (TEMPORARY). Every other code is permanent: trying again won't fix it.

A post (chat.postMessage) is tried again only when Slack surely didn't take it (429 / ratelimited / 5xx, or the
connection never opened); a timeout or reset while talking may have posted it, so it is `slack_unsure` and never
sent again (at most once). Notices pass tries=1, so a Slack outage can't hold the caller for long.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://slack.com/api/"
TIMEOUT_S = 15
TRIES = 3
RETRY_AFTER_MAX_S = 30
_TEMPORARY_ERRORS = {"ratelimited", "internal_error", "fatal_error", "service_unavailable", "request_timeout"}
TEMPORARY = {"slack_error"}  # the code for "Slack didn't answer properly": worth trying again later
_GONE = {"user_not_found", "user_disabled", "account_inactive"}  # the person has left the workspace
_POSTS = {"chat.postMessage"}  # not safe to send twice
_sleep = time.sleep

# Slack's channel link in message text: <#C123> or <#C123|name> (the label may be empty)
_CHANNEL_LINK = re.compile(r"<#([CG][A-Z0-9]+)(?:\|[^>]*)?>")


def _is_channel_id(value):
    """A Slack channel ID (C…/G…, upper-case letters and digits); a lower-case look-alike is a name."""
    return value[:1] in ("C", "G") and value[1:].isalnum() and value.upper() == value


MESSAGES = {
    "ok": "I can post there.",
    "no_token": "Slack isn't set up for posting (SLACK_BOT_TOKEN is not set).",
    "missing_scope": "I don't have permission to post messages there. An admin needs to add the Slack scopes "
                     "chat:write, channels:read, groups:read, im:write and users:read to the app.",
    "not_in_channel": "I'm not in that channel. Invite me with `/invite @Pico FinOps Agent`, then ask again.",
    "channel_not_found": "I can't see that channel. If it's private, invite me to it first.",
    "cannot_dm": "I can't send direct messages yet. An admin needs to add the Slack scope im:write to the app.",
    "channel_not_allowed": "Tasks aren't allowed to post in that channel here.",
    "not_a_member": "You can only send tasks to channels you're in yourself.",
    "is_archived": "That channel was archived.",
    "user_gone": "The person it sends to is no longer in this Slack workspace (they have left).",
    "slack_error": "Slack didn't answer properly just now. Try again in a minute.",
    "slack_unsure": "Slack didn't confirm the post; it may have gone out.",
}


class SlackError(Exception):
    def __init__(self, code, method=""):
        super().__init__(f"Slack {method}: {code}")
        self.code = code


def _http(token):
    def transport(method, params):
        req = urllib.request.Request(API + method, data=urllib.parse.urlencode(params).encode(),
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read() or b"{}"), {k.lower(): v for k, v in r.headers.items()}

    return transport


class Client:
    def __init__(self, token, transport=None):
        self.token = token or ""
        self._t = transport or _http(self.token)
        self._scopes = None

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("SLACK_BOT_TOKEN", ""))

    def _call(self, method, tries=None, **params):
        """One API call, tried again on temporary trouble; raises SlackError("slack_error") when that lasts, or
        SlackError("slack_unsure") when a post may have gone out."""
        tries = TRIES if tries is None else max(1, tries)
        for attempt in range(tries):
            try:
                body, headers = self._t(method, params)
            except urllib.error.HTTPError as e:  # 429 / 5xx: temporary; another status: Slack refused it
                status, after = e.code, (e.headers.get("Retry-After") if e.headers else None)
                e.close()
                if status != 429 and status < 500:
                    raise SlackError("slack_error", method) from None
                wait = _retry_after(after, attempt)
            except urllib.error.URLError:  # the connection never opened: nothing was sent
                wait = _retry_after(None, attempt)
            except Exception:  # timeout / reset while talking; `from None` so no traceback can carry the token
                if method in _POSTS:
                    raise SlackError("slack_unsure", method) from None
                wait = _retry_after(None, attempt)
            else:
                body, headers = body or {}, {str(k).lower(): v for k, v in (headers or {}).items()}
                if body.get("ok") is not False or body.get("error") not in _TEMPORARY_ERRORS:
                    return body, headers
                wait = _retry_after(headers.get("retry-after"), attempt)
            if attempt < tries - 1:
                _sleep(wait)
        raise SlackError("slack_error", method)

    def scopes(self):
        if self._scopes is None:
            body, headers = self._call("auth.test")
            if not body.get("ok"):
                raise SlackError(body.get("error", "slack_error"), "auth.test")
            self._scopes = {s.strip() for s in headers.get("x-oauth-scopes", "").split(",") if s.strip()}
        return self._scopes

    def dm_channel(self, user, tries=None):
        body, _ = self._call("conversations.open", tries=tries, users=user)
        if not body.get("ok"):
            err = body.get("error", "slack_error")
            raise SlackError("cannot_dm" if err in ("missing_scope", "not_allowed_token_type")
                             else "user_gone" if err in _GONE else err, "conversations.open")
        return body["channel"]["id"]

    def check_target(self, target):
        """'ok' or the reason the bot can't post to this target."""
        if not self.token:
            return "no_token"
        try:
            scopes = self.scopes()
            if scopes and "chat:write" not in scopes:
                return "missing_scope"
            if target["type"] == "user":
                self.dm_channel(target["id"])
                return "ok"
            body, _ = self._call("conversations.info", channel=target["id"])
        except SlackError as e:
            return e.code if e.code in MESSAGES else "slack_error"
        if not body.get("ok"):
            err = body.get("error", "")
            return err if err in ("channel_not_found", "missing_scope") else "slack_error"
        if body.get("channel", {}).get("is_archived"):
            return "is_archived"
        return "ok" if body.get("channel", {}).get("is_member") else "not_in_channel"

    def post(self, target, text, tries=None):
        channel = self.dm_channel(target["id"], tries=tries) if target["type"] == "user" else target["id"]
        body, _ = self._call("chat.postMessage", tries=tries, channel=channel, text=text)
        if not body.get("ok"):
            err = body.get("error", "slack_error")
            raise SlackError("user_gone" if err in _GONE else err, "chat.postMessage")
        return body.get("ts")

    def is_member(self, user, channel):
        """True when `user` is in `channel` (conversations.members, paged). Errors count as "no" (fail closed)."""
        cursor = ""
        for _ in range(20):  # up to 20 pages of 1000 members
            try:
                body, _ = self._call("conversations.members", channel=channel, limit=1000, cursor=cursor)
            except SlackError:
                return False
            if not body.get("ok"):
                return False
            if user in body.get("members", []):
                return True
            cursor = (body.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return False
        return False

    def user_tz(self, user):
        try:
            body, _ = self._call("users.info", user=user)
        except SlackError:
            return None
        return (body.get("user") or {}).get("tz") if body.get("ok") else None

    def resolve_channel(self, name):
        """A channel ID from "#name" / "name", or from the forms Slack and the model produce for an ID: Slack sends a
        channel as `<#C123>` / `<#C123|name>` (never the name; live 2026-11-12), and the model may pass it on as
        `#C123` or quoted. An ID is returned without a lookup; a user mention (`<@U…>`) is not a channel; None when
        not found."""
        name = (name or "").strip().strip("'\"").strip()
        if name.startswith("<@"):
            return None
        link = _CHANNEL_LINK.fullmatch(name)
        if link:
            return link.group(1)
        if _is_channel_id(name.lstrip("#")):
            return name.lstrip("#")
        wanted, cursor = name.lstrip("#").lower(), ""
        for _ in range(10):  # up to 10 pages of 1000
            body, _ = self._call("conversations.list", types="public_channel,private_channel",
                                 exclude_archived="true", limit=1000, cursor=cursor)
            if not body.get("ok"):
                return None
            for ch in body.get("channels", []):
                if ch.get("name", "").lower() == wanted:
                    return ch["id"]
            cursor = (body.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return None
        return None


def _retry_after(value, attempt):
    """Seconds to wait before trying again: Slack's Retry-After when given, else 1, 2, 4 ...; never more than the cap."""
    try:
        wait = float(value)
    except (TypeError, ValueError):
        wait = 2.0 ** attempt
    return max(0.0, min(wait, RETRY_AFTER_MAX_S))
