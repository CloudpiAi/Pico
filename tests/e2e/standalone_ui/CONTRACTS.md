# Contracts between the modules (read this first)

Package: `tests/e2e/standalone_ui/`. Tests: `tests/fork/e2e_standalone_ui/` (no network, no real Slack; Playwright
tests load local HTML with `page.set_content`). Python 3.12, `from playwright.sync_api import ...` (sync API only).
Imports are absolute: `from tests.e2e.standalone_ui.config import Config`.

Facts from the real Slack web app (walk-through 2026-11-11; see the plan's "Real flow"):
- A message: `[data-qa="virtual-list-item"][id^="message-list_"]` whose `data-item-key` is the Slack ts
  (`"1794383046.681299"`); day dividers are list items too but have no `[data-qa="message_container"]` (skip them).
- Inside: `[data-qa="message_sender_name"]` (text = display name; may be ABSENT on a follow-on message from the same
  sender: inherit the previous message's sender), `[data-qa="message_sender_app_badge"]` (present = an app/bot),
  `a[data-ts]` (ts again; href `/archives/<channel>/p<ts without dot>`), a profile link `a[href^="/team/U"]` (member
  id), `[data-qa="message-text"]` (use `innerText`), `[data-qa="reactji"]` with `aria-label` like
  `"1 reaction, react with white check mark emoji"` / `"... hourglass flowing sand emoji"` / `"... x emoji"`.
- Composer: `[data-qa="message_input"] [data-qa="texty_input"]`, aria-label `"Message to <bot name>"` in a DM,
  `"Message #<channel name>"` in a channel. `fill(text)` + `press("Enter")` sends.
- The list is virtual (old items unmount). A "N new messages" banner can appear; scroll the list to the bottom
  before reading. A "Slack needs your permission to enable notifications" bar may show (harmless).
- Slack intercepts a message starting with "/": send " /help" (leading space); it reaches the bot as a normal
  question (the gateway's own commands are not reachable from the app).
- Working/done marks are on MY message: hourglass_flowing_sand = working, white_check_mark = done, x = error.
- Scheduled posts end with: `Set up by @<user> · task tN · say 'stop task tN' to stop it`.
- The bot does NOT reliably repeat the task id when it creates a task ("All set! ... Next run: ..."). Task ids come
  from the task list ("List all my scheduled tasks with their ids." -> "• *t14*: Check e2e walkthrough (Reminder) •
  Status: Active • Schedule: One-time at 1:12 PM IST (11 Nov 2026) • Target: DM to you • *t7*: ...").
- The bot can answer "(No response generated)" and still get the done mark: that is a FAIL.
- A one-time task closes itself after it runs; removing it then gets "has already run (completed)": not a failure.
- Timings seen: answers 5-30 s, task changes up to 90 s, "in 2 minutes" ran ~4 min after sending.

## config.py  (batch A)
```python
@dataclass(frozen=True)
class Config:
    slack_url: str; team_id: str; bot_name: str
    bot_user: str | None; bot_dm: str | None; me: str | None
    test_channels: tuple[str, ...]; outside_channel: str | None; notify_channel: str | None
    state_file: str; agent_container: str | None; data_copy: str | None
    report_dir: str; headless: bool
    answer_timeout_s: float; task_timeout_s: float; deliver_timeout_s: float
def load_config(env_file: str | None = None, environ: Mapping[str, str] | None = None) -> Config
```
Reads `E2E_*` (names in `.env.example`) from `env_file` (simple KEY=VALUE, `#` comments, optional quotes) then
`environ` overrides. `~` expanded in paths. Empty optional value -> None / (). Missing required
(`E2E_SLACK_URL`, `E2E_TEAM_ID`, `E2E_BOT_NAME`, `E2E_STATE_FILE`) -> `ConfigError` naming every missing key.
`ConfigError(Exception)`. `Config.redact_values()` -> nothing secret lives in Config (ids only), returns ().

## answer_check.py, ground_truth.py  (batch A; copied from the API version, keep their tests green)
- `figures(text) -> list[Figure]`, `match_figures(text, expected, *, tol_pct, tol_abs, kinds, ignore_sign) -> list[str]`
  (missing names). Add: `$1.2 million`/`$3 billion`, `($45.10)` = negative, the Unicode minus `−`.
- `snapshot(container, dest, run=subprocess.run) -> dest`, `SnapshotError`; `Truth(path)`: `.vars() -> dict[str,str]`
  (newest_*, mtd_* only when partial, prev_* = newest complete month, data_through, top_service, second_service,
  top_account, top_region), `.value(sql) -> float | None`, `.close()`.

## checks.py  (batch A)
```python
EMPTY_ANSWERS = ("(No response generated)",)
@dataclass(frozen=True)
class Check: status: str  # "PASS" | "FAIL" | "SKIP"
             name: str; detail: str = ""
def judge_reply(case_id, expect, state, texts, *, truth_values=None, kinds=None, tol_pct=1.0, ignore_sign=False) -> list[Check]
    # state: "done" | "error" | "replied" | "timeout"; texts: the bot's reply texts for this case
    # checks, in order: finished (expect.reaction), reply (non-empty and not an EMPTY_ANSWERS text), contains,
    # not_contains, contains_any, figures (truth_values: name -> float|None; all None -> SKIP), refused
    # (expect.task in changed/removed and FAIL_PHRASES found -> FAIL)
FAIL_PHRASES: re.Pattern   # couldn't|could not|can't|cannot|not found|no such|doesn't exist|unable|failed (word-bounded)
def parse_task_list(text) -> dict[str, str]
    # "t14" -> "active" | "paused" | "done" | "unknown", from the list reply; ids like "*t14*", "`t14`", "t14:";
    # never "t3" inside "t3.micro"; a "(No tasks|You don't have any tasks)" reply -> {} ; unreadable -> raise ValueError
def task_footer(text) -> str | None          # "task t14 · say 'stop task t14'" -> "t14" (a scheduled post)
def new_tasks(before: dict, after: dict) -> list[str]      # ids in after not in before, numeric order
def to_remove(before: dict, after: dict) -> list[str]      # new and status not "done"
def already_closed(text) -> bool             # "has already run" / "completed" / "already stopped" -> True
```

## page.py  (batch B)
```python
@dataclass(frozen=True)
class Msg: ts: str; sender: str; sender_id: str | None; is_bot: bool; text: str
           reactions: tuple[str, ...]   # emoji names: "white_check_mark", "hourglass_flowing_sand", "x", ...
           task_id: str | None          # from the scheduled-post footer, else None
class PageError(Exception)              # a selector or navigation problem, message names the selector
class SignedOut(PageError)              # the session is gone (sign-in page shown)
class SlackPage:
    def __init__(self, page: playwright.sync_api.Page, cfg: Config, *, poll_s=2.0, sleep=time.sleep, clock=time.monotonic)
    def open(self, channel_id: str) -> None          # app.slack.com/client/<team>/<id>; waits for the list; SignedOut
    def open_bot_dm(self) -> str                     # cfg.bot_dm or the sidebar item named cfg.bot_name; returns D... id
    def messages(self) -> list[Msg]                  # the rendered messages, scrolled to the bottom, oldest first
    def send(self, text: str) -> str                # types in the composer, Enter; returns MY new message's ts
    def wait_done(self, my_ts: str, timeout_s: float, *, want_reaction=True) -> str
        # "done" (white_check_mark) | "error" (x) | "replied" (want_reaction=False and a bot message after my_ts)
        # | "timeout"
    def replies_after(self, my_ts: str, *, until_ts: str | None = None, exclude=frozenset()) -> list[Msg]
        # bot messages with ts > my_ts (and < until_ts), not in exclude, scheduled posts (task_id set) left out
    def posts_after(self, ts: str) -> list[Msg]      # bot messages after ts incl. scheduled posts (delivery check)
    def open_thread(self, root_ts: str) -> None      # opens the thread pane of that message
    def send_in_thread(self, text: str) -> str       # composer of the open thread pane; returns my ts
    def thread_replies_after(self, my_ts: str, exclude=frozenset()) -> list[Msg]
    def screenshot(self, path: str) -> None          # the message pane only
```
`login.py` (batch B): `python -m tests.e2e.standalone_ui.login [--env FILE]` opens a visible browser at
`cfg.slack_url`, waits until the client page (`app.slack.com/client/`) loads (the person signs in), then saves
`context.storage_state(path=cfg.state_file)` and chmods it 600.

## catalog.py, catalog.yaml, runner.py, report.py, run.py  (batch C)
- `catalog.py` keeps the copied API: `load`, `select`, `expand_channels`, `render`, `Case`, `Expect`, `MissingVar`,
  `CatalogError`; changes: `Expect.task` "created" no longer needs "Saved task" (the runner reads the task list);
  `where` adds nothing new. `catalog.yaml`: drop `qa_new`; `qa_help` text `" /help"` with reaction `done` and
  contains `"(?i)cost|aws"`; `qa_month_to_date` uses `mtd_month_name`/`mtd_month_start`; `sch_ask_not_met` gets
  `silent: {for_s: 300}`; add `sch_channel_post_soon` (DM ask "In 2 minutes, post last month's total AWS cost and
  top service in <#{{ test_channel }}>", deliver channel test_channel, figures true).
- `runner.py`: `Runner(page: SlackPage-like, cfg, truth, vars_, log: RunLog)`; `run(cases)`:
  1. task list BEFORE (if any case creates tasks) -> `parse_task_list`;
  2. each case: render (MissingVar -> SKIP), open the right place (DM / channel / thread), send, `wait_done`,
     settle after a timeout (wait again up to 120 s so a late answer can't land in the next case), read replies
     with `until_ts`/`exclude`, `judge_reply`, delivery (`posts_after` in the target channel, `task_footer`) /
     silent checks, screenshot on any FAIL;
  3. `finally`: task list AFTER, `to_remove(before, after)`, send "Please remove task tN." each, then list again and
     FAIL any id still active; Ctrl-C -> names the ids left.
- `report.py`: `RunLog(dir)`: `case_start(id, text)`, `add(Check)`, `case_end(id, seconds)`, writes `run.log`
  (one timestamped line per event, live), `report.json` (summary + every check + per-case timing + screenshots +
  vars + config ids + agent image), `report.html` (table PASS/FAIL/SKIP, filter by status, failure screenshots
  linked). Exit code: 0 no FAIL, 1 a FAIL, 2 setup failed (config, session, data copy).
- `run.py`: `python -m tests.e2e.standalone_ui.run [--env FILE] [--only GROUP] [--case ID] [--list]`; loads
  config, data copy (`snapshot` or `data_copy`), `Truth.vars()`, launches Chromium with `storage_state`, builds
  `SlackPage`, runs, prints the summary and the report path. The agent image (`docker inspect`) goes in the report.
