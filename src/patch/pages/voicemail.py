"""Voicemail page: JMP voicemail messages with transcripts.

Phase 0: empty state. Voicemail handling lands in Phase 7 once messaging
is solid — JMP delivers voicemails as XMPP messages with audio attachments
plus a transcript, so most of the plumbing is shared with the regular
messages path.
"""

from __future__ import annotations

import logging

from gi.repository import Adw, Gtk

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/patch/ui/voicemail.ui")
class PatchVoicemailPage(Adw.Bin):
    __gtype_name__ = "PatchVoicemailPage"

    voicemail_stack: Gtk.Stack    = Gtk.Template.Child()
    voicemail_list:  Gtk.ListBox  = Gtk.Template.Child()

    def __init__(self, account):
        super().__init__()
        self._account = account
        self.voicemail_stack.set_visible_child_name("empty")

    def get_page_props(self) -> dict:
        return {
            "name":       "voicemail",
            "title":      "Voicemail",
            "icon_name":  "media-record-symbolic",
        }
