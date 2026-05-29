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


# EDS + Folks may not be present in every runtime (the GNOME Platform
# image doesn't ship them; our flatpak bundles them; native builds get
# them from the system). Make the imports optional so the rest of the
# app survives — _load_eds() / Folks fallbacks all detect None and
# skip cleanly when the libraries aren't there.
try:
    from gi.repository import Folks
except (ImportError, ValueError):
    Folks = None

try:
    import gi as _gi
    _gi.require_version("EDataServer", "1.2")
    _gi.require_version("EBookContacts", "1.2")
    _gi.require_version("EBook", "1.2")
    from gi.repository import EDataServer, EBookContacts, EBook
except (ImportError, ValueError):
    EDataServer = EBookContacts = EBook = None


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

        Order is overlay-style — EDS first (the system address book
        and any GOA / CardDAV sources, where the bulk of contacts
        live), JSON file next (user-curated overrides), GSConnect
        cache last (live mirror of paired phone). Each later source
        wins on key conflict so the most-recently-edited representation
        is the one we surface.
        """
        self._load_eds()
        self._load_json_fallback()
        self._load_gsconnect_cache()

    def _load_eds(self) -> None:
        """Read every EDS address-book source via libebook directly.

        Talks to evolution-source-registry + evolution-addressbook-
        factory over D-Bus. In a flatpak that means the bundled
        libebook reaches the *host's* EDS daemons (because we declared
        --talk-name=org.gnome.evolution.dataserver.*). Each source's
        contacts are pulled in one bulk get_contacts_sync; we extract
        the display name + each TEL field and index by E.164.

        We use EDS direct rather than libfolks because:
          - folks adds cross-store merging we don't need for a
            number-to-name lookup,
          - the FolksEds backend has been empirically reluctant to
            surface Radicale/CardDAV sources in our test environment,
            while EBook.BookClient sees them immediately.
        """
        if EDataServer is None or EBook is None:
            return
        try:
            reg = EDataServer.SourceRegistry.new_sync(None)
        except Exception as exc:  # noqa: BLE001
            log.debug("EDS source registry unavailable: %s", exc)
            return
        ext = EDataServer.SOURCE_EXTENSION_ADDRESS_BOOK
        added = 0
        sources_seen = 0
        for src in reg.list_sources(None):
            if not src.has_extension(ext):
                continue
            sources_seen += 1
            try:
                book = EBook.BookClient.connect_sync(src, 5, None)
                ok, contacts = book.get_contacts_sync(
                    EBookContacts.book_query_any_field_contains("").to_string(),
                    None)
            except Exception as exc:  # noqa: BLE001
                log.debug("EDS book %s unreadable: %s",
                          src.get_display_name(), exc)
                continue
            if not contacts:
                continue
            for c in contacts:
                name = (c.get_property("full-name") or "").strip()
                if not name:
                    continue
                for attr in c.get_attributes(EBookContacts.ContactField.TEL):
                    value = (attr.get_value() or "").strip()
                    if not value:
                        continue
                    norm = numfmt.normalize_e164(value, "US")
                    if norm and norm not in self._by_number:
                        self._by_number[norm] = name
                        added += 1
        if added:
            log.info("EDS: loaded %d numbers from %d address book(s)",
                     added, sources_seen)

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

    def all_contacts(self) -> dict[str, str]:
        """Return {e164_number: display_name} for all known contacts."""
        return dict(self._by_number)

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

    # -- write path ------------------------------------------------------

    def _writable_book(self):
        """Connect to the address book we should write into, or None.

        Policy (mirrors the user's stated preference):
          1. the system default address book, if GNOME Contacts has one
             selected and it accepts writes,
          2. otherwise the local "Personal" book (system-address-book),
          3. otherwise the first writable EDS source.
        Returns a connected, non-readonly ``EBook.BookClient`` or None
        when EDS is absent / nothing is writable (callers then fall back
        to the JSON file).
        """
        if EDataServer is None or EBook is None:
            return None
        try:
            reg = EDataServer.SourceRegistry.new_sync(None)
        except Exception as exc:  # noqa: BLE001
            log.debug("EDS source registry unavailable: %s", exc)
            return None
        ext = EDataServer.SOURCE_EXTENSION_ADDRESS_BOOK
        ordered = []
        dflt = reg.ref_default_address_book()
        if dflt is not None and dflt.has_extension(ext):
            ordered.append(dflt)
        local = reg.ref_source("system-address-book")
        if local is not None and local.has_extension(ext):
            ordered.append(local)
        ordered.extend(reg.list_sources(None))
        seen: set[str] = set()
        for src in ordered:
            uid = src.get_uid()
            if uid in seen or not src.has_extension(ext) or not src.get_enabled():
                continue
            seen.add(uid)
            try:
                book = EBook.BookClient.connect_sync(src, 5, None)
            except Exception as exc:  # noqa: BLE001
                log.debug("EDS book %s unwritable: %s", src.get_display_name(), exc)
                continue
            if not book.is_readonly():
                return book
        return None

    def contact_targets(self) -> list[tuple[str, str]]:
        """Existing contacts the user could add a number to.

        Returns ``[(id, display_name)]`` sorted by name. With a writable
        EDS book the id is the contact UID; in JSON-fallback mode it's
        the name itself (the JSON shape is just number -> name).
        """
        book = self._writable_book()
        if book is not None:
            try:
                ok, contacts = book.get_contacts_sync(
                    EBookContacts.book_query_any_field_contains("").to_string(),
                    None)
            except Exception as exc:  # noqa: BLE001
                log.warning("EDS contact list failed: %s", exc)
                contacts = None
            out = []
            for c in contacts or []:
                name = (c.get_property("full-name") or "").strip()
                uid = c.get_property("id")
                if name and uid:
                    out.append((uid, name))
            out.sort(key=lambda t: t[1].casefold())
            return out
        names = sorted(set(self._by_number.values()), key=str.casefold)
        return [(n, n) for n in names]

    def create_contact(self, name: str, number_e164: str) -> bool:
        """Create a new contact with ``number_e164``. Returns success."""
        name = (name or "").strip()
        if not name or not number_e164:
            return False
        book = self._writable_book()
        if book is not None:
            contact = EBookContacts.Contact.new()
            contact.set_property("full-name", name)
            contact.add_attribute(self._tel_attribute(number_e164))
            try:
                book.add_contact_sync(
                    contact, EBookContacts.BookOperationFlags(0), None)
            except Exception as exc:  # noqa: BLE001
                log.warning("EDS add_contact failed: %s", exc)
                return False
        elif not self._write_json(number_e164, name):
            return False
        self._rebuild()
        return True

    def add_number_to_contact(self, contact_id: str, number_e164: str) -> bool:
        """Add ``number_e164`` to the existing contact ``contact_id``.

        ``contact_id`` is whatever ``contact_targets()`` returned — an
        EDS UID with a writable book, or the contact name in JSON mode.
        """
        if not contact_id or not number_e164:
            return False
        book = self._writable_book()
        if book is not None:
            try:
                ok, contact = book.get_contact_sync(contact_id, None)
            except Exception as exc:  # noqa: BLE001
                log.warning("EDS get_contact failed: %s", exc)
                return False
            if not ok or contact is None:
                return False
            contact.add_attribute(self._tel_attribute(number_e164))
            try:
                book.modify_contact_sync(
                    contact, EBookContacts.BookOperationFlags(0), None)
            except Exception as exc:  # noqa: BLE001
                log.warning("EDS modify_contact failed: %s", exc)
                return False
        elif not self._write_json(number_e164, contact_id):
            return False
        self._rebuild()
        return True

    @staticmethod
    def _tel_attribute(number_e164: str):
        attr = EBookContacts.VCardAttribute.new("", "TEL")
        attr.add_value(number_e164)
        return attr

    def _write_json(self, number_e164: str, name: str) -> bool:
        """Persist a number -> name pair into contacts.json."""
        path = self._json_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data: dict = {}
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    data = loaded
            data[number_e164] = name
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("contacts.json write failed: %s", exc)
            return False
        return True

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
            try:
                self._overlay_folks(new_index)
            except Exception as exc:  # noqa: BLE001
                # Gee templated containers don't always introspect cleanly
                # through PyGObject — values can come back as ints. Don't
                # let a broken overlay hide the EDS-direct contacts we
                # already loaded.
                log.warning("folks overlay failed (%s: %s); using local sources only",
                            exc.__class__.__name__, exc)
        if set(new_index.keys()) != prior_keys or new_index != self._by_number:
            log.info("contacts index: %d numbers", len(new_index))
            self._by_number = new_index
            self.emit("index-changed")

    def _overlay_folks(self, new_index: dict[str, str]) -> None:
        """Overlay Folks individuals onto ``new_index``.

        Gee.Map iteration via PyGObject is fragile — ``MapIterator.get_value``
        comes back as an int in some bindings because the templated return
        type isn't recovered. We iterate the keys set (plain ``Gee.Set<string>``,
        no templating ambiguity) and look each individual up by id.
        """
        individuals = self._aggregator.props.individuals
        keys = individuals.get_keys()
        itr = keys.iterator()
        while itr.next():
            key = itr.get()
            if not isinstance(key, str):
                continue
            ind = individuals.get(key)
            if ind is None or not hasattr(ind, "props"):
                continue
            name = ind.props.alias or ind.props.display_name or ""
            if not name:
                continue
            phones = ind.props.phone_numbers or []
            for ph in phones:
                value = ph.props.value if hasattr(ph, "props") else ph.get_value()
                norm = numfmt.normalize_e164(value, "US")
                if norm:
                    new_index[norm] = name
