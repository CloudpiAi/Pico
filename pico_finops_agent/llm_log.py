"""One `llm_call` log line per model call: why the model stopped, tokens, which tools it called.

Pico keeps a model's finish reason only on an OpenTelemetry span (pico/models/openai_compatible.py), which nothing
collects here, so an empty Slack answer was logged as just `outcome "empty"`. With this, Gemini's
`malformed_function_call` (it began a tool call and sent an empty one) is in `docker logs` for every call.

- Seam: a wrapper around ``litellm.acompletion``, installed over Pico's own patch (pico/models/sdk_patches.py). The
  OpenAI Agents SDK calls it for every Slack model request. A streamed reply is watched as the caller reads it; the
  line is written when the stream ends.
- The guard (slack_guard) opens a ``Record`` per Slack question with ``recording()``; the calls made for it, in any
  child task, add their finish reasons there and carry its id (``qid``) and attempt number.
- Never the prompt, the answer or a key in the log line. ``AGENT_SAVE_FAILED_CALLS=1`` also writes the request of an
  empty or failed call (messages + tool names, no key) to ``<data dir>/llm-failures/``, newest 50 kept, so the exact
  call can be replayed. Those files hold the customer's cost data: off by default, on the customer's own disk only.
"""
import asyncio
import contextlib
import contextvars
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from pico_finops_agent.log import describe, log_event

logger = logging.getLogger(__name__)

_MARK = "pico_finops_agent_llm_log"
_CURRENT = contextvars.ContextVar("pico_finops_agent_llm_record", default=None)
KEEP_FAILED = 50


class Record:
    """The model calls made for one Slack question."""

    def __init__(self, qid):
        self.qid = qid
        self.attempt = 1
        self.finishes = []


@contextlib.contextmanager
def recording(record):
    token = _CURRENT.set(record)
    try:
        yield record
    finally:
        _CURRENT.reset(token)


def save_dir_from_env(env=None, data_dir="/data"):
    env = os.environ if env is None else env
    return os.path.join(data_dir, "llm-failures") if env.get("AGENT_SAVE_FAILED_CALLS") == "1" else None


def _get(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class _Summary:
    def __init__(self):
        self.finish = None
        self.text_chars = 0
        self.tools = []
        self.prompt_tokens = None
        self.output_tokens = None

    def add(self, resp, streamed):
        for choice in _get(resp, "choices") or []:
            part = _get(choice, "delta" if streamed else "message")
            text = _get(part, "content")
            if isinstance(text, str):
                self.text_chars += len(text)
            for call in _get(part, "tool_calls") or []:
                name = _get(_get(call, "function"), "name")
                if name:
                    self.tools.append(name)
            finish = _get(choice, "finish_reason")
            if finish:
                self.finish = str(finish)
        usage = _get(resp, "usage")
        if usage is not None:
            for field, name in (("prompt_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
                value = _get(usage, name)
                if value is not None:  # a later chunk's empty usage never erases the counts
                    setattr(self, field, value)

    @property
    def empty(self):
        return not self.text_chars and not self.tools


class _Call:
    def __init__(self, kwargs, log, save_dir, keep, clock):
        self.kwargs = kwargs
        self.log = log
        self.save_dir = save_dir
        self.keep = keep
        self.clock = clock
        self.start = clock()
        self.record = _CURRENT.get()
        self.attempt = self.record.attempt if self.record else None
        self.summary = _Summary()
        self.done = False

    def finish(self, error=None):
        if self.done:
            return
        self.done = True
        s = self.summary
        if error is None:
            finish = s.finish or "none"
        else:
            finish = "cancelled" if isinstance(error, asyncio.CancelledError) else "exception"
        fields = {"qid": self.record.qid if self.record else None, "attempt": self.attempt,
                  "model": str(self.kwargs.get("model") or ""), "finish": finish, "empty": s.empty,
                  "seconds": round(self.clock() - self.start, 2), "prompt_tokens": s.prompt_tokens,
                  "output_tokens": s.output_tokens, "tools_called": s.tools, "text_chars": s.text_chars}
        if error is not None:
            fields["error_type"] = type(error).__name__
        if self.record is not None:
            self.record.finishes.append(finish)
        try:
            self.log("llm_call", **fields)
        except Exception:  # noqa: BLE001 - a log line never fails a model call
            logger.warning("llm_call log failed", exc_info=True)
        if self.save_dir and (error is not None or s.empty):
            self._save(finish)

    def _save(self, finish):
        try:
            os.makedirs(self.save_dir, mode=0o700, exist_ok=True)
            tools = [_get(_get(t, "function"), "name") for t in self.kwargs.get("tools") or []]
            data = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "qid": self.record.qid if self.record else None, "attempt": self.attempt,
                    "model": str(self.kwargs.get("model") or ""), "finish": finish,
                    "messages": self.kwargs.get("messages"), "tools": tools}
            safe = re.sub(r"[^a-z_]", "", finish.lower())[:40] or "unknown"  # provider text: never a path
            fd = os.open(os.path.join(self.save_dir, f"{time.time_ns()}-{safe}.json"),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # customer cost data: owner only
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, default=str)
            files = sorted(f for f in os.listdir(self.save_dir) if f.endswith(".json"))
            for old in files[:-self.keep] if len(files) > self.keep else []:
                os.remove(os.path.join(self.save_dir, old))
        except Exception as e:  # noqa: BLE001
            logger.warning("failed-call save skipped: %s", describe(e, 120))


class _WatchedStream:
    """The stream the caller reads, unchanged; each chunk also goes into the call's summary."""

    def __init__(self, inner, call):
        self._inner = inner
        self._call = call

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            item = await self._inner.__anext__()
        except StopAsyncIteration:
            self._call.finish()
            raise
        except BaseException as e:  # CancelledError too: a deadline-stopped call is the one most worth a line
            self._call.finish(error=e)
            raise
        self._call.summary.add(item, streamed=True)
        return item

    async def aclose(self):
        """A caller that stops reading early still gets its line (once)."""
        self._call.finish()
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()
        return False


def install(log=None, save_dir=None, keep=KEEP_FAILED, clock=time.monotonic):
    """Wrap litellm.acompletion (idempotent). Import pico.models first so Pico's own patch is underneath."""
    import litellm

    import pico.models  # noqa: F401 - applies Pico's SDK patches, which also replace litellm.acompletion

    log = log or (lambda event, **fields: log_event(event, **fields))
    original = litellm.acompletion
    if getattr(original, _MARK, False):
        return

    async def acompletion(*args, **kwargs):
        call = _Call(kwargs, log, save_dir, keep, clock)
        try:
            resp = await original(*args, **kwargs)
        except BaseException as e:
            call.finish(error=e)
            raise
        if kwargs.get("stream"):
            return _WatchedStream(resp, call)
        call.summary.add(resp, streamed=False)
        call.finish()
        return resp

    setattr(acompletion, _MARK, True)
    acompletion.__wrapped__ = original
    litellm.acompletion = acompletion
