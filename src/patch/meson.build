configure_file(
  input:        'patch.in',
  output:       'patch',
  configuration: conf,
  install:      true,
  install_dir:  bindir,
  install_mode: 'r-xr-xr-x',
)

configure_file(
  input:         'const.py.in',
  output:        'const.py',
  configuration: py_conf,
  install:       true,
  install_dir:   moduledir,
)

python.install_sources(
  files(
    '__init__.py',
    '__main__.py',
    'account.py',
    'audio.py',
    'calls.py',
    'contacts.py',
    'jingle_session.py',
    'logging_setup.py',
    'main.py',
    'message_persister.py',
    'notifications.py',
    'numfmt.py',
    'ringer.py',
    'window.py',
  ),
  subdir: meson.project_name(),
)

python.install_sources(
  files(
    'pages/__init__.py',
    'pages/dialer.py',
    'pages/messages.py',
    'pages/voicemail.py',
  ),
  subdir: meson.project_name() / 'pages',
)

python.install_sources(
  files(
    'dialogs/__init__.py',
    'dialogs/account_dialog.py',
    'dialogs/call_dialog.py',
    'dialogs/new_message_dialog.py',
    'dialogs/preferences_dialog.py',
  ),
  subdir: meson.project_name() / 'dialogs',
)

python.install_sources(
  files(
    'store/__init__.py',
    'store/db.py',
    'store/secrets.py',
  ),
  subdir: meson.project_name() / 'store',
)

python.install_sources(
  files(
    'xmpp/__init__.py',
    'xmpp/client.py',
    'xmpp/jingle.py',
    'xmpp/turn.py',
  ),
  subdir: meson.project_name() / 'xmpp',
)

python.install_sources(
  files(
    'push/__init__.py',
    'push/connector.py',
    'push/controller.py',
    'push/decrypt.py',
    'push/distributor.py',
    'push/enable_iq.py',
    'push/keys.py',
  ),
  subdir: meson.project_name() / 'push',
)
