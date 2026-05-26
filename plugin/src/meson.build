plugin_sources = files(
  'calls-xmpp-provider.c',
  'calls-xmpp-origin.c',
  'calls-xmpp-call.c',
)

shared_module(
  'calls-xmpp',
  plugin_sources,
  include_directories: calls_inc,
  dependencies: [glib_dep, gobject, gio, gtk, libpeas],
  install:      true,
  install_dir:  plugin_dir,
)
