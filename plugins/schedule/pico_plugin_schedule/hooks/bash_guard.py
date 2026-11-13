"""No variable or command expansion in a Pico agent's allow-listed bash commands.

Installed by the host agent (the standalone: pico_finops_agent.agent) and by the `ask` child (hooks/ask_child.py).
Pico runs an allowed command as `bash -c <command>` with the process environment, so `pico schedule add --text
"$SLACK_BOT_TOKEN"` would reach the command with the token already in it. install() wraps BashTool._is_command_allowed: when an allow-list
restricts the tool (not Pico's ["*"] agent nodes), a `$` or a backtick outside single quotes - the only place bash
leaves them alone - refuses the command. `'over $200'` still works; the skills ask for single-quoted values.
A runtime wrapper: no change under pico/.

install() also makes Pico's metacharacter check (`; & < > |`, newline) quote-aware for `pico schedule` only: Pico
refuses them even inside quotes, so `--question 'If it was, report it; if not, stay quiet'` or `--sql '... >= ...'`
stopped the Slack answer (live 2026-11-13). Inside single quotes bash takes every character literally, so for a
`pico schedule` command the check runs on the text with each single-quoted span replaced by ''. Pico's strict check
stays for anything that isn't plain quoting: a backslash anywhere, `$'...'`, an unbalanced quote, a newline or
carriage return, a `#` outside single quotes, or a `$`/backtick that bash would expand outside single quotes;
double-quoted text is still checked; every other command is untouched. Patched where both of Pico's layers look the function up:
pico.tools.permission.bash_rules (the permission ceiling) and pico.tools.func_tool.bash_tool (the allow-list).

install() also logs every command Pico's permission check refuses (log_denials): such a refusal stops the whole
answer ("Sorry, I couldn't answer that just now" in Slack) and Pico doesn't name the command (live 2026-11-12). One JSON
line `{"event": "bash_denied", "command": ..., "reason": ...}`, with the signed --ctx value replaced by <ctx>.
"""
import json
import re

_MARK = "pico_finops_agent_bash_guard"
_QUOTE_MARK = "pico_schedule_quote_aware_metachars"
_SCHEDULE = re.compile(r"\s*pico\s+schedule(?:\s|$)")
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


def single_quoted_removed(command):
    """`command` with each single-quoted span replaced by '' (bash takes its content literally), or None when the
    quoting is anything but plain: a backslash anywhere, ANSI-C `$'...'`, an unbalanced quote, any newline or
    carriage return, or a `#` outside single quotes. Bash ignores quotes inside a `#` comment, so `#'` opens no
    quote there and the next line runs (security review 2026-11-13: `pico schedule add x #'<newline>rm -rf /
    <newline>#'` looked like one quoted argument); one line and no `#` keeps every quote where bash sees it."""
    if "\\" in command or "\n" in command or "\r" in command:
        return None
    out = []
    in_single = in_double = False
    for i, c in enumerate(command):
        if in_single:
            if c == "'":
                in_single = False
                out.append("''")
            continue
        if c == "'" and not in_double:  # inside double quotes a ' is an ordinary character
            if i and command[i - 1] == "$":
                return None
            in_single = True
            continue
        if c == '"':
            in_double = not in_double
        out.append(c)
    if in_single or in_double:
        return None
    outside = "".join(out)
    if "#" in outside:
        return None
    return outside


def quote_aware(strict):
    """Pico's contains_shell_metachars, judged outside single quotes for `pico schedule` commands."""
    if getattr(strict, _QUOTE_MARK, False):
        return strict

    def contains_shell_metachars(text):
        # No relaxation when bash would expand anything outside single quotes: inside $[ ], $(( )) or ${ } a single
        # quote doesn't quote, so `$[ '$(id)' ]` would run id (security review 2026-11-13, second finding).
        if isinstance(text, str) and _SCHEDULE.match(text) and not expands_outside_single_quotes(text):
            outside = single_quoted_removed(text)
            if outside is not None:
                return strict(outside)
        return strict(text)

    setattr(contains_shell_metachars, _QUOTE_MARK, True)
    contains_shell_metachars.__wrapped__ = strict
    return contains_shell_metachars


def install():
    from pico.tools.func_tool import bash_tool
    from pico.tools.func_tool.bash_tool import BashTool
    from pico.tools.permission import bash_rules
    from pico.tools.permission.permission_hooks import PermissionHooks

    log_denials(PermissionHooks)
    for module in (bash_rules, bash_tool):  # each looks the function up in its own module globals
        module.contains_shell_metachars = quote_aware(module.contains_shell_metachars)
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
