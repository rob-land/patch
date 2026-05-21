desktop_file = i18n.merge_file(
  input:  'land.rob.patch.desktop.in',
  output: 'land.rob.patch.desktop',
  type:   'desktop',
  po_dir: '../po',
  install: true,
  install_dir: datadir / 'applications',
)

desktop_utils = find_program('desktop-file-validate', required: false)
if desktop_utils.found()
  test('Validate desktop file', desktop_utils, args: [desktop_file])
endif

appstream_file = i18n.merge_file(
  input:  'land.rob.patch.metainfo.xml.in',
  output: 'land.rob.patch.metainfo.xml',
  po_dir: '../po',
  install: true,
  install_dir: datadir / 'metainfo',
)

appstream_util = find_program('appstreamcli', required: false)
if appstream_util.found()
  test(
    'Validate metainfo',
    appstream_util,
    args: ['validate', '--no-net', appstream_file],
  )
endif

install_data(
  'land.rob.patch.gschema.xml',
  install_dir: datadir / 'glib-2.0' / 'schemas',
)

# D-Bus activation service file: lets dbus-daemon spin Patch up when a
# UnifiedPush distributor wants to deliver a push and we aren't running.
configure_file(
  input:        'land.rob.patch.service.in',
  output:       'land.rob.patch.service',
  configuration: conf,
  install:      true,
  install_dir:  datadir / 'dbus-1' / 'services',
)

install_data(
  'icons/hicolor/scalable/apps/land.rob.patch.svg',
  install_dir: datadir / 'icons' / 'hicolor' / 'scalable' / 'apps',
)
install_data(
  'icons/hicolor/symbolic/apps/land.rob.patch-symbolic.svg',
  install_dir: datadir / 'icons' / 'hicolor' / 'symbolic' / 'apps',
)

compile_schemas = find_program('glib-compile-schemas', required: false)
if compile_schemas.found()
  test(
    'Validate schema',
    compile_schemas,
    args: ['--strict', '--dry-run', meson.current_source_dir()],
  )
endif

# UI templates -> Blueprint -> .ui -> GResource bundle
subdir('ui')
