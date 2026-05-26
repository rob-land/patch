# calls-xmpp gnome-calls plugin

A native gnome-calls `CallsProvider` plugin that lets Patch's Jingle
sessions surface in the system call shell (`gnome-calls` on Phosh).
Without this plugin, calls happen inside Patch's own in-process dialog
(`src/patch/dialogs/call_dialog.py`); with it, Patch becomes a real
phone-app citizen — incoming JMI propose triggers Phosh's full-screen
ringer, gnome-calls owns the active-call UI, audio routes through the
standard call-audio category, etc.

## Why this is a separate project

gnome-calls plugins are loaded by `libpeas-2`. Inspecting the engine
init in [`src/calls-plugin-manager.c`][cpm], the project calls
`peas_engine_new()` without enabling the Python loader — so plugins
must be C (or Vala) shared modules. Patch itself stays Python; this
sub-project is the only piece that needs C.

The plugin talks to Patch over D-Bus. Patch exposes
`land.rob.patch.Calls1` on the session bus for placing outgoing calls
and receiving JMI notifications; the plugin translates between that and
gnome-calls' `CallsProvider` / `CallsOrigin` / `CallsCall` GObject
hierarchy.

[cpm]: https://gitlab.gnome.org/GNOME/calls/-/blob/main/src/calls-plugin-manager.c

## Status

Functionally complete — not yet build-tested against real `libcalls`
headers on a Phosh target. The three files compile-check on a host with
GLib/GTK4/libpeas, but `libcalls` is only packaged for phone images
(postmarketOS, Mobian, etc.).

### What's wired up

- **Provider** (`calls-xmpp-provider.c`): watches for `land.rob.patch`
  on the session bus via `g_bus_watch_name`; creates the D-Bus proxy
  asynchronously when Patch appears, drops it when Patch vanishes.
  Dispatches `IncomingCall` and `CallStateChanged` signals to the
  origin.
- **Origin** (`calls-xmpp-origin.c`): `dial()` calls Patch's `Dial`
  method over D-Bus. Tracks active calls in a `GHashTable`; emits
  `call-added` / `call-removed` for gnome-calls. Tears down all calls
  when Patch vanishes.
- **Call** (`calls-xmpp-call.c`): `answer()` → `Accept`, `hang_up()` →
  `Hangup`, `send_dtmf_tone()` → `SendDtmf`. Maps Patch's string
  states (`active`, `ringing`, `proposing`, `ended`, `rejected`,
  `retracted`) to `CallsCallState` enum values. Stores display name
  from the `IncomingCall` signal and exposes it via `get_name()`.

### Patch-side D-Bus surface

Published by `src/patch/calls_dbus.py` at bus name `land.rob.patch`,
object path `/land/rob/patch/calls`, interface
`land.rob.patch.Calls1`.

| Direction       | Method / Signal                                     |
|-----------------|-----------------------------------------------------|
| plugin → Patch  | `Dial(s number) → s session_id`                     |
| plugin → Patch  | `Accept(s session_id)`                               |
| plugin → Patch  | `Reject(s session_id)`                               |
| plugin → Patch  | `Hangup(s session_id)`                               |
| plugin → Patch  | `SendDtmf(s session_id, s digit)`                    |
| plugin → Patch  | `SetHold(s session_id, b hold)`                      |
| plugin → Patch  | `SetMute(s session_id, b muted)`                     |
| Patch → plugin  | `IncomingCall(s session_id, s number, s name)` signal |
| Patch → plugin  | `CallStateChanged(s session_id, s state)` signal     |

When gnome-calls owns `org.gnome.Calls` on the session bus, Patch
suppresses its built-in call dialog and lets gnome-calls drive the UI.

## Files

- `meson.build` — separate meson project, kept out of the main Patch
  build because it depends on `libcalls` (not available outside
  gnome-calls phone targets)
- `calls-xmpp.plugin.in.in` — libpeas plugin descriptor
- `src/calls-xmpp-provider.{c,h}` — `CallsProvider` subclass
- `src/calls-xmpp-origin.{c,h}` — `CallsOrigin` subclass (one per
  XMPP account; in practice always exactly one for Patch)
- `src/calls-xmpp-call.{c,h}` — `CallsCall` subclass; binds to a
  single Jingle session id

## Build (when targeting Phosh)

```bash
cd plugin
meson setup _build
meson compile -C _build
sudo meson install -C _build
```

Installs the `.so` and `.plugin` under
`/usr/lib/<arch>/calls/plugins/calls-xmpp/`. Confirm gnome-calls loads
it by running with debug logging:

```bash
G_MESSAGES_DEBUG=Calls gnome-calls
```

## Audio routing

Patch owns the GStreamer pipeline (pulsesrc → webrtcbin → pulsesink).
gnome-calls typically manages audio profiles via `libcallaudio`. When
both coexist, gnome-calls sets the PipeWire/PulseAudio call-audio
profile and Patch's pulsesink inherits it. The speaker toggle in
Patch's built-in dialog is suppressed when gnome-calls is active
(gnome-calls handles routing).

## Remaining work

- Build and test on a real Phosh target with `libcalls-dev`
- Verify gnome-calls' `CallsCall` API for hold vfunc availability —
  wire `SetHold` / `SetMute` if the interface supports it
- Test cold-start activation race: push wake → Patch starts →
  gnome-calls plugin sees the bus name → incoming call surfaces
