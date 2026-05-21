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
    'logging_setup.py',
    'main.py',
    'numfmt.py',
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
  ),
  subdir: meson.project_name() / 'xmpp',
)
