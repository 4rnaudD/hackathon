"""OpenFloat dashboard.

Run with:  uv run openfloat dashboard   (or: uv run streamlit run src/openfloat/dashboard.py)
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from openfloat.messages import Envelope, Invoice, SettlementInstruction
from openfloat.network import Network
from openfloat.nodes import (
    BuyerNode,
    BuyerPolicy,
    FunderNode,
    FunderPolicy,
    SupplierNode,
    SupplierProfile,
)
from openfloat.sim import ScenarioConfig, _baseline_path, build, evaluate

from openfloat.iso20022 import pain001
from openfloat.dashboard_charts import cost_scatter, exposure_chart, weekly_sources
from openfloat.dashboard_style import (
    AXIS, BLUE, CRITICAL, CSS, INK, INK_2, MUTED, ORANGE, SURFACE, chart_theme, money,
)

SERIES_ACTUAL, SERIES_BASE = "With early payments", "All paid on due date"
ROUTE_LABEL = {"BUYER_DISCOUNT": "Buyer's own cash", "FUNDER": "Bank"}

st.set_page_config(page_title="OpenFloat", page_icon="◐", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)


def plain(text: str) -> str:
    for code, label in {"BUYER_DISCOUNT": "buyer's own cash offer", "FUNDER": "bank offer"}.items():
        text = text.replace(code, label)
    return text


# --- message rendering ---------------------------------------------------------


def describe(env: Envelope) -> str:
    b = env.body
    match b.type:
        case "INVOICE":
            return f"Invoice {money(b.amount, 2)} {b.currency}, due {b.due_date}"
        case "PAYMENT_UNDERTAKING":
            return f"Signed promise to pay {money(b.amount, 2)} on {b.due_date}"
        case "INVOICE_REJECTED":
            return f"Invoice rejected: {b.reason}"
        case "EARLY_PAY_REQUEST":
            return f"Requests payment on {b.requested_date} (buyer's promise attached)"
        case "OFFER":
            return f"{ROUTE_LABEL[b.kind]} offer: {money(b.advance, 2)} on {b.pay_date} ({b.annualised_rate:.2%} APR)"
        case "ACCEPT":
            return f"Accepts offer from {b.offer.sender} ({money(b.offer.body.advance, 2)})"
        case "ASSIGNMENT_NOTICE":
            return "Claims the invoice: supplier's signed acceptance attached"
        case "ASSIGNMENT_ACK":
            return f"Will pay {b.new_payee_id} {money(b.amount, 2)} on {b.due_date}"
        case "ASSIGNMENT_REJECTED":
            return f"Claim rejected: {b.reason}"
        case "PAYMENT_STATUS":
            return f"My bank executed {money(b.amount, 2)} on {b.value_date} (status {b.status}, ref {b.bank_reference})"
        case "PAYMENT_RECEIVED":
            return f"My bank credited {money(b.amount, 2)} on {b.value_date} (ref {b.bank_reference})"
        case "PAYMENT_OVERDUE":
            return f"Overdue: {money(b.amount, 2)} was due {b.due_date} ({b.days_overdue} days)"
        case "SETTLEMENT_INSTRUCTION":
            return f"Pay {money(b.amount, 2)} to {b.payee_id} on {b.value_date} via {b.rail.replace('_', ' ').lower()}"
    return b.type


def message_table(entries) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Day": e.day,
                "Message": e.msg_type.replace("_", " ").title(),
                "From": e.sender,
                "To": ", ".join(e.recipients),
                "Invoice": e.invoice_id,
                "Signature": "✅" if e.status == "delivered" else "❌ " + e.status.removeprefix("rejected: "),
                "Content": describe(e.envelope),
            }
            for e in entries
        ]
    )


def show_messages(entries, key: str, compact: bool = False) -> None:
    if not entries:
        st.caption("No messages.")
        return
    df = message_table(entries)
    order = ["Message", "From", "To", "Content", "Signature"] if compact else \
        ["Day", "Message", "From", "To", "Invoice", "Content", "Signature"]
    event = st.dataframe(
        df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row", key=key,
        column_order=order,
        column_config={"Content": st.column_config.TextColumn(width="large"),
                       "Signature": st.column_config.TextColumn(width="small")},
    )
    rows = event.selection.rows if event and event.selection else []
    if rows:
        env = entries[rows[0]].envelope
        st.caption("Signed envelope (select another row to inspect it)")
        st.code(json.dumps(env.model_dump(mode="json"), indent=2), language="json")
    else:
        st.caption("Select a row to see the signed JSON envelope.")


# --- walkthrough ---------------------------------------------------------------

WALK_START = date(2026, 11, 2)

STEPS = [
    ("Two suppliers invoice the buyer",
     "Supplier A and Supplier B send invoices. The buyer checks each one and replies with a **signed promise to pay** on the due date."),
    ("Suppliers ask to be paid today",
     "Each supplier broadcasts an early-payment request with the buyer's signed promise attached. Both banks check the signature and reply with **firm offers priced on the buyer's credit**."),
    ("The buyer decides where its own cash goes",
     "The buyer's optimiser offers early payment from its own cash only where the discount beats its cash yield **and** its cash stays above the floor on every future day."),
    ("Suppliers accept, and one tries to double-finance",
     "Each supplier takes the best offer. Supplier B also accepts Bank B's offer, trying to finance the same invoice twice, "
     "and signs a statement that the invoice is not assigned or pledged elsewhere each time. Watch how the second claim is stopped. "
     "At the end of the day the banks execute the advances, and each payer and payee confirms the payment from its own bank's report."),
    ("Someone tampers with a message",
     "An attacker changes the amount on a payment instruction in transit. Every node verifies signatures, so the message is rejected before anyone acts on it."),
    ("Maturity",
     "On each due date the buyer pays whoever it acknowledged: the bank for Supplier B's invoice, Supplier A itself was paid early. "
     "The buyer's bank executes the payment; the payee confirms it only once its own bank reports the credit. "
     "If nothing arrives within a day of the due date, the payee sends a signed overdue notice."),
]


WALK_LATE_DAYS = 5


def new_walkthrough(dishonest: bool = False, late: bool = False) -> dict:
    net = Network(WALK_START)
    buyer = BuyerNode(
        "acme-buyer", net,
        BuyerPolicy(opening_cash=125_000_00, cash_floor=60_000_00, cash_yield=0.04, discount_apr=0.05,
                    approval_limit=500_000_00),
        {"supplier-a", "supplier-b"},
    )
    FunderNode("bank-a", net, FunderPolicy(limit=500_000_00, apr=0.055))
    FunderNode("bank-b", net, FunderPolicy(limit=500_000_00, apr=0.062))
    a = SupplierNode("supplier-a", net, SupplierProfile(factoring_apr=0.30))
    b = SupplierNode("supplier-b", net, SupplierProfile(factoring_apr=0.36, double_dip=True))
    buyer.equivocate = dishonest
    buyer.late_share, buyer.late_days = (1.0, WALK_LATE_DAYS) if late else (0.0, 0)
    w = {"net": net, "buyer": buyer, "a": a, "b": b, "step": 0, "marks": [0], "dec_marks": [0], "log_marks": [0],
         "dishonest": dishonest, "late": late}
    w["snaps"] = [snapshot(w)]
    return w


def advance(w: dict) -> None:
    net, buyer, a, b = w["net"], w["buyer"], w["a"], w["b"]
    s = w["step"]
    if s == 0:
        a.issue(Invoice(invoice_id="INV-A1", supplier_id="supplier-a", buyer_id=buyer.id, amount=30_000_00,
                        issue_date=WALK_START, due_date=WALK_START + timedelta(days=45),
                        description="Packaging materials", po_number="PO-48213"), want_early=True)
        b.issue(Invoice(invoice_id="INV-B1", supplier_id="supplier-b", buyer_id=buyer.id, amount=40_000_00,
                        issue_date=WALK_START, due_date=WALK_START + timedelta(days=60),
                        description="Electronic components", po_number="PO-48977"), want_early=True)
        net.deliver()
    elif s == 1:
        a.request_early_pay()
        b.request_early_pay()
        net.deliver()
    elif s == 2:
        buyer.run_batch()
        net.deliver()
    elif s == 3:
        a.choose_offers()
        b.choose_offers()
        net.deliver()
        net.end_of_day()  # banks execute the advances; both sides confirm
    elif s == 4:
        legit = next(e.envelope for e in reversed(net.log) if e.msg_type == "SETTLEMENT_INSTRUCTION")
        forged = legit.model_copy(deep=True)
        forged.body = SettlementInstruction(**{**legit.body.model_dump(), "amount": legit.body.amount * 10,
                                               "payee_id": "mallory"})
        forged.msg_id = forged.msg_id[:-4] + "beef"
        net.post(forged)
        net.deliver()
    elif s == 5:
        last_due = max(p.invoice.due_date for p in buyer.payables.values())
        while net.today <= last_due + timedelta(days=buyer.late_days + 2):
            buyer.settle_maturities()
            net.deliver()
            net.end_of_day()
            net.today += timedelta(days=1)
    w["step"] += 1
    w["marks"].append(len(net.log))
    w["dec_marks"].append(len(net.decisions))
    w["log_marks"].append(len(net.ack_log.entries))
    w["snaps"].append(snapshot(w))


SHORT_STEPS = ["Invoices", "Early-pay requests", "Buyer's cash", "Accept & assign", "Tampering", "Maturity"]


def snapshot(w: dict) -> dict:
    """Participant balances after the current step, for the state cards."""
    net, buyer = w["net"], w["buyer"]
    paid = sum(v for d, v in buyer.paid_out.items() if d <= net.today)
    snap = {"buyer_cash": buyer.policy.opening_cash - paid, "floor": buyer.policy.cash_floor}
    for f in net.funders:
        snap[f.id] = (f.exposure.get(buyer.id, 0), f.policy.limit, len(f.book), len(f.alarms))
    for s in (w["a"], w["b"]):
        snap[s.id] = (sum(r.received for r in s.receivables.values()),
                      next((r.received_on for r in s.receivables.values() if r.received_on), None),
                      sum(r.invoice.amount for r in s.receivables.values()))
    return snap


def stepper(step: int) -> None:
    cells = []
    for i, name in enumerate(SHORT_STEPS, start=1):
        cls = "done" if i < step else "current" if i == step else ""
        cells.append(f'<div class="of-step {cls}"><b>Step {i}</b>{name}</div>')
    st.markdown(f'<div class="of-steps">{"".join(cells)}</div>', unsafe_allow_html=True)


def card(role: str, name: str, value: str, sub: str, ratio: float | None = None, delta: str = "") -> str:
    bar = f'<div class="of-bar"><span style="width:{max(0, min(ratio, 1)) * 100:.0f}%"></span></div>' if ratio is not None else ""
    d = f'<div class="of-delta">{delta}</div>' if delta else ""
    return (f'<div class="of-card"><div class="of-role">{role}</div><div class="of-name">{name}</div>'
            f'<div class="of-val">{value}</div><div class="of-sub">{sub}</div>{bar}{d}</div>')


def state_cards(w: dict) -> None:
    now, before = w["snaps"][-1], w["snaps"][-2] if len(w["snaps"]) > 1 else w["snaps"][-1]
    net = w["net"]
    cols = st.columns(5)

    def chg(a: int, b: int) -> str:
        return "" if a == b else f"{'+' if a > b else '−'}{money(abs(a - b), 2)} this step"

    cash = now["buyer_cash"]
    cols[0].markdown(card("Buyer", w["buyer"].id, money(cash, 2), f"floor {money(now['floor'])}",
                          cash / w["buyer"].policy.opening_cash, chg(cash, before["buyer_cash"])), unsafe_allow_html=True)
    for col, f in zip(cols[1:3], net.funders):
        exp, limit, n, alarms = now[f.id]
        sub = f"{n} funded · limit {money(limit)}" + (" · ⚠ equivocation caught" if alarms else "")
        col.markdown(card("Bank", f.id, money(exp, 2), sub, exp / limit if limit else 0,
                          chg(exp, before[f.id][0]).replace("this step", "exposure")), unsafe_allow_html=True)
    for col, s in zip(cols[3:5], (w["a"], w["b"])):
        got, on, face = now[s.id]
        sub = f"received on {on}" if on else "waiting for payment"
        col.markdown(card("Supplier", s.id, money(got, 2), f"{sub} · invoiced {money(face)}",
                          got / face if face else 0, chg(got, before[s.id][0])), unsafe_allow_html=True)


def step_diagram(w: dict, entries) -> str:
    """Graphviz DOT: participants, with this step's messages as edges (rejected ones in red)."""
    net = w["net"]
    edges: dict[tuple[str, str, str, bool], int] = {}
    for e in entries:
        bad = e.status != "delivered" or e.msg_type in ("ASSIGNMENT_REJECTED", "PAYMENT_OVERDUE")
        for r in e.recipients:
            if r in net.nodes:
                key = (e.sender, r, e.msg_type.replace("_", " ").title(), bad)
                edges[key] = edges.get(key, 0) + 1
    log_new = w.get("log_marks", [0, 0])
    published = net.ack_log.entries[log_new[-2]:log_new[-1]]
    # buyer on top, so supplier-bank traffic runs underneath it rather than through it
    pos = {"supplier-a": (0, 1.5), "supplier-b": (0, 0), "acme-buyer": (2.9, 2.75),
           "bank-a": (5.8, 1.5), "bank-b": (5.8, 0), "ack-log": (5.8, 2.9)}
    lines = [
        "digraph G {",
        'graph [layout=neato, splines=curved, overlap=true, bgcolor="transparent", pad=0.3];',
        f'node [shape=box, style="rounded,filled", fillcolor="{SURFACE}", color="{AXIS}", fontname="Helvetica", '
        f'fontsize=17, fontcolor="{INK}", margin="0.2,0.08", width=1.3];',
        f'edge [fontname="Helvetica", fontsize=13, color="{BLUE}", fontcolor="{INK_2}", arrowsize=0.8, penwidth=1.6];',
    ]
    labels = {"acme-buyer": "acme-buyer\\nbuyer node", "supplier-a": "supplier-a", "supplier-b": "supplier-b",
              "bank-a": "bank-a", "bank-b": "bank-b"}
    for node, label in labels.items():
        x, y = pos[node]
        extra = f', penwidth=1.8, color="{INK_2}"' if node == "acme-buyer" else ""
        lines.append(f'"{node}" [label="{label}", pos="{x},{y}!"{extra}];')
    if net.ack_log.entries:
        x, y = pos["ack-log"]
        lines.append(f'"ack-log" [label="Acknowledgment log", shape=cylinder, pos="{x},{y}!", color="{MUTED}", '
                     f'fontcolor="{INK_2}", fontsize=14];')
    for (src, dst, label, bad), n in edges.items():
        lab = label + (f" ×{n}" if n > 1 else "")
        style = f'color="{CRITICAL}", fontcolor="{CRITICAL}", style=dashed, label="✕ {lab}"' if bad else f'label="{lab}"'
        lines.append(f'"{src}" -> "{dst}" [{style}];')
    for e in published:
        lines.append(f'"{e.funder_id}" -> "ack-log" [color="{MUTED}", fontcolor="{INK_2}", label="publish #{e.seq}"];')
    lines.append("}")
    return "\n".join(lines)


def walkthrough_tab() -> None:
    if "walk" not in st.session_state:
        st.session_state.walk = new_walkthrough()
    w = st.session_state.walk
    net = w["net"]

    left, right = st.columns([3, 1])
    with left:
        st.subheader("How a payment gets financed, step by step")
        st.caption("One buyer, two banks and two suppliers, each running its own node. "
                   "Every message below is a real signed envelope produced by the reference implementation.")
    with right:
        c1, c2 = st.columns(2)
        if c1.button("Next step ▶", type="primary", disabled=w["step"] >= len(STEPS), width="stretch"):
            advance(w)
            st.rerun()
        if c2.button("Restart", width="stretch"):
            st.session_state.walk = new_walkthrough(w["dishonest"], w["late"])
            st.rerun()
    t1, t2 = st.columns(2)
    dishonest = t1.toggle("Dishonest buyer: acknowledges every claim", value=w["dishonest"], disabled=w["step"] > 0,
                          help="Shows the acknowledgment log catching a buyer that acknowledges an invoice twice. "
                               "Set before starting.")
    late = t2.toggle(f"Buyer pays its maturities {WALK_LATE_DAYS} days late", value=w["late"], disabled=w["step"] > 0,
                     help="Shows the overdue path: the payee flags the missing payment, then records it as paid late.")
    if (dishonest, late) != (w["dishonest"], w["late"]):
        st.session_state.walk = new_walkthrough(dishonest, late)
        st.rerun()

    stepper(w["step"])

    if w["step"] == 0:
        st.info("Press **Next step** to start. " + STEPS[0][1].split(".")[0] + ".")
        parts = pd.DataFrame([
            {"Node": n.id, "Role": n.role, "Public key (Ed25519)": n.identity.public_key[:24] + "…"}
            for n in net.nodes.values()
        ])
        st.dataframe(parts, hide_index=True, width="stretch")
        return

    title, text = STEPS[w["step"] - 1]
    new = net.log[w["marks"][-2]:w["marks"][-1]]
    left, right = st.columns([5, 7], gap="large")
    with left:
        st.markdown(f"#### {w['step']}. {title}")
        st.markdown(text)
        st.caption("Blue arrows: messages delivered and acted on. Red dashed: rejected or overdue.")
    with right:
        st.graphviz_chart(step_diagram(w, new), width="stretch")
    state_cards(w)
    st.markdown("")
    if w["step"] == 5:
        bad = [e for e in new if e.status.startswith("rejected")]
        if bad:
            st.error(f"🛡️ **Rejected in transit:** \"{describe(bad[0].envelope)}\". "
                     "The signature no longer matches the content, so no node acted on it.")
    if w["step"] == 4:
        dbl = [d for d in net.decisions if "DOUBLE FINANCING" in d.decision and d.node == w["buyer"].id]
        for d in dbl:
            funder = d.decision.split("assignment to ")[1].split(" ")[0]
            winner = d.decision.rsplit(" ", 1)[-1]
            st.error(f"🛡️ **Blocked by the buyer's node:** {funder}'s claim on {d.invoice_id}. The buyer already "
                     f"acknowledged {winner} for this invoice, so {funder} never pays out.")
        for f in net.funders:
            for inv in f.alarms:
                st.error(f"🛡️ **Caught by the acknowledgment log:** the dishonest buyer acknowledged {inv} to two banks. "
                         f"{f.id} published its entry, found an earlier entry from another bank, and did not fund. "
                         "The first bank is protected by its own signed acknowledgment.")
        reps = [e for e in new if e.msg_type == "ACCEPT" and e.sender == w["b"].id]
        if len(reps) > 1:
            st.warning(f"📝 Supplier B signed **\"not assigned or pledged\"** in {len(reps)} acceptances for the same "
                       "invoice. Each one is signed evidence of misrepresentation the receiving bank can rely on.")

    st.markdown("**Messages in this step**")
    show_messages(new, key=f"walk-msgs-{w['step']}", compact=True)
    decisions = net.decisions[w["dec_marks"][-2]:w["dec_marks"][-1]]
    with st.expander(f"Decisions and reasons ({len(decisions)})", expanded=w["step"] in (3, 4)):
        if w["step"] == 5:
            st.caption("No decisions: the tampered message never reached a node.")
        for d in decisions:
            st.markdown(f"- `{d.node}` · {d.invoice_id}: {plain(d.decision)}")

    if w["step"] >= 4:
        log = net.ack_log
        with st.expander(f"Acknowledgment log ({len(log.entries)} entries) · chain "
                         + ("verified ✅" if log.verify(net.directory) else "BROKEN ❌"), expanded=w["step"] == 4):
            st.caption("Append-only and hash-chained. Funders publish a digest of every acknowledgment before funding. "
                       "No amounts or prices: only hashes and node ids.")
            st.dataframe(pd.DataFrame([{
                "#": e.seq, "Invoice hash": e.invoice_hash[:12] + "…", "Buyer": e.buyer_id, "Funder": e.funder_id,
                "Ack digest": e.ack_digest[:12] + "…", "Previous entry": e.prev_hash[:12] + "…",
                "Entry hash": e.entry_hash[:12] + "…",
            } for e in log.entries]), hide_index=True, width="stretch")

    if w["step"] == 6:
        overdue = [e.envelope for e in new if e.msg_type == "PAYMENT_OVERDUE"]
        for env in overdue:
            st.warning(f"⏰ **{env.sender}** sent a signed overdue notice to {env.recipients[0]}: "
                       f"{describe(env)}. The protocol cannot force payment; this notice is the evidence for "
                       "enforcement under the buyer's signed undertaking.")
        late = [x for n in net.nodes.values() for x in n.expected.values() if x.status == "PAID_LATE"]
        for x in late:
            st.info(f"{x.invoice_id}: paid {x.days_late} days late and confirmed by the payee once its bank credited it.")
        with st.expander("Payment files (ISO 20022 pain.001) each node would send to its bank"):
            payment_files(net, key="walk")

    if w["step"] >= len(STEPS):
        st.success("Done. Each supplier was paid early, each invoice was financed exactly once, "
                   "and the buyer paid exactly once at maturity.")
        rows = []
        for s in (w["a"], w["b"]):
            for r in s.receivables.values():
                days = (r.invoice.due_date - r.invoice.issue_date).days
                factoring = r.invoice.amount * s.profile.factoring_apr * days / 365
                rows.append({
                    "Invoice": r.invoice.invoice_id, "Supplier": s.id, "Face value": money(r.invoice.amount, 2),
                    "Received": money(r.received, 2), "On": r.received_on, "Cost via OpenFloat": money(r.invoice.amount - r.received, 2),
                    "Cost via own factoring": money(factoring, 2),
                })
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


# --- simulation ------------------------------------------------------------------


def payment_files(net, key: str) -> None:
    sent = [e.envelope for e in net.log if e.msg_type == "SETTLEMENT_INSTRUCTION" and e.status == "delivered"]
    payers = sorted({e.body.payer_id for e in sent})
    if not payers:
        st.caption("No payment instructions yet.")
        return
    who = st.selectbox("Payer", payers, key=f"pain-{key}")
    mine = [e for e in sent if e.body.payer_id == who]
    xml = pain001(who, mine)
    st.caption(f"{len(mine)} credit transfers. EndToEndId = the instruction's message id, so the bank's status "
               "and credit reports map back to the protocol. Account numbers and BICs are synthetic placeholders.")
    st.download_button("Download pain.001", xml, file_name=f"{who}-pain.001.xml", mime="application/xml",
                       key=f"dl-{key}")
    st.code(xml if len(xml) < 12_000 else xml[:12_000] + "\n<!-- truncated in the preview; download for the full file -->",
            language="xml")


def sidebar_config() -> ScenarioConfig:
    st.sidebar.markdown('<div class="of-side-title">Simulation settings</div><div class="of-side-sub">Drive the '
                        "Network outcome, Buyer, Suppliers, Funders and Message log tabs. Synthetic data, anchored "
                        "to the sources in docs/market-context.md.</div>", unsafe_allow_html=True)
    seed = st.sidebar.number_input("Random seed", 0, 10_000, 7)
    with st.sidebar.expander("Buyer", expanded=True):
        opening = st.slider("Opening cash (k)", 500, 5_000, 1_600, 100)
        floor = st.slider("Cash floor (k)", 0, 4_000, 1_500, 100)
        inflow = st.slider("Daily collections (k)", 0, 200, 35, 5)
        cash_yield = st.slider("Cash yield, e.g. money-market fund (%)", 0.0, 8.0, 4.0, 0.25) / 100
        disc = st.slider("APR the buyer asks for early payment (%)", 0.0, 15.0, 5.0, 0.25) / 100
        forecast = st.toggle("Use payables forecast", True,
                             help="Without it, the buyer only sees invoices it has already received.")
        greedy = st.toggle("Greedy allocator instead of MILP", False)
        late_share = st.slider("Maturities paid late (%)", 0, 100, 0, 5) / 100
        late_days = st.slider("Days late", 0, 30, 5, 1)
    with st.sidebar.expander("Funders"):
        a_apr = st.slider("Bank A APR (%)", 2.0, 15.0, 5.5, 0.1) / 100
        a_lim = st.slider("Bank A limit (k)", 0, 5_000, 1_500, 100)
        b_apr = st.slider("Bank B APR (%)", 2.0, 15.0, 6.2, 0.1) / 100
        b_lim = st.slider("Bank B limit (k)", 0, 5_000, 1_500, 100)
    with st.sidebar.expander("Suppliers"):
        p_early = st.slider("Share wanting early payment (%)", 0, 100, 60, 5) / 100
        f_lo, f_hi = st.slider("Own factoring APR range (%)", 5, 80, (20, 45), 1)
        dip = st.slider("Share attempting double financing (%)", 0, 50, 5, 1) / 100
    return ScenarioConfig(
        seed=int(seed), opening_cash=opening * 1000_00, cash_floor=floor * 1000_00, daily_inflow=inflow * 1000_00,
        cash_yield=cash_yield, discount_apr=disc, use_forecast=forecast, optimiser="greedy" if greedy else "milp",
        p_want_early=p_early, late_share=late_share, late_days=late_days if late_share else 0, factoring_apr=(f_lo / 100, f_hi / 100), p_double_dip=dip,
        funders=[
            ("bank-a", a_lim * 1000_00, a_apr),
            ("bank-b", b_lim * 1000_00, b_apr),
        ],
    )


@st.cache_resource(show_spinner="Running the network…", max_entries=8)
def run_scenario(cfg: ScenarioConfig):
    sc = build(cfg)
    sc.run()
    return sc, evaluate(sc)


def cash_chart(sc) -> alt.Chart:
    actual = sc.buyer.balance_path(sc.config.start, sc.net.today - timedelta(days=1))
    base = _baseline_path(sc)
    rows = [{"Day": d, "Series": SERIES_ACTUAL, "Balance": v / 100} for d, v in actual.items()]
    rows += [{"Day": d, "Series": SERIES_BASE, "Balance": v / 100} for d, v in base.items()]
    df = pd.DataFrame(rows)
    floor = sc.config.cash_floor / 100
    color = alt.Color("Series:N", scale=alt.Scale(
        domain=[SERIES_ACTUAL, SERIES_BASE], range=[BLUE, ORANGE]))
    hover = alt.selection_point(fields=["Day"], nearest=True, on="pointerover", empty=False, clear="pointerout")
    base_chart = alt.Chart(df).encode(
        x=alt.X("Day:T", title=None, axis=alt.Axis(format="%d %b", grid=False)),
        y=alt.Y("Balance:Q", title="Buyer cash balance", scale=alt.Scale(zero=False, nice=True),
                axis=alt.Axis(format="~s", tickCount=5)),
    )
    lines = base_chart.mark_line(strokeWidth=2, interpolate="step-after").encode(color=color)
    points = base_chart.mark_point(size=70, filled=True, stroke="#fcfcfb", strokeWidth=2).encode(
        color=color, opacity=alt.condition(hover, alt.value(1), alt.value(0)))
    wide = df.pivot(index="Day", columns="Series", values="Balance").reset_index()
    rule = alt.Chart(wide).mark_rule(color=AXIS, strokeWidth=1).encode(
        x="Day:T",
        opacity=alt.condition(hover, alt.value(1), alt.value(0)),
        tooltip=[alt.Tooltip("Day:T", format="%d %b %Y")]
        + [alt.Tooltip(f"{c}:Q", format=",.0f") for c in wide.columns if c != "Day"],
    ).add_params(hover)
    floor_rule = alt.Chart(pd.DataFrame({"y": [floor]})).mark_rule(color=MUTED, strokeWidth=1).encode(y="y:Q")
    floor_label = alt.Chart(pd.DataFrame({"Day": [df["Day"].max()], "y": [floor], "t": [f"Cash floor {floor:,.0f}"]})).mark_text(
        align="right", dy=13, color=INK_2, fontSize=12).encode(x="Day:T", y="y:Q", text="t:N")
    return chart_theme(alt.layer(floor_rule, floor_label, lines, points, rule).properties(height=320))


def funding_sources(sc) -> list[tuple[str, int, int]]:
    """(label, invoices, face value) per source of early-payment money."""
    out = [("Buyer's own cash", sum(1 for p in sc.buyer.payables.values() if p.early_advance is not None),
            sum(p.invoice.amount for p in sc.buyer.payables.values() if p.early_advance is not None))]
    for f in sc.funders:
        out.append((f.id, len(f.book), sum(u.amount for _, u in f.book)))
    return out


def overview_tab(sc, m) -> None:
    st.subheader("Network outcome")
    st.caption(f"{m.invoices} invoices from {len(sc.suppliers)} suppliers over {sc.config.days} days · "
               f"{len(sc.net.log):,} signed messages · synthetic data")
    k = st.columns(4)
    saving_pct = m.supplier_saving / m.supplier_cost_factoring if m.supplier_cost_factoring else 0
    k[0].metric("Supplier financing cost", money(m.supplier_cost_openfloat), f"-{saving_pct:.0%} vs own factoring",
                delta_color="inverse",
                help=f"Cost of early payment via OpenFloat. The same invoices at each supplier's own factoring rate: {money(m.supplier_cost_factoring)}.")
    k[1].metric("Requests financed", f"{m.financed} / {m.early_requests}",
                help="Invoices paid to the supplier before the due date.")
    k[2].metric("Double financing blocked", f"{m.double_financing_blocked}",
                help="Second claims on an already financed invoice, rejected by the buyer's node.")
    k[3].metric("Days under cash floor", f"{m.floor_breaches_caused}",
                help="Days the buyer's balance was under the floor and lower than if it had paid everything on the due date. "
                     "Payables forecast is " + ("on." if sc.config.use_forecast else "OFF."))

    k2 = st.columns(4)
    k2[0].metric("Face value financed", money(m.face_financed))
    k2[1].metric("Supplier saving", money(m.supplier_saving))
    k2[2].metric("Buyer net discount income", money(m.buyer_net_gain),
                 help="Discounts earned on early payments, minus the yield the cash would have earned.")
    k2[3].metric("Bank payments confirmed", f"{m.payments_confirmed} / {m.payments_executed}",
                 help="Payments the mock banks executed, and how many the payee confirmed from its own bank's credit report.")
    if m.overdue_notices:
        st.caption(f"⏰ {m.overdue_notices} overdue notices sent; {m.paid_late} payments arrived late; "
                   f"{m.unpaid_at_end} still unpaid when the run ended.")

    c1, c2 = st.columns([2, 1])
    with c1:
        st.markdown("**Buyer cash balance against its floor**")
        st.altair_chart(cash_chart(sc), width="stretch")
    with c2:
        st.markdown("**Who funded early payments**")
        grid = st.columns(2)
        for i, (label, n, face) in enumerate(funding_sources(sc)):
            short = "Buyer cash" if label == "Buyer's own cash" else label
            grid[i % 2].metric(short, f"{n}", f"{face / 1e8:.2f}M face value", delta_color="off", delta_arrow="off",
                               help=f"{label}: invoices funded early, and their total face value ({money(face)}).")

    c3, c4 = st.columns(2)
    with c3:
        st.markdown("**Financing cost per invoice: OpenFloat vs the supplier's own factoring**")
        st.altair_chart(cost_scatter(sc), width="stretch")
        st.caption("Each dot is one financed invoice. Own factoring is priced off the supplier's credit, "
                   "OpenFloat off the buyer's.")
    with c4:
        st.markdown("**Early payments funded per week, by source**")
        st.altair_chart(weekly_sources(sc), width="stretch")
        st.caption("The buyer funds from its own cash while its floor allows; banks take the rest.")


def buyer_tab(sc) -> None:
    b = sc.buyer
    rows = []
    for p in b.payables.values():
        rows.append({
            "Invoice": p.invoice.invoice_id, "Supplier": p.invoice.supplier_id, "Amount": p.invoice.amount / 100,
            "Due": p.invoice.due_date, "Status": p.status.value.replace("_", " ").title(), "Paid to": p.payee,
            "Paid early on": p.early_pay_date, "Discount earned": (p.invoice.amount - p.early_advance) / 100 if p.early_advance else None,
        })
    st.subheader(f"Buyer: {b.id}")
    st.caption("The buyer's node is the source of truth for who gets paid at maturity.")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"Amount": st.column_config.NumberColumn(format="%,.2f"),
                                "Discount earned": st.column_config.NumberColumn(format="%,.2f")})
    rejected = [d for d in sc.net.decisions if d.node == b.id and "REJECTED" in d.decision]
    if rejected:
        st.markdown("**Rejected claims**")
        st.dataframe(pd.DataFrame([{"Day": d.day, "Invoice": d.invoice_id, "Reason": d.decision} for d in rejected]),
                     hide_index=True, width="stretch")


def supplier_tab(sc) -> None:
    sup = st.selectbox("Supplier", [s.id for s in sc.suppliers])
    s = next(x for x in sc.suppliers if x.id == sup)
    p = s.profile
    st.caption(f"Own factoring rate {p.factoring_apr:.1%} APR"
               + (" · ⚠️ attempts double financing" if p.double_dip else ""))
    rows = []
    for r in s.receivables.values():
        days = (r.invoice.due_date - r.invoice.issue_date).days
        rows.append({
            "Invoice": r.invoice.invoice_id, "Amount": r.invoice.amount / 100, "Due": r.invoice.due_date,
            "Outcome": r.outcome, "Received": r.received / 100 if r.received else None, "On": r.received_on,
            "Cost": (r.invoice.amount - r.received) / 100 if r.received and r.received_on < r.invoice.due_date else None,
            "Own factoring would cost": r.invoice.amount * p.factoring_apr * days / 365 / 100 if r.want_early else None,
        })
    fmt = st.column_config.NumberColumn(format="%,.2f")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={c: fmt for c in ("Amount", "Received", "Cost", "Own factoring would cost")})
    inv = st.selectbox("Offers received for invoice", [r.invoice.invoice_id for r in s.receivables.values() if r.offers] or ["—"])
    r = s.receivables.get(inv)
    if r and r.offers:
        st.dataframe(pd.DataFrame([{
            "From": e.sender, "Route": ROUTE_LABEL[e.body.kind], "Advance": e.body.advance / 100,
            "Pay date": e.body.pay_date, "APR": f"{e.body.annualised_rate:.2%}",
        } for e in r.offers]), hide_index=True, width="stretch", column_config={"Advance": fmt})


def funder_tab(sc) -> None:
    st.markdown("**Exposure on the buyer over time, against each bank's limit**")
    st.altair_chart(exposure_chart(sc), width="stretch")
    st.caption("Firm offers reserve limit until they expire; exposure falls away as the buyer pays at maturity.")
    for f in sc.funders:
        price = f"{f.policy.apr:.2%} APR"
        funded = sum(o.advance for o, _ in f.book)
        face = sum(u.amount for _, u in f.book)
        st.markdown(f"**{f.id}** · Bank · {price} · limit {money(f.policy.limit)}")
        c = st.columns(3)
        c[0].metric("Invoices funded", len(f.book))
        c[1].metric("Advanced", money(funded))
        c[2].metric("Income at maturity", money(face - funded))
        if not f.book:
            st.caption(f"{f.id} funded nothing: a cheaper bank had room for every request.")
        st.divider()


def log_tab(sc) -> None:
    c1, c2, c3 = st.columns(3)
    types = sorted({e.msg_type for e in sc.net.log})
    pick = c1.multiselect("Message types", types, default=[])
    inv = c2.text_input("Invoice id contains")
    only_bad = c3.toggle("Rejected only")
    entries = [e for e in sc.net.log if (not pick or e.msg_type in pick) and inv.upper() in e.invoice_id.upper()
               and (not only_bad or e.status != "delivered")]
    st.caption(f"{len(entries):,} of {len(sc.net.log):,} messages")
    show_messages(entries[:2000], key="log")
    with st.expander("Payment files (ISO 20022 pain.001)"):
        payment_files(sc.net, key="log")


# --- layout ----------------------------------------------------------------------

st.markdown(
    '<div class="of-hero"><div><h1>OpenFloat</h1><div class="of-tag">Open protocol for financing at the payment '
    "layer. A supplier with a buyer-approved invoice gets paid early by the cheapest source of money.</div></div>"
    '<div class="of-pills"><span class="of-pill">No central operator</span><span class="of-pill">Signed messages</span>'
    '<span class="of-pill">No double financing</span><span class="of-pill">Apache-2.0</span></div></div>',
    unsafe_allow_html=True,
)

cfg = sidebar_config()
tabs = st.tabs(["Walkthrough", "Network outcome", "Buyer", "Suppliers", "Funders", "Message log"])
with tabs[0]:
    walkthrough_tab()
sc, m = run_scenario(cfg)
with tabs[1]:
    overview_tab(sc, m)
with tabs[2]:
    buyer_tab(sc)
with tabs[3]:
    supplier_tab(sc)
with tabs[4]:
    funder_tab(sc)
with tabs[5]:
    log_tab(sc)
