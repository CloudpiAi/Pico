# Slack end-to-end test of the standalone Pico FinOps Agent (Playwright)

A real browser signs in to the **Slack web app as a real person** and talks to the **running agent** the way a user
does:
- It sends questions in a DM, as an @mention in a channel, and as a follow-up in a thread.
- It waits for the bot's ✅ / ❌ mark and reads the answer from the page.
- It checks each answer against figures computed from the agent's own data (a copy of `focus.duckdb`).
- It sets up scheduled tasks and waits for them to arrive, then removes every task it created.

Nothing is mocked: real Slack, the real agent, the real model, real data.

- Questions: `catalog.yaml` (31 cases in 4 groups: `qa`, `status`, `schedule`, `refusal`)
- How the parts fit together: `CONTRACTS.md`

---

## 1. Before you run: checklist

| # | What | How to check |
|---|---|---|
| 1 | **The agent is running and connected to Slack** (the container, e.g. `pico-finops-agent`) | `docker ps` shows it `healthy`; the bot answers "hi" in its DM |
| 2 | **Python 3.13 + this repo's venv + Playwright + Chromium** on the machine that runs the test (the one with the agent container, so the data copy works) | see "First-time setup" below |
| 3 | **A saved Slack sign-in session** (`E2E_STATE_FILE`) | made once with the login script, see below |
| 4 | **The settings file** (`.env`) | copy `.env.example`, fill in (section 2) |
| 5 | **Slack channels** (optional, for the channel cases) | a test channel with you **and the bot** in it (`/invite @<bot name>`); a channel the bot is **not** in |
| 6 | **Agent settings for channel tasks** (optional) | in the **agent's** `.env`: `FOCUS_REPORT_CREATORS=<your member id>` (and optionally `FOCUS_REPORT_CHANNELS=<test channel ids>`), then restart the agent |
| 7 | **Not during a data load** | the agent loads at `FOCUS_SCHEDULE` (default 06:00 and 18:00 UTC); tasks are held while a load runs |

Why the sign-in is a saved session, not a password: Microsoft/Google sign-in asks for two-step verification (passkey,
Authenticator) every time, which a script can't do. You sign in once by hand; every run reuses that session.

---

## 2. Settings (`.env`)

Copy `.env.example` to `tests/e2e/standalone_ui/.env` (git-ignored) **or** `~/.pico-e2e/e2e.env` (`chmod 600`).
It holds only names and ids, never a password or a token. Any value can also be given as an environment variable
(it overrides the file).

### Required

| Variable | Example | Used for |
|---|---|---|
| `E2E_SLACK_URL` | `https://your-workspace.slack.com` | the workspace to open (the login script starts here) |
| `E2E_TEAM_ID` | `T0123456789` | building page addresses `app.slack.com/client/<team>/<channel>` (the `T…` after `/client/` in the browser) |
| `E2E_BOT_NAME` | `Pico FinOps Agent` | the bot's display name in this install (another install may call it `Pico`): the DM, the @mention, its replies |
| `E2E_STATE_FILE` | `~/.pico-e2e/state.json` | the saved sign-in session the browser loads (mode 600, outside the repo) |

### Optional

| Variable | Default / when empty | Used for |
|---|---|---|
| `E2E_BOT_USER` | found from the bot's profile link | the bot's member id (`U…`) |
| `E2E_BOT_DM` | opened from the sidebar by `E2E_BOT_NAME` | the DM id (`D…`); set it to skip the sidebar lookup |
| `E2E_ME` | not checked | your member id (`U…`); with it, only your own messages count as "mine" |
| `E2E_TEST_CHANNELS` | channel cases **SKIP** | channel ids (`C…`, comma-separated) with you and the bot in them; every channel case runs once per channel |
| `E2E_OUTSIDE_CHANNEL` | that refusal case **SKIP** | a channel id the bot is **not** in ("post a report in #x" must be refused) |
| `E2E_NOTIFY_CHANNEL` | the notice case **SKIP** | the agent's `SLACK_NOTIFY_CHANNEL` id; read only: the newest notice must be a known kind |
| `E2E_AGENT_CONTAINER` | — (set this or `E2E_DATA_COPY`) | the agent container; its `/data/focus.duckdb` is copied (`docker cp`) to compute the expected figures |
| `E2E_DATA_COPY` | — | a `focus.duckdb` copy you already have (used instead of the container) |
| `E2E_REPORT_DIR` | `~/.pico-e2e/runs` | where each run's folder (logs, report, screenshots) goes |
| `E2E_HEADLESS` | `true` | `false` shows the browser (needs a display) |
| `E2E_ANSWER_TIMEOUT_S` | `180` | how long to wait for an answer (the longer of this and the catalog's value is used) |
| `E2E_TASK_TIMEOUT_S` | `240` | how long to wait for a task change and the task list |
| `E2E_DELIVER_TIMEOUT_S` | `480` | how long to wait for a scheduled post to arrive ("in 2 minutes" runs about 4 minutes later) |

Where to find an id: a channel's id is in the browser address (`…/client/T…/C…`) or channel details → About; a
member id is profile → ⋮ → Copy member ID.

---

## 3. First-time setup

```bash
# 1. the venv (from the repo root)
uv sync --frozen --extra pico-finops-agent
uv pip install --no-deps -e plugins/focus -e plugins/schedule
uv pip install "playwright==1.55.0"
.venv/bin/python -m playwright install chromium        # add --with-deps if the system libraries are missing (sudo)

# 2. settings
cp tests/e2e/standalone_ui/.env.example tests/e2e/standalone_ui/.env   # then fill it in

# 3. sign in once: a browser window opens; sign in yourself (incl. two-step verification);
#    the session is saved to E2E_STATE_FILE (mode 600). Needs a display.
.venv/bin/python -m tests.e2e.standalone_ui.login
```

On a server without a display: run the login on your PC, then copy the session file to the server
(`scp state.json server:.pico-e2e/state.json && ssh server chmod 600 .pico-e2e/state.json`). When a run
says the session has expired (exit 2), sign in again the same way.

---

## 4. Running

```bash
.venv/bin/python -m tests.e2e.standalone_ui.run --list                    # the cases; no browser, no session
.venv/bin/python -m tests.e2e.standalone_ui.run                           # everything (about 30-40 min)
.venv/bin/python -m tests.e2e.standalone_ui.run --only qa                 # one group: qa | status | schedule | refusal
.venv/bin/python -m tests.e2e.standalone_ui.run --case qa_prev_total      # one case (pulls in the cases it needs)
.venv/bin/python -m tests.e2e.standalone_ui.run --case sch_reminder --case sch_edit
.venv/bin/python -m tests.e2e.standalone_ui.run --env ~/.pico-e2e/e2e.env --headed
```

- `--env` defaults to `tests/e2e/standalone_ui/.env` when present, else `~/.pico-e2e/e2e.env`.
- **Exit code:** `0` = nothing failed, `1` = a check failed, `2` = the run couldn't start (settings, session, data copy).
- **Ctrl-C:** still removes the run's tasks. A second Ctrl-C stops the clean-up, and the log names the tasks left.
- **What the test leaves in Slack:** its messages in your DM with the bot and in the test channels. Tasks are removed
  at the end; tasks you had before the run are never touched.

---

## 5. Reading the results

Each run writes `E2E_REPORT_DIR/<yyyymmdd-hhmmss>/`:

| File | What |
|---|---|
| `run.log` | live, one timestamped line per event: `START <case> sent: '…'`, `reply <case>: …`, `PASS/FAIL/SKIP <case> <check>: …`, notes (`.. task list …`, `.. waiting up to 480s for t18 …`), `END <case> PASS/FAIL/SKIP <s>`. Follow with `tail -f run.log`; failures with `grep ' FAIL ' run.log` |
| `report.html` | self-contained: counts, a PASS/FAIL/SKIP filter, per case the text sent, the reply, every check, the time, the failure screenshot |
| `report.json` | the same, plus the computed values, the settings' ids and the agent image (`docker inspect`) |
| `shots/<case>.png` | the message pane when a case failed |

- **Case status:** FAIL if any check failed. SKIP if a needed value is missing (for example no test channel, or the newest month is complete so there's no "month to date"). `_tasks_before` / `_cleanup` are the task-list read and the clean-up.
- **How tasks are tracked:** the bot doesn't reliably repeat a new task's id, so the run reads your task list ("List all my scheduled tasks with their ids.") before the run and after each task case. The new id belongs to that case. At the end it removes the tasks that are new and still open, then reads the list again to confirm.

### Known product issue (not a test bug)
The agent sometimes answers **"(No response generated)"** (with the ✅) to a task change. The change *is* made, and
the confirmation shows up in its next answer. Those cases FAIL on the `reply` check; the task checks still pass.

### Troubleshooting
| You see | Do |
|---|---|
| exit 2, "session" / sign-in page | sign in again (section 3) |
| `PageError: … selector …` | Slack changed its page; fix the named selector at the top of `page.py` |
| "The previous request is still being processed" | an earlier question was still running; rerun that case |
| channel case SKIP | set `E2E_TEST_CHANNELS` and invite the bot to those channels |
| channel task refused ("Only the people your admin has listed…") | set `FOCUS_REPORT_CREATORS` in the agent's `.env` and restart the agent |

---

## 6. Adding a question

Add a case to `catalog.yaml` (the rules are at the top of `catalog.py`):

```yaml
  - id: qa_top_region_cost          # unique
    group: qa                       # qa | status | schedule | refusal
    where: dm                       # dm | channel | thread (with after: <case>) | notify (with action: read_latest)
    text: "What did {{ top_region }} cost in {{ prev_month_name }}?"
    truth:                          # figures the answer must contain, computed from the data copy
      total: >-
        SELECT sum(billedcost) FROM focus WHERE billingperiodstart = TIMESTAMP '{{ prev_month_start }}'
        AND regionid = '{{ top_region | sql }}'
    expect:
      contains: ["(?i){{ top_region | re }}"]   # regexes on the bot's answer
```

- **Placeholders:** `prev_month_name`/`_start` (newest complete month), `mtd_month_name`/`_start` (only when the newest month is partial), `newest_month_*`, `top_service`, `second_service`, `top_account`, `top_region`, `data_through`, `test_channel`, `outside_channel`, `tasks.<case id>`.
- **Other `expect` keys:**
  - `reaction`: done | error | any | none
  - `not_contains`, `contains_any`
  - `task`: created | changed | removed | none (`task_of` for changed and removed)
  - `deliver`: `within_s`, `channel` (here | dm | test_channel), `contains`, `figures`
  - `silent`: `for_s`
- **Check it:** `--list`, `pytest tests/fork/e2e_standalone_ui/test_catalog.py`, then `--case <id>`.

Unit tests of the test itself (no Slack, no network): `pytest tests/fork/e2e_standalone_ui -o addopts="" -q`.
