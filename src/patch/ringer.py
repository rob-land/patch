"""Incoming-call ringtone.

Two paths:

1. **feedbackd** (Phosh) — fires the `phone-incoming-call` named event
   via `org.sigxcpu.Feedback` on the session bus. The compositor picks
   the right ringtone + haptic + LED behaviour from the user's
   feedback profile (silent / vibrate / loud).

2. **GStreamer fallback** — synthesises a 440/480 Hz US-style
   ringback tone using `audiotestsrc`. No file dependency; works in
   any runtime that has gst-plugins-base. Not cadenced — continuous
   while the call is ringing.

The class exposes start() / stop() and figures out which backend to
use at first start().
"""

from __future__ import annotations

import logging

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

_FEEDBACKD_BUS  = "org.sigxcpu.Feedback"
_FEEDBACKD_PATH = "/org/sigxcpu/Feedback"
_FEEDBACKD_IFC  = "org.sigxcpu.Feedback"


class Ringer:
    def __init__(self, app_id: str = "land.rob.patch"):
        self._app_id = app_id
        self._feedback_id: int | None = None
        self._pipeline = None       # Gst.Pipeline | None
        self._gst_init_failed = False

    def start(self) -> None:
        if self._try_feedbackd():
            return
        self._start_gst()

    def stop(self) -> None:
        if self._feedback_id is not None:
            self._stop_feedbackd()
            self._feedback_id = None
        if self._pipeline is not None:
            try:
                from gi.repository import Gst
                self._pipeline.set_state(Gst.State.NULL)
            except Exception:  # noqa: BLE001
                pass
            self._pipeline = None

    # -- feedbackd ------------------------------------------------------

    def _try_feedbackd(self) -> bool:
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            return False
        try:
            reply = bus.call_sync(
                _FEEDBACKD_BUS, _FEEDBACKD_PATH, _FEEDBACKD_IFC,
                "TriggerFeedback",
                GLib.Variant("(ssa{sv}i)",
                             (self._app_id, "phone-incoming-call", {}, -1)),
                GLib.VariantType.new("(u)"),
                Gio.DBusCallFlags.NONE, -1, None)
        except GLib.Error as exc:
            log.debug("feedbackd not available (%s) — falling back to gst",
                      exc.message)
            return False
        self._feedback_id = reply.unpack()[0]
        log.info("ringer: feedbackd event %s", self._feedback_id)
        return True

    def _stop_feedbackd(self) -> None:
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call_sync(
                _FEEDBACKD_BUS, _FEEDBACKD_PATH, _FEEDBACKD_IFC,
                "EndFeedback",
                GLib.Variant("(u)", (self._feedback_id,)),
                None, Gio.DBusCallFlags.NONE, -1, None)
        except GLib.Error as exc:
            log.debug("feedbackd end failed: %s", exc.message)

    # -- gst fallback ---------------------------------------------------

    def _start_gst(self) -> None:
        if self._gst_init_failed:
            return
        try:
            from gi.repository import Gst
            Gst.init(None)
            # US-style ringback: 440 + 480 Hz mix. Continuous (no cadence)
            # — Phosh's feedbackd would handle the on/off pattern in the
            # nicer path; this is just "you have an incoming call".
            self._pipeline = Gst.parse_launch(
                "audiomixer name=mix ! audioconvert ! audioresample ! "
                "audio/x-raw,channels=1,rate=48000 ! "
                "volume volume=0.35 ! pulsesink "
                "audiotestsrc wave=sine freq=440 is-live=true ! mix. "
                "audiotestsrc wave=sine freq=480 is-live=true ! mix."
            )
            self._pipeline.set_state(Gst.State.PLAYING)
            log.info("ringer: gst synthesized ringtone")
        except Exception as exc:  # noqa: BLE001
            log.warning("ringer gst start failed: %s", exc)
            self._gst_init_failed = True
            self._pipeline = None
