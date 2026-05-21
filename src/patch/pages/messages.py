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

    def __init__(self, account, store, xmpp):
        super().__init__()
        self._account = account
        self._store = store
        self._xmpp = xmpp
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

    def _on_message_received(self, _xmpp, remote_jid, body, incoming, timestamp,
                             attachment_url):
        # Group SMS bodies on JMP carry the sender in the body itself; split
        # that out so we can render it as a separate row label.
        sender_jid = None
        if numfmt.is_group_jid(remote_jid):
            sender_jid, body = numfmt.parse_group_body(body)
        self._store.add_message(
            remote_jid, bool(incoming), body, timestamp, sender_jid,
            attachment_url=attachment_url or None)
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


def _render_thread_row(msg: dict) -> Gtk.Widget:
    align = Gtk.Align.START if msg["incoming"] else Gtk.Align.END

    bubble_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    bubble_box.set_halign(align)
    bubble_box.set_margin_start(12)
    bubble_box.set_margin_end(12)
    bubble_box.set_margin_top(4)
    bubble_box.set_margin_bottom(4)
    bubble_box.add_css_class("card")

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

    row = Gtk.ListBoxRow(selectable=False, activatable=False)
    row.set_child(bubble_box)
    row.set_margin_top(2)
    row.set_margin_bottom(2)
    return row


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
