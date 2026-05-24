"""XEP-0215 External Service Discovery — fetch ephemeral TURN credentials
from the home server's mod_turn_external (or equivalent).

The server hands us back a short-lived HMAC username + password plus
host/port/transport. We assemble it into a `turn://user:pass@host:port`
URI for webrtcbin.

Used once per Jingle session start; the credentials are short-lived
(coturn default is 1h) but a single session well outlives that — we
re-query per session to keep things fresh.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from nbxmpp.protocol import Iq

log = logging.getLogger(__name__)

NS_EXTSERVICE = "urn:xmpp:extdisco:2"


def fetch_turn_uris(xmpp_client, server_jid: str, callback) -> None:
    """Send the disco IQ, call callback(list[str]) with all advertised
    TURN URIs (UDP first, TCP next, TURNS last) — empty list on failure
    or no services. Feeding webrtcbin multiple TURN servers (via
    add-turn-server) lets ICE relay-probe both UDP and TCP transports
    so a network that blocks UDP/3478 still gets through on TCP."""
    iq = Iq(typ="get", to=server_jid)
    iq.addChild("services", namespace=NS_EXTSERVICE,
                attrs={"type": "turn"})
    nbx = xmpp_client._client      # noqa: SLF001 — internal access ok
    if nbx is None:
        callback([])
        return

    # nbxmpp dispatches iq-response callbacks as
    #   func(client, response_stanza, **user_data)
    # — earlier this module used a one-arg signature which raised
    # TypeError inside nbxmpp's dispatcher (caught + swallowed by
    # its except-Exception clause), so the callback never reached
    # _on_turn_resolved and the audio engine never started.
    def _on_response(_client, response, **_kw):
        if response is None:
            log.warning("turn disco timed out")
            callback([])
            return
        try:
            uris = _extract_turn_uris(response)
        except Exception as exc:  # noqa: BLE001
            log.warning("turn disco parse failed: %s", exc)
            uris = []
        callback(uris)

    try:
        nbx.SendAndCallForResponse(iq, _on_response)
    except Exception as exc:  # noqa: BLE001
        log.warning("turn disco send failed: %s", exc)
        callback([])


def _extract_turn_uris(response) -> list[str]:
    services = response.getTag("services", namespace=NS_EXTSERVICE)
    if services is None:
        # XEP-0215 v1 (urn:xmpp:extdisco:1) is the older spelling. Try it.
        services = response.getTag("services", namespace="urn:xmpp:extdisco:1")
    if services is None:
        return []
    # Buckets so we can order UDP → TCP → TURNS regardless of disco order.
    by_transport: dict[str, list[str]] = {"udp": [], "tcp": [], "turns": []}
    for svc in services.getTags("service"):
        if svc.getAttr("type") not in ("turn", "turns"):
            continue
        host = svc.getAttr("host")
        port = svc.getAttr("port") or "3478"
        username = svc.getAttr("username") or ""
        password = svc.getAttr("password") or ""
        transport = (svc.getAttr("transport") or "udp").lower()
        if not host:
            continue
        scheme = "turns" if svc.getAttr("type") == "turns" else "turn"
        creds = ""
        if username:
            creds = quote(username, safe="") + ":" + quote(password, safe="") + "@"
        # webrtcbin's turn-server URI is `turn://[user:pass@]host:port`
        # — the transport is appended as ?transport=tcp if needed.
        uri = f"{scheme}://{creds}{host}:{port}"
        if transport != "udp":
            uri += f"?transport={transport}"
        bucket = "turns" if scheme == "turns" else transport
        by_transport.setdefault(bucket, []).append(uri)
    return by_transport["udp"] + by_transport["tcp"] + by_transport["turns"]
