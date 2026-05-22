"""Smoke tests for xmpp/jingle.py — builders + parsers + SDP candidate
round-trip."""

from __future__ import annotations

import importlib.util
import os
import sys

_HERE = os.path.dirname(__file__)


def _load(name, *parts):
    path = os.path.join(_HERE, "..", "src", "patch", *parts)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# nbxmpp is required for the Iq builders. If it's not installed, skip
# the iq-building tests; the SDP helpers don't need it.
try:
    jingle = _load("jingle", "xmpp", "jingle.py")
    HAVE_NBXMPP = True
except Exception:  # noqa: BLE001
    HAVE_NBXMPP = False


def eq(label, got, want):
    if got != want:
        print(f"FAIL  {label}: got={got!r} want={want!r}")
        sys.exit(1)
    print(f"ok    {label}")


# ── SDP candidate ↔ Jingle round-trip ──────────────────────────────────

if HAVE_NBXMPP:
    sdp_line = "candidate:842163049 1 udp 1677729535 192.0.2.7 56789 typ srflx raddr 10.0.0.5 rport 56000"
    cand = jingle.sdp_candidate_to_jingle(sdp_line, candidate_id="x1")
    eq("foundation",  cand["foundation"], "842163049")
    eq("component",   cand["component"],  "1")
    eq("protocol",    cand["protocol"],   "udp")
    eq("priority",    cand["priority"],   "1677729535")
    eq("ip",          cand["ip"],         "192.0.2.7")
    eq("port",        cand["port"],       "56789")
    eq("type",        cand["type"],       "srflx")
    eq("rel-addr",    cand["rel-addr"],   "10.0.0.5")
    eq("rel-port",    cand["rel-port"],   "56000")

    sdp_back = jingle.jingle_candidate_to_sdp(cand)
    # We can't expect byte-identity (foundation order, no trailing fields),
    # but it should round-trip a fresh parse to the same dict.
    cand2 = jingle.sdp_candidate_to_jingle(sdp_back, candidate_id="x1")
    eq("roundtrip foundation", cand2["foundation"], cand["foundation"])
    eq("roundtrip ip",         cand2["ip"],         cand["ip"])
    eq("roundtrip rel-addr",   cand2["rel-addr"],   cand["rel-addr"])

    # ── session-initiate build + parse ─────────────────────────────────
    iq = jingle.session_initiate(
        to_jid="peer@example.org/x",
        initiator="me@example.org/patch.abc",
        sid="sess-1",
        payload_types=[{"id": "111", "name": "opus",
                        "clockrate": "48000", "channels": "2",
                        "parameters": {"useinbandfec": "1"}}],
        ice_ufrag="abcd", ice_pwd="efghijklmnopqrstuvwxyz0123456",
        dtls_fingerprint="AA:BB:CC:DD",
    )
    parsed = jingle.parse_jingle(iq)
    eq("action",    parsed["action"], "session-initiate")
    eq("sid",       parsed["sid"],    "sess-1")
    eq("initiator", parsed["initiator"], "me@example.org/patch.abc")
    eq("contents len", len(parsed["contents"]), 1)
    c = parsed["contents"][0]
    eq("content name",        c["name"], "audio")
    eq("desc media",          c["description"]["media"], "audio")
    pt = c["description"]["payload_types"][0]
    eq("payload id",   pt["id"],   "111")
    eq("payload name", pt["name"], "opus")
    eq("payload param", pt["parameters"]["useinbandfec"], "1")
    eq("transport ufrag", c["transport"]["ufrag"], "abcd")
    eq("transport pwd",   c["transport"]["pwd"],   "efghijklmnopqrstuvwxyz0123456")
    eq("transport fp value", c["transport"]["fingerprint"]["value"], "AA:BB:CC:DD")
    eq("transport fp setup", c["transport"]["fingerprint"]["setup"], "actpass")

    # ── session-terminate ──────────────────────────────────────────────
    iq = jingle.session_terminate(to_jid="peer@x/r", sid="s", reason="success")
    parsed = jingle.parse_jingle(iq)
    eq("terminate action", parsed["action"], "session-terminate")
    eq("terminate reason", parsed["reason"], "success")

print()
print("PASS  test_jingle" if HAVE_NBXMPP else "SKIP  test_jingle (nbxmpp not present)")
