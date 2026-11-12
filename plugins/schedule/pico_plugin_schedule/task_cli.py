"""`pico schedule <action> ...`: what the agent runs to set up and manage scheduled tasks (PRD R1-R4,
R11-R17). Prints one JSON object: ok, code, message (the plain sentence to pass on) and the task / request / history.

Every action needs `--ctx <value>` from the signed `[slack ctx=...]` line of the current message, so "who" and
"where" come from Slack (PRD R16); a line from an older message of the same conversation is refused. A named channel
(`--to #name`) also needs the person to be in it. Runs started by a scheduled `ask` (PICO_FINOPS_AGENT_UNATTENDED=1)
can't manage tasks. Exit codes: 0 done or requested, 1 refused (the message says why).

  add      --kind reminder|report|query|ask --to me|here|#name|<channel ID> --name N (--cron "m h dom mon dow" |
           --at "YYYY-MM-DD HH:MM") [--tz Area/City] then --text | --template T [--service/--account/--region] |
           --sql | --question
  list | show ID | history ID | remove ID | pause ID | resume ID
  edit ID [--cron|--at] [--tz] [--to] [--name] [--text | --question | --sql | --template + filters]
  confirm RID | decline RID                  (only the task's owner; answers someone else's request)
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import kinds as task_kinds
from . import secret_guard, slack_api, slack_ctx, tasks


def slack_client():
    return slack_api.Client.from_env()


def _now():
    return datetime.now(timezone.utc)


def _out(ok, code, message, **extra):
    json.dump({"ok": ok, "code": code, "message": message, **extra}, sys.stdout, indent=2, default=str)
    print()
    return 0 if ok else 1


def _when(schedule, now):
    nxt = tasks.next_run(schedule, now)
    if nxt is None:
        return None
    local = nxt.astimezone(ZoneInfo(schedule.get("tz") or "UTC"))
    hour = f"{local:%I}".lstrip("0")
    return f"{local:%a} {local.day} {local:%b} {hour}:{local:%M %p} {local:%Z}"


def _is_channel(cid):
    return cid[:1] in ("C", "G")


def _target(to, channel, user, client, settings):
    if to in ("me", "", None):
        return {"type": "user", "id": user}
    if to == "here":
        return {"type": "channel", "id": channel} if _is_channel(channel) else {"type": "user", "id": user}
    found = client.resolve_channel(to)
    if not found:
        raise tasks.TaskError("channel_not_found", slack_api.MESSAGES["channel_not_found"])
    if found != channel and user not in settings.admins and not client.is_member(user, found):
        raise tasks.TaskError("not_a_member", slack_api.MESSAGES["not_a_member"])
    return {"type": "channel", "id": found}


_IN = re.compile(r"^\s*(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)\s*$", re.IGNORECASE)
_IN_HELP = "Give a delay like --in '10m' or --in '2h' (minutes or hours, at least 1 minute), or a time with --at."


def _in_to_at(a, tz, now):
    """--in '2m' / '1h': the --at time that far from now, in the task's time zone (the model has the date, not the
    time, so 'in 2 minutes' needs no clock; live 2026-11-12)."""
    if a.cron or a.at:
        raise tasks.TaskError("bad_in", "Use --in on its own, not with --cron or --at. " + _IN_HELP)
    found = _IN.match(a.in_ or "")
    if not found or int(found.group(1)) < 1:
        raise tasks.TaskError("bad_in", _IN_HELP)
    minutes = int(found.group(1)) * (60 if found.group(2).lower().startswith("h") else 1)
    return f"{(now + timedelta(minutes=minutes)).astimezone(ZoneInfo(tz or 'UTC')):%Y-%m-%d %H:%M}"


def _schedule(a, tz, now=None):
    if a.in_ is not None:
        return {"at": _in_to_at(a, tz, now or _now()), "tz": tz or "UTC"}
    sched = {"cron": a.cron} if a.cron else {"at": a.at}
    sched["tz"] = tz or "UTC"
    return sched


def _payload(a, kind):
    if kind == "reminder":
        return {"text": a.text}
    if kind == "query":
        if not a.source.supports_query:
            raise tasks.TaskError("bad_kind", "Saved queries aren't available here.")
        return {"sql": a.source.check_query(a.sql)}
    if kind == "ask":
        return {"question": a.question}
    flt = {k: v for k, v in (("service", a.service), ("account", a.account), ("region", a.region)) if v}
    return a.source.check_report({"template": a.template, **({"filter": flt} if flt else {})})


def _where_text(where):
    return f"<#{where}>" if _is_channel(where) else "a direct message"


def _tell(client, target, text):
    """Best effort: a failed notice never fails the command."""
    try:
        client.post(target, text)
        return True
    except slack_api.SlackError:
        return False


_VERB = {"remove": "stop", "pause": "pause", "resume": "resume", "edit": "change"}
_CONTENT = {"reminder": "text (--text)", "ask": "question (--question)", "query": "SQL (--sql)",
            "report": "report (--template)"}


def _describe(changes):
    """What a change request would do, for the owner's DM (so a "yes" is never blind)."""
    if not changes:
        return ""
    parts = []
    if "schedule" in changes:
        s = changes["schedule"]
        parts.append(f"time to `{s.get('cron') or s.get('at')}` ({s.get('tz')})")
    if "target" in changes:
        t = changes["target"]
        parts.append("place to " + (f"<#{t['id']}>" if t["type"] == "channel" else f"<@{t['id']}>'s DM"))
    if "name" in changes:
        parts.append(f"name to \"{changes['name']}\"")
    if "payload" in changes:
        parts.append("content to: " + json.dumps(changes["payload"])[:300])
    return ": " + "; ".join(parts)


def _cmd_add(a, d, channel, user, settings, client, now):
    target = tasks.check_target(_target(a.to, channel, user, client, settings), settings, user)
    code = client.check_target(target)
    if code != "ok":
        return _out(False, code, slack_api.MESSAGES[code])
    tz = a.tz or client.user_tz(user) or "UTC"
    schedule = _schedule(a, tz, now)
    payload = tasks.check_payload(a.kind, _payload(a, a.kind))
    tasks.check_schedule(schedule, now)
    preview = None
    if a.kind in ("report", "query"):
        draft = {"id": "preview", "kind": a.kind, "name": a.name or a.kind, "owner": user, "payload": payload,
                 "last_data_through": None}
        try:
            preview, _ = task_kinds.render(draft, a.source, settings, now)
        except Exception as e:  # noqa: BLE001 - a query that fails now would fail every day
            return _out(False, "bad_sql", f"That query doesn't run on the cost data: {str(e)[:200]}")
    task = tasks.add(d, owner=user, where=channel, kind=a.kind, name=a.name, target=target, schedule=schedule,
                     payload=payload, now=now, settings=settings)
    when = _when(schedule, now)
    place = "you by DM" if target["type"] == "user" else f"<#{target['id']}>"
    repeat = "once" if "at" in schedule else "repeating"
    return _out(True, "ok", f"Saved task {task['id']} ({repeat}) for {place}. First run: {when}.",
                task=task, next_run=when, preview=preview)


def _cmd_change(a, d, channel, user, settings, client, now):
    changes = None
    if a.action == "edit":
        current = tasks.get(d, a.id)
        changes = {}
        if a.cron or a.at or a.in_ is not None:
            changes["schedule"] = _schedule(a, a.tz or current["schedule"].get("tz"), now)
        elif a.tz:
            changes["schedule"] = {**current["schedule"], "tz": a.tz}
        if a.to:
            changes["target"] = _target(a.to, channel, user, client, settings)
            code = client.check_target(changes["target"])
            if code != "ok":
                return _out(False, code, slack_api.MESSAGES[code])
        if a.name:
            changes["name"] = a.name
        given = {"reminder": a.text, "ask": a.question, "query": a.sql, "report": a.template}[current["kind"]]
        if given:
            changes["payload"] = _payload(a, current["kind"])
        if not changes:
            return _out(False, "nothing_to_change", "Tell me what to change: the time, the place, the name or the "
                                                    f"{_CONTENT[current['kind']]}.")
    r = tasks.change(d, a.id, a.action, by=user, where=channel, changes=changes, now=now, settings=settings)
    task = r["task"]
    if r["result"] == "requested":
        req = r["request"]
        _tell(client, {"type": "user", "id": task["owner"]},
              f"<@{user}> asked to {_VERB[a.action]} your task *{task['name']}* ({task['id']}) in "
              f"{_where_text(channel)}{_describe(changes)}. Reply \"yes {req['id']}\" to agree or "
              f"\"no {req['id']}\" to keep it as it is.")
        return _out(True, "confirmation_requested",
                    f"<@{task['owner']}> set up this task, so I've asked them to confirm. "
                    "It stays as it is until they do.", task=task, request=req)
    nxt = _when(task["schedule"], now) if task["status"] == "active" else None
    return _out(True, "ok", f"Done: task {task['id']} is now {task['status']}"
                + (f", next run {nxt}." if nxt else "."), task=task, next_run=nxt)


def _cmd_resolve(a, d, _channel, user, settings, client, now):
    r = tasks.resolve(d, a.id, by=user, accept=a.action == "confirm", now=now, settings=settings)
    req, task = r["request"], r["task"]
    if r["result"] == "gone":
        return _out(True, "gone", "That task was already stopped, so there was nothing to do.", request=req)
    verb = _VERB[req["action"]]
    told = (f"<@{user}> agreed to {verb} task *{task['name']}* ({task['id']})." if r["result"] == "confirmed"
            else f"<@{user}> kept task *{task['name']}* ({task['id']}) as it is.")
    place = {"type": "channel", "id": req["where"]} if _is_channel(req["where"]) else {"type": "user", "id": req["by"]}
    _tell(client, place, f"<@{req['by']}> {told}" if place["type"] == "channel" else told)
    return _out(True, r["result"], told, task=task, request=req)


def run(argv, data_dir, source):
    p = argparse.ArgumentParser(prog="pico schedule")
    p.add_argument("action", choices=["add", "list", "show", "history", "remove", "pause", "resume", "edit",
                                      "confirm", "decline"])
    p.add_argument("id", nargs="?")
    p.add_argument("--ctx", required=True)
    p.add_argument("--kind", choices=tasks.KINDS)
    p.add_argument("--to")
    p.add_argument("--name")
    p.add_argument("--cron")
    p.add_argument("--at")
    p.add_argument("--in", dest="in_")
    p.add_argument("--tz")
    for opt in ("--text", "--template", "--service", "--account", "--region", "--sql", "--question"):
        p.add_argument(opt)
    # `--in -5m`: argparse would read "-5m" as an option and exit without the JSON answer; join it so a bad delay
    # gets the bad_in message
    argv = list(argv)
    for i in range(len(argv) - 1):
        if argv[i] == "--in":
            argv[i:i + 2] = [f"--in={argv[i + 1]}"]
            break
    a = p.parse_args(argv)
    a.source = source
    d = os.path.abspath(data_dir)
    now = _now()
    settings = tasks.Settings.from_env()
    if os.environ.get("PICO_FINOPS_AGENT_UNATTENDED") == "1":
        return _out(False, "not_from_a_scheduled_run", "Scheduled questions can't set up or change tasks.")
    try:
        ctx = slack_ctx.verify(slack_ctx.load_or_create_key(d), a.ctx)
    except slack_ctx.BadContext:
        return _out(False, "bad_context", "I can only set up or change tasks from a Slack message.")
    if not slack_ctx.is_current(d, ctx):
        return _out(False, "stale_context", "That came from an earlier message. Please ask again in a new message.")
    channel, user = ctx.channel, ctx.user
    try:
        if a.action in ("show", "history", "remove", "pause", "resume", "edit", "confirm", "decline") and not a.id:
            return _out(False, "missing_id", "Which task? Give its id, e.g. t3.")
        if a.action == "list":
            return _out(True, "ok", "", tasks=tasks.list_for(d, user=user, channel=channel))
        if a.action == "show":
            task = tasks.get(d, a.id)
            return _out(True, "ok", f"Task {task['id']} was set up by <@{task['owner']}> on {task['created_at']}.",
                        task=task, next_run=_when(task["schedule"], now) if task["status"] == "active" else None)
        if a.action == "history":
            tasks.get(d, a.id)
            return _out(True, "ok", "", history=tasks.history(d, a.id))
        user_args = [v for k, v in vars(a).items() if isinstance(v, str) and k not in ("ctx", "action", "kind")]
        if a.action in ("add", "edit") and any(secret_guard.contains_secret(v) for v in user_args):
            return _out(False, "secret_in_text", "That text contains a password or key from this system, so I won't "
                                                 "save it. Please write the task without it.")
        client = slack_client()
        if a.action == "add":
            if not a.kind:
                return _out(False, "bad_kind", "Which kind: reminder, report, query or ask?")
            return _cmd_add(a, d, channel, user, settings, client, now)
        if a.action in ("confirm", "decline"):
            return _cmd_resolve(a, d, channel, user, settings, client, now)
        return _cmd_change(a, d, channel, user, settings, client, now)
    except tasks.TaskError as e:
        return _out(False, e.code, e.message)
