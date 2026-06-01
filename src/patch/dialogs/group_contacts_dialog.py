"""Group-participants contact dialog.

A cheogram group-SMS JID encodes every participant's number in the
localpart (``+1555...,+1666...@cheogram.com``). When one or more of
those numbers isn't a known contact, the conversation overflow menu
opens this dialog: one row per participant, showing the contact name
when we have it and the formatted number when we don't. Tapping an
unknown row hands off to the same single-number PatchAddContactDialog
used for 1-on-1 threads, so adding a group member is identical to
adding any other unknown number.

The row list rebuilds on the contacts index-changed signal, so a
number flips from "Not in contacts" to its new name in place once the
add flow completes.
"""

from __future__ import annotations

import logging

from gi.repository import Adw, Gtk

from patch import numfmt

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/patch/ui/group-contacts-dialog.ui")
class PatchGroupContactsDialog(Adw.Dialog):
    __gtype_name__ = "PatchGroupContactsDialog"

    participants_group: Adw.PreferencesGroup = Gtk.Template.Child()

    def __init__(self, contacts, gateway: str, group_jid: str, parent_window):
        super().__init__()
        self._contacts = contacts
        self._gateway = gateway
        self._group_jid = group_jid
        self._parent_window = parent_window
        self._rows: list[Gtk.Widget] = []

        self._rebuild()
        # Keep the list current: when a number is added to contacts the
        # index changes and the matching row should flip to the name.
        self._index_handler = contacts.connect(
            "index-changed", lambda *_: self._rebuild())
        self.connect("closed", self._on_closed)

    def _participants(self) -> list[str]:
        """E.164 numbers for each group participant, in JID order."""
        domain = self._group_jid.partition("@")[2]
        numbers = []
        for part in self._group_jid.partition("@")[0].split(","):
            number = numfmt.jid_to_number(f"{part}@{domain}", self._gateway)
            if number:
                numbers.append(number)
        return numbers

    def _rebuild(self, *_):
        for row in self._rows:
            self.participants_group.remove(row)
        self._rows = []
        for number in self._participants():
            row = self._make_row(number)
            self.participants_group.add(row)
            self._rows.append(row)

    def _make_row(self, number: str) -> Adw.ActionRow:
        name = self._contacts.lookup(number) if self._contacts else None
        row = Adw.ActionRow()
        if name:
            row.set_title(name)
            row.set_subtitle(numfmt.format_for_display(number))
            check = Gtk.Image.new_from_icon_name("object-select-symbolic")
            check.add_css_class("dim-label")
            row.add_suffix(check)
        else:
            row.set_title(numfmt.format_for_display(number))
            row.set_subtitle("Not in contacts")
            row.set_activatable(True)
            row.add_suffix(
                Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", self._on_add, number)
        return row

    def _on_add(self, _row, number: str):
        from patch.dialogs.add_contact_dialog import PatchAddContactDialog
        dialog = PatchAddContactDialog(
            self._contacts, number, self._parent_window)
        dialog.present(self)

    def _on_closed(self, *_):
        if self._index_handler:
            self._contacts.disconnect(self._index_handler)
            self._index_handler = 0
