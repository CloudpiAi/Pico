"""A stalled model call is closed and tried once more (live 2026-11-13: no token for 177 s, then "That took too long").

The fake litellm.acompletion sends chunks after given delays; the stall limit is shortened to keep the tests fast."""

import asyncio

import litellm
import pytest

from pico_finops_agent import llm_stall

STALL = 0.2


class FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)  # (delay seconds, chunk)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        delay, chunk = self._chunks.pop(0)
        await asyncio.sleep(delay)
        return chunk

    async def aclose(self):
        self.closed = True


@pytest.fixture
def wired(monkeypatch):
    """llm_stall installed over a fake acompletion; returns (logs, replies, streams)."""
    logs, replies, streams = [], [], []

    async def fake_acompletion(*args, **kwargs):
        reply = replies.pop(0)
        if kwargs.get("stream"):
            s = FakeStream(reply)
            streams.append(s)
            return s
        await asyncio.sleep(reply[0])
        return reply[1]

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    llm_stall.install(log=lambda event, **f: logs.append((event, f)), seconds=STALL)
    return logs, replies, streams


def read_stream():
    async def go():
        resp = await litellm.acompletion(model="gemini/gemini-3.8-flash", messages=[], stream=True)
        return [c async for c in resp]

    return asyncio.run(go())


def test_a_normal_stream_is_unchanged(wired):
    logs, replies, _ = wired
    replies.append([(0, "a"), (0.05, "b"), (0.05, "c")])
    assert read_stream() == ["a", "b", "c"] and logs == []


def test_a_stall_before_the_first_chunk_is_retried_once(wired):
    logs, replies, streams = wired
    replies.append([(5, "never")])            # stalls
    replies.append([(0, "x"), (0, "y")])       # the retry answers
    assert read_stream() == ["x", "y"]
    assert streams[0].closed
    assert [(e, f["after"], f["retried"]) for e, f in logs] == [("llm_stalled", "request", True)]


def test_two_stalls_raise_a_timeout(wired):
    logs, replies, _ = wired
    replies += [[(5, "never")], [(5, "never")]]
    with pytest.raises(litellm.Timeout):
        read_stream()
    assert [f["retried"] for _, f in logs] == [True, False]


def test_a_stall_after_part_of_the_answer_is_not_retried(wired):
    """The caller already has part of the answer: trying again could repeat it, so the run ends with an error."""
    logs, replies, streams = wired
    replies.append([(0, "part"), (5, "never")])
    got = []

    async def go():
        resp = await litellm.acompletion(model="m", messages=[], stream=True)
        async for c in resp:
            got.append(c)

    with pytest.raises(litellm.Timeout):
        asyncio.run(go())
    assert got == ["part"] and streams[0].closed and len(streams) == 1
    assert logs[0][1]["after"] == "a_later_chunk" and logs[0][1]["retried"] is False


def test_a_slow_non_streamed_call_is_retried_once(wired):
    logs, replies, _ = wired
    replies += [(5, "late"), (0, "answer")]
    assert asyncio.run(litellm.acompletion(model="m", messages=[])) == "answer"
    assert logs[0][1]["retried"] is True


@pytest.mark.parametrize("raw,expected", [("", 45), ("30", 30), ("9", 45), ("171", 45), ("abc", 45), ("170", 170)])
def test_stall_seconds_setting(raw, expected):
    assert llm_stall.stall_seconds({"AGENT_MODEL_STALL_S": raw}) == expected


def test_install_is_idempotent(wired):
    first = litellm.acompletion
    llm_stall.install(log=lambda *a, **k: None, seconds=STALL)
    assert litellm.acompletion is first
