"""XEP-0050 Ad-hoc commands dialog (JMP account management).

Discovers available commands from the gateway/server, lets the user
pick one, renders the data-form response, and submits input fields
when the command is multi-step. Covers JMP-specific operations like
checking balance, configuring voicemail PIN, managing numbers.
"""

from __future__ import annotations

import logging

from gi.repository import Adw, GLib, Gtk

log = logging.getLogger(__name__)


class PatchAdHocDialog(Adw.Dialog):
    __gtype_name__ = "PatchAdHocDialog"

    def __init__(self, xmpp, gateway_domain: str):
        super().__init__()
        self._xmpp = xmpp
        self._gateway = gateway_domain
        self._current_cmd = None
        self.set_title("JMP Account")
        self.set_content_width(420)
        self.set_content_height(500)

        # Stack: "loading", "list", "form"
        self._stack = Gtk.Stack()
        # -- loading spinner
        spinner = Gtk.Spinner(spinning=True)
        spinner.set_halign(Gtk.Align.CENTER)
        spinner.set_valign(Gtk.Align.CENTER)
        self._stack.add_named(spinner, "loading")
        # -- command list
        self._list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.connect("row-activated", self._on_command_clicked)
        list_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        list_scroll.set_child(self._list_box)
        list_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        list_page.set_margin_top(16)
        list_page.set_margin_bottom(16)
        list_page.set_margin_start(16)
        list_page.set_margin_end(16)
        list_page.append(list_scroll)
        self._stack.add_named(list_page, "list")
        # -- form view (populated dynamically)
        self._form_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._form_box.set_margin_top(16)
        self._form_box.set_margin_bottom(16)
        self._form_box.set_margin_start(16)
        self._form_box.set_margin_end(16)
        form_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        form_scroll.set_child(self._form_box)
        self._stack.add_named(form_scroll, "form")

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(True)
        header.set_show_end_title_buttons(True)
        toolbar.add_top_bar(header)
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        self._stack.set_visible_child_name("loading")
        GLib.idle_add(self._discover_commands)

    # -- discovery -----------------------------------------------------------

    def _discover_commands(self) -> bool:
        client = self._xmpp._client  # noqa: SLF001
        if client is None:
            self._show_error("Not connected")
            return False
        try:
            module = client.get_module("AdHoc")
            task = module.request_command_list(jid=self._gateway)
            task.add_done_callback(self._on_commands_discovered)
        except Exception as exc:  # noqa: BLE001
            self._show_error(f"Discovery failed: {exc}")
        return False

    def _on_commands_discovered(self, task):
        try:
            commands = task.finish()
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self._show_error, f"Could not list commands: {exc}")
            return
        GLib.idle_add(self._populate_list, commands)

    def _populate_list(self, commands) -> bool:
        for cmd in commands:
            row = Adw.ActionRow(
                title=cmd.name or cmd.node,
                activatable=True,
            )
            row._adhoc_cmd = cmd  # stash for activation
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            self._list_box.append(row)
        self._stack.set_visible_child_name("list")
        return False

    # -- execute command ----------------------------------------------------

    def _on_command_clicked(self, _listbox, row):
        cmd = getattr(row, "_adhoc_cmd", None)
        if cmd is None:
            return
        self._execute(cmd)

    def _execute(self, cmd):
        self._stack.set_visible_child_name("loading")
        client = self._xmpp._client  # noqa: SLF001
        if client is None:
            self._show_error("Not connected")
            return
        try:
            module = client.get_module("AdHoc")
            task = module.execute_command(cmd)
            task.add_done_callback(self._on_command_result)
        except Exception as exc:  # noqa: BLE001
            self._show_error(f"Execute failed: {exc}")

    def _on_command_result(self, task):
        try:
            result = task.finish()
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self._show_error, f"Command failed: {exc}")
            return
        GLib.idle_add(self._render_form, result)

    # -- form rendering -----------------------------------------------------

    def _render_form(self, cmd) -> bool:
        self._current_cmd = cmd
        # Clear prior form contents.
        while True:
            child = self._form_box.get_first_child()
            if child is None:
                break
            self._form_box.remove(child)

        # Notes (informational text from server).
        for note in cmd.notes or []:
            label = Gtk.Label(label=note.text or "", wrap=True, xalign=0)
            label.add_css_class("body")
            self._form_box.append(label)

        # Data form fields.
        self._field_widgets: list[tuple] = []
        data_node = cmd.data
        if data_node is not None:
            title = data_node.getAttr("title") or ""
            if title:
                title_label = Gtk.Label(label=title, wrap=True, xalign=0)
                title_label.add_css_class("title-3")
                self._form_box.append(title_label)
            instructions = data_node.getTagData("instructions")
            if instructions:
                instr = Gtk.Label(label=instructions, wrap=True, xalign=0)
                instr.add_css_class("dim-label")
                self._form_box.append(instr)
            for field in data_node.getTags("field"):
                widget = self._build_field_widget(field)
                if widget is not None:
                    self._form_box.append(widget)

        # Action buttons.
        from nbxmpp.modules.adhoc import AdHocAction, AdHocStatus
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.CENTER)
        btn_box.set_margin_top(12)
        if cmd.status == AdHocStatus.EXECUTING:
            if AdHocAction.COMPLETE in (cmd.actions or set()):
                submit = Gtk.Button(label="Submit")
                submit.add_css_class("suggested-action")
                submit.add_css_class("pill")
                submit.connect("clicked", self._on_submit)
                btn_box.append(submit)
            elif cmd.actions:
                # "next" or "execute" — both mean proceed.
                proceed = Gtk.Button(label="Next")
                proceed.add_css_class("suggested-action")
                proceed.add_css_class("pill")
                proceed.connect("clicked", self._on_next)
                btn_box.append(proceed)
            else:
                # No explicit actions — try "complete"
                submit = Gtk.Button(label="Submit")
                submit.add_css_class("suggested-action")
                submit.add_css_class("pill")
                submit.connect("clicked", self._on_submit)
                btn_box.append(submit)
            cancel = Gtk.Button(label="Cancel")
            cancel.add_css_class("pill")
            cancel.connect("clicked", self._on_cancel)
            btn_box.append(cancel)
        else:
            # completed or canceled — just a Close button
            close = Gtk.Button(label="Close")
            close.add_css_class("pill")
            close.connect("clicked", lambda *_: self.force_close())
            btn_box.append(close)
        self._form_box.append(btn_box)
        self._stack.set_visible_child_name("form")
        return False

    def _build_field_widget(self, field) -> Gtk.Widget | None:
        field_type = field.getAttr("type") or "text-single"
        var = field.getAttr("var") or ""
        label_text = field.getAttr("label") or var
        value = field.getTagData("value") or ""

        if field_type == "hidden":
            # Invisible, carry through on submit.
            self._field_widgets.append((var, field_type, None, value))
            return None
        if field_type == "fixed":
            lbl = Gtk.Label(label=value, wrap=True, xalign=0)
            lbl.add_css_class("body")
            return lbl
        if field_type in ("text-single", "jid-single"):
            row = Adw.EntryRow(title=label_text)
            row.set_text(value)
            self._field_widgets.append((var, field_type, row, None))
            return row
        if field_type == "text-private":
            row = Adw.PasswordEntryRow(title=label_text)
            row.set_text(value)
            self._field_widgets.append((var, field_type, row, None))
            return row
        if field_type == "text-multi":
            frame = Gtk.Frame(label=label_text)
            tv = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
            tv.get_buffer().set_text(value)
            tv.set_vexpand(True)
            frame.set_child(tv)
            self._field_widgets.append((var, field_type, tv, None))
            return frame
        if field_type == "boolean":
            row = Adw.SwitchRow(title=label_text)
            row.set_active(value in ("1", "true"))
            self._field_widgets.append((var, field_type, row, None))
            return row
        if field_type == "list-single":
            options = []
            for opt in field.getTags("option"):
                opt_val = opt.getTagData("value") or ""
                opt_label = opt.getAttr("label") or opt_val
                options.append((opt_val, opt_label))
            model = Gtk.StringList()
            for _, ol in options:
                model.append(ol)
            row = Adw.ComboRow(title=label_text, model=model)
            # Pre-select the current value.
            for i, (ov, _) in enumerate(options):
                if ov == value:
                    row.set_selected(i)
                    break
            self._field_widgets.append((var, field_type, row, options))
            return row
        # Fallback: render as text entry.
        row = Adw.EntryRow(title=f"{label_text} ({field_type})")
        row.set_text(value)
        self._field_widgets.append((var, field_type, row, None))
        return row

    # -- submit / next / cancel ---------------------------------------------

    def _collect_form(self):
        """Build a SimpleDataForm from the current field widgets."""
        from nbxmpp.protocol import Node
        x = Node("x", attrs={"xmlns": "jabber:x:data", "type": "submit"})
        for var, ftype, widget, extra in self._field_widgets:
            field = x.addChild("field", attrs={"var": var})
            if ftype == "hidden":
                field.setAttr("type", "hidden")
                field.addChild("value").addData(extra or "")
            elif ftype in ("text-single", "jid-single", "text-private"):
                field.addChild("value").addData(widget.get_text())
            elif ftype == "text-multi":
                buf = widget.get_buffer()
                text = buf.get_text(buf.get_start_iter(),
                                    buf.get_end_iter(), False)
                field.addChild("value").addData(text)
            elif ftype == "boolean":
                field.addChild("value").addData(
                    "1" if widget.get_active() else "0")
            elif ftype == "list-single":
                idx = widget.get_selected()
                options = extra or []
                if 0 <= idx < len(options):
                    field.addChild("value").addData(options[idx][0])
            else:
                field.addChild("value").addData(widget.get_text())
        return x

    def _on_submit(self, *_):
        self._submit_with_action("complete")

    def _on_next(self, *_):
        self._submit_with_action("execute")

    def _submit_with_action(self, action_str: str):
        if self._current_cmd is None:
            return
        from nbxmpp.modules.adhoc import AdHocAction
        action = AdHocAction(action_str)
        form_node = self._collect_form()
        self._stack.set_visible_child_name("loading")
        client = self._xmpp._client  # noqa: SLF001
        if client is None:
            self._show_error("Not connected")
            return
        try:
            module = client.get_module("AdHoc")
            task = module.execute_command(
                self._current_cmd, action=action, dataform=form_node)
            task.add_done_callback(self._on_command_result)
        except Exception as exc:  # noqa: BLE001
            self._show_error(f"Submit failed: {exc}")

    def _on_cancel(self, *_):
        if self._current_cmd is None:
            self.force_close()
            return
        from nbxmpp.modules.adhoc import AdHocAction
        self._stack.set_visible_child_name("loading")
        client = self._xmpp._client  # noqa: SLF001
        if client is None:
            self.force_close()
            return
        try:
            module = client.get_module("AdHoc")
            module.execute_command(self._current_cmd, action=AdHocAction.CANCEL)
        except Exception:  # noqa: BLE001
            pass
        self.force_close()

    # -- error display ------------------------------------------------------

    def _show_error(self, msg: str) -> bool:
        while True:
            child = self._form_box.get_first_child()
            if child is None:
                break
            self._form_box.remove(child)
        label = Gtk.Label(label=msg, wrap=True, xalign=0)
        label.add_css_class("body")
        self._form_box.append(label)
        close = Gtk.Button(label="Close")
        close.add_css_class("pill")
        close.set_halign(Gtk.Align.CENTER)
        close.set_margin_top(12)
        close.connect("clicked", lambda *_: self.force_close())
        self._form_box.append(close)
        self._stack.set_visible_child_name("form")
        return False
