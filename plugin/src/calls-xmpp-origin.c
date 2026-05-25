/*
 * SPDX-FileCopyrightText: 2026 Rob
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * CallsOrigin implementation. dial() invokes Patch.Calls1.Dial via
 * D-Bus; incoming-call and state-change signals from the provider
 * create/update CallsXmppCall objects and emit gnome-calls' signals.
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

/* -- CallsOrigin vfuncs ---------------------------------------------- */

static const char *
calls_xmpp_origin_get_name(CallsOrigin *origin G_GNUC_UNUSED)
{
    return "JMP.chat";
}

static void
calls_xmpp_origin_dial(CallsOrigin *origin, const char *number)
{
    CallsXmppOrigin *self = CALLS_XMPP_ORIGIN(origin);
    GDBusProxy *proxy = calls_xmpp_provider_get_proxy(self->provider);
    if (proxy == NULL) {
        g_warning("dial: no Calls1 proxy available");
        return;
    }
    GError *error = NULL;
    GVariant *result = g_dbus_proxy_call_sync(
        proxy, "Dial",
        g_variant_new("(s)", number),
        G_DBUS_CALL_FLAGS_NONE, 5000, NULL, &error);
    if (error != NULL) {
        g_warning("Dial(%s) failed: %s", number, error->message);
        g_error_free(error);
        return;
    }
    const char *session_id = NULL;
    g_variant_get(result, "(&s)", &session_id);
    g_info("Dial → session %s", session_id);

    CallsXmppCall *call = calls_xmpp_call_new(session_id, number, FALSE);
    calls_xmpp_call_set_proxy(call, proxy);
    g_hash_table_insert(self->calls, g_strdup(session_id),
                        g_object_ref(call));
    /* gnome-calls picks up the new call from this signal. */
    g_signal_emit_by_name(self, "call-added", call);
    g_object_unref(call);
    g_variant_unref(result);
}

/* -- incoming / state dispatch from provider ------------------------- */

void
calls_xmpp_origin_handle_incoming(CallsXmppOrigin *self,
                                  const char *session_id,
                                  const char *number,
                                  const char *display_name G_GNUC_UNUSED)
{
    if (g_hash_table_contains(self->calls, session_id))
        return; /* duplicate */

    GDBusProxy *proxy = calls_xmpp_provider_get_proxy(self->provider);
    CallsXmppCall *call = calls_xmpp_call_new(session_id, number, TRUE);
    calls_xmpp_call_set_proxy(call, proxy);
    g_hash_table_insert(self->calls, g_strdup(session_id),
                        g_object_ref(call));
    g_signal_emit_by_name(self, "call-added", call);
    g_object_unref(call);
    g_info("IncomingCall: session=%s number=%s", session_id, number);
}

void
calls_xmpp_origin_handle_state_changed(CallsXmppOrigin *self,
                                       const char *session_id,
                                       const char *state)
{
    CallsXmppCall *call = g_hash_table_lookup(self->calls, session_id);
    if (call == NULL)
        return;
    calls_xmpp_call_set_state_from_string(call, state);
    /* Terminal states: clean up. gnome-calls reads the final state
     * from the call object before we drop it. */
    if (g_strcmp0(state, "ended") == 0 ||
        g_strcmp0(state, "rejected") == 0 ||
        g_strcmp0(state, "retracted") == 0)
    {
        g_signal_emit_by_name(self, "call-removed", call);
        g_hash_table_remove(self->calls, session_id);
    }
}

/* -- GObject lifecycle ----------------------------------------------- */

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

    object_class->finalize = calls_xmpp_origin_finalize;
    origin_class->get_name = calls_xmpp_origin_get_name;
    origin_class->dial     = calls_xmpp_origin_dial;
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
