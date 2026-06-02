"""New-message entry dialog.

The popup the user gets from the Messages tab's floating-action button.
They can either type a phone number and tap Open, or search and tap a
known contact — both paths normalise to E.164, build the JID, and route
through win.open-conversation (the same path the dialer's Send Message
button uses), so a new conversation appears in the list once the first
message is sent.
"""

from __future__ import annotations

import logging

from gi.repository import Adw, Gio, GLib, Gtk

from patch import APP_ID
from patch.numfmt import format_for_display, normalize_e164, number_to_jid

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/patch/ui/new-message-dialog.ui")
class PatchNewMessageDialog(Adw.Dialog):
    __gtype_name__ = "PatchNewMessageDialog"

    number_row:     Adw.EntryRow    = Gtk.Template.Child()
    open_button:    Gtk.Button      = Gtk.Template.Child()
    search_entry:   Gtk.SearchEntry = Gtk.Template.Child()
    contacts_stack: Gtk.Stack       = Gtk.Template.Child()
    contacts_list:  Gtk.ListBox     = Gtk.Template.Child()
    empty_page:     Adw.StatusPage  = Gtk.Template.Child()

    def __init__(self, account, parent_window, contacts=None):
        super().__init__()
        self._settings = Gio.Settings.new(APP_ID)
        self._account = account
        self._parent_window = parent_window
        self._contacts = contacts
        self._rows: list[Adw.ActionRow] = []

        self.open_button.connect("clicked", self._on_open)
        # Pressing Enter in the entry should trigger Open too.
        self.number_row.connect("entry-activated", self._on_open)
        # Drop the error state once the user starts editing the number.
        self.number_row.connect("changed", self._on_number_changed)

        self.search_entry.connect("search-changed", self._on_search_changed)
        self.contacts_list.connect("row-activated", self._on_contact_activated)
        self.contacts_list.set_filter_func(self._filter_contact)

        self._populate_contacts()

    # -- manual number ---------------------------------------------------

    def _on_number_changed(self, *_):
        self.number_row.remove_css_class("error")

    def _on_open(self, *_):
        raw = self.number_row.get_text().strip()
        default_country = self._settings.get_string("default-country") or "US"
        normalized = normalize_e164(raw, default_country=default_country)
        if not normalized:
            self.number_row.add_css_class("error")
            return
        self._open_jid(number_to_jid(normalized, self._account.gateway))

    # -- contacts --------------------------------------------------------

    def _populate_contacts(self) -> None:
        contacts = self._contacts.all_contacts() if self._contacts else {}
        # No contacts loaded: hide the search box and show the placeholder.
        self.search_entry.set_visible(bool(contacts))
        if not contacts:
            self.contacts_stack.set_visible_child_name("empty")
            return
        for number, name in sorted(contacts.items(),
                                   key=lambda kv: kv[1].casefold()):
            row = Adw.ActionRow(
                title=name,
                subtitle=format_for_display(number),
                activatable=True,
            )
            # Names/numbers are plain text — never interpret them as markup.
            row.set_use_markup(False)
            # Stash the raw E.164 number for filtering + activation.
            row.set_name(number)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            self.contacts_list.append(row)
            self._rows.append(row)
        self.contacts_stack.set_visible_child_name("list")

    def _on_search_changed(self, _entry):
        self.contacts_list.invalidate_filter()
        # Swap in a "no matches" placeholder when the query filters
        # everything out, so the boxed list never collapses to nothing.
        query = self.search_entry.get_text().strip().casefold()
        if query and not any(self._row_matches(row, query)
                             for row in self._rows):
            self.empty_page.set_title("No matches")
            self.empty_page.set_description(
                f"No contact matches “{self.search_entry.get_text().strip()}”.")
            self.contacts_stack.set_visible_child_name("empty")
        else:
            self.contacts_stack.set_visible_child_name("list")

    def _filter_contact(self, row) -> bool:
        return self._row_matches(
            row, self.search_entry.get_text().strip().casefold())

    @staticmethod
    def _row_matches(row, query: str) -> bool:
        if not query:
            return True
        # Match the contact name, the formatted number, and the raw
        # E.164 digits so "713" hits "+1 (713) …" and "+17135551234".
        return any(query in haystack for haystack in (
            row.get_title().casefold(),
            row.get_subtitle().casefold(),
            row.get_name(),
        ))

    def _on_contact_activated(self, _list, row):
        number = row.get_name()
        if number:
            self._open_jid(number_to_jid(number, self._account.gateway))

    # -- shared ----------------------------------------------------------

    def _open_jid(self, jid: str) -> None:
        self.force_close()
        self._parent_window.activate_action(
            "win.open-conversation", GLib.Variant("s", jid))
