"""The agent's own log lines: one JSON object per line on stdout (Docker keeps them; `docker logs pico-finops-agent`)."""
import json
from datetime import datetime, timezone


def log_event(event, **fields):
    rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
    print(json.dumps(rec), flush=True)


def describe(error, limit=200):
    """`Type: message` for a log line, cut to `limit` characters (never a traceback, never a secret's context)."""
    return f"{type(error).__name__}: {str(error)[:limit]}"
