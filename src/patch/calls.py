"""Single-call session model + state machine.

XEP-0353 Jingle Message Initiation only — no audio yet. This module owns
the state machine that drives the call UI:

  idle  -> proposing      (outgoing: we sent propose, waiting for proceed)
        -> ringing        (incoming: peer sent propose, we haven't acted)
  proposing -> active     (peer sent proceed; in a real impl we'd now
                           negotiate a Jingle session for audio)
  proposing -> rejected   (peer sent reject)
  proposing -> retracted  (we sent retract)
  ringing   -> active     (we sent accept)
  ringing   -> rejected   (we sent reject)
  active    -> ended      (either side hung up — JMI retract or session-terminate)

`CallSession` is a GObject so the UI can bind to its properties.
`CallManager` owns the single active session (multi-line is a later
phase) and serves as the bridge between XmppClient JMI signals and the
UI.
"""

from __future__ import annotations

import logging
import uuid

import time

from gi.repository import GObject

from patch import numfmt
from patch.jingle_session import JingleSession
from patch.ringer import Ringer
from patch.xmpp import jingle as jingle_mod

log = logging.getLogger(__name__)


# State machine values. Strings (not enum) for the same reason as
# account.STATE_* — they're easy to read in logs.
STATE_IDLE       = "idle"
STATE_PROPOSING  = "proposing"     # outgoing, awaiting proceed
STATE_RINGING    = "ringing"       # incoming, awaiting our accept/reject
STATE_ACTIVE     = "active"        # would have audio in a real impl
STATE_REJECTED   = "rejected"
STATE_RETRACTED  = "retracted"
STATE_ENDED      = "ended"

_TERMINAL = {STATE_REJECTED, STATE_RETRACTED, STATE_ENDED}


class CallSession(GObject.Object):
    __gtype_name__ = "PatchCallSession"

    session_id  = GObject.Property(type=str, default="")
    peer_jid    = GObject.Property(type=str, default="")
    peer_label  = GObject.Property(type=str, default="")
    incoming    = GObject.Property(type=bool, default=False)
    state       = GObject.Property(type=str, default=STATE_IDLE)

    def __init__(self, session_id: str, peer_jid: str, peer_label: str,
                 incoming: bool, initial_state: str):
        super().__init__()
        self.session_id = session_id
        self.peer_jid   = peer_jid
        self.peer_label = peer_label
        self.incoming   = incoming
        self.state      = initial_state

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL


class CallManager(GObject.Object):
    __gtype_name__ = "PatchCallManager"

    __gsignals__ = {
        # session, "incoming" | "outgoing" — new session began
        "call-started":   (GObject.SignalFlags.RUN_FIRST, None, (object, str)),
        # session — state transitioned (look at .state)
        "call-changed":   (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # session — terminal state reached; UI should dismiss
        "call-ended":     (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, account, xmpp, contacts=None, store=None):
        super().__init__()
        self._account = account
        self._xmpp = xmpp
        self._contacts = contacts
        self._store = store
        self._session: CallSession | None = None
        # Time the current session began, so the log entry gets the
        # original timestamp on terminal transition.
        self._session_started_at: float = 0.0
        # Active Jingle session — owns the GStreamer pipeline + handles
        # the SDP/ICE exchange. None when there's no audio in flight.
        self._jingle: JingleSession | None = None
        # For incoming calls we may receive the peer's session-initiate
        # before our user has tapped Accept. Stash it.
        self._pending_initiate: dict | None = None
        # Ringtone driver — feedbackd on Phosh, GStreamer-synthesised
        # ringback elsewhere. Singleton; start/stop is idempotent.
        self._ringer = Ringer()

        self._xmpp.connect("jmi-event", self._on_jmi)
        self._xmpp.connect("jingle-iq", self._on_jingle_iq)

    # -- outgoing --------------------------------------------------------

    def start_outgoing(self, peer_jid: str) -> CallSession | None:
        if self._session is not None and not self._session.is_terminal:
            log.warning("start_outgoing: already in a call");
            return None
        session_id = uuid.uuid4().hex
        peer_label = self._label_for(peer_jid)
        sess = CallSession(
            session_id=session_id, peer_jid=peer_jid, peer_label=peer_label,
            incoming=False, initial_state=STATE_PROPOSING)
        self._session = sess
        self._session_started_at = time.time()
        # Fire the signal before sending the stanza so the UI has a chance
        # to show 'connecting…' while the propose is in flight.
        self.emit("call-started", sess, "outgoing")
        # Pre-warm the Jingle session: build the audio engine + start ICE
        # gathering during the JMI propose -> proceed round-trip. When
        # proceed arrives, create_offer runs against an already-ready
        # engine instead of waiting for TURN disco + webrtcbin setup.
        own_jid_full = self._own_full_jid()
        self._jingle = JingleSession(
            self._xmpp, sid=session_id,
            peer_jid=peer_jid, own_jid=own_jid_full,
            incoming=False)
        self._jingle.prewarm()
        ok = self._xmpp.send_jmi("propose", session_id, peer_jid)
        if not ok:
            self._transition(sess, STATE_ENDED)
        return sess

    def retract_outgoing(self) -> None:
        sess = self._session
        if sess is None or sess.state != STATE_PROPOSING:
            return
        self._xmpp.send_jmi("retract", sess.session_id, sess.peer_jid)
        self._transition(sess, STATE_RETRACTED)

    # -- incoming --------------------------------------------------------

    def accept_incoming(self) -> None:
        sess = self._session
        if sess is None or sess.state != STATE_RINGING:
            return
        # XEP-0353 §6.2 (corrected — these used to be swapped, which
        # made the PSTN gateway give up after 3s because it never saw
        # its proceed signal):
        #   - <proceed/> to the *caller's* full JID — tells them to
        #     start the Jingle session-initiate.
        #   - <accept/>  to our *own bare JID* — informs other
        #     resources on this account that we're handling the call
        #     here, so they stop ringing.
        own_bare = self._account.jid
        self._xmpp.send_jmi("proceed", sess.session_id, sess.peer_jid)
        self._xmpp.send_jmi("accept",  sess.session_id, own_bare)
        # Start the Jingle audio session — the caller will follow up
        # with session-initiate, which we answer. If that initiate
        # already arrived (raced our proceed), use the buffered one.
        self._begin_jingle_incoming(sess)
        self._transition(sess, STATE_ACTIVE)

    def reject_incoming(self) -> None:
        sess = self._session
        if sess is None or sess.state != STATE_RINGING:
            return
        own_bare = self._account.jid
        self._xmpp.send_jmi("reject", sess.session_id, own_bare)
        self._xmpp.send_jmi("reject", sess.session_id, sess.peer_jid)
        self._transition(sess, STATE_REJECTED)

    # -- active -> ended -------------------------------------------------

    def hangup(self) -> None:
        sess = self._session
        if sess is None or sess.state != STATE_ACTIVE:
            return
        if self._jingle is not None:
            self._jingle.send_session_terminate("success")
            self._jingle = None
        self._transition(sess, STATE_ENDED)

    def send_dtmf(self, digit: str) -> bool:
        """Forward a touch-tone digit through the active Jingle session.

        Returns False if there's no active call to send through; the UI
        treats that as 'press did nothing' rather than an error.
        """
        sess = self._session
        if sess is None or sess.state != STATE_ACTIVE:
            return False
        if self._jingle is None:
            return False
        return self._jingle.send_dtmf(digit)

    def set_mic_mute(self, muted: bool) -> None:
        """Mute/unmute the local mic on the active call. No-op if no call."""
        if self._jingle is None:
            return
        self._jingle.set_mic_mute(muted)

    # -- inbound from XmppClient ----------------------------------------

    def _on_jmi(self, _xmpp, action, session_id, peer_jid, incoming):
        sess = self._session
        if action == "propose":
            if not incoming:
                return     # our own outgoing propose echoed back
            if sess is not None and not sess.is_terminal:
                # Busy. Reply with reject so the caller knows.
                self._xmpp.send_jmi("reject", session_id, peer_jid)
                return
            peer_label = self._label_for(peer_jid)
            sess = CallSession(
                session_id=session_id, peer_jid=peer_jid,
                peer_label=peer_label, incoming=True,
                initial_state=STATE_RINGING)
            self._session = sess
            self._session_started_at = time.time()
            # Start ringing immediately — _transition won't fire for the
            # initial state, so the ringer needs explicit kick here.
            self._ringer.start()
            self.emit("call-started", sess, "incoming")
            return

        if sess is None or sess.session_id != session_id:
            log.debug("JMI %s for unknown session %s (ignored)",
                      action, session_id)
            return

        if action == "proceed" and sess.state == STATE_PROPOSING:
            # Peer is ready for the Jingle session. We're the initiator
            # of the audio session-initiate.
            self._begin_jingle_outgoing(sess)
            self._transition(sess, STATE_ACTIVE)
        elif action == "accept" and sess.state == STATE_RINGING and not incoming:
            # XEP-0353 accept-to-self from another of our resources —
            # the user took the call on another device. Silently
            # dismiss with a distinct terminal so the UI can show
            # 'answered elsewhere' rather than 'rejected'.
            self._transition(sess, STATE_REJECTED)
        elif action == "accept" and sess.state == STATE_PROPOSING:
            # accept without an explicit proceed (cheogram does this for
            # certain endpoints). Same effect: we're the initiator.
            self._begin_jingle_outgoing(sess)
            self._transition(sess, STATE_ACTIVE)
        elif action == "reject":
            self._transition(sess, STATE_REJECTED)
        elif action == "retract":
            self._transition(sess, STATE_RETRACTED)

    # -- Jingle audio orchestration -------------------------------------

    def _begin_jingle_outgoing(self, sess: CallSession) -> None:
        # In the normal flow start_outgoing has already pre-warmed a
        # JingleSession with the right sid + peer; here we just fire the
        # session-initiate against that warm engine. Recreate only as a
        # defensive fallback (e.g. session swapped out by a race).
        if self._jingle is None or self._jingle.sid != sess.session_id:
            own_jid_full = self._own_full_jid()
            self._jingle = JingleSession(
                self._xmpp, sid=sess.session_id,
                peer_jid=sess.peer_jid, own_jid=own_jid_full,
                incoming=False)
        self._jingle.start_outgoing()

    def _begin_jingle_incoming(self, sess: CallSession) -> None:
        if self._jingle is not None:
            return
        own_jid_full = self._own_full_jid()
        self._jingle = JingleSession(
            self._xmpp, sid=sess.session_id,
            peer_jid=sess.peer_jid, own_jid=own_jid_full,
            incoming=True)
        # Start engine + ICE gathering now so they overlap with the
        # peer's JMI proceed -> session-initiate round-trip. When the
        # session-initiate arrives the engine is ready and we can fire
        # session-accept immediately instead of waiting for TURN +
        # webrtcbin setup.
        self._jingle.prewarm()
        if self._pending_initiate is not None:
            self._jingle.start_incoming(self._pending_initiate)
            self._pending_initiate = None
        # Otherwise: _on_jingle_iq routes the peer's session-initiate
        # to JingleSession.start_incoming when it arrives — by then
        # the engine is warm from the prewarm call above.

    def _on_jingle_iq(self, _xmpp, parsed, from_jid, _iq_id):
        action = parsed.get("action")
        sid = parsed.get("sid")
        sess = self._session
        # Stash session-initiate even if we don't have a CallSession yet
        # — JMI propose may race the Jingle initiate, especially on slow
        # links. _begin_jingle_incoming will pick it up on accept.
        if action == "session-initiate":
            if self._jingle is not None and self._jingle.sid == sid:
                self._jingle.start_incoming(parsed)
            else:
                self._pending_initiate = parsed
            return
        if self._jingle is None or self._jingle.sid != sid:
            log.debug("jingle %s for unknown sid %s — ignored", action, sid)
            return
        if action == "session-accept":
            self._jingle.handle_session_accept(parsed)
        elif action == "transport-info":
            self._jingle.handle_transport_info(parsed)
        elif action == "session-terminate":
            self._jingle.handle_session_terminate(parsed)
            self._jingle = None
            if sess and sess.state == STATE_ACTIVE:
                self._transition(sess, STATE_ENDED)

    def _own_full_jid(self) -> str:
        bare = self._account.jid
        try:
            return self._xmpp._client.get_bound_jid().full()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return bare

    # -- helpers --------------------------------------------------------

    def _transition(self, sess: CallSession, new_state: str) -> None:
        log.info("call %s -> %s", sess.session_id[:8], new_state)
        prior = sess.state
        sess.state = new_state
        # Ringtone control: ring while RINGING, silent everywhere else.
        if new_state == STATE_RINGING and prior != STATE_RINGING:
            self._ringer.start()
        elif prior == STATE_RINGING and new_state != STATE_RINGING:
            self._ringer.stop()
        self.emit("call-changed", sess)
        if sess.is_terminal:
            self._ringer.stop()
            # Tear down any in-flight Jingle session — this used to be
            # leaked when a call ended via JMI retract/reject, causing
            # a later hangup() on a fresh call to fire session-terminate
            # against the stale (older) sid and get back an
            # unknown-session error.
            if self._jingle is not None and self._jingle.sid == sess.session_id:
                self._jingle.shutdown()
                self._jingle = None
            self._log_call(sess)
            self.emit("call-ended", sess)
            # Keep `_session` around briefly so the UI can read terminal
            # state, then clear when a fresh call starts.
            if self._session is sess:
                self._session = sess if not sess.is_terminal else None

    def _log_call(self, sess: CallSession) -> None:
        if self._store is None:
            return
        try:
            self._store.add_call(
                peer_jid=sess.peer_jid,
                peer_label=sess.peer_label or sess.peer_jid,
                direction="incoming" if sess.incoming else "outgoing",
                state=sess.state,
                started_at=self._session_started_at or time.time(),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not log call: %s", exc)

    def _label_for(self, jid: str) -> str:
        # Delegate to contacts.label_for_jid which handles group JIDs
        # too. Falls back to bare JID if no contacts manager.
        if self._contacts is not None:
            return self._contacts.label_for_jid(jid)
        number = numfmt.jid_to_number(jid, self._account.gateway)
        if number:
            return numfmt.format_for_display(number)
        return jid
