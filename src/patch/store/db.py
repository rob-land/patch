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
    # XEP-0184 delivery receipts: outgoing messages persist their
    # stanza id here so the inbound <received id='..'/> can be matched
    # back. delivery_state is NULL on inbound rows (the column is
    # outgoing-only) and one of {'sent', 'delivered', 'failed'} on
    # outgoing rows. 'sent' is the local-echo state set at send-time;
    # 'delivered' fires when the receipt arrives.
    ("xmpp_id",
     "ALTER TABLE messages ADD COLUMN xmpp_id TEXT"),
    ("delivery_state",
     "ALTER TABLE messages ADD COLUMN delivery_state TEXT"),
    # XEP-0444 reactions: JSON map { sender_jid: [emoji, ...] }. A
    # reactions stanza from a peer REPLACES that peer's entire set
    # on a message, so we serialise the union once per write rather
    # than keeping a normalised reactions table.
    ("reactions_json",
     "ALTER TABLE messages ADD COLUMN reactions_json TEXT"),
    # XEP-0461 quoted reply: xmpp_id of the message this one is
    # replying to. Look up reply_to_id → messages.xmpp_id at render
    # time so a re-edited / corrected original surfaces the latest
    # body. NULL on messages that aren't a reply.
    ("reply_to_id",
     "ALTER TABLE messages ADD COLUMN reply_to_id TEXT"),
    # XEP-0308 last message correction: timestamp of the most recent
    # edit applied to body. NULL = never corrected. Renderer shows
    # "(edited)" caption when set.
    ("corrected_at",
     "ALTER TABLE messages ADD COLUMN corrected_at REAL"),
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
        self._ensure_fts()
        log.info("message store at %s", path)

    def _run_migrations(self) -> None:
        with self._cursor() as cur:
            cur.execute("PRAGMA table_info(messages)")
            existing = {row["name"] for row in cur.fetchall()}
            for col, ddl in _MIGRATIONS:
                if col not in existing:
                    log.info("running migration: add column %s", col)
                    cur.execute(ddl)

    def _ensure_fts(self) -> None:
        """Create the FTS5 full-text index over message bodies if it
        doesn't exist, and wire triggers to keep it in sync."""
        with self._cursor() as cur:
            # Check if the FTS table already exists — if we're about
            # to create it for the first time on an existing db, we
            # need to rebuild the index from the messages table.
            cur.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='messages_fts'")
            fts_existed = cur.fetchone() is not None

            cur.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                    USING fts5(body, content=messages, content_rowid=id);

                CREATE TRIGGER IF NOT EXISTS messages_fts_ai
                    AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, body)
                        VALUES (new.id, new.body);
                END;

                CREATE TRIGGER IF NOT EXISTS messages_fts_au
                    AFTER UPDATE OF body ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, body)
                        VALUES('delete', old.id, old.body);
                    INSERT INTO messages_fts(rowid, body)
                        VALUES (new.id, new.body);
                END;

                CREATE TRIGGER IF NOT EXISTS messages_fts_ad
                    AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, body)
                        VALUES('delete', old.id, old.body);
                END;
            """)
            # Always rebuild at startup. FTS5 external-content tables
            # can get out of sync if the prior process crashed mid-write
            # or if the table was created without a rebuild (the initial
            # bug). For phone-scale volumes (~10k messages) rebuild is
            # sub-second; for larger stores it's a one-time cost that
            # subsequent trigger-maintained inserts avoid repeating.
            cur.execute("SELECT COUNT(*) FROM messages")
            msg_count = cur.fetchone()[0]
            if msg_count > 0:
                cur.execute(
                    "INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")

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
                    attachment_url: Optional[str] = None,
                    xmpp_id: Optional[str] = None,
                    delivery_state: Optional[str] = None,
                    reply_to_id: Optional[str] = None) -> int:
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
                "(remote_jid, incoming, body, sender_jid, timestamp, "
                "attachment_url, xmpp_id, delivery_state, reply_to_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (remote_jid, 1 if incoming else 0, body, sender_jid,
                 timestamp, attachment_url, xmpp_id, delivery_state,
                 reply_to_id),
            )
            return cur.lastrowid

    def apply_correction(self, target_xmpp_id: str, new_body: str,
                         corrected_at: float) -> bool:
        """Replace the body of the message with stanza id
        ``target_xmpp_id`` and stamp corrected_at. XEP-0308 only allows
        the original sender to correct, so we don't enforce that here —
        the wire layer drops mismatched-sender corrections."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE messages SET body=?, corrected_at=? WHERE xmpp_id=?",
                (new_body, corrected_at, target_xmpp_id),
            )
            return cur.rowcount > 0

    def set_reactions(self, target_xmpp_id: str, sender_jid: str,
                      emojis: list[str]) -> bool:
        """Replace ``sender_jid``'s reactions on the message with stanza
        id ``target_xmpp_id``. Returns True if the target row was
        found. Reactions are stored as a JSON map { sender: [emojis] }
        keyed by bare JID; passing an empty list clears that sender's
        entry."""
        import json
        with self._cursor() as cur:
            cur.execute(
                "SELECT id, reactions_json FROM messages WHERE xmpp_id=? LIMIT 1",
                (target_xmpp_id,))
            row = cur.fetchone()
            if row is None:
                return False
            try:
                existing = json.loads(row["reactions_json"] or "{}")
            except (ValueError, TypeError):
                existing = {}
            if emojis:
                existing[sender_jid] = list(emojis)
            else:
                existing.pop(sender_jid, None)
            cur.execute(
                "UPDATE messages SET reactions_json=? WHERE id=?",
                (json.dumps(existing) if existing else None, row["id"]),
            )
            return True

    def set_delivery_state(self, xmpp_id: str, state: str) -> None:
        """Update delivery_state for an outgoing message keyed by its
        stanza id. No-op if no row matches — receipts can race ahead of
        the message persistence path on a slow disk, or come in for an
        outbound message that landed via another client."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE messages SET delivery_state=? "
                "WHERE xmpp_id=? AND incoming=0",
                (state, xmpp_id),
            )

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

    def search(self, query: str, limit: int = 50) -> list[dict]:
        """Full-text search across all message bodies via FTS5.

        Returns rows with id, remote_jid, body, timestamp, incoming —
        enough for the UI to show a result list with conversation
        context. Results are ranked by FTS5's bm25 relevance score.
        """
        if not query or not query.strip():
            return []
        with self._cursor() as cur:
            # FTS5 match syntax: quote the user's input so special
            # chars (AND, OR, NOT, quotes) don't break the query.
            # Wrapping in double-quotes makes it a phrase search;
            # appending * enables prefix matching.
            safe = query.strip().replace('"', '""')
            cur.execute("""
                SELECT m.id, m.remote_jid, m.body, m.timestamp, m.incoming,
                       m.sender_jid
                FROM   messages_fts f
                JOIN   messages m ON m.id = f.rowid
                WHERE  messages_fts MATCH ?
                ORDER  BY rank
                LIMIT  ?
            """, (f'"{safe}"*', limit))
            return [dict(r) for r in cur.fetchall()]

    def conversation_for(self, remote_jid: str) -> dict | None:
        """Single-conversation summary (same shape as conversations())."""
        with self._cursor() as cur:
            cur.execute("""
                SELECT remote_jid,
                       MAX(timestamp) AS last_ts,
                       SUM(CASE WHEN read=0 AND incoming=1 THEN 1 ELSE 0 END) AS unread
                FROM   messages
                WHERE  remote_jid=?
                GROUP BY remote_jid
            """, (remote_jid,))
            row = cur.fetchone()
            if row is None:
                return None
            c = dict(row)
            cur.execute("""
                SELECT body, incoming
                FROM   messages
                WHERE  remote_jid=?
                ORDER  BY timestamp DESC
                LIMIT  1
            """, (remote_jid,))
            body_row = cur.fetchone()
            if body_row:
                c["last_body"] = body_row["body"]
                c["last_incoming"] = bool(body_row["incoming"])
            return c

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

    # JMP voicemails arrive as chat messages from a cheogram-style JID
    # carrying an audio OOB attachment + the transcript in the body.
    # We detect by URL extension at query time so we don't need a
    # dedicated column; the cohort prefers heuristics over schema for
    # JMP-specific shapes since the wire format isn't stable.
    _VOICEMAIL_EXTS = ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".oga"

    def recent_voicemails(self, limit: int = 50) -> list[dict]:
        with self._cursor() as cur:
            ext_clause = " OR ".join(
                "LOWER(attachment_url) LIKE ?" for _ in self._VOICEMAIL_EXTS)
            params = ["%" + ext + "%" for ext in self._VOICEMAIL_EXTS] + [limit]
            cur.execute(f"""
                SELECT id, remote_jid, body, attachment_url, timestamp, read
                FROM   messages
                WHERE  attachment_url IS NOT NULL
                  AND  incoming = 1
                  AND  ({ext_clause})
                ORDER  BY timestamp DESC
                LIMIT  ?
            """, params)
            return [dict(r) for r in cur.fetchall()]

    def thread(self, remote_jid: str, limit: int = 200) -> list[dict]:
        """Return messages for a conversation, oldest first.

        Reply targets are surfaced via reply_to_id; the renderer joins
        back through a small in-memory by-xmpp-id map keyed off the
        same result set to find the quoted body.
        """
        with self._cursor() as cur:
            cur.execute("""
                SELECT id, remote_jid, incoming, body, sender_jid, timestamp,
                       read, attachment_url, xmpp_id, delivery_state,
                       reactions_json, reply_to_id, corrected_at
                FROM   messages
                WHERE  remote_jid=?
                ORDER  BY timestamp ASC
                LIMIT  ?
            """, (remote_jid, limit))
            return [dict(r) for r in cur.fetchall()]
