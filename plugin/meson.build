project(
  'calls-xmpp',
  'c',
  version: '0.1.0',
  license: 'GPL-3.0-or-later',
  meson_version: '>= 1.0.0',
  default_options: [
    'warning_level=2',
    'c_std=gnu11',
  ],
)

gnome      = import('gnome')

glib_dep   = dependency('glib-2.0',  version: '>= 2.66')
gobject    = dependency('gobject-2.0')
gio        = dependency('gio-2.0')
gtk        = dependency('gtk4',      version: '>= 4.10')
libpeas    = dependency('libpeas-2')

# gnome-calls doesn't ship a pkg-config file or -dev package.
# Point at the source tree to get the plugin headers.
calls_inc = include_directories(
  get_option('calls_source_dir') / 'src',
)

config_data = configuration_data()
config_data.set('VERSION', meson.project_version())

plugin_dir = join_paths(get_option('libdir'), 'calls', 'plugins',
                        'provider', 'calls-xmpp')

plugin_desc = configure_file(
  input:  'calls-xmpp.plugin.in.in',
  output: 'calls-xmpp.plugin',
  configuration: config_data,
)

install_data(plugin_desc, install_dir: plugin_dir)

subdir('src')
