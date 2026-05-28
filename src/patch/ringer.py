"""Incoming-call ringtone and outgoing ringback tone.

Two backends for the incoming ringtone:

1. **feedbackd** (Phosh) — fires the `phone-incoming-call` named event
   via `org.sigxcpu.Feedback` on the session bus. The compositor picks
   the right ringtone + haptic + LED behaviour from the user's
   feedback profile (silent / vibrate / loud). Cadencing is handled by
   feedbackd itself.

2. **GStreamer fallback** — synthesises a 440/480 Hz US-style ring
   using `audiotestsrc`. Cadenced: 2 s on, 4 s off.

Outgoing ringback always uses GStreamer (no feedbackd event for it).
Same 440/480 Hz tone, 2 s on / 4 s off, at lower volume through the
earpiece (``media.role=phone`` tells PipeWire to route to the
communication output).
"""

from __future__ import annotations

import logging

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

_FEEDBACKD_BUS  = "org.sigxcpu.Feedback"
_FEEDBACKD_PATH = "/org/sigxcpu/Feedback"
_FEEDBACKD_IFC  = "org.sigxcpu.Feedback"

_RING_ON_MS  = 2000
_RING_OFF_MS = 4000


class Ringer:
    def __init__(self, app_id: str = "land.rob.patch"):
        self._app_id = app_id
        self._feedback_id: int | None = None
        self._pipeline = None       # Gst.Pipeline | None
        self._gst_init_failed = False
        self._cadence_timer: int = 0

    def start(self) -> None:
        """Start the incoming-call ringtone."""
        if self._try_feedbackd():
            return
        self._start_gst(volume=0.35, phone_role=False)

    def start_ringback(self) -> None:
        """Start the outgoing-call ringback tone (earpiece, cadenced)."""
        self._start_gst(volume=0.20, phone_role=True)

    def stop(self) -> None:
        if self._cadence_timer:
            GLib.source_remove(self._cadence_timer)
            self._cadence_timer = 0
        if self._feedback_id is not None:
            self._stop_feedbackd()
            self._feedback_id = None
        self._stop_gst()

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

    # -- gst ring / ringback --------------------------------------------

    def _start_gst(self, *, volume: float, phone_role: bool) -> None:
        if self._gst_init_failed:
            return
        self._stop_gst()
        try:
            from gi.repository import Gst
            Gst.init(None)
            self._pipeline = Gst.parse_launch(
                "audiomixer name=mix ! audioconvert ! audioresample ! "
                "audio/x-raw,channels=1,rate=48000 ! "
                f"volume name=ring_vol volume={volume} ! "
                "pulsesink name=ring_sink "
                "audiotestsrc wave=sine freq=440 is-live=true ! mix. "
                "audiotestsrc wave=sine freq=480 is-live=true ! mix."
            )
            if phone_role:
                sink = self._pipeline.get_by_name("ring_sink")
                if sink is not None:
                    props = Gst.Structure.from_string(
                        'props,media.role=phone')[0]
                    sink.set_property("stream-properties", props)
            self._pipeline.set_state(Gst.State.PLAYING)
            kind = "ringback" if phone_role else "ringtone"
            log.info("ringer: gst %s started", kind)
            self._cadence_timer = GLib.timeout_add(
                _RING_ON_MS, self._cadence_off)
        except Exception as exc:  # noqa: BLE001
            log.warning("ringer gst start failed: %s", exc)
            self._gst_init_failed = True
            self._pipeline = None

    def _stop_gst(self) -> None:
        if self._pipeline is not None:
            try:
                from gi.repository import Gst
                self._pipeline.set_state(Gst.State.NULL)
            except Exception:  # noqa: BLE001
                pass
            self._pipeline = None

    def _cadence_off(self) -> bool:
        self._set_ring_volume(0.0)
        self._cadence_timer = GLib.timeout_add(
            _RING_OFF_MS, self._cadence_on)
        return False

    def _cadence_on(self) -> bool:
        self._set_ring_volume(-1.0)
        self._cadence_timer = GLib.timeout_add(
            _RING_ON_MS, self._cadence_off)
        return False

    def _set_ring_volume(self, vol: float) -> None:
        """Set ring volume. Negative means restore the original level."""
        if self._pipeline is None:
            return
        el = self._pipeline.get_by_name("ring_vol")
        if el is None:
            return
        if vol < 0:
            el.set_property("mute", False)
        else:
            el.set_property("mute", True)
