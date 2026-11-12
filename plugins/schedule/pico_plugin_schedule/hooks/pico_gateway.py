"""The signed "who is asking, and where" line in front of every Slack message.

The host (the standalone agent: pico_finops_agent.agent) calls install_context(data_dir) before it starts Pico's Slack
gateway. It wraps ChannelBridge.handle_message so each message starts with a signed `[slack ctx=<channel>.<user>.<sig>]` line
(any such line the user typed is removed first). Scheduled tasks need it: `pico schedule ... --ctx <value>`
checks the signature, so "who asked" and "where" always come from Slack, never from the message text
(PRD R2, R16). No change to pico/.
"""
import os


def install_context(data_dir=None):
    """Wrap ChannelBridge.handle_message so each message carries the signed context line."""
    from pico_plugin_schedule import slack_ctx  # noqa: I001 - the plugin, then Pico's gateway

    from pico.gateway import bridge

    original = bridge.ChannelBridge.handle_message
    if getattr(original, "pico_finops_agent_ctx", False):
        return
    data_dir = data_dir or os.environ.get("FOCUS_DATA_DIR", "/data")
    key = slack_ctx.load_or_create_key(data_dir)

    async def handle_message(self, msg, adapter, channel_config=None):
        if msg.sender_id and msg.conversation_id:
            # The same conversation key the bridge's session uses: in a channel, a top-level message starts its own
            # thread (thread_id = message_id, pico/gateway/bridge.py), and replies in it share that session. So a
            # reply by anyone makes the top-level line stale. DMs are one conversation ("").
            thread = msg.thread_id or (msg.message_id if msg.chat_type == "group" else "")
            # newest message of this conversation: an older ctx in the same thread stops working
            slack_ctx.remember_latest(data_dir, msg.conversation_id, thread, msg.message_id, msg.sender_id)
            msg = msg.model_copy(update={"text": slack_ctx.with_context(key, msg.conversation_id, msg.sender_id,
                                                                        msg.text, thread, msg.message_id)})
        return await original(self, msg, adapter, channel_config)

    handle_message.pico_finops_agent_ctx = True
    bridge.ChannelBridge.handle_message = handle_message
