"""The `columns` e2e group: natural-language questions over every FOCUS column, checked against the data copy.

New harness parts: a `number` figure kind (GB, requests, hours), `truth_text` (SQL whose text value must appear in the
reply: a bucket, an account, a region), and `covers` (the columns a case exercises; the shipped catalog covers all 60).
"""
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from tests.e2e.standalone_ui import catalog  # noqa: E402
from tests.e2e.standalone_ui.answer_check import figures, match_figures  # noqa: E402
from tests.e2e.standalone_ui.catalog import CatalogError, Expect  # noqa: E402
from tests.e2e.standalone_ui.checks import judge_reply  # noqa: E402
from tests.e2e.standalone_ui.ground_truth import Truth  # noqa: E402
from tests.fork.e2e_standalone_ui.test_catalog import FULL_VARS  # noqa: E402
from tests.fork.pico_finops_agent.test_focus_query_flow import CACHE_COLUMNS, fixture_rows  # noqa: E402

SHIPPED = str(Path(__file__).resolve().parents[3] / "tests" / "e2e" / "standalone_ui" / "catalog.yaml")


def _load(tmp_path, body):
    p = tmp_path / "c.yaml"
    p.write_text("cases:\n" + body, encoding="utf-8")
    return catalog.load(str(p))


# ---------- the number kind ----------

@pytest.mark.parametrize("text,want", [
    ("The bucket used 0.58 GB-months.", 0.583),
    ("We made 44,340 requests.", 44340.0),
    ("about 44.3K requests", 44340.0),
    ("roughly 1.2 million requests", 1_200_000.0),
    ("EC2 ran 1,512 hours", 1512.0),
])
def test_number_kind_finds_plain_quantities(text, want):
    assert match_figures(text, {"q": want}, kinds={"q": "number"}) == []


def test_number_kind_does_not_take_money_or_percent_digits():
    assert match_figures("It cost $44.34 (12%).", {"q": 44.34}, kinds={"q": "number"}) == ["q"]
    assert match_figures("It cost $44.34 (12%).", {"q": 12.0}, kinds={"q": "number"}) == ["q"]
    assert [f.kind for f in figures("$5 and 7%")] == ["money", "percent"]  # money/percent unchanged


# ---------- truth_text and covers in the catalog ----------

def test_truth_text_and_covers_load_and_render(tmp_path):
    (case,) = _load(tmp_path, (
        '  - {id: col_a, group: columns, where: dm, text: "Top bucket in {{ prev_month_name }}?",'
        ' truth_text: {bucket: "SELECT b FROM t WHERE m = \'{{ prev_month_start }}\'"},'
        ' kinds: {}, covers: [resourceid, pricingunit]}\n'))
    assert case.group == "columns" and case.covers == ("resourceid", "pricingunit")
    r = catalog.render(case, {"prev_month_name": "October 2026", "prev_month_start": "2026-10-01 00:00:00"})
    assert r.truth_text == {"bucket": "SELECT b FROM t WHERE m = '2026-10-01 00:00:00'"}


@pytest.mark.parametrize("body,match", [
    ('  - {id: c, group: columns, where: dm, text: q, truth_text: {a: 1}}\n', "truth_text value must be a SQL string"),
    ('  - {id: c, group: columns, where: dm, text: q, covers: resourceid}\n', "covers must be a list of column names"),
    ('  - {id: c, group: columns, where: dm, text: q, kinds: {a: number}}\n', "kinds must name truth values"),
])
def test_bad_truth_text_and_covers_are_refused(tmp_path, body, match):
    with pytest.raises(CatalogError, match=match):
        _load(tmp_path, body)


def test_number_is_a_known_kind(tmp_path):
    (case,) = _load(tmp_path, '  - {id: c, group: columns, where: dm, text: q, truth: {gb: "SELECT 1"},'
                              ' kinds: {gb: number}}\n')
    assert case.kinds == {"gb": "number"}


# ---------- judging names ----------

def _by_name(checks):
    return {c.name.split(" ", 1)[1]: c for c in checks}


def test_names_pass_when_each_value_is_in_the_reply_ignoring_case():
    checks = _by_name(judge_reply("c", Expect(), "done", ["The top bucket was *Logs-Bucket* in us-east-1"],
                                  truth_texts={"bucket": "logs-bucket", "region": "US-EAST-1"}))
    assert checks["names"].status == "PASS"


def test_names_fail_and_say_which_is_missing():
    checks = _by_name(judge_reply("c", Expect(), "done", ["The top bucket was logs"],
                                  truth_texts={"bucket": "logs-bucket"}))
    assert checks["names"].status == "FAIL" and "logs-bucket" in checks["names"].detail


def test_names_with_no_value_in_the_data_are_skipped():
    checks = _by_name(judge_reply("c", Expect(), "done", ["none"], truth_texts={"az": None}))
    assert checks["names"].status == "SKIP"


# ---------- Truth.text ----------

def test_truth_text_reads_the_first_cell_as_text(tmp_path):
    path = tmp_path / "f.duckdb"
    c = duckdb.connect(str(path))
    c.execute("CREATE TABLE t (name VARCHAR, n INT)")
    c.execute("INSERT INTO t VALUES ('logs-bucket', 3)")
    c.close()
    t = Truth(str(path))
    try:
        assert t.text("SELECT name FROM t") == "logs-bucket"
        assert t.text("SELECT n FROM t") == "3"
        assert t.text("SELECT name FROM t WHERE n = 0") is None
    finally:
        t.close()


# ---------- the shipped `columns` cases ----------

def _columns_cases():
    return [c for c in catalog.load(SHIPPED) if c.group == "columns"]


def test_the_columns_group_covers_every_focus_column():
    covered = {col for c in _columns_cases() for col in c.covers}
    assert not set(CACHE_COLUMNS) - covered, sorted(set(CACHE_COLUMNS) - covered)
    assert covered <= set(CACHE_COLUMNS), sorted(covered - set(CACHE_COLUMNS))


def test_every_columns_case_checks_something_and_says_what_it_covers():
    cases = _columns_cases()
    assert len(cases) >= 40
    for c in cases:
        assert c.covers, c.id
        assert c.truth or c.truth_text or c.expect.contains or c.expect.contains_any, c.id


def test_the_columns_group_mixes_cost_data_information_and_combinations():
    cases = _columns_cases()
    money = [c for c in cases if any(c.kinds.get(k, "money") == "money" for k in c.truth)]
    data = [c for c in cases if "number" in c.kinds.values()]
    info = [c for c in cases if c.truth_text]
    combos = [c for c in cases if len(c.covers) >= 3]
    texts = " ".join(c.text.lower() for c in cases)
    assert len(money) >= 15 and len(data) >= 4 and len(info) >= 10 and len(combos) >= 6
    for word in ("tax", "refund", "credit", "tag", "bucket", "region", "account", "discount"):
        assert word in texts, word


@pytest.fixture(scope="module")
def all_columns_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("cols") / "focus.duckdb"
    c = duckdb.connect(str(path))
    c.sql("CREATE TABLE focus (" + ", ".join(f"{n} {t}" for n, t in CACHE_COLUMNS.items()) + ")")
    names = list(CACHE_COLUMNS)
    c.executemany(f"INSERT INTO focus VALUES ({', '.join('?' * len(names))})",
                  [[r[n] for n in names] for r in fixture_rows()])
    c.close()
    t = Truth(str(path))
    yield t
    t.close()


def test_every_columns_truth_sql_runs_on_an_all_columns_table(all_columns_db):
    vars_ = {**FULL_VARS, "prev_month_start": "2026-10-01 00:00:00", "newest_month_start": "2026-11-01 00:00:00",
             "tasks": {}}
    for case in _columns_cases():
        r = catalog.render(case, vars_)
        for sql in r.truth.values():
            all_columns_db.value(sql)  # raises on a bad column or syntax
        for sql in r.truth_text.values():
            all_columns_db.text(sql)
