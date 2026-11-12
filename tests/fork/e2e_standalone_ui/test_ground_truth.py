"""Placeholders and expected figures come from a copy of focus.duckdb (tests/e2e/standalone/ground_truth.py)."""

from datetime import datetime

import duckdb
import pytest

from tests.e2e.standalone_ui.ground_truth import SnapshotError, Truth, snapshot

ROWS = [
    # billingperiodstart, chargeperiodstart, chargeperiodend, billedcost, servicename, subaccountid, regionid, category
    ("2026-10-01", "2026-10-05 00:00", "2026-10-05 01:00", 100.0, "Amazon EC2", "111", "us-east-1", "Usage"),
    ("2026-10-01", "2026-10-06 00:00", "2026-10-06 01:00", 50.0, "Amazon EC2", "111", "us-east-1", "Usage"),
    ("2026-10-01", "2026-10-07 00:00", "2026-10-07 01:00", 30.0, "Amazon S3", "222", "eu-west-1", "Usage"),
    ("2026-10-01", "2026-10-01 00:00", "2026-11-01 00:00", 5.0, "Tax", "111", None, "Tax"),
    ("2026-11-01", "2026-11-02 00:00", "2026-11-02 01:00", 20.0, "Amazon S3", "222", "eu-west-1", "Usage"),
    ("2026-11-01", "2026-11-03 10:00", "2026-11-03 11:00", 7.0, "Amazon EC2", "111", "us-east-1", "Usage"),
]


def _make(path, rows=ROWS):
    c = duckdb.connect(str(path))
    c.execute(
        "CREATE TABLE focus (billingperiodstart TIMESTAMP, chargeperiodstart TIMESTAMP, chargeperiodend TIMESTAMP, "
        "billedcost DOUBLE, servicename VARCHAR, subaccountid VARCHAR, regionid VARCHAR, chargecategory VARCHAR)"
    )
    for r in rows:
        c.execute("INSERT INTO focus VALUES (?, ?, ?, ?, ?, ?, ?, ?)", r)
    c.close()
    return str(path)


@pytest.fixture
def truth(tmp_path):
    t = Truth(_make(tmp_path / "focus.duckdb"))
    yield t
    t.close()


def test_vars_describe_the_newest_and_previous_month(truth):
    v = truth.vars()
    assert v["newest_month"] == "2026-11"
    assert v["newest_month_name"] == "November 2026"
    assert v["newest_month_start"] == "2026-11-01 00:00:00"
    assert v["prev_month"] == "2026-10"
    assert v["prev_month_name"] == "October 2026"
    assert v["prev_month_start"] == "2026-10-01 00:00:00"
    assert v["data_through"] == "2026-11-03"


def test_vars_pick_the_top_service_account_and_region_of_the_previous_month(truth):
    v = truth.vars()
    assert v["top_service"] == "Amazon EC2"
    assert v["second_service"] == "Amazon S3"
    assert v["top_account"] == "111"
    assert v["top_region"] == "us-east-1"


def test_value_returns_the_first_cell_as_a_number(truth):
    sql = "SELECT sum(billedcost) FROM focus WHERE billingperiodstart = TIMESTAMP '2026-10-01 00:00:00'"
    assert truth.value(sql) == pytest.approx(185.0)


def test_value_is_none_when_nothing_matches(truth):
    assert truth.value("SELECT sum(billedcost) FROM focus WHERE servicename = 'nope'") is None
    assert truth.value("SELECT billedcost FROM focus WHERE servicename = 'nope'") is None


def test_the_copy_is_opened_read_only(truth):
    with pytest.raises(duckdb.Error):
        truth.value("DELETE FROM focus RETURNING 1")


def test_an_empty_table_gives_no_month_vars(tmp_path):
    t = Truth(_make(tmp_path / "empty.duckdb", rows=[]))
    try:
        v = t.vars()
    finally:
        t.close()
    assert "newest_month" not in v and "top_service" not in v


def test_one_month_only_gives_no_previous_month(tmp_path):
    t = Truth(_make(tmp_path / "one.duckdb", rows=[r for r in ROWS if r[0] == "2026-11-01"]))
    try:
        v = t.vars()
    finally:
        t.close()
    assert v["newest_month"] == "2026-11" and "prev_month" not in v and "top_service" not in v


class _Run:
    def __init__(self, rc=0, err=""):
        self.calls, self.rc, self.err = [], rc, err

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return type("P", (), {"returncode": self.rc, "stderr": self.err, "stdout": ""})()


def test_snapshot_copies_with_docker_cp_and_no_shell(tmp_path):
    run = _Run()
    dest = str(tmp_path / "copy.duckdb")
    assert snapshot("agent-run-tasks", dest, run=run) == dest
    argv, kw = run.calls[0]
    assert argv == ["docker", "cp", "agent-run-tasks:/data/focus.duckdb", dest]
    assert kw.get("shell") in (None, False)


@pytest.mark.parametrize("name", ["", "a;rm -rf /", "x y", "-rm", "$(id)", "a/b"])
def test_snapshot_refuses_odd_container_names(name, tmp_path):
    with pytest.raises(SnapshotError):
        snapshot(name, str(tmp_path / "c.duckdb"), run=_Run())


def test_snapshot_failure_says_why(tmp_path):
    with pytest.raises(SnapshotError, match="No such container"):
        snapshot("gone", str(tmp_path / "c.duckdb"), run=_Run(rc=1, err="Error: No such container: gone"))


def test_vars_are_plain_strings(truth):
    assert all(isinstance(v, str) for v in truth.vars().values())


def test_data_through_ignores_tax_rows(truth):
    # the October tax row ends 2026-11-01, but the Usage rows decide "data through"
    assert truth.vars()["data_through"] == datetime(2026, 11, 3).strftime("%Y-%m-%d")


# -- review fixes --


def test_a_complete_newest_month_is_the_previous_month_and_has_no_month_to_date(tmp_path):
    rows = [r for r in ROWS if r[0] == "2026-10-01"] + [
        ("2026-09-01", "2026-09-03 00:00", "2026-09-03 01:00", 9.0, "Amazon EC2", "111", "us-east-1", "Usage")]
    # October's usage runs to its last hour: the newest month is complete
    rows.append(("2026-10-01", "2026-10-31 23:00", "2026-11-01 00:00", 1.0, "Amazon EC2", "111", "us-east-1", "Usage"))
    t = Truth(_make(tmp_path / "complete.duckdb", rows=rows))
    try:
        v = t.vars()
    finally:
        t.close()
    assert v["prev_month"] == "2026-10" and "mtd_month_name" not in v


def test_a_partial_newest_month_gives_month_to_date_vars(truth):
    v = truth.vars()
    assert v["mtd_month_name"] == "November 2026" and v["mtd_month_start"] == "2026-11-01 00:00:00"


def test_top_service_ranks_every_charge_like_the_expected_cost(tmp_path):
    rows = [
        ("2026-10-01", "2026-10-05 00:00", "2026-10-05 01:00", 100.0, "Amazon EC2", "111", "us-east-1", "Usage"),
        ("2026-10-01", "2026-10-05 00:00", "2026-10-05 01:00", 90.0, "Amazon S3", "111", "us-east-1", "Usage"),
        ("2026-10-01", "2026-10-01 00:00", "2026-11-01 00:00", 20.0, "Amazon S3", "111", "us-east-1", "Purchase"),
        ("2026-11-01", "2026-11-02 00:00", "2026-11-02 01:00", 1.0, "Amazon EC2", "111", "us-east-1", "Usage"),
    ]
    t = Truth(_make(tmp_path / "rank.duckdb", rows=rows))
    try:
        assert t.vars()["top_service"] == "Amazon S3"
    finally:
        t.close()
