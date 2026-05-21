"""XEP-0357 <enable> IQ with our UnifiedPush publish_options data form.

Builds the IQ that registers our endpoint with `mod_cloud_notify_unifiedpush`
on chat.rob.land. The form fields match the FORM_TYPE the module recognises;
the values come from `push.keys.PushKeys` (p256dh + auth) and the
distributor (endpoint URL).

Server-side parsing lives in `publish_options.lua`.
"""

from __future__ import annotations

from nbxmpp.protocol import Iq

UP_FORM_TYPE = "https://rob.land/protocol/unifiedpush#0"
PUSH_NS = "urn:xmpp:push:0"
DATA_NS = "jabber:x:data"


def build_enable_iq(
    *,
    to_jid: str,
    push_jid: str,
    node: str,
    endpoint: str,
    p256dh_b64url: str,
    auth_b64url: str,
) -> Iq:
    """Build an XEP-0357 <enable> IQ for the given JID + endpoint.

    Arguments
    ---------
    to_jid          The user's own bare JID — push enable goes to your home
                    server's account routing.
    push_jid        Required by XEP-0357 even though we never contact it.
                    Convention: pass the bare JID; the module short-circuits
                    once it sees the UP publish_options form.
    node            Pubsub node — opaque identifier for this registration.
                    Convention: `up-<distributor-short>` so multi-device
                    setups don't clobber each other.
    endpoint        UnifiedPush endpoint URL from the distributor.
    p256dh_b64url   65-byte uncompressed P-256 public key, base64url.
    auth_b64url     16-byte client auth secret, base64url.
    """
    iq = Iq(typ="set", to=to_jid)
    enable = iq.addChild(
        "enable",
        namespace=PUSH_NS,
        attrs={"jid": push_jid, "node": node},
    )
    form = enable.addChild(
        "x", namespace=DATA_NS, attrs={"type": "submit"})

    def add_field(var: str, value: str, typ: str | None = None):
        attrs = {"var": var}
        if typ:
            attrs["type"] = typ
        field = form.addChild("field", attrs=attrs)
        field.addChild("value").addData(value)

    add_field("FORM_TYPE", UP_FORM_TYPE, typ="hidden")
    add_field("endpoint",  endpoint)
    add_field("p256dh",    p256dh_b64url)
    add_field("auth",      auth_b64url)

    return iq
