/*
 * SPDX-FileCopyrightText: 2026 Rob
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Scaffold only. Methods proxy through to Patch's D-Bus surface; state
 * transitions arrive via Patch.Calls1.State signals and are translated
 * into CallsCallState transitions here.
 */

#define G_LOG_DOMAIN "CallsXmppCall"

#include "calls-xmpp-call.h"

struct _CallsXmppCall {
    CallsCall parent_instance;

    char *session_id;        /* matches Patch's CallSession.session_id */
    char *peer_number;       /* E.164 for display */
    gboolean inbound;
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
    return self;
}

/* CallsCall vfuncs --------------------------------------------------- */

static const char *
calls_xmpp_call_get_id(CallsCall *call)
{
    return CALLS_XMPP_CALL(call)->peer_number;
}

static void
calls_xmpp_call_answer(CallsCall *call)
{
    /* TODO: call Patch.Calls1.Accept(session_id) via D-Bus. */
    g_message("answer: session=%s (no-op stub)",
              CALLS_XMPP_CALL(call)->session_id);
}

static void
calls_xmpp_call_hang_up(CallsCall *call)
{
    /* TODO: call Patch.Calls1.Hangup(session_id) via D-Bus. */
    g_message("hang_up: session=%s (no-op stub)",
              CALLS_XMPP_CALL(call)->session_id);
}

/* GObject lifecycle -------------------------------------------------- */

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

    object_class->finalize = calls_xmpp_call_finalize;
    call_class->get_id     = calls_xmpp_call_get_id;
    call_class->answer     = calls_xmpp_call_answer;
    call_class->hang_up    = calls_xmpp_call_hang_up;
}

static void
calls_xmpp_call_class_finalize(CallsXmppCallClass *klass G_GNUC_UNUSED)
{
}

static void
calls_xmpp_call_init(CallsXmppCall *self G_GNUC_UNUSED)
{
}
