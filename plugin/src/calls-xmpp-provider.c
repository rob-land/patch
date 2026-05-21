/*
 * SPDX-FileCopyrightText: 2026 Rob
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Scaffold only. The real implementation talks to Patch over D-Bus
 * (`land.rob.patch.Calls1` on bus name `land.rob.patch`, path
 * `/land/rob/patch/calls`) — see ../README.md for the planned surface.
 */

#define G_LOG_DOMAIN "CallsXmppProvider"

#include "calls-xmpp-provider.h"
#include "calls-xmpp-origin.h"

#include <libpeas.h>

struct _CallsXmppProvider {
    CallsProvider parent_instance;

    /* The single origin we expose; gnome-calls calls dial() on it. */
    CallsXmppOrigin *origin;

    /* Patch's D-Bus name. We open a proxy to it lazily on first use. */
    GDBusProxy *patch_proxy;
};

G_DEFINE_DYNAMIC_TYPE(CallsXmppProvider, calls_xmpp_provider,
                     CALLS_TYPE_PROVIDER);

/* CallsProvider vfuncs ----------------------------------------------- */

static const char *
calls_xmpp_provider_get_name(CallsProvider *provider)
{
    return "XMPP / JMP.chat";
}

static const char *
calls_xmpp_provider_get_status(CallsProvider *provider)
{
    /* TODO: surface online/offline based on the proxy's Patch.Calls1.State
     * signal. For now: always claim normal so gnome-calls shows the
     * provider as available. */
    return "normal";
}

static GListModel *
calls_xmpp_provider_get_origins(CallsProvider *provider)
{
    CallsXmppProvider *self = CALLS_XMPP_PROVIDER(provider);
    /* Lazy-create the origin on first request. */
    if (self->origin == NULL) {
        self->origin = calls_xmpp_origin_new(self);
    }
    /* gnome-calls wants a GListModel; one entry suffices. */
    GListStore *store = g_list_store_new(G_TYPE_OBJECT);
    g_list_store_append(store, G_OBJECT(self->origin));
    return G_LIST_MODEL(store);
}

/* GObject lifecycle -------------------------------------------------- */

static void
calls_xmpp_provider_finalize(GObject *object)
{
    CallsXmppProvider *self = CALLS_XMPP_PROVIDER(object);
    g_clear_object(&self->origin);
    g_clear_object(&self->patch_proxy);
    G_OBJECT_CLASS(calls_xmpp_provider_parent_class)->finalize(object);
}

static void
calls_xmpp_provider_class_init(CallsXmppProviderClass *klass)
{
    GObjectClass *object_class = G_OBJECT_CLASS(klass);
    CallsProviderClass *provider_class = CALLS_PROVIDER_CLASS(klass);

    object_class->finalize = calls_xmpp_provider_finalize;
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

/* libpeas entry point ----------------------------------------------- */

G_MODULE_EXPORT void
peas_register_types(PeasObjectModule *module)
{
    calls_xmpp_provider_register_type(G_TYPE_MODULE(module));
    peas_object_module_register_extension_type(
        module, CALLS_TYPE_PROVIDER, CALLS_TYPE_XMPP_PROVIDER);
}
