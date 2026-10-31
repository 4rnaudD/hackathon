"""Buyer-side liquidity optimiser.

Given early-pay requests, decide which to fund from the buyer's own cash
(earning the early-payment discount) and which to leave to funders.

Paying invoice i early moves its outflow from due_i to pay_i: the balance is
lower by `advance_i` on days [pay_i, due_i) and higher by the discount from
due_i onwards. We maximise discount income net of the cash yield forgone,
subject to the balance never dropping below the floor on any day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp


@dataclass(frozen=True)
class Candidate:
    invoice_id: str
    amount: int  # face value, cents
    advance: int  # what the buyer would pay early, cents
    pay_date: date
    due_date: date

    @property
    def discount(self) -> int:
        return self.amount - self.advance

    @property
    def days_early(self) -> int:
        return (self.due_date - self.pay_date).days

    def net_value(self, cash_yield: float) -> float:
        """Discount earned minus interest the cash would have earned in the meantime."""
        return self.discount - self.advance * cash_yield * self.days_early / 365


@dataclass(frozen=True)
class Decision:
    invoice_id: str
    fund: bool
    reason: str
    net_value: float


def _delta(c: Candidate, day: date) -> int:
    if c.pay_date <= day < c.due_date:
        return -c.advance
    if day >= c.due_date:
        return c.discount
    return 0


def choose_self_funded(
    candidates: list[Candidate],
    baseline: dict[date, int],
    floor: int,
    cash_yield: float,
    method: str = "milp",
) -> list[Decision]:
    """Pick which candidates the buyer funds itself.

    `baseline` is the projected end-of-day balance per day with no new early
    payments. Days already below the floor are not made any worse.
    """
    decisions: dict[str, Decision] = {}
    viable = []
    for c in candidates:
        v = c.net_value(cash_yield)
        if c.days_early <= 0:
            decisions[c.invoice_id] = Decision(c.invoice_id, False, "not early", v)
        elif v <= 0:
            decisions[c.invoice_id] = Decision(
                c.invoice_id, False, f"discount below cash yield ({cash_yield:.1%})", v
            )
        else:
            viable.append(c)

    if viable:
        days = sorted(baseline)
        selected = (
            _solve_milp(viable, baseline, days, floor, cash_yield)
            if method == "milp"
            else None
        )
        if selected is None:  # solver unavailable/failed, or greedy requested
            selected = _greedy(viable, baseline, days, floor, cash_yield)
        for c in viable:
            v = c.net_value(cash_yield)
            if c.invoice_id in selected:
                decisions[c.invoice_id] = Decision(c.invoice_id, True, "self-fund", v)
            else:
                decisions[c.invoice_id] = Decision(c.invoice_id, False, "cash floor binding", v)

    return [decisions[c.invoice_id] for c in candidates]


def _limit(baseline: dict[date, int], day: date, floor: int) -> int:
    return min(floor, baseline[day])


def _solve_milp(viable, baseline, days, floor, cash_yield) -> set[str] | None:
    """Binary knapsack with one cash-floor constraint per day, solved with HiGHS."""
    values = np.array([c.net_value(cash_yield) for c in viable])
    rows, lower = [], []
    for day in days:
        row = [_delta(c, day) for c in viable]
        if any(row):
            rows.append(row)
            lower.append(_limit(baseline, day, floor) - baseline[day])
    constraints = [LinearConstraint(np.array(rows, dtype=float), lb=np.array(lower, dtype=float))] if rows else []
    try:
        res = milp(
            c=-values,  # milp minimises
            integrality=np.ones(len(viable)),
            bounds=Bounds(0, 1),
            constraints=constraints,
        )
    except ValueError:
        return None
    if not res.success:
        return None
    return {c.invoice_id for c, v in zip(viable, res.x) if v > 0.5}


def _greedy(viable, baseline, days, floor, cash_yield) -> set[str]:
    """Deterministic fallback: take best value-per-dollar first while the floor holds."""
    balance = dict(baseline)
    chosen: set[str] = set()
    for c in sorted(viable, key=lambda c: (-c.net_value(cash_yield) / c.advance, c.invoice_id)):
        if all(balance[d] + _delta(c, d) >= _limit(baseline, d, floor) for d in days):
            for d in days:
                balance[d] += _delta(c, d)
            chosen.add(c.invoice_id)
    return chosen


def date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]
