"""JMP/XMPP account model.

Phase 0: holds the JID, password, and connection state in memory plus
libsecret persistence. The actual XMPP connection is wired up in Phase 1
(messages.py + an xmpp engine module).

The Account is a `GObject` so views can bind to its properties (state,
display name) instead of polling.
"""

from __future__ import annotations

import logging
from typing import Optional

from gi.repository import GLib, GObject, Gio

from patch import APP_ID
from patch.store import secrets

log = logging.getLogger(__name__)


# Connection state machine values. Strings (not enum) so they're trivial
# to read in GSettings dumps, log lines, and accessibility tooling.
STATE_OFFLINE      = "offline"        # no creds configured
STATE_CONNECTING   = "connecting"     # XMPP login in flight
STATE_CONNECTED    = "connected"      # online + ready
STATE_FAILED       = "failed"         # tried, failed; will retry
STATE_DISCONNECTED = "disconnected"   # was connected, lost it


class Account(GObject.Object):
    __gtype_name__ = "PatchAccount"

    # The single active account JID. Empty string == no account configured.
    jid          = GObject.Property(type=str, default="")
    # Optional explicit XMPP host (else derived from JID domain).
    host         = GObject.Property(type=str, default="")
    # Gateway domain for E.164 ↔ JID conversion.
    gateway      = GObject.Property(type=str, default="cheogram.com")
    # Connection state machine (one of STATE_*).
    state        = GObject.Property(type=str, default=STATE_OFFLINE)
    # Last error string for surface display; empty on success.
    last_error   = GObject.Property(type=str, default="")

    def __init__(self):
        super().__init__()
        self._settings = Gio.Settings.new(APP_ID)
        # Load from GSettings on construction. Password isn't a property —
        # we never want it accidentally bound into a widget — it stays in
        # libsecret and is only fetched at connect time.
        self.jid     = self._settings.get_string("account-jid")
        self.host    = self._settings.get_string("xmpp-host")
        self.gateway = self._settings.get_string("gateway-domain")

    # -- credential persistence -------------------------------------------

    def get_password(self) -> Optional[str]:
        if not self.jid:
            return None
        return secrets.get(self.jid, secrets.PURPOSE_PASSWORD)

    def save(self, jid: str, password: str, host: str = "") -> bool:
        """Persist a (jid, password, host) tuple. Replaces any prior credentials."""
        if not jid:
            return False
        old_jid = self.jid
        if old_jid and old_jid != jid:
            # User is switching accounts. Clear out the previous one's
            # password so we don't leave stale secrets behind.
            secrets.clear(old_jid, secrets.PURPOSE_PASSWORD)
        ok = secrets.set(jid, secrets.PURPOSE_PASSWORD, password)
        if not ok:
            log.warning("could not store password in libsecret — keyring unavailable?")
            return False
        self._settings.set_string("account-jid", jid)
        self._settings.set_string("xmpp-host", host or "")
        self.jid  = jid
        self.host = host
        return True

    # -- connection state machine -----------------------------------------

    def set_state(self, state: str, error: str = "") -> None:
        # Toggling state on the main loop so widget bindings update on a
        # consistent thread. Callers from worker threads should funnel
        # through this rather than poking the property directly.
        def _apply():
            if self.state != state:
                self.state = state
            if self.last_error != error:
                self.last_error = error
            return False
        GLib.idle_add(_apply)

    @property
    def is_configured(self) -> bool:
        return bool(self.jid)

    @property
    def display_name(self) -> str:
        if not self.jid:
            return ""
        # Local part of the JID. For JMP gateway accounts this happens to
        # be the user's PSTN number — render it nicely if so, else show
        # the raw JID.
        from patch.numfmt import jid_to_number, format_for_display
        number = jid_to_number(self.jid, self.gateway)
        if number:
            return format_for_display(number)
        return self.jid
