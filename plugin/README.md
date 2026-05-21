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

The plugin talks to Patch over D-Bus. Patch exposes a small interface
on `land.rob.patch` for placing outgoing calls and receiving JMI
notifications; the plugin translates between that and gnome-calls'
`CallsProvider` / `CallsOrigin` / `CallsCall` GObject hierarchy.

[cpm]: https://gitlab.gnome.org/GNOME/calls/-/blob/main/src/calls-plugin-manager.c

## Status

Scaffold only. Files compile-check against the gnome-calls headers on
a target with `libcalls`-dev installed, but no provider/origin/call
plumbing is wired up yet. This directory exists so:

- The eventual phone-deploy build target has a place to land
- Future work can land in small commits (header includes → empty
  provider → origin → call) instead of one big drop
- The Patch-side D-Bus interface can be designed against a real
  consumer

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

## Patch's D-Bus surface (planned)

We'll add an interface `land.rob.patch.Calls1` at `/land/rob/patch/calls`
to be defined as we wire up the plugin. Rough shape:

| Direction       | Method/Signal                                |
|-----------------|----------------------------------------------|
| plugin → Patch  | `Dial(s number) -> s session_id`             |
| plugin → Patch  | `Accept(s session_id)`                       |
| plugin → Patch  | `Reject(s session_id)`                       |
| plugin → Patch  | `Hangup(s session_id)`                       |
| Patch → plugin  | `Incoming(s session_id, s number)` (signal)  |
| Patch → plugin  | `Stateʼ(s session_id, s state)` (signal)     |

Wiring this is the next milestone for the plugin.
