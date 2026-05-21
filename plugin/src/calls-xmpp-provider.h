/*
 * SPDX-FileCopyrightText: 2026 Rob
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * calls-xmpp gnome-calls plugin — provider GObject.
 *
 * Subclasses CallsProvider. One instance is loaded by libpeas at
 * gnome-calls startup and represents "the XMPP/JMP transport". The
 * provider owns one or more CallsOrigin objects (one per configured
 * XMPP account); for Patch's single-account model the count is always
 * exactly one.
 */

#pragma once

#include <calls-provider.h>

G_BEGIN_DECLS

#define CALLS_TYPE_XMPP_PROVIDER (calls_xmpp_provider_get_type())
G_DECLARE_FINAL_TYPE(CallsXmppProvider, calls_xmpp_provider,
                    CALLS, XMPP_PROVIDER, CallsProvider)

G_END_DECLS
