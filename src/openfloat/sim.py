"""Synthetic scenario generator and evaluation.

All data here is SYNTHETIC. Parameter defaults are anchored to the sourced
ranges in docs/EVIDENCE.md (SME factoring ~30-57% APR, investment-grade
reverse factoring ~benchmark + 60-100 bps, B2B card fees ~2%).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from .messages import Invoice, OfferKind
from .network import Network
from .nodes import (
    BuyerNode,
    BuyerPolicy,
    FunderNode,
    FunderPolicy,
    SupplierNode,
    SupplierProfile,
    discounted,
)


@dataclass
class ScenarioConfig:
    seed: int = 7
    start: date = date(2026, 11, 2)
    days: int = 30  # days during which invoices are issued
    n_suppliers: int = 25
    invoices_per_day: float = 6.0
    mean_invoice: int = 25_000_00  # cents
    terms: tuple[int, ...] = (30, 45, 60, 90)
    p_want_early: float = 0.6
    p_accepts_card: float = 0.5
    p_double_dip: float = 0.05
    factoring_apr: tuple[float, float] = (0.20, 0.45)
    # buyer
    opening_cash: int = 3_000_000_00
    cash_floor: int = 1_500_000_00
    daily_inflow: int = 150_000_00
    cash_yield: float = 0.040  # e.g. money-market fund yield
    discount_apr: float = 0.050
    approval_limit: int = 150_000_00
    optimiser: str = "milp"
    use_forecast: bool = True  # buyer forecasts payables it hasn't received yet
    # funders: (id, kind, limit, apr, card_fee)
    funders: list[tuple[str, OfferKind, int, float, float]] = field(
        default_factory=lambda: [
            ("bank-a", OfferKind.FUNDER, 1_500_000_00, 0.055, 0.0),
            ("bank-b", OfferKind.FUNDER, 1_500_000_00, 0.062, 0.0),
            ("card-co", OfferKind.CARD, 500_000_00, 0.0, 0.020),
        ]
    )


@dataclass
class Scenario:
    config: ScenarioConfig
    net: Network
    buyer: BuyerNode
    suppliers: list[SupplierNode]
    funders: list[FunderNode]
    schedule: dict[date, list[tuple[SupplierNode, Invoice, bool]]]

    def run(self, extra_days: int | None = None) -> None:
        """Issue invoices for `config.days`, then run until every invoice matures."""
        last_due = max((inv.due_date for day in self.schedule.values() for _, inv, _ in day), default=self.net.today)
        end = last_due + timedelta(days=1) if extra_days is None else self.net.today + timedelta(days=self.config.days + extra_days)
        while self.net.today <= end:
            todays = self.schedule.get(self.net.today, [])
            self.net.run_day(lambda n, todays=todays: [s.issue(inv, early) for s, inv, early in todays])

    def step(self) -> None:
        todays = self.schedule.get(self.net.today, [])
        self.net.run_day(lambda n: [s.issue(inv, early) for s, inv, early in todays])


def build(cfg: ScenarioConfig) -> Scenario:
    rng = random.Random(cfg.seed)
    net = Network(cfg.start)
    sup_ids = [f"sup-{i:02d}" for i in range(cfg.n_suppliers)]
    horizon = cfg.days + max(cfg.terms) + 2
    inflows = {cfg.start + timedelta(days=d): cfg.daily_inflow for d in range(horizon)}
    buyer = BuyerNode(
        "acme-buyer",
        net,
        BuyerPolicy(
            opening_cash=cfg.opening_cash, cash_floor=cfg.cash_floor, cash_yield=cfg.cash_yield,
            discount_apr=cfg.discount_apr, approval_limit=cfg.approval_limit, daily_inflows=inflows, optimiser=cfg.optimiser,
            payables_forecast=_run_rate_forecast(cfg) if cfg.use_forecast else None,
        ),
        set(sup_ids),
    )
    funders = [
        FunderNode(fid, net, FunderPolicy(kind, limit=limit, apr=apr, card_fee=fee))
        for fid, kind, limit, apr, fee in cfg.funders
    ]
    suppliers = [
        SupplierNode(
            sid, net,
            SupplierProfile(
                factoring_apr=round(rng.uniform(*cfg.factoring_apr), 3),
                accepts_card=rng.random() < cfg.p_accepts_card,
                double_dip=rng.random() < cfg.p_double_dip,
            ),
        )
        for sid in sup_ids
    ]
    schedule: dict[date, list] = {}
    n = 0
    for d in range(cfg.days):
        day = cfg.start + timedelta(days=d)
        for _ in range(_poisson(rng, cfg.invoices_per_day)):
            s = rng.choice(suppliers)
            amount = max(1_000_00, int(rng.lognormvariate(0, 0.8) * cfg.mean_invoice / 1.377))
            n += 1
            inv = Invoice(
                invoice_id=f"INV-{n:04d}", supplier_id=s.id, buyer_id=buyer.id, amount=amount,
                issue_date=day, due_date=day + timedelta(days=rng.choice(cfg.terms)),
                description=rng.choice(["Packaging materials", "Logistics services", "Electronic components", "Raw resin", "Contract labour"]),
                po_number=f"PO-{rng.randint(10000, 99999)}",
            )
            schedule.setdefault(day, []).append((s, inv, rng.random() < cfg.p_want_early))
    return Scenario(cfg, net, buyer, suppliers, funders, schedule)


def _run_rate_forecast(cfg: ScenarioConfig):
    """Expected maturities on `day` from invoices issued after `as_of` (run-rate forecast).

    Uses only the configured averages, never the actual future invoices.
    """
    per_term = cfg.invoices_per_day * cfg.mean_invoice / len(cfg.terms)
    last_issue = cfg.start + timedelta(days=cfg.days - 1)

    def forecast(as_of: date, day: date) -> int:
        total = 0
        for term in cfg.terms:
            issue = day - timedelta(days=term)
            if as_of < issue <= last_issue:
                total += per_term
        return int(total)

    return forecast


def _poisson(rng: random.Random, lam: float) -> int:
    # Knuth; fine for small lambda
    L, k, p = pow(2.718281828459045, -lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


# --- evaluation -------------------------------------------------------------


@dataclass
class Metrics:
    invoices: int = 0
    early_requests: int = 0
    financed: int = 0
    by_route: dict[str, int] = field(default_factory=dict)
    face_financed: int = 0
    supplier_cost_openfloat: int = 0  # face - received, for financed invoices
    supplier_cost_factoring: int = 0  # same invoices at each supplier's own factoring rate
    buyer_discount_income: int = 0
    buyer_yield_forgone: float = 0.0
    double_financing_blocked: int = 0
    bad_signatures: int = 0
    min_balance: int = 0
    floor: int = 0
    floor_breaches_caused: int = 0

    @property
    def supplier_saving(self) -> int:
        return self.supplier_cost_factoring - self.supplier_cost_openfloat

    @property
    def buyer_net_gain(self) -> float:
        return self.buyer_discount_income - self.buyer_yield_forgone


def evaluate(sc: Scenario) -> Metrics:
    m = Metrics(floor=sc.config.cash_floor)
    route_of = {}
    for d in sc.net.decisions:
        if d.decision.startswith("accepted ") and d.node.startswith("sup-"):
            route_of[d.invoice_id] = d.decision.split()[1]
    for s in sc.suppliers:
        for r in s.receivables.values():
            m.invoices += 1
            if r.want_early and r.undertaking is not None:
                m.early_requests += 1
            if r.received and r.received_on and r.received_on < r.invoice.due_date:
                m.financed += 1
                route = route_of.get(r.invoice.invoice_id, "?")
                m.by_route[route] = m.by_route.get(route, 0) + 1
                m.face_financed += r.invoice.amount
                m.supplier_cost_openfloat += r.invoice.amount - r.received
                days = (r.invoice.due_date - r.invoice.issue_date).days
                m.supplier_cost_factoring += r.invoice.amount - discounted(r.invoice.amount, s.profile.factoring_apr, days)
    for p in sc.buyer.payables.values():
        if p.early_advance is not None:
            m.buyer_discount_income += p.invoice.amount - p.early_advance
            days = (p.invoice.due_date - p.early_pay_date).days
            m.buyer_yield_forgone += p.early_advance * sc.config.cash_yield * days / 365
    m.double_financing_blocked = sum(1 for d in sc.net.decisions if "DOUBLE FINANCING" in d.decision and d.node == sc.buyer.id)
    m.bad_signatures = sum(1 for e in sc.net.log if e.status.startswith("rejected"))
    # realised cash path vs floor
    start = sc.config.start
    path = sc.buyer.balance_path(start, sc.net.today - timedelta(days=1))
    m.min_balance = min(path.values()) if path else sc.config.opening_cash
    no_early = _baseline_path(sc)
    m.floor_breaches_caused = sum(1 for d, b in path.items() if b < m.floor and b < no_early.get(d, b))
    return m


def _baseline_path(sc: Scenario) -> dict[date, int]:
    """Balance path if every approved invoice were simply paid at maturity."""
    flows: dict[date, int] = dict(sc.buyer.policy.daily_inflows)
    for p in sc.buyer.payables.values():
        flows[p.invoice.due_date] = flows.get(p.invoice.due_date, 0) - p.invoice.amount
    bal, out, d = sc.config.opening_cash, {}, sc.config.start
    while d < sc.net.today:
        bal += flows.get(d, 0)
        out[d] = bal
        d += timedelta(days=1)
    return out
