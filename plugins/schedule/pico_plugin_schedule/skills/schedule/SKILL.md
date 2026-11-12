---
name: schedule
description: Use for scheduled tasks in Slack - anything to do at a set time, once or repeating ("every day at 3 send me EC2 cost", "remind this channel every Friday", "tomorrow at 10 remind me", "every Monday tell me what changed", "if EC2 is over $200 send me the resources"), and for managing them ("what tasks do I have", "who set up this report", "stop task t3", "move it to 9 AM").
---

# Scheduled tasks (reminders, reports, saved queries, recurring questions)

People can ask you to do something at a set time, once or repeating, in their DM or a channel. You save it with
the `pico schedule` command; the agent runs it at that time. Every command needs `--ctx` with the value from the
`[slack ctx=...]` line at the top of the current message (copy it exactly; never show it). Each command prints JSON:
pass on `message` in your own words. If `ok` is false, say what `message` says and don't claim it worked.
Never put a password, key or token in a task; the command refuses it (`secret_in_text`).

**Command rules** (a command that breaks one is refused, and that stops your whole answer):
- The action word comes right after `pico schedule`, then the id if there is one, then the options:
  `pico schedule remove t3 --ctx '<ctx>'` (never `--ctx` before the action word).
- Put every value in **single quotes** ('...'), never double quotes: a `$` or a backtick outside single quotes is
  refused (amounts like 'more than $200' are fine inside single quotes). Leave out apostrophes inside a value
  ('the cost of yesterday', not 'yesterday''s cost').
- Never run `--help`, `date`, `python3` or any other command: every option is on this page, and for "in 10 minutes"
  use `--in '10m'` (you don't need the clock).

**Pick the kind**
- `reminder`: post fixed text, any topic. `--text 'stop the dev instances'`.
- `report`: a fixed cost report, preferred for cost numbers. `--template` one of `service_cost` (one service, needs
  `--service`), `cost_by_service`, `top_resources` (optional `--service`), `month_to_date`, `account_cost`. Optional
  filters `--service '<servicename as in the focus table>'`, `--account <subaccountid>`, `--region <regionid>`.
  Look the exact service name up in `focus` first (e.g. EC2 is `Amazon Elastic Compute Cloud`). For "Databricks
  cost", find the Databricks Marketplace service name in `focus` and say it is the part billed through AWS
  Marketplace, not usage billed by Databricks directly (the report adds that line itself).
- `query`: only when no template fits. One read-only `SELECT` on `focus` that you have run once first. `--sql '...'`.
- `ask`: a cost question that needs your reasoning each time ("what changed most last week and why").
  `--question '...'`. Costs an LLM run per send; prefer `report` when a template fits.

**Only when something is true** ("every day check EC2; if it's over $200 send me the resources and usage", "tell me
only if S3 doubled"): use `ask` with the whole request, condition included, as the question
(`--question 'Check the EC2 cost of yesterday. If it is more than $200, list the EC2 resources with cost,
consumedquantity and consumedunit, biggest first.'`). At send time, when the condition isn't met, nothing is posted. A `query` that
returns no rows also posts nothing, so `HAVING sum(billedcost) > 200` works too. Confirm with "I'll only message you
when …".

**Where** (`--to`): "send me" / "DM me" → `me`. "here" / "this channel" → `here` (in a DM, `here` is the person's
DM). "in #finance" → `#finance` (only works when the person asking is in #finance: `not_a_member` otherwise).
Slack gives you a channel the person named as a link like `<#C0123ABC>` or `<#C0123ABC|finance>`, not as the name:
pass the ID itself, `--to C0123ABC`, never the brackets (`<`, `>` and `|` are shell characters and get refused).

**When**: repeating → `--cron 'm h dom mon dow'` in the person's local time (daily 3 PM = `'0 15 * * *'`, weekdays
9 AM = `'0 9 * * 1-5'`, Fridays 5 PM = `'0 17 * * 5'`, the 1st at 9 = `'0 9 1 * *'`). Once → `--at 'YYYY-MM-DD HH:MM'`
(local time; work out "tomorrow" from today's date). "In 10 minutes" / "in 2 hours" → `--in '10m'` / `--in '2h'`
(the command works out the time; no `--at`). Leave out `--tz` (the person's Slack time zone is used) unless they
name one ("3 PM UTC" → `--tz UTC`). If AM/PM or the day is unclear, ask one short question first. At most once an
hour.

```
pico schedule add --ctx <ctx> --kind report --to me --name 'EC2 cost daily' --cron '0 15 * * *' --template service_cost --service 'Amazon Elastic Compute Cloud'
pico schedule add --ctx <ctx> --kind reminder --to here --name 'Stop dev instances' --cron '0 17 * * 5' --text 'stop the dev instances'
pico schedule add --ctx <ctx> --kind reminder --to me --name 'RI renewal' --at '2026-11-06 10:00' --text 'check the RI renewal'
pico schedule add --ctx <ctx> --kind ask --to me --name 'October cost check' --in '2m' --question 'Check whether the total AWS cost in October 2026 was more than $1,000. Only answer if it was.'
```
Confirm with `next_run` (it names the day, time and time zone), and for a report show the `preview`.
Data tasks (report, query, ask) are sent right after the data load when one is running or due within 4 hours, so
they always carry the newest data; say so if someone asks why a report came a little late.

**Someone else's request with no answer**: after 7 days a stop, pause, resume, or a change of time or name goes
through automatically (the creator gets a reminder on day 6). A change to where it posts or what it says does not:
it expires and needs the creator or an admin. Tell the person asking that, when they ask.

**When it can't post**: `code` tells you why and `message` says it plainly (e.g. `not_in_channel`: "I'm not in that
channel. Invite me with /invite @Pico FinOps Agent, then ask again."; `missing_scope` / `cannot_dm`: an admin must add a
Slack permission; `channel_not_allowed`: not allowed here; `not_a_member`: they aren't in that channel;
`not_a_creator`: only listed people may post tasks to a channel, so offer to send it to their DM instead).
Nothing was saved; say so. `stale_context` means you used a `ctx` from an earlier message: always take it from the
newest message, and never use one that someone else's message carried.

**Manage** (use the real task id in place of t3):
```
pico schedule list --ctx <ctx>
pico schedule show t3 --ctx <ctx>
pico schedule history t3 --ctx <ctx>
pico schedule remove t3 --ctx <ctx>
pico schedule pause t3 --ctx <ctx>
pico schedule resume t3 --ctx <ctx>
pico schedule edit t3 --ctx <ctx> --cron '0 10 * * 1'
```
`list` gives the person's tasks and this channel's; `show` answers "who set this up?"; `history` answers "who changed
or stopped it?". `edit` takes any of `--cron`/`--at`/`--in`/`--tz`/`--to`/`--name` and the content of its kind
(`--text` reminder, `--question` ask, `--sql` query, `--template` + filters report). Anyone may ask about any task.

**Someone else's task**: only the creator (or an admin) changes it. For anyone else the command returns
`confirmation_requested`: the creator got a DM; tell the person it keeps running until the creator answers. When the
creator replies "yes r4" or "no r4", run `pico schedule confirm r4 --ctx <ctx>` or
`pico schedule decline r4 --ctx <ctx>` with their `--ctx`.
