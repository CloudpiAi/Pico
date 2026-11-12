"""Never let a secret into a task's text.

The first defence is the host's: the standalone agent refuses `$` and backticks outside single quotes in its
allow-listed commands (pico_finops_agent.bash_guard), so `"$SLACK_BOT_TOKEN"` is never expanded. This module is the second:
`pico schedule` runs with the same environment, so it compares every argument (and the runner the final text) with the
secret values it holds - also a long piece of one, with case, spaces and separators ignored.
"""
import os
import re

# Name parts that make a variable a secret (fail closed: any KEY part counts), except the exact names in _NOT_SECRET.
_SECRET_PARTS = {"TOKEN", "TOKENS", "SECRET", "SECRETS", "PASSWORD", "PASS", "PASSWD", "PWD", "CREDENTIAL",
                 "CREDENTIALS", "APIKEY", "KEY", "KEYS"}
_NOT_SECRET = {"FOCUS_STATE_KEY"}  # exact names known to hold no secret (an S3 object key, a path)
_URL_PASSWORD = re.compile(r"://[^:/@\s]+:([^@\s]+)@")  # scheme://user:PASSWORD@host
MIN_LEN = 8   # shorter values would match ordinary words
WINDOW = 12   # a piece this long of a secret counts as the secret


def is_secret_name(name):
    if name.upper() in _NOT_SECRET:
        return False
    return any(p in _SECRET_PARTS for p in name.upper().split("_"))


def _norm(s):
    return re.sub(r"[^0-9a-z]", "", s.casefold())


def secret_values(environ=None):
    environ = os.environ if environ is None else environ
    vals = set()
    for k, v in environ.items():
        if not v:
            continue
        if is_secret_name(k) and len(v.strip()) >= MIN_LEN:
            vals.add(v.strip())
        vals.update(m.group(1) for m in _URL_PASSWORD.finditer(v) if len(m.group(1)) >= MIN_LEN)
    return vals


def contains_secret(text, environ=None):
    """True when `text` holds a secret's value, or a WINDOW-long piece of one (case, spaces, separators ignored)."""
    if not text:
        return False
    t = _norm(str(text))
    for v in secret_values(environ):
        n = _norm(v)
        if len(n) < MIN_LEN:
            continue
        if len(n) <= WINDOW:
            if n in t:
                return True
        elif any(n[i:i + WINDOW] in t for i in range(len(n) - WINDOW + 1)):
            return True
    return False
