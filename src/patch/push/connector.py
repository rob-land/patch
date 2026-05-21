"""UnifiedPush `org.unifiedpush.Connector1` D-Bus service.

A distributor (KUnifiedPush, ntfy-android, etc.) talks to this service
to deliver:

  - `NewEndpoint(token, endpoint)`        — the endpoint URL we should
                                             register with the application
                                             server (our chat.rob.land)
  - `Message(token, message, message_id)` — an actual push payload
  - `Unregistered(token)`                 — distributor revoked us
  - `RegistrationFailed(token, reason)`   — distributor refused

Our token is a stable string ("patch-default" for now) so the same
slot is reused across restarts. Multi-account / multi-distributor
support is a later phase.

The Connector1 interface is exposed on the session bus as
`/org/unifiedpush/Connector` so it's findable by any well-behaved
distributor that follows the UP spec.
"""

from __future__ import annotations

import logging

from gi.repository import Gio, GLib, GObject

log = logging.getLogger(__name__)


CONNECTOR_OBJECT_PATH = "/org/unifiedpush/Connector"
CONNECTOR_IFACE       = "org.unifiedpush.Connector1"

# Stable token used for all our subscriptions. The token is opaque to the
# distributor; we use it on the receiving side to disambiguate which of our
# subscriptions a delivery is for (we only have one for now).
DEFAULT_TOKEN = "patch-default"

_INTROSPECTION_XML = f"""
<node>
  <interface name='{CONNECTOR_IFACE}'>
    <method name='NewEndpoint'>
      <arg type='s' name='token'    direction='in'/>
      <arg type='s' name='endpoint' direction='in'/>
    </method>
    <method name='Unregistered'>
      <arg type='s' name='token' direction='in'/>
    </method>
    <method name='RegistrationFailed'>
      <arg type='s' name='token'  direction='in'/>
      <arg type='s' name='reason' direction='in'/>
    </method>
    <method name='Message'>
      <arg type='s'  name='token'      direction='in'/>
      <arg type='ay' name='message'    direction='in'/>
      <arg type='s'  name='message_id' direction='in'/>
    </method>
  </interface>
</node>
"""


class PushConnector(GObject.Object):
    """Owns the Connector1 D-Bus object + emits typed GObject signals."""

    __gtype_name__ = "PatchPushConnector"

    __gsignals__ = {
        # endpoint URL (str)
        "new-endpoint":        (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # message bytes, message id
        "message":             (GObject.SignalFlags.RUN_FIRST, None, (object, str)),
        # reason
        "registration-failed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # no args
        "unregistered":        (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self._regid: int | None = None
        self._connection: Gio.DBusConnection | None = None

    def publish(self) -> None:
        """Register the Connector1 object on the session bus."""
        if self._regid is not None:
            return
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            log.warning("session bus unavailable: %s", exc.message)
            return
        info = Gio.DBusNodeInfo.new_for_xml(_INTROSPECTION_XML)
        iface_info = info.lookup_interface(CONNECTOR_IFACE)
        if iface_info is None:
            log.error("introspection lookup failed");
            return
        self._regid = bus.register_object(
            object_path=CONNECTOR_OBJECT_PATH,
            interface_info=iface_info,
            method_call_closure=self._on_method_call,
            get_property_closure=None,
            set_property_closure=None,
        )
        self._connection = bus
        log.info("Connector1 published at %s", CONNECTOR_OBJECT_PATH)

    def unpublish(self) -> None:
        if self._regid is not None and self._connection is not None:
            self._connection.unregister_object(self._regid)
            self._regid = None

    # -- D-Bus method dispatch -------------------------------------------

    def _on_method_call(self, connection, sender, object_path, interface_name,
                        method_name, parameters, invocation):
        try:
            args = parameters.unpack()
            if method_name == "NewEndpoint":
                token, endpoint = args
                log.info("NewEndpoint token=%s endpoint=%s", token, endpoint)
                self.emit("new-endpoint", endpoint)
                invocation.return_value(None)
            elif method_name == "Message":
                token, message_bytes, message_id = args
                log.info("Message token=%s id=%s len=%d",
                         token, message_id, len(message_bytes))
                self.emit("message", bytes(message_bytes), message_id)
                invocation.return_value(None)
            elif method_name == "RegistrationFailed":
                token, reason = args
                log.warning("RegistrationFailed token=%s reason=%s", token, reason)
                self.emit("registration-failed", reason)
                invocation.return_value(None)
            elif method_name == "Unregistered":
                token, = args
                log.info("Unregistered token=%s", token)
                self.emit("unregistered")
                invocation.return_value(None)
            else:
                invocation.return_error_literal(
                    Gio.dbus_error_quark(),
                    Gio.DBusError.UNKNOWN_METHOD,
                    f"Unknown method {method_name}")
        except Exception as exc:
            log.exception("connector dispatch failed: %s", exc)
            invocation.return_error_literal(
                Gio.dbus_error_quark(),
                Gio.DBusError.FAILED,
                str(exc))
