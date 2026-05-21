# Project Style Guide

Conventions for native GNOME / Phosh apps in this collection (banter,
clicker, finlit, jamjar, tonic). The goal is well-structured, readable
code that follows idiomatic Python (PEP 8) and GNOME / libadwaita
conventions; this guide encodes the cohort-specific patterns layered
on top. Drop this file into a new project alongside `CLAUDE.md` so
those patterns apply from day one.

## Identity

- **App-id namespace**: `land.rob.<project>` — **all lowercase**
  (e.g. `land.rob.clicker`, `land.rob.tock`). Flathub's submission
  guidelines treat the app id as case-sensitive and recommend
  lowercase ASCII for the final component; mixed-case ids like
  `land.rob.Clicker` cause friction during review even if they
  technically validate. Use the lowercase form in every metadata
  file and code path. Do not use reverse-DNS forms like
  `io.github.<user>.<App>`.
- **Project name** in lowercase: `clicker`, `finlit`, etc. — used as
  the Python package name, the launcher script name, the systemd /
  Flatpak `command:` value, AND the app id's final component (so the
  app id and project name share the same casing).
- **License**: GPL-3.0-or-later, filename `COPYING` (not `LICENSE`).
- **Class prefix**: `<Project>Window`, `<Project>Application`,
  `<Project>SomePage` (capitalised, since these are Python class
  names — only the *app id* is lowercase). Avoid bare `MainWindow`.
  Set `__gtype_name__ = "<Project><ClassName>"` on every
  `Gtk.Template`d class and on widgets exposed to GResource lookups.
- **GResource prefix**: `/land/rob/<project>/...` (matches the
  lowercase app id).
- **GSettings schema id**: `land.rob.<project>` (file:
  `data/land.rob.<project>.gschema.xml`).

## Source layout

```
<project>/
├── meson.build                     # root build (defines APP_ID etc.)
├── meson_options.txt               # only if real options exist
├── COPYING                         # GPL-3.0-or-later
├── README.md                       # public-facing
├── CLAUDE.md                       # committed; no identifying info
├── requirements.txt                # Python runtime deps (one per line)
├── build-all.sh                    # multi-arch flatpak driver
├── fix-flatpak-deps.py             # tarball -> wheel patcher
├── build-aux/
│   └── flatpak/
│       ├── land.rob.<project>.json # Flatpak manifest (JSON, not YAML)
│       └── python3-deps.json       # generated, gitignored
├── data/
│   ├── meson.build
│   ├── land.rob.<project>.desktop.in
│   ├── land.rob.<project>.metainfo.xml.in
│   ├── land.rob.<project>.gschema.xml
│   ├── icons/hicolor/{scalable,symbolic}/apps/...svg
│   └── ui/
│       ├── meson.build             # blueprint-compiler + gresource
│       ├── land.rob.<project>.gresource.xml
│       └── *.blp                   # one per template
├── po/
│   ├── LINGUAS
│   ├── POTFILES.in
│   └── meson.build
└── src/
    ├── meson.build
    ├── <project>.in                # processed to bin script by Meson
    ├── const.py.in                 # processed to const.py with paths
    └── <package>/
        ├── __init__.py
        ├── main.py                 # entry point
        ├── application.py          # Adw.Application subclass
        ├── window.py               # main window
        └── <subpackage>/...
```

The Python package lives under `src/<package>/`. Subpackages group
features (e.g. `devices/`, `discovery/`, `pages/`, `widgets/`,
`dialogs/`). Tests live in `tests/` if present; never in `src/`.

**Files should be no longer than they need to be.** When a module
accumulates multiple distinct concerns or grows past ~500 lines, split
it into focused submodules. A long file is a structural smell, not a
tradeoff — the cohort has several god modules from earlier iterations
(`chat_view.py`, `playback.py`, `vault_page.py`, `window.py` in
several projects) that are slated for breakup; don't add to that
list. Default to one purpose per file: a widget per file, a dialog
per file, a controller per file. Splitting before the file is painful
is cheaper than splitting after.

## Build system

- **Meson + Ninja**. Canonical root `meson.build`:
  ```meson
  project('<project>',
    version: '<x.y.z>',
    meson_version: '>= 1.0.0',
    license: 'GPL-3.0-or-later',
    default_options: ['warning_level=2'],
  )

  i18n   = import('i18n')
  gnome  = import('gnome')
  python = import('python').find_installation('python3')

  application_id = 'land.rob.<project>'
  prefix     = get_option('prefix')
  bindir     = prefix / get_option('bindir')
  datadir    = prefix / get_option('datadir')
  localedir  = prefix / get_option('localedir')
  pkgdatadir = datadir / meson.project_name()
  moduledir  = python.get_install_dir() / meson.project_name()

  # Bare-string substitutions for XML/desktop/launcher .in files.
  conf = configuration_data()
  conf.set('PYTHON',     python.full_path())
  conf.set('APP_ID',     application_id)
  conf.set('VERSION',    meson.project_version())
  conf.set('PKGDATADIR', pkgdatadir)
  conf.set('LOCALEDIR',  localedir)

  # Quoted-string substitutions for Python .in files (paths/IDs need
  # to land as Python string literals).
  py_conf = configuration_data()
  py_conf.set('PYTHON',          python.full_path())
  py_conf.set_quoted('APP_ID',     application_id)
  py_conf.set_quoted('VERSION',    meson.project_version())
  py_conf.set_quoted('PKGDATADIR', pkgdatadir)
  py_conf.set_quoted('LOCALEDIR',  localedir)

  subdir('data')
  subdir('src/<package>')
  subdir('po')

  gnome.post_install(
    glib_compile_schemas:    true,
    gtk_update_icon_cache:   true,
    update_desktop_database: true,
  )
  ```
- The variable name is `application_id` (full word), not `app_id`.
- The conf keys are uppercase (`APP_ID`, `VERSION`, `PKGDATADIR`,
  `LOCALEDIR`, `PYTHON`).
- **Install Python sources** with
  `python.install_sources(sources, subdir: '<package>')`. Avoid
  `install_data(install_dir: moduledir)`.
- **Launcher script**: `src/<package>/<project>.in` configured by
  Meson into `bin/<project>`, sets `PYTHONPATH`/`GSETTINGS_SCHEMA_DIR`
  and execs `python3 -m <package>`.
- **Constants module**: `src/<package>/const.py.in` configured to
  `const.py` with the substituted paths. Do not hard-code
  `/usr/share/...` in Python.

## UI: Blueprint

- UI is defined in `data/ui/*.blp` (Blueprint), compiled to `.ui` at
  build time, bundled via GResource. Logic stays in Python.
- `data/ui/meson.build` does both the blueprint compile and the
  gresource bundle:
  ```
  blueprint_compiler = find_program('blueprint-compiler')
  blueprint_sources = files('window.blp', 'foo.blp', ...)
  blueprints = custom_target('blueprints',
    input:  blueprint_sources,
    output: '.',
    command: [blueprint_compiler, 'batch-compile',
              '@OUTPUT@', '@CURRENT_SOURCE_DIR@', '@INPUT@'],
  )
  gnome.compile_resources(
    APP_ID,
    APP_ID + '.gresource.xml',
    gresource_bundle: true,
    install: true,
    install_dir: pkgdatadir,
    dependencies: blueprints,
    source_dir: meson.current_build_dir(),
  )
  ```
- The Flatpak manifest must bundle blueprint-compiler so offline
  builds work. Cleanup `*` so it isn't shipped at runtime:
  ```json
  {
    "name": "blueprint-compiler",
    "buildsystem": "meson",
    "cleanup": ["*"],
    "sources": [{
      "type": "git",
      "url": "https://gitlab.gnome.org/jwestman/blueprint-compiler.git",
      "tag": "v0.16.0"
    }]
  }
  ```
- The gresource.xml lists files by their build-tree name (no `ui/`
  prefix) and aliases them under `/land/rob/<project>/ui/...` so
  `Gtk.Template(resource_path='/land/rob/<project>/ui/foo.ui')`
  works in Python.

## Adaptive shell

Pick a shell based on what the app actually does, not as a uniform
mandate:

- **List + detail apps** (a persistent list/selection alongside a
  detail view of one selected thing) use `Adw.NavigationSplitView`
  or `Adw.OverlaySplitView`. The breakpoint at `max-width: 600sp`
  collapses the split on narrow widths so the same code works on
  phone and desktop without per-form-factor branches:

  ```
  content: Adw.NavigationSplitView nav_split {
    sidebar: Adw.NavigationPage { ... };
    content: Adw.NavigationPage { ... };
  };

  Adw.Breakpoint {
    condition ("max-width: 600sp")
    setters {
      nav_split.collapsed: true;
    }
  }
  ```

  Cohort examples: banter (chats sidebar + chat content), jamjar
  (library sidebar + now-playing content), coffer (vault categories
  + item detail).

- **Single-task, kiosk, single-stream, or webview-wrapping apps**
  stay single-pane. The list/detail criterion doesn't apply.
  Cohort examples: couch (TV kiosk), roam (live tracker — one
  stream of state), tock (one watch at a time), tonic (cadence
  drill loop), homie (HA dashboard webview; HA already provides
  its own sidebar inside).

The criterion that actually decides: *does the app have a long-lived
list/selection alongside a detail view of one selected thing?*
Yes → split. No → single-pane. The other questions (desktop vs.
mobile, breakpoints, narrow handling) are downstream of that
answer — `NavigationSplitView` handles them once.

## Flatpak

- Manifest at `build-aux/flatpak/land.rob.<project>.json` (JSON, not
  YAML).
- Runtime: `org.gnome.Platform//50` + SDK. Bump in lockstep across
  projects when GNOME advances.
- Module order: blueprint-compiler first, then `python3-deps.json`,
  then the project module pointing at `"path": "../.."` (the source
  tree is the repo root, two levels up from the manifest).
- `python3-deps.json` is generated by `flatpak-pip-generator`,
  gitignored, regenerated when `requirements.txt` changes.
- Patch source tarballs to pre-built wheels with
  `fix-flatpak-deps.py build-aux/flatpak/python3-deps.json` so the
  build sandbox doesn't need a Rust toolchain for `cryptography`
  etc. The script is idempotent and emits multi-arch wheels gated by
  `only-arches` so a single manifest covers x86_64 + aarch64.

## Build driver

`build-all.sh` is a thin wrapper that:

- accepts `--arch <x86_64|aarch64>`, `--regen-deps`, `--install`
- runs `fix-flatpak-deps.py` (idempotent) before building
- warns if qemu binfmt isn't registered when cross-building
- emits `<project>-<arch>.flatpak` bundles at the project root
- does not push or sign anything

Keep it identical across projects so the user has muscle memory for
`./build-all.sh --arch aarch64 --install`.

## CI: two-server (Codeberg + selfhost) layout

Cohort projects often live on both Codeberg (public mirror) and
the self-hosted Forgejo at `git.rob.land` (private build target
that publishes to `flatpak.rob.land`). The same workflow files
end up on both servers, but their runner pools have no labels in
common.

Conventions:

- `.forgejo/workflows/ci.yml` runs the test gate on Codeberg.
  - `runs-on: codeberg-small` (matches Codeberg's shared runner).
  - `if: startsWith(github.server_url, 'https://codeberg.org')`
    guards the body. The startsWith form is forgiving of trailing
    slashes / reverse-proxy URL variants.
- `.forgejo/workflows/publish.yml` builds + signs flatpaks and
  rsyncs to `flatpak.rob.land` on the self-hosted Forgejo.
  - `runs-on: ubuntu-latest` (matches the self-hosted runner's
    `ubuntu-latest` label; the `flatpak` label was originally
    intended for this but the cohort settled on `ubuntu-latest`
    so the standard apt-installable toolchain works).
  - `if: startsWith(github.server_url, 'https://git.rob.land')`.

Forgejo-runner v12 quirks to know:

- `runs-on:` is evaluated **before** `if:` at dispatch time, so a
  job whose `runs-on:` label doesn't exist on the current server
  queues forever even when the `if:` would skip it. Mitigation:
  disable the irrelevant workflow per-repo (Settings → Actions →
  individual workflow → Disable) on the wrong server, OR add the
  missing label to the runner so it can claim and skip.
- The runner's strict schema validator rejects `gitea.*`
  variable accesses in `if:` expressions, even though the server
  evaluates them fine. Use `github.server_url` — Forgejo exposes
  it as the GitHub-compat alias on every event payload.

Required secrets on the self-hosted Forgejo for `publish.yml`:

| Secret | Value |
|---|---|
| `FLATPAK_GPG_PRIVATE_KEY` | base64 of `gpg --export-secret-keys --armor <KEY>` |
| `FLATPAK_GPG_KEY_ID` | the signing key id (matches `.flatpakrepo`'s public key) |
| `DEPLOY_SSH_PRIVATE_KEY` | base64 of an ssh key on the deploy account's `authorized_keys` |
| `DEPLOY_KNOWN_HOSTS` | `ssh-keyscan <flatpak-repo-host>` output |
| `DEPLOY_TARGET` | `flatpak@host:/path/to/repo` (rsync target) |

## Async work

Pick one of two patterns based on what else needs to run async:

- **asyncio with a worker thread.** Required when you use
  `aiohttp`, `bleak`, `dbus-next`, `websockets`, or any other
  asyncio-native library. Implement an `AsyncRunner` class (or a
  domain-specific subclass like `BleManager`): one asyncio loop on a
  daemon thread, **owned by the Application instance** (`self.runner
  = AsyncRunner()` in `do_startup`, `self.runner.stop()` in
  `do_shutdown`). Submit coroutines with `runner.submit(coro)` or
  `runner.run_async(coro, on_result=…, on_error=…)`; the runner
  marshals results back via `GLib.idle_add`. **The trap:**
  `asyncio.set_event_loop(loop)` must run on the worker thread before
  `run_forever()`, or `run_coroutine_threadsafe` fails silently. The
  class lives in `async_loop.py`; for backward compatibility a small
  set of module-level shims (`run_async(coro, …)`, `call_on_main(fn)`)
  may delegate to `Adw.Application.get_default().runner`.
- **Soup3 (GLib-native HTTP).** Use
  `Soup.Session.send_and_read_async` for HTTP and
  `Soup.WebsocketConnection` for WebSockets. No worker thread;
  callbacks fire on the main loop directly. Strictly simpler when
  HTTP is the only async need — no asyncio↔GLib marshalling, no
  thread to start or stop. Pick this when nothing in your
  dependencies pulls in asyncio.

For sync-only blocking work (image decoding, libsecret access, file
hashing — things with no asyncio API), an instance-owned
`BackgroundRunner` backed by `concurrent.futures.ThreadPoolExecutor`
is the right tool. Banter uses this; the executor is reused across
calls instead of spawning a fresh thread each time.

Don't sleep or block the GTK main loop. Don't call `requests` /
`urllib` / `time.sleep` on the main thread. Don't spawn ad-hoc
threads when the shared runner will do.

## Application lifecycle

A few `Gio.Application` / `Adw.ApplicationWindow` patterns the cohort
has consistently got wrong; fix them once per project, then leave
them alone.

- **Persist window geometry with `get_width()` / `get_height()`**, not
  `get_default_size()`. The latter returns the *configured default*
  (last value passed to `set_default_size`), so any user resize is
  silently discarded on close.
  ```python
  def _on_close_request(self, *_):
      if not self.is_maximized():
          self._settings.set_int('window-width',  self.get_width())
          self._settings.set_int('window-height', self.get_height())
      self._settings.set_boolean('window-maximized', self.is_maximized())
  ```
- **Register `--debug` (and any other custom flag) via
  `add_main_option`.** With `HANDLES_COMMAND_LINE` set, an unknown
  option is a hard error that aborts startup; without
  `HANDLES_COMMAND_LINE`, `Gio.Application` strips unknown options
  with a warning but `--help` won't list them. `configure_logging()`
  reads `sys.argv` directly so the registration is just there to make
  `Gio.Application` accept the flag and document it.
  ```python
  self.add_main_option(
      'debug', ord('d'), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
      'Enable debug logging', None,
  )
  ```
  Don't strip flags from `sys.argv` before `app.run()` — it bypasses
  `--help` and hides what the binary accepts.
- **Clear the `--background` flag on the second activation.** A
  headless launch path that holds the app open should track its
  state so a later notification click (which re-enters
  `do_command_line` without `--background`) can promote out of
  background mode and build the window.
  ```python
  def do_command_line(self, command_line):
      opts = command_line.get_options_dict().end().unpack()
      if opts.get('background'):
          self._background = True
          self.hold()
      else:
          self._background = False    # promote on the next activation
          self.activate()
      return 0
  ```

## Imports and Python conventions

- **Follow PEP 8.** It's the baseline; this section layers
  cohort-specific conventions on top. If you're unsure how to format
  something, check PEP 8 first. Notable defaults that align: 4-space
  indents, imports grouped (stdlib / third-party / local) at the top
  of the module, `snake_case` for functions and variables,
  `PascalCase` for classes, two blank lines between top-level
  definitions.
- **Single-entry `gi.require_version`**: declare the required GI
  versions exactly once at the application entry point (the launcher
  `<project>.in` and/or `main.py`), before any `from gi.repository`
  import. Sub-modules just `from gi.repository import …` directly —
  no repeated `require_version` per file. Keeps the imports terse;
  any module that's run in isolation should pull in the entry-point
  module first.
  ```python
  # main.py / <project>.in (entry point, runs first)
  import gi
  gi.require_version('Gtk', '4.0')
  gi.require_version('Adw', '1')
  from gi.repository import Gtk, Adw, Gio
  ```
  ```python
  # any other module
  from gi.repository import Gtk, Adw, Gio, GLib
  ```
- Use `from __future__ import annotations` for projects targeting
  3.10+ when they use forward references.
- Type-hint internal APIs lightly; don't over-annotate one-shot
  helpers.
- Logging uses Python's standard `logging` module. Each module owns a
  `log = logging.getLogger(__name__)` at the top; calls are
  `log.debug(...)` / `log.info(...)` / `log.exception('msg')`. Don't
  use bare `print()` in shipped code.
- Use **`%`-style placeholders, not f-strings**, in log calls so the
  formatting is skipped when the level is disabled:
  ```python
  log.debug('keypress %s -> %s', action, kc)   # right
  log.debug(f'keypress {action} -> {kc}')      # wrong
  ```
- A `logging_setup.py` per project owns `configure_logging()`; main
  calls it before anything else (not inside `do_command_line`, which
  may run after import-time errors have already missed the file
  handler). Default level is INFO; `--debug` (or `<APP>_DEBUG=1` in
  the environment) bumps to DEBUG. The setup installs both a stderr
  stream and a rotating file handler under
  `GLib.get_user_data_dir()/<project>/<project>.log` so Phosh users
  can read logs without `journalctl`.

## Documentation

- **README.md** — public-facing. Features, install, build, layout
  reminder, license pointer.
- **CLAUDE.md** — committed (no identifying info — no real
  hostnames, IPs, emails, paths with `/home/rob/`). Sections:
  *What this project is*, *Code quality*, *Tech stack*, *Source layout*,
  *Build workflow*, *Key conventions*, *Things to watch out for*.
- **DESIGN.md** — optional architecture overview. The "why" of the
  project: pedagogy, stack, design decisions, state machine. Tonic
  and jamjar have one.
- **TODO.md** — optional backlog with rationale. One file per
  project; older `ROADMAP.md` / `BACKLOG.md` variants should fold
  into it. Banter and jamjar have one.
- **STYLE_GUIDE.md** — this file. Drop in unchanged.

## .gitignore

Use the curated short form, not the upstream Python boilerplate:

```
# Build artifacts
_build/
_flatpak/
_flatpak_x86_64/
_flatpak_aarch64/
.flatpak-builder/
repo/
*.flatpak

# Python
__pycache__/
*.pyc
*.egg-info/
.venv/

# Editors
.vscode/
.idea/
*.swp
.DS_Store

# Claude workspace
.claude/

# Generated (regenerated per build)
build-aux/flatpak/python3-deps.json
build-aux/flatpak/python3-deps.json.bak
```

`CLAUDE.md` is **tracked**, not gitignored. The `.claude/` directory
(Claude workspace state) is ignored in full — no carve-out for
`settings.json` or anything else. `CLAUDE.md` at the project root is
project documentation and ships with the repo.

## Phone install (postmarketOS / Phosh)

The default postmarketOS `nftables` ruleset has `policy drop` on
input with no allowance for mDNS or SSDP. Drop these in for any app
that does device discovery:

```
# /etc/nftables.d/60_mdns.nft
table inet filter {
    chain input {
        iifname "wlan*" udp dport 5353 accept comment "mDNS"
    }
}

# /etc/nftables.d/61_ssdp.nft
table inet filter {
    chain input {
        iifname "wlan*" udp sport 1900 accept comment "SSDP responses"
    }
}
```

Then `sudo nft -f /etc/nftables.nft`.
