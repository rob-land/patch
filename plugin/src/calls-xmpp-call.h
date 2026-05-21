/*
 * SPDX-FileCopyrightText: 2026 Rob
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#pragma once

#include <calls-call.h>

G_BEGIN_DECLS

#define CALLS_TYPE_XMPP_CALL (calls_xmpp_call_get_type())
G_DECLARE_FINAL_TYPE(CallsXmppCall, calls_xmpp_call,
                    CALLS, XMPP_CALL, CallsCall)

CallsXmppCall *calls_xmpp_call_new(const char *session_id,
                                   const char *peer_number,
                                   gboolean inbound);

G_END_DECLS
