"""libsecret-backed credential storage.

A thin GObject-introspection wrapper over libsecret. Keyed by `(jid, purpose)`
so the same code path stores the XMPP password, the UnifiedPush P-256 private
key, and the auth secret in three separate items under the same schema.

All _sync calls use a 5-second GCancellable timeout to prevent indefinite
blocking when gnome-keyring hasn't unlocked yet (e.g. early in session startup
on Phosh).
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from gi.repository import Gio, GLib, Secret

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

_TIMEOUT = 5


def _cancellable_with_timeout():
    """Return a GCancellable that auto-cancels after _TIMEOUT seconds."""
    cancel = Gio.Cancellable()
    timer = threading.Timer(_TIMEOUT, cancel.cancel)
    timer.daemon = True
    timer.start()
    return cancel, timer


def _attrs(jid: str, purpose: str) -> dict:
    return {"jid": jid, "purpose": purpose}


def _label(jid: str, purpose: str) -> str:
    return f"Patch — {purpose} for {jid}"


def get(jid: str, purpose: str) -> Optional[str]:
    """Return the stored secret, or None if absent.

    Empty-string returns None too — we never want to use an empty value
    where a real one was expected.
    """
    cancel, timer = _cancellable_with_timeout()
    try:
        value = Secret.password_lookup_sync(_SCHEMA, _attrs(jid, purpose), cancel)
    except GLib.Error as exc:
        log.warning("libsecret lookup failed for %s/%s: %s", jid, purpose, exc.message)
        return None
    finally:
        timer.cancel()
    return value or None


def set(jid: str, purpose: str, secret: str) -> bool:
    """Store (or replace) a secret. Returns True on success."""
    cancel, timer = _cancellable_with_timeout()
    try:
        return Secret.password_store_sync(
            _SCHEMA,
            _attrs(jid, purpose),
            Secret.COLLECTION_DEFAULT,
            _label(jid, purpose),
            secret,
            cancel,
        )
    except GLib.Error as exc:
        log.warning("libsecret store failed for %s/%s: %s", jid, purpose, exc.message)
        return False
    finally:
        timer.cancel()


def clear(jid: str, purpose: str) -> bool:
    cancel, timer = _cancellable_with_timeout()
    try:
        return Secret.password_clear_sync(_SCHEMA, _attrs(jid, purpose), cancel)
    except GLib.Error as exc:
        log.warning("libsecret clear failed for %s/%s: %s", jid, purpose, exc.message)
        return False
    finally:
        timer.cancel()
