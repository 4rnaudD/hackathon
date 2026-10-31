"""Mock bank rails standing in for each participant's own bank.

Nodes hand SETTLEMENT_INSTRUCTIONs to their bank (in production: a pain.001 file
or a bank API call). On the value date the bank debits the payer and credits the
payee, and reports both sides back to its own customer: a status report to the
payer (pain.002) and a credit notification to the payee (camt.054). Nodes turn
those bank reports into signed PAYMENT_STATUS / PAYMENT_RECEIVED messages.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from .messages import Envelope

if TYPE_CHECKING:
    from .network import Network


@dataclass
class BankPayment:
    instruction: Envelope
    submitted_on: date
    executed_on: date | None = None
    bank_reference: str = ""

    @property
    def end_to_end_id(self) -> str:
        return self.instruction.msg_id


class MockRails:
    def __init__(self, net: "Network") -> None:
        self.net = net
        self.payments: list[BankPayment] = []

    def submit(self, instruction: Envelope) -> None:
        self.payments.append(BankPayment(instruction, self.net.today))

    def process(self, today: date) -> list[BankPayment]:
        """Execute every submitted payment whose value date has come; report to both sides."""
        done = []
        for p in self.payments:
            body = p.instruction.body
            if p.executed_on is None and body.value_date <= today:
                p.executed_on = today
                p.bank_reference = "BK" + hashlib.sha256(p.end_to_end_id.encode()).hexdigest()[:14].upper()
                payer, payee = self.net.nodes.get(body.payer_id), self.net.nodes.get(body.payee_id)
                if payer:
                    payer.on_bank_debit(p)
                if payee:
                    payee.on_bank_credit(p)
                done.append(p)
        return done
