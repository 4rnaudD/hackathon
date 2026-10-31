from datetime import date, timedelta

import pytest
from hypothesis import given, settings
from pydantic import ValidationError
from hypothesis import strategies as st

from openfloat.crypto import Identity
from openfloat.messages import (
    Accept,
    AssignmentNotice,
    EarlyPayRequest,
    Envelope,
    Invoice,
    PaymentUndertaking,
)
from openfloat.network import Network
from openfloat.nodes import (
    BuyerNode,
    BuyerPolicy,
    FunderNode,
    FunderPolicy,
    InvStatus,
    SupplierNode,
    SupplierProfile,
)
from openfloat.optimiser import Candidate, _delta, choose_self_funded, date_range
from openfloat.sim import ScenarioConfig, build, evaluate

D = date(2026, 11, 2)


def small_net(cash=10_000_000_00, floor=0, double_dip=False, limit=1_000_000_00):
    net = Network(D)
    buyer = BuyerNode("buyer", net, BuyerPolicy(opening_cash=cash, cash_floor=floor, cash_yield=0.04,
                                                discount_apr=0.05, approval_limit=500_000_00), {"sup"})
    fa = FunderNode("bank-a", net, FunderPolicy(limit=limit, apr=0.055))
    fb = FunderNode("bank-b", net, FunderPolicy(limit=limit, apr=0.060))
    sup = SupplierNode("sup", net, SupplierProfile(factoring_apr=0.30, double_dip=double_dip))
    return net, buyer, fa, fb, sup


def invoice(iid="INV-1", amount=100_000_00, days=60, supplier="sup"):
    return Invoice(invoice_id=iid, supplier_id=supplier, buyer_id="buyer", amount=amount,
                   issue_date=D, due_date=D + timedelta(days=days))


# --- signatures ---------------------------------------------------------------


def test_tampered_message_is_rejected_by_network():
    net, buyer, *_ , sup = small_net()
    env = Envelope.seal(sup.identity, ["buyer"], D, invoice())
    env.body.amount = 999_999_00  # tamper in flight
    net.post(env)
    net.deliver()
    assert net.log[-1].status == "rejected: bad signature"
    assert "INV-1" not in buyer.payables


def test_forged_undertaking_is_declined_by_funder():
    net, buyer, fa, fb, sup = small_net()
    inv = invoice()
    # supplier forges an undertaking, signing it itself
    fake = Envelope.seal(sup.identity, ["sup"], D, PaymentUndertaking(
        invoice_id=inv.invoice_id, invoice_hash=inv.content_hash(), buyer_id="buyer", supplier_id="sup",
        amount=inv.amount, currency="SGD", due_date=inv.due_date))
    net.post(Envelope.seal(sup.identity, ["bank-a"], D, EarlyPayRequest(invoice_id="INV-1", requested_date=D, undertaking=fake)))
    net.deliver()
    assert not fa.offers
    assert any("invalid payment undertaking" in d.decision for d in net.decisions)


def test_unknown_signer_rejected():
    net, *_ = small_net()
    stranger = Identity("mallory")
    net.post(Envelope.seal(stranger, ["buyer"], D, invoice(supplier="mallory")))
    net.deliver()
    assert net.log[-1].status == "rejected: bad signature"


# --- double financing ---------------------------------------------------------


def test_double_dipping_supplier_is_funded_once():
    net, buyer, fa, fb, sup = small_net(cash=0, double_dip=True)  # no buyer cash -> funders compete
    net.run_day(lambda n: sup.issue(invoice(), True))
    p = buyer.payables["INV-1"]
    assert p.status == InvStatus.ASSIGNED
    funded = [s for s in net.settlements() if s.body.payee_id == "sup"]
    assert len(funded) == 1
    assert any("DOUBLE FINANCING" in d.decision for d in net.decisions)
    assert fb.exposure.get("buyer", 0) == 0  # loser released its limit


def test_replayed_assignment_notice_rejected():
    net, buyer, fa, fb, sup = small_net(cash=0)
    net.run_day(lambda n: sup.issue(invoice(), True))
    notice = next(e.envelope for e in net.log if e.msg_type == "ASSIGNMENT_NOTICE")
    net.post(notice)  # replay the same signed notice
    net.deliver()
    assert sum("assignment acknowledged" in d.decision for d in net.decisions) == 1
    assert any("REJECTED" in d.decision for d in net.decisions)


def test_funder_cannot_claim_with_someone_elses_acceptance():
    net, buyer, fa, fb, sup = small_net(cash=0)
    net.run_day(lambda n: sup.issue(invoice(), True))
    acceptance = next(e.envelope for e in net.log if e.msg_type == "ACCEPT")
    # bank-b reuses the supplier's acceptance of bank-a's offer to claim the invoice
    steal = Envelope.seal(fb.identity, ["buyer"], net.today, AssignmentNotice(
        invoice_id="INV-1", invoice_hash=buyer.payables["INV-1"].invoice_hash, new_payee_id="bank-b", acceptance=acceptance))
    net.post(steal)
    net.deliver()
    assert buyer.payables["INV-1"].payee == "bank-a"


def test_expired_offer_cannot_be_accepted():
    net, buyer, fa, fb, sup = small_net(cash=0)
    sup.issue(invoice(), True)
    net.deliver()
    sup.request_early_pay()
    net.deliver()
    offer = next(e for e in sup.receivables["INV-1"].offers if e.sender == "bank-a")
    net.today += timedelta(days=2)
    net.post(Envelope.seal(sup.identity, ["bank-a"], net.today, Accept(offer=offer, supplier_id="sup", representation="NOT_ASSIGNED_OR_PLEDGED")))
    net.deliver()
    assert buyer.payables["INV-1"].status == InvStatus.APPROVED


# --- supplier representation and acknowledgment log ---------------------------


@pytest.mark.filterwarnings("ignore::UserWarning")  # the test deliberately sets an invalid value
def test_accept_requires_signed_representation():
    net, buyer, fa, fb, sup = small_net(cash=0)
    sup.issue(invoice(), True)
    net.deliver()
    sup.request_early_pay()
    net.deliver()
    offer = sup.receivables["INV-1"].offers[0]
    with pytest.raises(ValidationError):
        Accept(offer=offer, supplier_id="sup")
    acc = Envelope.seal(sup.identity, ["bank-a"], D, Accept(offer=offer, supplier_id="sup", representation="NOT_ASSIGNED_OR_PLEDGED"))
    assert acc.verify(net.directory)
    acc.body.representation = "SOMETHING_ELSE"  # cannot be altered after signing
    assert not acc.verify(net.directory)


def test_equivocating_buyer_caught_by_ack_log():
    net, buyer, fa, fb, sup = small_net(cash=0, double_dip=True)
    buyer.equivocate = True  # buyer acknowledges both funders
    net.run_day(lambda n: sup.issue(invoice(), True))
    acks = [e for e in net.log if e.msg_type == "ASSIGNMENT_ACK"]
    assert len(acks) == 2  # the buyer really did acknowledge twice
    funded = [s for s in net.settlements() if s.body.payee_id == "sup"]
    assert len(funded) == 1 and funded[0].sender == "bank-a"
    assert fb.alarms == ["INV-1"] and fb.exposure.get("buyer", 0) == 0
    assert [e.funder_id for e in net.ack_log.for_invoice(buyer.payables["INV-1"].invoice_hash)] == ["bank-a", "bank-b"]
    assert net.ack_log.verify(net.directory)


def test_ack_log_tampering_detected():
    net, buyer, fa, fb, sup = small_net(cash=0)
    net.run_day(lambda n: [sup.issue(invoice(f"I{i}"), True) for i in range(3)])
    log = net.ack_log
    assert len(log.entries) >= 2 and log.verify(net.directory)
    original = list(log.entries)
    log.entries[0] = original[0].__class__(**{**original[0].__dict__, "funder_id": "bank-b"})  # rewrite history
    assert not log.verify(net.directory)
    log.entries = [original[1], original[0], *original[2:]]  # reorder
    assert not log.verify(net.directory)
    log.entries = original[1:]  # drop the first entry
    assert not log.verify(net.directory)


def test_funder_declines_invoice_already_in_log():
    net, buyer, fa, fb, sup = small_net(cash=0)
    net.run_day(lambda n: sup.issue(invoice(), True))
    r = sup.receivables["INV-1"]
    net.post(Envelope.seal(sup.identity, ["bank-b"], net.today, EarlyPayRequest(invoice_id="INV-1", requested_date=net.today, undertaking=r.undertaking)))
    net.deliver()
    assert any(d.node == "bank-b" and "already in the acknowledgment log" in d.decision for d in net.decisions)


# --- policy -------------------------------------------------------------------


def test_invoice_rules():
    net, buyer, *_ , sup = small_net()
    net.run_day(lambda n: (sup.issue(invoice("BIG", amount=900_000_00), False), sup.issue(invoice("OK"), False)))
    assert "BIG" not in buyer.payables and "OK" in buyer.payables
    assert sup.receivables["BIG"].outcome.startswith("rejected")


def test_funder_limit_respected():
    net, buyer, fa, fb, sup = small_net(cash=0, limit=150_000_00)
    net.run_day(lambda n: [sup.issue(invoice(f"I{i}"), True) for i in range(4)])
    for _ in range(3):  # unfunded invoices are re-requested daily as offers expire
        assert fa.exposure.get("buyer", 0) <= 150_000_00 and fb.exposure.get("buyer", 0) <= 150_000_00
        net.run_day()
    assert fa.exposure["buyer"] == 100_000_00 and fb.exposure["buyer"] == 100_000_00
    assert sum(1 for p in buyer.payables.values() if p.status == InvStatus.ASSIGNED) == 2


# --- whole-network invariants over random scenarios ---------------------------


@settings(max_examples=12, deadline=None)
@given(seed=st.integers(0, 10_000), dip=st.sampled_from([0.0, 0.3]))
def test_invariants_random_scenarios(seed, dip):
    sc = build(ScenarioConfig(seed=seed, days=8, n_suppliers=8, p_double_dip=dip,
                              opening_cash=600_000_00, cash_floor=500_000_00, daily_inflow=30_000_00))
    sc.run()
    sup_ids = {s.id for s in sc.suppliers}
    paid_to_supplier: dict[str, int] = {}
    paid_by_buyer: dict[str, list] = {}
    for env in sc.net.settlements():
        s = env.body
        if s.payee_id in sup_ids:
            paid_to_supplier[s.invoice_id] = paid_to_supplier.get(s.invoice_id, 0) + 1
        if s.payer_id == sc.buyer.id:
            paid_by_buyer.setdefault(s.invoice_id, []).append(s)
    for iid, p in sc.buyer.payables.items():
        assert paid_to_supplier.get(iid, 0) == 1, f"{iid} paid to supplier {paid_to_supplier.get(iid, 0)} times"
        assert len(paid_by_buyer[iid]) == 1, f"buyer paid {iid} {len(paid_by_buyer[iid])} times"
        assert p.status in (InvStatus.SETTLED, InvStatus.PAID_EARLY)
    for f in sc.funders:
        assert all(v == 0 for v in f.exposure.values())  # everything matured and released
    m = evaluate(sc)
    assert m.supplier_cost_openfloat <= m.supplier_cost_factoring


# --- optimiser ----------------------------------------------------------------


@settings(max_examples=40, deadline=None)
@given(
    amounts=st.lists(st.integers(10_000_00, 300_000_00), min_size=1, max_size=12),
    terms=st.lists(st.sampled_from([20, 30, 45, 60, 90]), min_size=12, max_size=12),
    headroom=st.integers(0, 800_000_00),
)
def test_optimiser_respects_floor_and_beats_greedy(amounts, terms, headroom):
    floor = 1_000_000_00
    days = date_range(D, D + timedelta(days=95))
    baseline = {d: floor + headroom for d in days}
    cands = [Candidate(f"c{i}", a, a - round(a * 0.05 * t / 365), D, D + timedelta(days=t))
             for i, (a, t) in enumerate(zip(amounts, terms))]
    results = {}
    for method in ("milp", "greedy"):
        dec = choose_self_funded(cands, baseline, floor, 0.04, method)
        chosen = [c for c, d in zip(cands, dec) if d.fund]
        for day in days:
            assert baseline[day] + sum(_delta(c, day) for c in chosen) >= floor
        results[method] = sum(c.net_value(0.04) for c in chosen)
    assert results["milp"] >= results["greedy"] - 1e-6


def test_optimiser_skips_offers_below_cash_yield():
    c = Candidate("x", 100_000_00, 100_000_00 - 100, D, D + timedelta(days=60))  # tiny discount
    [d] = choose_self_funded([c], {D: 10**12}, 0, 0.04)
    assert not d.fund and "below cash yield" in d.reason
