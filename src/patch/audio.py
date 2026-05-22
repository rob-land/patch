"""GStreamer webrtcbin audio engine + Jingle ↔ SDP translation.

Codec choice: **PCMU** (G.711 µ-law). Cheogram/JMP is a PSTN bridge
and its session-initiate offers PCMU/G722/telephone-event — never
Opus. PCMU is the universal SIP codec and is what makes the gateway
actually connect the SIP side. We do not try to negotiate Opus.

We delegate ICE, DTLS-SRTP, and RTP to webrtcbin. Our job is to:

  1. Build a pipeline that captures local audio (PCMU, 8kHz mono) and
     plays back the remote stream.
  2. Drive the WebRTC negotiation state machine: create-offer / answer,
     set-local-description, set-remote-description.
  3. Translate the SDP produced by webrtcbin into the Jingle XML the
     cheogram/JMP gateway expects, and vice versa.
  4. Forward local ICE candidates to the peer as Jingle transport-info
     stanzas, and inject inbound candidates back into webrtcbin.

This is enough for one-to-one audio calls. Video, screen-sharing, DTMF
output, multi-stream BUNDLE, and Opus negotiation are not implemented.
"""

from __future__ import annotations

import logging
import uuid

from gi.repository import GLib, GObject, Gst, GstSdp, GstWebRTC

log = logging.getLogger(__name__)

_GST_INITIALISED = False

# Codecs we'll accept from the peer's offer, in order of preference.
# PCMU is what cheogram/JMP serves; we keep G722 as a fallback because
# it's also a common SIP codec, but we only have a pipeline path for
# PCMU at the moment.
_ACCEPTED_CODECS = {"PCMU", "G722"}
# RFC 4733 DTMF events — we don't generate them yet but we accept the
# payload type so the peer's SDP gets faithfully echoed back.
_DTMF_CODEC = "telephone-event"


def _ensure_gst() -> bool:
    global _GST_INITIALISED
    if _GST_INITIALISED:
        return True
    Gst.init(None)
    if not Gst.ElementFactory.find("webrtcbin"):
        log.warning("webrtcbin GStreamer element missing; calls have no audio")
        return False
    _GST_INITIALISED = True
    return True


# ──────────────────────────────────────────────────────────────────────
# Pipeline
# ──────────────────────────────────────────────────────────────────────

def _build_webrtcbin(turn_uri: str | None = None) -> Gst.Element:
    """Construct the webrtcbin element by hand.

    NOTE: We used to do this via Gst.parse_launch("webrtcbin name=...
    ...") but parse_launch returns the inner element directly when the
    description has only one element — there's no enclosing GstPipeline
    to call get_by_name on. Build the pipeline explicitly.
    """
    el = Gst.ElementFactory.make("webrtcbin", "webrtcbin")
    if el is None:
        raise RuntimeError("webrtcbin factory missing")
    el.set_property("bundle-policy", 3)  # max-bundle
    el.set_property("stun-server", "stun://stun.l.google.com:19302")
    if turn_uri:
        # webrtcbin accepts turn-server=turn://user:cred@host:port for a
        # single TURN. chat.rob.land's coturn presents UDP at this host,
        # which is enough for the basic case.
        el.set_property("turn-server", turn_uri)
    return el


# ──────────────────────────────────────────────────────────────────────
# SDP wrapping
# ──────────────────────────────────────────────────────────────────────
#
# We get an SDP string out of webrtcbin and have to translate it into a
# Jingle <content> with <description> + <transport>. The mapping is
# spelled out in XEP-0167 §10. We do enough of it for one audio content
# with PCMU + DTLS-SRTP + ICE-UDP, which is what cheogram speaks.

def sdp_to_jingle_description(sdp_text: str) -> dict:
    """Extract Jingle description + transport bits from an SDP string.

    Returns a dict with: payload_types, ice_ufrag, ice_pwd,
    dtls_fingerprint, dtls_hash, dtls_setup. The caller plugs these
    into jingle.session_initiate / session_accept.
    """
    # Trim to the first media block (m=audio …). cheogram doesn't expect
    # multiple media; webrtcbin shouldn't emit any since we only add one
    # transceiver.
    m_idx = sdp_text.find("m=")
    if m_idx == -1:
        raise ValueError("SDP has no m= line")
    session = sdp_text[:m_idx]
    media = sdp_text[m_idx:]

    payload_types = _parse_payload_types(media)
    ice_ufrag, ice_pwd = _parse_ice(media) or _parse_ice(session) or ("", "")
    fp_hash, fp_value, fp_setup = (_parse_fingerprint(media)
                                    or _parse_fingerprint(session)
                                    or ("sha-256", "", "actpass"))
    return {
        "payload_types":    payload_types,
        "ice_ufrag":        ice_ufrag,
        "ice_pwd":          ice_pwd,
        "dtls_fingerprint": fp_value,
        "dtls_hash":        fp_hash,
        "dtls_setup":       fp_setup,
    }


def jingle_content_to_sdp(content: dict, *, role: str) -> str:
    """Build the SDP webrtcbin will accept as a remote description.

    `role` is "offer" or "answer". For "offer" we mark the m= line as
    sendrecv; for "answer" we mirror whatever the peer offered.

    `content` is one entry from jingle.parse_jingle()['contents'].
    """
    desc = content["description"] or {}
    transport = content["transport"] or {}
    fp = transport.get("fingerprint") or {}
    pts = desc.get("payload_types") or []
    # Filter to codecs we can actually handle, but keep the peer's
    # ordering so the m= line lists the audio PT first.
    usable = [p for p in pts if (p.get("name") or "").upper() in
              _ACCEPTED_CODECS or p.get("name") == _DTMF_CODEC]
    if not usable:
        # Cheogram's canonical PSTN profile — assume the peer just
        # forgot to advertise.
        usable = [{"id": "0", "name": "PCMU", "clockrate": "8000", "channels": "1"}]
    pt_ids = " ".join(p["id"] for p in usable)

    lines = [
        "v=0",
        f"o=- {uuid.uuid4().int >> 96} 2 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE 0",
        "a=msid-semantic:WMS *",
        f"m=audio 9 UDP/TLS/RTP/SAVPF {pt_ids}",
        "c=IN IP4 0.0.0.0",
        "a=rtcp:9 IN IP4 0.0.0.0",
        f"a=ice-ufrag:{transport.get('ufrag') or ''}",
        f"a=ice-pwd:{transport.get('pwd') or ''}",
        "a=ice-options:trickle",
    ]
    if fp.get("value"):
        lines.append(f"a=fingerprint:{fp.get('hash', 'sha-256')} {fp['value']}")
    setup = fp.get("setup") or "actpass"
    lines.append(f"a=setup:{setup}")
    lines.append("a=mid:0")
    lines.append("a=sendrecv")
    lines.append("a=rtcp-mux")
    for p in usable:
        rate = p.get("clockrate", "8000")
        channels = p.get("channels", "1")
        lines.append(f"a=rtpmap:{p['id']} {p['name']}/{rate}/{channels}")
        params = p.get("parameters") or {}
        if params:
            fmtp = ";".join(f"{k}={v}" for k, v in params.items())
            lines.append(f"a=fmtp:{p['id']} {fmtp}")
    return "\r\n".join(lines) + "\r\n"


# ──────────────────────────────────────────────────────────────────────
# Low-level parsers
# ──────────────────────────────────────────────────────────────────────

def _parse_payload_types(media: str) -> list[dict]:
    """Each m=audio line carries the supported payload type ids; rtpmap +
    fmtp lines describe them. We keep PCMU/G722/telephone-event — that's
    everything cheogram offers — and drop anything else (Opus etc.) so
    the negotiated set actually intersects with the gateway."""
    rtpmaps = {}     # id -> (name, clockrate, channels)
    fmtps   = {}     # id -> {param: value}
    for line in media.splitlines():
        if line.startswith("a=rtpmap:"):
            try:
                head, body = line[len("a=rtpmap:"):].split(" ", 1)
            except ValueError:
                continue
            parts = body.split("/")
            name = parts[0]
            clock = parts[1] if len(parts) > 1 else "0"
            channels = parts[2] if len(parts) > 2 else "1"
            rtpmaps[head] = (name, clock, channels)
        elif line.startswith("a=fmtp:"):
            try:
                head, body = line[len("a=fmtp:"):].split(" ", 1)
            except ValueError:
                continue
            params = {}
            for kv in body.split(";"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    params[k.strip()] = v.strip()
            fmtps[head] = params

    pts = []
    for pt_id, (name, clock, channels) in rtpmaps.items():
        upper = name.upper()
        if upper not in _ACCEPTED_CODECS and name != _DTMF_CODEC:
            continue
        d = {"id": pt_id, "name": name, "clockrate": clock, "channels": channels}
        if fmtps.get(pt_id):
            d["parameters"] = fmtps[pt_id]
        pts.append(d)
    if not pts:
        # Fall back to a canonical PCMU + DTMF entry.
        pts = [
            {"id": "0",   "name": "PCMU",            "clockrate": "8000", "channels": "1"},
            {"id": "101", "name": "telephone-event", "clockrate": "8000", "channels": "1"},
        ]
    return pts


def _parse_ice(block: str) -> tuple[str, str] | None:
    ufrag = pwd = None
    for line in block.splitlines():
        if line.startswith("a=ice-ufrag:"):
            ufrag = line[len("a=ice-ufrag:"):].strip()
        elif line.startswith("a=ice-pwd:"):
            pwd = line[len("a=ice-pwd:"):].strip()
    if ufrag and pwd:
        return ufrag, pwd
    return None


def _parse_fingerprint(block: str) -> tuple[str, str, str] | None:
    fp_hash = fp_value = setup = None
    for line in block.splitlines():
        if line.startswith("a=fingerprint:"):
            parts = line[len("a=fingerprint:"):].split(" ", 1)
            if len(parts) == 2:
                fp_hash, fp_value = parts[0].strip(), parts[1].strip()
        elif line.startswith("a=setup:"):
            setup = line[len("a=setup:"):].strip()
    if fp_hash and fp_value:
        return fp_hash, fp_value, setup or "actpass"
    return None


# ──────────────────────────────────────────────────────────────────────
# Audio engine
# ──────────────────────────────────────────────────────────────────────

class AudioEngine(GObject.Object):
    """One in-flight WebRTC audio session.

    Lifecycle:
      e = AudioEngine(); e.start(direction="outgoing")
      e.create_offer(callback=on_sdp_offer)   # for outgoing
      # ... wrap SDP in Jingle, send ...
      e.set_remote_description(peer_sdp)      # peer's answer arrives
      e.add_remote_candidate(sdp_line)        # peer's trickle ICE

    Or for incoming:
      e.start(direction="incoming")
      e.set_remote_description(peer_offer_sdp)
      e.create_answer(callback=on_sdp_answer)
      # send Jingle session-accept, then trickle ICE

    Signals:
      local-candidate(sdp_candidate_line: str)
        Emit whenever webrtcbin produces a new ICE candidate. Caller
        wraps in a Jingle transport-info.
    """

    __gtype_name__ = "PatchAudioEngine"

    __gsignals__ = {
        "local-candidate":  (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "ice-state-change": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(self, turn_uri: str | None = None):
        super().__init__()
        self._pipeline: Gst.Pipeline | None = None
        self._webrtc:   Gst.Element  | None = None
        self._turn_uri = turn_uri
        self._stats_source: int = 0

    def start(self, direction: str) -> bool:
        """Build the pipeline and add the audio transceiver."""
        if not _ensure_gst():
            return False
        if self._pipeline is not None:
            return True
        try:
            self._webrtc = _build_webrtcbin(self._turn_uri)
        except (GLib.Error, RuntimeError) as exc:
            log.warning("failed to build webrtcbin: %s", exc)
            return False
        self._pipeline = Gst.Pipeline.new("call-pipeline")
        self._pipeline.add(self._webrtc)

        # Audio capture: pulsesrc → 8kHz mono → mulawenc → rtppcmupay → webrtcbin.
        # rtppcmupay's pt property is 0 (the universal PCMU PT). The
        # output already carries application/x-rtp,media=audio,
        # encoding-name=PCMU caps which webrtcbin uses to register a
        # sendrecv audio transceiver.
        # NOTE: gst_parse_bin_from_description does NOT accept a
        # trailing caps-filter string (unlike gst_parse_launch) — the
        # bin parser wants a real element at the end so it can ghost
        # the src pad. A trailing "application/x-rtp,..." gets parsed
        # as a missing element. Don't add one back.
        mic = Gst.parse_bin_from_description(
            "pulsesrc ! audioconvert ! audioresample "
            "! audio/x-raw,rate=8000,channels=1 "
            "! mulawenc ! rtppcmupay pt=0",
            True)
        self._pipeline.add(mic)
        if not mic.link(self._webrtc):
            log.warning("failed to link mic into webrtcbin")
        # Playback: pad-added → rtppcmudepay → mulawdec → pulsesink.
        self._webrtc.connect("pad-added", self._on_pad_added)
        self._webrtc.connect("on-ice-candidate", self._on_ice_candidate)
        # All three webrtcbin state machines are worth watching when
        # debugging "audio doesn't connect": ICE (host candidates +
        # connectivity checks), PeerConnection (the overall negotiation),
        # and Signaling (offer/answer exchange).
        self._webrtc.connect("notify::ice-connection-state",
                             self._on_ice_state_change)
        self._webrtc.connect("notify::ice-gathering-state",
                             self._on_state_notify)
        self._webrtc.connect("notify::connection-state",
                             self._on_state_notify)
        self._webrtc.connect("notify::signaling-state",
                             self._on_state_notify)

        # Pipe GStreamer bus messages (errors / warnings / EOS) into the
        # Python log so a failed mic source or RTP issue doesn't die
        # silently inside the pipeline.
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        ret = self._pipeline.set_state(Gst.State.PLAYING)
        log.info("audio engine started (%s, gst state=%s)",
                 direction, ret.value_nick)
        # Periodically dump webrtcbin's stats so we can see whether RTP
        # is actually flowing in either direction. 3s cadence is enough
        # to catch the first inbound packet without log spam.
        self._stats_source = GLib.timeout_add_seconds(3, self._tick_stats)
        return True

    def stop(self) -> None:
        if self._stats_source:
            GLib.source_remove(self._stats_source)
            self._stats_source = 0
        if self._pipeline is None:
            return
        self._pipeline.set_state(Gst.State.NULL)
        self._pipeline = None
        self._webrtc = None
        log.info("audio engine stopped")

    def _tick_stats(self) -> bool:
        if self._webrtc is None:
            return False
        self._dump_ice_stats()
        return True  # keep firing

    # -- offer / answer ------------------------------------------------

    def create_offer(self, callback) -> None:
        """callback(sdp_text: str)"""
        self._webrtc.emit(
            "create-offer", None,
            Gst.Promise.new_with_change_func(self._on_offer_created,
                                              callback, None))

    def create_answer(self, callback) -> None:
        self._webrtc.emit(
            "create-answer", None,
            Gst.Promise.new_with_change_func(self._on_answer_created,
                                              callback, None))

    def set_remote_description(self, sdp_text: str, sdp_type: str = "answer") -> None:
        """sdp_type ∈ {"offer", "answer"} as per WebRTC."""
        sdp = self._parse_sdp(sdp_text)
        if sdp is None:
            return
        desc = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.OFFER if sdp_type == "offer"
            else GstWebRTC.WebRTCSDPType.ANSWER,
            sdp)
        self._webrtc.emit(
            "set-remote-description", desc,
            Gst.Promise.new_with_change_func(lambda *_: None, None, None))

    def add_remote_candidate(self, sdp_line: str, mline_index: int = 0) -> None:
        """SDP-formatted `candidate:…` line (no leading `a=`)."""
        if sdp_line.startswith("a="):
            sdp_line = sdp_line[2:]
        self._webrtc.emit("add-ice-candidate", mline_index, sdp_line)

    # -- internal callbacks --------------------------------------------

    def _on_offer_created(self, promise, callback, _user):
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        promise = Gst.Promise.new_with_change_func(
            lambda *_: None, None, None)
        self._webrtc.emit("set-local-description", offer, promise)
        sdp_text = offer.sdp.as_text()
        GLib.idle_add(callback, sdp_text)

    def _on_answer_created(self, promise, callback, _user):
        reply = promise.get_reply()
        answer = reply.get_value("answer")
        promise = Gst.Promise.new_with_change_func(
            lambda *_: None, None, None)
        self._webrtc.emit("set-local-description", answer, promise)
        sdp_text = answer.sdp.as_text()
        GLib.idle_add(callback, sdp_text)

    def _on_pad_added(self, _bin, pad: Gst.Pad):
        direction = pad.get_direction().value_nick
        try:
            caps = pad.get_current_caps() or pad.query_caps(None)
            caps_str = caps.to_string() if caps else "<no caps>"
        except Exception as exc:  # noqa: BLE001
            caps_str = f"<err {exc}>"
        log.info("webrtc pad-added: name=%s direction=%s caps=%s",
                 pad.get_name(), direction, caps_str)
        if pad.get_direction() != Gst.PadDirection.SRC:
            return
        sink = Gst.parse_bin_from_description(
            "rtppcmudepay ! mulawdec ! audioconvert ! audioresample ! pulsesink",
            True)
        self._pipeline.add(sink)
        sink.sync_state_with_parent()
        result = pad.link(sink.get_static_pad("sink"))
        log.info("playback bin linked: %s", result.value_nick)

    def _on_ice_candidate(self, _bin, mline_index: int, candidate: str):
        log.debug("ice candidate (m=%d): %s", mline_index, candidate)
        self.emit("local-candidate", candidate)

    def _on_ice_state_change(self, *_):
        state = self._webrtc.get_property("ice-connection-state")
        log.info("ice connection state -> %s", state.value_nick)
        if state.value_nick == "failed":
            self._dump_ice_stats()
        self.emit("ice-state-change", int(state))

    def _dump_ice_stats(self) -> None:
        """Pull webrtcbin's stats so we can see WHY ICE failed.

        Logs every local-candidate, remote-candidate, and candidate-
        pair entry — these include selected pair, state (succeeded /
        failed / in-progress), and the IP/port the agent was checking.
        """
        try:
            promise = Gst.Promise.new_with_change_func(
                self._on_stats_ready, None, None)
            self._webrtc.emit("get-stats", None, promise)
        except Exception as exc:  # noqa: BLE001
            log.warning("get-stats failed: %s", exc)

    def _on_stats_ready(self, promise, *_):
        try:
            stats = promise.get_reply()
        except Exception as exc:  # noqa: BLE001
            log.warning("stats reply unavailable: %s", exc)
            return
        if stats is None:
            log.info("ice stats: <empty>")
            return
        n = stats.n_fields()
        # Aggregate a one-line summary plus per-entry details. Each
        # field on the top-level stats structure is itself a structure
        # whose name is the stats *kind* (candidate-pair, transport,
        # inbound-rtp, outbound-rtp, etc.).
        summary = []
        details = []
        for i in range(n):
            name = stats.nth_field_name(i)
            sub  = stats.get_value(name)
            if not hasattr(sub, "to_string"):
                continue
            kind = sub.get_name() if hasattr(sub, "get_name") else "?"
            interesting = any(k in kind for k in
                              ("candidate", "pair", "ice",
                               "transport", "inbound", "outbound"))
            if not interesting:
                continue
            # Pull common fields into the one-liner.
            extra = []
            for key in ("packets-sent", "packets-received",
                        "bytes-sent", "bytes-received", "state",
                        "selected"):
                try:
                    val = sub.get_value(key)
                except Exception:  # noqa: BLE001
                    val = None
                if val is not None:
                    extra.append(f"{key}={val}")
            summary.append(f"{kind}({','.join(extra)})" if extra else kind)
            details.append(f"  {kind}: {sub.to_string()}")
        log.info("stats: %s", " | ".join(summary) if summary else "<none>")
        for line in details:
            log.info(line)

    def _on_state_notify(self, _bin, pspec):
        try:
            state = self._webrtc.get_property(pspec.name)
            nick = state.value_nick if hasattr(state, "value_nick") else state
        except Exception as exc:  # noqa: BLE001
            nick = f"<unknown: {exc}>"
        log.info("webrtc %s -> %s", pspec.name, nick)

    def _on_bus_message(self, _bus, msg):
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            log.warning("gst error from %s: %s (%s)",
                        msg.src.get_name() if msg.src else "?",
                        err.message, debug)
        elif t == Gst.MessageType.WARNING:
            err, debug = msg.parse_warning()
            log.info("gst warning from %s: %s (%s)",
                     msg.src.get_name() if msg.src else "?",
                     err.message, debug)
        elif t == Gst.MessageType.EOS:
            log.info("gst eos")

    def _parse_sdp(self, sdp_text: str) -> GstSdp.SDPMessage | None:
        result, sdp = GstSdp.SDPMessage.new()
        if result != GstSdp.SDPResult.OK:
            log.warning("failed to allocate SDPMessage")
            return None
        sdp_bytes = sdp_text.encode("utf-8")
        result = GstSdp.sdp_message_parse_buffer(sdp_bytes, sdp)
        if result != GstSdp.SDPResult.OK:
            log.warning("failed to parse remote SDP")
            return None
        return sdp
