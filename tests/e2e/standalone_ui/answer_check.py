"""Find the figures in a bot reply and match them to the expected values.

A figure is money (`$1,234.56`, `$1.2K`, `$1.2 million`, `-$45.10`, `($45.10)`, `USD 12.30`, `1,050.00 USD`) or a
percent (`12.5%`). A figure in parentheses, or after a minus sign (ASCII `-` or Unicode U+2212), is negative. Each
figure keeps its display precision (half of its last shown digit), so `$1.2K` matches anything from 1,150 to 1,250.
"""

import re
from dataclasses import dataclass
from typing import Mapping, Optional

_SCALE = {"": 1.0, "k": 1e3, "m": 1e6, "b": 1e9, "thousand": 1e3, "million": 1e6, "billion": 1e9}
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MINUS_SIGNS = ("-", "−")  # ASCII hyphen-minus and the Unicode minus sign
_MINUS = "-−"
_MONEY = re.compile(
    rf"(?P<lp>\()?(?P<neg1>[{_MINUS}])?\$\s?(?P<neg2>[{_MINUS}])?(?P<n1>{_NUM})"
    rf"(?:\s?(?P<s1>(?i:thousand|million|billion)|[KkMmBb])\b)?(?P<rp>\))?"
    rf"|\bUSD\s?(?P<neg3>[{_MINUS}])?(?P<n2>{_NUM})"
    rf"|(?P<neg4>[{_MINUS}])?(?P<n3>{_NUM})\s?USD\b"
)
_PERCENT = re.compile(rf"(?P<neg>[{_MINUS}+])?(?P<n>{_NUM})\s?%")
# A plain quantity (GB, requests, hours): a number outside every money and percent figure, with an optional scale.
_NUMBER = re.compile(
    rf"(?<![\w.])(?P<neg>[{_MINUS}])?(?P<n>{_NUM})(?:\s?(?P<s>(?i:thousand|million|billion)|[KkMmBb])\b)?")


@dataclass(frozen=True)
class Figure:
    value: float
    kind: str  # "money" | "percent" | "number"
    precision: float
    text: str
    # `($45.10)`: read as an accounting negative, but it may just be an aside ("September ($1,097.39)"), so it
    # matches an expected value of either sign
    parenthesized: bool = False


def _precision(number: str, scale: float) -> float:
    decimals = len(number.split(".", 1)[1]) if "." in number else 0
    return 0.5 * 10 ** (-decimals) * scale


def figures(text: str) -> list[Figure]:
    """Every money and percent figure in `text`, in order of appearance within each kind."""
    found: list[Figure] = []
    money_spans = []
    for m in _MONEY.finditer(text):
        number = m.group("n1") or m.group("n2") or m.group("n3")
        scale = _SCALE[(m.group("s1") or "").lower()]
        minus = any(m.group(g) for g in ("neg1", "neg2", "neg3", "neg4"))
        paren = bool(m.group("lp") and m.group("rp")) and not minus
        value = float(number.replace(",", "")) * scale * (-1 if minus or paren else 1)
        found.append(Figure(value, "money", _precision(number.replace(",", ""), scale), m.group(0), paren))
        money_spans.append(m.span())
    for m in _PERCENT.finditer(text):
        if any(a <= m.start() < b for a, b in money_spans):
            continue
        value = float(m.group("n").replace(",", "")) * (-1 if m.group("neg") in _MINUS_SIGNS else 1)
        found.append(Figure(value, "percent", _precision(m.group("n"), 1.0), m.group(0)))
        money_spans.append(m.span())
    for m in _NUMBER.finditer(text):
        if any(a <= m.start() < b for a, b in money_spans):
            continue
        number = m.group("n").replace(",", "")
        scale = _SCALE[(m.group("s") or "").lower()]
        value = float(number) * scale * (-1 if m.group("neg") else 1)
        found.append(Figure(value, "number", _precision(number, scale), m.group(0)))
    return found


def _close(fig: Figure, expected: float, tol_pct: float, tol_abs: float, ignore_sign: bool) -> bool:
    ignore_sign = ignore_sign or fig.parenthesized
    value, want = (abs(fig.value), abs(expected)) if ignore_sign else (fig.value, expected)
    allowed = max(tol_abs, abs(want) * tol_pct / 100.0, fig.precision)
    return abs(value - want) <= allowed + 1e-9


def match_figures(
    text: str,
    expected: Mapping[str, float],
    *,
    tol_pct: float = 1.0,
    tol_abs: float = 0.01,
    kinds: Optional[Mapping[str, str]] = None,
    ignore_sign: bool = False,
) -> list[str]:
    """The names of the expected values that no figure of the right kind in `text` matches (empty = all found)."""
    kinds = kinds or {}
    found = figures(text)
    missing = []
    for name, want in expected.items():
        kind = kinds.get(name, "money")
        if not any(f.kind == kind and _close(f, want, tol_pct, tol_abs, ignore_sign) for f in found):
            missing.append(name)
    return missing
