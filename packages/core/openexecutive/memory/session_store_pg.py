"""Postgres implementation of the chat-history session store.

Mirrors the SQLite functions in ``session_store`` one-for-one (same names,
same return shapes) so the facade can dispatch on ``DATABASE_URL`` without
any caller noticing. Timestamps are returned as ISO-8601 strings, as the
SQLite rows hold them — but these carry an explicit ``+00:00`` offset where
some older SQLite rows are naive (they were written from ``utcnow()``). The
UI parses both as UTC (see ``lib/relativeTime.ts``).

Scope: every function takes ``client_slug`` — the active client slot, or
``fixture:<name>`` while a demo fixture is loaded, ``None`` for the default
single-company mode (stored as ``''``). Conversations are unique per
``(client_slug, id)`` and **every** lookup, including by id, is scoped: on
SQLite a slot switch swaps the whole database file, so another client's
conversations were physically unreachable; here the scope predicate is what
provides that isolation. Person ids and channel thread ids repeat across
client slots, so nothing may be resolved by ``id`` alone.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from openexecutive.memory import postgres

logger = logging.getLogger(__name__)

# Storage value for the default (no client slot, no fixture) scope. NULL is
# avoided so the UNIQUE (client_slug, id) constraint applies to it too.
DEFAULT_SCOPE = ""

_SUMMARY_SELECT = """
    SELECT c.id AS session_id,
           c.title,
           c.owner_person_id AS caller_person_id,
           c.client_slug,
           c.created_at,
           c.updated_at,
           COUNT(m.id) AS message_count
    FROM conversations c
    LEFT JOIN messages m ON m.conversation_pk = c.pk
"""


def _scope(client_slug: str | None) -> str:
    return DEFAULT_SCOPE if client_slug is None else client_slug


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": row["session_id"],
        "title": row["title"],
        "caller_person_id": row["caller_person_id"],
        "client_slug": row["client_slug"] or None,
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "message_count": int(row["message_count"]),
    }


def create_session(
    session_id: str,
    title: str,
    created_at: str,
    caller_person_id: int | None = None,
    client_slug: str | None = None,
) -> str | None:
    """Idempotent insert; returns the row's effective title.

    On conflict only a NULL owner is late-bound, never overwritten — the same
    contract as the SQLite ``INSERT OR IGNORE`` + owner backfill — and the
    title is replaced only while it is still the "New chat" placeholder, so a
    user-chosen title survives the first chat turn.
    """
    # Local import: the placeholder is the facade's constant and the facade
    # imports this module lazily, so a module-level import would be a cycle.
    from openexecutive.memory.session_store import NEW_CHAT_TITLE

    with postgres.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO conversations
                (id, client_slug, title, owner_person_id, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s::timestamptz, %s::timestamptz)
            ON CONFLICT (client_slug, id) DO UPDATE
                SET owner_person_id = COALESCE(conversations.owner_person_id,
                                               EXCLUDED.owner_person_id),
                    title = CASE WHEN conversations.title = %s
                                 THEN EXCLUDED.title
                                 ELSE conversations.title END
            RETURNING title
            """,
            (
                session_id, _scope(client_slug), title, caller_person_id,
                created_at, created_at, NEW_CHAT_TITLE,
            ),
        ).fetchone()
    return str(row[0]) if row else None


def update_session_title(session_id: str, title: str, client_slug: str | None = None) -> bool:
    with postgres.connection() as conn:
        cur = conn.execute(
            """
            UPDATE conversations SET title = %s, updated_at = now()
            WHERE id = %s AND client_slug = %s
            """,
            (title, session_id, _scope(client_slug)),
        )
        return cur.rowcount > 0


def update_session_timestamp(session_id: str, client_slug: str | None = None) -> None:
    with postgres.connection() as conn:
        conn.execute(
            "UPDATE conversations SET updated_at = now() WHERE id = %s AND client_slug = %s",
            (session_id, _scope(client_slug)),
        )


def save_message(
    session_id: str,
    role: str,
    content: str | list[dict[str, Any]],
    action_chips: str | None = None,
    client_slug: str | None = None,
) -> None:
    """Persist one message. ``action_chips`` arrives JSON-encoded (the chat
    route already serialises it for the SQLite column); it is stored as JSONB.

    The conversation row must already exist in this scope (``create_session``
    always precedes the first ``save_message`` on every call path); a miss is
    logged rather than raised so a persist step can never abort a turn.
    """
    text = content if isinstance(content, str) else str(content)
    chips: Jsonb | None = None
    if action_chips:
        try:
            chips = Jsonb(json.loads(action_chips))
        except (ValueError, TypeError):
            logger.warning("save_message: dropping undecodable action_chips for %s", session_id)
    with postgres.connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO messages (conversation_pk, role, content, action_chips)
            SELECT pk, %s, %s, %s FROM conversations
            WHERE id = %s AND client_slug = %s
            """,
            (role, text, chips, session_id, _scope(client_slug)),
        )
        if cur.rowcount == 0:
            # No conversation with this id in this scope. The only way to get
            # here on a normal path is an active-company switch mid-turn (see
            # the module docstring in session_store): the message is dropped
            # rather than written into another company's history. Loud,
            # because the SSE stream has already reported the turn as fine.
            logger.error(
                "save_message: no conversation %r in scope %r; message DROPPED "
                "(did the active client/fixture change mid-turn?)",
                session_id, _scope(client_slug),
            )


def load_messages(session_id: str, client_slug: str | None = None) -> list[dict[str, Any]]:
    with postgres.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        rows = cur.execute(
            """
            SELECT m.role, m.content, m.action_chips
            FROM messages m
            JOIN conversations c ON c.pk = m.conversation_pk
            WHERE c.id = %s AND c.client_slug = %s
            ORDER BY m.created_at, m.id
            """,
            (session_id, _scope(client_slug)),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        msg: dict[str, Any] = {"role": row["role"], "content": row["content"]}
        chips = row["action_chips"]
        if chips:
            msg["actions"] = chips
        out.append(msg)
    return out


def list_sessions(
    caller_person_id: int,
    client_slug: str | None = None,
) -> list[dict[str, Any]]:
    """Sessions owned by ``caller_person_id`` in the given scope, newest first.

    Rows with a NULL owner are never listed (``owner = ?`` cannot match
    NULL), mirroring the SQLite store; they stay reachable by id in scope.
    """
    with postgres.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        rows = cur.execute(
            _SUMMARY_SELECT
            + """
            WHERE c.owner_person_id = %s AND c.client_slug = %s
            GROUP BY c.pk
            ORDER BY c.updated_at DESC
            """,
            (caller_person_id, _scope(client_slug)),
        ).fetchall()
    return [_summary(row) for row in rows]


def delete_session(session_id: str, client_slug: str | None = None) -> bool:
    """Delete a conversation; its messages go with it (``ON DELETE CASCADE``)."""
    with postgres.connection() as conn:
        cur = conn.execute(
            "DELETE FROM conversations WHERE id = %s AND client_slug = %s",
            (session_id, _scope(client_slug)),
        )
        return cur.rowcount > 0


def get_session_metadata(
    session_id: str, client_slug: str | None = None
) -> dict[str, Any] | None:
    with postgres.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        row = cur.execute(
            _SUMMARY_SELECT + " WHERE c.id = %s AND c.client_slug = %s GROUP BY c.pk",
            (session_id, _scope(client_slug)),
        ).fetchone()
    return _summary(row) if row else None


def purge_scope(client_slug: str | None) -> int:
    """Delete every conversation (and, via cascade, message) in one scope.

    Used by the destructive state paths that used to wipe the SQLite chat
    tables wholesale: fixture reset/unload and client-slot deletion. Returns
    the number of conversations removed.
    """
    with postgres.connection() as conn:
        cur = conn.execute(
            "DELETE FROM conversations WHERE client_slug = %s",
            (_scope(client_slug),),
        )
        return int(cur.rowcount)
