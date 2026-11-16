"""A model call that stalls is given up and tried once more, instead of using up the whole Slack answer deadline.

Live 2026-11-13: a Gemini call sent no token for 177 s; the question's 180 s deadline then stopped it and the person
got "That took too long". LiteLLM has no per-call limit of its own here (its default is 6000 s), so one stuck call
spent the whole budget.

- A streamed call (the Slack path) must send its first chunk within ``AGENT_MODEL_STALL_S`` seconds (default 45,
  10-170). If it doesn't, the call is closed and made once more with the same request: nothing reached the caller
  yet, so trying again can't send an answer twice. A second stall, or a gap of the same length between two later
  chunks (part of the answer was already read, so it is not repeated), raises ``litellm.Timeout``: the run ends with
  an error, which the Slack guard handles like any failed attempt.
- A call that isn't streamed gets twice that time in all, and the same one retry.
- Seam: a wrapper around ``litellm.acompletion``, installed over llm_log's (so each try has its own ``llm_call``
  line). Every stall is logged as ``llm_stalled``.
"""

import asyncio
import os

from pico_finops_agent.log import log_event

DEFAULT_STALL_S = 45
_MARK = "_pico_finops_agent_llm_stall"


def stall_seconds(env=None):
    """AGENT_MODEL_STALL_S: whole seconds, 10-170 (below the 180 s answer deadline); anything else: the default."""
    env = os.environ if env is None else env
    try:
        value = int(env.get("AGENT_MODEL_STALL_S", ""))
    except ValueError:
        return DEFAULT_STALL_S
    return value if 10 <= value <= 170 else DEFAULT_STALL_S


def _timeout(kwargs, seconds, attempt):
    import litellm

    model = str(kwargs.get("model") or "")
    return litellm.Timeout(message=f"no answer from the model within {seconds} s (try {attempt})", model=model,
                           llm_provider=model.split("/", 1)[0] if "/" in model else "")


async def _close(stream):
    close = getattr(stream, "aclose", None)
    if close is not None:
        try:
            await close()
        except Exception:  # noqa: BLE001 - the stream is being dropped anyway
            pass


class _Resumed:
    """The stream the caller reads: the first chunk (already received), then the rest, each within the stall limit."""

    def __init__(self, stream, first, seconds, kwargs, log):
        self._stream = stream
        self._first = first
        self._seconds = seconds
        self._kwargs = kwargs
        self._log = log

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._first is not None:
            first, self._first = self._first, None
            return first
        try:
            return await asyncio.wait_for(self._stream.__anext__(), timeout=self._seconds)
        except asyncio.TimeoutError:
            self._log("llm_stalled", model=str(self._kwargs.get("model") or ""), after="a_later_chunk",
                      seconds=self._seconds, retried=False)
            await _close(self._stream)
            raise _timeout(self._kwargs, self._seconds, "mid-answer") from None

    async def aclose(self):
        await _close(self._stream)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()
        return False


def install(log=None, seconds=None, sleep=asyncio.sleep):
    """Wrap litellm.acompletion (idempotent). Install after llm_log, so this wrapper is the outer one."""
    import litellm

    log = log or (lambda event, **fields: log_event(event, **fields))
    seconds = stall_seconds() if seconds is None else seconds
    original = litellm.acompletion
    if getattr(original, _MARK, False):
        return

    async def acompletion(*args, **kwargs):
        model = str(kwargs.get("model") or "")
        for attempt in (1, 2):
            if not kwargs.get("stream"):
                try:
                    return await asyncio.wait_for(original(*args, **kwargs), timeout=2 * seconds)
                except asyncio.TimeoutError:
                    log("llm_stalled", model=model, after="request", seconds=2 * seconds, retried=attempt == 1)
                    if attempt == 2:
                        raise _timeout(kwargs, 2 * seconds, attempt) from None
                    continue
            stream = None
            try:
                async with asyncio.timeout(seconds):  # opening the stream and its first chunk, together
                    stream = await original(*args, **kwargs)
                    first = await stream.__anext__()
            except StopAsyncIteration:
                return _Resumed(_Empty(), None, seconds, kwargs, log)
            except TimeoutError:
                log("llm_stalled", model=model, after="request", seconds=seconds, retried=attempt == 1)
                if stream is not None:
                    await _close(stream)
                if attempt == 2:
                    raise _timeout(kwargs, seconds, attempt) from None
                await sleep(0)
                continue
            return _Resumed(stream, first, seconds, kwargs, log)

    setattr(acompletion, _MARK, True)
    acompletion.__wrapped__ = original
    litellm.acompletion = acompletion


class _Empty:
    """A stream that already ended (the model sent nothing at all)."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration
