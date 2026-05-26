/*
 * SPDX-FileCopyrightText: 2026 Rob
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * gnome-calls provider that proxies to Patch's land.rob.patch.Calls1
 * D-Bus interface. Watches for the bus name to appear/vanish so the
 * plugin tolerates Patch starting after gnome-calls (or restarting
 * mid-session). IncomingCall + CallStateChanged signals are dispatched
 * into the CallsOrigin -> CallsCall hierarchy that gnome-calls manages.
 */

#define G_LOG_DOMAIN "CallsXmppProvider"

#include "calls-xmpp-provider.h"
#include "calls-xmpp-origin.h"

#include <libpeas.h>

#define PATCH_BUS_NAME  "land.rob.patch"
#define PATCH_OBJ_PATH  "/land/rob/patch/calls"
#define PATCH_IFACE     "land.rob.patch.Calls1"

static const char * const supported_protocols[] = { "tel", NULL };

struct _CallsXmppProvider {
    CallsProvider parent_instance;

    CallsXmppOrigin *origin;
    GListStore      *origins_store;
    GDBusProxy      *proxy;
    gulong           signal_id;
    guint            watch_id;
};

G_DEFINE_DYNAMIC_TYPE(CallsXmppProvider, calls_xmpp_provider,
                     CALLS_TYPE_PROVIDER);

/* -- D-Bus signal handler -------------------------------------------- */

static void
on_dbus_signal(GDBusProxy  *proxy      G_GNUC_UNUSED,
               const gchar *sender     G_GNUC_UNUSED,
               const gchar *signal_name,
               GVariant    *parameters,
               gpointer     user_data)
{
    CallsXmppProvider *self = CALLS_XMPP_PROVIDER(user_data);

    if (g_strcmp0(signal_name, "IncomingCall") == 0) {
        const char *session_id = NULL;
        const char *number     = NULL;
        const char *name       = NULL;
        g_variant_get(parameters, "(&s&s&s)", &session_id, &number, &name);
        if (self->origin != NULL)
            calls_xmpp_origin_handle_incoming(self->origin, session_id,
                                              number, name);
    } else if (g_strcmp0(signal_name, "CallStateChanged") == 0) {
        const char *session_id = NULL;
        const char *state      = NULL;
        g_variant_get(parameters, "(&s&s)", &session_id, &state);
        if (self->origin != NULL)
            calls_xmpp_origin_handle_state_changed(self->origin,
                                                    session_id, state);
    }
}

/* -- proxy lifecycle -------------------------------------------------- */

static void
drop_proxy(CallsXmppProvider *self)
{
    if (self->proxy != NULL && self->signal_id != 0) {
        g_signal_handler_disconnect(self->proxy, self->signal_id);
        self->signal_id = 0;
    }
    g_clear_object(&self->proxy);
}

static void
on_proxy_ready(GObject      *source G_GNUC_UNUSED,
               GAsyncResult *res,
               gpointer      user_data)
{
    CallsXmppProvider *self = CALLS_XMPP_PROVIDER(user_data);
    GError *error = NULL;

    self->proxy = g_dbus_proxy_new_for_bus_finish(res, &error);
    if (error != NULL) {
        g_warning("Could not create Calls1 proxy: %s", error->message);
        g_error_free(error);
        return;
    }
    self->signal_id = g_signal_connect(self->proxy, "g-signal",
                                       G_CALLBACK(on_dbus_signal), self);
    g_info("Calls1 proxy connected to %s", PATCH_BUS_NAME);
}

static void
on_name_appeared(GDBusConnection *connection G_GNUC_UNUSED,
                 const gchar     *name       G_GNUC_UNUSED,
                 const gchar     *name_owner G_GNUC_UNUSED,
                 gpointer         user_data)
{
    CallsXmppProvider *self = CALLS_XMPP_PROVIDER(user_data);

    if (self->proxy != NULL)
        return;

    g_dbus_proxy_new_for_bus(
        G_BUS_TYPE_SESSION,
        G_DBUS_PROXY_FLAGS_DO_NOT_LOAD_PROPERTIES,
        NULL,
        PATCH_BUS_NAME,
        PATCH_OBJ_PATH,
        PATCH_IFACE,
        NULL,
        on_proxy_ready,
        self);
}

static void
on_name_vanished(GDBusConnection *connection G_GNUC_UNUSED,
                 const gchar     *name       G_GNUC_UNUSED,
                 gpointer         user_data)
{
    CallsXmppProvider *self = CALLS_XMPP_PROVIDER(user_data);
    drop_proxy(self);
    if (self->origin != NULL)
        calls_xmpp_origin_handle_patch_lost(self->origin);
}

/* -- public accessor ------------------------------------------------- */

GDBusProxy *
calls_xmpp_provider_get_proxy(CallsXmppProvider *self)
{
    return self->proxy;
}

/* -- CallsProvider vfuncs -------------------------------------------- */

static const char *
xmpp_provider_get_name(CallsProvider *provider G_GNUC_UNUSED)
{
    return "XMPP / JMP.chat";
}

static const char *
xmpp_provider_get_status(CallsProvider *provider)
{
    CallsXmppProvider *self = CALLS_XMPP_PROVIDER(provider);
    return self->proxy != NULL ? "normal" : "offline";
}

static GListModel *
xmpp_provider_get_origins(CallsProvider *provider)
{
    CallsXmppProvider *self = CALLS_XMPP_PROVIDER(provider);
    return G_LIST_MODEL(self->origins_store);
}

static const char *const *
xmpp_provider_get_protocols(CallsProvider *provider G_GNUC_UNUSED)
{
    return supported_protocols;
}

static gboolean
xmpp_provider_is_modem(CallsProvider *provider G_GNUC_UNUSED)
{
    return FALSE;
}

static gboolean
xmpp_provider_is_operational(CallsProvider *provider)
{
    CallsXmppProvider *self = CALLS_XMPP_PROVIDER(provider);
    return self->proxy != NULL;
}

/* -- GObject lifecycle ----------------------------------------------- */

static void
calls_xmpp_provider_constructed(GObject *object)
{
    CallsXmppProvider *self = CALLS_XMPP_PROVIDER(object);

    G_OBJECT_CLASS(calls_xmpp_provider_parent_class)->constructed(object);

    self->origin = calls_xmpp_origin_new(self);
    self->origins_store = g_list_store_new(CALLS_TYPE_ORIGIN);
    g_list_store_append(self->origins_store, G_OBJECT(self->origin));

    self->watch_id = g_bus_watch_name(
        G_BUS_TYPE_SESSION,
        PATCH_BUS_NAME,
        G_BUS_NAME_WATCHER_FLAGS_NONE,
        on_name_appeared,
        on_name_vanished,
        self, NULL);
}

static void
calls_xmpp_provider_finalize(GObject *object)
{
    CallsXmppProvider *self = CALLS_XMPP_PROVIDER(object);
    if (self->watch_id)
        g_bus_unwatch_name(self->watch_id);
    drop_proxy(self);
    g_clear_object(&self->origin);
    g_clear_object(&self->origins_store);
    G_OBJECT_CLASS(calls_xmpp_provider_parent_class)->finalize(object);
}

static void
calls_xmpp_provider_class_init(CallsXmppProviderClass *klass)
{
    GObjectClass       *object_class   = G_OBJECT_CLASS(klass);
    CallsProviderClass *provider_class = CALLS_PROVIDER_CLASS(klass);

    object_class->constructed      = calls_xmpp_provider_constructed;
    object_class->finalize         = calls_xmpp_provider_finalize;
    provider_class->get_name        = xmpp_provider_get_name;
    provider_class->get_status      = xmpp_provider_get_status;
    provider_class->get_origins     = xmpp_provider_get_origins;
    provider_class->get_protocols   = xmpp_provider_get_protocols;
    provider_class->is_modem        = xmpp_provider_is_modem;
    provider_class->is_operational  = xmpp_provider_is_operational;
}

static void
calls_xmpp_provider_class_finalize(CallsXmppProviderClass *klass G_GNUC_UNUSED)
{
}

static void
calls_xmpp_provider_init(CallsXmppProvider *self G_GNUC_UNUSED)
{
}

/* -- libpeas entry point --------------------------------------------- */

G_MODULE_EXPORT void
peas_register_types(PeasObjectModule *module)
{
    calls_xmpp_provider_register_type(G_TYPE_MODULE(module));
    peas_object_module_register_extension_type(
        module, CALLS_TYPE_PROVIDER, CALLS_TYPE_XMPP_PROVIDER);
}
