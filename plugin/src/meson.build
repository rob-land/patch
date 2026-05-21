plugin_sources = files(
  'calls-xmpp-provider.c',
  'calls-xmpp-origin.c',
  'calls-xmpp-call.c',
)

plugin_headers = files(
  'calls-xmpp-provider.h',
  'calls-xmpp-origin.h',
  'calls-xmpp-call.h',
)

shared_module(
  'calls-xmpp',
  plugin_sources,
  dependencies: [glib_dep, gobject, gtk, libpeas, libcalls],
  install:      true,
  install_dir:  plugin_dir,
)
