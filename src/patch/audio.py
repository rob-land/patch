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

def _call_stream_properties() -> Gst.Structure:
    """Stream-property hints that label our pulsesrc/pulsesink as a phone
    call, so WirePlumber/PipeWire applies the communication routing
    profile (earpiece-preferred on mobile, HSP over A2DP for Bluetooth,
    AEC enabled, ducking of music/notification streams). This is the
    Linux equivalent of Android's ``AudioManager.MODE_IN_COMMUNICATION``
    that Cheogram Android sets at call accept.

    Built via ``from_string`` because PA property keys contain dots,
    which GstStructure's structured-field setters don't always accept;
    the string parser does."""
    return Gst.Structure.from_string(
        'props,media.role=phone,media.name="Patch call"')[0]


def _build_webrtcbin(turn_uris: list[str] | None = None) -> Gst.Element:
    """Construct the webrtcbin element by hand.

    NOTE: We used to do this via Gst.parse_launch("webrtcbin name=...
    ...") but parse_launch returns the inner element directly when the
    description has only one element — there's no enclosing GstPipeline
    to call get_by_name on. Build the pipeline explicitly.

    ``turn_uris`` is an ordered list (UDP → TCP → TURNS). The first
    goes into the convenience ``turn-server`` property; any extras are
    pushed via the ``add-turn-server`` action signal. ICE relay-probes
    all of them so a network blocking UDP/3478 still gets connectivity
    via the TCP fallback.
    """
    el = Gst.ElementFactory.make("webrtcbin", "webrtcbin")
    if el is None:
        raise RuntimeError("webrtcbin factory missing")
    el.set_property("bundle-policy", 3)  # max-bundle
    el.set_property("stun-server", "stun://stun.l.google.com:19302")
    if turn_uris:
        el.set_property("turn-server", turn_uris[0])
        for extra in turn_uris[1:]:
            el.emit("add-turn-server", extra)
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

    def __init__(self, turn_uris: list[str] | None = None):
        super().__init__()
        self._pipeline: Gst.Pipeline | None = None
        self._webrtc:   Gst.Element  | None = None
        self._funnel:   Gst.Element  | None = None
        self._dtmf_src: Gst.Element  | None = None
        self._turn_uris = list(turn_uris or [])
        self._stats_source: int = 0

    def start(self, direction: str) -> bool:
        """Build the pipeline and add the audio transceiver."""
        if not _ensure_gst():
            return False
        if self._pipeline is not None:
            return True
        try:
            self._webrtc = _build_webrtcbin(self._turn_uris)
        except (GLib.Error, RuntimeError) as exc:
            log.warning("failed to build webrtcbin: %s", exc)
            return False
        self._pipeline = Gst.Pipeline.new("call-pipeline")
        self._pipeline.add(self._webrtc)

        # Audio capture chain — built as individual elements directly
        # on the main pipeline (NOT inside a sub-bin). Earlier we used
        # parse_bin_from_description for the mic chain; that worked for
        # element instantiation but a sub-bin's segment / live-clock
        # events don't propagate cleanly across the bin boundary into
        # webrtcbin. rtpsession then complained:
        #     "running time not set, can not create SR for SSRC ..."
        #     "generated empty RTCP messages for all the sources"
        # and no outbound RTP ever shipped (inbound worked fine because
        # webrtcbin's own internal pipeline is unaffected). Building the
        # chain flat fixes that — pulsesrc's clock is elected as the
        # pipeline clock and PTSes flow straight through.
        def _make(factory, name=None, **props):
            el = Gst.ElementFactory.make(factory, name)
            if el is None:
                raise RuntimeError(f"missing GStreamer element: {factory}")
            for k, v in props.items():
                el.set_property(k.replace("_", "-"), v)
            self._pipeline.add(el)
            return el

        try:
            # pulsesrc is intrinsically a live source — it has no
            # is-live property to set (that's audiotestsrc's). Don't
            # pass kwargs here; the live behaviour is baked in.
            # stream-properties tags the PA/PipeWire stream as a phone
            # call so the system applies the communication routing
            # profile (see _call_stream_properties for details).
            src      = _make("pulsesrc",     "mic_src",
                             stream_properties=_call_stream_properties())
            convert  = _make("audioconvert", "mic_conv")
            resample = _make("audioresample","mic_res")
            capsf    = _make("capsfilter",   "mic_caps",
                             caps=Gst.Caps.from_string(
                                 "audio/x-raw,rate=8000,channels=1"))
            encoder  = _make("mulawenc",     "mic_enc")
            # rtppcmupay's default packetisation depends on the upstream
            # buffer size — pulsesrc tends to feed it ~10ms chunks, so
            # without explicit ptime we ship 80-byte / 10ms PCMU packets
            # while cheogram (and every SIP gateway) wants the standard
            # 20ms / 160-byte profile. Pin min/max-ptime to 20ms so the
            # payloader buffers and emits one packet per 160 samples.
            payloader = _make("rtppcmupay",  "mic_pay", pt=0,
                              min_ptime=20_000_000,
                              max_ptime=20_000_000)
            outcaps  = _make("capsfilter",   "mic_outcaps",
                             caps=Gst.Caps.from_string(
                                 "application/x-rtp,media=audio,"
                                 "encoding-name=PCMU,payload=0,"
                                 "clock-rate=8000"))
            # DTMF (RFC 4733). rtpdtmfsrc sits idle until send_dtmf()
            # pushes a dtmf-event upstream — then it injects RTP
            # packets at PT 101 alongside the audio stream. We funnel
            # the two together so both flow through a single webrtcbin
            # sink pad (and therefore a single transceiver with shared
            # SSRC, which is what SIP gateways expect).
            self._funnel = _make("funnel",   "audio_funnel")
            self._dtmf_src = _make("rtpdtmfsrc", "dtmf_src",
                                   pt=101, clock_rate=8000)
            # NOTE: rtpdtmfsrc's pad template uses
            # encoding-name=TELEPHONE-EVENT (uppercase). Caps are
            # case-sensitive, so the SDP-shape lowercase form would
            # silently refuse the link.
            dtmf_caps = _make("capsfilter",  "dtmf_caps",
                              caps=Gst.Caps.from_string(
                                  "application/x-rtp,media=audio,"
                                  "encoding-name=TELEPHONE-EVENT,"
                                  "payload=101,clock-rate=8000"))
        except RuntimeError as exc:
            log.warning("audio engine init: %s", exc)
            return False

        for a, b in ((src, convert), (convert, resample),
                     (resample, capsf), (capsf, encoder),
                     (encoder, payloader), (payloader, outcaps)):
            if not a.link(b):
                log.warning("failed to link %s -> %s",
                            a.get_name(), b.get_name())
                return False
        # Plumb mic + DTMF through the funnel into webrtcbin's request
        # sink_%u pad. webrtcbin reads caps from each input and adds
        # both payload types to the audio transceiver.
        for upstream in (outcaps, dtmf_caps):
            if not upstream.link(self._funnel):
                log.warning("failed to link %s -> funnel",
                            upstream.get_name())
                return False
        if not self._dtmf_src.link(dtmf_caps):
            log.warning("failed to link dtmf src into caps")
            return False
        if not self._funnel.link(self._webrtc):
            log.warning("failed to link audio funnel into webrtcbin")
            return False
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

    # -- DTMF (RFC 4733) -------------------------------------------------

    # GStreamer's rtpdtmfsrc uses 0-9 for digits, 10 for '*', 11 for '#',
    # 12-15 for A-D (per RFC 4733). We map the touch-tone characters
    # the dialer surfaces to those event numbers.
    _DTMF_MAP = {
        "0": 0, "1": 1, "2": 2, "3": 3, "4": 4,
        "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
        "*": 10, "#": 11,
        "A": 12, "B": 13, "C": 14, "D": 15,
    }

    def set_mic_mute(self, muted: bool) -> None:
        """Mute/unmute the mic by toggling pulsesrc's mute property.

        Cheaper than start/stopping the source — pulsesrc keeps the
        stream open with silence, so PipeWire doesn't tear down the
        route or renegotiate AEC mid-call.
        """
        if self._pipeline is None:
            return
        src = self._pipeline.get_by_name("mic_src")
        if src is None:
            return
        src.set_property("mute", bool(muted))
        log.info("mic mute = %s", muted)

    def send_dtmf(self, digit: str, duration_ms: int = 200) -> bool:
        """Send a single RFC 4733 DTMF tone via the rtpdtmfsrc element.

        Returns True if dispatched. Schedules a stop event after the
        configured duration so the tone has a finite length on the
        peer's side; without that, the peer would hear a continuous
        tone until we restart or terminate the session.
        """
        if self._dtmf_src is None or self._funnel is None \
                or self._pipeline is None:
            return False
        digit = digit.upper()
        event_num = self._DTMF_MAP.get(digit)
        if event_num is None:
            log.warning("unknown DTMF digit: %r", digit)
            return False
        # The dtmf-event is a CUSTOM_UPSTREAM event — it must travel
        # from a downstream sink pad UP to rtpdtmfsrc. Pushing it on
        # rtpdtmfsrc's own src pad goes the wrong direction (gst warns
        # "pushing custom-upstream event in wrong direction"). Send it
        # via the funnel's src pad instead, which carries it back
        # upstream to all the funnel's sinks — rtpdtmfsrc included.
        pad = self._funnel.get_static_pad("src")
        if pad is None:
            return False

        def _event(start: bool) -> Gst.Event:
            struct = Gst.Structure.new_empty("dtmf-event")
            struct.set_value("type", 1)          # RTP event
            struct.set_value("number", event_num)
            struct.set_value("method", 1)        # RFC 2833 / 4733
            struct.set_value("volume", 25)       # dB attenuation
            struct.set_value("start", start)
            return Gst.Event.new_custom(
                Gst.EventType.CUSTOM_UPSTREAM, struct)

        if not pad.send_event(_event(True)):
            log.warning("dtmf start event refused for %r", digit)
            return False
        log.info("dtmf: %s (%dms)", digit, duration_ms)

        def _stop() -> bool:
            pad.send_event(_event(False))
            return False  # one-shot
        GLib.timeout_add(duration_ms, _stop)
        return True

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
        # Build playback chain as flat elements (same reason as the mic
        # chain — no sub-bin so segments propagate cleanly).
        depay   = Gst.ElementFactory.make("rtppcmudepay", "pb_depay")
        decoder = Gst.ElementFactory.make("mulawdec",     "pb_dec")
        convert = Gst.ElementFactory.make("audioconvert", "pb_conv")
        resample = Gst.ElementFactory.make("audioresample","pb_res")
        sink    = Gst.ElementFactory.make("pulsesink",    "pb_sink")
        for el in (depay, decoder, convert, resample, sink):
            if el is None:
                log.warning("playback element missing — no inbound audio")
                return
            self._pipeline.add(el)
            el.sync_state_with_parent()
        # Same role tagging as the mic side — see _call_stream_properties.
        sink.set_property("stream-properties", _call_stream_properties())
        for a, b in ((depay, decoder), (decoder, convert),
                     (convert, resample), (resample, sink)):
            if not a.link(b):
                log.warning("playback link failed: %s -> %s",
                            a.get_name(), b.get_name())
                return
        result = pad.link(depay.get_static_pad("sink"))
        log.info("playback chain linked: %s", result.value_nick)

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
