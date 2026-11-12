"""Who is asking, and where: a signed line the Slack wrapper adds to every message (PRD R2, R16).

The host's Slack hook (hooks/pico_gateway.py for Pico's gateway) puts `[slack ctx=<channel>.<user>.<thread>.<message>.<sig>]` in front of each
Slack message, after removing any such line the user typed, and records that message as the newest one of its
conversation (channel + thread) in `slack-ctx-latest.json`. `pico schedule ...` takes the value as `--ctx` and
accepts it only when
  1. the HMAC matches the key in the data dir (so nobody can make one up), and
  2. it belongs to the newest message of its conversation (so a line from an earlier message in a shared thread
     can't be replayed by someone else: as soon as anyone writes after it, it stops working).
Both run as the same user on the same /data volume; the key never leaves it.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
from typing import NamedTuple

KEY_FILE = "slack-ctx.key"
LATEST_FILE = "slack-ctx-latest.json"
KEY_HEX = 64
SIG_LEN = 32
MAX_CONVERSATIONS = 2000
TAG_RE = re.compile(r"\[slack ctx=(?P<ctx>[^\]\s]+)\]")
_ANY_TAG_RE = re.compile(r"\[slack\b[^\]]*\]", re.IGNORECASE)
_CTX_RE = re.compile(r"^(?P<channel>[CDG][A-Z0-9]{2,})\.(?P<user>[UW][A-Z0-9]{2,})\.(?P<thread>[0-9_]+)\."
                     r"(?P<msg>[0-9_]+)\.(?P<sig>[0-9a-f]{%d})$" % SIG_LEN)


class BadContext(ValueError):
    """The ctx value is missing, malformed, not signed by this agent, or the key is unusable."""


class Ctx(NamedTuple):
    channel: str
    user: str
    thread: str  # "" outside a thread
    msg: str


def _check_key(key):
    if not re.fullmatch(r"[0-9a-f]{%d}" % KEY_HEX, key or ""):
        raise BadContext(f"{KEY_FILE} is damaged; delete it and restart the agent")
    return key


def load_or_create_key(data_dir):
    """The signing key (hex), created once with mode 0600. Written to a temp file first and linked into place, so
    a reader never sees a half-written (or empty) key."""
    path = os.path.join(data_dir, KEY_FILE)
    if not os.path.exists(path):
        tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(secrets.token_hex(KEY_HEX // 2))
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)  # fails if another process won the race; then use theirs
        except FileExistsError:
            pass
        finally:
            os.unlink(tmp)
    with open(path, encoding="utf-8") as fh:
        return _check_key(fh.read().strip())


def _ts(value):
    return (value or "0").replace(".", "_")


def _unts(value):
    return "" if value == "0" else value.replace("_", ".")


def _sig(key, channel, user, thread, msg):
    body = f"{channel}.{user}.{_ts(thread)}.{_ts(msg)}"
    return hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()[:SIG_LEN]


def make_tag(key, channel, user, thread, msg):
    return f"[slack ctx={channel}.{user}.{_ts(thread)}.{_ts(msg)}.{_sig(key, channel, user, thread, msg)}]"


def strip_tags(text):
    """Remove every `[slack ...]` the user typed (repeated, so nested tricks don't rebuild one)."""
    text = text or ""
    while True:
        cleaned = _ANY_TAG_RE.sub("", text)
        if cleaned == text:
            return cleaned.strip()
        text = cleaned


def with_context(key, channel, user, text, thread, msg):
    return f"{make_tag(key, channel, user, thread, msg)}\n{strip_tags(text)}"


def verify(key, token):
    """The Ctx from a signed value, or BadContext. Check `is_current` too before acting on it."""
    m = _CTX_RE.match(token or "")
    if not m:
        raise BadContext("the Slack context is missing or not valid")
    ctx = Ctx(m.group("channel"), m.group("user"), _unts(m.group("thread")), _unts(m.group("msg")))
    if not hmac.compare_digest(m.group("sig"), _sig(key, *ctx)):
        raise BadContext("the Slack context is missing or not valid")
    return ctx


def _conv(channel, thread):
    return f"{channel}:{thread or ''}"


def _read_latest(data_dir):
    try:
        with open(os.path.join(data_dir, LATEST_FILE), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def remember_latest(data_dir, channel, thread, msg, user):
    """Called by the gateway for every message, before the agent sees it."""
    latest = _read_latest(data_dir)
    latest.pop(_conv(channel, thread), None)
    latest[_conv(channel, thread)] = {"msg": msg, "user": user}
    if len(latest) > MAX_CONVERSATIONS:  # keep the most recent conversations (insertion order)
        latest = dict(list(latest.items())[-MAX_CONVERSATIONS:])
    path = os.path.join(data_dir, LATEST_FILE)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(latest, fh)
    os.replace(tmp, path)


def is_current(data_dir, ctx):
    """True only for the newest message of its conversation."""
    entry = _read_latest(data_dir).get(_conv(ctx.channel, ctx.thread)) or {}
    return entry.get("msg") == ctx.msg and entry.get("user") == ctx.user
