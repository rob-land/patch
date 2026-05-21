"""Tests for the SQLite message store.

Uses a per-test temp file so the user's real ~/.local/share/patch/patch.db
isn't touched. Cohort ad-hoc style; run with `python3 tests/test_db.py`.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "db",
    os.path.join(_HERE, "..", "src", "patch", "store", "db.py"))
db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(db)


def eq(label, got, want):
    if got != want:
        print(f"FAIL  {label}: got={got!r} want={want!r}")
        sys.exit(1)
    print(f"ok    {label}")


with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "patch-test.db")
    store = db.MessageStore(path=path)

    # Empty state
    eq("empty conversations", store.conversations(), [])
    eq("empty thread",        store.thread("nobody@example.org"), [])

    # Single inbound message
    store.add_message("+15551111@cheogram.com", incoming=True,
                      body="hi", timestamp=100.0)
    convs = store.conversations()
    eq("one conversation",         len(convs), 1)
    eq("conversation jid",         convs[0]["remote_jid"], "+15551111@cheogram.com")
    eq("conversation last_body",   convs[0]["last_body"], "hi")
    eq("conversation last_incoming", convs[0]["last_incoming"], True)
    eq("conversation unread",      convs[0]["unread"], 1)

    # Outbound message into the same conversation
    store.add_message("+15551111@cheogram.com", incoming=False,
                      body="howdy", timestamp=101.0)
    convs = store.conversations()
    eq("still one conversation",   len(convs), 1)
    eq("conversation last_body 2", convs[0]["last_body"], "howdy")
    eq("conversation unread (outbound doesn't count)", convs[0]["unread"], 1)

    # Mark read
    store.mark_read("+15551111@cheogram.com")
    convs = store.conversations()
    eq("conversation unread cleared", convs[0]["unread"], 0)

    # Thread ordering
    thread = store.thread("+15551111@cheogram.com")
    eq("thread length", len(thread), 2)
    eq("thread[0] body", thread[0]["body"], "hi")
    eq("thread[1] body", thread[1]["body"], "howdy")

    # Multi-conversation sort by recency
    store.add_message("+15552222@cheogram.com", incoming=True,
                      body="newer", timestamp=200.0)
    convs = store.conversations()
    eq("convs sorted by recency: newest first",
       convs[0]["remote_jid"], "+15552222@cheogram.com")

    # Group SMS — sender_jid stored separately
    store.add_message("+15551111,+15552222@cheogram.com", incoming=True,
                      body="hello group", timestamp=300.0,
                      sender_jid="+15553333@cheogram.com")
    thread = store.thread("+15551111,+15552222@cheogram.com")
    eq("group thread sender_jid",
       thread[0]["sender_jid"], "+15553333@cheogram.com")

print()
print("PASS  test_db")
