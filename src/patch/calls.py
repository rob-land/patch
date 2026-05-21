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

from gi.repository import GObject

from patch import numfmt

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

    def __init__(self, account, xmpp, contacts=None):
        super().__init__()
        self._account = account
        self._xmpp = xmpp
        self._contacts = contacts
        self._session: CallSession | None = None

        self._xmpp.connect("jmi-event", self._on_jmi)

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
        # Fire the signal before sending the stanza so the UI has a chance
        # to show 'connecting…' while the propose is in flight.
        self.emit("call-started", sess, "outgoing")
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
        # XEP-0353 §6.2: respond to the user's own bare JID with proceed
        # AND broadcast accept to the original peer. cheogram doesn't
        # need the proceed-to-self path because it's a gateway and not
        # a multi-device endpoint, but we follow the spec.
        own_bare = self._account.jid
        self._xmpp.send_jmi("proceed", sess.session_id, own_bare)
        self._xmpp.send_jmi("accept", sess.session_id, sess.peer_jid)
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
        # Without a real Jingle session in place, "hangup" is a no-op
        # at the protocol level. Once Jingle audio lands, this will
        # send session-terminate. For now we just close out the UI.
        self._transition(sess, STATE_ENDED)

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
            self.emit("call-started", sess, "incoming")
            return

        if sess is None or sess.session_id != session_id:
            log.debug("JMI %s for unknown session %s (ignored)",
                      action, session_id)
            return

        if action == "proceed" and sess.state == STATE_PROPOSING:
            self._transition(sess, STATE_ACTIVE)
        elif action == "accept" and sess.state == STATE_PROPOSING:
            self._transition(sess, STATE_ACTIVE)
        elif action == "reject":
            self._transition(sess, STATE_REJECTED)
        elif action == "retract":
            self._transition(sess, STATE_RETRACTED)

    # -- helpers --------------------------------------------------------

    def _transition(self, sess: CallSession, new_state: str) -> None:
        log.info("call %s -> %s", sess.session_id[:8], new_state)
        sess.state = new_state
        self.emit("call-changed", sess)
        if sess.is_terminal:
            self.emit("call-ended", sess)
            # Keep `_session` around briefly so the UI can read terminal
            # state, then clear when a fresh call starts.
            if self._session is sess:
                self._session = sess if not sess.is_terminal else None

    def _label_for(self, jid: str) -> str:
        number = numfmt.jid_to_number(jid, self._account.gateway)
        if number:
            name = self._contacts.lookup(number) if self._contacts else None
            return name or numfmt.format_for_display(number)
        return jid
