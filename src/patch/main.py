import sys

from gi.repository import Adw, Gio, GLib

from patch import APP_ID
from patch.logging_setup import configure_logging
from patch.window import PatchWindow


class PatchApplication(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        # Registered so --help lists it and GApplication accepts the
        # flag; configure_logging() reads sys.argv directly.
        self.add_main_option(
            "debug", ord("d"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
            "Enable debug logging", None,
        )

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._show_about)
        self.add_action(about_action)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("win.show-help-overlay", ["<Control>question"])

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            win = PatchWindow(application=self)
        win.present()

    def _show_about(self, *_):
        from patch import APP_NAME, VERSION
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=VERSION,
            license_type=__import__("gi").repository.Gtk.License.GPL_3_0,
        )
        about.present(self.props.active_window)


def main(argv=None):
    configure_logging()
    return PatchApplication().run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    sys.exit(main())
