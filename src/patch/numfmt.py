"""Phone number normalization and JID round-trip for cheogram-style gateways.

We always speak E.164 (`+17135551234`) internally. JMP encodes phone numbers
as JIDs of the form `<E164>@<gateway-domain>` (`+17135551234@cheogram.com`
for the default gateway), so the conversion is mechanical.

`libphonenumber` would be the canonical dependency here — gnome-calls uses
it for the same purpose — but it pulls in a non-trivial C++ build. We use
a small subset of its functionality (E.164 normalize + display formatting)
that we can implement in pure Python without sacrificing accuracy on the
common JMP cases.
"""

from __future__ import annotations

import re

_DIGITS_RE = re.compile(r"[^0-9+]")


# A handful of NANP-known country codes for the common cases. The default
# country in GSettings (`default-country`, default "US") tells us which one
# to assume when the user types a 10-digit local-form number; for anything
# else we expect the user to enter E.164 directly (leading "+").
_COUNTRY_PREFIXES = {
    "US": "+1",
    "CA": "+1",
    "GB": "+44",
    "AU": "+61",
    "NZ": "+64",
}


def normalize_e164(raw: str, default_country: str = "US") -> str | None:
    """Return an E.164-formatted number, or None if the input can't be parsed.

    Accepts: "+1 713 555 1234", "(713) 555-1234", "713-555-1234", etc.
    Strips spaces, dashes, parens, then either trusts a leading "+" or
    prepends the default country prefix.
    """
    if not raw:
        return None
    s = _DIGITS_RE.sub("", raw.strip())
    if not s:
        return None
    if s.startswith("+"):
        # Already E.164-ish — just sanity-check the remainder is digits.
        if not s[1:].isdigit():
            return None
        return s
    # Local-form number. Need a country prefix.
    prefix = _COUNTRY_PREFIXES.get(default_country.upper())
    if not prefix:
        return None
    # NANP local numbers are 10 digits; some users dial with the leading
    # "1" included. Either way we strip a leading "1" before prepending
    # "+1" so we don't end up with "+11713...".
    if prefix == "+1" and s.startswith("1") and len(s) == 11:
        s = s[1:]
    return prefix + s


def number_to_jid(number_e164: str, gateway_domain: str) -> str:
    """`+17135551234`, `cheogram.com` -> `+17135551234@cheogram.com`."""
    return f"{number_e164}@{gateway_domain}"


def jid_to_number(jid: str, gateway_domain: str) -> str | None:
    """Reverse of number_to_jid; returns None when the JID is not on the gateway.

    Defensive: only strips the gateway-domain suffix; arbitrary other XMPP
    JIDs (chat.example.org, conference rooms, etc.) return None so callers
    don't accidentally treat e.g. `admin@example.org` as a phone number.
    """
    if "@" not in jid:
        return None
    local, _, domain = jid.partition("@")
    # Resource? Strip anything after `/`.
    domain = domain.partition("/")[0]
    if domain != gateway_domain:
        return None
    if not local.startswith("+"):
        return None
    return local


# A handful of group-SMS JIDs we've seen from JMP use a comma-separated list of
# numbers in the local part: `+1555,+1666@cheogram.com`. Treat these as multi-
# party single-thread conversations; the *individual* sender is encoded in the
# message body per cheogram convention.
def is_group_jid(jid: str) -> bool:
    local, _, _ = jid.partition("@")
    return "," in local


_BODY_SENDER_RE = re.compile(r"^<xmpp:([^>]+)>\s*(.*)", re.DOTALL)


def parse_group_body(body: str) -> tuple[str | None, str]:
    """Split a cheogram group-SMS body into (sender_jid, plaintext).

    The wire format is `<xmpp:+15551234@cheogram.com> the message text`.
    Returns (None, body) if the body doesn't match — let the caller decide
    whether to render the prefix verbatim or treat it as a single-sender.
    """
    m = _BODY_SENDER_RE.match(body or "")
    if not m:
        return None, body or ""
    return m.group(1), m.group(2)


def format_for_display(number_e164: str) -> str:
    """Cosmetic E.164 -> nicer display. Pure heuristic — no localization."""
    if not number_e164.startswith("+"):
        return number_e164
    digits = number_e164[1:]
    if len(digits) == 11 and digits.startswith("1"):
        # NANP: +1 (713) 555-1234
        return f"+1 ({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    if len(digits) == 10:
        # Local-form NANP: (713) 555-1234
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    # Fall through unchanged.
    return number_e164


def format_as_typed(raw: str) -> str:
    """Progressively format a number as the user types it.

    Mirrors the iOS / Android dialer behaviour: '7' stays '7',
    '713' becomes '713', '7135' becomes '713-5', '7135551234' becomes
    '(713) 555-1234'. Non-digit characters (`*`, `#`, leading `+`)
    are preserved so DTMF-style input still works.

    Pure NANP heuristic for now — international users with leading `+`
    get raw digits back (no localized grouping). Matches what Cheogram-
    Android does.
    """
    if not raw:
        return ""
    # Preserve leading '+' (E.164) and any trailing DTMF-style chars
    # the user might have typed (* or #). We only re-group the digit
    # prefix.
    plus = ""
    if raw.startswith("+"):
        plus = "+"
        rest = raw[1:]
    else:
        rest = raw
    # Split into a leading digit run and the tail (anything that isn't
    # a digit, e.g. '*' '#' user-typed mid-string). We only format the
    # leading run; the tail stays verbatim.
    i = 0
    while i < len(rest) and rest[i].isdigit():
        i += 1
    digits = rest[:i]
    tail = rest[i:]

    if plus == "+":
        # International — leave digits alone aside from a single space
        # after the country-code prefix where we recognise it.
        if digits.startswith("1") and len(digits) > 1:
            head = "1 "
            digits = digits[1:]
        else:
            head = ""
        return "+" + head + digits + tail

    # NANP grouping
    if len(digits) <= 3:
        body = digits
    elif len(digits) <= 6:
        body = f"({digits[:3]}) {digits[3:]}" if len(digits) > 3 else digits
    elif len(digits) <= 10:
        body = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    elif len(digits) == 11 and digits.startswith("1"):
        # User typed a leading 1 — render as +1 (xxx) xxx-xxxx
        body = f"+1 ({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    else:
        body = digits
    return body + tail
