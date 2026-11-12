"""No variable or command expansion in a Pico agent's allow-listed bash commands.

Installed by the host agent (the standalone: pico_finops_agent.agent) and by the `ask` child (hooks/ask_child.py).
Pico runs an allowed command as `bash -c <command>` with the process environment, so `pico schedule add --text
"$SLACK_BOT_TOKEN"` would reach the command with the token already in it. install() wraps BashTool._is_command_allowed: when an allow-list
restricts the tool (not Pico's ["*"] agent nodes), a `$` or a backtick outside single quotes - the only place bash
leaves them alone - refuses the command. `'over $200'` still works; the skills ask for single-quoted values.
A runtime wrapper: no change under pico/.

install() also logs every command Pico's permission check refuses (log_denials): such a refusal stops the whole
answer ("Sorry, I couldn't answer that just now" in Slack) and Pico doesn't name the command (live 2026-11-12). One JSON
line `{"event": "bash_denied", "command": ..., "reason": ...}`, with the signed --ctx value replaced by <ctx>.
"""
import json
import re

_MARK = "pico_finops_agent_bash_guard"
_DENIAL_MARK = "pico_finops_agent_bash_denial_log"
_CTX = re.compile(r"(--ctx)(?:=|\s+)('[^']*'|\"[^\"]*\"|\S+)")
_REASON = re.compile(r"\(([^()]*)\)")
MAX_LOGGED = 400


def _redacted(command):
    return _CTX.sub(r"\1 <ctx>", command)[:MAX_LOGGED]


def log_denials(cls):
    """Wrap cls._handle_bash_permission: a PermissionDeniedException is logged, then raised unchanged."""
    original = cls._handle_bash_permission
    if getattr(original, _DENIAL_MARK, False):
        return

    async def logged(self, context, tool_name, pattern_name):
        try:
            return await original(self, context, tool_name, pattern_name)
        except Exception as e:
            if type(e).__name__ == "PermissionDeniedException":
                try:
                    args = self._parse_tool_args(context)
                    command = args.get("command") if isinstance(args, dict) else None
                except Exception:  # noqa: BLE001 - logging must never change the outcome
                    command = None
                found = _REASON.search(str(e))
                print(json.dumps({"event": "bash_denied", "command": _redacted(str(command or "")),
                                  "reason": found.group(1) if found else str(e)[:200]}), flush=True)
            raise

    setattr(logged, _DENIAL_MARK, True)
    cls._handle_bash_permission = logged


def expands_outside_single_quotes(command):
    """True when bash would expand something in `command`: a `$` or a backtick that is not inside single quotes
    (a backslash-escaped one outside single quotes is literal)."""
    in_single = in_double = False
    i = 0
    while i < len(command):
        c = command[i]
        if in_single:
            if c == "'":
                in_single = False
        elif c == "\\":
            i += 2
            continue
        elif c == "'" and not in_double:
            in_single = True
        elif c == '"':
            in_double = not in_double
        elif c in "$`":
            return True
        i += 1
    return False


def install():
    from pico.tools.func_tool.bash_tool import BashTool
    from pico.tools.permission.permission_hooks import PermissionHooks

    log_denials(PermissionHooks)
    original = BashTool._is_command_allowed
    if getattr(original, _MARK, False):
        return

    def guarded(self, command):
        restricted = bool(self.allowed_patterns) and "*" not in self.allowed_patterns
        if restricted and expands_outside_single_quotes(command):
            return False
        return original(self, command)

    setattr(guarded, _MARK, True)
    BashTool._is_command_allowed = guarded
