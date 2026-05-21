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

        Checks both ListNames (already-running) and ListActivatableNames
        (autostart-on-demand). KUnifiedPush comes up as a systemd user
        service and only appears in ListNames once running.
        """
        cached = self._settings.get_string("push-distributor")
        if cached and self._has_owner(cached):
            return cached

        bus = self._bus()
        if bus is None:
            return None

        candidates: set[str] = set()
        for method in ("ListNames", "ListActivatableNames"):
            try:
                reply = bus.call_sync(
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    method,
                    None,
                    GLib.VariantType.new("(as)"),
                    Gio.DBusCallFlags.NONE, -1, None)
            except GLib.Error as exc:
                log.debug("%s failed: %s", method, exc.message)
                continue
            (names,) = reply.unpack()
            candidates.update(
                n for n in names if n.startswith(DISTRIBUTOR_NAME_PREFIX))

        if not candidates:
            log.info("no UnifiedPush distributor found on the session bus")
            return None
        # Stable pick — sort so the same distributor is chosen across runs.
        name = sorted(candidates)[0]
        log.info("found UP distributor %s", name)
        self._settings.set_string("push-distributor", name)
        return name

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

        Distributor1.Register is a synchronous call: it returns a
        `(result, reason)` string pair where result is one of
        "REGISTRATION_SUCCEEDED", "REGISTRATION_FAILED", or
        "INTERNAL_ERROR". On success the distributor follows up
        asynchronously with a `NewEndpoint` call on our Connector1 (see
        connector.py) carrying the actual endpoint URL.

        Returns True iff the distributor accepted the registration.
        """
        name = self.find_distributor()
        if name is None:
            return False
        bus = self._bus()
        if bus is None:
            return False
        try:
            reply = bus.call_sync(
                name,
                DISTRIBUTOR_PATH,
                DISTRIBUTOR_IFACE,
                "Register",
                GLib.Variant("(sss)", (
                    APP_ID,
                    DEFAULT_TOKEN,
                    "Patch — JMP.chat phone client",
                )),
                GLib.VariantType.new("(ss)"),
                Gio.DBusCallFlags.NONE, -1, None)
        except GLib.Error as exc:
            log.warning("Register call failed: %s", exc.message)
            return False
        result, reason = reply.unpack()
        if result == "REGISTRATION_SUCCEEDED":
            log.info("Register accepted by %s", name)
            return True
        log.warning("Register refused by %s: %s (%s)", name, result, reason)
        return False

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
