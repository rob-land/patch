/*
 * SPDX-FileCopyrightText: 2026 Rob
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * CallsCall implementation. answer/hang_up/send_dtmf proxy through to
 * Patch's Calls1 D-Bus methods. State transitions arrive from the
 * provider's signal handler and are mapped to CallsCallState values.
 */

#define G_LOG_DOMAIN "CallsXmppCall"

#include "calls-xmpp-call.h"

struct _CallsXmppCall {
    CallsCall parent_instance;

    char       *session_id;
    char       *peer_number;
    gboolean    inbound;
    GDBusProxy *proxy;     /* borrowed from provider; not ref'd */
};

G_DEFINE_DYNAMIC_TYPE(CallsXmppCall, calls_xmpp_call, CALLS_TYPE_CALL);

CallsXmppCall *
calls_xmpp_call_new(const char *session_id, const char *peer_number,
                   gboolean inbound)
{
    CallsXmppCall *self = g_object_new(CALLS_TYPE_XMPP_CALL, NULL);
    self->session_id  = g_strdup(session_id);
    self->peer_number = g_strdup(peer_number);
    self->inbound     = inbound;
    /* Set initial state based on direction. */
    CallsCallState initial = inbound ? CALLS_CALL_STATE_INCOMING
                                     : CALLS_CALL_STATE_DIALING;
    calls_call_set_state(CALLS_CALL(self), initial);
    return self;
}

void
calls_xmpp_call_set_proxy(CallsXmppCall *self, GDBusProxy *proxy)
{
    self->proxy = proxy;
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
        return; /* unknown state — no transition */
    calls_call_set_state(CALLS_CALL(self), cs);
}

/* -- CallsCall vfuncs ------------------------------------------------ */

static const char *
calls_xmpp_call_get_id(CallsCall *call)
{
    return CALLS_XMPP_CALL(call)->peer_number;
}

static gboolean
calls_xmpp_call_get_inbound(CallsCall *call)
{
    return CALLS_XMPP_CALL(call)->inbound;
}

static void
calls_xmpp_call_answer(CallsCall *call)
{
    CallsXmppCall *self = CALLS_XMPP_CALL(call);
    if (self->proxy == NULL) return;
    g_dbus_proxy_call(self->proxy, "Accept",
                      g_variant_new("(s)", self->session_id),
                      G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL);
}

static void
calls_xmpp_call_hang_up(CallsCall *call)
{
    CallsXmppCall *self = CALLS_XMPP_CALL(call);
    if (self->proxy == NULL) return;
    g_dbus_proxy_call(self->proxy, "Hangup",
                      g_variant_new("(s)", self->session_id),
                      G_DBUS_CALL_FLAGS_NONE, -1, NULL, NULL, NULL);
}

static void
calls_xmpp_call_send_dtmf_tone(CallsCall *call, char digit)
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
    g_clear_pointer(&self->peer_number, g_free);
    G_OBJECT_CLASS(calls_xmpp_call_parent_class)->finalize(object);
}

static void
calls_xmpp_call_class_init(CallsXmppCallClass *klass)
{
    GObjectClass   *object_class = G_OBJECT_CLASS(klass);
    CallsCallClass *call_class   = CALLS_CALL_CLASS(klass);

    object_class->finalize     = calls_xmpp_call_finalize;
    call_class->get_id         = calls_xmpp_call_get_id;
    call_class->get_inbound    = calls_xmpp_call_get_inbound;
    call_class->answer         = calls_xmpp_call_answer;
    call_class->hang_up        = calls_xmpp_call_hang_up;
    call_class->send_dtmf_tone = calls_xmpp_call_send_dtmf_tone;
}

static void
calls_xmpp_call_class_finalize(CallsXmppCallClass *klass G_GNUC_UNUSED)
{
}

static void
calls_xmpp_call_init(CallsXmppCall *self G_GNUC_UNUSED)
{
}
