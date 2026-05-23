"""Active-call dialog. Pure XEP-0353 JMI signalling — no audio yet."""

from __future__ import annotations

import logging
import time

from gi.repository import Adw, Gio, GLib, Gtk

from patch import calls

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/patch/ui/call-dialog.ui")
class PatchCallDialog(Adw.Dialog):
    __gtype_name__ = "PatchCallDialog"

    header_title:   Adw.WindowTitle = Gtk.Template.Child()
    peer_label:     Gtk.Label       = Gtk.Template.Child()
    state_label:    Gtk.Label       = Gtk.Template.Child()
    accept_button:  Gtk.Button      = Gtk.Template.Child()
    reject_button:  Gtk.Button      = Gtk.Template.Child()
    hangup_button:  Gtk.Button      = Gtk.Template.Child()
    dtmf_pad:       Gtk.Grid        = Gtk.Template.Child()

    def __init__(self, manager, session):
        super().__init__()
        self._manager = manager
        self._session = session
        # Set when the session first enters ACTIVE — the timer tick reads
        # this to compute MM:SS.
        self._active_since: float | None = None
        self._timer_source: int = 0

        # Dialpad action: each button activates patch.dtmf("<digit>")
        # which we route through CallManager.send_dtmf during ACTIVE
        # calls. Scoped to the dialog so it disappears with it.
        actions = Gio.SimpleActionGroup()
        dtmf_action = Gio.SimpleAction.new(
            "dtmf", GLib.VariantType.new("s"))
        dtmf_action.connect("activate", self._on_dtmf)
        actions.add_action(dtmf_action)
        self.insert_action_group("patch", actions)

        self.peer_label.set_text(session.peer_label or session.peer_jid)
        self._refresh_state()

        # Re-render on every state change. CallManager fires call-changed
        # for transitions and call-ended for terminals; both end up here.
        self._manager.connect("call-changed", self._on_call_event)
        self._manager.connect("call-ended",   self._on_call_event)

        self.accept_button.connect("clicked", lambda *_: self._manager.accept_incoming())
        self.reject_button.connect("clicked", lambda *_: self._manager.reject_incoming())
        self.hangup_button.connect("clicked", lambda *_: self._on_hangup())

    def _on_dtmf(self, _action, param):
        digit = param.get_string()
        self._manager.send_dtmf(digit)

    # -- state-driven UI -------------------------------------------------

    _STATE_COPY = {
        calls.STATE_RINGING:    ("Incoming call",   "Ringing…"),
        calls.STATE_PROPOSING:  ("Calling",         "Waiting for proceed…"),
        calls.STATE_ACTIVE:     ("In call",         "00:00"),
        calls.STATE_REJECTED:   ("Call rejected",   "Closed."),
        calls.STATE_RETRACTED:  ("Call cancelled",  "Closed."),
        calls.STATE_ENDED:      ("Call ended",      "Closed."),
    }

    def _refresh_state(self):
        sess = self._session
        title, sub = self._STATE_COPY.get(sess.state, (sess.state, ""))
        self.header_title.set_title(title)
        self.state_label.set_text(sub)

        ringing  = sess.state == calls.STATE_RINGING
        active   = sess.state == calls.STATE_ACTIVE
        proposing = sess.state == calls.STATE_PROPOSING
        self.accept_button.set_visible(ringing)
        self.reject_button.set_visible(ringing)
        self.hangup_button.set_visible(active or proposing)
        # Touch-tone dialpad only makes sense while a call is live.
        self.dtmf_pad.set_visible(active)
        # The header has no close button — let the user dismiss only
        # after the session is terminal.
        self.set_can_close(sess.is_terminal)

        # Manage the call-duration ticker. Start once on entry to
        # ACTIVE, stop on any other state.
        if active and self._active_since is None:
            self._active_since = time.time()
            self._timer_source = GLib.timeout_add_seconds(1, self._tick)
            self._tick()  # populate 00:00 immediately
        elif not active and self._timer_source:
            GLib.source_remove(self._timer_source)
            self._timer_source = 0

    def _tick(self) -> bool:
        if self._active_since is None:
            return False
        elapsed = int(time.time() - self._active_since)
        m, s = divmod(elapsed, 60)
        h, m = divmod(m, 60)
        if h:
            label = f"{h:d}:{m:02d}:{s:02d}"
        else:
            label = f"{m:02d}:{s:02d}"
        self.state_label.set_text(label)
        return True  # keep firing

    def _on_call_event(self, _manager, session):
        if session is not self._session:
            return
        self._refresh_state()
        if session.is_terminal:
            # Auto-dismiss the terminal screen after a beat so the user
            # gets to see why the call ended (rejected vs cancelled vs
            # peer hung up) without having to tap.
            from gi.repository import GLib
            GLib.timeout_add(1500, self._close_once)

    def _close_once(self):
        try:
            self.force_close()
        except Exception:  # noqa: BLE001
            pass
        return False

    def _on_hangup(self):
        if self._session.state == calls.STATE_PROPOSING:
            self._manager.retract_outgoing()
        else:
            self._manager.hangup()
