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
  catch-up on connect (off by default — nbxmpp 7.2 parse-finished bug
  on large batches; opt in with `PATCH_MAM_CATCHUP=1`), Send Message
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
  state machine + `Adw.Dialog` call screen. Audio is unwired (no
  Jingle session-initiate yet) — needs xmpp-vala or equivalent.
- **Phase 4** Incoming Jingle audio — not started.
- **Phase 5** ✅ Outgoing calls (signalling only — audio side as above).
- **Phase 6** ✅ MMS — inbound XEP-0066 OOB image rendering inline in
  the conversation, outbound attach button → XEP-0363 HTTP upload →
  PUT → send with OOB.
- **Phase 7** ✅ Voicemail — `recent_voicemails()` filter on audio
  extensions, `Adw.ExpanderRow` per voicemail with `Gtk.MediaControls`
  inline streaming via `Gtk.MediaFile.new_for_file(uri)`.
- **Phase 8** Polish — partial. Recent-calls list in dialer, libfolks
  contact-name resolution, persisted call log, status banner. Still
  TODO: real app icon, proper Preferences dialog (currently aliased
  to Account), DTMF, ringer integration via libfeedback, MAM
  pagination fix.

## Sibling pieces

- `plugin/` — scaffold of a future gnome-calls C plugin (libpeas-2
  shared module). Stubs that compile against `libcalls` on a Phosh
  target; lays out `CallsProvider`/`CallsOrigin`/`CallsCall` and
  documents the `land.rob.patch.Calls1` D-Bus surface Patch will
  expose for it to drive.

## Companion server module

`mod_cloud_notify_unifiedpush` lives at
`/home/rob/projects/xmpp-up/prosody-mod-cloud-notify-unifiedpush/` and
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
