"""Reference OpenFloat nodes: buyer, supplier and funder.

Each node only sees signed envelopes addressed to it and keeps its own state.
Every decision is recorded with a human-readable reason for the audit trail.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from .crypto import Identity, sha256_hex
from .messages import (
    Accept,
    AssignmentAck,
    AssignmentNotice,
    AssignmentRejected,
    EarlyPayRequest,
    Envelope,
    NOT_ASSIGNED_OR_PLEDGED,
    Invoice,
    InvoiceRejected,
    Offer,
    OfferKind,
    PaymentOverdue,
    PaymentReceived,
    PaymentStatus,
    PaymentUndertaking,
    SettlementInstruction,
)
from .optimiser import Candidate, choose_self_funded, date_range

if TYPE_CHECKING:
    from .network import Network
    from .rails import BankPayment

OVERDUE_GRACE_DAYS = 1


def discounted(amount: int, apr: float, days: int) -> int:
    """Advance after a simple-interest discount at `apr` for `days`."""
    return amount - round(amount * apr * max(days, 0) / 365)


def apr_of(amount: int, advance: int, days: int) -> float:
    if days <= 0 or advance <= 0:
        return 0.0
    return (amount - advance) / advance * 365 / days


@dataclass
class Expectation:
    """A payment this node is owed, tracked until its own bank reports the credit."""

    invoice_id: str
    payer: str
    amount: int
    due: date
    kind: str  # "advance" or "maturity"
    status: str = "EXPECTED"  # EXPECTED -> PAID, or EXPECTED -> OVERDUE -> PAID_LATE
    received_on: date | None = None
    days_late: int = 0


class Node:
    role = "node"

    def __init__(self, node_id: str, net: "Network") -> None:
        self.id = node_id
        self.identity = Identity(node_id)
        self.net = net
        self.expected: dict[str, Expectation] = {}
        net.join(self)

    def send(self, recipients: list[str], body) -> Envelope:
        env = Envelope.seal(self.identity, recipients, self.net.today, body)
        self.net.post(env)
        if isinstance(body, SettlementInstruction):
            self.net.rails.submit(env)  # hand the instruction to our own bank (pain.001 / bank API)
        return env

    # --- money movement: expectations, bank reports, overdue ------------------

    def expect(self, invoice_id: str, payer: str, amount: int, due: date, kind: str) -> None:
        self.expected[invoice_id] = Expectation(invoice_id, payer, amount, due, kind)

    def on_bank_debit(self, p: "BankPayment") -> None:
        """Our bank executed a payment we instructed: tell the payee, citing the bank reference."""
        b = p.instruction.body
        self.send([b.payee_id], PaymentStatus(invoice_id=b.invoice_id, end_to_end_id=p.end_to_end_id, status="ACSC",
                                              amount=b.amount, currency=b.currency, value_date=p.executed_on,
                                              bank_reference=p.bank_reference))

    def on_bank_credit(self, p: "BankPayment") -> None:
        """Our bank credited us: match it to what we are owed, then confirm receipt to the payer."""
        b = p.instruction.body
        e = self.expected.get(b.invoice_id)
        if e is None or e.status in ("PAID", "PAID_LATE"):
            self.decide(b.invoice_id, f"unexpected credit of {b.amount/100:,.2f} from {b.payer_id}")
        elif e.payer != b.payer_id or e.amount != b.amount:
            self.decide(b.invoice_id, f"credit does not match expectation: got {b.amount/100:,.2f} from "
                                      f"{b.payer_id}, expected {e.amount/100:,.2f} from {e.payer}")
        else:
            e.received_on = p.executed_on
            if e.status == "OVERDUE":
                e.status, e.days_late = "PAID_LATE", (p.executed_on - e.due).days
                self.decide(b.invoice_id, f"received {b.amount/100:,.2f} from {b.payer_id}, {e.days_late} days late")
            else:
                e.status = "PAID"
                self.decide(b.invoice_id, f"received {b.amount/100:,.2f} from {b.payer_id} (bank ref {p.bank_reference})")
            self._credited(e)
        self.send([b.payer_id], PaymentReceived(invoice_id=b.invoice_id, end_to_end_id=p.end_to_end_id,
                                                amount=b.amount, currency=b.currency, value_date=p.executed_on,
                                                bank_reference=p.bank_reference))

    def _credited(self, e: Expectation) -> None:
        """Hook for subclasses once an expected payment has arrived."""

    def check_overdue(self) -> None:
        today = self.net.today
        for e in self.expected.values():
            if e.status == "EXPECTED" and (today - e.due).days > OVERDUE_GRACE_DAYS:
                e.status = "OVERDUE"
                days = (today - e.due).days
                self.decide(e.invoice_id, f"OVERDUE: {e.amount/100:,.2f} from {e.payer} was due {e.due} ({days} days)")
                self.send([e.payer], PaymentOverdue(invoice_id=e.invoice_id, amount=e.amount, currency="SGD",
                                                    due_date=e.due, days_overdue=days))

    def on_payment_message(self, env: Envelope) -> None:
        if isinstance(env.body, PaymentOverdue):
            self.decide(env.body.invoice_id, f"overdue notice from {env.sender}: {env.body.amount/100:,.2f} "
                                             f"due {env.body.due_date}")

    def decide(self, invoice_id: str, what: str) -> None:
        self.net.record_decision(self.id, invoice_id, what)

    def handle(self, env: Envelope) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


# --- buyer ------------------------------------------------------------------


class InvStatus(StrEnum):
    APPROVED = "APPROVED"
    PAID_EARLY = "PAID_EARLY"
    ASSIGNED = "ASSIGNED"
    SETTLED = "SETTLED"


@dataclass
class Payable:
    invoice: Invoice
    invoice_hash: str
    status: InvStatus = InvStatus.APPROVED
    payee: str = ""
    early_advance: int | None = None
    early_pay_date: date | None = None


@dataclass
class BuyerPolicy:
    opening_cash: int
    cash_floor: int
    cash_yield: float  # what idle cash earns, e.g. money-market fund yield
    discount_apr: float  # APR the buyer asks for when paying early
    approval_limit: int  # invoices above this need manual approval (rejected here)
    daily_inflows: dict[date, int] = field(default_factory=dict)
    optimiser: str = "milp"
    # expected outflow on `day` from invoices not yet received as of `as_of` (treasury forecast)
    payables_forecast: Callable[[date, date], int] | None = None


class BuyerNode(Node):
    """The obligor. Single source of truth for who gets paid, and when."""

    role = "buyer"

    def __init__(self, node_id: str, net: "Network", policy: BuyerPolicy, suppliers: set[str]) -> None:
        super().__init__(node_id, net)
        self.policy = policy
        self.suppliers = suppliers
        self.payables: dict[str, Payable] = {}
        self.pending_requests: list[EarlyPayRequest] = []
        self.my_offers: dict[str, Offer] = {}
        self.paid_out: dict[date, int] = {}  # actual outflows by day
        self.discount_income = 0
        self.equivocate = False  # adversarial: acknowledge every assignment (for demos/tests)
        self.late_share = 0.0  # share of maturities this buyer pays late (scenario setting)
        self.late_days = 0

    # cash forecast --------------------------------------------------------

    def balance_path(self, start: date, end: date, extra_outflows: dict[date, int] | None = None) -> dict[date, int]:
        """Projected end-of-day balance assuming every open payable is paid at maturity."""
        flows: dict[date, int] = {}
        for d, amt in self.policy.daily_inflows.items():
            flows[d] = flows.get(d, 0) + amt
        for d, amt in self.paid_out.items():
            flows[d] = flows.get(d, 0) - amt
        for p in self.payables.values():
            if p.status in (InvStatus.APPROVED, InvStatus.ASSIGNED):
                flows[p.invoice.due_date] = flows.get(p.invoice.due_date, 0) - p.invoice.amount
        for d, amt in (extra_outflows or {}).items():
            flows[d] = flows.get(d, 0) - amt
        bal = self.policy.opening_cash + sum(a for d, a in flows.items() if d < start)
        path = {}
        for d in date_range(start, end):
            bal += flows.get(d, 0)
            path[d] = bal
        return path

    def horizon_end(self) -> date:
        dues = [p.invoice.due_date for p in self.payables.values()]
        return max(dues + [self.net.today]) + timedelta(days=1)

    # message handling -----------------------------------------------------

    def handle(self, env: Envelope) -> None:
        match env.body:
            case Invoice() as inv:
                self._on_invoice(env, inv)
            case EarlyPayRequest() as req:
                self._on_request(env, req)
            case Accept() as acc:
                self._on_accept(env, acc)
            case AssignmentNotice() as note:
                self._on_assignment(env, note)
            case PaymentStatus() | PaymentReceived() | PaymentOverdue():
                self.on_payment_message(env)

    def _on_invoice(self, env: Envelope, inv: Invoice) -> None:
        if env.sender != inv.supplier_id or inv.buyer_id != self.id:
            return self._reject_invoice(env.sender, inv, "sender/buyer mismatch")
        if inv.supplier_id not in self.suppliers:
            return self._reject_invoice(env.sender, inv, "supplier not onboarded")
        if inv.invoice_id in self.payables:
            return self._reject_invoice(env.sender, inv, "duplicate invoice id")
        if inv.amount > self.policy.approval_limit:
            return self._reject_invoice(env.sender, inv, "above auto-approval limit; needs manual review")
        h = inv.content_hash()
        self.payables[inv.invoice_id] = Payable(inv, h, payee=inv.supplier_id)
        self.decide(inv.invoice_id, f"approved; undertaking to pay {inv.amount/100:,.2f} on {inv.due_date}")
        self.send(
            [inv.supplier_id],
            PaymentUndertaking(
                invoice_id=inv.invoice_id,
                invoice_hash=h,
                buyer_id=self.id,
                supplier_id=inv.supplier_id,
                amount=inv.amount,
                currency=inv.currency,
                due_date=inv.due_date,
            ),
        )

    def _reject_invoice(self, to: str, inv: Invoice, reason: str) -> None:
        self.decide(inv.invoice_id, f"rejected invoice: {reason}")
        self.send([to], InvoiceRejected(invoice_id=inv.invoice_id, reason=reason))

    def _on_request(self, env: Envelope, req: EarlyPayRequest) -> None:
        p = self.payables.get(req.invoice_id)
        if p and p.status == InvStatus.APPROVED and env.sender == p.invoice.supplier_id:
            self.pending_requests.append(req)

    def run_batch(self) -> None:
        """Decide which pending requests to fund from own cash, then offer on those."""
        today = self.net.today
        self.my_offers = {k: o for k, o in self.my_offers.items() if o.expires >= today}
        reqs, self.pending_requests = self.pending_requests, []
        if not reqs:
            return
        cands = []
        for r in reqs:
            p = self.payables[r.invoice_id]
            pay = max(r.requested_date, today)
            days = (p.invoice.due_date - pay).days
            cands.append(
                Candidate(r.invoice_id, p.invoice.amount, discounted(p.invoice.amount, self.policy.discount_apr, days), pay, p.invoice.due_date)
            )
        # cash already promised to outstanding (unaccepted) offers counts as spent
        reserved: dict[date, int] = {}
        for o in self.my_offers.values():
            reserved[o.pay_date] = reserved.get(o.pay_date, 0) + o.advance
            due = self.payables[o.invoice_id].invoice.due_date
            reserved[due] = reserved.get(due, 0) - o.advance
        end = self.horizon_end()
        if self.policy.payables_forecast:
            for d in date_range(today, end):
                reserved[d] = reserved.get(d, 0) + self.policy.payables_forecast(today, d)
        baseline = self.balance_path(today, end, reserved)
        for d, c in zip(choose_self_funded(cands, baseline, self.policy.cash_floor, self.policy.cash_yield, self.policy.optimiser), cands):
            if not d.fund:
                self.decide(d.invoice_id, f"no own-cash offer: {d.reason}")
                continue
            p = self.payables[c.invoice_id]
            offer = Offer(
                invoice_id=c.invoice_id,
                invoice_hash=p.invoice_hash,
                offeror_id=self.id,
                kind=OfferKind.BUYER_DISCOUNT,
                advance=c.advance,
                pay_date=c.pay_date,
                expires=today,
                annualised_rate=apr_of(c.amount, c.advance, c.days_early),
            )
            self.my_offers[offer.offer_id] = offer
            self.decide(c.invoice_id, f"offer early pay from own cash: net value {d.net_value/100:,.2f} above cash yield")
            self.send([p.invoice.supplier_id], offer)

    def _on_accept(self, env: Envelope, acc: Accept) -> None:
        offer_env = acc.offer
        offer = offer_env.body
        if offer_env.sender != self.id or offer.offer_id not in self.my_offers:
            return
        p = self.payables.get(offer.invoice_id)
        del self.my_offers[offer.offer_id]
        if p is None or env.sender != p.invoice.supplier_id:
            return
        if offer.expires < self.net.today:
            return self.decide(offer.invoice_id, "acceptance ignored: offer expired")
        if p.status != InvStatus.APPROVED:
            return self.decide(offer.invoice_id, f"acceptance ignored: invoice already {p.status}")
        p.status = InvStatus.PAID_EARLY
        p.early_advance, p.early_pay_date = offer.advance, offer.pay_date
        self.paid_out[offer.pay_date] = self.paid_out.get(offer.pay_date, 0) + offer.advance
        self.discount_income += p.invoice.amount - offer.advance
        self.decide(offer.invoice_id, f"paid early: {offer.advance/100:,.2f} (discount {(p.invoice.amount-offer.advance)/100:,.2f})")
        self._instruct(p, p.invoice.supplier_id, offer.advance, offer.pay_date, "BANK_TRANSFER")

    def _on_assignment(self, env: Envelope, note: AssignmentNotice) -> None:
        p = self.payables.get(note.invoice_id)

        def reject(reason: str) -> None:
            self.decide(note.invoice_id, f"assignment to {note.new_payee_id} REJECTED: {reason}")
            self.send([note.new_payee_id], AssignmentRejected(invoice_id=note.invoice_id, new_payee_id=note.new_payee_id, reason=reason))

        if p is None:
            return reject("unknown invoice")
        acc_env = note.acceptance
        acc = acc_env.body
        if not isinstance(acc, Accept) or acc_env.sender != p.invoice.supplier_id:
            return reject("acceptance not signed by the invoice's supplier")
        offer_env = acc.offer
        if offer_env.sender != note.new_payee_id or env.sender != note.new_payee_id:
            return reject("offer not issued by the notifying funder")
        if offer_env.body.invoice_hash != p.invoice_hash or note.invoice_hash != p.invoice_hash:
            return reject("invoice hash mismatch")
        if not (self.equivocate and p.status == InvStatus.ASSIGNED):
            if p.status == InvStatus.ASSIGNED:
                return reject(f"DOUBLE FINANCING: already assigned to {p.payee}")
            if p.status == InvStatus.PAID_EARLY:
                return reject("DOUBLE FINANCING: invoice already paid early by buyer")
            if p.status != InvStatus.APPROVED:
                return reject(f"invoice is {p.status}")
        if p.status == InvStatus.ASSIGNED:  # only reachable when equivocating: signs a second ack, ledger unchanged
            self.decide(note.invoice_id, f"DISHONEST: acknowledged {note.new_payee_id} although already assigned to {p.payee}")
        else:
            p.status, p.payee = InvStatus.ASSIGNED, note.new_payee_id
        self.decide(note.invoice_id, f"assignment acknowledged: will pay {note.new_payee_id} {p.invoice.amount/100:,.2f} on {p.invoice.due_date}")
        self.send(
            [note.new_payee_id, p.invoice.supplier_id],
            AssignmentAck(invoice_id=note.invoice_id, invoice_hash=p.invoice_hash, new_payee_id=note.new_payee_id, amount=p.invoice.amount, due_date=p.invoice.due_date),
        )

    def pays_late(self, invoice_id: str) -> int:
        """Days this buyer's payment run is late for an invoice (deterministic per invoice id)."""
        bucket = int(sha256_hex(invoice_id)[:4], 16) % 100
        return self.late_days if bucket < self.late_share * 100 else 0

    def settle_maturities(self) -> None:
        for p in self.payables.values():
            pay_day = p.invoice.due_date + timedelta(days=self.pays_late(p.invoice.invoice_id))
            if pay_day == self.net.today and p.status in (InvStatus.APPROVED, InvStatus.ASSIGNED):
                self.paid_out[self.net.today] = self.paid_out.get(self.net.today, 0) + p.invoice.amount
                p.status = InvStatus.SETTLED
                self.decide(p.invoice.invoice_id, f"maturity: paid {p.payee} {p.invoice.amount/100:,.2f}")
                self._instruct(p, p.payee, p.invoice.amount, self.net.today, "BANK_TRANSFER")

    def _instruct(self, p: Payable, payee: str, amount: int, value_date: date, rail: str) -> None:
        self.send(
            [payee],
            SettlementInstruction(
                invoice_id=p.invoice.invoice_id, payer_id=self.id, payee_id=payee, amount=amount,
                currency=p.invoice.currency, value_date=value_date, rail=rail, reference=f"OF-{p.invoice.invoice_id}",
            ),
        )


# --- funder -------------------------------------------------------------------


@dataclass
class FunderPolicy:
    limit: int  # max face value outstanding on any one buyer
    apr: float  # priced off the buyer's credit
    min_days: int = 5


class FunderNode(Node):
    role = "funder"

    def __init__(self, node_id: str, net: "Network", policy: FunderPolicy) -> None:
        super().__init__(node_id, net)
        self.policy = policy
        self.exposure: dict[str, int] = {}  # buyer -> face value outstanding (incl. pending)
        self.offers: dict[str, tuple[Offer, PaymentUndertaking]] = {}  # firm offers: reserve limit until expiry
        self.pending: dict[str, tuple[Offer, PaymentUndertaking]] = {}  # invoice -> awaiting buyer ack
        self.book: list[tuple[Offer, PaymentUndertaking]] = []  # funded deals
        self.alarms: list[str] = []  # invoices where the log showed buyer equivocation

    def handle(self, env: Envelope) -> None:
        match env.body:
            case EarlyPayRequest() as req:
                self._on_request(env, req)
            case Accept() as acc:
                self._on_accept(env, acc)
            case AssignmentAck() as ack:
                self._on_ack(env, ack)
            case AssignmentRejected() as rej:
                self._on_rejected(env, rej)
            case SettlementInstruction():
                pass
            case PaymentStatus() | PaymentReceived() | PaymentOverdue():
                self.on_payment_message(env)

    def _on_request(self, env: Envelope, req: EarlyPayRequest) -> None:
        u_env = req.undertaking
        u = u_env.body
        # the undertaking must be signed by the buyer it names, for the requesting supplier
        if not isinstance(u, PaymentUndertaking) or u_env.sender != u.buyer_id or self.net.directory.role(u.buyer_id) != "buyer" or u.supplier_id != env.sender:
            return self.decide(req.invoice_id, "declined: invalid payment undertaking")
        if self.net.ack_log.for_invoice(u.invoice_hash):
            return self.decide(req.invoice_id, "declined: invoice already in the acknowledgment log")
        pay = max(req.requested_date, self.net.today)
        days = (u.due_date - pay).days
        if days < self.policy.min_days:
            return self.decide(req.invoice_id, f"declined: tenor {days}d below minimum")
        if self._committed(u.buyer_id) + u.amount > self.policy.limit:
            return self.decide(req.invoice_id, f"declined: limit on {u.buyer_id} reached")
        advance = discounted(u.amount, self.policy.apr, days)
        offer = Offer(
            invoice_id=req.invoice_id, invoice_hash=u.invoice_hash, offeror_id=self.id, kind=OfferKind.FUNDER,
            advance=advance, pay_date=pay, expires=self.net.today, annualised_rate=apr_of(u.amount, advance, days),
        )
        self.offers[offer.offer_id] = (offer, u)
        self.decide(req.invoice_id, f"offered {advance/100:,.2f} on {pay} ({offer.annualised_rate:.2%} APR)")
        self.send([env.sender], offer)

    def _committed(self, buyer_id: str) -> int:
        """Exposure plus limit reserved by live offers; expired offers are dropped."""
        self.offers = {k: (o, u) for k, (o, u) in self.offers.items() if o.expires >= self.net.today}
        reserved = sum(u.amount for o, u in self.offers.values() if u.buyer_id == buyer_id)
        return self.exposure.get(buyer_id, 0) + reserved

    def _on_accept(self, env: Envelope, acc: Accept) -> None:
        offer = acc.offer.body
        if acc.offer.sender != self.id or offer.offer_id not in self.offers:
            return
        o, u = self.offers.pop(offer.offer_id)
        if env.sender != u.supplier_id or o.expires < self.net.today:
            return self.decide(o.invoice_id, "acceptance ignored: wrong sender or expired")
        if acc.representation != NOT_ASSIGNED_OR_PLEDGED:
            return self.decide(o.invoice_id, "acceptance declined: missing supplier representation")
        if self.net.ack_log.for_invoice(u.invoice_hash):
            return self.decide(o.invoice_id, "acceptance declined: invoice already in the acknowledgment log")
        if self._committed(u.buyer_id) + u.amount > self.policy.limit:  # defensive: offer was reserved
            return self.decide(o.invoice_id, "acceptance declined: limit reached since offer")
        self.exposure[u.buyer_id] = self.exposure.get(u.buyer_id, 0) + u.amount
        self.pending[o.invoice_id] = (o, u)
        self.decide(o.invoice_id, "accepted by supplier; notifying buyer of assignment")
        self.send([u.buyer_id], AssignmentNotice(invoice_id=o.invoice_id, invoice_hash=u.invoice_hash, new_payee_id=self.id, acceptance=env))

    def _on_ack(self, env: Envelope, ack: AssignmentAck) -> None:
        if ack.invoice_id not in self.pending or ack.new_payee_id != self.id:
            return
        o, u = self.pending.pop(ack.invoice_id)
        if env.sender != u.buyer_id or ack.invoice_hash != u.invoice_hash or ack.amount != u.amount:
            self.exposure[u.buyer_id] -= u.amount
            return self.decide(o.invoice_id, "ack does not match undertaking; not funding")
        entry = self.net.ack_log.publish(self.identity, env, self.net.today)
        earlier = self.net.ack_log.conflicts(entry)
        if earlier:
            self.exposure[u.buyer_id] -= u.amount
            self.alarms.append(o.invoice_id)
            return self.decide(o.invoice_id, f"EQUIVOCATION: buyer already acknowledged {earlier[0].funder_id} "
                                             f"(log entry #{earlier[0].seq}); not funding")
        self.decide(o.invoice_id, f"published ack to log (entry #{entry.seq}); no earlier acknowledgment")
        self.book.append((o, u))
        self.expect(o.invoice_id, u.buyer_id, u.amount, u.due_date, "maturity")
        self.decide(o.invoice_id, f"buyer acknowledged; funding {o.advance/100:,.2f}")
        self.send(
            [u.supplier_id],
            SettlementInstruction(invoice_id=o.invoice_id, payer_id=self.id, payee_id=u.supplier_id, amount=o.advance,
                                  currency=u.currency, value_date=o.pay_date, rail="BANK_TRANSFER", reference=f"OF-{o.invoice_id}"),
        )

    def _on_rejected(self, env: Envelope, rej: AssignmentRejected) -> None:
        if rej.invoice_id in self.pending:
            o, u = self.pending.pop(rej.invoice_id)
            if env.sender == u.buyer_id:
                self.exposure[u.buyer_id] -= u.amount
                self.decide(o.invoice_id, f"NOT funding: buyer rejected assignment ({rej.reason})")
            else:
                self.pending[rej.invoice_id] = (o, u)

    def release_matured(self) -> None:
        """Exposure falls away once the buyer pays at maturity."""
        for o, u in self.book:
            if u.due_date == self.net.today:
                self.exposure[u.buyer_id] -= u.amount


# --- supplier ---------------------------------------------------------------


@dataclass
class SupplierProfile:
    factoring_apr: float  # what this supplier's own factor charges (its fallback)
    double_dip: bool = False  # adversarial: accept every funder offer (for demos/tests)


@dataclass
class Receivable:
    invoice: Invoice
    undertaking: Envelope | None = None
    want_early: bool = False
    offers: list[Envelope] = field(default_factory=list)
    outcome: str = "open"
    received: int = 0
    received_on: date | None = None


class SupplierNode(Node):
    role = "supplier"

    def __init__(self, node_id: str, net: "Network", profile: SupplierProfile) -> None:
        super().__init__(node_id, net)
        self.profile = profile
        self.receivables: dict[str, Receivable] = {}

    def issue(self, invoice: Invoice, want_early: bool) -> None:
        self.receivables[invoice.invoice_id] = Receivable(invoice, want_early=want_early)
        self.send([invoice.buyer_id], invoice)

    def handle(self, env: Envelope) -> None:
        match env.body:
            case PaymentUndertaking() as u:
                r = self.receivables.get(u.invoice_id)
                if r and env.sender == r.invoice.buyer_id and u.invoice_hash == r.invoice.content_hash():
                    r.undertaking = env
                    r.outcome = "approved"
                    self.expect(u.invoice_id, u.buyer_id, u.amount, u.due_date, "maturity")
            case InvoiceRejected() as rej:
                if rej.invoice_id in self.receivables:
                    self.receivables[rej.invoice_id].outcome = f"rejected: {rej.reason}"
            case Offer() as o:
                r = self.receivables.get(o.invoice_id)
                if r and o.invoice_hash == r.invoice.content_hash():
                    r.offers.append(env)
            case AssignmentAck() as ack:
                r = self.receivables.get(ack.invoice_id)
                if r:
                    r.outcome = f"financed by {ack.new_payee_id}"
                    e = self.expected.get(ack.invoice_id)
                    if e and e.kind == "maturity" and e.status == "EXPECTED":
                        del self.expected[ack.invoice_id]  # the funder, not we, is now owed at maturity
            case SettlementInstruction() as s:
                r = self.receivables.get(s.invoice_id)
                e = self.expected.get(s.invoice_id)
                early = r is not None and s.value_date < r.invoice.due_date
                if r and s.payee_id == self.id and r.received == 0 and early and not (e and e.kind == "advance"):
                    # a payer committed to pay us early: expect that amount on that date instead.
                    # A maturity payment never replaces the original expectation, so lateness is kept.
                    self.expect(s.invoice_id, s.payer_id, s.amount, s.value_date, "advance")
            case PaymentStatus() | PaymentReceived() | PaymentOverdue():
                self.on_payment_message(env)

    def _credited(self, e: Expectation) -> None:
        r = self.receivables.get(e.invoice_id)
        if r and r.received == 0:
            r.received, r.received_on = e.amount, e.received_on

    def request_early_pay(self) -> None:
        funders = self.net.directory.nodes_with_role("funder")
        for r in self.receivables.values():
            retry = r.outcome.startswith("no offers")  # limits may have freed up since yesterday
            if r.want_early and r.undertaking and (r.outcome == "approved" or retry):
                r.offers = []
                req = EarlyPayRequest(invoice_id=r.invoice.invoice_id, requested_date=self.net.today, undertaking=r.undertaking)
                self.send([r.invoice.buyer_id, *funders], req)
                r.outcome = "requested"

    def choose_offers(self) -> None:
        for r in self.receivables.values():
            if r.outcome != "requested":
                continue
            valid = [e for e in r.offers if e.body.expires >= self.net.today]
            if not valid:
                r.outcome = "no offers; waiting for maturity"
                self.decide(r.invoice.invoice_id, r.outcome)
                continue
            best = max(valid, key=lambda e: (e.body.advance, -e.body.pay_date.toordinal()))
            fallback = discounted(r.invoice.amount, self.profile.factoring_apr, (r.invoice.due_date - self.net.today).days)
            if best.body.advance < fallback:
                r.outcome = "own factoring cheaper; declined offers"
                self.decide(r.invoice.invoice_id, r.outcome)
                continue
            self.decide(
                r.invoice.invoice_id,
                f"accepted {best.body.kind} from {best.sender}: {best.body.advance/100:,.2f} vs {fallback/100:,.2f} via own factor",
            )
            targets = [best]
            if self.profile.double_dip:
                targets += [e for e in valid if e is not best and e.body.kind != OfferKind.BUYER_DISCOUNT]
            for e in targets:
                self.send([e.sender], Accept(offer=e, supplier_id=self.id, representation=NOT_ASSIGNED_OR_PLEDGED))
            r.outcome = "accepted"
