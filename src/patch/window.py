from __future__ import annotations

import logging

from gi.repository import Adw, Gio, GLib, Gtk

from patch import APP_ID
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

    def __init__(self, application, account, store, xmpp, **kwargs):
        super().__init__(application=application, **kwargs)
        self._settings = Gio.Settings.new(APP_ID)
        self._account = account
        self._store = store
        self._xmpp = xmpp

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

        # -- pages --------------------------------------------------------
        # Messages page needs the store + xmpp client; the others are
        # account-only for now.
        pages = [
            PatchDialerPage(self._account),
            PatchMessagesPage(self._account, self._store, self._xmpp),
            PatchVoicemailPage(self._account),
        ]
        for page in pages:
            props = page.get_page_props()
            stack_page = self.view_stack.add_titled_with_icon(
                page, props["name"], props["title"], props["icon_name"],
            )
            stack_page.set_use_underline(True)

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
