"""Tests for the phone number / JID round trip.

The cohort uses ad-hoc test scripts rather than a formal pytest setup; run
this with `python3 tests/test_numfmt.py` from the project root. Exits
non-zero on failure.
"""

from __future__ import annotations

import os
import sys

# Load numfmt directly without going through patch/__init__.py — that
# pulls in patch.const which is only generated at meson-install time.
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "numfmt",
    os.path.join(os.path.dirname(__file__), "..", "src", "patch", "numfmt.py"))
numfmt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(numfmt)


def eq(label, got, want):
    if got != want:
        print(f"FAIL  {label}: got={got!r} want={want!r}")
        sys.exit(1)
    print(f"ok    {label}")


# E.164 normalization
eq("e164: already E.164",
   numfmt.normalize_e164("+17135551234"), "+17135551234")
eq("e164: NANP local 10-digit",
   numfmt.normalize_e164("7135551234"), "+17135551234")
eq("e164: NANP 11-digit with leading 1",
   numfmt.normalize_e164("17135551234"), "+17135551234")
eq("e164: pretty NANP",
   numfmt.normalize_e164("(713) 555-1234"), "+17135551234")
eq("e164: GB local",
   numfmt.normalize_e164("2079460958", "GB"), "+442079460958")
eq("e164: empty",       numfmt.normalize_e164(""),  None)
eq("e164: just spaces", numfmt.normalize_e164("   "), None)

# JID round trip
eq("number_to_jid",
   numfmt.number_to_jid("+17135551234", "cheogram.com"),
   "+17135551234@cheogram.com")
eq("jid_to_number: gateway match",
   numfmt.jid_to_number("+17135551234@cheogram.com", "cheogram.com"),
   "+17135551234")
eq("jid_to_number: gateway mismatch",
   numfmt.jid_to_number("user@example.org", "cheogram.com"), None)
eq("jid_to_number: handles resource",
   numfmt.jid_to_number("+17135551234@cheogram.com/foo", "cheogram.com"),
   "+17135551234")
eq("jid_to_number: no '+' prefix",
   numfmt.jid_to_number("17135551234@cheogram.com", "cheogram.com"), None)

# Group JID detection + body parsing (cheogram group SMS shape)
eq("is_group_jid: yes",
   numfmt.is_group_jid("+15551111,+15552222@cheogram.com"), True)
eq("is_group_jid: no",
   numfmt.is_group_jid("+15551111@cheogram.com"), False)
eq("parse_group_body: tagged",
   numfmt.parse_group_body("<xmpp:+15551111@cheogram.com> hello"),
   ("+15551111@cheogram.com", "hello"))
eq("parse_group_body: untagged",
   numfmt.parse_group_body("just text"), (None, "just text"))

# Display formatting
eq("format: NANP 11-digit",
   numfmt.format_for_display("+17135551234"),
   "+1 (713) 555-1234")
eq("format: 10-digit",
   numfmt.format_for_display("7135551234"), "7135551234")

print()
print("PASS  test_numfmt")
