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
    'logging_setup.py',
    'main.py',
    'window.py',
  ),
  subdir: meson.project_name(),
)
