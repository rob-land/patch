"""Voicemail page.

JMP delivers voicemails as chat messages with an audio attachment (OOB
URL) and the transcribed text in the body. Our store records them in
the same `messages` table that holds SMS; the voicemail page filters
to messages whose attachment is audio-typed.

Each row is an expandable Adw.ExpanderRow with the transcript as the
subtitle and inline Gtk.MediaControls (Gtk.MediaFile loaded from the
URL) inside the expansion. No download to disk — Gtk.MediaFile +
GFile.new_for_uri() streams the audio.
"""

from __future__ import annotations

import datetime as dt
import logging

from gi.repository import Adw, Gio, GLib, Gtk

from patch import numfmt

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/patch/ui/voicemail.ui")
class PatchVoicemailPage(Adw.Bin):
    __gtype_name__ = "PatchVoicemailPage"

    voicemail_stack: Gtk.Stack    = Gtk.Template.Child()
    voicemail_list:  Gtk.ListBox  = Gtk.Template.Child()

    def __init__(self, account, store=None, xmpp=None, contacts=None):
        super().__init__()
        self._account = account
        self._store = store
        self._xmpp = xmpp
        self._contacts = contacts

        # Live: refresh when any new message arrives, since voicemails
        # come through the same message-received path.
        if self._xmpp is not None:
            self._xmpp.connect("message-received",
                               lambda *_: self._refresh())
        if self._contacts is not None:
            self._contacts.connect("index-changed",
                                   lambda *_: self._refresh())

        self._refresh()

    def get_page_props(self) -> dict:
        return {
            "name":       "voicemail",
            "title":      "Voicemail",
            "icon_name":  "media-record-symbolic",
        }

    # -- list ------------------------------------------------------------

    def _refresh(self) -> None:
        if self._store is None:
            self.voicemail_stack.set_visible_child_name("empty")
            return
        rows = self._store.recent_voicemails(limit=50)
        # Clear existing children.
        while True:
            row = self.voicemail_list.get_first_child()
            if row is None:
                break
            self.voicemail_list.remove(row)
        if not rows:
            self.voicemail_stack.set_visible_child_name("empty")
            return
        for r in rows:
            self.voicemail_list.append(self._make_row(r))
        self.voicemail_stack.set_visible_child_name("list")

    def _make_row(self, msg: dict) -> Gtk.Widget:
        title = self._display_name_for(msg["remote_jid"])
        when = dt.datetime.fromtimestamp(msg["timestamp"])
        ts_label = when.strftime("%a %b %-d, %H:%M")
        # Transcript or fallback to the URL when JMP failed to transcribe.
        body = msg.get("body") or ""
        url = msg.get("attachment_url") or ""
        if body.strip() == url.strip():
            body = "(no transcript)"

        expander = Adw.ExpanderRow(title=title, subtitle=ts_label)
        # Transcript preview as a label inside the expanded body.
        transcript = Gtk.Label(
            label=body,
            wrap=True,
            wrap_mode=2,
            xalign=0,
            margin_start=12, margin_end=12,
            margin_top=4, margin_bottom=4,
            selectable=True,
        )
        expander.add_row(_wrap_in_row(transcript))

        # Inline audio player. Gtk.MediaFile loads the URL lazily on the
        # first prepare() — we defer that until the row is expanded so a
        # voicemail list of 50 doesn't issue 50 GETs upfront.
        controls_holder = Gtk.Box()
        expander.add_row(_wrap_in_row(controls_holder))

        def _on_expanded(_row, _param):
            if not _row.get_expanded():
                return
            if controls_holder.get_first_child() is not None:
                return     # already wired
            mf = Gtk.MediaFile.new_for_file(Gio.File.new_for_uri(url))
            controls = Gtk.MediaControls(media_stream=mf)
            controls.set_margin_start(12); controls.set_margin_end(12)
            controls.set_margin_top(4);    controls.set_margin_bottom(4)
            controls_holder.append(controls)

        expander.connect("notify::expanded", _on_expanded)
        return expander

    def _display_name_for(self, jid: str) -> str:
        if numfmt.is_group_jid(jid):
            local = jid.partition("@")[0]
            parts = []
            for n in local.split(","):
                name = self._contacts.lookup(n) if self._contacts else None
                parts.append(name or numfmt.format_for_display(n))
            return ", ".join(parts)
        number = numfmt.jid_to_number(jid, self._account.gateway)
        if number:
            name = self._contacts.lookup(number) if self._contacts else None
            return name or numfmt.format_for_display(number)
        return jid


def _wrap_in_row(child: Gtk.Widget) -> Gtk.ListBoxRow:
    row = Gtk.ListBoxRow(selectable=False, activatable=False)
    row.set_child(child)
    return row
