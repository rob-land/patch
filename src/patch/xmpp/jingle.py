"""XEP-0166 Jingle stanza builders + parsers.

Scope: the subset needed for one-to-one audio calls with cheogram/JMP:
- XEP-0167 RTP audio descriptions (opus only)
- XEP-0176 ICE-UDP transports with trickle candidates
- XEP-0320 DTLS-SRTP fingerprints

Everything is pure Node-level (nbxmpp simplexml) so the rest of the
app can build/parse Jingle stanzas without pulling in GStreamer or
libnice. The actual media plumbing lives in audio.py.
"""

from __future__ import annotations

import re
from typing import Iterable

from nbxmpp.protocol import Iq

NS_JINGLE       = "urn:xmpp:jingle:1"
NS_RTP          = "urn:xmpp:jingle:apps:rtp:1"
NS_RTP_INFO     = "urn:xmpp:jingle:apps:rtp:info:1"
NS_RTP_HDREXT   = "urn:xmpp:jingle:apps:rtp:rtp-hdrext:0"
NS_ICE_UDP      = "urn:xmpp:jingle:transports:ice-udp:1"
NS_DTLS         = "urn:xmpp:jingle:apps:dtls:0"


# ── builders ────────────────────────────────────────────────────────────

def session_initiate(
    *, to_jid: str, initiator: str, sid: str,
    payload_types: list[dict],
    ice_ufrag: str, ice_pwd: str,
    dtls_fingerprint: str, dtls_hash: str = "sha-256",
    dtls_setup: str = "actpass",
    content_name: str = "audio",
) -> Iq:
    iq = Iq(typ="set", to=to_jid)
    jingle = iq.addChild("jingle", namespace=NS_JINGLE, attrs={
        "action": "session-initiate", "sid": sid, "initiator": initiator,
    })
    _add_audio_content(jingle, payload_types,
                       ice_ufrag, ice_pwd,
                       dtls_fingerprint, dtls_hash, dtls_setup,
                       content_name, creator="initiator")
    return iq


def session_accept(
    *, to_jid: str, responder: str, sid: str,
    payload_types: list[dict],
    ice_ufrag: str, ice_pwd: str,
    dtls_fingerprint: str, dtls_hash: str = "sha-256",
    dtls_setup: str = "active",
    content_name: str = "audio",
) -> Iq:
    iq = Iq(typ="set", to=to_jid)
    jingle = iq.addChild("jingle", namespace=NS_JINGLE, attrs={
        "action": "session-accept", "sid": sid, "responder": responder,
    })
    _add_audio_content(jingle, payload_types,
                       ice_ufrag, ice_pwd,
                       dtls_fingerprint, dtls_hash, dtls_setup,
                       content_name, creator="initiator")
    return iq


def session_terminate(*, to_jid: str, sid: str, reason: str = "success") -> Iq:
    iq = Iq(typ="set", to=to_jid)
    jingle = iq.addChild("jingle", namespace=NS_JINGLE, attrs={
        "action": "session-terminate", "sid": sid,
    })
    reason_el = jingle.addChild("reason")
    reason_el.addChild(reason)
    return iq


def transport_info(
    *, to_jid: str, sid: str,
    candidates: list[dict],
    content_name: str = "audio",
) -> Iq:
    """Trickle-ICE candidate update.

    Each `candidates` entry is a dict like:
        {component, foundation, ip, port, priority, protocol, type,
         generation, network, id, raddr (opt), rport (opt), tcptype (opt)}
    """
    iq = Iq(typ="set", to=to_jid)
    jingle = iq.addChild("jingle", namespace=NS_JINGLE, attrs={
        "action": "transport-info", "sid": sid,
    })
    content = jingle.addChild("content", attrs={
        "creator": "initiator", "name": content_name,
    })
    transport = content.addChild(
        "transport", namespace=NS_ICE_UDP, attrs={})
    for cand in candidates:
        transport.addChild("candidate", attrs={k: str(v) for k, v in cand.items()})
    return iq


# ── audio content composer (used by initiate + accept) ──────────────────

def _add_audio_content(jingle, payload_types,
                       ice_ufrag, ice_pwd,
                       dtls_fp, dtls_hash, dtls_setup,
                       content_name, creator):
    content = jingle.addChild("content", attrs={
        "creator": creator, "name": content_name, "senders": "both",
    })
    desc = content.addChild("description", namespace=NS_RTP,
                            attrs={"media": "audio"})
    for pt in payload_types:
        attrs = {k: str(v) for k, v in pt.items() if k != "parameters"}
        pt_el = desc.addChild("payload-type", attrs=attrs)
        for pname, pval in (pt.get("parameters") or {}).items():
            pt_el.addChild("parameter", attrs={"name": pname, "value": str(pval)})

    transport = content.addChild("transport", namespace=NS_ICE_UDP,
                                 attrs={"ufrag": ice_ufrag, "pwd": ice_pwd})
    fp = transport.addChild("fingerprint", namespace=NS_DTLS,
                            attrs={"hash": dtls_hash, "setup": dtls_setup})
    fp.addData(dtls_fp)


# ── parsers (counterparts to the above) ─────────────────────────────────


def parse_jingle(iq) -> dict | None:
    """Top-level Jingle-iq parse. Returns a dict the caller can switch
    on by `action`, or None if not a Jingle iq."""
    jingle = iq.getTag("jingle", namespace=NS_JINGLE)
    if jingle is None:
        return None
    return {
        "action":     jingle.getAttr("action"),
        "sid":        jingle.getAttr("sid"),
        "initiator":  jingle.getAttr("initiator"),
        "responder":  jingle.getAttr("responder"),
        "from":       iq.getAttr("from"),
        "to":         iq.getAttr("to"),
        "id":         iq.getAttr("id"),
        "contents":   _parse_contents(jingle),
        "reason":     _parse_reason(jingle),
    }


def _parse_contents(jingle) -> list[dict]:
    out = []
    for content in jingle.getTags("content"):
        c = {
            "name":      content.getAttr("name"),
            "creator":   content.getAttr("creator"),
            "senders":   content.getAttr("senders"),
            "description": None,
            "transport":   None,
        }
        desc = content.getTag("description", namespace=NS_RTP)
        if desc is not None:
            c["description"] = {
                "media": desc.getAttr("media"),
                "payload_types": [_parse_payload_type(p)
                                  for p in desc.getTags("payload-type")],
            }
        transport = content.getTag("transport", namespace=NS_ICE_UDP)
        if transport is not None:
            c["transport"] = _parse_transport(transport)
        out.append(c)
    return out


def _parse_payload_type(pt) -> dict:
    d = dict(pt.getAttrs() or {})
    params = {}
    for p in pt.getTags("parameter"):
        params[p.getAttr("name")] = p.getAttr("value")
    if params:
        d["parameters"] = params
    return d


def _parse_transport(transport) -> dict:
    fp = transport.getTag("fingerprint", namespace=NS_DTLS)
    out = {
        "ufrag": transport.getAttr("ufrag"),
        "pwd":   transport.getAttr("pwd"),
        "fingerprint": None,
        "candidates": [dict(c.getAttrs() or {})
                       for c in transport.getTags("candidate")],
    }
    if fp is not None:
        out["fingerprint"] = {
            "hash":  fp.getAttr("hash"),
            "setup": fp.getAttr("setup"),
            "value": fp.getData() or "",
        }
    return out


def _parse_reason(jingle) -> str | None:
    r = jingle.getTag("reason")
    if r is None:
        return None
    for child in (r.getChildren() or []):
        return child.getName()
    return None


# ── SDP candidate string ↔ Jingle candidate dict ────────────────────────

_CAND_RE = re.compile(
    r"^candidate:(\S+)\s+(\d+)\s+(\S+)\s+(\d+)\s+(\S+)\s+(\d+)\s+typ\s+(\S+)"
    r"(?:\s+raddr\s+(\S+)\s+rport\s+(\d+))?"
)


def sdp_candidate_to_jingle(line: str, candidate_id: str = "") -> dict | None:
    """Parse an SDP a=candidate: line (or full attribute) into a Jingle
    candidate dict suitable for transport_info()."""
    if line.startswith("a="):
        line = line[2:]
    m = _CAND_RE.match(line.strip())
    if not m:
        return None
    foundation, component, protocol, priority, ip, port, ctype, raddr, rport = m.groups()
    out = {
        "foundation": foundation,
        "component":  component,
        "protocol":   protocol.lower(),
        "priority":   priority,
        "ip":         ip,
        "port":       port,
        "type":       ctype,
        "generation": "0",
        "id":         candidate_id or foundation,
        "network":    "0",
    }
    if raddr:
        out["rel-addr"] = raddr
        out["rel-port"] = rport
    return out


def jingle_candidate_to_sdp(cand: dict) -> str:
    """Inverse: build an `a=candidate:...` SDP line from a Jingle candidate."""
    parts = [
        f"candidate:{cand['foundation']}",
        cand["component"],
        cand["protocol"].upper(),
        cand["priority"],
        cand["ip"],
        cand["port"],
        "typ", cand["type"],
    ]
    if cand.get("rel-addr"):
        parts += ["raddr", cand["rel-addr"], "rport", cand.get("rel-port", "0")]
    return " ".join(parts)
