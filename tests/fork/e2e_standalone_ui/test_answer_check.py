"""Figures in a bot reply are found and matched to the expected values (tests/e2e/standalone/answer_check.py)."""

import pytest

from tests.e2e.standalone_ui.answer_check import Figure, figures, match_figures


def _values(text, kind="money"):
    return [f.value for f in figures(text) if f.kind == kind]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("You spent $1,234.56 in October.", [1234.56]),
        ("EC2 was $12 and S3 $0.40", [12.0, 0.40]),
        ("Total: $1.2K this month", [1200.0]),
        ("about $3.4M so far", [3_400_000.0]),
        ("roughly $2B", [2_000_000_000.0]),
        ("Credits were -$45.10", [-45.10]),
        ("Credits were $-45.10", [-45.10]),
        ("It came to USD 12.30 overall", [12.30]),
        ("It came to 1,050.00 USD overall", [1050.0]),
        ("no money here, only t3 and 2026", []),
    ],
)
def test_money_figures_are_found(text, expected):
    assert _values(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Total was $1.2 million last month", [1_200_000.0]),
        ("Total was $1.2 Million last month", [1_200_000.0]),
        ("about $3 billion", [3_000_000_000.0]),
        ("about $3 BILLION", [3_000_000_000.0]),
        ("just $2.5 thousand", [2_500.0]),
        ("Credits were ($45.10) this month", [-45.10]),
        ("Credits were ($1,045.10)", [-1045.10]),
        ("Credits were −$45.10", [-45.10]),
        ("Credits were $−45.10", [-45.10]),
        ("EC2 ($12.00 of it) was compute", [12.0]),
    ],
)
def test_scale_words_parentheses_and_unicode_minus(text, expected):
    assert _values(text) == pytest.approx(expected)


def test_scale_word_carries_its_precision():
    (fig,) = figures("$1.2 million")
    assert fig.precision == pytest.approx(50_000.0)
    assert match_figures("$1.2 million", {"total": 1_234_000.0}) == []
    assert match_figures("$1.2 million", {"total": 1_300_000.0}) == ["total"]


def test_parenthesized_figure_matches_either_sign():
    # "($45.10)" is an accounting negative, but "September ($1,097.39)" is just an aside
    assert match_figures("Credits were ($45.10)", {"credits": -45.10}) == []
    assert match_figures("September ($1,097.39)", {"sep": 1097.39}) == []
    # an explicit minus keeps the sign strict
    assert match_figures("Credits were −$45.10", {"credits": 45.10}) == ["credits"]


def test_unicode_minus_on_a_percent():
    assert _values("S3 fell −3%", "percent") == pytest.approx([-3.0])


def test_percent_figures_are_found_apart_from_money():
    text = "Up 12.5% to $100.00, while S3 fell -3%"
    assert _values(text, "percent") == pytest.approx([12.5, -3.0])
    assert _values(text) == pytest.approx([100.0])


def test_shorthand_figures_carry_their_display_precision():
    (fig,) = figures("$1.2K")
    assert isinstance(fig, Figure)
    assert fig.precision == pytest.approx(50.0)  # half of the last shown digit (0.1K)
    (exact,) = figures("$1,234.56")
    assert exact.precision == pytest.approx(0.005)


def test_all_expected_figures_present_means_nothing_missing():
    reply = "October was $1,234.56, up 12.5% on September ($1,097.39)."
    assert match_figures(reply, {"october": 1234.561, "september": 1097.385}) == []


def test_a_missing_figure_is_named():
    assert match_figures("October was $1,234.56.", {"october": 1234.56, "september": 999.0}) == ["september"]


def test_relative_tolerance_applies():
    assert match_figures("about $1,010.00", {"total": 1000.0}, tol_pct=1.0) == []
    assert match_figures("about $1,020.00", {"total": 1000.0}, tol_pct=1.0) == ["total"]


def test_shorthand_matches_within_its_rounding():
    assert match_figures("$1.2K", {"total": 1234.0}) == []
    assert match_figures("$1.2K", {"total": 1300.0}) == ["total"]


def test_absolute_tolerance_for_cents():
    assert match_figures("$0.01", {"tiny": 0.014}, tol_pct=0.0) == []


def test_percent_expectations_match_percent_figures_only():
    reply = "Up 12.5% to $12.50"
    assert match_figures(reply, {"change_pct": 12.5}, kinds={"change_pct": "percent"}) == []
    assert match_figures("Up to $12.50", {"change_pct": 12.5}, kinds={"change_pct": "percent"}) == ["change_pct"]


def test_sign_is_ignored_when_asked():
    # "down 3%" is often written without a minus sign
    assert match_figures("down 3%", {"chg": -3.0}, kinds={"chg": "percent"}, ignore_sign=True) == []
    assert match_figures("down 3%", {"chg": -3.0}, kinds={"chg": "percent"}) == ["chg"]
