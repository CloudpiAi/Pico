"""A bash command Pico's permission check refuses is logged (live 2026-11-12: three Slack answers stopped with
"Sorry, I couldn't answer that just now", and neither Pico's log nor the session named the refused command).

bash_guard.log_denials wraps a hooks class's `_handle_bash_permission`: when it raises PermissionDeniedException, one
JSON line `{"event": "bash_denied", "command": ..., "reason": ...}` is printed and the exception goes on unchanged.
The signed `--ctx` value is replaced by <ctx>.
"""
import asyncio
import json

import pytest

from pico_plugin_schedule.hooks import bash_guard

pytest.importorskip("pico.tools.permission.permission_hooks")
from pico.tools.permission.permission_hooks import PermissionDeniedException  # noqa: E402

DENIED = ("PERMISSION_DENIED: Bash command requires confirmation (No bash command rule matched) but this flow runs "
          "non-interactively under the 'normal' profile. STOP retrying — surface the failure to the caller.")


def _hooks(command, raises=True):
    class Hooks:
        def _parse_tool_args(self, context):
            return {"command": command}

        async def _handle_bash_permission(self, context, tool_name, pattern_name):
            if raises:
                raise PermissionDeniedException(DENIED)
            return True

    bash_guard.log_denials(Hooks)
    return Hooks()


def _events(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]


def test_a_refused_command_is_logged_with_the_ctx_hidden_and_the_error_kept(capsys):
    hooks = _hooks("pico schedule --ctx 'D0CF2.U0BS.0.1794475104_105399.d68c575375b219936df17ee3e2e8eb17' remove t67")
    with pytest.raises(PermissionDeniedException):
        asyncio.run(hooks._handle_bash_permission(None, "bash", "bash"))
    (event,) = _events(capsys)
    assert event["event"] == "bash_denied"
    assert event["command"] == "pico schedule --ctx <ctx> remove t67"
    assert event["reason"] == "No bash command rule matched"
    assert "d68c5753" not in json.dumps(event)


def test_an_unquoted_or_equals_ctx_is_hidden_too(capsys):
    for cmd in ("pico schedule list --ctx D0.U0.0.1_1.abcdef", "pico schedule list --ctx=D0.U0.0.1_1.abcdef"):
        with pytest.raises(PermissionDeniedException):
            asyncio.run(_hooks(cmd)._handle_bash_permission(None, "bash", "bash"))
    assert [e["command"] for e in _events(capsys)] == ["pico schedule list --ctx <ctx>"] * 2


def test_an_allowed_command_logs_nothing(capsys):
    assert asyncio.run(_hooks("pico schedule list --ctx x", raises=False)._handle_bash_permission(None, "b", "b"))
    assert _events(capsys) == []


def test_a_long_command_is_cut(capsys):
    with pytest.raises(PermissionDeniedException):
        asyncio.run(_hooks("python3 -c '" + "x" * 2000 + "'")._handle_bash_permission(None, "bash", "bash"))
    (event,) = _events(capsys)
    assert len(event["command"]) <= 400


def test_log_denials_wraps_once():
    class Hooks:
        async def _handle_bash_permission(self, context, tool_name, pattern_name):
            return True

    bash_guard.log_denials(Hooks)
    first = Hooks._handle_bash_permission
    bash_guard.log_denials(Hooks)
    assert Hooks._handle_bash_permission is first


def test_install_wraps_pico_permission_hooks():
    from pico.tools.permission.permission_hooks import PermissionHooks

    bash_guard.install()
    assert getattr(PermissionHooks._handle_bash_permission, bash_guard._DENIAL_MARK, False)
