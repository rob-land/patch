"""Dialer page: number entry + dialpad + recent calls."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from gi.repository import Adw, Gio, GLib, Gtk

from patch.numfmt import format_as_typed, normalize_e164, number_to_jid

log = logging.getLogger(__name__)


_STATE_ICONS = {
    "ended":     "call-stop-symbolic",
    "active":    "call-start-symbolic",
    "rejected":  "call-missed-symbolic",
    "retracted": "call-missed-symbolic",
}


@Gtk.Template(resource_path="/land/rob/patch/ui/dialer.ui")
class PatchDialerPage(Adw.Bin):
    __gtype_name__ = "PatchDialerPage"

    recent_stack:    Gtk.Stack    = Gtk.Template.Child()
    recent_list:     Gtk.ListBox  = Gtk.Template.Child()
    number_entry:    Gtk.Entry    = Gtk.Template.Child()
    backspace_button: Gtk.Button  = Gtk.Template.Child()
    message_button:  Gtk.Button   = Gtk.Template.Child()
    call_button:     Gtk.Button   = Gtk.Template.Child()

    def __init__(self, account, store=None, calls=None):
        super().__init__()
        self._account = account
        self._store = store
        self._calls = calls

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
        self.message_button.connect("clicked", self._on_message)
        self.recent_list.connect("row-activated", self._on_recent_activated)
        # Reformat the number as it changes — same UX as the iOS/
        # Android system dialer. Underlying value (digits + DTMF chars)
        # is preserved; we only adjust the visible grouping.
        self._suppress_reformat = False
        self._raw_number = ""
        self.number_entry.connect("notify::text", self._on_entry_changed)

        if self._calls is not None:
            # Refresh the recents whenever a call terminates (the manager
            # logs the call entry at terminal transition).
            self._calls.connect("call-ended",
                                lambda *_: self._refresh_recent())

        self._refresh_recent()

    # -- input handlers ----------------------------------------------------

    def _on_dial_digit(self, _action, param):
        digit = param.get_string()
        # Track raw input ourselves so re-formatting doesn't lose chars.
        self._raw_number += digit
        self._render_entry()

    def _on_backspace(self, *_):
        # Strip the last raw digit (not the last formatted char — that
        # might be a space or paren we inserted ourselves).
        if not self._raw_number:
            return
        self._raw_number = self._raw_number[:-1]
        self._render_entry()

    def _on_entry_changed(self, *_):
        if self._suppress_reformat:
            return
        # The user typed into the entry directly. Capture the digit
        # content and re-render.
        text = self.number_entry.get_text()
        # Keep only the bits we accept as input — digits, '+', '*', '#'.
        cleaned = "".join(c for c in text if c.isdigit() or c in "+*#")
        if cleaned != self._raw_number:
            self._raw_number = cleaned
            self._render_entry()

    def _render_entry(self) -> None:
        self._suppress_reformat = True
        try:
            self.number_entry.set_text(format_as_typed(self._raw_number))
            # Park the cursor at the end so the next digit lands there.
            self.number_entry.set_position(-1)
        finally:
            self._suppress_reformat = False

    def _parse_entry(self) -> str | None:
        # Use the raw input we've been tracking, not the formatted text
        # — normalize_e164 strips formatting characters but it's
        # cleaner to start from the user's original digits.
        normalized = normalize_e164(self._raw_number, default_country="US")
        if not normalized:
            log.info("dial: could not parse %r as a phone number",
                     self._raw_number)
            self.activate_action("win.toast", GLib.Variant("s", "Invalid number"))
            return None
        return normalized

    def _on_call(self, *_):
        normalized = self._parse_entry()
        if not normalized:
            return
        jid = number_to_jid(normalized, self._account.gateway)
        log.info("dial: %s -> %s", normalized, jid)
        # win.start-call wires through main.py to CallManager and brings
        # up the call dialog. Real audio is still TBD — JMI signalling
        # only for now — but the user gets a working call surface.
        self.activate_action("win.start-call", GLib.Variant("s", jid))

    def _on_message(self, *_):
        normalized = self._parse_entry()
        if not normalized:
            return
        jid = number_to_jid(normalized, self._account.gateway)
        log.info("message: opening conversation for %s -> %s", normalized, jid)
        # Window-scoped action: switches to the Messages tab and pushes
        # the thread page for this JID (creating the conversation if
        # it's new — see PatchMessagesPage.open_conversation).
        self.activate_action("win.open-conversation", GLib.Variant("s", jid))

    # -- recent calls ----------------------------------------------------

    def _refresh_recent(self) -> None:
        if self._store is None:
            self.recent_stack.set_visible_child_name("empty")
            return
        calls = self._store.recent_calls(limit=30)
        # Clear existing rows.
        while True:
            row = self.recent_list.get_first_child()
            if row is None:
                break
            self.recent_list.remove(row)
        if not calls:
            self.recent_stack.set_visible_child_name("empty")
            return
        for c in calls:
            self.recent_list.append(_make_call_row(c))
        self.recent_stack.set_visible_child_name("list")

    def _on_recent_activated(self, _list, row):
        peer_jid = row.get_name()
        if not peer_jid:
            return
        # Tap to redial. Same path the dialer Call button uses.
        self.activate_action(
            "win.start-call", GLib.Variant("s", peer_jid))

    # -- view-stack accessor used by the window's tab wiring -----------

    def get_page_props(self) -> dict:
        """Return the kwargs the window uses when adding us to the ViewStack."""
        return {
            "name":       "dialer",
            "title":      "Dialer",
            "icon_name":  "call-start-symbolic",
        }


def _make_call_row(call: dict) -> Adw.ActionRow:
    when = dt.datetime.fromtimestamp(call["started_at"])
    today = dt.date.today()
    if when.date() == today:
        ts_label = when.strftime("%H:%M")
    elif (today - when.date()).days < 7:
        ts_label = when.strftime("%a %H:%M")
    else:
        ts_label = when.strftime("%b %-d")
    direction_icon = "go-up-symbolic" if call["direction"] == "outgoing" else "go-down-symbolic"
    state_icon = _STATE_ICONS.get(call["state"], "call-stop-symbolic")
    subtitle = f"{call['direction'].capitalize()} · {call['state']} · {ts_label}"
    row = Adw.ActionRow(
        title=call.get("peer_label") or call["peer_jid"],
        subtitle=subtitle,
    )
    row.set_name(call["peer_jid"])
    row.add_prefix(Gtk.Image.new_from_icon_name(direction_icon))
    row.add_suffix(Gtk.Image.new_from_icon_name(state_icon))
    return row
