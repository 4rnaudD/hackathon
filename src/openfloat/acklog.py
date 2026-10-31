"""Acknowledgment transparency log.

An append-only, hash-chained log in the style of Certificate Transparency.
Funders publish a digest of every ASSIGNMENT_ACK they rely on before funding,
then check for an earlier entry from a different funder for the same invoice.
Such an entry means the buyer acknowledged the invoice twice.

Entries carry no amounts, prices or parties beyond what the invoice hash
commits to. The operator cannot forge entries (funder-signed) or rewrite
history undetectably (hash chain), but could withhold entries; production
deployments use several independent logs, signed tree heads and gossip.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

from .crypto import Directory, Identity, sha256_hex, verify
from .messages import Envelope

GENESIS = "0" * 64


@dataclass(frozen=True)
class AckLogEntry:
    seq: int
    invoice_hash: str
    buyer_id: str
    funder_id: str
    ack_digest: str  # SHA-256 of the signed ASSIGNMENT_ACK envelope
    published_on: date
    prev_hash: str
    funder_signature: str
    entry_hash: str

    def body(self) -> dict:
        """The fields the funder signs (everything the log does not assign)."""
        return {"invoice_hash": self.invoice_hash, "buyer_id": self.buyer_id, "funder_id": self.funder_id,
                "ack_digest": self.ack_digest, "published_on": self.published_on.isoformat()}

    def chained(self) -> dict:
        d = asdict(self)
        d.pop("entry_hash")
        d["published_on"] = self.published_on.isoformat()
        return d


def ack_digest(ack: Envelope) -> str:
    return sha256_hex(ack.model_dump(mode="json"))


class AckLog:
    def __init__(self) -> None:
        self.entries: list[AckLogEntry] = []

    def publish(self, funder: Identity, ack: Envelope, published_on: date) -> AckLogEntry:
        body = ack.body
        draft = {"invoice_hash": body.invoice_hash, "buyer_id": ack.sender, "funder_id": funder.node_id,
                 "ack_digest": ack_digest(ack), "published_on": published_on.isoformat()}
        signature = funder.sign(draft)
        prev = self.entries[-1].entry_hash if self.entries else GENESIS
        partial = AckLogEntry(
            seq=len(self.entries), invoice_hash=body.invoice_hash, buyer_id=ack.sender, funder_id=funder.node_id,
            ack_digest=draft["ack_digest"], published_on=published_on, prev_hash=prev,
            funder_signature=signature, entry_hash="",
        )
        entry = AckLogEntry(**{**asdict(partial), "entry_hash": sha256_hex(partial.chained())})
        self.entries.append(entry)
        return entry

    def for_invoice(self, invoice_hash: str) -> list[AckLogEntry]:
        return [e for e in self.entries if e.invoice_hash == invoice_hash]

    def conflicts(self, entry: AckLogEntry) -> list[AckLogEntry]:
        """Earlier entries for the same invoice from a different funder: buyer equivocation."""
        return [e for e in self.for_invoice(entry.invoice_hash) if e.seq < entry.seq and e.funder_id != entry.funder_id]

    def verify(self, directory: Directory) -> bool:
        """Recompute the chain and check every funder signature."""
        prev = GENESIS
        for i, e in enumerate(self.entries):
            if e.seq != i or e.prev_hash != prev or sha256_hex(e.chained()) != e.entry_hash:
                return False
            key = directory.public_key(e.funder_id)
            if key is None or not verify(key, e.body(), e.funder_signature):
                return False
            prev = e.entry_hash
        return True
