"""pico_finops_agent.llm_log: one `llm_call` line per model call with the reason the model stopped.

Gemini's MALFORMED_FUNCTION_CALL ("Function call is empty") came back as an answer with no text and no tool call;
Pico keeps that reason only on a disabled tracing span, so the log said just `outcome "empty"`. These tests drive
the wrapper with LiteLLM's own response types, streaming and not.
"""
import asyncio
import json

import litellm
import pytest
from litellm.types.utils import ChatCompletionDeltaToolCall, Delta, Function, StreamingChoices

import pico.models  # noqa: F401 - Pico patches the real litellm.acompletion first, never a test's fake
from pico_finops_agent import llm_log

REQUEST = {"model": "gemini/gemini-3.8-flash",
           "messages": [{"role": "system", "content": "rules"}, {"role": "user", "content": "What did EC2 cost?"}],
           "tools": [{"type": "function", "function": {"name": "execute_sql", "parameters": {}}},
                     {"type": "function", "function": {"name": "bash", "parameters": {}}}],
           "api_key": "AIzaSECRET"}


def chunk(content=None, tool=None, finish=None, usage=None):
    calls = None
    if tool:
        calls = [ChatCompletionDeltaToolCall(id="c1", type="function", index=0,
                                             function=Function(name=tool, arguments="{}"))]
    resp = litellm.ModelResponseStream(choices=[StreamingChoices(delta=Delta(content=content, tool_calls=calls),
                                                                 finish_reason=finish)])
    if usage:
        resp.usage = litellm.Usage(**usage)
    return resp


class FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.extra = "kept"

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """llm_log installed over a fake litellm.acompletion; returns (logs, set_reply)."""
    logs = []
    state = {}

    async def fake_acompletion(*args, **kwargs):
        state["kwargs"] = kwargs
        reply = state["reply"]
        if isinstance(reply, Exception):
            raise reply
        return FakeStream(reply) if kwargs.get("stream") else reply

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    llm_log.install(log=lambda event, **f: logs.append((event, f)), save_dir=None)

    def set_reply(reply):
        state["reply"] = reply

    return logs, set_reply, state


def run_stream(**extra):
    async def go():
        resp = await litellm.acompletion(**REQUEST, stream=True, **extra)
        return [c async for c in resp], resp

    return asyncio.run(go())


def test_streamed_malformed_call_is_logged_with_its_reason(wired):
    logs, set_reply, _ = wired
    set_reply([chunk(finish="malformed_function_call", usage={"prompt_tokens": 16482, "completion_tokens": 0})])
    run_stream()
    assert [e for e, _ in logs] == ["llm_call"]
    f = logs[0][1]
    assert f["finish"] == "malformed_function_call"
    assert f["empty"] is True
    assert f["prompt_tokens"] == 16482 and f["output_tokens"] == 0
    assert f["tools_called"] == [] and f["text_chars"] == 0
    assert f["model"] == "gemini/gemini-3.8-flash"


def test_streamed_tool_call_is_not_empty(wired):
    logs, set_reply, _ = wired
    set_reply([chunk(tool="execute_sql"), chunk(finish="stop", usage={"prompt_tokens": 100, "completion_tokens": 9})])
    got, resp = run_stream()
    assert len(got) == 2  # the caller still gets every chunk
    assert resp.extra == "kept"  # and the stream's own attributes
    f = logs[0][1]
    assert f["finish"] == "stop" and f["tools_called"] == ["execute_sql"] and f["empty"] is False


def test_non_streamed_answer(wired):
    logs, set_reply, _ = wired
    set_reply(litellm.ModelResponse(choices=[{"message": {"role": "assistant", "content": "EC2 was $72.80"},
                                              "finish_reason": "stop"}],
                                    usage={"prompt_tokens": 50, "completion_tokens": 7, "total_tokens": 57}))
    resp = asyncio.run(litellm.acompletion(**REQUEST))
    assert resp.choices[0].message.content == "EC2 was $72.80"
    f = logs[0][1]
    assert f["finish"] == "stop" and f["text_chars"] == 14 and f["empty"] is False and f["output_tokens"] == 7


def test_a_failed_call_is_logged_and_still_raises(wired):
    logs, set_reply, _ = wired
    set_reply(litellm.RateLimitError("429 quota for key AIzaSECRET", llm_provider="gemini", model="m"))
    with pytest.raises(litellm.RateLimitError):
        asyncio.run(litellm.acompletion(**REQUEST))
    f = logs[0][1]
    assert f["finish"] == "exception" and f["error_type"] == "RateLimitError"


def test_log_lines_never_hold_prompt_answer_or_key(wired):
    logs, set_reply, _ = wired
    set_reply([chunk(content="EC2 was $72.80"), chunk(finish="stop")])
    run_stream()
    text = json.dumps(logs)
    for secret in ("What did EC2 cost", "EC2 was", "rules", "AIzaSECRET"):
        assert secret not in text


def test_calls_inside_a_question_are_recorded_for_the_guard(wired):
    logs, set_reply, _ = wired
    rec = llm_log.Record("q1")

    async def question():
        with llm_log.recording(rec):
            set_reply([chunk(finish="malformed_function_call")])
            await asyncio.ensure_future(_consume())  # a child task, like Pico's chat task
            rec.attempt = 2
            set_reply([chunk(tool="execute_sql"), chunk(finish="stop")])
            await _consume()

    async def _consume():
        resp = await litellm.acompletion(**REQUEST, stream=True)
        async for _ in resp:
            pass

    asyncio.run(question())
    assert rec.finishes == ["malformed_function_call", "stop"]
    assert [(f["qid"], f["attempt"]) for _, f in logs] == [("q1", 1), ("q1", 2)]


def test_outside_a_question_qid_is_none(wired):
    logs, set_reply, _ = wired
    set_reply([chunk(finish="stop")])
    run_stream()
    assert logs[0][1]["qid"] is None


def test_install_is_idempotent(wired):
    first = litellm.acompletion
    llm_log.install(log=lambda *a, **k: None)
    assert litellm.acompletion is first


def test_failed_call_request_saved_only_when_asked(monkeypatch, tmp_path):
    saved = tmp_path / "llm-failures"

    async def fake_acompletion(*args, **kwargs):
        return FakeStream([chunk(finish="malformed_function_call")])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    llm_log.install(log=lambda *a, **k: None, save_dir=str(saved), keep=2)

    async def go():
        for _ in range(3):
            resp = await litellm.acompletion(**REQUEST, stream=True)
            async for _ in resp:
                pass

    asyncio.run(go())
    files = sorted(saved.iterdir())
    assert len(files) == 2  # only the newest `keep`
    data = json.loads(files[-1].read_text())
    assert data["finish"] == "malformed_function_call"
    assert data["messages"] == REQUEST["messages"]
    assert data["tools"] == ["execute_sql", "bash"]  # names only
    assert "AIzaSECRET" not in files[-1].read_text()


def test_good_calls_are_never_saved(monkeypatch, tmp_path):
    saved = tmp_path / "llm-failures"

    async def fake_acompletion(*args, **kwargs):
        return FakeStream([chunk(tool="execute_sql"), chunk(finish="stop")])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    llm_log.install(log=lambda *a, **k: None, save_dir=str(saved))

    async def go():
        resp = await litellm.acompletion(**REQUEST, stream=True)
        async for _ in resp:
            pass

    asyncio.run(go())
    assert not saved.exists() or not list(saved.iterdir())


def test_save_dir_from_env():
    assert llm_log.save_dir_from_env({}, "/data") is None
    assert llm_log.save_dir_from_env({"AGENT_SAVE_FAILED_CALLS": "0"}, "/data") is None
    assert llm_log.save_dir_from_env({"AGENT_SAVE_FAILED_CALLS": "1"}, "/data") == "/data/llm-failures"



def test_a_cancelled_stream_is_logged(wired):
    logs, set_reply, _ = wired

    class Hangs(FakeStream):
        async def __anext__(self):
            await asyncio.sleep(30)

    async def go():
        set_reply([])
        resp = await litellm.acompletion(**REQUEST, stream=True)
        resp._inner = Hangs([])
        task = asyncio.ensure_future(resp.__anext__())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert logs[0][1]["finish"] == "cancelled"


def test_a_stream_closed_early_is_logged_once(wired):
    logs, set_reply, _ = wired
    set_reply([chunk(tool="execute_sql"), chunk(finish="stop")])

    async def go():
        resp = await litellm.acompletion(**REQUEST, stream=True)
        await resp.__anext__()
        await resp.aclose()
        await resp.aclose()

    asyncio.run(go())
    assert len(logs) == 1 and logs[0][1]["tools_called"] == ["execute_sql"]


def test_usage_none_does_not_erase_earlier_counts(wired):
    logs, set_reply, _ = wired
    set_reply([chunk(finish="stop", usage={"prompt_tokens": 42, "completion_tokens": 3}), chunk()])
    run_stream()
    assert logs[0][1]["prompt_tokens"] == 42


def test_failed_call_files_are_private_and_safely_named(monkeypatch, tmp_path):
    import os
    import stat

    saved = tmp_path / "llm-failures"

    async def fake_acompletion(*args, **kwargs):
        return FakeStream([chunk(finish="../../etc/x")])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    llm_log.install(log=lambda *a, **k: None, save_dir=str(saved))

    async def go():
        resp = await litellm.acompletion(**REQUEST, stream=True)
        async for _ in resp:
            pass

    asyncio.run(go())
    files = list(saved.iterdir())
    assert len(files) == 1 and files[0].parent == saved and ".." not in files[0].name
    if os.name == "posix":
        assert stat.S_IMODE(saved.stat().st_mode) == 0o700
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
