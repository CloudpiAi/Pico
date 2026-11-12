"""pico_finops_agent.slack_guard: Slack answer hardening for the standalone agent.

The model sees only the last N user turns; an empty answer is run once more, then a clear sentence; raw errors and
slow answers become clear sentences; a per-person hourly limit; a reset phrase; one log line per question with no
question or answer text. The guard wraps Pico's real ChannelBridge here (only the task manager is a fake), so the
tests see the bridge's own reactions, dedup and fallback text.
"""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from pico_finops_agent import slack_guard as sg
from pico.api.models.cli_models import (
    IMessageContent,
    SSEDataType,
    SSEEndData,
    SSEErrorData,
    SSEEvent,
    SSEMessageData,
    SSEMessagePayload,
)
from pico.gateway.bridge import ChannelBridge
from pico.gateway.channel.base import ChannelAdapter
from pico.gateway.models import DONE_EMOJI, ERROR_EMOJI, PROCESSING_EMOJI, ChannelConfig, InboundMessage, Verbose

# ======================================================================
# History cap: trim_to_last_turns
# ======================================================================

def user(text):
    return {"role": "user", "content": text}


def typed_user(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def bot(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def call(cid):
    return {"type": "function_call", "call_id": cid, "name": "execute_sql", "arguments": "{}"}


def output(cid):
    return {"type": "function_call_output", "call_id": cid, "output": "ok"}


def conversation(turns):
    items = []
    for i in range(turns):
        items += [user(f"q{i}"), call(f"c{i}"), output(f"c{i}"), bot(f"a{i}")]
    return items


def test_trim_keeps_items_from_the_nth_last_user_message():
    items = conversation(10)
    got = sg.trim_to_last_turns(items, 3)
    assert got == items[7 * 4:]
    assert sg.count_user_turns(got) == 3
    assert got[0] == user("q7")


def test_trim_counts_typed_and_plain_user_messages():
    items = [typed_user("q0"), bot("a0"), user("q1"), bot("a1"), typed_user("q2"), bot("a2")]
    assert sg.trim_to_last_turns(items, 2) == items[2:]


def test_trim_keeps_system_and_developer_items():
    sys_item = {"role": "system", "content": "rules"}
    dev_item = {"type": "message", "role": "developer", "content": "more rules"}
    items = [sys_item, user("q0"), bot("a0"), dev_item, user("q1"), bot("a1"), user("q2"), bot("a2")]
    assert sg.trim_to_last_turns(items, 1) == [sys_item, dev_item, user("q2"), bot("a2")]


def test_trim_never_splits_a_tool_call_from_its_output():
    # a user message queued mid-run lands between a call and its output
    items = [user("q0"), bot("a0"), user("q1"), call("c1"), user("insert"), output("c1"), bot("a1")]
    got = sg.trim_to_last_turns(items, 1)
    assert got == [call("c1"), user("insert"), output("c1"), bot("a1")]
    ids = [i.get("call_id") for i in got if i.get("type") == "function_call_output"]
    assert all(any(j.get("type") == "function_call" and j.get("call_id") == c for j in got) for c in ids)


def test_trim_unchanged_when_history_is_short():
    items = conversation(3)
    assert sg.trim_to_last_turns(items, 3) is items
    assert sg.trim_to_last_turns(items, 8) is items


@pytest.mark.parametrize("n", [0, -1, None])
def test_trim_off_when_n_not_positive(n):
    items = conversation(5)
    assert sg.trim_to_last_turns(items, n) is items


@pytest.mark.parametrize("bad", [None, "text", {"role": "user"}, [], [bot("a"), call("c")]])
def test_trim_leaves_malformed_or_userless_input_untouched(bad):
    assert sg.trim_to_last_turns(bad, 2) is bad


def test_trim_handles_object_items():
    items = [SimpleNamespace(role="user", type="message"), bot("a0"), SimpleNamespace(role="user", type="message"),
             bot("a1"), None, "odd"]
    got = sg.trim_to_last_turns(items, 1)
    assert got == items[2:]


def test_trim_does_not_mutate_its_input():
    items = conversation(5)
    before = json.dumps(items)
    sg.trim_to_last_turns(items, 2)
    assert json.dumps(items) == before


# ---------- the run-config filter install ----------

class FakeSession:
    def __init__(self, session_id):
        self.session_id = session_id


@pytest.fixture
def restore_pico(monkeypatch):
    """Undo every class patch the guard makes (monkeypatch restores the saved originals)."""
    from pico.models.base import LLMBaseModel

    monkeypatch.setattr(LLMBaseModel, "_build_run_config", LLMBaseModel._build_run_config)
    monkeypatch.setattr(ChannelBridge, "handle_message", ChannelBridge.handle_message)
    sg._TURNS_SENT.clear()
    return monkeypatch


def _call_filter(run_config, items, instructions="sys"):
    from agents.run import CallModelData, ModelInputData

    data = CallModelData(model_data=ModelInputData(input=list(items), instructions=instructions), agent=None,
                         context=None)
    return asyncio.run(run_config.call_model_input_filter(data))


def test_history_cap_filter_trims_gateway_sessions(restore_pico):
    from pico.models.base import LLMBaseModel

    sg.install_history_cap(2)
    cfg = LLMBaseModel._build_run_config(object(), session=FakeSession("gateway_ch--D1"))
    assert cfg is not None and cfg.call_model_input_filter is not None
    got = _call_filter(cfg, conversation(6))
    assert got.input == conversation(6)[16:]
    assert got.instructions == "sys"
    assert sg.history_turns_sent("gateway_ch--D1") == 2


def test_history_cap_leaves_other_sessions_alone(restore_pico):
    from pico.models.base import LLMBaseModel

    sg.install_history_cap(2)
    assert LLMBaseModel._build_run_config(object(), session=FakeSession("chat_session_1")) is None
    assert LLMBaseModel._build_run_config(object(), session=None) is None


def test_history_cap_chains_after_the_existing_filter(restore_pico):
    from agents.run import ModelInputData, RunConfig

    from pico.models.base import LLMBaseModel

    seen = {}

    async def inner(data):
        seen["inner_input_len"] = len(data.model_data.input)
        return ModelInputData(input=list(data.model_data.input) + [user("queued")], instructions="rewritten")

    def fake_build(self, pending_input_queue=None, session=None, **kw):
        return RunConfig(call_model_input_filter=inner, workflow_name="kept")

    restore_pico.setattr(LLMBaseModel, "_build_run_config", fake_build)
    sg.install_history_cap(1)
    cfg = LLMBaseModel._build_run_config(object(), session=FakeSession("gateway_x"))
    assert cfg.workflow_name == "kept"
    got = _call_filter(cfg, conversation(4))
    assert seen["inner_input_len"] == 16  # the existing filter (compaction, queued inserts) sees everything
    assert got.input == [user("queued")]  # ours runs on its output
    assert got.instructions == "rewritten"


def test_history_cap_never_breaks_the_run(restore_pico, monkeypatch):
    from pico.models.base import LLMBaseModel

    sg.install_history_cap(2)
    monkeypatch.setattr(sg, "trim_to_last_turns", lambda items, n: 1 / 0)
    cfg = LLMBaseModel._build_run_config(object(), session=FakeSession("gateway_x"))
    items = conversation(3)
    assert _call_filter(cfg, items).input == items


def test_history_cap_install_is_idempotent(restore_pico):
    from pico.models.base import LLMBaseModel

    sg.install_history_cap(2)
    first = LLMBaseModel._build_run_config
    sg.install_history_cap(2)
    assert LLMBaseModel._build_run_config is first
    assert getattr(first, "pico_finops_agent_history_cap", False)


# ======================================================================
# Plain helpers
# ======================================================================

def test_settings_defaults_and_env():
    s = sg.settings_from_env({})
    assert (s.history_turns, s.deadline_s, s.questions_per_hour) == (8, 180, 30)
    s = sg.settings_from_env({"AGENT_HISTORY_TURNS": "3", "AGENT_ANSWER_DEADLINE_S": "60",
                              "AGENT_QUESTIONS_PER_HOUR": "0"})
    assert (s.history_turns, s.deadline_s, s.questions_per_hour) == (3, 60, 0)


@pytest.mark.parametrize("env", [{"AGENT_HISTORY_TURNS": "0"}, {"AGENT_HISTORY_TURNS": "51"},
                                 {"AGENT_HISTORY_TURNS": "x"}, {"AGENT_ANSWER_DEADLINE_S": "10"},
                                 {"AGENT_ANSWER_DEADLINE_S": "901"}, {"AGENT_QUESTIONS_PER_HOUR": "-1"},
                                 {"AGENT_QUESTIONS_PER_HOUR": ""}])
def test_settings_out_of_range_fall_back_to_defaults(env):
    s = sg.settings_from_env(env)
    assert (s.history_turns, s.deadline_s, s.questions_per_hour) == (8, 180, 30)


def test_recognises_the_bridge_texts():
    assert sg.is_fallback("(No response generated)")
    assert not sg.is_fallback("No response generated, sorry")
    assert sg.is_raw_error("❌ Error: 429 quota exceeded for key AIza...")
    assert sg.is_raw_error("❌ An error occurred.")
    assert not sg.is_raw_error("Errors in your bill: none")
    assert sg.is_busy("The previous request is still being processed. Please wait.")


def test_retry_message_id_is_new_and_ctx_safe():
    assert sg.retry_message_id("1731330000.123456") != "1731330000.123456"
    assert sg.retry_message_id("1731330000.123456").replace(".", "").isdigit()
    assert sg.retry_message_id("abc") != "abc"


@pytest.mark.parametrize("text", ["reset", "Reset", "  RESET!  ", "new conversation", "New conversation.",
                                  "start over", "Start   over", "clear", "/new", "/reset", "/clear"])
def test_reset_phrase_whole_message(text):
    assert sg.is_reset_phrase(text)


@pytest.mark.parametrize("text", ["reset my cost report", "please reset", "clear the cache", "how do I start over?",
                                  "", None, "new conversation about EC2"])
def test_not_a_reset_phrase(text):
    assert not sg.is_reset_phrase(text)


def test_rate_limiter_per_key_sliding_hour():
    now = [1000.0]
    rl = sg.RateLimiter(2, clock=lambda: now[0])
    assert rl.allow("U1") and rl.allow("U1")
    assert not rl.allow("U1")
    assert rl.allow("U2")  # per person
    now[0] += 3599
    assert not rl.allow("U1")
    now[0] += 2
    assert rl.allow("U1")


def test_rate_limiter_zero_is_off():
    rl = sg.RateLimiter(0)
    assert all(rl.allow("U1") for _ in range(500))


def test_turn_stats_from_events():
    events = [tool_event("c1"), tool_event("c1", result=True), tool_event("c2"), message_event("hi"),
              end_event(input_tokens=1200, output_tokens=80, requests=3)]
    assert sg.turn_stats(events) == {"tool_calls": 2, "requests": 3, "input_tokens": 1200, "output_tokens": 80}
    assert sg.turn_stats([error_event("boom", "RateLimitError")])["error_type"] == "RateLimitError"
    assert sg.turn_stats(None) == {}


def test_log_fields_have_no_text():
    fields = sg.question_log_fields("answered", 12.3456, {"tool_calls": 2, "input_tokens": 5}, 8)
    assert fields == {"outcome": "answered", "seconds": 12.35, "tool_calls": 2, "input_tokens": 5,
                      "history_turns": 8}
    with pytest.raises(ValueError):
        sg.question_log_fields("whatever", 1, {}, None)


# ======================================================================
# The guard around Pico's real ChannelBridge
# ======================================================================

def _ts():
    return datetime.now().isoformat() + "Z"


def message_event(text):
    return SSEEvent(id=1, event="message", timestamp=_ts(), data=SSEMessageData(
        type=SSEDataType.CREATE_MESSAGE,
        payload=SSEMessagePayload(message_id="m1", role="assistant",
                                  content=[IMessageContent(type="markdown", payload={"content": text})])))


def tool_event(cid, result=False):
    kind = "call-tool-result" if result else "call-tool"
    return SSEEvent(id=1, event="message", timestamp=_ts(), data=SSEMessageData(
        type=SSEDataType.CREATE_MESSAGE,
        payload=SSEMessagePayload(message_id="m0", role="assistant", content=[
            IMessageContent(type=kind, payload={"callToolId": cid, "toolName": "execute_sql"})])))


def end_event(**usage):
    return SSEEvent(id=9, event="end", timestamp=_ts(),
                    data=SSEEndData(session_id="s", total_events=3, action_count=2, duration=1.0, **usage))


def error_event(text, kind):
    return SSEEvent(id=9, event="error", timestamp=_ts(), data=SSEErrorData(error=text, error_type=kind))


SCRIPTS = {
    "answer": lambda: [tool_event("c1"), tool_event("c1", result=True), message_event("Here is your answer"),
                       end_event(input_tokens=100, output_tokens=20, requests=2)],
    "empty": lambda: [end_event(input_tokens=170000, output_tokens=0, requests=1)],
    "error": lambda: [error_event("429 quota exceeded for key AIzaSECRET", "RateLimitError")],
    "slow": lambda: [message_event("late")],
}


class FakeTasks:
    """Stands in for ChatTaskManager: each start_chat takes the next script."""

    def __init__(self, *scripts, stop_works=True):
        self.scripts = list(scripts)
        self.requests = []
        self.tasks = {}
        self.stopped = []
        self.stop_works = stop_works

    async def start_chat(self, agent_config, request, sub_agent_id=None):
        kind = self.scripts.pop(0)
        self.requests.append(request)
        if kind == "busy":
            raise ValueError("A task is already running for session x")
        run = asyncio.ensure_future(asyncio.sleep(30 if kind == "slow" else 0))
        task = SimpleNamespace(session_id=request.session_id, kind=kind, events=SCRIPTS[kind](), asyncio_task=run,
                               status="running" if kind == "slow" else "completed")
        self.tasks[request.session_id] = task
        return task

    async def consume_events(self, task):
        if task.kind == "slow":
            await asyncio.sleep(30)
        for event in task.events:
            yield event

    def get_task(self, session_id, user_id=None):
        return self.tasks.get(session_id)

    async def stop_task(self, session_id, user_id=None):
        self.stopped.append(session_id)
        if self.stop_works:
            self.tasks[session_id].asyncio_task.cancel()
        return True


class RecordingAdapter(ChannelAdapter):
    def __init__(self):
        super().__init__("slack", {}, bridge=None)
        self.sent = []
        self.added = []
        self.removed = []
        self.n = 0

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send_message(self, message):
        self.sent.append(message)
        self.n += 1
        return f"bot{self.n}"

    async def add_reaction(self, conversation_id, message_id, emoji, thread_id=None):
        self.added.append((message_id, emoji))

    async def remove_reaction(self, conversation_id, message_id, emoji, thread_id=None):
        self.removed.append((message_id, emoji))

    def texts(self):
        return [m.text for m in self.sent]


def inbound(text="how much did EC2 cost in October?", message_id="1731330000.000100", sender="U0AAA00001",
            chat_type="p2p", thread_id=None, mentions_bot=False):
    return InboundMessage(channel_id="slack", sender_id=sender, conversation_id="D0AAA00001",
                          message_id=message_id, text=text, chat_type=chat_type, thread_id=thread_id,
                          mentions_bot=mentions_bot)


async def ctx_like_wrapper(self, msg, adapter, channel_config=None):
    """Like the schedule plugin's ctx wrapper (installed before the guard): the bridge gets a first line in front of
    the person's text, so Pico's own first-word commands ("reset ...") don't fire on a question."""
    msg = msg.model_copy(update={"text": f"[slack ctx=test]\n{msg.text}"})
    return await ChannelBridge.handle_message(self, msg, adapter, channel_config)


class Harness:
    def __init__(self, tmp_path, *scripts, settings=None, stop_works=True, stop_wait_s=5):
        self.tasks = FakeTasks(*scripts, stop_works=stop_works)
        self.bridge = ChannelBridge(SimpleNamespace(session_dir=str(tmp_path / "sessions")), self.tasks)
        self.adapter = RecordingAdapter()
        self.logs = []
        self.cleared = []
        self.bridge.clear_session = lambda msg: self.cleared.append(self.bridge.build_session_id(msg))
        self.settings = settings or sg.Settings(history_turns=8, deadline_s=5, questions_per_hour=30)
        self.handle = sg.make_guard(ctx_like_wrapper, self.settings,
                                    log=lambda event, **f: self.logs.append((event, f)), stop_wait_s=stop_wait_s)
        # as deployed: verbose quiet (deploy/pico-finops-agent.yml), so only the answer is posted
        self.channel_config = ChannelConfig(adapter="slack", verbose=Verbose.OFF)

    def send(self, msg):
        asyncio.run(self.handle(self.bridge, msg, self.adapter, self.channel_config))

    def outcomes(self):
        return [f["outcome"] for e, f in self.logs if e == "slack_question"]


def test_normal_answer_passes_through(tmp_path):
    h = Harness(tmp_path, "answer")
    h.send(inbound())
    assert h.adapter.texts() == ["Here is your answer"]
    mid = "1731330000.000100"
    assert h.adapter.added == [(mid, PROCESSING_EMOJI), (mid, DONE_EMOJI)]
    assert h.adapter.removed == [(mid, PROCESSING_EMOJI)]
    assert h.outcomes() == ["answered"]
    fields = h.logs[0][1]
    assert fields["tool_calls"] == 1 and fields["input_tokens"] == 100 and fields["output_tokens"] == 20
    assert "seconds" in fields and "history_turns" in fields


def test_empty_answer_is_retried_once_with_a_new_message_id(tmp_path):
    h = Harness(tmp_path, "empty", "answer")
    h.send(inbound())
    assert h.adapter.texts() == ["Here is your answer"]  # no "(No response generated)" posted
    assert len(h.tasks.requests) == 2
    assert h.tasks.requests[0].session_id == h.tasks.requests[1].session_id == "gateway_slack--D0AAA00001"
    mid = "1731330000.000100"
    # the reactions stay on the person's message: one processing, one done, nothing on the retry id
    assert h.adapter.added == [(mid, PROCESSING_EMOJI), (mid, DONE_EMOJI)]
    assert h.adapter.removed == [(mid, PROCESSING_EMOJI)]
    assert h.outcomes() == ["retried"]
    assert h.logs[0][1]["input_tokens"] == 170100  # both attempts


def test_empty_twice_posts_the_sorry_sentence(tmp_path):
    h = Harness(tmp_path, "empty", "empty")
    h.send(inbound())
    assert h.adapter.texts() == [sg.SORRY]
    assert len(h.tasks.requests) == 2
    mid = "1731330000.000100"
    assert h.adapter.added == [(mid, PROCESSING_EMOJI), (mid, ERROR_EMOJI)]
    assert h.adapter.removed == [(mid, PROCESSING_EMOJI)]
    assert h.outcomes() == ["empty"]


def test_group_top_level_retry_keeps_the_same_thread_session(tmp_path):
    h = Harness(tmp_path, "empty", "answer")
    h.send(inbound(chat_type="group", mentions_bot=True))
    sid = "gateway_slack--D0AAA00001--1731330000.000100"
    assert [r.session_id for r in h.tasks.requests] == [sid, sid]
    assert all(m.thread_id == "1731330000.000100" for m in h.adapter.sent)


def test_raw_error_text_never_reaches_slack(tmp_path):
    h = Harness(tmp_path, "error")
    h.send(inbound())
    assert h.adapter.texts() == [sg.SORRY]
    assert h.adapter.added[-1] == ("1731330000.000100", ERROR_EMOJI)
    assert h.outcomes() == ["error"]
    assert h.logs[0][1]["error_type"] == "RateLimitError"
    assert "AIza" not in json.dumps(h.logs)


def test_busy_session_reply_passes_through(tmp_path):
    h = Harness(tmp_path, "busy")
    h.send(inbound())
    assert h.adapter.texts() == ["The previous request is still being processed. Please wait."]
    assert h.outcomes() == ["busy"]


def test_deadline_stops_the_answer(tmp_path):
    h = Harness(tmp_path, "slow", settings=sg.Settings(history_turns=8, deadline_s=0.2, questions_per_hour=30))
    h.send(inbound())
    assert h.adapter.texts() == [sg.TOO_LONG]
    assert h.tasks.stopped == ["gateway_slack--D0AAA00001"]  # the agent run is stopped too
    mid = "1731330000.000100"
    assert h.adapter.removed == [(mid, PROCESSING_EMOJI)]
    assert h.adapter.added[-1] == (mid, ERROR_EMOJI)
    assert DONE_EMOJI not in [e for _, e in h.adapter.added]
    assert h.outcomes() == ["too_long"]
    assert h.logs[0][1]["stop_confirmed"] is True  # the agent run really ended


def test_deadline_stop_that_does_not_end_the_run_is_flagged(tmp_path, caplog):
    h = Harness(tmp_path, "slow", settings=sg.Settings(history_turns=8, deadline_s=0.2, questions_per_hour=30),
                stop_works=False, stop_wait_s=0.2)
    with caplog.at_level("ERROR", logger="pico_finops_agent.slack_guard"):
        h.send(inbound())
    assert h.adapter.texts() == [sg.TOO_LONG]
    assert h.outcomes() == ["too_long"] and h.logs[0][1]["stop_confirmed"] is False
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_deadline_stop_call_is_bounded(tmp_path):
    h = Harness(tmp_path, "slow", settings=sg.Settings(history_turns=8, deadline_s=0.2, questions_per_hour=30),
                stop_wait_s=0.2)

    async def hangs(session_id, user_id=None):
        await asyncio.sleep(60)

    h.tasks.stop_task = hangs
    h.send(inbound())
    assert h.adapter.texts() == [sg.TOO_LONG] and h.logs[0][1]["stop_confirmed"] is False


def test_rate_limit_per_person_across_threads(tmp_path):
    h = Harness(tmp_path, "answer", "answer",
                settings=sg.Settings(history_turns=8, deadline_s=5, questions_per_hour=2))
    h.send(inbound(message_id="1.000001"))
    h.send(inbound(message_id="1.000002", thread_id="1.000001"))
    h.send(inbound(message_id="1.000003", thread_id="9.000009"))  # another thread: still the same person
    assert len(h.tasks.requests) == 2
    assert h.adapter.texts()[-1] == sg.SLOW_DOWN
    assert h.outcomes() == ["answered", "answered", "rate_limited"]


def test_rate_limit_zero_is_off(tmp_path):
    h = Harness(tmp_path, *["answer"] * 4, settings=sg.Settings(history_turns=8, deadline_s=5, questions_per_hour=0))
    for i in range(4):
        h.send(inbound(message_id=f"1.00000{i}"))
    assert h.outcomes() == ["answered"] * 4


def test_reset_phrase_clears_the_session_without_the_model(tmp_path):
    h = Harness(tmp_path)
    h.send(inbound(text="Start over"))
    assert h.cleared == ["gateway_slack--D0AAA00001"]
    assert h.adapter.texts() == [sg.RESET_DONE]
    assert h.tasks.requests == []
    assert h.outcomes() == ["reset"]


def test_reset_in_a_thread_clears_only_that_thread(tmp_path):
    h = Harness(tmp_path)
    h.send(inbound(text="reset", chat_type="group", mentions_bot=True, thread_id="5.000005"))
    assert h.cleared == ["gateway_slack--D0AAA00001--5.000005"]
    assert h.adapter.sent[0].thread_id == "5.000005"


def test_reset_words_inside_a_question_go_to_the_model(tmp_path):
    h = Harness(tmp_path, "answer")
    h.send(inbound(text="reset my cost report"))
    assert h.cleared == [] and len(h.tasks.requests) == 1


def test_resets_do_not_count_against_the_limit(tmp_path):
    h = Harness(tmp_path, "answer", settings=sg.Settings(history_turns=8, deadline_s=5, questions_per_hour=1))
    h.send(inbound(text="reset", message_id="1.000001"))
    h.send(inbound(message_id="1.000002"))
    assert h.outcomes() == ["reset", "answered"]


def test_duplicate_delivery_is_not_counted_or_logged(tmp_path):
    h = Harness(tmp_path, "answer", settings=sg.Settings(history_turns=8, deadline_s=5, questions_per_hour=1))
    h.send(inbound())
    h.send(inbound())  # Slack re-delivers the same message: the bridge drops it
    assert h.outcomes() == ["answered"] and len(h.tasks.requests) == 1
    assert sg.SLOW_DOWN not in h.adapter.texts()


def test_ignored_group_message_is_not_counted_or_logged(tmp_path):
    h = Harness(tmp_path, settings=sg.Settings(history_turns=8, deadline_s=5, questions_per_hour=1))
    h.send(inbound(chat_type="group", mentions_bot=False))  # no mention, no thread: the bridge ignores it
    h.send(inbound(chat_type="group", mentions_bot=False, thread_id="7.000007", message_id="2.000002"))
    assert h.logs == [] and h.adapter.sent == []


def test_log_line_never_has_question_or_answer_text(tmp_path):
    h = Harness(tmp_path, "answer", "empty", "empty", "error")
    h.send(inbound(message_id="1.000001"))
    h.send(inbound(message_id="1.000002"))
    h.send(inbound(message_id="1.000003"))
    dumped = json.dumps(h.logs)
    assert "EC2" not in dumped and "Here is your answer" not in dumped and "quota" not in dumped
    assert {e for e, _ in h.logs} == {"slack_question"}


# ---------- install ----------

def test_install_wraps_outermost_and_once(restore_pico):
    calls = []

    async def ctx_wrapper(self, msg, adapter, channel_config=None):
        calls.append(msg.text)

    restore_pico.setattr(ChannelBridge, "handle_message", ctx_wrapper)
    sg.install(sg.Settings(history_turns=8, deadline_s=5, questions_per_hour=30))
    wrapped = ChannelBridge.handle_message
    assert getattr(wrapped, "pico_finops_agent_guard", False)
    sg.install(sg.Settings(history_turns=8, deadline_s=5, questions_per_hour=30))
    assert ChannelBridge.handle_message is wrapped
    from pico.models.base import LLMBaseModel

    assert getattr(LLMBaseModel._build_run_config, "pico_finops_agent_history_cap", False)


# ======================================================================
# Review fixes (2026-11-11)
# ======================================================================

def test_trim_moves_the_cut_over_the_reasoning_before_a_call():
    reasoning = {"type": "reasoning", "id": "r1", "summary": []}
    items = [user("q0"), bot("a0"), user("q1"), reasoning, call("c1"), user("insert"), output("c1"), bot("a1")]
    assert sg.trim_to_last_turns(items, 1) == [reasoning, call("c1"), user("insert"), output("c1"), bot("a1")]


def test_compaction_resume_marker_is_not_a_user_turn():
    from pico.utils.message_utils import COMPACT_RESUME_MARKER

    marker = typed_user(f"{COMPACT_RESUME_MARKER} continue where you left off")
    items = [user("q0"), bot("a0"), user("q1"), bot("a1"), marker, bot("a1b")]
    assert sg.count_user_turns(items) == 2
    assert sg.trim_to_last_turns(items, 1) == items[2:]
    plain = {"role": "user", "content": f"  {COMPACT_RESUME_MARKER} go on"}
    assert sg.count_user_turns([plain]) == 0


def test_group_thread_reset_only_by_the_thread_starter_with_a_mention(tmp_path):
    h = Harness(tmp_path, "answer")
    top = "3.000003"
    sid = f"gateway_slack--D0AAA00001--{top}"
    h.send(inbound(chat_type="group", mentions_bot=True, message_id=top, sender="U0STARTER1"))
    # someone else in the thread, with a mention: refused, nothing cleared
    h.send(inbound(text="reset", chat_type="group", mentions_bot=True, thread_id=top, message_id="3.000004",
                   sender="U0OTHER001"))
    assert h.cleared == []
    assert h.adapter.texts()[-1] == sg.RESET_NOT_YOURS
    # the starter without a mention: not a reset (the bridge ignores it: no session on disk in this test)
    h.send(inbound(text="clear", chat_type="group", mentions_bot=False, thread_id=top, message_id="3.000005",
                   sender="U0STARTER1"))
    assert h.cleared == []
    # the starter with a mention: cleared
    h.send(inbound(text="reset", chat_type="group", mentions_bot=True, thread_id=top, message_id="3.000006",
                   sender="U0STARTER1"))
    assert h.cleared == [sid]
    assert h.outcomes() == ["answered", "reset_refused", "reset"]


def test_group_thread_reset_with_unknown_starter_needs_a_mention_and_is_logged(tmp_path):
    h = Harness(tmp_path)
    h.send(inbound(text="reset", chat_type="group", mentions_bot=True, thread_id="5.000005"))
    assert h.cleared == ["gateway_slack--D0AAA00001--5.000005"]
    assert h.logs[0][1]["starter_known"] is False


def test_group_thread_reset_word_without_mention_is_never_a_reset(tmp_path, monkeypatch):
    h = Harness(tmp_path, "answer")
    from pico.models.session_manager import SessionManager

    # a bot thread with a session on disk (the bridge and the guard both check it)
    monkeypatch.setattr(SessionManager, "session_exists", lambda self, session_id: True)
    h.send(inbound(text="clear", chat_type="group", mentions_bot=False, thread_id="6.000006"))
    assert h.cleared == [] and len(h.tasks.requests) == 1  # it goes to the model as an ordinary message


def test_dm_reset_needs_no_mention(tmp_path):
    h = Harness(tmp_path)
    h.send(inbound(text="reset", chat_type="p2p", mentions_bot=False))
    assert h.cleared == ["gateway_slack--D0AAA00001"]


def test_reset_while_an_answer_is_running_does_not_clear(tmp_path):
    h = Harness(tmp_path)
    sid = "gateway_slack--D0AAA00001"
    h.tasks.tasks[sid] = SimpleNamespace(status="running", events=[])
    h.send(inbound(text="start over"))
    assert h.cleared == []
    assert h.adapter.texts() == [sg.RESET_WHILE_BUSY]
    assert h.outcomes() == ["busy"]


def test_bridge_exception_sends_sorry_then_reraises(tmp_path):
    h = Harness(tmp_path)

    async def broken(self, msg, adapter, channel_config=None):
        raise RuntimeError("secret detail AIzaXYZ")

    h.handle = sg.make_guard(broken, h.settings, log=lambda event, **f: h.logs.append((event, f)))
    with pytest.raises(RuntimeError):
        h.send(inbound())
    assert h.adapter.texts() == [sg.SORRY]
    assert h.outcomes() == ["error"] and h.logs[0][1]["error_type"] == "RuntimeError"
    assert "AIza" not in json.dumps(h.logs)


def test_empty_sender_does_not_share_a_rate_bucket(tmp_path):
    h = Harness(tmp_path, *["answer"] * 3, settings=sg.Settings(history_turns=8, deadline_s=5, questions_per_hour=1))
    for i in range(3):
        h.send(inbound(sender="", message_id=f"4.00000{i}"))
    assert h.outcomes() == ["answered"] * 3


def test_busy_reply_gets_no_stats_from_the_running_task(tmp_path):
    h = Harness(tmp_path, "busy")
    sid = "gateway_slack--D0AAA00001"
    h.tasks.tasks[sid] = SimpleNamespace(status="running", events=SCRIPTS["answer"]())
    sg._remember_turns(sid, 8)  # recorded by the other message's model call
    h.send(inbound())
    fields = h.logs[0][1]
    assert fields["outcome"] == "busy"
    assert "input_tokens" not in fields and "tool_calls" not in fields
    assert fields["history_turns"] is None
    assert sg.history_turns_sent(sid) == 8  # the other message's value is left alone


def test_history_turns_are_only_those_recorded_after_the_message_started():
    sg._remember_turns("gateway_t", 3)
    later = sg._now() + 1
    assert sg.history_turns_sent("gateway_t") == 3
    assert sg.history_turns_sent("gateway_t", since=later) is None


def test_install_logs_partial_when_only_the_history_cap_is_on(restore_pico, monkeypatch):
    events = []
    monkeypatch.setattr(sg, "log_event", lambda event, **f: events.append((event, f)))
    monkeypatch.setattr(sg, "make_guard", lambda *a, **k: 1 / 0)
    sg.install(sg.Settings(history_turns=8, deadline_s=5, questions_per_hour=30))
    from pico.models.base import LLMBaseModel

    assert getattr(LLMBaseModel._build_run_config, "pico_finops_agent_history_cap", False)
    assert not getattr(ChannelBridge.handle_message, "pico_finops_agent_guard", False)
    assert events[0][0] == "slack_guard_partial"
    assert events[0][1]["history_cap"] == "on" and events[0][1]["guard"] == "off"
