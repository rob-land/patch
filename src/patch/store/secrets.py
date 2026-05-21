"""libsecret-backed credential storage.

A thin GObject-introspection wrapper over libsecret. Keyed by `(jid, purpose)`
so the same code path stores the XMPP password, the UnifiedPush P-256 private
key, and the auth secret in three separate items under the same schema.

The libsecret API is callback-based async, but for the credentials path the
caller is always blocking on the user filling in a dialog or reconnecting an
account — there's no benefit to async here. We use the synchronous methods
and accept that they may block for tens of milliseconds while the keyring
unlocks.
"""

from __future__ import annotations

import logging
from typing import Optional

from gi.repository import GLib, Secret

log = logging.getLogger(__name__)


SCHEMA_NAME = "land.rob.patch.Account"


# Per STYLE_GUIDE and Secret.SchemaAttributeType convention. NONE means
# the schema is application-defined, not a system-wide one like NetworkManager.
_SCHEMA = Secret.Schema.new(
    SCHEMA_NAME,
    Secret.SchemaFlags.NONE,
    {
        "jid":     Secret.SchemaAttributeType.STRING,
        "purpose": Secret.SchemaAttributeType.STRING,
    },
)


# Recognized purposes — keep these stable; existing items live under them.
PURPOSE_PASSWORD = "password"
PURPOSE_UP_PRIVATE_KEY = "up_private_key"
PURPOSE_UP_AUTH_SECRET = "up_auth_secret"


def _attrs(jid: str, purpose: str) -> dict:
    return {"jid": jid, "purpose": purpose}


def _label(jid: str, purpose: str) -> str:
    return f"Patch — {purpose} for {jid}"


def get(jid: str, purpose: str) -> Optional[str]:
    """Return the stored secret, or None if absent.

    Empty-string returns None too — we never want to use an empty value
    where a real one was expected.
    """
    try:
        value = Secret.password_lookup_sync(_SCHEMA, _attrs(jid, purpose), None)
    except GLib.Error as exc:
        log.warning("libsecret lookup failed for %s/%s: %s", jid, purpose, exc.message)
        return None
    return value or None


def set(jid: str, purpose: str, secret: str) -> bool:
    """Store (or replace) a secret. Returns True on success."""
    try:
        return Secret.password_store_sync(
            _SCHEMA,
            _attrs(jid, purpose),
            Secret.COLLECTION_DEFAULT,
            _label(jid, purpose),
            secret,
            None,
        )
    except GLib.Error as exc:
        log.warning("libsecret store failed for %s/%s: %s", jid, purpose, exc.message)
        return False


def clear(jid: str, purpose: str) -> bool:
    try:
        return Secret.password_clear_sync(_SCHEMA, _attrs(jid, purpose), None)
    except GLib.Error as exc:
        log.warning("libsecret clear failed for %s/%s: %s", jid, purpose, exc.message)
        return False
