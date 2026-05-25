/*
 * SPDX-FileCopyrightText: 2026 Rob
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#pragma once

#include <calls-provider.h>

G_BEGIN_DECLS

#define CALLS_TYPE_XMPP_PROVIDER (calls_xmpp_provider_get_type())
G_DECLARE_FINAL_TYPE(CallsXmppProvider, calls_xmpp_provider,
                    CALLS, XMPP_PROVIDER, CallsProvider)

GDBusProxy *calls_xmpp_provider_get_proxy(CallsXmppProvider *self);

G_END_DECLS
