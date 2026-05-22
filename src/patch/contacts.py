"""libfolks-backed contact name resolution.

`Folks.IndividualAggregator` aggregates contacts from every backend that
implements the folks protocol (Evolution Data Server / EDS is the
common one on GNOME). At startup we kick off `prepare()` async, then
listen for the `individuals-changed-detailed` signal and rebuild an
in-memory `E.164 phone -> display name` index.

The lookup path is sync: callers (the messages list renderer, the
dialer recents, the call dialog header) want the name when they render
the row and don't want to deal with async. Until prepare() resolves
the lookup just returns None and callers fall back to the raw number.

We deliberately don't cache JID -> name. JIDs vary per gateway (cheogram
vs other JMP-style gateways) and we want the number-extracted lookup
to work uniformly across them.
"""

from __future__ import annotations

import logging

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
        """Kick off the Folks aggregator. Safe to call once at startup."""
        if Folks is None:
            log.info("folks typelib not present — contacts lookup disabled")
            return
        try:
            self._aggregator = Folks.IndividualAggregator.dup()
        except Exception as exc:  # noqa: BLE001
            log.warning("folks aggregator unavailable: %s", exc)
            return
        self._aggregator.connect(
            "individuals-changed-detailed", self._on_individuals_changed)
        self._aggregator.prepare(self._on_prepared)

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
        if self._aggregator is None:
            return
        new_index: dict[str, str] = {}
        individuals = self._aggregator.props.individuals
        # GeeMap iter: pull values
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
        if new_index != self._by_number:
            log.info("contacts index: %d numbers", len(new_index))
            self._by_number = new_index
            self.emit("index-changed")
