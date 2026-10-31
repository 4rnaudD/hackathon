"""Smoke scenario: one buyer, two banks, a card issuer, two suppliers (one double-dipping)."""
from datetime import date, timedelta

from openfloat.messages import Invoice, OfferKind
from openfloat.network import Network
from openfloat.nodes import BuyerNode, BuyerPolicy, FunderNode, FunderPolicy, SupplierNode, SupplierProfile

D = date(2026, 11, 2)
net = Network(D)
BuyerNode("acme-buyer", net, BuyerPolicy(opening_cash=50_000_00, cash_floor=20_000_00, cash_yield=0.04,
                                         discount_apr=0.08, approval_limit=1_000_000_00), {"sup-a", "sup-b"})
FunderNode("bank-a", net, FunderPolicy(OfferKind.FUNDER, limit=100_000_00, apr=0.055))
FunderNode("bank-b", net, FunderPolicy(OfferKind.FUNDER, limit=100_000_00, apr=0.062))
FunderNode("card-co", net, FunderPolicy(OfferKind.CARD, limit=100_000_00, card_fee=0.02))
a = SupplierNode("sup-a", net, SupplierProfile(factoring_apr=0.30))
b = SupplierNode("sup-b", net, SupplierProfile(factoring_apr=0.30, double_dip=True))


def issue(n):
    a.issue(Invoice(invoice_id="INV-1", supplier_id="sup-a", buyer_id="acme-buyer", amount=20_000_00, issue_date=D, due_date=D + timedelta(60)), True)
    a.issue(Invoice(invoice_id="INV-2", supplier_id="sup-a", buyer_id="acme-buyer", amount=40_000_00, issue_date=D, due_date=D + timedelta(90)), True)
    b.issue(Invoice(invoice_id="INV-3", supplier_id="sup-b", buyer_id="acme-buyer", amount=30_000_00, issue_date=D, due_date=D + timedelta(45)), True)


net.run_day(issue)
for d in net.decisions:
    print(f"{d.node:11} {d.invoice_id:6} {d.decision}")
print("--- settlements")
for e in net.settlements():
    s = e.body
    print(f"{s.payer_id:11} -> {s.payee_id:11} {s.amount/100:>10,.2f} {s.value_date} {s.rail}")
