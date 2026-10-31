"""OpenFloat protocol messages.

All amounts are integer minor units (cents). All messages travel inside a
signed Envelope; some bodies embed other signed envelopes as proofs (e.g. an
early-pay request carries the buyer's signed payment undertaking).
"""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from .crypto import Directory, Identity, sha256_hex, verify

PROTOCOL_VERSION = "openfloat/0.1"


class OfferKind(StrEnum):
    BUYER_DISCOUNT = "BUYER_DISCOUNT"  # buyer pays early from own cash, minus a discount
    FUNDER = "FUNDER"  # funder advances now, buyer repays funder at maturity


# --- bodies -----------------------------------------------------------------


class Invoice(BaseModel):
    type: Literal["INVOICE"] = "INVOICE"
    invoice_id: str
    supplier_id: str
    buyer_id: str
    amount: int = Field(gt=0)
    currency: str = "SGD"
    issue_date: date
    due_date: date
    description: str = ""
    po_number: str | None = None

    def content_hash(self) -> str:
        return sha256_hex(self.model_dump(mode="json"))


class PaymentUndertaking(BaseModel):
    """Buyer's irrevocable promise to pay `amount` on `due_date`."""

    type: Literal["PAYMENT_UNDERTAKING"] = "PAYMENT_UNDERTAKING"
    invoice_id: str
    invoice_hash: str
    buyer_id: str
    supplier_id: str
    amount: int
    currency: str
    due_date: date


class InvoiceRejected(BaseModel):
    type: Literal["INVOICE_REJECTED"] = "INVOICE_REJECTED"
    invoice_id: str
    reason: str


class EarlyPayRequest(BaseModel):
    type: Literal["EARLY_PAY_REQUEST"] = "EARLY_PAY_REQUEST"
    invoice_id: str
    requested_date: date
    undertaking: "Envelope"  # signed by the buyer


class Offer(BaseModel):
    type: Literal["OFFER"] = "OFFER"
    offer_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    invoice_id: str
    invoice_hash: str
    offeror_id: str
    kind: OfferKind
    advance: int  # what the supplier receives
    pay_date: date  # when the supplier receives it
    expires: date  # offer invalid after this date
    annualised_rate: float  # effective APR of the discount, for comparison


class Accept(BaseModel):
    type: Literal["ACCEPT"] = "ACCEPT"
    offer: "Envelope"  # the signed offer being accepted
    supplier_id: str


class AssignmentNotice(BaseModel):
    """Funder tells the buyer: the supplier assigned this receivable to me."""

    type: Literal["ASSIGNMENT_NOTICE"] = "ASSIGNMENT_NOTICE"
    invoice_id: str
    invoice_hash: str
    new_payee_id: str
    acceptance: "Envelope"  # supplier's signed Accept, proving consent


class AssignmentAck(BaseModel):
    """Buyer confirms it will pay the new payee at maturity. Issued once per invoice."""

    type: Literal["ASSIGNMENT_ACK"] = "ASSIGNMENT_ACK"
    invoice_id: str
    invoice_hash: str
    new_payee_id: str
    amount: int
    due_date: date


class AssignmentRejected(BaseModel):
    type: Literal["ASSIGNMENT_REJECTED"] = "ASSIGNMENT_REJECTED"
    invoice_id: str
    new_payee_id: str
    reason: str


class SettlementInstruction(BaseModel):
    """Instruction to the payer's own rail (bank file or API). Never moves money itself."""

    type: Literal["SETTLEMENT_INSTRUCTION"] = "SETTLEMENT_INSTRUCTION"
    invoice_id: str
    payer_id: str
    payee_id: str
    amount: int
    currency: str
    value_date: date
    rail: Literal["BANK_TRANSFER"]
    reference: str


Body = Annotated[
    Union[
        Invoice,
        PaymentUndertaking,
        InvoiceRejected,
        EarlyPayRequest,
        Offer,
        Accept,
        AssignmentNotice,
        AssignmentAck,
        AssignmentRejected,
        SettlementInstruction,
    ],
    Field(discriminator="type"),
]


# --- envelope ---------------------------------------------------------------


class Envelope(BaseModel):
    protocol: str = PROTOCOL_VERSION
    msg_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    sender: str
    recipients: list[str]
    sent_on: date
    body: Body
    signature: str = ""

    def signing_payload(self) -> dict:
        return self.model_dump(mode="json", exclude={"signature"})

    @classmethod
    def seal(cls, identity: Identity, recipients: list[str], sent_on: date, body: BaseModel) -> "Envelope":
        env = cls(sender=identity.node_id, recipients=recipients, sent_on=sent_on, body=body)
        env.signature = identity.sign(env.signing_payload())
        return env

    def verify(self, directory: Directory) -> bool:
        """Check this envelope's signature and, recursively, any embedded envelopes."""
        key = directory.public_key(self.sender)
        if key is None or not verify(key, self.signing_payload(), self.signature):
            return False
        for proof in _embedded(self.body):
            if not proof.verify(directory):
                return False
        return True


def _embedded(body: BaseModel) -> list[Envelope]:
    match body:
        case EarlyPayRequest():
            return [body.undertaking]
        case Accept():
            return [body.offer]
        case AssignmentNotice():
            return [body.acceptance]
    return []


for _model in (EarlyPayRequest, Accept, AssignmentNotice):
    _model.model_rebuild()
