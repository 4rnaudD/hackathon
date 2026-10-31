# OpenFloat

**An open protocol for financing at the payment layer.**

A supplier holding an invoice its buyer has approved should be able to get paid today, at a price based on
the *buyer's* credit, from whoever offers the cheapest money: the buyer's own cash or a bank/funder.
OpenFloat is the open, operator-less protocol that makes this possible, plus an Apache-2.0
reference implementation.

> Status: `openfloat/0.1` draft. Reference implementation and simulation only. No real money moves,
> and nodes only ever emit settlement *instructions* for existing payment rails.

## The problem

- Small suppliers who need cash before their invoices are due use **factoring**, which typically costs
  **~1–5% per month, around 30–57% APR**. That price reflects the supplier's credit.
- The same invoice, once a large buyer has confirmed it will pay, is close to a **buyer-quality asset**.
  Banks fund confirmed payables at roughly **benchmark + 60–100 bps**.
- That gap is today captured through **proprietary supply-chain-finance platforms**, each a closed
  silo run by one operator. Factoring volume alone is over **€4tn a year**, and supply chain finance is
  around **$2.6tn**.
- Silos are opaque. **Double financing**, the same receivable funded more than once, is a recurring
  failure mode in the sector. It is under investigation in the 2025 First Brands bankruptcy, and
  opacity was central to the 2021 Greensill collapse.

Sources and confidence levels: [docs/market-context.md](docs/market-context.md).

## How it works

**The gap.** The supplier wants cash on day 0. The buyer has approved the invoice and will pay it on
day 60. Either of two liquidity sources can fill the gap, and the supplier takes the cheapest offer.

```mermaid
flowchart LR
    B["Buyer<br/>owes face value on day 60"]
    S["Supplier<br/>wants cash on day 0"]
    F["Bank or funder"]

    B -->|"Buyer cash: pays early,<br/>face value minus a discount"| S
    F -->|"Funder: advance on day 0"| S
    B -.->|"face value on day 60"| F
```

**The messages.** Happy path with two competing banks, followed by a supplier trying to finance the
same invoice a second time.

```mermaid
sequenceDiagram
    autonumber
    participant S as Supplier
    participant B as Buyer node
    participant A as Bank A
    participant X as Bank B
    participant L as Ack log

    S->>B: INVOICE
    B->>S: PAYMENT_UNDERTAKING (pay face value on day 60)
    S->>B: EARLY_PAY_REQUEST + undertaking
    S->>A: EARLY_PAY_REQUEST + undertaking
    S->>X: EARLY_PAY_REQUEST + undertaking
    Note over A,X: verify buyer signature, price off the buyer credit
    A->>S: OFFER (firm)
    X->>S: OFFER (firm)
    B->>S: OFFER BUYER_DISCOUNT (only if its cash allows)
    S->>A: ACCEPT + signed representation
    A->>B: ASSIGNMENT_NOTICE + supplier signed ACCEPT
    Note over B: APPROVED to ASSIGNED, payee is now Bank A
    B->>A: ASSIGNMENT_ACK
    B->>S: ASSIGNMENT_ACK
    A->>L: publish ack entry
    A->>L: check for an earlier ack to another bank
    L-->>A: none found
    A->>S: SETTLEMENT_INSTRUCTION advance, only after ack and log check
    A->>S: PAYMENT_STATUS settled
    S->>A: PAYMENT_RECEIVED

    rect rgba(220, 50, 50, 0.12)
    Note over S,X: Double-financing attempt
    S->>X: ACCEPT second offer + signed representation
    X->>L: check log for this invoice
    L-->>X: already acknowledged to Bank A
    Note over X: declines, keeps the signed representation as evidence
    Note over B,X: If both accepts arrive before Bank A publishes, the buyer node rejects the second ASSIGNMENT_NOTICE instead
    end

    Note over S,X: Day 60, maturity
    B->>A: SETTLEMENT_INSTRUCTION full face value
    B->>A: PAYMENT_STATUS settled
    A->>B: PAYMENT_RECEIVED
```

**The network.** Every participant runs its own node. Nodes exchange signed messages directly, resolve
each other's keys through a participant directory, and emit settlement instructions to the payment
rails each participant already uses. There is no central platform in the message path.

```mermaid
flowchart TB
    D[("Participant directory<br/>node id to public key and role")]

    subgraph P2P["Signed messages, peer to peer"]
        SN["Supplier node"]
        BN["Buyer node"]
        FN["Bank node"]
    end

    SN <--> BN
    SN <--> FN
    FN <--> BN
    D -.->|"key lookup"| P2P

    subgraph RAILS["Existing payment rails"]
        BR["Buyer bank"]
        FR["Bank payment system"]
    end

    BN -->|"SETTLEMENT_INSTRUCTION"| BR
    FN -->|"SETTLEMENT_INSTRUCTION"| FR
```

- **No central operator.** The buyer, who owes the money, is the natural source of truth for who gets
  paid. Its node acknowledges one assignment per invoice and rejects any second claim, the same logic as
  a notice of assignment in receivables law.
- **Everything is signed** (Ed25519 over canonical JSON). Claims carry the counterparty's signed message
  as proof, so any node can verify without trusting anyone in the middle.
- **Double-financing checks beyond the buyer.** Each `ACCEPT` carries the supplier's signed
  representation that the invoice is not already assigned or pledged, which gives funders recourse if it
  was financed outside the protocol. Funders also publish every acknowledgment to an append-only,
  hash-chained transparency log and check it before funding, so a buyer that acknowledges the same
  invoice to two funders is caught before the second one pays.
- **Deterministic decisions.** Pricing, limits and the buyer's cash allocation are deterministic. The
  buyer decides which early payments to fund itself with a MILP that respects a daily cash floor and a
  payables forecast.
- **Payments are evidenced, not moved.** Banks move the money. Each settlement instruction is rendered
  as an ISO 20022 pain.001 payment file, and nodes exchange signed confirmations of what their own banks
  report: `PAYMENT_STATUS` from the payer, `PAYMENT_RECEIVED` from the payee once credited, and
  `PAYMENT_OVERDUE` when an expected payment has not arrived by its due date.

Full protocol: **[SPEC.md](SPEC.md)**.

## Quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/4rnaudD/openfloat && cd openfloat
uv sync
uv run openfloat simulate            # synthetic network: 1 buyer, 25 suppliers, 2 banks
uv run openfloat simulate --no-forecast   # see what happens when the buyer ignores its forecast
uv run openfloat dashboard           # interactive walkthrough, network outcome and message log
uv run pytest                        # protocol and invariant tests
```

## Results on synthetic data

Synthetic scenario: about 185 invoices over 30 days, 60% of suppliers wanting early payment, supplier
factoring rates drawn from 20–45% APR, banks at 5.5% and 6.2%, 5% of suppliers
attempting to double-finance. Seeds 7, 11 and 23.

| Metric | Result |
|---|---|
| Early-payment requests financed | 102–112 per seed, **100%** of requests |
| Route mix | 51–56% buyer cash, the rest banks |
| **Supplier financing cost vs. own factoring** | **~$19.3k vs ~$110k on ~$2.45M financed** (seed 7), 82–85% lower across seeds |
| Double-financing attempts | 0–17 per seed (depends on which suppliers draw as double-dippers), **all blocked** |
| Days the buyer fell below its cash floor *because of early payments* | **0–1 with forecast**, 10–41 without |

**Where it fails:** the buyer's payables forecast is a simple run-rate. When actual invoices exceed it,
early payments can still push the balance under the floor for a few days. A better forecast, or a
safety margin on the floor, would fix that.

All figures come from synthetic data with the stated assumptions. They are not a claim about any real
programme.

## Dashboard

`uv run openfloat dashboard` opens a local Streamlit app with:

- **Walkthrough:** one buyer, two banks and two suppliers exchange real signed messages step
  by step. One supplier tries to finance the same invoice twice, and a message is tampered with in
  transit. Both are rejected. It can also be run with a dishonest buyer that acknowledges the same
  invoice to both banks: the second bank finds the earlier entry in the acknowledgment log and does not
  fund. Mock banks execute each settlement instruction and the nodes exchange the resulting payment
  confirmations. A "buyer pays late" option delays the maturity payment so the payee sends an overdue
  notice.
- **Network outcome:** the synthetic network's results, the buyer's cash balance against its floor, and
  who funded early payments. Every parameter can be changed from the sidebar.
- **Buyer, supplier and funder views**, plus a filterable log where any message can be opened as its
  signed JSON envelope. The Message log tab also shows each node's pain.001 payment file, which can be
  viewed and downloaded.

## Repository layout

```
SPEC.md                    protocol specification (CC BY 4.0)
src/openfloat/crypto.py    identities, canonical JSON, Ed25519, directory
src/openfloat/messages.py  message schemas and signed envelopes
src/openfloat/nodes.py     buyer, supplier and funder reference nodes
src/openfloat/optimiser.py buyer liquidity MILP + greedy fallback
src/openfloat/network.py   in-process transport and audit log
src/openfloat/acklog.py    acknowledgment transparency log
src/openfloat/rails.py     mock bank rails (stand-in for each participant's bank)
src/openfloat/iso20022.py  pain.001 payment files from settlement instructions
src/openfloat/sim.py       synthetic scenarios and evaluation
src/openfloat/dashboard*.py Streamlit dashboard, charts and styling
tests/                     protocol, security and invariant tests
docs/market-context.md     sourced market figures
```

## Roadmap

- HTTPS transport and directory lookup, so nodes run independently
- Plain-language offer explanations for suppliers (LLM-assisted, never deciding)
- Dilution (credit notes, disputes), partial financing, term-extension guardrails
- Mapping invoices to Peppol BIS Billing 3.0 / InvoiceNow
- Card payments as a buyer-side payment option (pay by card now, settle on the card statement), not a funder

## License

Code: [Apache-2.0](LICENSE). Specification: CC BY 4.0.
