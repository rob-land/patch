"""Owns one in-flight Jingle audio session.

Lifecycle wired to CallSession via CallManager:

  outgoing call:
    CallSession goes from PROPOSING -> ACTIVE (peer accepted JMI)
    CallManager.start_jingle_audio() instantiates one of these
    -> AudioEngine.start("outgoing")
    -> AudioEngine.create_offer() -> SDP
    -> wrap SDP in jingle.session_initiate(), send via XmppClient
    -> ICE candidates trickle out via signal-driven transport-info

  incoming call:
    CallSession goes RINGING -> ACTIVE (we sent JMI accept)
    The peer's session-initiate arrives via XmppClient.jingle-iq
    -> set_remote_description(offer)
    -> AudioEngine.create_answer() -> SDP
    -> wrap in jingle.session_accept(), send back

Inbound transport-info: peer's ICE candidates arrive — we extract them
from the Jingle transport and inject into webrtcbin.

Termination: CallSession -> ENDED fires send_jingle_terminate() which
sends session-terminate AND tears down the audio engine.
"""

from __future__ import annotations

import logging

from gi.repository import GObject

from patch.audio import AudioEngine, sdp_to_jingle_description, jingle_content_to_sdp
from patch.xmpp import jingle as jingle_mod
from patch.xmpp.turn import fetch_turn_uri

log = logging.getLogger(__name__)


class JingleSession(GObject.Object):
    __gtype_name__ = "PatchJingleSession"

    def __init__(self, xmpp, *, sid: str, peer_jid: str, own_jid: str,
                 incoming: bool):
        super().__init__()
        self._xmpp = xmpp
        self.sid = sid
        self.peer_jid = peer_jid
        self.own_jid = own_jid
        self.incoming = incoming
        # Engine constructed lazily once we know the TURN URI.
        self.engine: AudioEngine | None = None
        self._engine_ready = False
        # Buffered candidates if they arrive before set_remote_description
        # has settled — webrtcbin requires the remote desc first.
        self._pending_remote_candidates: list[str] = []
        self._remote_desc_set = False
        # Deferred work that needs to run AFTER the engine is built.
        self._on_engine_ready: list = []

    # -- outgoing flow --------------------------------------------------

    def start_outgoing(self) -> None:
        self._with_engine(lambda: self.engine.create_offer(
            self._send_session_initiate))

    def _send_session_initiate(self, sdp_text: str) -> None:
        log.info("local offer SDP ready (%d bytes)", len(sdp_text))
        log.debug("offer SDP:\n%s", sdp_text)
        try:
            kw = sdp_to_jingle_description(sdp_text)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not translate SDP to Jingle: %s", exc)
            return
        iq = jingle_mod.session_initiate(
            to_jid=self.peer_jid,
            initiator=self.own_jid,
            sid=self.sid,
            **kw,
        )
        self._xmpp.send_iq(iq)

    # -- incoming flow --------------------------------------------------

    def start_incoming(self, parsed_jingle: dict) -> None:
        self._with_engine(lambda: self._handle_session_initiate(parsed_jingle))

    def _handle_session_initiate(self, parsed: dict) -> None:
        contents = parsed.get("contents") or []
        if not contents:
            log.warning("session-initiate with no contents; ignoring")
            return
        content = contents[0]
        sdp = jingle_content_to_sdp(content, role="offer")
        log.debug("remote offer translated SDP:\n%s", sdp)
        self.engine.set_remote_description(sdp, sdp_type="offer")
        self._remote_desc_set = True
        # Flush any candidates that arrived before the description.
        for line in self._pending_remote_candidates:
            self.engine.add_remote_candidate(line)
        self._pending_remote_candidates.clear()
        self.engine.create_answer(self._send_session_accept)

    def _send_session_accept(self, sdp_text: str) -> None:
        log.info("local answer SDP ready (%d bytes)", len(sdp_text))
        log.debug("answer SDP:\n%s", sdp_text)
        try:
            kw = sdp_to_jingle_description(sdp_text)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not translate SDP to Jingle: %s", exc)
            return
        iq = jingle_mod.session_accept(
            to_jid=self.peer_jid,
            responder=self.own_jid,
            sid=self.sid,
            **kw,
        )
        self._xmpp.send_iq(iq)

    # -- inbound stanzas from the peer ---------------------------------

    def handle_session_accept(self, parsed: dict) -> None:
        contents = parsed.get("contents") or []
        if not contents:
            return
        sdp = jingle_content_to_sdp(contents[0], role="answer")
        log.debug("remote answer translated SDP:\n%s", sdp)
        self.engine.set_remote_description(sdp, sdp_type="answer")
        self._remote_desc_set = True
        for line in self._pending_remote_candidates:
            self.engine.add_remote_candidate(line)
        self._pending_remote_candidates.clear()

    def handle_transport_info(self, parsed: dict) -> None:
        contents = parsed.get("contents") or []
        for content in contents:
            transport = content.get("transport") or {}
            for cand in transport.get("candidates") or []:
                line = jingle_mod.jingle_candidate_to_sdp(cand)
                if self._remote_desc_set:
                    self.engine.add_remote_candidate(line)
                else:
                    self._pending_remote_candidates.append(line)

    def handle_session_terminate(self, _parsed: dict) -> None:
        log.info("peer terminated session")
        self.shutdown()

    # -- our side of termination ---------------------------------------

    def send_session_terminate(self, reason: str = "success") -> None:
        try:
            iq = jingle_mod.session_terminate(
                to_jid=self.peer_jid, sid=self.sid, reason=reason)
            self._xmpp.send_iq(iq)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._engine_ready and self.engine is not None:
            self.engine.stop()
            self._engine_ready = False

    # -- candidate trickle ---------------------------------------------

    def _on_local_candidate(self, _engine, sdp_line: str):
        cand = jingle_mod.sdp_candidate_to_jingle(sdp_line)
        if cand is None:
            return
        iq = jingle_mod.transport_info(
            to_jid=self.peer_jid, sid=self.sid, candidates=[cand])
        self._xmpp.send_iq(iq)

    # -- engine lifecycle (TURN-aware) ---------------------------------

    def _with_engine(self, work) -> None:
        """Run `work()` once the AudioEngine is up.

        We discover the TURN URI via XEP-0215 on first use, then build
        the engine, then call work. If the engine is already up, just
        run work immediately.
        """
        if self._engine_ready:
            work()
            return
        self._on_engine_ready.append(work)
        if self.engine is not None:
            # Engine build is in flight from a prior call — work will
            # run once it's ready.
            return
        # Kick off TURN discovery against the server's domain.
        server = self.own_jid.split("/", 1)[0].split("@", 1)[-1] or "rob.land"
        fetch_turn_uri(self._xmpp, server, self._on_turn_resolved)

    def _on_turn_resolved(self, turn_uri):
        log.info("TURN URI for engine: %s",
                 turn_uri if turn_uri else "(none — relying on STUN only)")
        self.engine = AudioEngine(turn_uri=turn_uri)
        self.engine.connect("local-candidate", self._on_local_candidate)
        if not self.engine.start(
                "incoming" if self.incoming else "outgoing"):
            log.warning("audio engine failed to start")
            return
        self._engine_ready = True
        for work in self._on_engine_ready:
            try:
                work()
            except Exception as exc:  # noqa: BLE001
                log.exception("deferred jingle work failed: %s", exc)
        self._on_engine_ready.clear()
