"""Messages page: conversation list + thread view + compose.

Uses an in-process MessageStore (SQLite) so the UI has something to render
between launches and before MAM catches up. The XMPP connection lives in
the parent application; this page just listens to its `message-received`
signal.

State machine:
  empty   no messages anywhere -> StatusPage
  list    one or more conversations -> ListBox with rows
  thread  user has tapped a row -> NavigationView pushes the thread page

The narrow / wide breakpoint is handled by the AdwViewSwitcher in the
parent; this page is just one of three top-level tabs and doesn't need
a split-view itself.
"""

from __future__ import annotations

import datetime as dt
import logging

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from patch import account as account_mod
from patch import numfmt

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/patch/ui/messages.ui")
class PatchMessagesPage(Adw.Bin):
    __gtype_name__ = "PatchMessagesPage"

    nav:               Adw.NavigationView = Gtk.Template.Child()
    list_page:         Adw.NavigationPage = Gtk.Template.Child()
    thread_page:       Adw.NavigationPage = Gtk.Template.Child()
    messages_stack:    Gtk.Stack          = Gtk.Template.Child()
    conversations_list: Gtk.ListBox       = Gtk.Template.Child()
    thread_title:      Adw.WindowTitle    = Gtk.Template.Child()
    thread_list:       Gtk.ListBox        = Gtk.Template.Child()
    compose_entry:     Gtk.Entry          = Gtk.Template.Child()
    send_button:       Gtk.Button         = Gtk.Template.Child()

    def __init__(self, account, store, xmpp):
        super().__init__()
        self._account = account
        self._store = store
        self._xmpp = xmpp
        self._open_jid: str | None = None

        self.conversations_list.connect("row-activated", self._on_row_activated)
        self.compose_entry.connect("activate", self._on_compose_activate)
        self.send_button.connect("clicked", self._on_compose_activate)

        actions = Gio.SimpleActionGroup()
        send_action = Gio.SimpleAction.new("send-message", None)
        send_action.connect("activate", self._on_compose_activate)
        actions.add_action(send_action)
        self.insert_action_group("patch", actions)

        # Listen for inbound messages on the XMPP client. Outbound echos
        # come through the same signal so the conversation list updates
        # without a round-trip via MAM.
        self._xmpp.connect("message-received", self._on_message_received)

        self._refresh_conversation_list()

    def get_page_props(self) -> dict:
        return {
            "name":       "messages",
            "title":      "Messages",
            "icon_name":  "user-available-symbolic",
        }

    # -- conversation list -----------------------------------------------

    def _refresh_conversation_list(self) -> None:
        convs = self._store.conversations()
        # Clear existing rows.
        while True:
            row = self.conversations_list.get_first_child()
            if row is None:
                break
            self.conversations_list.remove(row)

        if not convs:
            self.messages_stack.set_visible_child_name("empty")
            return

        gateway = self._account.gateway
        for c in convs:
            row = Adw.ActionRow(
                title=self._display_name_for(c["remote_jid"], gateway),
                subtitle=_truncate(c.get("last_body") or ""),
            )
            # Store the JID on the row so the activation handler knows
            # which conversation to open.
            row.set_name(c["remote_jid"])
            if c.get("unread"):
                badge = Gtk.Label(label=str(c["unread"]))
                badge.add_css_class("numeric")
                badge.add_css_class("accent")
                row.add_suffix(badge)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            self.conversations_list.append(row)
        self.messages_stack.set_visible_child_name("list")

    # -- thread view -----------------------------------------------------

    def _on_row_activated(self, _listbox, row):
        jid = row.get_name()
        if not jid:
            return
        self._open_thread(jid)

    def _open_thread(self, remote_jid: str) -> None:
        self._open_jid = remote_jid
        self.thread_title.set_title(
            self._display_name_for(remote_jid, self._account.gateway))
        # Repopulate the thread list.
        while True:
            child = self.thread_list.get_first_child()
            if child is None:
                break
            self.thread_list.remove(child)
        for msg in self._store.thread(remote_jid):
            self.thread_list.append(_render_thread_row(msg))
        self._store.mark_read(remote_jid)
        # Refresh the conversations list so unread badges clear.
        self._refresh_conversation_list()
        self.nav.push(self.thread_page)

    def _on_compose_activate(self, *_):
        if not self._open_jid:
            return
        body = self.compose_entry.get_text().strip()
        if not body:
            return
        if self._account.state != account_mod.STATE_CONNECTED:
            self.activate_action(
                "win.toast",
                GLib.Variant("s", "Not connected — message not sent"))
            return
        ok = self._xmpp.send_chat_message(self._open_jid, body)
        if not ok:
            self.activate_action(
                "win.toast", GLib.Variant("s", "Send failed"))
            return
        self.compose_entry.set_text("")

    # -- inbound -----------------------------------------------------------

    def _on_message_received(self, _xmpp, remote_jid, body, incoming, timestamp):
        # Group SMS bodies on JMP carry the sender in the body itself; split
        # that out so we can render it as a separate row label.
        sender_jid = None
        if numfmt.is_group_jid(remote_jid):
            sender_jid, body = numfmt.parse_group_body(body)
        self._store.add_message(
            remote_jid, bool(incoming), body, timestamp, sender_jid)
        if self._open_jid == remote_jid:
            # Append directly to the visible thread without a full refetch.
            msg = {
                "remote_jid": remote_jid,
                "incoming":   bool(incoming),
                "body":       body,
                "sender_jid": sender_jid,
                "timestamp":  timestamp,
                "read":       1,
            }
            self.thread_list.append(_render_thread_row(msg))
            self._store.mark_read(remote_jid)
        self._refresh_conversation_list()

    # -- display helpers --------------------------------------------------

    def _display_name_for(self, jid: str, gateway: str) -> str:
        # For cheogram-style group JIDs, render as a list of formatted numbers.
        if numfmt.is_group_jid(jid):
            local = jid.partition("@")[0]
            return ", ".join(numfmt.format_for_display(n) for n in local.split(","))
        number = numfmt.jid_to_number(jid, gateway)
        if number:
            return numfmt.format_for_display(number)
        return jid


# Module-level pure helpers so the page class stays focused on UI wiring.

def _truncate(s: str, n: int = 64) -> str:
    if len(s) <= n:
        return s
    return s[:n - 1] + "…"


def _render_thread_row(msg: dict) -> Gtk.Widget:
    align = Gtk.Align.START if msg["incoming"] else Gtk.Align.END
    bubble = Gtk.Label(
        label=msg["body"],
        wrap=True,
        wrap_mode=2,  # WORD_CHAR
        max_width_chars=40,
        xalign=0 if msg["incoming"] else 1,
        halign=align,
        selectable=True,
    )
    bubble.add_css_class("card")
    bubble.add_css_class("body")
    bubble.set_margin_start(12)
    bubble.set_margin_end(12)
    bubble.set_margin_top(4)
    bubble.set_margin_bottom(4)

    row = Gtk.ListBoxRow(selectable=False, activatable=False)
    row.set_child(bubble)
    row.set_margin_top(2)
    row.set_margin_bottom(2)
    return row


def _format_ts(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp).strftime("%H:%M")
