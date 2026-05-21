from __future__ import annotations

import logging

from gi.repository import Adw, Gio, GLib, Gtk

from patch import APP_ID
from patch import account as account_mod
from patch.pages.dialer    import PatchDialerPage
from patch.pages.messages  import PatchMessagesPage
from patch.pages.voicemail import PatchVoicemailPage

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/patch/ui/window.ui")
class PatchWindow(Adw.ApplicationWindow):
    __gtype_name__ = "PatchWindow"

    view_stack:    Adw.ViewStack    = Gtk.Template.Child()
    view_switcher: Adw.ViewSwitcher = Gtk.Template.Child()
    view_switcher_bar: Adw.ViewSwitcherBar = Gtk.Template.Child()
    title_stack:   Gtk.Stack        = Gtk.Template.Child()
    window_title:  Adw.WindowTitle  = Gtk.Template.Child()
    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    status_banner: Adw.Banner       = Gtk.Template.Child()

    def __init__(self, application, account, store, xmpp, calls, contacts,
                 **kwargs):
        super().__init__(application=application, **kwargs)
        self._settings = Gio.Settings.new(APP_ID)
        self._account = account
        self._store = store
        self._xmpp = xmpp
        self._calls = calls
        self._contacts = contacts

        # Persisted window geometry. get_default_size() returns the
        # configured default, not the live size — get_width/height per
        # STYLE_GUIDE so user resizes actually save.
        self.set_default_size(
            self._settings.get_int("window-width"),
            self._settings.get_int("window-height"),
        )
        if self._settings.get_boolean("window-maximized"):
            self.maximize()
        self.connect("close-request", self._on_close_request)

        # Help-overlay action — needed for the menu entry and Ctrl-?.
        help_action = Gio.SimpleAction.new("show-help-overlay", None)
        help_action.connect("activate", self._show_help_overlay)
        self.add_action(help_action)

        # Window-scoped action that any child page can fire to surface a
        # toast. Used by the dialer for "invalid number" etc.
        toast_action = Gio.SimpleAction.new("toast", GLib.VariantType.new("s"))
        toast_action.connect("activate", self._on_toast)
        self.add_action(toast_action)

        # Notification "tap" reroutes here so we can switch tabs + open
        # the right thread on the page. The app fires this action from
        # NotificationManager._on_open_conversation.
        open_conv_action = Gio.SimpleAction.new(
            "open-conversation", GLib.VariantType.new("s"))
        open_conv_action.connect("activate", self._on_open_conversation)
        self.add_action(open_conv_action)

        # Dialer fires win.start-call(jid) when the user taps Call.
        start_call_action = Gio.SimpleAction.new(
            "start-call", GLib.VariantType.new("s"))
        start_call_action.connect("activate", self._on_start_call)
        self.add_action(start_call_action)

        # -- pages --------------------------------------------------------
        # Messages page needs the store + xmpp client; the others are
        # account-only for now. Keep a direct reference to the messages
        # page so notification-tap navigation can call into it.
        self._dialer_page    = PatchDialerPage(self._account,
                                                store=self._store,
                                                calls=self._calls)
        self._messages_page  = PatchMessagesPage(self._account, self._store,
                                                  self._xmpp, self._contacts)
        self._voicemail_page = PatchVoicemailPage(self._account)
        pages = [self._dialer_page, self._messages_page, self._voicemail_page]
        for page in pages:
            props = page.get_page_props()
            stack_page = self.view_stack.add_titled_with_icon(
                page, props["name"], props["title"], props["icon_name"],
            )
            stack_page.set_use_underline(True)

        # -- connection status banner ------------------------------------
        # Mirror the account state machine into a banner. CONNECTED hides
        # it; CONNECTING shows a neutral note; FAILED shows the error and
        # offers a Connect button that retries immediately (bypassing the
        # backoff timer).
        self.status_banner.connect("button-clicked", self._on_banner_button)
        self._account.connect("notify::state",      self._refresh_banner)
        self._account.connect("notify::last-error", self._refresh_banner)
        self._refresh_banner()

    # -- handlers ---------------------------------------------------------

    def _on_close_request(self, *_):
        if not self.is_maximized():
            self._settings.set_int("window-width",  self.get_width())
            self._settings.set_int("window-height", self.get_height())
        self._settings.set_boolean("window-maximized", self.is_maximized())
        return False

    def _show_help_overlay(self, *_):
        builder = Gtk.Builder.new_from_resource(
            "/land/rob/patch/ui/help-overlay.ui")
        overlay = builder.get_object("help_overlay")
        overlay.set_transient_for(self)
        overlay.present()

    def _on_toast(self, _action, param):
        msg = param.get_string()
        self.toast_overlay.add_toast(Adw.Toast.new(msg))

    def _refresh_banner(self, *_):
        state = self._account.state
        if state == account_mod.STATE_CONNECTED or not self._account.is_configured:
            self.status_banner.set_revealed(False)
            return
        if state == account_mod.STATE_CONNECTING:
            self.status_banner.set_title("Connecting…")
            self.status_banner.set_button_label("")
        elif state == account_mod.STATE_FAILED:
            err = self._account.last_error or "Connection failed"
            self.status_banner.set_title(err)
            self.status_banner.set_button_label("Connect")
        elif state == account_mod.STATE_DISCONNECTED:
            self.status_banner.set_title("Offline")
            self.status_banner.set_button_label("Connect")
        else:
            self.status_banner.set_title(state.capitalize())
            self.status_banner.set_button_label("")
        self.status_banner.set_revealed(True)

    def _on_banner_button(self, _banner):
        # Bypass the XMPP client's backoff and try again now.
        self.activate_action("app.connect")

    # -- NotificationManager support -------------------------------------

    def messages_focused_jid(self):
        """Return the JID currently visible in the thread view, else None.

        NotificationManager calls this through its focus_provider to
        decide whether to suppress notifications.
        """
        return self._messages_page.focused_jid()

    def _on_open_conversation(self, _action, param):
        jid = param.get_string()
        # Switch to the messages tab first so the navigation push lands
        # on the page the user can actually see.
        self.view_stack.set_visible_child_name("messages")
        self._messages_page.open_conversation(jid)

    def _on_start_call(self, _action, param):
        jid = param.get_string()
        if self._account.state != account_mod.STATE_CONNECTED:
            self._on_toast(None, GLib.Variant("s", "Not connected — can't dial"))
            return
        sess = self._calls.start_outgoing(jid)
        if sess is None:
            self._on_toast(None, GLib.Variant("s", "Already in a call"))
