"""Contact name resolution — Folks if available, else a JSON file.

Primary source: `Folks.IndividualAggregator` (Evolution Data Server,
the common GNOME backend). At startup we kick off `prepare()` async,
listen for `individuals-changed-detailed`, and rebuild an in-memory
`E.164 phone -> display name` index.

Fallback (when Folks isn't in the runtime, which is the case for the
flatpak under org.gnome.Platform//50): read
`$XDG_CONFIG_HOME/patch/contacts.json` — a flat JSON object of
`{ "name": "raw phone number" }` or `{ "raw phone number": "name" }`
(both shapes accepted). Numbers are normalised to E.164 with US as
the default country, so "713-555-1234" → "+17135551234" — that's
the shape cheogram uses for its JIDs, which is what callers look up
against.

The lookup path is sync — callers (messages list, dialer recents,
call dialog header) want the name at render time.
"""

from __future__ import annotations

import json
import logging
import os

from gi.repository import GLib, GObject

from patch import numfmt

log = logging.getLogger(__name__)


# Folks may not be present in the Flatpak runtime (the GNOME Platform
# image doesn't include libfolks). Make the import optional so the rest
# of the app survives — ContactsManager will log a warning at start()
# and lookup() will always return None, falling back to numfmt rendering.
try:
    from gi.repository import Folks
except (ImportError, ValueError):
    Folks = None


class ContactsManager(GObject.Object):
    __gtype_name__ = "PatchContactsManager"

    __gsignals__ = {
        # No args. Fires whenever the index is rebuilt.
        "index-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, account):
        super().__init__()
        self._account = account
        # number (E.164 incl. leading +) -> display name
        self._by_number: dict[str, str] = {}
        self._aggregator: Folks.IndividualAggregator | None = None

    def start(self) -> None:
        """Kick off the contacts source. Safe to call once at startup."""
        # Always try the local file sources first so they're available
        # even while Folks is preparing (async).
        self._load_local_sources()
        if Folks is None:
            log.info("folks typelib not present — local-source contacts only")
            return
        try:
            self._aggregator = Folks.IndividualAggregator.dup()
        except Exception as exc:  # noqa: BLE001
            log.warning("folks aggregator unavailable: %s", exc)
            return
        self._aggregator.connect(
            "individuals-changed-detailed", self._on_individuals_changed)
        self._aggregator.prepare(self._on_prepared)

    def _load_local_sources(self) -> None:
        """Pull contacts from every local cache we know how to read.

        Order is overlay-style — JSON file first (the user's manual
        overrides), GSConnect device caches last (live mirror of the
        paired phone). Later sources beat earlier ones on key conflict
        so the live phone data wins over a stale manual entry.
        """
        self._load_json_fallback()
        self._load_gsconnect_cache()

    def _load_json_fallback(self) -> None:
        """Read ~/.config/patch/contacts.json.

        Format: a flat JSON object. Accepts either
            { "name": "phone number" }
        or  { "phone number": "name" }
        (we detect which way each pair runs by checking if either
        side parses as an E.164 number).
        """
        path = self._json_path()
        if not os.path.exists(path):
            log.debug("no contacts.json at %s", path)
            return
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("contacts.json: %s", exc)
            return
        if not isinstance(raw, dict):
            log.warning("contacts.json: top-level must be an object")
            return
        added = 0
        for k, v in raw.items():
            if not isinstance(v, str):
                continue
            # Try "name -> number" first (common direction); if that
            # fails to normalise, try the reverse "number -> name".
            num = numfmt.normalize_e164(v, "US")
            name = k
            if num is None:
                num = numfmt.normalize_e164(k, "US")
                name = v
            if num is None:
                log.debug("contacts.json: skipping %r/%r (no parseable phone)",
                          k, v)
                continue
            self._by_number[num] = name
            added += 1
        if added:
            log.info("contacts.json: loaded %d numbers from %s", added, path)

    def _json_path(self) -> str:
        base = os.environ.get("XDG_CONFIG_HOME") or \
            os.path.expanduser("~/.config")
        return os.path.join(base, "patch", "contacts.json")

    def _load_gsconnect_cache(self) -> None:
        """Read every paired-device contact cache GSConnect maintains.

        Path: $XDG_CACHE_HOME/gsconnect/<device-id>/contacts.json.
        Format: a dict keyed by per-contact id, where each value has
        `name` and `numbers: [{value, type}, ...]`. Values may be
        local-format ('12817709319'), 10-digit local ('9792199404'),
        or already-E.164 ('+18326591976') — normalize_e164 handles
        all three with the US default. Devices the user pairs after
        Patch launches won't show up until the next start (we don't
        currently watch the cache dir).
        """
        base = os.environ.get("XDG_CACHE_HOME") or \
            os.path.expanduser("~/.cache")
        gsc_root = os.path.join(base, "gsconnect")
        if not os.path.isdir(gsc_root):
            return
        added = 0
        for entry in os.listdir(gsc_root):
            path = os.path.join(gsc_root, entry, "contacts.json")
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    contacts = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("gsconnect %s: %s", path, exc)
                continue
            if not isinstance(contacts, dict):
                continue
            for c in contacts.values():
                if not isinstance(c, dict):
                    continue
                name = (c.get("name") or "").strip()
                if not name:
                    continue
                for ph in c.get("numbers") or []:
                    value = (ph.get("value") or "").strip() if isinstance(ph, dict) else ""
                    if not value:
                        continue
                    norm = numfmt.normalize_e164(value, "US")
                    if norm and norm not in self._by_number:
                        self._by_number[norm] = name
                        added += 1
        if added:
            log.info("gsconnect: loaded %d numbers across %s",
                     added, gsc_root)

    def lookup(self, number_e164: str) -> str | None:
        """Return the display name for a number, or None if unknown."""
        if not number_e164:
            return None
        return self._by_number.get(number_e164)

    def lookup_jid(self, jid: str) -> str | None:
        """Convenience wrapper for JID -> name via the gateway domain."""
        number = numfmt.jid_to_number(jid, self._account.gateway)
        if number is None:
            return None
        return self.lookup(number)

    def label_for_jid(self, jid: str) -> str:
        """Best-effort display label: contact name if we know it,
        else a pretty-formatted version of the phone number, else the
        raw JID."""
        number = numfmt.jid_to_number(jid, self._account.gateway)
        if number:
            name = self.lookup(number)
            if name:
                return name
            return numfmt.format_for_display(number)
        return jid

    def label_for_group_jid(self, jid: str) -> str:
        """Group-chat label.

        cheogram's group SMS JIDs encode every participant's number,
        comma-separated, in the localpart — e.g.
            "+15551234567,+15557654321,+15558765432@cheogram.com"
        We split on commas, look each up, and join. Unknown numbers
        fall back to their pretty-formatted form. If a single JID
        (no comma), we hand off to label_for_jid.
        """
        if not numfmt.is_group_jid(jid):
            return self.label_for_jid(jid)
        localpart = jid.split("@", 1)[0]
        names: list[str] = []
        for part in localpart.split(","):
            piece_jid = part + "@" + jid.split("@", 1)[1]
            names.append(self.label_for_jid(piece_jid))
        return ", ".join(names)

    # -- callbacks -------------------------------------------------------

    def _on_prepared(self, _aggregator, result):
        try:
            self._aggregator.prepare_finish(result)
        except GLib.Error as exc:
            log.warning("folks prepare failed: %s", exc.message)
            return
        log.info("folks ready, rebuilding contacts index")
        self._rebuild()

    def _on_individuals_changed(self, _aggregator, _changes):
        # We just rebuild on every change. The phone-mobile cohort is
        # contact-light, so iterating the aggregator is cheap.
        self._rebuild()

    def _rebuild(self) -> None:
        # Start from the local file sources (JSON + GSConnect cache)
        # and let Folks overlay on top — Folks entries win on conflict
        # because they're the canonical system address book when present.
        new_index: dict[str, str] = {}
        prior_keys = set(self._by_number.keys())
        self._by_number = {}
        self._load_local_sources()
        new_index.update(self._by_number)
        if self._aggregator is not None:
            individuals = self._aggregator.props.individuals
            for ind in individuals.values():
                name = ind.props.alias or ind.props.display_name or ""
                if not name:
                    continue
                phones = ind.props.phone_numbers or []
                for ph in phones:
                    value = ph.props.value if hasattr(ph, "props") else ph.get_value()
                    norm = numfmt.normalize_e164(value, "US")
                    if norm:
                        new_index[norm] = name
        if set(new_index.keys()) != prior_keys or new_index != self._by_number:
            log.info("contacts index: %d numbers", len(new_index))
            self._by_number = new_index
            self.emit("index-changed")
