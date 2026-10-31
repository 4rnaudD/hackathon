"""Render a node's settlement instructions as an ISO 20022 pain.001.001.09 file.

This is the customer credit transfer initiation a corporate uploads to its bank
(host-to-host or bank API). EndToEndId carries the instruction's msg_id, so the
bank's status reports (pain.002) and credit notifications (camt.054) map back to
exactly one OpenFloat instruction.

Account numbers and BICs here are SYNTHETIC placeholders derived from node ids.
The structure follows pain.001.001.09; it is not validated against the official XSD.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime

from .messages import Envelope

NS = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"


def synthetic_account(node_id: str) -> tuple[str, str]:
    """(account number, BIC) placeholders, stable per node id. Not real bank details."""
    h = hashlib.sha256(node_id.encode()).hexdigest()
    account = str(int(h[:12], 16))[:10].rjust(10, "0")
    bic = "XOF" + "".join(c for c in node_id.upper() if c.isalpha())[:1].ljust(1, "X") + "SGSGXXX"
    return account, bic


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs) -> ET.Element:
    el = ET.SubElement(parent, tag, attrs)
    if text is not None:
        el.text = text
    return el


def _party(parent: ET.Element, role: str, node_id: str) -> None:
    account, bic = synthetic_account(node_id)
    _sub(_sub(parent, role), "Nm", node_id)
    _sub(_sub(_sub(_sub(parent, f"{role}Acct"), "Id"), "Othr"), "Id", account)
    _sub(_sub(_sub(parent, f"{role}Agt"), "FinInstnId"), "BICFI", bic)


def pain001(debtor_id: str, instructions: list[Envelope], created: datetime | None = None) -> str:
    """One file for one debtor: a payment-information block per execution date."""
    instructions = [e for e in instructions if e.body.type == "SETTLEMENT_INSTRUCTION" and e.body.payer_id == debtor_id]
    created = created or datetime(2026, 1, 1)
    msg_id = "OF" + hashlib.sha256("".join(e.msg_id for e in instructions).encode()).hexdigest()[:20].upper()
    total = sum(e.body.amount for e in instructions)

    root = ET.Element("Document", {"xmlns": NS})
    init = _sub(root, "CstmrCdtTrfInitn")
    hdr = _sub(init, "GrpHdr")
    _sub(hdr, "MsgId", msg_id)
    _sub(hdr, "CreDtTm", created.strftime("%Y-%m-%dT%H:%M:%S"))
    _sub(hdr, "NbOfTxs", str(len(instructions)))
    _sub(hdr, "CtrlSum", f"{total / 100:.2f}")
    _sub(_sub(hdr, "InitgPty"), "Nm", debtor_id)

    by_date: dict = defaultdict(list)
    for e in instructions:
        by_date[e.body.value_date].append(e)
    for i, (day, group) in enumerate(sorted(by_date.items()), start=1):
        pmt = _sub(init, "PmtInf")
        _sub(pmt, "PmtInfId", f"{msg_id}-{i:03d}")
        _sub(pmt, "PmtMtd", "TRF")
        _sub(pmt, "NbOfTxs", str(len(group)))
        _sub(pmt, "CtrlSum", f"{sum(e.body.amount for e in group) / 100:.2f}")
        _sub(_sub(pmt, "ReqdExctnDt"), "Dt", day.isoformat())
        _party(pmt, "Dbtr", debtor_id)
        for e in group:
            b = e.body
            tx = _sub(pmt, "CdtTrfTxInf")
            _sub(_sub(tx, "PmtId"), "EndToEndId", e.msg_id)
            _sub(_sub(tx, "Amt"), "InstdAmt", f"{b.amount / 100:.2f}", Ccy=b.currency)
            account, bic = synthetic_account(b.payee_id)
            _sub(_sub(_sub(tx, "CdtrAgt"), "FinInstnId"), "BICFI", bic)
            _sub(_sub(tx, "Cdtr"), "Nm", b.payee_id)
            _sub(_sub(_sub(_sub(tx, "CdtrAcct"), "Id"), "Othr"), "Id", account)
            _sub(_sub(tx, "RmtInf"), "Ustrd", f"OpenFloat {b.invoice_id} {b.reference}")
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
