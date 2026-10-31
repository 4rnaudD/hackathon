"""In-process transport for the reference implementation.

Delivers signed envelopes between nodes, verifying every signature (including
embedded proofs) before a node sees the message. Keeps a full message log and
decision log for the audit trail. A production deployment would replace this
with HTTPS between independently operated nodes; the messages are identical.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from .crypto import Directory
from .messages import Envelope

if TYPE_CHECKING:
    from .nodes import BuyerNode, FunderNode, Node, SupplierNode


@dataclass
class LogEntry:
    day: date
    msg_type: str
    sender: str
    recipients: list[str]
    invoice_id: str
    status: str  # "delivered" or "rejected: <reason>"
    envelope: Envelope


@dataclass
class DecisionEntry:
    day: date
    node: str
    invoice_id: str
    decision: str


class Network:
    def __init__(self, start: date) -> None:
        self.today = start
        self.directory = Directory()
        self.nodes: dict[str, "Node"] = {}
        self.queue: deque[Envelope] = deque()
        self.log: list[LogEntry] = []
        self.decisions: list[DecisionEntry] = []

    def join(self, node: "Node") -> None:
        self.nodes[node.id] = node
        self.directory.register(node.identity, node.role)

    def post(self, env: Envelope) -> None:
        self.queue.append(env)

    def record_decision(self, node: str, invoice_id: str, decision: str) -> None:
        self.decisions.append(DecisionEntry(self.today, node, invoice_id, decision))

    def deliver(self) -> None:
        """Deliver queued messages (and any replies they trigger) until quiet."""
        while self.queue:
            env = self.queue.popleft()
            inv = getattr(env.body, "invoice_id", "")
            if not env.verify(self.directory):
                self.log.append(LogEntry(self.today, env.body.type, env.sender, env.recipients, inv, "rejected: bad signature", env))
                continue
            self.log.append(LogEntry(self.today, env.body.type, env.sender, env.recipients, inv, "delivered", env))
            for r in env.recipients:
                if r in self.nodes:
                    self.nodes[r].handle(env)

    # --- daily cycle ----------------------------------------------------------

    def _by_role(self, role: str) -> list:
        return [n for n in self.nodes.values() if n.role == role]

    @property
    def buyers(self) -> list["BuyerNode"]:
        return self._by_role("buyer")

    @property
    def suppliers(self) -> list["SupplierNode"]:
        return self._by_role("supplier")

    @property
    def funders(self) -> list["FunderNode"]:
        return self._by_role("funder")

    def run_day(self, issue=None) -> None:
        """One business day. `issue(net)` lets the caller have suppliers send invoices."""
        if issue:
            issue(self)
        self.deliver()  # invoices -> undertakings
        for s in self.suppliers:
            s.request_early_pay()
        self.deliver()  # requests -> funder offers (buyer queues)
        for b in self.buyers:
            b.run_batch()
        self.deliver()  # buyer offers
        for s in self.suppliers:
            s.choose_offers()
        self.deliver()  # accepts -> assignment notices -> acks/rejections -> settlements
        for b in self.buyers:
            b.settle_maturities()
        self.deliver()
        for f in self.funders:
            f.release_matured()
        self.today += timedelta(days=1)

    def settlements(self) -> list[Envelope]:
        return [e.envelope for e in self.log if e.msg_type == "SETTLEMENT_INSTRUCTION" and e.status == "delivered"]
