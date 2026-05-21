"""P-256 keypair + auth secret for RFC 8291 Web Push.

Two values per account:

  - **P-256 keypair** — public key (the `p256dh` field in our XEP-0357
    publish_options form) is shared with the server; private key stays in
    libsecret and is used to decrypt incoming push payloads.
  - **Auth secret** — 16-byte random; ties the encryption to a specific
    subscription. Stored in libsecret too, shared with the server.

Storage is libsecret, keyed by `(jid, purpose)` where purpose is one of
`up_private_key` (32-byte raw P-256 scalar, hex) or `up_auth_secret`
(16 random bytes, hex).

Public key is always derivable from the private key on the fly — no
need to store it separately.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from patch.store import secrets

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PushKeys:
    private_scalar: bytes   # 32 bytes, big-endian
    public_raw: bytes       # 65 bytes, 0x04 || X || Y
    auth_secret: bytes      # 16 bytes

    def public_b64url(self) -> str:
        import base64
        return base64.urlsafe_b64encode(self.public_raw).rstrip(b"=").decode()

    def auth_b64url(self) -> str:
        import base64
        return base64.urlsafe_b64encode(self.auth_secret).rstrip(b"=").decode()


def _scalar_to_public_raw(scalar: bytes) -> bytes:
    """Derive the 65-byte uncompressed P-256 public key from a private scalar."""
    n = int.from_bytes(scalar, "big")
    priv = ec.derive_private_key(n, ec.SECP256R1())
    pub = priv.public_key()
    return pub.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )


def generate() -> PushKeys:
    """Fresh keypair + auth secret."""
    priv = ec.generate_private_key(ec.SECP256R1())
    scalar = priv.private_numbers().private_value.to_bytes(32, "big")
    return PushKeys(
        private_scalar=scalar,
        public_raw=_scalar_to_public_raw(scalar),
        auth_secret=os.urandom(16),
    )


def load(jid: str) -> PushKeys | None:
    """Read the keypair + auth secret stored under `jid`, or None if absent."""
    priv_hex = secrets.get(jid, secrets.PURPOSE_UP_PRIVATE_KEY)
    auth_hex = secrets.get(jid, secrets.PURPOSE_UP_AUTH_SECRET)
    if not priv_hex or not auth_hex:
        return None
    try:
        scalar = bytes.fromhex(priv_hex)
        auth = bytes.fromhex(auth_hex)
    except ValueError:
        log.warning("stored push keys for %s are corrupt", jid)
        return None
    if len(scalar) != 32 or len(auth) != 16:
        log.warning("stored push keys for %s have wrong length", jid)
        return None
    return PushKeys(
        private_scalar=scalar,
        public_raw=_scalar_to_public_raw(scalar),
        auth_secret=auth,
    )


def store(jid: str, keys: PushKeys) -> bool:
    ok1 = secrets.set(jid, secrets.PURPOSE_UP_PRIVATE_KEY, keys.private_scalar.hex())
    ok2 = secrets.set(jid, secrets.PURPOSE_UP_AUTH_SECRET, keys.auth_secret.hex())
    return ok1 and ok2


def load_or_generate(jid: str) -> PushKeys:
    """Idempotent: return the stored keypair, or generate + persist a new one."""
    existing = load(jid)
    if existing is not None:
        return existing
    fresh = generate()
    if not store(jid, fresh):
        log.warning("could not persist freshly-generated push keys for %s", jid)
    return fresh
