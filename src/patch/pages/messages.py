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
    attach_button:     Gtk.Button         = Gtk.Template.Child()
    new_message_button: Gtk.Button        = Gtk.Template.Child()

    def __init__(self, account, store, xmpp, contacts=None, avatars=None):
        super().__init__()
        self._account = account
        self._store = store
        self._xmpp = xmpp
        self._contacts = contacts
        self._avatars = avatars
        self._open_jid: str | None = None
        # Track whether the thread view is currently visible (not just
        # the conversation that was last navigated to). NotificationManager
        # reads `focused_jid()` to decide whether to fire a desktop
        # notification or stay quiet.
        self.nav.connect("notify::visible-page", self._on_nav_changed)

        self.conversations_list.connect("row-activated", self._on_row_activated)
        self.compose_entry.connect("activate", self._on_compose_activate)
        self.send_button.connect("clicked", self._on_compose_activate)
        self.attach_button.connect("clicked", self._on_attach_clicked)
        self.new_message_button.connect("clicked", self._on_new_message_clicked)

        actions = Gio.SimpleActionGroup()
        send_action = Gio.SimpleAction.new("send-message", None)
        send_action.connect("activate", self._on_compose_activate)
        actions.add_action(send_action)
        block_action = Gio.SimpleAction.new("block-thread", None)
        block_action.connect("activate", self._on_block_thread)
        actions.add_action(block_action)
        self.insert_action_group("patch", actions)

        # Listen for inbound messages on the XMPP client. Outbound echos
        # come through the same signal so the conversation list updates
        # without a round-trip via MAM.
        self._xmpp.connect("message-received", self._on_message_received)
        # XEP-0184 delivery receipts: redraw the open thread so the
        # outgoing bubble's ✓ flips to ✓✓ live. Cheap — only renders
        # when the thread is currently visible.
        self._xmpp.connect("message-receipt", self._on_message_receipt)
        # XEP-0444 reactions: the persister has already written the
        # new set; re-render the open thread so the strip updates.
        self._xmpp.connect("reaction-received", self._on_reaction_received)
        # When the contacts index rebuilds, redraw the conversation list
        # so number-only rows pick up the freshly-resolved name.
        if self._contacts is not None:
            self._contacts.connect("index-changed",
                                   lambda *_: self._refresh_conversation_list())
        # XEP-0084 PEP avatars: redraw the conversation list when a new
        # avatar lands on disk so the affected row picks up the icon.
        if self._avatars is not None:
            self._avatars.connect("avatar-changed",
                                  lambda *_: self._refresh_conversation_list())

        self._refresh_conversation_list()

    def get_page_props(self) -> dict:
        return {
            "name":       "messages",
            "title":      "Messages",
            "icon_name":  "user-available-symbolic",
        }

    def focused_jid(self) -> str | None:
        """Return the JID of the currently-visible thread, else None.

        Used by NotificationManager to suppress notifications for the
        conversation the user is staring at.
        """
        visible = self.nav.get_visible_page()
        if visible is None or visible.get_tag() != "thread":
            return None
        return self._open_jid

    def open_conversation(self, remote_jid: str) -> None:
        """Public entry point used by the dialer + notification activation.

        Idempotent: opening an empty conversation just pushes the thread
        page with an empty thread list — the user's first outbound send
        populates the store and the conversation appears in the list on
        the next refresh.
        """
        self._open_thread(remote_jid)

    def _on_nav_changed(self, *_):
        # Whenever the visible page changes (e.g. user backs out of the
        # thread), let the dimmed status push back down through the
        # focused_jid() helper — no state to mutate here, just a hook.
        pass

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
            title = self._display_name_for(c["remote_jid"], gateway)
            row = Adw.ActionRow(
                title=title,
                subtitle=_truncate(c.get("last_body") or ""),
                # Adw.ActionRow defaults to non-activatable, which
                # silently swallows row-activated. Without this, the
                # only way into a thread is the notification tap.
                activatable=True,
            )
            # Store the JID on the row so the activation handler knows
            # which conversation to open.
            row.set_name(c["remote_jid"])
            row.add_prefix(self._build_avatar_widget(c["remote_jid"], title))
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
            self.thread_list.append(_render_thread_row(
                msg, self._contacts, send_reaction=self._send_reaction))
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

    # -- new-conversation flow ------------------------------------------

    def _on_new_message_clicked(self, *_):
        # Lazy import to keep page-init cheap and avoid a circular
        # at module-load time (the dialog imports nothing back into us
        # but still — keep the pages directory cheap to enter).
        from patch.dialogs.new_message_dialog import PatchNewMessageDialog
        window = self.get_root() if isinstance(self.get_root(), Gtk.Window) else None
        if window is None:
            return
        dialog = PatchNewMessageDialog(self._account, window)
        dialog.present(window)

    # -- outbound attach -------------------------------------------------

    def _on_attach_clicked(self, *_):
        if not self._open_jid:
            return
        if self._account.state != account_mod.STATE_CONNECTED:
            self.activate_action(
                "win.toast",
                GLib.Variant("s", "Not connected — can't upload"))
            return
        dialog = Gtk.FileDialog()
        dialog.set_title("Send image")
        # Only show images by default — JMP only sends MMS for image/
        # mime types; other file types route as plain XMPP file shares
        # which the recipient PSTN side can't render.
        filter_img = Gtk.FileFilter()
        filter_img.set_name("Images")
        filter_img.add_mime_type("image/jpeg")
        filter_img.add_mime_type("image/png")
        filter_img.add_mime_type("image/gif")
        filter_img.add_mime_type("image/webp")
        filters = __import__("gi").repository.Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filter_img)
        dialog.set_filters(filters)
        dialog.set_default_filter(filter_img)
        parent = self.get_root() if isinstance(self.get_root(), Gtk.Window) else None
        dialog.open(parent, None, self._on_file_chosen)

    def _on_file_chosen(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
        except Exception:  # noqa: BLE001
            return       # user cancelled or error
        if gfile is None:
            return
        path = gfile.get_path()
        if not path:
            return
        import mimetypes
        import os
        size = os.path.getsize(path)
        filename = os.path.basename(path)
        ctype, _ = mimetypes.guess_type(path)
        ctype = ctype or "application/octet-stream"
        upload_jid = self._upload_service_jid()
        if not upload_jid:
            self.activate_action(
                "win.toast",
                GLib.Variant("s", "No upload service known for this server"))
            return
        log.info("requesting upload slot for %s (%d bytes, %s) via %s",
                 filename, size, ctype, upload_jid)

        # Capture into closure: we need the path again at PUT time.
        def on_slot(put_url, get_url, headers, error):
            if error or not put_url:
                log.warning("upload slot failed: %s", error)
                self.activate_action(
                    "win.toast",
                    GLib.Variant("s", "Upload slot request failed"))
                return
            log.info("got slot: PUT %s -> GET %s", put_url, get_url)
            self._put_file_to_slot(path, ctype, put_url, get_url, headers)

        self._xmpp.request_upload_slot(
            upload_jid, filename, size, ctype, on_slot)

    def _upload_service_jid(self) -> str:
        """Heuristic for the JID of the XEP-0363 HTTP upload component.

        We assume the standard 'upload.<server>' convention used by
        most prosody deployments (including chat.rob.land). Disco-based
        discovery is a later improvement once we have a generalised
        server-info module.
        """
        jid = self._account.jid
        if "@" not in jid:
            return ""
        return "upload." + jid.partition("@")[2]

    def _put_file_to_slot(self, path, ctype, put_url, get_url, headers):
        from gi.repository import GLib, Gio, Soup
        log.info("PUT %s -> %s", path, put_url)
        # Load the file. We could stream it for large attachments, but
        # JMP MMS caps are sub-megabyte — read all is fine.
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            log.warning("upload read failed: %s", exc)
            return

        session = Soup.Session()
        message = Soup.Message.new("PUT", put_url)
        message.get_request_headers().append("Content-Type", ctype)
        for k, v in (headers or {}).items():
            message.get_request_headers().append(k, v)
        message.set_request_body_from_bytes(ctype, GLib.Bytes.new(data))

        def on_put(_sess, result):
            try:
                _ = session.send_and_read_finish(result)
            except GLib.Error as exc:
                log.warning("PUT failed: %s", exc.message)
                self.activate_action(
                    "win.toast",
                    GLib.Variant("s", "Image upload failed"))
                return
            code = message.get_status()
            if code < 200 or code >= 300:
                log.warning("PUT got HTTP %d", code)
                self.activate_action(
                    "win.toast",
                    GLib.Variant("s",
                                 f"Image upload failed (HTTP {code})"))
                return
            log.info("PUT ok, sending chat with OOB url")
            self._xmpp.send_chat_message(
                self._open_jid, get_url, attachment_url=get_url)

        session.send_and_read_async(
            message, GLib.PRIORITY_DEFAULT, None, on_put)

    # -- inbound -----------------------------------------------------------

    def _on_block_thread(self, *_):
        jid = self._open_jid
        if not jid:
            return
        if self._xmpp.block(jid):
            self.activate_action(
                "win.toast",
                GLib.Variant("s", f"Blocked {self._display_name_for(jid, self._account.gateway)}"))
            # Pop back to the conversation list — the user blocked
            # this number, no reason to keep them in the thread.
            self.nav.pop()
        else:
            self.activate_action(
                "win.toast", GLib.Variant("s", "Block failed"))

    def _on_reaction_received(self, _xmpp, _target_id, _sender, conv_jid, _emojis):
        if self._open_jid == conv_jid:
            self._open_thread(conv_jid)

    def _send_reaction(self, target_id: str, emojis: list[str]) -> None:
        if not self._open_jid:
            return
        if not self._xmpp.send_reaction(self._open_jid, target_id, emojis):
            self.activate_action(
                "win.toast", GLib.Variant("s", "Reaction failed"))

    def _on_message_receipt(self, _xmpp, _message_id, _state):
        # We don't try to surgically swap one bubble's glyph — just
        # re-render the visible thread (cheap; messages are small).
        # The conversation list doesn't show receipts so skip refresh
        # for that.
        if self._open_jid is not None:
            self._open_thread(self._open_jid)

    def _on_message_received(self, _xmpp, remote_jid, body, incoming, timestamp,
                             attachment_url, message_id):
        # The MessagePersister (app-level, always alive) writes to the
        # store regardless of whether this page exists. Here we only do
        # view-side work — split the group-SMS body so the thread row
        # renders the inline sender, and refresh the conversation list.
        sender_jid = None
        if numfmt.is_group_jid(remote_jid):
            sender_jid, body = numfmt.parse_group_body(body)
        if self._open_jid == remote_jid:
            # Append directly to the visible thread without a full refetch.
            msg = {
                "remote_jid":     remote_jid,
                "incoming":       bool(incoming),
                "body":           body,
                "sender_jid":     sender_jid,
                "timestamp":      timestamp,
                "read":           1,
                "attachment_url": attachment_url or None,
                "xmpp_id":        message_id if not incoming else None,
                "delivery_state": "sent" if not incoming else None,
            }
            self.thread_list.append(_render_thread_row(
                msg, self._contacts, send_reaction=self._send_reaction))
            self._store.mark_read(remote_jid)
        self._refresh_conversation_list()

    # -- display helpers --------------------------------------------------

    _AVATAR_SIZE = 40

    def _build_avatar_widget(self, jid: str, fallback_text: str) -> Gtk.Widget:
        """Adw.Avatar showing the cached PEP image, falling back to
        initials-on-color when no avatar has been published. For group
        SMS JIDs we always fall through to the fallback — the metadata
        is per-participant, not per-thread."""
        avatar = Adw.Avatar(size=self._AVATAR_SIZE, text=fallback_text or jid,
                            show_initials=True)
        path = None
        if (self._avatars is not None
                and not numfmt.is_group_jid(jid)):
            path = self._avatars.path_for(jid)
        if path:
            try:
                from gi.repository import Gdk
                texture = Gdk.Texture.new_from_filename(path)
                avatar.set_custom_image(texture)
            except Exception as exc:  # noqa: BLE001
                log.debug("avatar texture load failed for %s: %s", jid, exc)
        return avatar

    def _display_name_for(self, jid: str, gateway: str) -> str:
        # For cheogram-style group JIDs, render as a list of formatted
        # numbers (with contact-name substitution per participant).
        if numfmt.is_group_jid(jid):
            local = jid.partition("@")[0]
            parts = []
            for n in local.split(","):
                name = self._contacts.lookup(n) if self._contacts else None
                parts.append(name or numfmt.format_for_display(n))
            return ", ".join(parts)
        number = numfmt.jid_to_number(jid, gateway)
        if number:
            name = self._contacts.lookup(number) if self._contacts else None
            return name or numfmt.format_for_display(number)
        return jid


# Module-level pure helpers so the page class stays focused on UI wiring.

def _truncate(s: str, n: int = 64) -> str:
    if len(s) <= n:
        return s
    return s[:n - 1] + "…"


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif")


def _is_image_url(url: str) -> bool:
    """Cheap heuristic — extension check on the path component.

    Cheogram / JMP attaches MMS images with their original filename, so
    extension-sniffing is reliable. For non-image attachments we still
    surface the URL but skip the image preview.
    """
    if not url:
        return False
    from urllib.parse import urlparse
    return urlparse(url).path.lower().endswith(_IMAGE_EXTS)


# XEP-0444 quick-pick set. Long-press / right-click on a bubble shows
# these in a popover; chosen emoji toggles in the user's reaction set
# for that message.
_QUICK_REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏"]


def _render_thread_row(msg: dict, contacts=None,
                       send_reaction=None) -> Gtk.Widget:
    align = Gtk.Align.START if msg["incoming"] else Gtk.Align.END

    # For group-SMS messages we know the per-message sender JID; render
    # the contact name (falling back to formatted number) as a small
    # label above the bubble so the user can tell who said what.
    # Only on incoming messages — outgoing ones are obviously us.
    sender_label = None
    sender_jid = msg.get("sender_jid")
    if sender_jid and msg["incoming"]:
        label_text = (contacts.label_for_jid(sender_jid)
                      if contacts is not None else sender_jid)
        sender_label = Gtk.Label(
            label=label_text,
            xalign=0,
            halign=Gtk.Align.START,
            wrap=False,
            ellipsize=3,  # PANGO_ELLIPSIZE_END
        )
        sender_label.set_margin_start(16)
        sender_label.set_margin_top(2)
        sender_label.add_css_class("caption")
        sender_label.add_css_class("dim-label")

    bubble_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    bubble_box.set_halign(align)
    bubble_box.set_margin_start(12)
    bubble_box.set_margin_end(12)
    bubble_box.set_margin_top(4)
    bubble_box.set_margin_bottom(4)
    bubble_box.add_css_class("patch-bubble")
    bubble_box.add_css_class(
        "patch-bubble-incoming" if msg["incoming"] else "patch-bubble-outgoing")

    # If there's an image attachment, render the picture above the body
    # text. Loading is async via Soup3 — the placeholder shows the URL
    # until the bytes land.
    url = msg.get("attachment_url") or ""
    if url and _is_image_url(url):
        picture = Gtk.Picture()
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_size_request(240, 180)
        bubble_box.append(picture)
        # Kick off the fetch. Failure leaves the placeholder visible.
        _load_image_async(picture, url)

    body_text = msg["body"] or ""
    # Don't duplicate the URL: if body is exactly the OOB URL, suppress it.
    if url and body_text.strip() == url.strip():
        body_text = ""

    if body_text:
        label = Gtk.Label(
            label=body_text,
            wrap=True,
            wrap_mode=2,
            max_width_chars=40,
            xalign=0 if msg["incoming"] else 1,
            selectable=True,
        )
        bubble_box.append(label)

    # Outgoing delivery indicator (XEP-0184 receipts). Classic SMS-app
    # convention: ✓ = sent (server-acked), ✓✓ = delivered (peer ack).
    # No indicator for inbound or for old rows that predate receipts.
    if not msg["incoming"]:
        ds = msg.get("delivery_state")
        glyph = {"sent": "✓", "delivered": "✓✓", "failed": "⚠"}.get(ds or "")
        if glyph:
            status = Gtk.Label(label=glyph, xalign=1, halign=Gtk.Align.END)
            status.add_css_class("caption")
            status.add_css_class("dim-label")
            bubble_box.append(status)

    # XEP-0444 reactions strip — aggregate { emoji: count } across all
    # senders and render as small pill-buttons below the bubble.
    reactions_strip = _build_reactions_strip(msg)
    if reactions_strip is not None:
        bubble_box.append(reactions_strip)

    # Right-click / long-press → reaction popover. Only attach when we
    # have a send callback AND a stanza id to target.
    target_id = msg.get("xmpp_id") or ""
    if send_reaction is not None and target_id:
        _attach_reaction_menu(bubble_box, target_id, send_reaction)

    row = Gtk.ListBoxRow(selectable=False, activatable=False)
    if sender_label is not None:
        # Wrap label + bubble in a vertical container so the sender
        # caption sits directly above its message bubble.
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        wrap.append(sender_label)
        wrap.append(bubble_box)
        row.set_child(wrap)
    else:
        row.set_child(bubble_box)
    row.set_margin_top(2)
    row.set_margin_bottom(2)
    return row


def _build_reactions_strip(msg: dict) -> Gtk.Widget | None:
    """Aggregate reactions_json (per-sender lists) into a single
    counted-emoji row. Returns None when there are no reactions."""
    import json
    raw = msg.get("reactions_json")
    if not raw:
        return None
    try:
        by_sender = json.loads(raw)
    except (ValueError, TypeError):
        return None
    counts: dict[str, int] = {}
    for emojis in by_sender.values():
        for e in emojis or []:
            counts[e] = counts.get(e, 0) + 1
    if not counts:
        return None
    strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
    strip.set_halign(Gtk.Align.START if msg["incoming"] else Gtk.Align.END)
    for emoji, n in counts.items():
        label = emoji if n == 1 else f"{emoji} {n}"
        pill = Gtk.Label(label=label)
        pill.add_css_class("caption")
        pill.add_css_class("dim-label")
        strip.append(pill)
    return strip


def _attach_reaction_menu(widget: Gtk.Widget, target_id: str,
                          send_reaction) -> None:
    """Wire a long-press + right-click handler onto ``widget`` that
    pops up the quick-react emoji picker. ``send_reaction`` is invoked
    as ``send_reaction(target_id, [emoji])`` — the wire format wants
    the FULL set, so toggling state across reacts would need a model;
    we ship the simple "tap an emoji to add it" semantic here."""
    popover = Gtk.Popover.new()
    popover.set_parent(widget)
    popover.set_has_arrow(True)
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
    row.set_margin_top(4); row.set_margin_bottom(4)
    row.set_margin_start(4); row.set_margin_end(4)
    for emoji in _QUICK_REACTIONS:
        btn = Gtk.Button(label=emoji)
        btn.add_css_class("flat")
        def _on_clicked(_b, e=emoji):
            send_reaction(target_id, [e])
            popover.popdown()
        btn.connect("clicked", _on_clicked)
        row.append(btn)
    popover.set_child(row)

    # Long-press: phones / touchpads.
    long_press = Gtk.GestureLongPress.new()
    def _on_long_press(_g, _x, _y):
        popover.popup()
    long_press.connect("pressed", _on_long_press)
    widget.add_controller(long_press)

    # Right-click: desktop / mouse users.
    right_click = Gtk.GestureClick.new()
    right_click.set_button(3)
    def _on_right_click(_g, _n, _x, _y):
        popover.popup()
    right_click.connect("pressed", _on_right_click)
    widget.add_controller(right_click)


# Soup3 async image fetcher. The fetched bytes are decoded into a
# GdkTexture and set on the Picture. We use a one-shot per-URL session
# because conversation rendering is the only consumer.

def _load_image_async(picture: Gtk.Picture, url: str) -> None:
    from gi.repository import GLib, Gio, Soup, Gdk
    session = Soup.Session()
    message = Soup.Message.new("GET", url)

    def on_done(_sess, result):
        try:
            data = session.send_and_read_finish(result).get_data()
        except GLib.Error as exc:
            log.warning("image fetch failed %s: %s", url, exc.message)
            return
        if not data:
            return
        try:
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
        except GLib.Error as exc:
            log.debug("image decode failed %s: %s", url, exc.message)
            return
        picture.set_paintable(texture)

    session.send_and_read_async(message, GLib.PRIORITY_DEFAULT, None, on_done)


def _format_ts(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp).strftime("%H:%M")
