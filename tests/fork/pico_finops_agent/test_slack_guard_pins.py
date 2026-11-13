"""Pins: every Pico internal pico_finops_agent/slack_guard.py relies on.

slack_guard wraps Pico at install time instead of editing pico/ (project rule). These checks fail loudly when an
upstream sync moves or renames one of the seams, so the wrappers never silently stop working.
"""
import dataclasses
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "plugins" / "schedule"))


# ---------- the Slack path: ChannelBridge ----------

def test_bridge_handle_message_signature_and_flow():
    from pico.gateway.bridge import ChannelBridge

    fn = ChannelBridge.handle_message
    assert inspect.iscoroutinefunction(fn)
    assert list(inspect.signature(fn).parameters) == ["self", "msg", "adapter", "channel_config"]
    src = inspect.getsource(fn)
    # dedup by channel + message id: a retry needs a new message id
    assert 'dedup_key = f"{msg.channel_id}_{msg.message_id}"' in src
    # a top-level channel message becomes its own thread (the retry keeps that thread)
    assert 'msg.model_copy(update={"thread_id": msg.message_id})' in src
    # the texts the guard recognises on the way out
    assert '"(No response generated)"' in src
    assert 'f"\\u274c Error: {error_text}" if error_text else "\\u274c An error occurred."' in src
    assert '"The previous request is still being processed. Please wait."' in src
    # reactions on the user's message: processing, then done/error
    assert "adapter.add_reaction(msg.conversation_id, msg.message_id, PROCESSING_EMOJI, msg.thread_id)" in src
    assert "adapter.remove_reaction(msg.conversation_id, msg.message_id, PROCESSING_EMOJI, msg.thread_id)" in src
    assert "done_emoji = ERROR_EMOJI if has_error else DONE_EMOJI" in src
    # all sending goes through the adapter argument (the guard passes a proxy)
    assert "self._task_manager.start_chat(" in src and "self._task_manager.consume_events(task)" in src
    assert "await adapter.send_message(" in src and "await adapter.finalize_stream(stream_id)" in src
    # group messages without a mention: ignored unless the thread already has a session
    assert 'msg.chat_type == "group" and not msg.mentions_bot' in src
    assert "session_mgr.session_exists(session_id)" in src


def test_bridge_session_id_clear_and_task_manager():
    from pico.gateway.bridge import ChannelBridge

    assert '"--".join(parts)' in inspect.getsource(ChannelBridge._conversation_key)
    assert 'f"gateway_{conv_key}"' in inspect.getsource(ChannelBridge.build_session_id)
    clear = inspect.getsource(ChannelBridge.clear_session)
    assert "self.build_session_id(msg)" in clear and "session_mgr.clear_session(session_id)" in clear
    init = inspect.getsource(ChannelBridge.__init__)
    assert "self._task_manager = task_manager" in init and "self._agent_config = agent_config" in init


def test_new_session_command_is_bridge_clear_session():
    from pico.gateway.commands import NewSessionCommand

    assert "ctx.bridge.clear_session(ctx.msg)" in inspect.getsource(NewSessionCommand.execute)


def test_session_manager_api():
    from pico.models.session_manager import SessionManager

    for name in ("session_exists", "clear_session"):
        assert callable(getattr(SessionManager, name))
    assert "session_dir" in inspect.signature(SessionManager.__init__).parameters


def test_unanswered_turn_rollback_api():
    """The guard takes an unanswered attempt back out with Pico's own turn checkpoint (the CLI's mechanism)."""
    from pico.models.session_manager import SessionManager, SessionTurnCheckpoint

    assert list(inspect.signature(SessionManager.checkpoint_turn).parameters) == ["self", "session_id"]
    assert list(inspect.signature(SessionManager.rollback_turn).parameters) == ["self", "session_id", "checkpoint"]
    assert not inspect.iscoroutinefunction(SessionManager.rollback_turn)  # run with asyncio.to_thread
    src = inspect.getsource(SessionManager.rollback_turn)
    # both sides of a turn go (pop_item would leave message_structure behind), and a None checkpoint is a no-op
    assert "DELETE FROM message_structure" in src and "DELETE FROM agent_messages" in src
    assert "if checkpoint is None:" in src
    assert {f.name for f in dataclasses.fields(SessionTurnCheckpoint)} >= {"max_message_id", "max_sequence_number"}


def test_inbound_message_fields_and_emojis():
    from pico.gateway import models

    fields = models.InboundMessage.model_fields
    for name in ("channel_id", "sender_id", "conversation_id", "message_id", "text", "mentions_bot", "chat_type",
                 "thread_id"):
        assert name in fields, name
    for name in ("channel_id", "conversation_id", "thread_id", "text"):
        assert name in models.OutboundMessage.model_fields, name
    for name in ("PROCESSING_EMOJI", "DONE_EMOJI", "ERROR_EMOJI"):
        assert isinstance(getattr(models, name), str), name


def test_adapter_methods_the_proxy_forwards():
    from pico.gateway.channel.base import ChannelAdapter

    for name in ("send_message", "add_reaction", "remove_reaction", "finalize_stream"):
        assert inspect.iscoroutinefunction(getattr(ChannelAdapter, name)), name
    assert list(inspect.signature(ChannelAdapter.add_reaction).parameters) == [
        "self", "conversation_id", "message_id", "emoji", "thread_id"]
    assert isinstance(ChannelAdapter.supports_streaming, property)


# ---------- the task manager: turn usage, stop ----------

def test_chat_task_manager_usage_and_stop():
    from pico.api.models.cli_models import IMessageContent, SSEEndData, SSEErrorData, SSEMessageData
    from pico.api.services.chat_task_manager import ChatTask, ChatTaskManager

    for name in ("requests", "input_tokens", "output_tokens"):
        assert name in SSEEndData.model_fields, name
    assert "error_type" in SSEErrorData.model_fields
    assert "payload" in SSEMessageData.model_fields and "payload" in IMessageContent.model_fields
    assert "self.events" in inspect.getsource(ChatTask.__init__)
    assert list(inspect.signature(ChatTaskManager.get_task).parameters)[:2] == ["self", "session_id"]
    assert inspect.iscoroutinefunction(ChatTaskManager.stop_task)
    assert "task.asyncio_task.cancel()" in inspect.getsource(ChatTaskManager.stop_task)
    loop = inspect.getsource(ChatTaskManager._run_loop)
    assert "turn_usage = await node.get_last_turn_usage()" in loop and 'event="end"' in loop
    # finished tasks stay readable for a while (the guard reads their events after the bridge returns)
    assert "self._completed_tasks[session_id] = task" in loop
    # the on-disk (and SDK) session id is the bridge's gateway_... id
    assert "session_id=session_id if session_id is not None else node_id" in inspect.getsource(
        ChatTaskManager._create_node)


def test_bridge_counts_tool_calls_by_call_tool_id():
    from pico.gateway.bridge import ChannelBridge

    src = inspect.getsource(ChannelBridge._event_to_outbound)
    assert 'content_type == "call-tool"' in src and 'payload.get("callToolId", "")' in src


# ---------- the model call: per-call input filter ----------

def test_build_run_config_seam():
    from agents.run import CallModelData, ModelInputData, RunConfig

    from pico.models.base import LLMBaseModel

    params = inspect.signature(LLMBaseModel._build_run_config).parameters
    assert "session" in params
    src = inspect.getsource(LLMBaseModel._build_run_config)
    assert "call_model_input_filter=_filter" in src and "return None" in src
    assert dataclasses.is_dataclass(RunConfig)
    assert "call_model_input_filter" in {f.name for f in dataclasses.fields(RunConfig)}
    assert list(inspect.signature(ModelInputData).parameters) == ["input", "instructions"]
    assert "model_data" in inspect.signature(CallModelData).parameters
    # no provider overrides it, so patching the base class covers them all
    from pico.models import codex_model, openai_compatible

    assert "_build_run_config" not in vars(openai_compatible.OpenAICompatibleModel)
    assert "_build_run_config" not in vars(codex_model.CodexModel)


def test_openai_compatible_passes_the_session_and_run_config():
    from pico.models import openai_compatible

    src = inspect.getsource(openai_compatible)
    assert "run_config = self._build_run_config(\n                    pending_input_queue=pending_input_queue,\n" \
           "                    session=session," in src
    assert "run_config=run_config" in src


def test_sdk_session_has_session_id():
    from agents import SQLiteSession

    assert "self.session_id" in inspect.getsource(SQLiteSession.__init__)


def test_compaction_resume_marker():
    from pico.utils import message_utils

    assert message_utils.COMPACT_RESUME_MARKER == "[PICO_COMPACT_RESUME]"
    assert message_utils.is_compact_resume_text(f"  {message_utils.COMPACT_RESUME_MARKER} x")


def test_chat_task_status_and_asyncio_task():
    from pico.api.services.chat_task_manager import ChatTask

    src = inspect.getsource(ChatTask.__init__)
    assert 'self.status: str = "running"' in src and "self.asyncio_task = asyncio_task" in src


# ---------- config and the ctx line ----------

def test_chat_node_reads_max_turns_from_agentic_nodes_chat():
    from pico.agent.node.chat_agentic_node import ChatAgenticNode

    src = inspect.getsource(ChatAgenticNode.__init__)
    assert 'agent_config.agentic_nodes["chat"]' in src and 'agentic_node_config.get("max_turns", 50)' in src


def test_ctx_line_accepts_the_retry_message_id():
    """The schedule plugin signs the message id into the ctx line; it accepts only digits and _ there."""
    from pico_finops_agent import slack_guard
    from pico_plugin_schedule import slack_ctx

    key = "k" * 64
    retry = slack_guard.retry_message_id("1731330000.123456")
    tag = slack_ctx.make_tag(key, "D0AAA00001", "U0AAA00001", "", retry)
    assert slack_ctx.verify(key, tag[len("[slack ctx="):-1]).msg == retry


def test_tool_summary_fallback_seam():
    """install_no_tool_summary_answer blanks ctx.last_tool_summary around Pico's own result builder."""
    from pico.agent.node.chat_agentic_node import ChatAgenticNode

    fn = ChatAgenticNode._build_success_result
    fn = getattr(fn, "__wrapped__", fn)
    assert list(inspect.signature(fn).parameters) == ["self", "ctx"]
    src = inspect.getsource(fn)
    assert "if not response_content and ctx.last_tool_summary:" in src
    assert "response_content = ctx.last_tool_summary" in src
    from pico.agent.node.stream_run_context import StreamRunContext

    assert "last_tool_summary" in {f.name for f in dataclasses.fields(StreamRunContext)}



def test_chat_run_inherits_the_guards_context():
    """The history mode and the llm_log record are contextvars: Pico must start the run as a task in the caller's
    context (asyncio.create_task copies it), not on another thread or loop."""
    from pico.api.services.chat_task_manager import ChatTaskManager

    src = inspect.getsource(ChatTaskManager.start_chat)
    assert "asyncio.create_task(" in src
    assert "run_in_executor" not in src and "contextvars.Context(" not in src and "new_event_loop" not in src


def test_ctx_line_accepts_every_retry_id():
    from pico_finops_agent import slack_guard
    from pico_plugin_schedule import slack_ctx

    key = "k" * 64
    second = slack_guard.retry_message_id("1731330000.123456")
    third = slack_guard.retry_message_id(second)  # attempt 3 since the retries escalate
    for retry in (second, third):
        tag = slack_ctx.make_tag(key, "D0AAA00001", "U0AAA00001", "", retry)
        assert slack_ctx.verify(key, tag[len("[slack ctx="):-1]).msg == retry
