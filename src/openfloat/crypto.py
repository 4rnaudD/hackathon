"""Identities, canonical encoding and Ed25519 signatures.

Every OpenFloat message is signed over its canonical JSON encoding, so any
participant can verify who said what without trusting the transport.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def canonical_json(obj: object) -> bytes:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_hex(obj: object) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@dataclass
class Identity:
    """A participant's keypair. The private key never leaves its node."""

    node_id: str
    _key: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate, repr=False)

    @property
    def public_key(self) -> str:
        raw = self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return _b64(raw)

    def sign(self, payload: object) -> str:
        return _b64(self._key.sign(canonical_json(payload)))


def verify(public_key_b64: str, payload: object, signature_b64: str) -> bool:
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature_b64), canonical_json(payload))
        return True
    except (InvalidSignature, ValueError):
        return False


class Directory:
    """Maps node ids to public keys and roles.

    In production this is the participant registry (comparable to Peppol's
    SML/SMP lookup or a PKI). The protocol only requires that every node can
    resolve a sender id to a public key.
    """

    def __init__(self) -> None:
        self._keys: dict[str, str] = {}
        self._roles: dict[str, str] = {}

    def register(self, identity: Identity, role: str) -> None:
        self._keys[identity.node_id] = identity.public_key
        self._roles[identity.node_id] = role

    def public_key(self, node_id: str) -> str | None:
        return self._keys.get(node_id)

    def role(self, node_id: str) -> str | None:
        return self._roles.get(node_id)

    def nodes_with_role(self, role: str) -> list[str]:
        return [n for n, r in self._roles.items() if r == role]
