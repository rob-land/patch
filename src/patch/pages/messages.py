"""Messages page: conversation list with detail-view navigation.

Phase 0: empty state only. Phase 1 wires up the XMPP message store, MAM
sync, and an inline detail view via NavigationView.
"""

from __future__ import annotations

import logging

from gi.repository import Adw, Gtk

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/patch/ui/messages.ui")
class PatchMessagesPage(Adw.Bin):
    __gtype_name__ = "PatchMessagesPage"

    messages_stack:     Gtk.Stack    = Gtk.Template.Child()
    conversations_list: Gtk.ListBox  = Gtk.Template.Child()

    def __init__(self, account):
        super().__init__()
        self._account = account
        self.messages_stack.set_visible_child_name("empty")

    def get_page_props(self) -> dict:
        return {
            "name":       "messages",
            "title":      "Messages",
            "icon_name":  "user-available-symbolic",
        }
