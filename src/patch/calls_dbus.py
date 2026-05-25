"""D-Bus service exposing ``land.rob.patch.Calls1``.

Allows the gnome-calls plugin (``plugin/``) — or any other D-Bus
consumer — to drive Patch's call engine externally. On Phosh this
means incoming JMI proposals surface via the system ringer and
gnome-calls' full-screen call UI; on desktop GNOME the built-in
``PatchCallDialog`` keeps working as before (it checks whether
gnome-calls is active and yields if so).

Published at: bus name ``land.rob.patch``, object path
``/land/rob/patch/calls``, interface ``land.rob.patch.Calls1``.

Methods (plugin → Patch):
  Dial(s number) → s session_id
  Accept(s session_id)
  Reject(s session_id)
  Hangup(s session_id)
  SendDtmf(s session_id, s digit)
  SetHold(s session_id, b hold)
  SetMute(s session_id, b muted)

Signals (Patch → plugin):
  IncomingCall(s session_id, s number, s display_name)
  CallStateChanged(s session_id, s state)
"""

from __future__ import annotations

import logging

from gi.repository import Gio, GLib

from patch import calls as calls_mod
from patch import numfmt

log = logging.getLogger(__name__)

INTERFACE_NAME = "land.rob.patch.Calls1"
OBJECT_PATH    = "/land/rob/patch/calls"

_INTROSPECTION_XML = """
<node>
  <interface name="land.rob.patch.Calls1">
    <method name="Dial">
      <arg direction="in"  type="s" name="number"/>
      <arg direction="out" type="s" name="session_id"/>
    </method>
    <method name="Accept">
      <arg direction="in" type="s" name="session_id"/>
    </method>
    <method name="Reject">
      <arg direction="in" type="s" name="session_id"/>
    </method>
    <method name="Hangup">
      <arg direction="in" type="s" name="session_id"/>
    </method>
    <method name="SendDtmf">
      <arg direction="in" type="s" name="session_id"/>
      <arg direction="in" type="s" name="digit"/>
    </method>
    <method name="SetHold">
      <arg direction="in" type="s" name="session_id"/>
      <arg direction="in" type="b" name="hold"/>
    </method>
    <method name="SetMute">
      <arg direction="in" type="s" name="session_id"/>
      <arg direction="in" type="b" name="muted"/>
    </method>
    <signal name="IncomingCall">
      <arg type="s" name="session_id"/>
      <arg type="s" name="number"/>
      <arg type="s" name="display_name"/>
    </signal>
    <signal name="CallStateChanged">
      <arg type="s" name="session_id"/>
      <arg type="s" name="state"/>
    </signal>
  </interface>
</node>
"""


class CallsDBusService:
    """Publishes the Calls1 D-Bus interface on the session bus.

    Bridges CallManager signals → D-Bus signals, and inbound D-Bus
    method calls → CallManager actions. Instantiated once from main.py
    and kept alive for the process lifetime.
    """

    def __init__(self, call_manager: calls_mod.CallManager, account):
        self._manager = call_manager
        self._account = account
        self._connection: Gio.DBusConnection | None = None
        self._registration_id: int = 0

        self._manager.connect("call-started", self._on_call_started)
        self._manager.connect("call-changed", self._on_call_changed)
        self._manager.connect("call-ended",   self._on_call_ended)

    def publish(self) -> None:
        """Register the object on the session bus.

        Safe to call early — the bus name (land.rob.patch) is already
        owned by the Adw.Application. We just register an additional
        object path on the existing connection.
        """
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            log.warning("Calls1: could not get session bus: %s", exc.message)
            return
        self._connection = bus
        node_info = Gio.DBusNodeInfo.new_for_xml(_INTROSPECTION_XML)
        iface_info = node_info.lookup_interface(INTERFACE_NAME)
        try:
            self._registration_id = bus.register_object(
                OBJECT_PATH, iface_info,
                self._on_method_call, None, None)
        except GLib.Error as exc:
            log.warning("Calls1: register_object failed: %s", exc.message)
            return
        log.info("Calls1 published at %s", OBJECT_PATH)

    # -- D-Bus method handler --------------------------------------------

    def _on_method_call(self, _connection, _sender, _path, _iface,
                        method_name, parameters, invocation):
        if method_name == "Dial":
            number = parameters.unpack()[0]
            jid = numfmt.number_to_jid(number, self._account.gateway)
            sess = self._manager.start_outgoing(jid)
            if sess is None:
                invocation.return_error_literal(
                    Gio.dbus_error_quark(), Gio.DBusError.FAILED,
                    "Could not start call")
                return
            invocation.return_value(GLib.Variant("(s)", (sess.session_id,)))
        elif method_name == "Accept":
            self._manager.accept_incoming()
            invocation.return_value(None)
        elif method_name == "Reject":
            self._manager.reject_incoming()
            invocation.return_value(None)
        elif method_name == "Hangup":
            self._manager.hangup()
            invocation.return_value(None)
        elif method_name == "SendDtmf":
            _sid, digit = parameters.unpack()
            self._manager.send_dtmf(digit)
            invocation.return_value(None)
        elif method_name == "SetHold":
            _sid, hold = parameters.unpack()
            self._manager.set_hold(hold)
            invocation.return_value(None)
        elif method_name == "SetMute":
            _sid, muted = parameters.unpack()
            self._manager.set_mic_mute(muted)
            invocation.return_value(None)
        else:
            invocation.return_error_literal(
                Gio.dbus_error_quark(), Gio.DBusError.UNKNOWN_METHOD,
                f"Unknown method: {method_name}")

    # -- CallManager → D-Bus signals ------------------------------------

    def _emit_signal(self, signal_name: str, args: GLib.Variant) -> None:
        if self._connection is None:
            return
        try:
            self._connection.emit_signal(
                None, OBJECT_PATH, INTERFACE_NAME, signal_name, args)
        except GLib.Error:
            pass

    def _on_call_started(self, _manager, session, direction):
        if direction == "incoming":
            number = numfmt.jid_to_number(
                session.peer_jid, self._account.gateway) or session.peer_jid
            name = session.peer_label or number
            self._emit_signal("IncomingCall", GLib.Variant(
                "(sss)", (session.session_id, number, name)))
        self._emit_signal("CallStateChanged", GLib.Variant(
            "(ss)", (session.session_id, session.state)))

    def _on_call_changed(self, _manager, session):
        self._emit_signal("CallStateChanged", GLib.Variant(
            "(ss)", (session.session_id, session.state)))

    def _on_call_ended(self, _manager, session):
        self._emit_signal("CallStateChanged", GLib.Variant(
            "(ss)", (session.session_id, session.state)))


def gnome_calls_is_active() -> bool:
    """Check whether gnome-calls is running on the session bus.

    Used by the call-dialog show path to decide whether to yield to
    gnome-calls (which drives its own UI via our Calls1 interface) or
    present Patch's built-in dialog.
    """
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", ("org.gnome.Calls",)),
            GLib.VariantType.new("(b)"),
            Gio.DBusCallFlags.NONE, 500, None)
        return result.unpack()[0]
    except Exception:  # noqa: BLE001
        return False
