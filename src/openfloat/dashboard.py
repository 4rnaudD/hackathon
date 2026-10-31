"""OpenFloat dashboard.

Run with:  uv run openfloat dashboard   (or: uv run streamlit run src/openfloat/dashboard.py)
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from openfloat.messages import Envelope, Invoice, OfferKind, SettlementInstruction
from openfloat.network import Network
from openfloat.nodes import (
    BuyerNode,
    BuyerPolicy,
    FunderNode,
    FunderPolicy,
    SupplierNode,
    SupplierProfile,
)
from openfloat.sim import PUBLIC_B2B_VIRTUAL_IC, ScenarioConfig, _baseline_path, build, evaluate

# reference palette (light): categorical slots 1-2, ink and chrome
BLUE, ORANGE = "#2a78d6", "#eb6834"
INK, INK_2, MUTED, GRID, AXIS = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

SERIES_ACTUAL, SERIES_BASE = "With early payments", "All paid on due date"
ROUTE_LABEL = {"BUYER_DISCOUNT": "Buyer's own cash", "FUNDER": "Bank", "CARD": "Card"}

st.set_page_config(page_title="OpenFloat", page_icon="◐", layout="wide")


def money(cents: float, dp: int = 0) -> str:
    return f"{cents / 100:,.{dp}f}"


def chart_theme(chart: alt.Chart) -> alt.Chart:
    return (
        chart.configure(font=FONT, background="#fcfcfb")
        .configure_view(stroke=None)
        .configure_axis(
            gridColor=GRID, gridWidth=1, domainColor=AXIS, tickColor=AXIS,
            labelColor=MUTED, titleColor=INK_2, labelFontSize=12, titleFontSize=12, titleFontWeight="normal",
        )
        .configure_legend(labelColor=INK_2, titleColor=INK_2, orient="top", title=None, symbolStrokeWidth=2)
    )


def plain(text: str) -> str:
    for code, label in {"BUYER_DISCOUNT": "buyer's own cash offer", "FUNDER": "bank offer", "CARD": "card offer"}.items():
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
            return f"Claims the invoice: supplier's signed acceptance attached"
        case "ASSIGNMENT_ACK":
            return f"Will pay {b.new_payee_id} {money(b.amount, 2)} on {b.due_date}"
        case "ASSIGNMENT_REJECTED":
            return f"Claim rejected: {b.reason}"
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
     "Each supplier broadcasts an early-payment request with the buyer's signed promise attached. Banks and the card issuer check the signature and reply with **firm offers priced on the buyer's credit**."),
    ("The buyer decides where its own cash goes",
     "The buyer's optimiser offers early payment from its own cash only where the discount beats its cash yield **and** its cash stays above the floor on every future day."),
    ("Suppliers accept, and one tries to double-finance",
     "Each supplier takes the best offer. Supplier B also accepts the other offers, trying to finance the same invoice twice. Watch the buyer's node reject the second and third claims."),
    ("Someone tampers with a message",
     "An attacker changes the amount on a payment instruction in transit. Every node verifies signatures, so the message is rejected before anyone acts on it."),
    ("Maturity",
     "On each due date the buyer pays whoever it acknowledged: the bank for Supplier B's invoice. The supplier was paid weeks earlier."),
]


def new_walkthrough() -> dict:
    net = Network(WALK_START)
    buyer = BuyerNode(
        "acme-buyer", net,
        BuyerPolicy(opening_cash=125_000_00, cash_floor=60_000_00, cash_yield=0.04, discount_apr=0.05,
                    approval_limit=500_000_00),
        {"supplier-a", "supplier-b"},
    )
    FunderNode("bank-a", net, FunderPolicy(OfferKind.FUNDER, limit=500_000_00, apr=0.055))
    FunderNode("bank-b", net, FunderPolicy(OfferKind.FUNDER, limit=500_000_00, apr=0.062))
    FunderNode("card-co", net, FunderPolicy(OfferKind.CARD, limit=500_000_00, card_fee=PUBLIC_B2B_VIRTUAL_IC))
    a = SupplierNode("supplier-a", net, SupplierProfile(factoring_apr=0.30))
    b = SupplierNode("supplier-b", net, SupplierProfile(factoring_apr=0.36, double_dip=True))
    return {"net": net, "buyer": buyer, "a": a, "b": b, "step": 0, "marks": [0], "dec_marks": [0]}


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
        while net.today <= last_due:
            buyer.settle_maturities()
            net.deliver()
            for f in net.funders:
                f.release_matured()
            net.today += timedelta(days=1)
    w["step"] += 1
    w["marks"].append(len(net.log))
    w["dec_marks"].append(len(net.decisions))


def walkthrough_tab() -> None:
    if "walk" not in st.session_state:
        st.session_state.walk = new_walkthrough()
    w = st.session_state.walk
    net = w["net"]

    left, right = st.columns([3, 1])
    with left:
        st.subheader("How a payment gets financed, step by step")
        st.caption("One buyer, two banks, a card issuer and two suppliers, each running its own node. "
                   "Every message below is a real signed envelope produced by the reference implementation.")
    with right:
        c1, c2 = st.columns(2)
        if c1.button("Next step ▶", type="primary", disabled=w["step"] >= len(STEPS), width="stretch"):
            advance(w)
            st.rerun()
        if c2.button("Restart", width="stretch"):
            st.session_state.walk = new_walkthrough()
            st.rerun()

    st.progress(w["step"] / len(STEPS), text=f"Step {w['step']} of {len(STEPS)}")

    if w["step"] == 0:
        st.info("Press **Next step** to start. " + STEPS[0][1].split(".")[0] + ".")
        parts = pd.DataFrame([
            {"Node": n.id, "Role": n.role, "Public key (Ed25519)": n.identity.public_key[:24] + "…"}
            for n in net.nodes.values()
        ])
        st.dataframe(parts, hide_index=True, width="stretch")
        return

    title, text = STEPS[w["step"] - 1]
    st.markdown(f"#### {w['step']}. {title}")
    st.markdown(text)

    new = net.log[w["marks"][-2]:w["marks"][-1]]
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
            st.error(f"🛡️ **Blocked:** {funder}'s claim on {d.invoice_id}. The buyer already acknowledged "
                     f"{winner} for this invoice, so {funder} never pays out.")

    st.markdown("**Messages in this step**")
    show_messages(new, key=f"walk-msgs-{w['step']}", compact=True)
    decisions = net.decisions[w["dec_marks"][-2]:w["dec_marks"][-1]]
    with st.expander(f"Decisions and reasons ({len(decisions)})", expanded=w["step"] in (3, 4)):
        if w["step"] == 5:
            st.caption("No decisions: the tampered message never reached a node.")
        for d in decisions:
            st.markdown(f"- `{d.node}` · {d.invoice_id}: {plain(d.decision)}")

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


def sidebar_config() -> ScenarioConfig:
    st.sidebar.header("Simulation settings")
    st.sidebar.caption("Synthetic data. Ranges are anchored to the sources in docs/market-context.md.")
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
    with st.sidebar.expander("Funders"):
        a_apr = st.slider("Bank A APR (%)", 2.0, 15.0, 5.5, 0.1) / 100
        a_lim = st.slider("Bank A limit (k)", 0, 5_000, 1_500, 100)
        b_apr = st.slider("Bank B APR (%)", 2.0, 15.0, 6.2, 0.1) / 100
        b_lim = st.slider("Bank B limit (k)", 0, 5_000, 1_500, 100)
        card_fee = st.slider("Card fee paid by supplier (%)", 0.0, 4.0, PUBLIC_B2B_VIRTUAL_IC * 100, 0.1,
                             help="Default: Visa's published B2B virtual-card interchange (2.0%, Oct 2025 schedule). "
                                  "Excludes the acquirer's markup, so the real supplier cost is a little higher.") / 100
        card_lim = st.slider("Card limit (k)", 0, 2_000, 500, 50)
    with st.sidebar.expander("Suppliers"):
        p_early = st.slider("Share wanting early payment (%)", 0, 100, 60, 5) / 100
        f_lo, f_hi = st.slider("Own factoring APR range (%)", 5, 80, (20, 45), 1)
        dip = st.slider("Share attempting double financing (%)", 0, 50, 5, 1) / 100
    return ScenarioConfig(
        seed=int(seed), opening_cash=opening * 1000_00, cash_floor=floor * 1000_00, daily_inflow=inflow * 1000_00,
        cash_yield=cash_yield, discount_apr=disc, use_forecast=forecast, optimiser="greedy" if greedy else "milp",
        p_want_early=p_early, factoring_apr=(f_lo / 100, f_hi / 100), p_double_dip=dip,
        funders=[
            ("bank-a", OfferKind.FUNDER, a_lim * 1000_00, a_apr, 0.0),
            ("bank-b", OfferKind.FUNDER, b_lim * 1000_00, b_apr, 0.0),
            ("card-co", OfferKind.CARD, card_lim * 1000_00, 0.0, card_fee),
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
    k[1].metric("Early-payment requests financed", f"{m.financed} / {m.early_requests}",
                help="Invoices paid to the supplier before the due date.")
    k[2].metric("Double financing blocked", f"{m.double_financing_blocked}",
                help="Second claims on an already financed invoice, rejected by the buyer's node.")
    k[3].metric("Days under floor from early pay", f"{m.floor_breaches_caused}",
                help="Days the buyer's balance was under the floor and lower than if it had paid everything on the due date. "
                     "Payables forecast is " + ("on." if sc.config.use_forecast else "OFF."))

    k2 = st.columns(4)
    k2[0].metric("Face value financed", money(m.face_financed))
    k2[1].metric("Supplier saving", money(m.supplier_saving))
    k2[2].metric("Buyer net discount income", money(m.buyer_net_gain),
                 help="Discounts earned on early payments, minus the yield the cash would have earned.")
    k2[3].metric("Messages signed and verified", f"{len(sc.net.log) - m.bad_signatures:,}")

    c1, c2 = st.columns([2, 1])
    with c1:
        st.markdown("**Buyer cash balance against its floor**")
        st.altair_chart(cash_chart(sc), width="stretch")
    with c2:
        st.markdown("**Who funded early payments**")
        grid = st.columns(2)
        for i, (label, n, face) in enumerate(funding_sources(sc)):
            grid[i % 2].metric(label, f"{n}", money(face) + " face value", delta_color="off", delta_arrow="off",
                               help="Invoices funded early, and their total face value.")
        if "CARD" not in m.by_route:
            st.caption("Card never won here: at the public 2% B2B interchange, banks are cheaper unless their limits are used up.")


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
    st.caption(f"Own factoring rate {p.factoring_apr:.1%} APR · accepts card: {'yes' if p.accepts_card else 'no'}"
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
    for f in sc.funders:
        kind = "Card issuer" if f.policy.kind == OfferKind.CARD else "Bank"
        price = f"{f.policy.card_fee:.1%} fee (public B2B interchange)" if f.policy.kind == OfferKind.CARD else f"{f.policy.apr:.2%} APR"
        funded = sum(o.advance for o, _ in f.book)
        face = sum(u.amount for _, u in f.book)
        st.markdown(f"**{f.id}** · {kind} · {price} · limit {money(f.policy.limit)}")
        c = st.columns(3)
        c[0].metric("Invoices funded", len(f.book))
        c[1].metric("Advanced", money(funded))
        c[2].metric("Income at maturity", money(face - funded))
        peak = 0
        exposure = 0
        events = sorted([(o.pay_date, u.amount) for o, u in f.book] + [(u.due_date, -u.amount) for _, u in f.book])
        for _, amt in events:
            exposure += amt
            peak = max(peak, exposure)
        st.progress(min(peak / f.policy.limit, 1.0) if f.policy.limit else 0.0,
                    text=f"Peak exposure on the buyer: {money(peak)} of {money(f.policy.limit)} limit")
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


# --- layout ----------------------------------------------------------------------

st.title("OpenFloat")
st.markdown("Open protocol for financing at the payment layer. A supplier with a buyer-approved invoice gets paid "
            "early by the cheapest source of money, with **no central operator** and **no double financing**.")

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
