from gi.repository import Adw, Gio, Gtk

from patch import APP_ID


@Gtk.Template(resource_path="/land/rob/patch/ui/window.ui")
class PatchWindow(Adw.ApplicationWindow):
    __gtype_name__ = "PatchWindow"

    window_title: Adw.WindowTitle = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._settings = Gio.Settings.new(APP_ID)

        # Restore persisted geometry.
        self.set_default_size(
            self._settings.get_int("window-width"),
            self._settings.get_int("window-height"),
        )
        if self._settings.get_boolean("window-maximized"):
            self.maximize()
        self.connect("close-request", self._on_close_request)

        action = Gio.SimpleAction.new("show-help-overlay", None)
        action.connect("activate", self._show_help_overlay)
        self.add_action(action)

    def _on_close_request(self, *_):
        # `get_default_size()` returns the configured default, not the
        # live size; use `get_width()`/`get_height()` so user resizes
        # actually persist.
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
