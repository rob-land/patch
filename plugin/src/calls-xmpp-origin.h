/*
 * SPDX-FileCopyrightText: 2026 Rob
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#pragma once

#include <calls-origin.h>
#include "calls-xmpp-provider.h"

G_BEGIN_DECLS

#define CALLS_TYPE_XMPP_ORIGIN (calls_xmpp_origin_get_type())
G_DECLARE_FINAL_TYPE(CallsXmppOrigin, calls_xmpp_origin,
                    CALLS, XMPP_ORIGIN, GObject)

CallsXmppOrigin *calls_xmpp_origin_new(CallsXmppProvider *provider);

void calls_xmpp_origin_handle_incoming(CallsXmppOrigin *self,
                                       const char *session_id,
                                       const char *number,
                                       const char *display_name);
void calls_xmpp_origin_handle_state_changed(CallsXmppOrigin *self,
                                            const char *session_id,
                                            const char *state);
void calls_xmpp_origin_handle_patch_lost(CallsXmppOrigin *self);

G_END_DECLS
