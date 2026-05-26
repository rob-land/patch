/*
 * SPDX-FileCopyrightText: 2026 Rob
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * CallsOrigin implementation. dial() invokes Patch.Calls1.Dial via
 * D-Bus; incoming-call and state-change signals from the provider
 * create/update CallsXmppCall objects and emit gnome-calls' signals.
 */

#define G_LOG_DOMAIN "CallsXmppOrigin"

#include "calls-message-source.h"
#include "calls-xmpp-origin.h"
#include "calls-xmpp-call.h"

struct _CallsXmppOrigin {
    GObject parent_instance;

    CallsXmppProvider *provider;     /* weak; provider owns us */
    GList             *calls;        /* CallsXmppCall*, owned refs */
    GHashTable        *calls_by_sid; /* session_id -> CallsXmppCall* (borrowed) */
};

static void calls_xmpp_origin_origin_iface_init(CallsOriginInterface *iface);
static void calls_xmpp_origin_message_source_iface_init(CallsMessageSourceInterface *iface);

G_DEFINE_TYPE_WITH_CODE(
    CallsXmppOrigin, calls_xmpp_origin, G_TYPE_OBJECT,
    G_IMPLEMENT_INTERFACE(CALLS_TYPE_MESSAGE_SOURCE,
                          calls_xmpp_origin_message_source_iface_init)
    G_IMPLEMENT_INTERFACE(CALLS_TYPE_ORIGIN,
                          calls_xmpp_origin_origin_iface_init))

enum {
    PROP_0,
    PROP_ID,
    PROP_NAME,
    PROP_CALLS,
    PROP_COUNTRY_CODE,
    PROP_EMERGENCY_NUMBERS,
    PROP_LAST,
};
static GParamSpec *props[PROP_LAST];

CallsXmppOrigin *
calls_xmpp_origin_new(CallsXmppProvider *provider)
{
    CallsXmppOrigin *self = g_object_new(CALLS_TYPE_XMPP_ORIGIN, NULL);
    self->provider = provider;
    return self;
}

/* -- property accessors ---------------------------------------------- */

static void
get_property(GObject *object, guint prop_id, GValue *value, GParamSpec *pspec)
{
    CallsXmppOrigin *self = CALLS_XMPP_ORIGIN(object);

    switch (prop_id) {
    case PROP_ID:
        g_value_set_string(value, "xmpp-jmp");
        break;
    case PROP_NAME:
        g_value_set_string(value, "JMP.chat");
        break;
    case PROP_CALLS:
        g_value_set_pointer(value, g_list_copy(self->calls));
        break;
    case PROP_COUNTRY_CODE:
        g_value_set_string(value, NULL);
        break;
    case PROP_EMERGENCY_NUMBERS:
        g_value_set_boxed(value, NULL);
        break;
    default:
        G_OBJECT_WARN_INVALID_PROPERTY_ID(object, prop_id, pspec);
        break;
    }
}

/* -- CallsOrigin vfuncs ---------------------------------------------- */

static void
xmpp_origin_dial(CallsOrigin *origin, const char *number)
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
    g_info("Dial -> session %s", session_id);

    CallsXmppCall *call = calls_xmpp_call_new(session_id, number,
                                               NULL, FALSE);
    calls_xmpp_call_set_proxy(call, proxy);
    self->calls = g_list_append(self->calls, call);
    g_hash_table_insert(self->calls_by_sid, g_strdup(session_id), call);
    g_signal_emit_by_name(self, "call-added", call);
    g_variant_unref(result);
}

static gboolean
xmpp_origin_supports_protocol(CallsOrigin *origin G_GNUC_UNUSED,
                              const char *protocol)
{
    return g_strcmp0(protocol, "tel") == 0;
}

/* -- incoming / state dispatch from provider ------------------------- */

void
calls_xmpp_origin_handle_incoming(CallsXmppOrigin *self,
                                  const char *session_id,
                                  const char *number,
                                  const char *display_name)
{
    if (g_hash_table_contains(self->calls_by_sid, session_id))
        return;

    GDBusProxy *proxy = calls_xmpp_provider_get_proxy(self->provider);
    CallsXmppCall *call = calls_xmpp_call_new(session_id, number,
                                               display_name, TRUE);
    calls_xmpp_call_set_proxy(call, proxy);
    self->calls = g_list_append(self->calls, call);
    g_hash_table_insert(self->calls_by_sid, g_strdup(session_id), call);
    g_signal_emit_by_name(self, "call-added", call);
    g_info("IncomingCall: session=%s number=%s", session_id, number);
}

void
calls_xmpp_origin_handle_state_changed(CallsXmppOrigin *self,
                                       const char *session_id,
                                       const char *state)
{
    CallsXmppCall *call = g_hash_table_lookup(self->calls_by_sid,
                                               session_id);
    if (call == NULL)
        return;
    calls_xmpp_call_set_state_from_string(call, state);
    if (g_strcmp0(state, "ended") == 0 ||
        g_strcmp0(state, "rejected") == 0 ||
        g_strcmp0(state, "retracted") == 0)
    {
        g_signal_emit_by_name(self, "call-removed", call);
        self->calls = g_list_remove(self->calls, call);
        g_hash_table_remove(self->calls_by_sid, session_id);
        g_object_unref(call);
    }
}

void
calls_xmpp_origin_handle_patch_lost(CallsXmppOrigin *self)
{
    GList *l = self->calls;
    self->calls = NULL;
    g_hash_table_remove_all(self->calls_by_sid);

    for (GList *node = l; node != NULL; node = node->next) {
        CallsXmppCall *call = CALLS_XMPP_CALL(node->data);
        calls_xmpp_call_set_proxy(call, NULL);
        calls_xmpp_call_set_state_from_string(call, "ended");
        g_signal_emit_by_name(self, "call-removed", call);
        g_object_unref(call);
    }
    g_list_free(l);
    g_info("Patch vanished -- ended all active calls");
}

/* -- interface init -------------------------------------------------- */

static void
calls_xmpp_origin_origin_iface_init(CallsOriginInterface *iface)
{
    iface->dial = xmpp_origin_dial;
    iface->supports_protocol = xmpp_origin_supports_protocol;
}

static void
calls_xmpp_origin_message_source_iface_init(
    CallsMessageSourceInterface *iface G_GNUC_UNUSED)
{
}

/* -- GObject lifecycle ----------------------------------------------- */

static void
calls_xmpp_origin_finalize(GObject *object)
{
    CallsXmppOrigin *self = CALLS_XMPP_ORIGIN(object);
    g_list_free_full(self->calls, g_object_unref);
    g_clear_pointer(&self->calls_by_sid, g_hash_table_unref);
    G_OBJECT_CLASS(calls_xmpp_origin_parent_class)->finalize(object);
}

static void
calls_xmpp_origin_class_init(CallsXmppOriginClass *klass)
{
    GObjectClass *object_class = G_OBJECT_CLASS(klass);

    object_class->get_property = get_property;
    object_class->finalize     = calls_xmpp_origin_finalize;

#define IMPLEMENTS(ID, NAME) \
    g_object_class_override_property(object_class, ID, NAME); \
    props[ID] = g_object_class_find_property(object_class, NAME);

    IMPLEMENTS(PROP_ID, "id");
    IMPLEMENTS(PROP_NAME, "name");
    IMPLEMENTS(PROP_CALLS, "calls");
    IMPLEMENTS(PROP_COUNTRY_CODE, "country-code");
    IMPLEMENTS(PROP_EMERGENCY_NUMBERS, "emergency-numbers");

#undef IMPLEMENTS
}

static void
calls_xmpp_origin_init(CallsXmppOrigin *self)
{
    self->calls_by_sid = g_hash_table_new_full(g_str_hash, g_str_equal,
                                                g_free, NULL);
}
