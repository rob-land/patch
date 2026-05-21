"""Preferences dialog — top-level settings surface.

Distinct from the Account dialog (which is the narrow JID + password
form). Preferences exposes connection diagnostics (the active push
distributor and endpoint, the log path) and a placeholder calls page
that documents the audio-not-wired-up limitation.
"""

from __future__ import annotations

import logging
import os

from gi.repository import Adw, Gio, Gtk

from patch import APP_ID
from patch.logging_setup import log_path

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/patch/ui/preferences-dialog.ui")
class PatchPreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = "PatchPreferencesDialog"

    distributor_row: Adw.ActionRow = Gtk.Template.Child()
    endpoint_row:    Adw.ActionRow = Gtk.Template.Child()
    log_path_row:    Adw.ActionRow = Gtk.Template.Child()

    def __init__(self):
        super().__init__()
        settings = Gio.Settings.new(APP_ID)
        dist = settings.get_string("push-distributor") or "(none configured)"
        ep   = settings.get_string("push-endpoint")    or "(not registered yet)"
        self.distributor_row.set_subtitle(dist)
        self.endpoint_row.set_subtitle(ep)
        self.log_path_row.set_subtitle(log_path())
