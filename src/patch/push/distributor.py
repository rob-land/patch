"""Discovery and registration with a UnifiedPush distributor.

The distributor exposes `org.unifiedpush.Distributor1` on the session bus.
We discover any active distributors via NameHasOwner / well-known UP names,
then call `Register(connector, token, description)` to ask for an endpoint.

Per the UP spec, the response is asynchronous: the distributor calls
`NewEndpoint` on *our* Connector1 (see `connector.py`) with the new
endpoint URL once registration succeeds.

Phase 2 keeps this minimal:

  - At account-ready time, ping the cached `push-distributor` name from
    GSettings. If that name owns the bus, call Register and wait for
    NewEndpoint.
  - If no distributor is known, scan the bus for any well-known UP
    distributor name (`org.unifiedpush.Distributor.*`) and pick the
    first one found. Cache its name in GSettings.

Manual selection (preferences UI) and multi-distributor support are
later work.
"""

from __future__ import annotations

import logging

from gi.repository import Gio, GLib, GObject

from patch import APP_ID
from patch.push.connector import CONNECTOR_OBJECT_PATH, DEFAULT_TOKEN

log = logging.getLogger(__name__)


DISTRIBUTOR_IFACE = "org.unifiedpush.Distributor1"
DISTRIBUTOR_PATH  = "/org/unifiedpush/Distributor"

# Well-known D-Bus name prefix for UP distributors. We match by prefix when
# scanning the bus; a single host can have multiple distributors running but
# only one should be configured as primary.
DISTRIBUTOR_NAME_PREFIX = "org.unifiedpush.Distributor."


class PushDistributor(GObject.Object):
    __gtype_name__ = "PatchPushDistributor"

    def __init__(self):
        super().__init__()
        self._settings = Gio.Settings.new(APP_ID)
        self._connection: Gio.DBusConnection | None = None

    def _bus(self) -> Gio.DBusConnection | None:
        if self._connection is None:
            try:
                self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            except GLib.Error as exc:
                log.warning("session bus unavailable: %s", exc.message)
        return self._connection

    # -- discovery --------------------------------------------------------

    def find_distributor(self) -> str | None:
        """Return the bus name of an available UP distributor, or None.

        Uses the cached `push-distributor` GSettings value first; falls
        back to a bus scan for `org.unifiedpush.Distributor.*` names.
        """
        cached = self._settings.get_string("push-distributor")
        if cached and self._has_owner(cached):
            return cached

        bus = self._bus()
        if bus is None:
            return None
        try:
            reply = bus.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "ListActivatableNames",
                None,
                GLib.VariantType.new("(as)"),
                Gio.DBusCallFlags.NONE, -1, None)
        except GLib.Error as exc:
            log.warning("ListActivatableNames failed: %s", exc.message)
            return None
        (names,) = reply.unpack()
        for name in names:
            if name.startswith(DISTRIBUTOR_NAME_PREFIX):
                log.info("found UP distributor %s", name)
                self._settings.set_string("push-distributor", name)
                return name
        log.info("no UnifiedPush distributor found on the session bus")
        return None

    def _has_owner(self, name: str) -> bool:
        bus = self._bus()
        if bus is None:
            return False
        try:
            reply = bus.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                GLib.Variant("(s)", (name,)),
                GLib.VariantType.new("(b)"),
                Gio.DBusCallFlags.NONE, -1, None)
        except GLib.Error:
            return False
        return reply.unpack()[0]

    # -- registration -----------------------------------------------------

    def register(self) -> bool:
        """Call Register on the active distributor.

        Returns True if the call was dispatched (the actual endpoint URL
        arrives later via Connector1.NewEndpoint). Returns False if no
        distributor is available or the call errored.
        """
        name = self.find_distributor()
        if name is None:
            return False
        bus = self._bus()
        if bus is None:
            return False
        try:
            bus.call_sync(
                name,
                DISTRIBUTOR_PATH,
                DISTRIBUTOR_IFACE,
                "Register",
                GLib.Variant("(sss)", (
                    APP_ID,
                    DEFAULT_TOKEN,
                    "Patch — JMP.chat phone client",
                )),
                None,            # any reply
                Gio.DBusCallFlags.NONE, -1, None)
        except GLib.Error as exc:
            log.warning("Register call failed: %s", exc.message)
            return False
        log.info("Register dispatched to %s", name)
        return True

    def unregister(self) -> bool:
        name = self._settings.get_string("push-distributor")
        if not name:
            return True
        bus = self._bus()
        if bus is None:
            return False
        try:
            bus.call_sync(
                name,
                DISTRIBUTOR_PATH,
                DISTRIBUTOR_IFACE,
                "Unregister",
                GLib.Variant("(s)", (DEFAULT_TOKEN,)),
                None,
                Gio.DBusCallFlags.NONE, -1, None)
        except GLib.Error as exc:
            log.warning("Unregister call failed: %s", exc.message)
            return False
        return True
