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


def fetch_turn_uri(xmpp_client, server_jid: str, callback) -> None:
    """Send the disco IQ, call callback(uri | None) when the result arrives."""
    iq = Iq(typ="get", to=server_jid)
    iq.addChild("services", namespace=NS_EXTSERVICE,
                attrs={"type": "turn"})
    nbx = xmpp_client._client      # noqa: SLF001 — internal access ok
    if nbx is None:
        callback(None)
        return

    def _on_response(response):
        try:
            uri = _extract_first_turn(response)
        except Exception as exc:  # noqa: BLE001
            log.warning("turn disco parse failed: %s", exc)
            uri = None
        callback(uri)

    try:
        nbx.SendAndCallForResponse(iq, _on_response)
    except Exception as exc:  # noqa: BLE001
        log.warning("turn disco send failed: %s", exc)
        callback(None)


def _extract_first_turn(response) -> str | None:
    services = response.getTag("services", namespace=NS_EXTSERVICE)
    if services is None:
        # XEP-0215 v1 (urn:xmpp:extdisco:1) is the older spelling. Try it.
        services = response.getTag("services", namespace="urn:xmpp:extdisco:1")
    if services is None:
        return None
    for svc in services.getTags("service"):
        if svc.getAttr("type") not in ("turn", "turns"):
            continue
        host = svc.getAttr("host")
        port = svc.getAttr("port") or "3478"
        username = svc.getAttr("username") or ""
        password = svc.getAttr("password") or ""
        transport = svc.getAttr("transport") or "udp"
        if not host:
            continue
        scheme = "turns" if svc.getAttr("type") == "turns" else "turn"
        creds = ""
        if username:
            creds = quote(username, safe="") + ":" + quote(password, safe="") + "@"
        # webrtcbin's turn-server URI is `turn://[user:pass@]host:port`
        # — the transport is appended as ?transport=tcp if needed.
        uri = f"{scheme}://{creds}{host}:{port}"
        if transport.lower() != "udp":
            uri += f"?transport={transport.lower()}"
        return uri
    return None
