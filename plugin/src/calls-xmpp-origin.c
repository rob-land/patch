/*
 * SPDX-FileCopyrightText: 2026 Rob
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Scaffold only. dial() will call Patch.Calls1.Dial via D-Bus; the
 * resulting session id maps to a CallsXmppCall that we keep in a hash
 * table keyed by session id.
 */

#define G_LOG_DOMAIN "CallsXmppOrigin"

#include "calls-xmpp-origin.h"
#include "calls-xmpp-call.h"

struct _CallsXmppOrigin {
    CallsOrigin parent_instance;

    CallsXmppProvider *provider;     /* weak; provider owns us */

    /* session_id (char*) -> CallsXmppCall* */
    GHashTable *calls;
};

G_DEFINE_DYNAMIC_TYPE(CallsXmppOrigin, calls_xmpp_origin,
                     CALLS_TYPE_ORIGIN);

CallsXmppOrigin *
calls_xmpp_origin_new(CallsXmppProvider *provider)
{
    CallsXmppOrigin *self = g_object_new(CALLS_TYPE_XMPP_ORIGIN, NULL);
    self->provider = provider;
    return self;
}

/* CallsOrigin vfuncs ------------------------------------------------- */

static const char *
calls_xmpp_origin_get_name(CallsOrigin *origin)
{
    return "JMP.chat";
}

static void
calls_xmpp_origin_dial(CallsOrigin *origin, const char *number)
{
    /* TODO: call Patch.Calls1.Dial(number) via the provider's D-Bus
     * proxy. The reply contains a session id; we then construct a
     * CallsXmppCall and emit "call-added" so gnome-calls picks it up
     * for the active-call UI. */
    g_message("dial: %s (no-op stub)", number);
}

/* GObject lifecycle -------------------------------------------------- */

static void
calls_xmpp_origin_finalize(GObject *object)
{
    CallsXmppOrigin *self = CALLS_XMPP_ORIGIN(object);
    g_clear_pointer(&self->calls, g_hash_table_unref);
    G_OBJECT_CLASS(calls_xmpp_origin_parent_class)->finalize(object);
}

static void
calls_xmpp_origin_class_init(CallsXmppOriginClass *klass)
{
    GObjectClass     *object_class = G_OBJECT_CLASS(klass);
    CallsOriginClass *origin_class = CALLS_ORIGIN_CLASS(klass);

    object_class->finalize  = calls_xmpp_origin_finalize;
    origin_class->get_name  = calls_xmpp_origin_get_name;
    origin_class->dial      = calls_xmpp_origin_dial;
}

static void
calls_xmpp_origin_class_finalize(CallsXmppOriginClass *klass G_GNUC_UNUSED)
{
}

static void
calls_xmpp_origin_init(CallsXmppOrigin *self)
{
    self->calls = g_hash_table_new_full(g_str_hash, g_str_equal,
                                        g_free, g_object_unref);
}
