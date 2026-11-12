"""What a scheduled task posts (PRD R9, R10, R18).

reminder  the saved text, as is (mentions removed unless FOCUS_REPORT_ALLOW_MENTIONS=true)
report    a fixed template from the host's data source (Source.read), optional filter; no LLM
query     a saved read-only query the data source runs (Source.read); no LLM
ask       `pico -p "<question>"` (Pico's one-shot mode, same profile), its final answer is posted. The child gets no
          Slack tokens and PICO_FINOPS_AGENT_UNATTENDED=1, so it can't post, load or manage tasks itself.
Mentions (@channel / @here / @everyone / user groups) are removed from everything except a reminder's own text,
which keeps them only when FOCUS_REPORT_ALLOW_MENTIONS=true.

Conditional tasks ("only if EC2 is over $200 ...", decided 2026-11-06): an `ask` whose answer is exactly
NOTHING_TO_SEND, or a `query` that returns no rows, produces no text (render returns None) and nothing is posted.

The figures come from the host's Source (pico_plugin_schedule.source); the FOCUS one is
pico_plugin_focus_data.schedule_source.FocusSource.

Data tasks start with "Data through <day>" (the newest full day the source has) and say "No new data since the last
report" when that day hasn't moved since the task's last send. Every post ends with who set it up and how to stop it.
"""
import json
import os
import re
import subprocess
import sys

from . import child, secret_guard

ASK_TIMEOUT_S = 600
_CHILD_ENV_DROP = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")
SKIP_TOKEN = "NOTHING_TO_SEND"
ASK_SUFFIX = ("\n\n(This is a scheduled check; the answer is posted in Slack. If the request has a condition "
              f"that is not met, or there is nothing to report, reply with exactly {SKIP_TOKEN} and nothing else.)")
_MENTION = re.compile(r"<!(channel|here|everyone)(\|[^>]*)?>|<!subteam\^[^>]+>")
_AT_MENTION = re.compile(r"(?<![\w<])@(channel|here|everyone)\b")


class RenderError(Exception):
    """The task couldn't produce its text (bad SQL after a schema change, the agent failed, ...). `spent`: the agent
    (an LLM) already ran for it, so a question still counts against the daily limit."""

    def __init__(self, message, spent=False):
        super().__init__(message)
        self.spent = spent


def _day_label(d):
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def sanitize_mentions(text, allow):
    if allow:
        return _AT_MENTION.sub(lambda m: f"<!{m.group(1)}>", text)
    return _AT_MENTION.sub(lambda m: m.group(1), _MENTION.sub(lambda m: (m.group(1) or "group"), text))


def footer(task):
    return f"_Set up by <@{task['owner']}> · task {task['id']} · say 'stop task {task['id']}' to stop it_"


def final_answer(lines):
    """The last top-level markdown text from `pico -p` output (MessagePayload JSON lines)."""
    answer = None
    for line in lines:
        try:
            msg = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(msg, dict) or msg.get("depth", 0) != 0:
            continue
        for part in msg.get("content") or []:
            text = (part.get("payload") or {}).get("content") if part.get("type") == "markdown" else None
            if text and text.strip():
                answer = text.strip()
    return answer


# A failed top-level run is printed as a depth-0 "thinking" part: "<node> interaction failed: <error>". The step limit is
# Pico's MODEL_MAX_TURNS_EXCEEDED ("Maximum turns (15) exceeded") or the Agents SDK's MaxTurnsExceeded ("Max turns (15)").
_TURN_LIMIT = re.compile(r"interaction failed:.*?\bMax(?:imum)? turns \((\d+)\) exceeded", re.S)


def turn_limit(lines):
    """The step limit (an int) when the top-level run stopped on it, else None. Lines that aren't the expected
    MessagePayload shape are skipped, never an error."""
    for line in lines:
        try:
            msg = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(msg, dict) or msg.get("depth", 0) != 0 or not isinstance(msg.get("content"), list):
            continue
        for part in msg["content"]:
            payload = part.get("payload") if isinstance(part, dict) else None
            found = _TURN_LIMIT.search(str(payload.get("content") or "")) if isinstance(payload, dict) else None
            if found:
                return int(found.group(1))
    return None


def _ask_env():
    """The ask child's environment: no secrets except the LLM keys (`*_API_KEY`) it needs to answer."""
    env = {k: v for k, v in os.environ.items()
           if k not in _CHILD_ENV_DROP and not (secret_guard.is_secret_name(k) and not k.upper().endswith("_API_KEY"))}
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = os.pathsep.join(p for p in (pkg_parent, env.get("PYTHONPATH")) if p)
    env.update(PICO_DISABLE_VERSION_CHECK="1", PICO_FINOPS_AGENT_UNATTENDED="1")
    return env


def _ask(question, runner):
    try:
        r = runner([sys.executable, "-m", "pico_plugin_schedule.hooks.ask_child", "-p", question + ASK_SUFFIX],
                   capture_output=True, text=True, timeout=ASK_TIMEOUT_S, env=_ask_env())
    except subprocess.TimeoutExpired as e:
        raise RenderError("the agent didn't answer within 10 minutes", spent=True) from e
    if r.returncode != 0:  # the error text stays in the agent log, never in Slack
        print(json.dumps({"event": "ask_failed", "rc": r.returncode, "stderr": (r.stderr or "")[-500:]}), flush=True)
        raise RenderError(f"the agent stopped with an error (exit {r.returncode}; details in the agent log)",
                          spent=True)
    lines = (r.stdout or "").splitlines()
    answer = final_answer(lines)
    if not answer:
        limit = turn_limit(lines)
        if limit:
            raise RenderError(f"the question needed more than {limit} steps; ask it more narrowly "
                              "(one service, one month or a top 10)", spent=True)
        raise RenderError("the agent gave no answer", spent=True)
    return None if answer.strip().strip("`*_. ") == SKIP_TOKEN else answer


def render(task, source, settings, now, runner=child.run):
    """(text to post, data_through ISO day or None)."""
    kind, payload = task["kind"], task["payload"]
    if kind == "reminder":
        text = sanitize_mentions(payload["text"], settings.allow_mentions)
        return f":alarm_clock: Reminder: {text}\n{footer(task)}", None
    clean = sanitize_mentions  # names, results and answers never ping anyone

    day, body = source.read(kind, payload)
    if kind == "ask":
        answer = _ask(payload["question"], runner)
        body = [answer] if answer is not None else None
    elif day is None:
        return (f"*{clean(task['name'], False)}*\nThe cost data is not loaded yet, so there's nothing to report. "
                f"I'll try again next time.\n{footer(task)}"), None

    if body is None:  # condition not met / no rows: send nothing
        return None, (day.isoformat() if day else None)
    head = [f"*{task['name']}*"]
    if day is not None:
        through = day.isoformat()
        head.append(f"_Data through {_day_label(day)}_")
        if task.get("last_data_through") == through:
            head.append("_No new data since the last report._")
    return clean("\n".join([*head, *body]), False) + "\n" + footer(task), (day.isoformat() if day else None)
