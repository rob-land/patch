"""Add-to-contacts dialog.

Reached from the conversation overflow menu when the open number isn't
already a known contact. The user either creates a fresh contact or
folds the number into an existing one; ContactsManager handles the
write (system address book via EDS when available, else contacts.json)
and refreshes its index, so the thread title updates to the new name.
"""

from __future__ import annotations

import logging

from gi.repository import Adw, GLib, Gtk

from patch import numfmt

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/patch/ui/add-contact-dialog.ui")
class PatchAddContactDialog(Adw.Dialog):
    __gtype_name__ = "PatchAddContactDialog"

    number_row:  Adw.ActionRow = Gtk.Template.Child()
    target_row:  Adw.ComboRow  = Gtk.Template.Child()
    name_row:    Adw.EntryRow  = Gtk.Template.Child()
    save_button: Gtk.Button    = Gtk.Template.Child()

    def __init__(self, contacts, number_e164: str, parent_window):
        super().__init__()
        self._contacts = contacts
        self._number = number_e164
        self._parent_window = parent_window

        self.number_row.set_subtitle(numfmt.format_for_display(number_e164))

        # Combo: "New contact" first, then every existing contact. The
        # row index maps onto _target_ids (None == create new).
        self._target_ids: list[str | None] = [None]
        model = Gtk.StringList.new(["New contact"])
        for cid, name in contacts.contact_targets():
            self._target_ids.append(cid)
            model.append(name)
        self.target_row.set_model(model)
        self.target_row.set_expression(
            Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))

        self.target_row.connect("notify::selected", self._on_target_changed)
        self.save_button.connect("clicked", self._on_save)
        self.name_row.connect("entry-activated", self._on_save)
        self._on_target_changed()

    def _creating_new(self) -> bool:
        return self._target_ids[self.target_row.get_selected()] is None

    def _on_target_changed(self, *_):
        self.name_row.set_visible(self._creating_new())

    def _on_save(self, *_):
        if self._creating_new():
            name = self.name_row.get_text().strip()
            if not name:
                self.name_row.add_css_class("error")
                return
            ok = self._contacts.create_contact(name, self._number)
            done = name
        else:
            cid = self._target_ids[self.target_row.get_selected()]
            ok = self._contacts.add_number_to_contact(cid, self._number)
            done = self.target_row.get_selected_item().get_string()

        if ok:
            self.force_close()
            self._parent_window.activate_action(
                "win.toast", GLib.Variant("s", f"Saved to {done}"))
        else:
            self._parent_window.activate_action(
                "win.toast", GLib.Variant("s", "Couldn’t save contact"))
