"""Verify push.decrypt() against the RFC 8291 §5 test vector.

Same encryption flow as the prosody-side mod_cloud_notify_unifiedpush, so
running both halves against the canonical vector means any drift between
server and client surfaces here rather than as a missed-push silent
failure in the field.
"""

from __future__ import annotations

import base64
import importlib.util
import os
import sys

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "decrypt",
    os.path.join(_HERE, "..", "src", "patch", "push", "decrypt.py"))
decrypt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(decrypt)


def b64url(s):
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + "=" * pad)


# RFC 8291 §5 / Appendix A
PLAINTEXT     = b64url("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24")
UA_PRIVATE    = b64url("q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94")
AUTH_SECRET   = b64url("BTBZMqHH6r4Tts7J_aSIgg")
# §5 body — three 64-char lines concatenated.
BODY = b64url(
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
    "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
    "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)


def eq(label, got, want):
    if got != want:
        print(f"FAIL  {label}: got={got!r} want={want!r}")
        sys.exit(1)
    print(f"ok    {label}")


got = decrypt.decrypt(BODY, UA_PRIVATE, AUTH_SECRET)
eq("RFC 8291 §5 plaintext roundtrip", got, PLAINTEXT)

print()
print("PASS  test_decrypt")
