"""RFC 8291 Web Push decryption.

Inverse of the encryption implemented in the prosody-side
`mod_cloud_notify_unifiedpush`. Reads the `aes128gcm` framed body
(salt + rs + idlen + keyid + ciphertext) per RFC 8188 §2.1, derives the
content encryption key + nonce via the RFC 8291 §3.4 KDF, and AES-GCM
decrypts.

We exercise the inverse in `tests/test_decrypt.py` against the RFC 8291
§5 test vector so any drift between the server's encrypt path and our
decrypt path surfaces in CI rather than as a missed-push silent failure.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes, hmac, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _hkdf_extract_expand(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    """One-shot HKDF-SHA256 with a single-block T(1) expand (length <= 32)."""
    h = hmac.HMAC(salt, hashes.SHA256())
    h.update(ikm)
    prk = h.finalize()
    h = hmac.HMAC(prk, hashes.SHA256())
    h.update(info + b"\x01")
    return h.finalize()[:length]


def _derive_cek_nonce(ecdh_secret: bytes, auth_secret: bytes, salt: bytes,
                     ua_public_raw: bytes, as_public_raw: bytes) -> tuple[bytes, bytes]:
    # IKM = HKDF(salt=auth_secret, IKM=ecdh, info="WebPush: info\0" + ua_pub + as_pub)
    key_info = b"WebPush: info\x00" + ua_public_raw + as_public_raw
    ikm = _hkdf_extract_expand(auth_secret, ecdh_secret, key_info, 32)
    # CEK / NONCE use the per-push salt as the HKDF salt and PRK chain
    # rooted at IKM. We need PRK separately to feed two expand() calls,
    # so do the extract step explicitly.
    h = hmac.HMAC(salt, hashes.SHA256())
    h.update(ikm)
    prk = h.finalize()

    def expand(info: bytes, length: int) -> bytes:
        h = hmac.HMAC(prk, hashes.SHA256())
        h.update(info + b"\x01")
        return h.finalize()[:length]

    cek = expand(b"Content-Encoding: aes128gcm\x00", 16)
    nonce = expand(b"Content-Encoding: nonce\x00", 12)
    return cek, nonce


def decrypt(body: bytes, private_scalar: bytes, auth_secret: bytes) -> bytes:
    """Decrypt an aes128gcm push body. Returns the plaintext payload.

    Raises ValueError if the body is malformed; any cryptography exception
    (bad tag, etc.) propagates from `AESGCM.decrypt`.
    """
    if len(body) < 21:
        raise ValueError("body too short")
    salt    = body[:16]
    # rs    = int.from_bytes(body[16:20], "big")  # record size, not needed for decrypt
    idlen   = body[20]
    if len(body) < 21 + idlen:
        raise ValueError("truncated keyid")
    as_pub  = body[21:21 + idlen]
    ct      = body[21 + idlen:]
    if idlen != 65 or as_pub[0:1] != b"\x04":
        raise ValueError("expected 65-byte uncompressed P-256 keyid")

    # Reconstruct the receiver's private key and derive ECDH against the
    # application server's ephemeral public key.
    n = int.from_bytes(private_scalar, "big")
    priv = ec.derive_private_key(n, ec.SECP256R1())
    as_pubkey = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub)
    ecdh_secret = priv.exchange(ec.ECDH(), as_pubkey)

    # We need the receiver's own public key in raw form for the KDF.
    ua_pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )

    cek, nonce = _derive_cek_nonce(
        ecdh_secret, auth_secret, salt, ua_pub, as_pub)

    aes = AESGCM(cek)
    padded = aes.decrypt(nonce, ct, None)

    # RFC 8188 §2: last-record padding is a single 0x02 followed by zeros.
    # Strip trailing zeros, then verify the delimiter, then drop it.
    end = len(padded)
    while end > 0 and padded[end - 1] == 0x00:
        end -= 1
    if end == 0 or padded[end - 1] != 0x02:
        raise ValueError("missing aes128gcm last-record delimiter")
    return padded[:end - 1]
