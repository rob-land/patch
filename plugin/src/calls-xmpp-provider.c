/*
 * SPDX-FileCopyrightText: 2026 Rob
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * gnome-calls provider that proxies to Patch's land.rob.patch.Calls1
 * D-Bus interface. Opens a GDBusProxy on construction, subscribes to
 * IncomingCall + CallStateChanged signals, and dispatches them into
 * the CallsOrigin → CallsCall hierarchy that gnome-calls manages.
 */

#define G_LOG_DOMAIN "CallsXmppProvider"

#include "calls-xmpp-provider.h"
#include "calls-xmpp-origin.h"
#include "calls-xmpp-call.h"

#include <libpeas.h>

#define PATCH_BUS_NAME  "land.rob.patch"
#define PATCH_OBJ_PATH  "/land/rob/patch/calls"
#define PATCH_IFACE     "land.rob.patch.Calls1"

struct _CallsXmppProvider {
    CallsProvider parent_instance;

    CallsXmppOrigin *origin;
    GDBusProxy      *proxy;
    gulong           signal_id;
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
ensure_proxy(CallsXmppProvider *self)
{
    if (self->proxy != NULL)
        return;

    GError *error = NULL;
    self->proxy = g_dbus_proxy_new_for_bus_sync(
        G_BUS_TYPE_SESSION,
        G_DBUS_PROXY_FLAGS_DO_NOT_LOAD_PROPERTIES,
        NULL, /* GDBusInterfaceInfo — let GLib discover from introspection */
        PATCH_BUS_NAME,
        PATCH_OBJ_PATH,
        PATCH_IFACE,
        NULL, /* cancellable */
        &error);
    if (error != NULL) {
        g_warning("Could not create Calls1 proxy: %s", error->message);
        g_error_free(error);
        return;
    }
    self->signal_id = g_signal_connect(self->proxy, "g-signal",
                                       G_CALLBACK(on_dbus_signal), self);
    g_info("Calls1 proxy connected to %s", PATCH_BUS_NAME);
}

/* -- public accessor ------------------------------------------------- */

GDBusProxy *
calls_xmpp_provider_get_proxy(CallsXmppProvider *self)
{
    ensure_proxy(self);
    return self->proxy;
}

/* -- CallsProvider vfuncs -------------------------------------------- */

static const char *
calls_xmpp_provider_get_name(CallsProvider *provider G_GNUC_UNUSED)
{
    return "XMPP / JMP.chat";
}

static const char *
calls_xmpp_provider_get_status(CallsProvider *provider)
{
    CallsXmppProvider *self = CALLS_XMPP_PROVIDER(provider);
    ensure_proxy(self);
    return self->proxy != NULL ? "normal" : "offline";
}

static GListModel *
calls_xmpp_provider_get_origins(CallsProvider *provider)
{
    CallsXmppProvider *self = CALLS_XMPP_PROVIDER(provider);
    ensure_proxy(self);
    if (self->origin == NULL)
        self->origin = calls_xmpp_origin_new(self);
    GListStore *store = g_list_store_new(G_TYPE_OBJECT);
    g_list_store_append(store, G_OBJECT(self->origin));
    return G_LIST_MODEL(store);
}

/* -- GObject lifecycle ----------------------------------------------- */

static void
calls_xmpp_provider_finalize(GObject *object)
{
    CallsXmppProvider *self = CALLS_XMPP_PROVIDER(object);
    if (self->proxy && self->signal_id)
        g_signal_handler_disconnect(self->proxy, self->signal_id);
    g_clear_object(&self->origin);
    g_clear_object(&self->proxy);
    G_OBJECT_CLASS(calls_xmpp_provider_parent_class)->finalize(object);
}

static void
calls_xmpp_provider_class_init(CallsXmppProviderClass *klass)
{
    GObjectClass     *object_class   = G_OBJECT_CLASS(klass);
    CallsProviderClass *provider_class = CALLS_PROVIDER_CLASS(klass);

    object_class->finalize     = calls_xmpp_provider_finalize;
    provider_class->get_name    = calls_xmpp_provider_get_name;
    provider_class->get_status  = calls_xmpp_provider_get_status;
    provider_class->get_origins = calls_xmpp_provider_get_origins;
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
