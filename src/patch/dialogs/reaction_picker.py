"""Reaction picker sheet — ported from Banter's ReactionsSheet.

A single-message emoji picker plus a "Reacted" summary of who reacted
with what. Unlike Banter (GroupMe powerup packs over a REST API) this
is XEP-0444 over XMPP, so there are no image packs — just the shared
Unicode reaction set. Picking an emoji REPLACES the user's reaction on
the message (picking the one you already gave removes it), matching
Banter's toggle semantics and SMS tapback behaviour.

Built imperatively (no .blp) because the emoji grid and reactor list
are fully dynamic — same approach Banter takes.
"""

from __future__ import annotations

from gi.repository import Adw, Gtk

# Shared Unicode reaction set — copied from Banter's constants.py so the
# two apps offer the same palette.
DEFAULT_REACTIONS = [
    # Hearts + affection
    "❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍", "🤎",
    "❤️‍🔥", "💖", "💗", "💓", "💞", "💕", "💘", "💝", "💔",
    # Smiley faces
    "😂", "🤣", "😊", "😇", "🙂", "😎", "🥰", "😍", "😘", "🥲",
    "🤔", "🙄", "😐", "😏", "😑", "😴", "🥱", "🤤", "🤗", "🤭",
    "🤫", "🤐", "🤪", "🥳", "🤓", "🧐", "😮", "😯", "😲", "🤯",
    "😢", "😭", "🥺", "😤", "😠", "😡", "🤬", "🤮", "🤢", "🤕",
    "😱", "😨", "😰", "😓", "🥶", "🥵", "🫠", "🫡", "🫢",
    # Gestures + hands
    "👍", "👎", "👏", "🙏", "🫶", "🤝", "🤲", "👐", "🙌", "💪",
    "✌️", "🤞", "🤟", "🤘", "🤙", "👌", "🫰", "👀", "🤷", "🤦",
    # Objects + symbols
    "🔥", "🎉", "✨", "💯", "💀", "☠️", "💩", "🐐", "👑", "💎",
    "🎂", "🍕", "☕", "🍺", "🍻", "🥂", "🌟", "⭐", "⚡", "☀️",
    "✅", "❌", "⚠️", "💅", "🗣️", "💬", "👋", "🫣", "🫵", "🙈",
]

# The six surfaced in the long-press quick row, kept here so the picker
# and the quick popover stay in sync.
QUICK_REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏"]

_CELL = 40


class PatchReactionPicker(Adw.Dialog):
    """Emoji picker + reactor summary for one message."""

    def __init__(self, *, target_id: str, by_sender: dict, own_jid: str,
                 contacts, on_apply, parent_window):
        super().__init__()
        self._target_id = target_id
        self._own_jid = own_jid
        self._contacts = contacts
        self._on_apply = on_apply
        self._parent_window = parent_window
        self._own_emojis = set(by_sender.get(own_jid, []))

        self.set_title("Reactions")
        self.set_content_width(420)
        self.set_content_height(560)

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())

        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        body.set_margin_start(16)
        body.set_margin_end(16)
        body.set_margin_top(12)
        body.set_margin_bottom(16)

        given = self._build_given_section(by_sender)
        if given is not None:
            body.append(given)
        body.append(self._build_emoji_section())

        scroll.set_child(body)
        tv.set_content(scroll)
        self.set_child(tv)

    # -- sections --------------------------------------------------------

    def _build_emoji_section(self) -> Gtk.Widget:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        heading = Gtk.Label(label="Emoji", xalign=0)
        heading.add_css_class("heading")
        section.append(heading)

        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            column_spacing=4, row_spacing=4,
            min_children_per_line=6, max_children_per_line=10,
            homogeneous=True)
        for code in DEFAULT_REACTIONS:
            flow.append(self._make_emoji_button(code))
        section.append(flow)
        return section

    def _make_emoji_button(self, code: str) -> Gtk.Button:
        btn = Gtk.Button(label=code)
        btn.add_css_class("flat")
        btn.add_css_class("reaction-picker-btn")
        btn.set_size_request(_CELL, _CELL)
        if code in self._own_emojis:
            btn.add_css_class("reaction-picker-mine")
        btn.connect("clicked", self._on_pick, code)
        return btn

    def _build_given_section(self, by_sender: dict) -> Gtk.Widget | None:
        # Aggregate { emoji: [reactor jids] } so the summary lists each
        # emoji once with the people who used it.
        by_emoji: dict[str, list[str]] = {}
        for jid, emojis in by_sender.items():
            for e in emojis or []:
                by_emoji.setdefault(e, []).append(jid)
        if not by_emoji:
            return None

        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        heading = Gtk.Label(label="Reacted", xalign=0)
        heading.add_css_class("heading")
        section.append(heading)

        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        for emoji, jids in by_emoji.items():
            row = Adw.ActionRow(title=f"  ×  {len(jids)}")
            pill = Gtk.Label(label=emoji)
            pill.add_css_class("title-3")
            row.add_prefix(pill)
            names = []
            for jid in jids:
                if jid == self._own_jid:
                    names.append("You")
                elif self._contacts is not None:
                    names.append(self._contacts.label_for_jid(jid))
                else:
                    names.append(jid)
            row.set_subtitle(", ".join(sorted(names)))
            row.set_subtitle_lines(3)
            listbox.append(row)
        section.append(listbox)
        return section

    # -- pick ------------------------------------------------------------

    def _on_pick(self, _btn, code: str):
        # Toggle off if it's the only reaction the user already gave,
        # otherwise replace their reaction with the new one.
        new = [] if self._own_emojis == {code} else [code]
        self.force_close()
        self._on_apply(self._target_id, new)
