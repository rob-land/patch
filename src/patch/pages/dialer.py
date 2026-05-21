"""Dialer page: number entry + dialpad + recent calls."""

from __future__ import annotations

import logging
from typing import Optional

from gi.repository import Adw, Gio, GLib, Gtk

from patch.numfmt import normalize_e164

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/patch/ui/dialer.ui")
class PatchDialerPage(Adw.Bin):
    __gtype_name__ = "PatchDialerPage"

    recent_stack:   Gtk.Stack    = Gtk.Template.Child()
    recent_list:    Gtk.ListBox  = Gtk.Template.Child()
    number_entry:   Gtk.Entry    = Gtk.Template.Child()
    backspace_button: Gtk.Button = Gtk.Template.Child()
    call_button:    Gtk.Button   = Gtk.Template.Child()

    def __init__(self, account):
        super().__init__()
        self._account = account

        # Local action group "patch" — the per-digit dial buttons fire
        # `patch.dial-digit("3")` from the .blp. Variant payload is a
        # single string ("0".."9", "*", "#") so one handler covers all 12.
        actions = Gio.SimpleActionGroup()
        dial_digit = Gio.SimpleAction.new("dial-digit", GLib.VariantType.new("s"))
        dial_digit.connect("activate", self._on_dial_digit)
        actions.add_action(dial_digit)
        self.insert_action_group("patch", actions)

        self.backspace_button.connect("clicked", self._on_backspace)
        self.call_button.connect("clicked", self._on_call)

        # No recent-calls store yet; show the empty page. Wired up in a
        # later phase when call history lands.
        self.recent_stack.set_visible_child_name("empty")

    # -- input handlers ----------------------------------------------------

    def _on_dial_digit(self, _action, param):
        digit = param.get_string()
        buffer = self.number_entry.get_buffer()
        buffer.insert_text(buffer.get_length(), digit, len(digit.encode()))

    def _on_backspace(self, *_):
        buffer = self.number_entry.get_buffer()
        length = buffer.get_length()
        if length > 0:
            buffer.delete_text(length - 1, 1)

    def _on_call(self, *_):
        text = self.number_entry.get_text().strip()
        # Use the account's gateway country if set; default GSettings is "US".
        country = "US"
        normalized = normalize_e164(text, default_country=country)
        if not normalized:
            log.info("dial: could not parse %r as a phone number", text)
            self.activate_action("win.toast", GLib.Variant("s", "Invalid number"))
            return
        log.info("dial: %s (would route via gnome-calls in Phase 3)", normalized)
        # Phase 3 hook point: this is where the gnome-calls plugin
        # gets activated with the normalized number. For Phase 0 we
        # just surface the parsed number as a toast.
        self.activate_action("win.toast", GLib.Variant("s", f"Would dial {normalized}"))

    # -- view-stack accessor used by the window's tab wiring --------------

    def get_page_props(self) -> dict:
        """Return the kwargs the window uses when adding us to the ViewStack."""
        return {
            "name":       "dialer",
            "title":      "Dialer",
            "icon_name":  "call-start-symbolic",
        }
