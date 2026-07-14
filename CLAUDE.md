# Patch — CLAUDE.md

## What this project is

Patch is a JMP.chat-first phone client for GNOME mobile. The user types
phone numbers, sees SMS conversations, hears voicemails — XMPP is the
transport, hidden as an implementation detail. The push path is
self-hosted: a companion Prosody module (`mod_cloud_notify_unifiedpush`)
encrypts XEP-0357 push notifications per RFC 8291 and ships them via
UnifiedPush + a self-hosted ntfy to KUnifiedPush on the phone, which
hands them off to this app over D-Bus.

App ID: `land.rob.patch`. License: GPL-3.0-or-later.

## Phase status

- **Phase 0** ✅ Three-tab AdwViewSwitcher skeleton, dialpad UI, libsecret
  credential storage, JID ↔ E.164 round-trip for cheogram gateways.
- **Phase 1** ✅ XMPP messaging via `nbxmpp` (GLib mainloop-native, no
  asyncio worker thread). Conversation list + thread view + compose.
  Cheogram group-SMS body parsing. SQLite cache for offline display.
- **Phase 1.5** ✅ Reconnection with exponential backoff + Adw.Banner
  status surface, desktop notifications on inbound messages, MAM
  catch-up on connect (on by default, paginated via RSM in pages of
  `MAM_PAGE`=20 to sidestep the nbxmpp 7.2 large-batch parse-finished
  bug; resumes from the latest cached message minus `MAM_RESUME_OVERLAP`
  with xmpp_id dedup; `PATCH_MAM_CATCHUP=0` to opt out), Send Message
  shortcut on the dialer for new conversations.
- **Phase 2** ✅ UnifiedPush receiver — full end-to-end including the
  cold-start activation race fix. `org.unifiedpush.Connector1` D-Bus
  service, distributor discovery + registration, P-256 keypair in
  libsecret, XEP-0357 enable IQ with our publish_options form, RFC
  8291 decryption, D-Bus activation `.service`. Verified against
  chat.rob.land → ntfy.kde.org → KUnifiedPush → flatpak wake from
  cold in ~11s.
- **Phase 3** ✅ Outgoing calls — XEP-0353 JMI propose/proceed/accept/
  reject/retract through `xmpp/client.py`, in-process `CallSession`
  state machine + `Adw.Dialog` call screen. Live-tested end-to-end
  against JMP: propose → proceed/accept (XEP-0353 targets per §6.2) →
  active.
- **Phase 4** ✅ Incoming Jingle audio — bidirectional audio confirmed
  end-to-end against JMP 2026-05-22. `src/patch/xmpp/jingle.py` builds
  + parses XEP-0166/0167/0176/0320; `src/patch/jingle_session.py`
  orchestrates one session; `src/patch/audio.py` drives webrtcbin
  with PCMU (8 kHz mono — cheogram offers no Opus, only the SIP
  toll-grade trio PCMU/G722/telephone-event). Required to get there:
    - `<rtcp-mux/>` advertised in session-accept (without it cheogram
      allocates separate component=2 candidates for RTCP, ICE never
      converges on the missing pair)
    - real nbxmpp dispatcher handler for iq/set in NS_JINGLE that
      raises NodeProcessed (just listening to 'stanza-received' lets
      the default handler send feature-not-implemented alongside our
      iq-result, confusing the gateway)
    - ufrag/pwd/fingerprint on every trickled transport-info
      `<transport>` (peer drops candidates that can't be paired with
      a session)
    - flat element chain on the main pipeline for mic + playback
      (Gst.parse_bin_from_description nests in a sub-bin whose
      segment/live-clock doesn't propagate to webrtcbin; rtpsession
      can't compute running-time and outbound RTP never gets framed)
    - `min-ptime=max-ptime=20_000_000 ns` on rtppcmupay (default
      packetisation follows upstream buffer size; pulsesrc tends to
      feed 10ms chunks, but SIP gateways expect the standard 20ms /
      160-byte PCMU profile)
  Remaining: cold-start to first-audio is ~10s (TURN disco +
  ICE checks + DTLS handshake). The deployment recipe at
  `data/patch-warm-resident.service.example` is the practical fix.
- **Phase 5** ✅ Outgoing calls — JMI propose; if/when audio flows
  it'll use the same engine path as Phase 4.
- **Phase 6** ✅ MMS — inbound XEP-0066 OOB image rendering inline in
  the conversation, outbound attach button → XEP-0363 HTTP upload →
  PUT → send with OOB.
- **Phase 7** ✅ Voicemail — `recent_voicemails()` filter on audio
  extensions, `Adw.ExpanderRow` per voicemail with `Gtk.MediaControls`
  inline streaming via `Gtk.MediaFile.new_for_file(uri)`.
- **Phase 8** Polish — recent-calls list, libfolks contact resolution,
  persisted call log, status banner, real app icon, real Preferences
  dialog, ringer (feedbackd primary + GStreamer fallback), call
  duration timer, progressive dialer formatting, CSS message bubbles,
  compose-new dialog, DTMF during active call (RFC 4733
  telephone-event via rtpdtmfsrc, PT 101).

## Sibling pieces

- `plugin/` — gnome-calls C plugin (libpeas-2 shared module).
  Functionally complete: `CallsProvider` subclass with
  `g_bus_watch_name` for Patch, `CallsOrigin` interface impl with
  `dial`/`call-added`/`call-removed`, `CallsCall` subclass with
  `answer`/`hang_up`/`send_dtmf_tone`/`get_protocol`. Compiles and
  loads on FuriOS FLX1s (gnome-calls 48.1); build requires
  gnome-calls source headers (`-Dcalls_source_dir=`). Patch-side
  D-Bus surface (`calls_dbus.py`) is production-ready.

## Companion server module

`mod_cloud_notify_unifiedpush` lives in a separate private repo and
is deployed by the Selfhost ansible role
(`selfhost/ansible/roles/prosody/`). It loads on `chat.rob.land` and
shares the RFC 8291 wire format with the `push/decrypt.py` here; both
halves pass the RFC 8291 §5 test vector (run `tests/test_decrypt.py`
in this repo, and `tests/test_rfc8291.lua` in the prosody module repo).

## Code quality

A core goal is well-structured, readable code that follows idiomatic Python (PEP 8) and GNOME / libadwaita conventions; the cohort-shared [`STYLE_GUIDE.md`](STYLE_GUIDE.md) layers on top. When existing code doesn't meet that bar, refactor rather than perpetuate the pattern.

## Before making changes

Read [`STYLE_GUIDE.md`](STYLE_GUIDE.md) first when touching any of:

- Meson build files, the Flatpak manifest, or `requirements.txt`
- Anything under `data/ui/` or `data/icons/`
- New top-level Python files, or new modules under `src/<pkg>/`
- Imports — especially `import gi` / `gi.require_version`
- New launcher / `.in` substitution targets

The five-project unification (banter, clicker, finlit, jamjar, tonic)
established conventions that drift easily from intuition. A `Stop`
hook in `.claude/settings.json` runs `~/projects/style-check.py` and
will surface violations back at the end of each turn.

## Tech stack

- **Language**: Python 3.10+
- **UI toolkit**: GTK4 + libadwaita (PyGObject), Blueprint (`.blp`)
  templates compiled to `.ui` at build time and bundled via GResource
- **Build system**: Meson + Ninja
- **Packaging**: Flatpak (manifest:
  `build-aux/flatpak/land.rob.patch.json`), GNOME 50 SDK

## Source layout

```
meson.build                     Root build (APP_ID, conf, py_conf, subdirs)
build-aux/flatpak/
  land.rob.patch.json          Flatpak manifest
build-all.sh                    Multi-arch flatpak driver
fix-flatpak-deps.py             Tarball -> wheel patcher
requirements.txt                Python runtime deps
data/
  meson.build
  land.rob.patch.{desktop,metainfo.xml,gschema.xml}*.in
  icons/hicolor/{scalable,symbolic}/apps/land.rob.patch*.svg
  ui/
    meson.build                 blueprint-compiler + gnome.compile_resources
    land.rob.patch.gresource.xml
    *.blp                       Blueprint UI templates
po/
  LINGUAS, POTFILES.in, meson.build
src/patch/
  meson.build
  patch.in                     Launcher (Meson-substituted)
  const.py.in                   Build-time constants
  __init__.py, __main__.py, main.py, window.py
```

## Key conventions

- See [`STYLE_GUIDE.md`](STYLE_GUIDE.md) for the full cross-project
  convention reference (this is a sibling of banter, clicker, finlit,
  jamjar, and tonic).
- UI lives in `data/ui/*.blp`. Don't introduce inline `Gtk.Builder.new_from_string(...)`
  for new templates — author a `.blp`, register it in
  `data/ui/land.rob.patch.gresource.xml`, and use
  `@Gtk.Template(resource_path="/land/rob/patch/ui/<name>.ui")`.
- `gi.require_version` is declared once in `src/patch/patch.in`;
  sub-modules just `from gi.repository import …`.
