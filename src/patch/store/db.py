"""SQLite cache for messages and conversations.

Lives under `GLib.get_user_data_dir() / patch / patch.db`. Schema is
tiny — one table for messages, one for cached contact metadata. The
canonical record stays on the server (MAM); this DB exists so the
Messages tab has something to show before MAM catches up and after the
client has reconnected.

All operations are synchronous. SQLite on a local file is fast enough
that we don't need a worker thread for the read paths, and the write
paths run from message-arrival callbacks already on the main loop.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional

from gi.repository import GLib

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    remote_jid      TEXT    NOT NULL,
    incoming        INTEGER NOT NULL,
    body            TEXT    NOT NULL,
    sender_jid      TEXT,    -- for group SMS, the actual sender; NULL otherwise
    timestamp       REAL    NOT NULL,
    read            INTEGER NOT NULL DEFAULT 0,
    attachment_url  TEXT     -- XEP-0066 OOB url, or NULL if text-only
);
CREATE INDEX IF NOT EXISTS idx_messages_remote_ts
    ON messages(remote_jid, timestamp);

CREATE TABLE IF NOT EXISTS calls_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_jid        TEXT    NOT NULL,
    peer_label      TEXT    NOT NULL,
    direction       TEXT    NOT NULL,   -- 'incoming' | 'outgoing'
    state           TEXT    NOT NULL,   -- terminal CallSession.state
    started_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_log_started_at
    ON calls_log(started_at DESC);
"""

# Schema-evolution migrations. Each entry is a single ALTER applied iff
# the column is missing in an existing db. Keep the list append-only.
_MIGRATIONS = [
    ("attachment_url",
     "ALTER TABLE messages ADD COLUMN attachment_url TEXT"),
]


class MessageStore:
    def __init__(self, path: Optional[str] = None):
        if path is None:
            data_dir = os.path.join(GLib.get_user_data_dir(), "patch")
            os.makedirs(data_dir, exist_ok=True)
            path = os.path.join(data_dir, "patch.db")
        self._path = path
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._run_migrations()
        log.info("message store at %s", path)

    def _run_migrations(self) -> None:
        with self._cursor() as cur:
            cur.execute("PRAGMA table_info(messages)")
            existing = {row["name"] for row in cur.fetchall()}
            for col, ddl in _MIGRATIONS:
                if col not in existing:
                    log.info("running migration: add column %s", col)
                    cur.execute(ddl)

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    # -- writes ----------------------------------------------------------

    def add_message(self, remote_jid: str, incoming: bool, body: str,
                    timestamp: float, sender_jid: Optional[str] = None,
                    attachment_url: Optional[str] = None) -> int:
        # Dedup: MAM catch-up after a brief disconnect can replay a
        # message we already had via the live stream (or the local-echo
        # path for outbound). Same conversation, same body, timestamp
        # within 5 seconds == duplicate. Return the existing id so
        # callers see the same shape either way.
        with self._cursor() as cur:
            cur.execute("""
                SELECT id FROM messages
                WHERE remote_jid=?
                  AND body=?
                  AND incoming=?
                  AND ABS(timestamp - ?) < 5
                LIMIT 1
            """, (remote_jid, body, 1 if incoming else 0, timestamp))
            row = cur.fetchone()
            if row is not None:
                return row["id"]
            cur.execute(
                "INSERT INTO messages "
                "(remote_jid, incoming, body, sender_jid, timestamp, attachment_url) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (remote_jid, 1 if incoming else 0, body, sender_jid,
                 timestamp, attachment_url),
            )
            return cur.lastrowid

    def mark_read(self, remote_jid: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE messages SET read=1 WHERE remote_jid=? AND read=0",
                (remote_jid,),
            )

    # -- call log --------------------------------------------------------

    def add_call(self, peer_jid: str, peer_label: str, direction: str,
                 state: str, started_at: float) -> int:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO calls_log "
                "(peer_jid, peer_label, direction, state, started_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (peer_jid, peer_label, direction, state, started_at),
            )
            return cur.lastrowid

    def recent_calls(self, limit: int = 50) -> list[dict]:
        with self._cursor() as cur:
            cur.execute("""
                SELECT id, peer_jid, peer_label, direction, state, started_at
                FROM   calls_log
                ORDER  BY started_at DESC
                LIMIT  ?
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]

    # -- misc ------------------------------------------------------------

    def latest_timestamp(self) -> float:
        """Return the most-recent message timestamp, or 0 if the store is empty.

        Used as the lower bound for MAM catch-up queries — fetch only
        messages that arrived after our last-known-good moment.
        """
        with self._cursor() as cur:
            cur.execute("SELECT MAX(timestamp) FROM messages")
            (ts,) = cur.fetchone()
            return ts or 0.0

    # -- reads -----------------------------------------------------------

    def conversations(self) -> list[dict]:
        """One row per remote_jid, with the latest message preview + unread count.

        Sorted by most-recent message first.
        """
        with self._cursor() as cur:
            cur.execute("""
                SELECT remote_jid,
                       MAX(timestamp)                                  AS last_ts,
                       SUM(CASE WHEN read=0 AND incoming=1 THEN 1 ELSE 0 END) AS unread
                FROM   messages
                GROUP BY remote_jid
                ORDER BY last_ts DESC
            """)
            convs = [dict(r) for r in cur.fetchall()]
            # Pull the latest body for each (cheap because there are
            # typically few conversations and this is a UI render path).
            for c in convs:
                cur.execute("""
                    SELECT body, incoming
                    FROM   messages
                    WHERE  remote_jid=?
                    ORDER  BY timestamp DESC
                    LIMIT  1
                """, (c["remote_jid"],))
                row = cur.fetchone()
                if row:
                    c["last_body"] = row["body"]
                    c["last_incoming"] = bool(row["incoming"])
            return convs

    def thread(self, remote_jid: str, limit: int = 200) -> list[dict]:
        """Return messages for a conversation, oldest first."""
        with self._cursor() as cur:
            cur.execute("""
                SELECT id, remote_jid, incoming, body, sender_jid, timestamp,
                       read, attachment_url
                FROM   messages
                WHERE  remote_jid=?
                ORDER  BY timestamp ASC
                LIMIT  ?
            """, (remote_jid, limit))
            return [dict(r) for r in cur.fetchall()]
