"""A case's reply is judged and task lists are read (tests/e2e/standalone_ui/checks.py)."""

import pytest

from tests.e2e.standalone_ui.catalog import Expect
from tests.e2e.standalone_ui.checks import (
    EMPTY_ANSWERS,
    FAIL_PHRASES,
    Check,
    already_closed,
    judge_reply,
    new_tasks,
    parse_task_list,
    task_footer,
    to_remove,
)

LIST = (
    "Here are your scheduled tasks: • *t14*: Check e2e walkthrough (Reminder) • Status: Active • "
    "Schedule: One-time at 1:12 PM IST (11 Nov 2026) • Target: DM to you • *t7*: Cost by service daily "
    "(Report) • Status: Active • Schedule: Daily at 12:34 PM IST (34 12 * * *) • Target: DM to you "
    "• *t3*: EC2 cost check (Ask) • Status: Paused • Schedule: One-time at 12:17 PM IST (10 Nov 2026) "
    "• Target: DM to you • *t2*: EC2 cost report (Report) • Status: Paused • Schedule: One-time at "
    "12:05 PM IST (10 Nov 2026) • Target: DM to you"
)
CLOSED = (
    "Task *t14* has already run (completed), so it can no longer be modified or removed. Here are your remaining "
    "scheduled tasks: • *t7*: Cost by service daily (Report) • Status: Active • Schedule: Daily at "
    "12:34 PM IST (34 12 * * *) • Target: DM to you"
)
FOOTER = (
    "⏰ Reminder: check the e2e walkthrough Set up by @Ravi Kumar · task t14 · "
    "say 'stop task t14' to stop it"
)


def _by_name(checks):
    return {c.name.split(" ", 1)[1]: c for c in checks}


# --- parse_task_list ---------------------------------------------------------------------------------------------

def test_real_task_list_parses():
    assert parse_task_list(LIST) == {"t14": "active", "t7": "active", "t3": "paused", "t2": "paused"}


def test_already_run_reply_marks_the_task_done():
    parsed = parse_task_list(CLOSED)
    assert parsed["t14"] == "done"
    assert parsed["t7"] == "active"
    assert already_closed(CLOSED) is True


@pytest.mark.parametrize("lead", ["Task *t15* has been removed.", "Task t15 was deleted.", "Task *t15* has been stopped."])
def test_a_removed_task_named_before_the_list_is_done(lead):
    # live 2026-11-11 (clean-up of t15): the confirmation of the removal comes first, then the remaining list
    text = (f"{lead} Here is your current list of scheduled tasks: • *t7*: Cost by service daily (Report) • "
            "Status: Active • Schedule: Daily at 12:34 PM IST (34 12 * * *) • Target: DM to you")
    assert parse_task_list(text) == {"t15": "done", "t7": "active"}


def test_the_bold_name_list_format_parses():
    # live 2026-11-11 15:21 (after an agent restart) the bot changed its wording: the bold runs over id AND name
    text = ("Here are your scheduled tasks: • *t7 — Cost by service daily* (active) • Type: Cost Report "
            "(cost_by_service) • Schedule: Daily at 12:34 (Asia/Kolkata) • Target: DM to you • Last sent: Nov 11, 2026 "
            "at 07:04 UTC • *t3 — EC2 cost check* (paused) • Type: Recurring question • *t2 — EC2 cost report* (paused)")
    assert parse_task_list(text) == {"t7": "active", "t3": "paused", "t2": "paused"}


def test_tasks_removed_together_before_the_list_are_done():
    # live 2026-11-11 16:30: "I've removed tasks *t28*, *t29*, and *t30*." then the remaining list
    text = ("I've removed tasks *t28*, *t29*, and *t30*. Here is your updated list of scheduled tasks: "
            "• *t7 — Cost by service daily* (active) • Type: Cost report • *t3 — EC2 cost check* (paused)")
    assert parse_task_list(text) == {"t28": "done", "t29": "done", "t30": "done", "t7": "active", "t3": "paused"}


def test_an_id_only_named_before_the_list_without_removal_is_not_a_listed_task():
    # "I've updated task t28 to 10:00 AM" before the list: t28's status comes from the list, not the preamble
    text = ("I've updated task *t28* to 10:00 AM. Here are your scheduled tasks: • *t28 — Reminder* (active) "
            "• *t7 — Cost by service daily* (active)")
    assert parse_task_list(text) == {"t28": "active", "t7": "active"}


def test_a_task_id_in_parentheses_is_read():
    text = "I've set up your daily AWS cost report (task ID: t22). Here are all your tasks: • *t22 — Daily report* (active)"
    assert parse_task_list(text) == {"t22": "active"}


@pytest.mark.parametrize("text", ["`t5`: Daily report • Status: Active", "t5: Daily report - Status: active"])
def test_other_id_styles(text):
    assert parse_task_list(text) == {"t5": "active"}


def test_instance_types_are_not_task_ids():
    text = "• *t9*: Check t3.micro and t2.large spend • Status: Paused"
    assert parse_task_list(text) == {"t9": "paused"}


def test_status_without_a_known_word_is_unknown():
    assert parse_task_list("• *t4*: Something • Schedule: Daily") == {"t4": "unknown"}


@pytest.mark.parametrize(
    "text",
    ["No tasks scheduled.", "You don't have any tasks yet.", "You don’t have any scheduled tasks."],
)
def test_no_tasks_reply_is_empty(text):
    assert parse_task_list(text) == {}


@pytest.mark.parametrize("text", ["", "Sorry, something went wrong.", "Your t3.micro costs $4.00"])
def test_unreadable_reply_raises(text):
    with pytest.raises(ValueError):
        parse_task_list(text)


# --- task_footer, new_tasks, to_remove, already_closed -----------------------------------------------------------

def test_footer_gives_the_task_id():
    assert task_footer(FOOTER) == "t14"


@pytest.mark.parametrize("text", ["EC2 cost is $4.00 on t3.micro", "Saved task t5. Next run: tomorrow", ""])
def test_no_footer_is_none(text):
    assert task_footer(text) is None


def test_new_tasks_in_numeric_order():
    before = {"t2": "paused", "t3": "paused"}
    after = {"t2": "paused", "t3": "paused", "t14": "active", "t9": "done", "t10": "paused"}
    assert new_tasks(before, after) == ["t9", "t10", "t14"]


def test_to_remove_leaves_out_done_tasks():
    before = {"t2": "paused"}
    after = {"t2": "paused", "t14": "done", "t15": "active", "t16": "unknown"}
    assert to_remove(before, after) == ["t15", "t16"]


@pytest.mark.parametrize(
    "text,closed",
    [
        ("Task t14 has already run, nothing to remove.", True),
        ("That task is completed.", True),
        ("Task t7 is already stopped.", True),
        ("Removed task t7.", False),
        ("I couldn't find task t99.", False),
    ],
)
def test_already_closed(text, closed):
    assert already_closed(text) is closed


@pytest.mark.parametrize(
    "text,hit",
    [
        ("I couldn't find that task", True),
        ("I could not find that task", True),
        ("I can't do that", True),
        ("Task not found", True),
        ("No such task", True),
        ("That task doesn't exist", True),
        ("I was unable to remove it", True),
        ("The update failed", True),
        ("Paused task t7.", False),
        ("unfailed and scannot", False),
    ],
)
def test_fail_phrases(text, hit):
    assert bool(FAIL_PHRASES.search(text)) is hit


# --- judge_reply -------------------------------------------------------------------------------------------------

def test_a_good_answer_passes_every_check():
    exp = Expect(reaction="done", contains=["(?i)cost"], not_contains=["(?i)error"], contains_any=["EC2", "S3"])
    checks = judge_reply("qa_x", exp, "done", ["Your EC2 cost was $1,234.56."],
                         truth_values={"total": 1234.56})
    assert [c.name for c in checks] == [
        "qa_x finished", "qa_x reply", "qa_x contains", "qa_x not_contains", "qa_x contains_any", "qa_x figures",
    ]
    assert all(isinstance(c, Check) and c.status == "PASS" for c in checks)


def test_empty_lists_and_no_truth_give_only_finished_and_reply():
    checks = judge_reply("qa_x", Expect(), "done", ["hello"])
    assert [c.name for c in checks] == ["qa_x finished", "qa_x reply"]


@pytest.mark.parametrize(
    "reaction,state,status",
    [
        ("done", "done", "PASS"),
        ("done", "error", "FAIL"),
        ("done", "timeout", "FAIL"),
        ("error", "error", "PASS"),
        ("error", "done", "FAIL"),
        ("any", "done", "PASS"),
        ("any", "error", "PASS"),
        ("any", "replied", "PASS"),
        ("any", "timeout", "FAIL"),
        ("none", "replied", "PASS"),
    ],
)
def test_finished_follows_the_expected_reaction(reaction, state, status):
    checks = _by_name(judge_reply("c", Expect(reaction=reaction), state, ["text"]))
    assert checks["finished"].status == status
    assert state in checks["finished"].detail


def test_no_response_generated_is_a_failed_reply():
    assert EMPTY_ANSWERS == ("(No response generated)",)
    checks = _by_name(judge_reply("c", Expect(), "done", ["(No response generated)"]))
    assert checks["finished"].status == "PASS"
    assert checks["reply"].status == "FAIL"


@pytest.mark.parametrize("texts", [[], ["   "], ["", "(No response generated)"]])
def test_empty_reply_fails(texts):
    assert _by_name(judge_reply("c", Expect(), "done", texts))["reply"].status == "FAIL"


def test_no_reply_expected():
    exp = Expect(reaction="none", no_reply=True)
    assert _by_name(judge_reply("c", exp, "timeout", []))["reply"].status == "PASS"
    assert _by_name(judge_reply("c", exp, "replied", ["I answered anyway"]))["reply"].status == "FAIL"


def test_contains_failures_name_the_patterns():
    exp = Expect(contains=["(?i)cost", "(?i)region"], not_contains=["(?i)sorry"], contains_any=["EC2", "RDS"])
    checks = _by_name(judge_reply("c", exp, "done", ["Sorry, the cost", "is unknown"]))
    assert checks["contains"].status == "FAIL" and "region" in checks["contains"].detail
    assert checks["not_contains"].status == "FAIL" and "sorry" in checks["not_contains"].detail
    assert checks["contains_any"].status == "FAIL"


def test_contains_looks_across_all_reply_texts():
    exp = Expect(contains=["(?i)cost", "(?i)region"])
    assert _by_name(judge_reply("c", exp, "done", ["the cost", "by region"]))["contains"].status == "PASS"


def test_figures_missing_value_fails_and_names_it():
    checks = _by_name(judge_reply("c", Expect(), "done", ["Total $100.00"],
                                  truth_values={"total": 100.0, "s3": 55.0}))
    assert checks["figures"].status == "FAIL"
    assert "s3" in checks["figures"].detail


def test_figures_all_unknown_is_one_skip():
    checks = judge_reply("c", Expect(), "done", ["Total $100.00"], truth_values={"a": None, "b": None})
    figs = [c for c in checks if c.name == "c figures"]
    assert len(figs) == 1 and figs[0].status == "SKIP"


def test_figures_some_unknown_checks_the_rest():
    checks = _by_name(judge_reply("c", Expect(), "done", ["Total $100.00"],
                                  truth_values={"total": 100.0, "other": None}))
    assert checks["figures"].status == "PASS"
    assert "other" in checks["figures"].detail


def test_figures_kinds_tolerance_and_sign_are_passed_on():
    ok = _by_name(judge_reply("c", Expect(), "done", ["down 3%"], truth_values={"chg": -3.0},
                              kinds={"chg": "percent"}, ignore_sign=True))
    assert ok["figures"].status == "PASS"
    loose = _by_name(judge_reply("c", Expect(), "done", ["$105.00"], truth_values={"t": 100.0}, tol_pct=10.0))
    assert loose["figures"].status == "PASS"
    strict = _by_name(judge_reply("c", Expect(), "done", ["$105.00"], truth_values={"t": 100.0}))
    assert strict["figures"].status == "FAIL"


@pytest.mark.parametrize("task", ["changed", "removed"])
def test_refused_task_change_fails(task):
    checks = _by_name(judge_reply("c", Expect(task=task), "done", ["I couldn't find task t99."]))
    assert checks["refused"].status == "FAIL"


def test_task_change_done_passes_refused():
    checks = _by_name(judge_reply("c", Expect(task="removed"), "done", ["Removed task t7."]))
    assert checks["refused"].status == "PASS"


def test_removing_an_already_run_task_is_not_refused():
    checks = _by_name(judge_reply("c", Expect(task="removed"), "done", [CLOSED]))
    assert checks["refused"].status == "PASS"


def test_no_refused_check_for_created_tasks():
    checks = _by_name(judge_reply("c", Expect(task="created"), "done", ["I couldn't parse the time, but saved."]))
    assert "refused" not in checks


# Live 2026-11-12 13:39: a bare id before "• Name:"; the reader found
# no id, so 7 schedule cases failed and 5 were skipped.
BARE = ("Here is your current list of scheduled tasks with their IDs: t2 • Name: EC2 cost report • Kind: Report "
        "(service_cost) • Schedule: One-off (2026-11-10 12:05) • Target: DM • Status: Paused t3 • Name: EC2 cost check "
        "• Kind: Ask • Schedule: One-off (2026-11-10 12:17) • Target: DM • Status: Paused t7 • Name: Cost by service "
        "daily • Kind: Report (cost_by_service) • Schedule: Daily at 12:34 (Asia/Kolkata) • Target: DM • Status: Active "
        "t58 • Name: Review AWS bill • Kind: Reminder • Schedule: Every Monday at 9:00 (Asia/Kolkata) • Target: DM • "
        "Status: Active")


def test_bare_ids_before_name_are_read():
    assert parse_task_list(BARE) == {"t2": "paused", "t3": "paused", "t7": "active", "t58": "active"}


def test_bare_ids_on_their_own_lines_are_read():
    text = ("Here are all of your scheduled tasks:\n\nt7\n• Name: Cost by service daily\n• Status: Active\n\n"
            "t58\n• Name: Review AWS bill\n• Status: Active")
    assert parse_task_list(text) == {"t7": "active", "t58": "active"}


def test_a_bare_id_in_prose_or_an_instance_type_is_not_a_task():
    text = "I've scheduled a reminder (t58) for you. It checks t3.micro costs. • *t7*: Cost by service • Status: Active"
    assert parse_task_list(text) == {"t7": "active"}


SORRY_TEXT = "Sorry, I couldn't answer that just now. Please ask again in a moment."
TOO_LONG_TEXT = "That took too long to work out. Please try a narrower question, for example one service or one month."


@pytest.mark.parametrize("text", [SORRY_TEXT, TOO_LONG_TEXT])
def test_the_agents_cant_answer_sentence_is_a_failed_reply(text):
    """sch_ask_not_met passed on 2026-11-13 although the bot only said SORRY (a task appeared by accident)."""
    reply = _by_name(judge_reply("c", Expect(), "done", [text]))["reply"]
    assert reply.status == "FAIL"
    assert "couldn't answer" in reply.detail and text[:40] in reply.detail


def test_a_refusal_case_may_end_in_sorry():
    # reaction "any": the refusal cases only need the bad thing not to happen
    assert _by_name(judge_reply("c", Expect(reaction="any"), "done", [SORRY_TEXT]))["reply"].status == "PASS"


def test_sorry_plus_a_real_answer_passes():
    checks = _by_name(judge_reply("c", Expect(), "done", [SORRY_TEXT, "EC2 cost $72.80 in October."]))
    assert checks["reply"].status == "PASS"


def test_the_sentences_match_the_agent():
    from pico_finops_agent import slack_guard

    assert (SORRY_TEXT, TOO_LONG_TEXT) == (slack_guard.SORRY, slack_guard.TOO_LONG)
    from tests.e2e.standalone_ui.checks import CANT_ANSWER

    assert set(CANT_ANSWER) == {slack_guard.SORRY, slack_guard.TOO_LONG}
