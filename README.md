# Patch

A native GNOME application.

## Tech stack

- Python 3.10+ + PyGObject
- GTK 4 + libadwaita
- Blueprint (`.blp`) UI templates compiled to `.ui` and bundled via
  GResource
- Meson + Ninja, packaged as a Flatpak on the GNOME 50 SDK

## Running locally (host install)

```sh
meson setup _build --prefix="$PWD/_install"
meson install -C _build

PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHONPATH="$PWD/_install/lib/python$PYVER/site-packages" \
GSETTINGS_SCHEMA_DIR="$PWD/_install/share/glib-2.0/schemas" \
XDG_DATA_DIRS="$PWD/_install/share:${XDG_DATA_DIRS:-/usr/share}" \
"$PWD/_install/bin/patch"
```

## Running as a Flatpak

```sh
flatpak install --user flathub org.gnome.Platform//50 org.gnome.Sdk//50

./build-all.sh                  # both arches
./build-all.sh --arch x86_64    # single arch
./build-all.sh --regen-deps     # regenerate python3-deps.json from requirements.txt
./build-all.sh --install        # also installs the host-arch bundle (--user)
```

The first invocation auto-regenerates `build-aux/flatpak/python3-deps.json`
from `requirements.txt` if the file is missing.

## License

GPL-3.0-or-later. See `COPYING`.
