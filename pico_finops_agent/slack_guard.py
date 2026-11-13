"""Slack answer hardening for the standalone agent.

One Slack DM is one Pico session that grows forever; past ~125k input tokens Gemini often returns nothing and the
bridge posts "(No response generated)". This module, installed by pico_finops_agent.agent when Slack is on:

- History cap: before each model call of a Slack (``gateway_...``) session, the model sees only the items from the
  Nth-last user message on (system items kept, a tool call never split from its output). The session on disk keeps
  everything; only the per-call model input is trimmed. Seam: an extra ``call_model_input_filter``, chained after
  Pico's own one, in ``LLMBaseModel._build_run_config``.
- Answer guard around ``ChannelBridge.handle_message`` (outermost wrapper, so it sees the text the person typed):
  an empty answer is run again with a new message id (the bridge dedups by id) and a lighter history: attempt 2
  sees earlier turns as text only (questions and answers, no tool calls), attempt 3 only the question; then SORRY.
  Replays of Gemini's MALFORMED_FUNCTION_CALL inputs: same input failed 29/30, text-only 3/30, question-only 0/30.
  An attempt that
  ends unanswered (empty or error, once its run has ended; stopped at the deadline, only when the stop is confirmed)
  is taken back out of the session with Pico's own ``SessionManager.checkpoint_turn``/``rollback_turn``, so no later
  question carries it (or carries it out). Never an attempt that may have changed something: a tool result seen, a
  tool call the deadline cut off, or stats that can't be read. That attempt stays in the session and every later
  attempt keeps the full history, so the model sees what was already done instead of doing it again. A raw
  "❌ Error: ..." text becomes SORRY; a question running past the deadline is stopped with TOO_LONG; more than
  AGENT_QUESTIONS_PER_HOUR per person gets SLOW_DOWN; a whole-message reset phrase clears that DM's/thread's session.
- No stand-in answer: when the model ends a Slack turn with no text, Pico would post the last tool's one-line summary
  ("2×2 rows"); for ``gateway_`` sessions that fallback is skipped, so the turn counts as empty and is retried.
  Seam: ``ChatAgenticNode._build_success_result``.
- One ``slack_question`` log line per message: outcome, seconds, tool calls, tokens, history turns sent, and the
  ``qid`` + ``finish_reasons`` of its model calls (each also logged as ``llm_call`` by pico_finops_agent.llm_log). Never
  the question or the answer text.

No change to pico/: every internal used is pinned by tests/fork/pico_finops_agent/test_slack_guard_pins.py.
"""
import asyncio
import contextvars
import dataclasses
import inspect
import logging
import os
import re
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass

from pico_finops_agent import llm_log
from pico_finops_agent.log import describe, log_event

logger = logging.getLogger(__name__)

SORRY = "Sorry, I couldn't answer that just now. Please ask again in a moment."
TOO_LONG = "That took too long to work out. Please try a narrower question, for example one service or one month."
SLOW_DOWN = "You've asked a lot of questions in the last hour. Please give it a few minutes and ask again."
RESET_DONE = "Started a new conversation."
RESET_WHILE_BUSY = "I'm still working on your last question. Try again when it's done."
RESET_NOT_YOURS = "Only the person who started this thread can start it over."

FALLBACK_TEXT = "(No response generated)"  # pico/gateway/bridge.py, when nothing was sent
ERROR_PREFIX = "❌ Error:"
ERROR_GENERIC = "❌ An error occurred."
BUSY_TEXT = "The previous request is still being processed. Please wait."

RESET_PHRASES = frozenset({"new conversation", "reset", "start over", "clear", "/new", "/reset", "/clear"})
OUTCOMES = frozenset({"answered", "retried", "empty", "error", "too_long", "rate_limited", "reset", "reset_refused",
                      "busy"})
SESSION_PREFIX = "gateway_"  # ChannelBridge.build_session_id

_MAX_REMEMBERED = 1000
# The history an attempt's model calls see: the guard sets it around each attempt; its tasks inherit it.
HISTORY_MODES = ("full", "text", "none")  # attempt 1, 2, 3
_HISTORY_MODE = contextvars.ContextVar("pico_finops_agent_history_mode", default="full")
_TURNS_SENT: "OrderedDict[str, tuple]" = OrderedDict()  # session id -> (user turns sent, monotonic time)


def _now():
    return time.monotonic()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    history_turns: int = 8
    deadline_s: float = 180
    questions_per_hour: int = 30


def _int_setting(env, name, default, lo, hi):
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return value if lo <= value <= hi else default


def settings_from_env(env=None):
    """AGENT_HISTORY_TURNS (8, 1-50), AGENT_ANSWER_DEADLINE_S (180, 30-900), AGENT_QUESTIONS_PER_HOUR (30, 0 = off).
    entrypoint.sh refuses bad values at start; here a bad value falls back to the default."""
    env = os.environ if env is None else env
    return Settings(history_turns=_int_setting(env, "AGENT_HISTORY_TURNS", 8, 1, 50),
                    deadline_s=_int_setting(env, "AGENT_ANSWER_DEADLINE_S", 180, 30, 900),
                    questions_per_hour=_int_setting(env, "AGENT_QUESTIONS_PER_HOUR", 30, 0, 100000))


# ---------------------------------------------------------------------------
# History cap
# ---------------------------------------------------------------------------

def _field(item, name):
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _text_of(item):
    content = _field(item, "content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            text = _field(part, "text")
            if isinstance(text, str):
                return text
    return None


def _is_user(item):
    """A person's message. Pico's in-turn compaction resume item is a user item too, but not a turn."""
    if _field(item, "role") != "user" or _field(item, "type") not in (None, "message"):
        return False
    from pico.utils.message_utils import is_compact_resume_text

    return not is_compact_resume_text(_text_of(item))


def _is_system(item):
    return _field(item, "role") in ("system", "developer")


def count_user_turns(items):
    return sum(1 for it in items if _is_user(it)) if isinstance(items, list) else 0


def _turn_cut(items, n):
    """Index where the last ``n`` user turns start (moved back so a tool call keeps its output); None: no cut."""
    if not isinstance(items, list) or isinstance(n, bool) or not isinstance(n, int) or n <= 0:
        return None
    users = [i for i, it in enumerate(items) if _is_user(it)]
    if len(users) < n or not users:
        return None
    cut = users[-n]
    first_call = {}
    for i, it in enumerate(items):
        cid = _field(it, "call_id")
        if _field(it, "type") == "function_call" and cid is not None:
            first_call.setdefault(cid, i)
    moved = True
    while moved:
        moved = False
        for it in items[cut:]:
            if _field(it, "type") == "function_call_output":
                j = first_call.get(_field(it, "call_id"))
                if j is not None and j < cut:
                    if j > 0 and _field(items[j - 1], "type") == "reasoning":
                        j -= 1  # the reasoning item that produced the call stays with it
                    cut, moved = j, True
    return cut or None


def trim_to_last_turns(items, n):
    """Items from the Nth-last user message on, plus any earlier system/developer items. A tool call is never split
    from its output (the cut moves back to the call). Returns ``items`` itself when nothing is cut, n <= 0, or the
    input isn't a list with user messages. Never mutates ``items``."""
    if not isinstance(items, list) or isinstance(n, bool) or not isinstance(n, int) or n <= 0:
        return items
    if sum(1 for it in items if _is_user(it)) <= n:
        return items
    cut = _turn_cut(items, n)
    if cut is None:
        return items
    return [it for it in items[:cut] if _is_system(it)] + items[cut:]


def current_turn_only(items):
    """System/developer items and the question being answered (with this run's own tool calls)."""
    return trim_to_last_turns(items, 1)


def text_only_history(items):
    """Earlier turns as text only: system/developer and user messages, assistant answers with text; no tool calls,
    tool outputs or reasoning items. The question being answered stays whole. ``items`` itself when nothing to do."""
    cut = _turn_cut(items, 1)
    if cut is None:
        return items
    kept = []
    for it in items[:cut]:
        if _field(it, "type") not in (None, "message"):
            continue
        role = _field(it, "role")
        if role in ("system", "developer", "user") or (role == "assistant" and (_text_of(it) or "").strip()):
            kept.append(it)
    return kept + items[cut:]


_LIGHTER = {"full": lambda items: items, "text": text_only_history, "none": current_turn_only}
if set(_LIGHTER) != set(HISTORY_MODES):  # a renamed mode must never fall back to the full history unnoticed
    raise RuntimeError(f"history modes {HISTORY_MODES} and their filters {sorted(_LIGHTER)} differ")


def _remember_turns(session_id, count):
    _TURNS_SENT.pop(session_id, None)
    _TURNS_SENT[session_id] = (count, _now())
    while len(_TURNS_SENT) > _MAX_REMEMBERED:
        _TURNS_SENT.popitem(last=False)


def history_turns_sent(session_id, since=None):
    """User turns the model saw in the latest call for this session; None when no call ran (at or after ``since``,
    a time.monotonic() value, when given)."""
    entry = _TURNS_SENT.get(session_id)
    if entry is None or (since is not None and entry[1] < since):
        return None
    return entry[0]


def _make_filter(inner, n, session_id):
    async def _filter(data):
        result = data.model_data
        if inner is not None:
            result = inner(data)
            if inspect.isawaitable(result):
                result = await result
        try:
            from agents.run import ModelInputData

            items = list(result.input)
            trimmed = trim_to_last_turns(items, n)
            trimmed = _LIGHTER[_HISTORY_MODE.get()](trimmed)
            _remember_turns(session_id, count_user_turns(trimmed))
            if trimmed is items:
                return result
            return ModelInputData(input=trimmed, instructions=result.instructions)
        except Exception:  # noqa: BLE001 - a filter error would abort the whole run
            logger.warning("history cap skipped for this call", exc_info=True)
            return result

    return _filter


def install_history_cap(n):
    """Chain the trim after Pico's own call_model_input_filter (compaction, queued inserts), Slack sessions only."""
    from agents.run import RunConfig

    from pico.models.base import LLMBaseModel

    original = LLMBaseModel._build_run_config
    if getattr(original, "pico_finops_agent_history_cap", False):
        return
    signature = inspect.signature(original)

    def _build_run_config(self, *args, **kwargs):
        cfg = original(self, *args, **kwargs)
        try:
            session = signature.bind_partial(self, *args, **kwargs).arguments.get("session")
        except TypeError:
            return cfg
        session_id = getattr(session, "session_id", None)
        if not (isinstance(session_id, str) and session_id.startswith(SESSION_PREFIX)):
            return cfg
        inner = cfg.call_model_input_filter if cfg is not None else None
        filt = _make_filter(inner, n, session_id)
        if cfg is None:
            return RunConfig(call_model_input_filter=filt)
        return dataclasses.replace(cfg, call_model_input_filter=filt)

    _build_run_config.pico_finops_agent_history_cap = True
    _build_run_config.__wrapped__ = original
    LLMBaseModel._build_run_config = _build_run_config


def install_no_tool_summary_answer():
    """Slack sessions only: a tool's one-line summary (``ctx.last_tool_summary``, e.g. "2×2 rows") is not an answer."""
    from pico.agent.node.chat_agentic_node import ChatAgenticNode

    original = ChatAgenticNode._build_success_result
    if getattr(original, "pico_finops_agent_no_summary", False):
        return

    def _build_success_result(self, ctx):
        session_id = getattr(self, "session_id", None)
        if not (isinstance(session_id, str) and session_id.startswith(SESSION_PREFIX)):
            return original(self, ctx)
        if getattr(ctx, "response_content", None) or not getattr(ctx, "last_tool_summary", None):
            return original(self, ctx)
        saved = ctx.last_tool_summary
        ctx.last_tool_summary = ""
        try:
            return original(self, ctx)
        finally:
            ctx.last_tool_summary = saved

    _build_success_result.pico_finops_agent_no_summary = True
    _build_success_result.__wrapped__ = original
    ChatAgenticNode._build_success_result = _build_success_result


# ---------------------------------------------------------------------------
# Plain helpers for the guard
# ---------------------------------------------------------------------------

def is_fallback(text):
    return (text or "").strip() == FALLBACK_TEXT


def is_raw_error(text):
    text = (text or "").strip()
    return text.startswith(ERROR_PREFIX) or text == ERROR_GENERIC


def is_busy(text):
    return (text or "").strip() == BUSY_TEXT


def retry_message_id(message_id):
    """A new id for the retry: the bridge drops a repeated id. A Slack ts gets one more digit (7 decimals, never a
    real ts), so the schedule plugin's signed ctx line (digits and _ only) still accepts it."""
    if re.fullmatch(r"\d+\.\d+", message_id or ""):
        return f"{message_id}1"
    return f"{message_id}-retry"


def is_reset_phrase(text):
    """The whole message is a reset phrase (case and trailing . or ! ignored); 'reset my cost report' is not."""
    if not isinstance(text, str):
        return False
    norm = " ".join(text.strip().lower().split()).rstrip(".!").strip()
    return norm in RESET_PHRASES


class RateLimiter:
    """At most ``limit`` questions per key in a sliding window (0 = off)."""

    def __init__(self, limit, window_s=3600, clock=time.monotonic):
        self.limit = limit
        self.window_s = window_s
        self.clock = clock
        self._hits = {}

    def allow(self, key):
        if not self.limit or self.limit <= 0:
            return True
        now = self.clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.window_s:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        if len(self._hits) > 10000:
            self._hits = {k: v for k, v in self._hits.items() if v}
        return True


def turn_stats(events):
    """Tool calls, LLM requests and tokens from a chat task's SSE events; the error type, never its text."""
    if not isinstance(events, list):
        return {}
    tool_ids = set()
    results = 0
    stats = {}
    for event in events:
        kind = getattr(event, "event", None)
        data = getattr(event, "data", None)
        if kind == "message":
            for item in getattr(getattr(data, "payload", None), "content", None) or []:
                payload = getattr(item, "payload", None)
                if getattr(item, "type", None) == "call-tool" and isinstance(payload, dict):
                    tool_ids.add(payload.get("callToolId", ""))
                elif getattr(item, "type", None) == "call-tool-result":
                    results += 1
        elif kind == "end":
            for name in ("requests", "input_tokens", "output_tokens"):
                stats[name] = int(getattr(data, name, 0) or 0)
        elif kind == "error":
            stats["error_type"] = str(getattr(data, "error_type", "") or "")[:80]
    return {"tool_calls": len(tool_ids), "tool_results": results, **stats}


def _add_stats(total, more):
    for key, value in more.items():
        if isinstance(value, int) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value
        else:
            total[key] = value
    return total


def question_log_fields(outcome, seconds, stats, history_turns):
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    return {"outcome": outcome, "seconds": round(float(seconds), 2), **(stats or {}), "history_turns": history_turns}


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

class _Proxy:
    """The adapter the bridge sees for one attempt: rewrites what goes out, keeps reactions on the person's message."""

    def __init__(self, real, *, retry_allowed, remap=None, skip_first_processing=False):
        from pico.gateway.models import DONE_EMOJI, ERROR_EMOJI, PROCESSING_EMOJI

        self._real = real
        self._done, self._err, self._processing = DONE_EMOJI, ERROR_EMOJI, PROCESSING_EMOJI
        self.retry_allowed = retry_allowed
        self.remap = remap or {}
        self.skip_processing = skip_first_processing
        self.hold = False  # empty first attempt: keep the processing reaction for the retry
        self.empty = self.error = self.busy = self.replaced = self.timed_out = False
        self.sent_any = self.error_sent = False

    def __getattr__(self, name):
        return getattr(self._real, name)

    @property
    def supports_streaming(self):
        return self._real.supports_streaming

    async def send_message(self, message):
        text = message.text or ""
        if is_fallback(text) and not self.sent_any:
            self.empty = True
            if self.retry_allowed:
                self.hold = True
                return None
            self.replaced = True
            return await self._real.send_message(message.model_copy(update={"text": SORRY, "ir": None}))
        if is_raw_error(text):
            self.error = True
            if self.error_sent:
                return None
            self.error_sent = True
            return await self._real.send_message(message.model_copy(update={"text": SORRY, "ir": None}))
        if is_busy(text):
            self.busy = True
        self.sent_any = True
        return await self._real.send_message(message)

    async def add_reaction(self, conversation_id, message_id, emoji, thread_id=None):
        message_id = self.remap.get(message_id, message_id)
        if self.skip_processing and emoji == self._processing:
            self.skip_processing = False  # still on the message from the first attempt
            return None
        if self.hold:
            return None
        if emoji == self._done and (self.replaced or self.timed_out):
            emoji = self._err
        return await self._real.add_reaction(conversation_id, message_id, emoji, thread_id)

    async def remove_reaction(self, conversation_id, message_id, emoji, thread_id=None):
        message_id = self.remap.get(message_id, message_id)
        if self.hold:
            return None
        return await self._real.remove_reaction(conversation_id, message_id, emoji, thread_id)

    async def finalize_stream(self, stream_id):
        return await self._real.finalize_stream(stream_id)


def _conversation(msg):
    """The message as the bridge keys its session: a top-level channel message is its own thread."""
    if msg.chat_type == "group" and not msg.thread_id:
        return msg.model_copy(update={"thread_id": msg.message_id})
    return msg


def _will_answer(bridge, msg):
    """False for the group messages the bridge ignores (no mention, and no bot session in the thread)."""
    if msg.chat_type == "group" and not msg.mentions_bot:
        if not msg.thread_id:
            return False
        try:
            from pico.models.session_manager import SessionManager

            mgr = SessionManager(session_dir=bridge._agent_config.session_dir)
            return bool(mgr.session_exists(bridge.build_session_id(msg)))
        except Exception:  # noqa: BLE001
            return False
    return True


async def _reply(adapter, conv, text):
    from pico.gateway.models import OutboundMessage

    try:
        await adapter.send_message(OutboundMessage(channel_id=conv.channel_id, conversation_id=conv.conversation_id,
                                                   thread_id=conv.thread_id, text=text))
    except Exception:  # noqa: BLE001
        logger.warning("Slack reply failed", exc_info=True)


async def _mark_error(adapter, msg):
    from pico.gateway.models import ERROR_EMOJI

    try:
        await adapter.add_reaction(msg.conversation_id, msg.message_id, ERROR_EMOJI, msg.thread_id)
    except Exception:  # noqa: BLE001
        logger.warning("Slack reaction failed", exc_info=True)


async def _attempt(original, bridge, msg, proxy, channel_config, timeout):
    """Run the wrapped bridge; True when the deadline stopped it."""
    task = asyncio.ensure_future(original(bridge, msg, proxy, channel_config))
    try:
        done, _ = await asyncio.wait({task}, timeout=max(timeout, 0.001))
    except asyncio.CancelledError:
        task.cancel()
        raise
    if task in done:
        task.result()
        return False
    proxy.timed_out = True  # the bridge's own finally then ends the reactions with error
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    return True


def _task_stats(bridge, session_id):
    """This attempt's stats from its chat task's events; None when they can't be read (then nothing is undone)."""
    try:
        events = getattr(bridge._task_manager.get_task(session_id), "events", None)
    except Exception:  # noqa: BLE001
        logger.warning("attempt stats unreadable (session %s)", session_id, exc_info=True)
        return None
    if not isinstance(events, list):
        return None
    return turn_stats(events)


def _task_running(bridge, session_id):
    try:
        task = bridge._task_manager.get_task(session_id)
    except Exception:  # noqa: BLE001
        return False
    return getattr(task, "status", None) == "running"


def _sessions(bridge):
    from pico.models.session_manager import SessionManager

    return SessionManager(session_dir=bridge._agent_config.session_dir)


async def _checkpoint(bridge, session_id):
    """The session's boundary before an attempt; None when it can't be read (then nothing is ever rolled back)."""
    try:
        return await asyncio.to_thread(_sessions(bridge).checkpoint_turn, session_id)
    except Exception:  # noqa: BLE001
        logger.warning("session checkpoint failed", exc_info=True)
        return None


async def _run_finished(bridge, session_id, timeout):
    """True once the attempt's agent run has ended (it writes the session until then)."""
    try:
        run = getattr(bridge._task_manager.get_task(session_id), "asyncio_task", None)
    except Exception:  # noqa: BLE001
        logger.warning("run state unreadable (session %s): no rollback", session_id, exc_info=True)
        return False
    if run is not None and not run.done():
        await asyncio.wait({run}, timeout=timeout)
    return run is None or run.done()


def _record_fields(record):
    return {"qid": record.qid, "finish_reasons": list(record.finishes)}


def make_guard(original, settings, *, log=None, clock=time.monotonic, limiter=None, stop_wait_s=5.0):
    """The wrapper for ChannelBridge.handle_message (``original`` is what was there: Pico's or the ctx wrapper)."""
    log = log or (lambda event, **fields: log_event(event, **fields))
    limiter = limiter or RateLimiter(settings.questions_per_hour, clock=clock)
    seen = OrderedDict()
    starters = OrderedDict()  # group thread session id -> the Slack user who started it (seen in this process)
    # Sessions with a question in flight: from its first checkpoint to its last rollback no other message may start a
    # run there (a rollback would take that run's rows too). Checked and set with no await in between: one event loop.
    in_flight = set()

    def emit(outcome, start, stats=None, session_id=None, since=None, **extra):
        try:
            turns = history_turns_sent(session_id, since=since) if session_id and outcome != "busy" else None
            fields = question_log_fields(outcome, clock() - start, stats, turns)
            log("slack_question", **fields, **extra)
        except Exception:  # noqa: BLE001 - never fail an answer on its log line
            logger.warning("slack_question log failed", exc_info=True)

    async def stop_run(bridge, session_id):
        """Stop the timed-out agent run (bounded) and wait a little for it to end; True when it did."""
        try:
            await asyncio.wait_for(bridge._task_manager.stop_task(session_id), timeout=min(3.0, stop_wait_s))
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            logger.warning("stop_task did not return in time", exc_info=True)
        try:
            run = getattr(bridge._task_manager.get_task(session_id), "asyncio_task", None)
        except Exception:  # noqa: BLE001
            run = None
        if run is not None and not run.done():
            await asyncio.wait({run}, timeout=stop_wait_s)
        confirmed = run is not None and run.done()
        if not confirmed:
            logger.error("timed-out Slack answer still running after stop (session %s)", session_id)
        return confirmed

    async def roll_back(bridge, session_id, checkpoint, stats):
        """Take an unanswered attempt (empty or error) back out of the session, once its run has ended. Skipped
        (rollback_skipped) while the run is still going; a rollback that itself fails is logged, never raised."""
        if checkpoint is None:
            return
        if not await _run_finished(bridge, session_id, stop_wait_s):
            logger.warning("unanswered-turn rollback skipped: the run is still going (session %s)", session_id)
            stats["rollback_skipped"] = stats.get("rollback_skipped", 0) + 1
            return
        try:
            await asyncio.to_thread(_sessions(bridge).rollback_turn, session_id, checkpoint)
        except Exception:  # noqa: BLE001 - the answer (or SORRY) still goes out
            logger.warning("unanswered-turn rollback failed", exc_info=True)
            return
        stats["rolled_back"] = stats.get("rolled_back", 0) + 1

    async def stopped_too_long(bridge, adapter, conv, session_id, stats, checkpoint):
        stats["stop_confirmed"] = await stop_run(bridge, session_id)
        if stats["stop_confirmed"]:  # a run that didn't stop may still be writing: leave its session alone
            await roll_back(bridge, session_id, checkpoint, stats)
        await _reply(adapter, conv, TOO_LONG)
        return "too_long", stats

    def add_attempt_stats(stats, proxy, bridge, session_id):
        """Adds this attempt's stats to the question's; returns this attempt's alone (None: unreadable)."""
        if proxy.busy:  # a busy reply started no task: the running one belongs to another message
            return {}
        attempt = _task_stats(bridge, session_id)
        if attempt is None:
            logger.warning("attempt stats unreadable (session %s): kept as if a tool ran", session_id)
            stats["stats_unreadable"] = stats.get("stats_unreadable", 0) + 1
            return None
        _add_stats(stats, attempt)
        return attempt

    async def answer(bridge, msg, conv, adapter, channel_config, session_id, start, record):
        """Up to len(HISTORY_MODES) attempts; each empty one is rolled back and the next sees a lighter history."""
        deadline = start + settings.deadline_s
        stats = {}
        attempt_msg, proxy = msg, None
        tool_ran = False  # once a tool has run, every later attempt sees the full history (and nothing is undone)
        for n, mode in enumerate(HISTORY_MODES, start=1):
            mode = "full" if tool_ran else mode
            last = n == len(HISTORY_MODES)
            if n > 1:
                if deadline - clock() <= 0:  # no time left for another attempt: finish the held reactions here
                    await _reply(adapter, conv, SORRY)
                    for call in (adapter.remove_reaction(msg.conversation_id, msg.message_id, proxy._processing,
                                                         msg.thread_id),
                                 adapter.add_reaction(msg.conversation_id, msg.message_id, proxy._err, msg.thread_id)):
                        try:
                            await call
                        except Exception:  # noqa: BLE001
                            pass
                    return "empty", stats
                record.attempt = n
                retry_id = retry_message_id(attempt_msg.message_id)
                attempt_msg = msg.model_copy(update={"message_id": retry_id, "thread_id": conv.thread_id})
                proxy = _Proxy(adapter, retry_allowed=not last, remap={retry_id: msg.message_id},
                               skip_first_processing=True)
            else:
                proxy = _Proxy(adapter, retry_allowed=True)
            checkpoint = await _checkpoint(bridge, session_id)
            if checkpoint is None:  # no boundary, so this attempt can't be undone: say so in the question line
                stats["checkpoint_failed"] = stats.get("checkpoint_failed", 0) + 1
            token = _HISTORY_MODE.set(mode)  # _attempt's task copies the context: its model calls see the mode
            try:
                timed_out = await _attempt(original, bridge, attempt_msg, proxy, channel_config, deadline - clock())
            finally:
                _HISTORY_MODE.reset(token)
            stats["attempts"] = n
            attempt = add_attempt_stats(stats, proxy, bridge, session_id)
            # Unknown (None) counts as "a tool may have run": keep the attempt rather than risk doing it twice.
            ran = attempt is None or attempt.get("tool_results", 0) > 0
            tool_ran = tool_ran or ran
            if timed_out:  # a tool call the deadline cut off (no result yet) may have done its work too
                cut_off = attempt is not None and attempt.get("tool_calls", 0) > 0
                return await stopped_too_long(bridge, adapter, conv, session_id, stats,
                                              None if (ran or cut_off) else checkpoint)
            if not proxy.busy and not ran and (proxy.empty or proxy.error):  # busy: another run's session
                await roll_back(bridge, session_id, checkpoint, stats)
            if not proxy.empty:
                done = "answered" if n == 1 else "retried"
                return ("busy" if proxy.busy else "error" if proxy.error else done), stats
        return "empty", stats

    async def handle_message(self, msg, adapter, channel_config=None):
        key = f"{msg.channel_id}_{msg.message_id}"
        if key in seen:
            return None  # a re-delivery: the bridge would drop it too
        if not _will_answer(self, msg):
            return await original(self, msg, adapter, channel_config)
        seen[key] = None
        while len(seen) > _MAX_REMEMBERED:
            seen.popitem(last=False)
        start = clock()
        since = _now()
        conv = _conversation(msg)
        session_id = self.build_session_id(conv)
        group = msg.chat_type == "group"
        # In a channel a reset needs an @mention: a bare "clear" in a busy thread is an ordinary message.
        if is_reset_phrase(msg.text) and (not group or msg.mentions_bot):
            extra = {}
            if group:
                starter = starters.get(session_id)
                extra["starter_known"] = starter is not None
                if starter is not None and starter != msg.sender_id:
                    await _reply(adapter, conv, RESET_NOT_YOURS)
                    emit("reset_refused", start, **extra)
                    return None
            if _task_running(self, session_id) or session_id in in_flight:
                await _reply(adapter, conv, RESET_WHILE_BUSY)  # never clear a session under a running answer
                emit("busy", start, **extra)
                return None
            try:
                self.clear_session(conv)  # the asker's own DM, or this thread only
            except Exception:  # noqa: BLE001
                logger.warning("session clear failed", exc_info=True)
            await _reply(adapter, conv, RESET_DONE)
            emit("reset", start, **extra)
            return None
        if session_id in in_flight:  # the same reply Pico gives while a run is going; nothing starts, nothing counted
            await _reply(adapter, conv, BUSY_TEXT)
            await _mark_error(adapter, msg)  # as Pico's busy path does: this message is finished, not pending
            emit("busy", start)
            return None
        if msg.sender_id and not limiter.allow(msg.sender_id):  # no sender id: no shared bucket for all of them
            await _reply(adapter, conv, SLOW_DOWN)
            emit("rate_limited", start)
            return None
        if group and not msg.thread_id and msg.sender_id:
            starters[session_id] = msg.sender_id  # a top-level channel message starts its thread
            while len(starters) > _MAX_REMEMBERED:
                starters.popitem(last=False)
        in_flight.add(session_id)
        record = llm_log.Record(uuid.uuid4().hex[:12])
        try:
            with llm_log.recording(record):  # the attempts' tasks inherit it: their model calls land in `record`
                outcome, stats = await answer(self, msg, conv, adapter, channel_config, session_id, start, record)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await _reply(adapter, conv, SORRY)  # no silent failure and no raw error text in Slack
            emit("error", start, {"error_type": type(e).__name__, **_record_fields(record)}, session_id, since)
            raise
        finally:
            in_flight.discard(session_id)
        emit(outcome, start, {**stats, **_record_fields(record)}, session_id, since)
        return None

    return handle_message


def install(settings=None):
    """Wire the history cap and the guard into Pico (idempotent). Call after the ctx wrapper, so this one is
    outermost and sees the text the person typed."""
    from pico.gateway import bridge

    settings = settings or settings_from_env()
    install_history_cap(settings.history_turns)  # if this fails the caller logs slack_guard_off
    install_no_tool_summary_answer()
    original = bridge.ChannelBridge.handle_message
    if getattr(original, "pico_finops_agent_guard", False):
        return
    try:
        guarded = make_guard(original, settings)
        guarded.pico_finops_agent_guard = True
        bridge.ChannelBridge.handle_message = guarded
    except Exception as e:  # noqa: BLE001
        log_event("slack_guard_partial", history_cap="on", guard="off", reason=describe(e, 200))
        return
    log_event("slack_guard_on", history_turns=settings.history_turns, deadline_s=settings.deadline_s,
              questions_per_hour=settings.questions_per_hour)
