/*
 * SPDX-FileCopyrightText: 2026 Rob
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * CallsCall subclass. answer/hang_up/send_dtmf proxy through to
 * Patch's Calls1 D-Bus methods. State transitions arrive from the
 * provider's signal handler and are mapped to CallsCallState values.
 */

#define G_LOG_DOMAIN "CallsXmppCall"

#include "calls-xmpp-call.h"

struct _CallsXmppCall {
    CallsCall parent_instance;

    char       *session_id;
    GDBusProxy *proxy;
};

G_DEFINE_TYPE(CallsXmppCall, calls_xmpp_call, CALLS_TYPE_CALL);

CallsXmppCall *
calls_xmpp_call_new(const char *session_id, const char *peer_number,
                     const char *display_name, gboolean inbound)
{
    CallsXmppCall *self = g_object_new(
        CALLS_TYPE_XMPP_CALL,
        "id", peer_number,
        "inbound", inbound,
        NULL);
    self->session_id = g_strdup(session_id);
    if (display_name != NULL && display_name[0] != '\0')
        calls_call_set_name(CALLS_CALL(self), display_name);
    return self;
}

void
calls_xmpp_call_set_proxy(CallsXmppCall *self, GDBusProxy *proxy)
{
    if (self->proxy == proxy)
        return;
    g_clear_object(&self->proxy);
    if (proxy != NULL)
        self->proxy = g_object_ref(proxy);
}

/* -- state mapping --------------------------------------------------- */

void
calls_xmpp_call_set_state_from_string(CallsXmppCall *self, const char *state)
{
    CallsCallState cs;
    if (g_strcmp0(state, "active") == 0)
        cs = CALLS_CALL_STATE_ACTIVE;
    else if (g_strcmp0(state, "ringing") == 0)
        cs = CALLS_CALL_STATE_INCOMING;
    else if (g_strcmp0(state, "proposing") == 0)
        cs = CALLS_CALL_STATE_DIALING;
    else if (g_strcmp0(state, "ended") == 0 ||
             g_strcmp0(state, "rejected") == 0 ||
             g_strcmp0(state, "retracted") == 0)
        cs = CALLS_CALL_STATE_DISCONNECTED;
    else
        return;
    calls_call_set_state(CALLS_CALL(self), cs);
}

/* -- CallsCall vfuncs ------------------------------------------------ */

static const char *
xmpp_call_get_protocol(CallsCall *call G_GNUC_UNUSED)
{
    return "tel";
}

static void
xmpp_call_answer(CallsCall *call)
{
    CallsXmppCall *self = CALLS_XMPP_CALL(call);
    if (self->proxy == NULL) return;
    g_dbus_proxy_call(self->proxy, "Accept",
                      g_variant_new("(s)", self->session_id),
                      G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL);
}

static void
xmpp_call_hang_up(CallsCall *call)
{
    CallsXmppCall *self = CALLS_XMPP_CALL(call);
    if (self->proxy == NULL) return;
    g_dbus_proxy_call(self->proxy, "Hangup",
                      g_variant_new("(s)", self->session_id),
                      G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL);
}

static void
xmpp_call_send_dtmf_tone(CallsCall *call, char digit)
{
    CallsXmppCall *self = CALLS_XMPP_CALL(call);
    if (self->proxy == NULL) return;
    char buf[2] = { digit, '\0' };
    g_dbus_proxy_call(self->proxy, "SendDtmf",
                      g_variant_new("(ss)", self->session_id, buf),
                      G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL);
}

/* -- GObject lifecycle ----------------------------------------------- */

static void
calls_xmpp_call_finalize(GObject *object)
{
    CallsXmppCall *self = CALLS_XMPP_CALL(object);
    g_clear_pointer(&self->session_id, g_free);
    g_clear_object(&self->proxy);
    G_OBJECT_CLASS(calls_xmpp_call_parent_class)->finalize(object);
}

static void
calls_xmpp_call_class_init(CallsXmppCallClass *klass)
{
    GObjectClass   *object_class = G_OBJECT_CLASS(klass);
    CallsCallClass *call_class   = CALLS_CALL_CLASS(klass);

    object_class->finalize     = calls_xmpp_call_finalize;
    call_class->get_protocol   = xmpp_call_get_protocol;
    call_class->answer         = xmpp_call_answer;
    call_class->hang_up        = xmpp_call_hang_up;
    call_class->send_dtmf_tone = xmpp_call_send_dtmf_tone;
}

static void
calls_xmpp_call_init(CallsXmppCall *self G_GNUC_UNUSED)
{
}
