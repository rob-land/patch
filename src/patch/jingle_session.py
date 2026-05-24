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
        # Buffered LOCAL candidates emitted by webrtcbin during pre-warm,
        # before our session-initiate / session-accept has been sent.
        # They can't be transport-info'd yet because the peer doesn't
        # know the session — and they'd carry empty ufrag/pwd anyway.
        # Flushed by _flush_pending_local once we have our local SDP.
        self._pending_local_candidates: list[str] = []
        self._local_sdp_sent = False
        # Deferred work that needs to run AFTER the engine is built.
        self._on_engine_ready: list = []

    # -- pre-warm -------------------------------------------------------

    def prewarm(self) -> None:
        """Build the audio engine ahead of needing it.

        Triggers TURN discovery (cache hit on a warm XmppClient), then
        builds webrtcbin and starts ICE gathering. Called as soon as we
        commit to a call attempt (JMI propose sent for outgoing, JMI
        accept clicked for incoming) so the engine setup overlaps with
        the JMI round-trip rather than running serially after it. The
        Cheogram Android client gets a similar win via its PRE_APPROVED
        pipelining.
        """
        self._with_engine(lambda: None)

    # -- outgoing flow --------------------------------------------------

    def start_outgoing(self) -> None:
        self._with_engine(lambda: self.engine.create_offer(
            self._send_session_initiate))

    def _send_session_initiate(self, sdp_text: str) -> None:
        log.info("local offer SDP ready (%d bytes):\n%s",
                 len(sdp_text), sdp_text)
        try:
            kw = sdp_to_jingle_description(sdp_text)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not translate SDP to Jingle: %s", exc)
            return
        self._local_ice_ufrag  = kw.get("ice_ufrag", "")
        self._local_ice_pwd    = kw.get("ice_pwd", "")
        self._local_dtls_fp    = kw.get("dtls_fingerprint", "")
        self._local_dtls_hash  = kw.get("dtls_hash", "sha-256")
        self._local_dtls_setup = kw.get("dtls_setup", "")
        iq = jingle_mod.session_initiate(
            to_jid=self.peer_jid,
            initiator=self.own_jid,
            sid=self.sid,
            **kw,
        )
        self._xmpp.send_iq(iq)
        # Now that ufrag/pwd/fp are stamped on the session, any candidates
        # that arrived during pre-warm are safe to trickle.
        self._flush_pending_local()

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
        log.info("remote offer translated SDP (%d bytes):\n%s", len(sdp), sdp)
        self.engine.set_remote_description(sdp, sdp_type="offer")
        self._remote_desc_set = True
        # Flush any candidates that arrived before the description.
        for line in self._pending_remote_candidates:
            self.engine.add_remote_candidate(line)
        self._pending_remote_candidates.clear()
        self.engine.create_answer(self._send_session_accept)

    def _send_session_accept(self, sdp_text: str) -> None:
        log.info("local answer SDP ready (%d bytes):\n%s",
                 len(sdp_text), sdp_text)
        try:
            kw = sdp_to_jingle_description(sdp_text)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not translate SDP to Jingle: %s", exc)
            return
        # Cache for transport-info enrichment — cheogram requires
        # ufrag/pwd/fingerprint on every trickled <transport>.
        self._local_ice_ufrag  = kw.get("ice_ufrag", "")
        self._local_ice_pwd    = kw.get("ice_pwd", "")
        self._local_dtls_fp    = kw.get("dtls_fingerprint", "")
        self._local_dtls_hash  = kw.get("dtls_hash", "sha-256")
        self._local_dtls_setup = kw.get("dtls_setup", "")
        iq = jingle_mod.session_accept(
            to_jid=self.peer_jid,
            responder=self.own_jid,
            sid=self.sid,
            **kw,
        )
        self._xmpp.send_iq(iq)
        # Same as session-initiate path: pre-warm-gathered candidates
        # are now safe to trickle now that the peer has our ufrag/pwd.
        self._flush_pending_local()

    # -- inbound stanzas from the peer ---------------------------------

    def handle_session_accept(self, parsed: dict) -> None:
        contents = parsed.get("contents") or []
        if not contents:
            log.warning("session-accept has no contents")
            return
        if self.engine is None:
            log.warning("session-accept arrived before engine ready")
            return
        sdp = jingle_content_to_sdp(contents[0], role="answer")
        log.info("session-accept: %d byte SDP applied", len(sdp))
        log.debug("remote answer SDP:\n%s", sdp)
        self.engine.set_remote_description(sdp, sdp_type="answer")
        self._remote_desc_set = True
        for line in self._pending_remote_candidates:
            self.engine.add_remote_candidate(line)
        self._pending_remote_candidates.clear()

    def handle_transport_info(self, parsed: dict) -> None:
        contents = parsed.get("contents") or []
        added = pended = 0
        for content in contents:
            transport = content.get("transport") or {}
            for cand in transport.get("candidates") or []:
                line = jingle_mod.jingle_candidate_to_sdp(cand)
                if self._remote_desc_set and self.engine is not None:
                    self.engine.add_remote_candidate(line)
                    added += 1
                else:
                    self._pending_remote_candidates.append(line)
                    pended += 1
        if added or pended:
            log.info("transport-info: %d added, %d pending", added, pended)

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

    def send_dtmf(self, digit: str) -> bool:
        """User pressed a dialpad digit during an active call."""
        if self.engine is None or not self._engine_ready:
            log.debug("dtmf %s: engine not ready", digit)
            return False
        return self.engine.send_dtmf(digit)

    def set_mic_mute(self, muted: bool) -> None:
        if self.engine is None:
            return
        self.engine.set_mic_mute(muted)

    # -- candidate trickle ---------------------------------------------

    def _on_local_candidate(self, _engine, sdp_line: str):
        # While pre-warming we'll often gather candidates before we've
        # had a chance to send session-initiate / session-accept. The
        # peer doesn't know the session yet, and our ufrag/pwd/fp
        # aren't extracted from local SDP until the offer/answer fires.
        # Buffer here and flush from _flush_pending_local.
        if not self._local_sdp_sent:
            self._pending_local_candidates.append(sdp_line)
            return
        self._send_trickle_candidate(sdp_line)

    def _send_trickle_candidate(self, sdp_line: str) -> None:
        cand = jingle_mod.sdp_candidate_to_jingle(sdp_line)
        if cand is None:
            return
        # Trickled candidates MUST carry the same ufrag/pwd (and
        # ideally the fingerprint) as our session-initiate / session-
        # accept's transport — otherwise the peer drops them.
        iq = jingle_mod.transport_info(
            to_jid=self.peer_jid, sid=self.sid, candidates=[cand],
            ice_ufrag=getattr(self, "_local_ice_ufrag", ""),
            ice_pwd=getattr(self, "_local_ice_pwd", ""),
            dtls_fp=getattr(self, "_local_dtls_fp", ""),
            dtls_hash=getattr(self, "_local_dtls_hash", "sha-256"),
            dtls_setup=getattr(self, "_local_dtls_setup", ""))
        self._xmpp.send_iq(iq)

    def _flush_pending_local(self) -> None:
        self._local_sdp_sent = True
        for line in self._pending_local_candidates:
            self._send_trickle_candidate(line)
        self._pending_local_candidates.clear()

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
        # XmppClient pre-fetches the TURN URIs on login and caches them
        # for ~30 min, so this is usually a synchronous hit. Falls back
        # to a fresh disco IQ when the cache is empty or expired. The
        # list carries UDP/TCP/TURNS in preference order so ICE can
        # relay via TCP when UDP/3478 is blocked.
        self._xmpp.get_turn_uris(self._on_turn_resolved)

    def _on_turn_resolved(self, turn_uris):
        log.info("TURN URIs for engine: %s",
                 turn_uris if turn_uris else "(none — relying on STUN only)")
        self.engine = AudioEngine(turn_uris=turn_uris)
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
