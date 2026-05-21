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

#
# Dependencies. libcalls headers live in the gnome-calls install
# under /usr/include/calls-*. They are only present on phone-deploy
# targets (postmarketOS, Phosh dev), not on a generic Fedora dev host,
# so this build is deliberately gated.
#
gnome      = import('gnome')
i18n       = import('i18n')

glib_dep   = dependency('glib-2.0',  version: '>= 2.66')
gobject    = dependency('gobject-2.0')
gtk        = dependency('gtk4',      version: '>= 4.10')
libpeas    = dependency('libpeas-2')
libcalls   = dependency('libcalls',  required: true,
                        not_found_message:
                          'libcalls headers not present — install ' +
                          'gnome-calls and its -dev/-devel package, ' +
                          'or build this plugin on a Phosh target.')

#
# Substitution data for the .plugin descriptor.
#
config_data = configuration_data()
config_data.set('VERSION', meson.project_version())

plugin_dir = join_paths(get_option('libdir'), 'calls', 'plugins', 'calls-xmpp')

plugin_desc = configure_file(
  input:  'calls-xmpp.plugin.in.in',
  output: 'calls-xmpp.plugin.in',
  configuration: config_data,
)

i18n.merge_file(
  input:  plugin_desc,
  output: 'calls-xmpp.plugin',
  type:   'desktop',
  install: true,
  install_dir: plugin_dir,
  po_dir: meson.current_source_dir() / 'po',
)

#
# Provider .so
#
subdir('src')
