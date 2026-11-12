"""The question catalog loads, validates and renders with live values (tests/e2e/standalone_ui/catalog.py)."""

import os

import pytest

from tests.e2e.standalone_ui import catalog
from tests.e2e.standalone_ui.catalog import CatalogError, MissingVar

SHIPPED = os.path.join(os.path.dirname(catalog.__file__), "catalog.yaml")


def _write(tmp_path, body):
    p = tmp_path / "c.yaml"
    p.write_text(body, encoding="utf-8")
    return str(p)


GOOD = """
defaults: {timeout_s: 120, tol_pct: 2.0}
cases:
  - id: total
    group: qa
    where: dm
    text: "How much did we spend in {{ prev_month_name }}?"
    truth:
      total: "SELECT sum(billedcost) FROM focus WHERE billingperiodstart = TIMESTAMP '{{ prev_month_start }}'"
  - id: follow
    group: qa
    where: thread
    after: total
    text: "And {{ top_service }}?"
    expect: {contains: ["(?i)ec2|{{ top_service }}"]}
  - id: help
    group: qa
    where: channel
    text: "/help"
    expect: {reaction: none, contains: ["Available commands"]}
"""


def test_cases_load_with_defaults(tmp_path):
    cases = catalog.load(_write(tmp_path, GOOD))
    assert [c.id for c in cases] == ["total", "follow", "help"]
    total = cases[0]
    assert total.timeout_s == 120 and total.tol_pct == 2.0
    assert total.expect.reaction == "done"  # the default
    assert total.action == "ask"
    assert cases[2].expect.reaction == "none"


def test_render_fills_text_truth_and_patterns(tmp_path):
    cases = catalog.load(_write(tmp_path, GOOD))
    vars_ = {"prev_month_name": "October 2026", "prev_month_start": "2026-10-01 00:00:00", "top_service": "Amazon EC2"}
    r = catalog.render(cases[0], vars_)
    assert r.text == "How much did we spend in October 2026?"
    assert r.truth == {"total": "SELECT sum(billedcost) FROM focus WHERE billingperiodstart = TIMESTAMP '2026-10-01 00:00:00'"}
    f = catalog.render(cases[1], vars_)
    assert f.expect.contains == ["(?i)ec2|Amazon EC2"]


def test_a_missing_var_raises_missing_var_naming_it(tmp_path):
    cases = catalog.load(_write(tmp_path, GOOD))
    with pytest.raises(MissingVar) as e:
        catalog.render(cases[0], {})
    assert "prev_month_name" in str(e.value)


def test_captured_task_ids_render(tmp_path):
    body = """
cases:
  - {id: add, group: schedule, where: dm, text: "remind me", expect: {task: created}}
  - {id: pause, group: schedule, where: dm, text: "pause task {{ tasks.add }}", expect: {task: changed, task_of: add}}
"""
    cases = catalog.load(_write(tmp_path, body))
    assert catalog.render(cases[1], {"tasks": {"add": "t7"}}).text == "pause task t7"
    with pytest.raises(MissingVar):
        catalog.render(cases[1], {"tasks": {}})


@pytest.mark.parametrize(
    "body,match",
    [
        ("cases:\n  - {id: a, group: qa, where: dm}\n", "text"),
        ("cases:\n  - {id: a, group: nope, where: dm, text: x}\n", "group"),
        ("cases:\n  - {id: a, group: qa, where: moon, text: x}\n", "where"),
        ("cases:\n  - {id: a, group: qa, where: thread, text: x}\n", "after"),
        ("cases:\n  - {id: a, group: qa, where: thread, after: b, text: x}\n", "after"),
        ("cases:\n  - {id: a, group: qa, where: dm, text: x}\n  - {id: a, group: qa, where: dm, text: y}\n", "twice"),
        ("cases:\n  - {id: a, group: qa, where: dm, text: x, colour: red}\n", "colour"),
        ("cases:\n  - {id: a, group: qa, where: dm, text: x, expect: {reaction: maybe}}\n", "reaction"),
        ("cases:\n  - {id: a, group: qa, where: dm, text: x, expect: {smell: ok}}\n", "smell"),
        ("cases:\n  - {id: a, group: qa, where: dm, text: x, expect: {task: changed}}\n", "task_of"),
        ("cases:\n  - {id: a, group: qa, where: dm, text: x, expect: {contains: ['(']}}\n", "regex"),
        ("cases:\n  - {id: a, group: qa, where: dm, text: x, kinds: {z: money}}\n", "kinds"),
        ("cases:\n  - {id: a, group: status, action: read_latest, where: dm}\n", "notify"),
        ("cases: []\n", "no cases"),
    ],
)
def test_bad_catalogs_are_refused_with_the_reason(tmp_path, body, match):
    with pytest.raises(CatalogError, match=match):
        catalog.load(_write(tmp_path, body))


def test_only_filters_by_group_and_case(tmp_path):
    cases = catalog.load(_write(tmp_path, GOOD))
    assert [c.id for c in catalog.select(cases, groups=["qa"], ids=[])] == ["total", "follow", "help"]
    assert [c.id for c in catalog.select(cases, groups=[], ids=["help"])] == ["help"]
    # a thread case pulls in the case it follows
    assert [c.id for c in catalog.select(cases, groups=[], ids=["follow"])] == ["total", "follow"]


def test_the_shipped_catalog_loads_and_covers_every_group():
    cases = catalog.load(SHIPPED)
    groups = {c.group for c in cases}
    assert groups == {"qa", "schedule", "refusal", "status", "columns"}
    assert {c.where for c in cases} >= {"dm", "channel", "thread"}
    # every task a case creates is changed or removed by a later case or by the clean-up
    assert any(c.expect.task == "created" for c in cases)


# -- review fixes and channel features --


@pytest.mark.parametrize("template", ["{{ ''.__class__.__mro__ }}", "{{ cycler.__init__.__globals__ }}",
                                      "{{ self.__init__.__globals__ }}"])
def test_templates_run_sandboxed(tmp_path, template):
    body = f'cases:\n  - {{id: a, group: qa, where: dm, text: "{template}"}}\n'
    (case,) = catalog.load(_write(tmp_path, body))
    with pytest.raises((MissingVar, CatalogError)):
        catalog.render(case, {})


@pytest.mark.parametrize(
    "body,match",
    [
        ("cases:\n  - {id: a, group: qa, where: dm, text: x, expect: {contains: abc}}\n", "list"),
        ("cases:\n  - {id: a, group: qa, where: dm, text: x, expect: {not_contains: [1]}}\n", "list"),
        ("cases:\n  - {id: a, group: qa, where: dm, text: x, expect: {deliver: {within_s: soon}}}\n", "within_s"),
        ("cases:\n  - {id: a, group: qa, where: dm, text: x, expect: {deliver: {within_s: 9, channel: moon}}}\n",
         "channel"),
        ("cases:\n  - {id: a, group: qa, where: dm, text: x, expect: {deliver: {within_s: 9, figures: 2}}}\n",
         "figures"),
        ("cases:\n  - {id: a, group: qa, where: dm, text: x, expect: {silent: {for_s: x}}}\n", "for_s"),
        ("cases:\n  - {id: a, group: qa, where: dm, text: x, timeout_s: long}\n", "timeout_s"),
        ("cases: [unclosed\n", "YAML"),
        ("- just a list\n", "mapping"),
        ("cases:\n  - just text\n", "mapping"),
    ],
)
def test_wrong_types_are_refused(tmp_path, body, match):
    with pytest.raises(CatalogError, match=match):
        catalog.load(_write(tmp_path, body))


def test_a_pattern_that_breaks_after_rendering_is_a_catalog_error(tmp_path):
    body = 'cases:\n  - {id: a, group: qa, where: dm, text: x, expect: {contains: ["{{ v }}"]}}\n'
    (case,) = catalog.load(_write(tmp_path, body))
    with pytest.raises(CatalogError, match="regex"):
        catalog.render(case, {"v": "("})


def test_deliver_and_silent_fields_load(tmp_path):
    body = """
cases:
  - id: a
    group: schedule
    where: dm
    text: x
    truth: {t: "S"}
    expect: {task: created, deliver: {within_s: 300, channel: test_channel, figures: true, contains: [y]}}
  - {id: b, group: schedule, where: dm, text: x, expect: {task: created, silent: {for_s: 200}}}
"""
    a, b = catalog.load(_write(tmp_path, body))
    assert a.expect.deliver == {"within_s": 300.0, "channel": "test_channel", "figures": True, "contains": ["y"]}
    assert b.expect.silent == {"for_s": 200.0}


EXPAND = """
cases:
  - {id: dm1, group: qa, where: dm, text: "plain"}
  - {id: ch, group: qa, where: channel, text: "q"}
  - {id: th, group: qa, where: thread, after: ch, text: "f"}
  - {id: post, group: schedule, where: dm, text: "post in <#{{ test_channel }}>", expect: {task: created}}
  - {id: rm, group: schedule, where: dm, text: "remove {{ tasks.post }}", expect: {task: removed, task_of: post}}
"""


def test_channel_cases_are_repeated_for_each_extra_channel(tmp_path):
    cases = catalog.load(_write(tmp_path, EXPAND))
    out = catalog.expand_channels(cases, ["C1", "C2"])
    assert [(c.id, c.channel) for c in out] == [
        ("dm1", None), ("ch", "C1"), ("th", "C1"), ("post", "C1"), ("rm", "C1"),
        ("ch@C2", "C2"), ("th@C2", "C2"), ("post@C2", "C2"), ("rm@C2", "C2"),
    ]
    th2 = next(c for c in out if c.id == "th@C2")
    rm2 = next(c for c in out if c.id == "rm@C2")
    assert th2.after == "ch@C2" and rm2.expect.task_of == "post@C2"


def test_no_channels_leaves_channel_cases_unbound(tmp_path):
    cases = catalog.load(_write(tmp_path, EXPAND))
    assert [c.channel for c in catalog.expand_channels(cases, [])] == [None] * 5


# -- the Slack web app version of the shipped catalog --

FULL_VARS = {
    "prev_month_name": "October 2026", "prev_month_start": "2026-10-01 00:00:00",
    "newest_month_name": "November 2026", "newest_month_start": "2026-11-01 00:00:00",
    "mtd_month_name": "November 2026", "mtd_month_start": "2026-11-01 00:00:00",
    "top_service": "Amazon EC2", "second_service": "Amazon S3", "top_account": "123456789012",
    "top_region": "us-east-1", "data_through": "2026-11-10", "me": "U0ME", "test_channel": "C0TEST",
    "outside_channel": "C0OUT", "test_channel_name": "cost-tests", "outside_channel_name": "no-bot",
}


def _shipped():
    return {c.id: c for c in catalog.load(SHIPPED)}


def test_shipped_catalog_drops_new_and_sends_help_with_a_leading_space():
    cases = _shipped()
    assert "qa_new" not in cases
    help_ = cases["qa_help"]
    assert help_.text == " /help"
    assert help_.expect.reaction == "done"
    assert help_.expect.contains == ["(?i)cost|aws"]


def test_month_to_date_uses_the_mtd_values():
    mtd = _shipped()["qa_month_to_date"]
    assert "mtd_month_name" in mtd.text
    assert "mtd_month_start" in mtd.truth["total"]
    with pytest.raises(MissingVar):  # a complete newest month has no mtd_* values: the case is SKIP
        catalog.render(mtd, {k: v for k, v in FULL_VARS.items() if not k.startswith("mtd_")})


def test_condition_not_met_must_stay_silent():
    assert _shipped()["sch_ask_not_met"].expect.silent == {"for_s": 300.0}


def test_channel_post_soon_is_delivered_in_the_test_channel_with_figures():
    case = _shipped()["sch_channel_post_soon"]
    assert case.where == "dm" and case.expect.task == "created"
    assert case.expect.deliver["channel"] == "test_channel"
    assert case.expect.deliver["figures"] is True
    assert case.truth
    r = catalog.render(case, {**FULL_VARS, "tasks": {}})
    assert r.text == ("In 2 minutes, post last month's total AWS cost and top service in #cost-tests")
    # it depends on the test channel, so it runs once per channel
    out = catalog.expand_channels(list(catalog.load(SHIPPED)), ["C1", "C2"])
    assert {c.id for c in out} >= {"sch_channel_post_soon", "sch_channel_post_soon@C2"}


def test_every_shipped_case_renders_with_the_full_vars():
    tasks = {c.id: f"t{i}" for i, c in enumerate(catalog.load(SHIPPED), start=1)}
    for case in catalog.load(SHIPPED):
        catalog.render(case, {**FULL_VARS, "tasks": tasks})


def test_the_shipped_catalog_types_channel_names_not_ids():
    # a person types "#cost-reports"; "<#C…>" made the agent's shell guard refuse the command (live 2026-11-11)
    text = open(SHIPPED, encoding="utf-8").read()
    assert "<#" not in text
    assert "#{{ test_channel_name }}" in text and "#{{ outside_channel_name }}" in text
