"""Chat-history session store.

Two backends behind one set of functions:

- **Postgres** when ``DATABASE_URL`` is set (Fly Managed Postgres in
  production, ``fly proxy`` or a local server in development) — see
  ``session_store_pg`` and ``memory/migrations/``.
- **SQLite** otherwise: the ``sessions`` / ``chat_messages`` tables in
  ``episodic_memory.db``, created by ``episodic.initialize_db``. This is the
  zero-config path for local development and the unit-test suite.

The dispatch is per call (``postgres.is_configured()`` reads the env var), so
tests can flip backends with ``monkeypatch.setenv``. The ``db_path`` keyword
is only meaningful for SQLite and is ignored by the Postgres backend.

The scope is likewise resolved per call, which assumes the active company
does not change *within* one chat turn. If an operator switches client slot
or loads a fixture between a turn's ``create_session`` and its
``save_message``, the halves target different scopes: the write finds no
conversation and is dropped with an error log (see
:func:`session_store_pg.save_message`) rather than landing in the wrong
company's history.

Scope (Postgres only): conversations are keyed by ``(client_slug, id)`` where
the slug is the active client slot (or ``fixture:<name>`` during a demo), see
:func:`current_client_scope`. Every operation — including by-id reads and
writes — is scoped, because person ids and channel thread ids repeat across
client slots. The SQLite backend needs no scope because client slots and
fixtures swap the entire database file.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.memory import postgres
from openexecutive.memory.episodic import DB_PATH, _get_conn

logger = logging.getLogger(__name__)

# Placeholder title until the first user turn (or its Haiku-generated topic
# title) lands. Shared by the API route and the chat route so the sidebar
# never shows an empty label.
NEW_CHAT_TITLE = "New chat"
FIXTURE_SCOPE_PREFIX = "fixture:"


def fixture_scope(fixture_name: str) -> str:
    """Scope key for conversations created while demo fixture ``fixture_name``
    is loaded. The one place the ``fixture:<name>`` shape is spelled out."""
    return f"{FIXTURE_SCOPE_PREFIX}{fixture_name}"


def _use_postgres() -> bool:
    return postgres.is_configured()


def current_client_scope() -> str | None:
    """The scope new conversations belong to right now.

    ``"<client-slug>"`` while a client slot is active, ``"fixture:<name>"``
    while a demo fixture is loaded, ``None`` in plain single-company mode.

    This is an isolation control, so it fails closed: if the scope cannot be
    resolved the error propagates (the calling request fails) rather than
    silently reading or writing the default scope. In practice the settings
    object is already loaded at boot and the sentinel readers swallow their
    own I/O errors as "not active", so this only trips on a broken install.
    """
    from openexecutive.cli.fixture_loader import get_fixture_status
    from openexecutive.clients.slots import get_active_client
    from openexecutive.config import get_settings

    settings = get_settings()
    active_client = get_active_client(settings)
    if active_client:
        return active_client
    fixture = get_fixture_status(settings).get("active_fixture")
    if fixture:
        return fixture_scope(fixture)
    return None


# --------------------------------------------------------------------------
# Public API — each function dispatches to Postgres or SQLite.
# --------------------------------------------------------------------------


def create_session(
    session_id: str,
    title: str,
    created_at: str,
    caller_person_id: int | None = None,
    db_path: Path = DB_PATH,
) -> str | None:
    """Create the conversation row if absent; return its *effective* title.

    Idempotent. On an existing row the owner is late-bound only when it was
    NULL, and the title is replaced only when it is still the
    :data:`NEW_CHAT_TITLE` placeholder — so a title the user chose (via
    ``POST /sessions`` or a rename) survives the first chat turn. The
    returned title lets the caller tell "the title is still the one I just
    supplied" (safe to auto-rename later) from "the user named this chat".
    """
    if _use_postgres():
        from openexecutive.memory import session_store_pg

        return session_store_pg.create_session(
            session_id,
            title,
            created_at,
            caller_person_id=caller_person_id,
            client_slug=current_client_scope(),
        )
    with _get_conn(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, title, created_at, updated_at, caller_person_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, title, created_at, created_at, caller_person_id),
        )
        # Bind the owner late if turn 1 landed without a resolved caller
        # (e.g. principal not yet seeded, or DB lookup transiently failed)
        # and turn 2 succeeded in resolving one. INSERT OR IGNORE would
        # otherwise leave the row orphaned with caller_person_id = NULL,
        # invisible to its real owner forever.
        if caller_person_id is not None:
            conn.execute(
                "UPDATE sessions SET caller_person_id = ? "
                "WHERE session_id = ? AND caller_person_id IS NULL",
                (caller_person_id, session_id),
            )
        # Row pre-created empty by POST /sessions and this is the first real
        # turn: upgrade the placeholder to the caller's title. Never touches
        # a title the user chose (no-op when `title` is the placeholder).
        conn.execute(
            "UPDATE sessions SET title = ? WHERE session_id = ? AND title = ?",
            (title, session_id, NEW_CHAT_TITLE),
        )
        row = conn.execute(
            "SELECT title FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    return str(row["title"]) if row else None


def update_session_title(session_id: str, title: str, db_path: Path = DB_PATH) -> bool:
    """Rename a conversation. False when no row matched in the current scope."""
    if _use_postgres():
        from openexecutive.memory import session_store_pg

        return session_store_pg.update_session_title(
            session_id, title, client_slug=current_client_scope()
        )
    with _get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE sessions SET title = ? WHERE session_id = ?", (title, session_id)
        )
        return cur.rowcount > 0


def update_session_timestamp(session_id: str, db_path: Path = DB_PATH) -> None:
    if _use_postgres():
        from openexecutive.memory import session_store_pg

        session_store_pg.update_session_timestamp(session_id, client_slug=current_client_scope())
        return
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (now, session_id))


def save_message(
    session_id: str,
    role: str,
    content: str | list[dict[str, Any]],
    db_path: Path = DB_PATH,
    action_chips: str | None = None,
) -> None:
    """Persist one chat message. ``action_chips`` is a JSON-encoded list of the
    assistant turn's action-chip dicts (or None), so reopening a saved session
    restores the ✓ tool-action pills instead of bare prose."""
    if _use_postgres():
        from openexecutive.memory import session_store_pg

        session_store_pg.save_message(
            session_id, role, content,
            action_chips=action_chips, client_slug=current_client_scope(),
        )
        return
    text = content if isinstance(content, str) else str(content)
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, created_at, action_chips) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, role, text, now, action_chips),
        )


def load_messages(session_id: str, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    if _use_postgres():
        from openexecutive.memory import session_store_pg

        return session_store_pg.load_messages(session_id, client_slug=current_client_scope())
    if not db_path.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT role, content, action_chips FROM chat_messages "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        msg: dict[str, Any] = {"role": row["role"], "content": row["content"]}
        raw = row["action_chips"]
        if raw:
            try:
                chips = json.loads(raw)
            except (ValueError, TypeError):
                chips = None
            if chips:
                msg["actions"] = chips
        out.append(msg)
    return out


def list_sessions(
    caller_person_id: int,
    db_path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """List sessions owned by `caller_person_id`, newest first.

    Legacy rows with caller_person_id IS NULL (created before this column
    existed) are excluded — the comparison `NULL = ?` never matches in
    SQLite. They remain reachable by direct session_id URL.
    """
    if _use_postgres():
        from openexecutive.memory import session_store_pg

        return session_store_pg.list_sessions(
            caller_person_id, client_slug=current_client_scope()
        )
    if not db_path.exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.session_id, s.title, s.created_at, s.updated_at,
                   s.caller_person_id,
                   COUNT(m.id) AS message_count
            FROM sessions s
            LEFT JOIN chat_messages m ON m.session_id = s.session_id
            WHERE s.caller_person_id = ?
            GROUP BY s.session_id
            ORDER BY s.updated_at DESC
            """,
            (caller_person_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_session(session_id: str, db_path: Path = DB_PATH) -> bool:
    if _use_postgres():
        from openexecutive.memory import session_store_pg

        return session_store_pg.delete_session(session_id, client_slug=current_client_scope())
    if not db_path.exists():
        return False
    with _get_conn(db_path) as conn:
        conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
        cur = conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        return cur.rowcount > 0


def get_session_metadata(session_id: str, db_path: Path = DB_PATH) -> dict[str, Any] | None:
    """Summary row for one session (``caller_person_id`` included so routes
    can enforce ownership), or ``None`` when it does not exist."""
    if _use_postgres():
        from openexecutive.memory import session_store_pg

        return session_store_pg.get_session_metadata(
            session_id, client_slug=current_client_scope()
        )
    if not db_path.exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT s.session_id, s.title, s.created_at, s.updated_at,
                   s.caller_person_id,
                   COUNT(m.id) AS message_count
            FROM sessions s
            LEFT JOIN chat_messages m ON m.session_id = s.session_id
            WHERE s.session_id = ?
            GROUP BY s.session_id
            """,
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def purge_scope(client_slug: str | None) -> int:
    """Postgres only: drop every conversation in one client/fixture scope.

    The SQLite backend has nothing to do here — the callers (fixture reset /
    unload, client-slot delete) already wipe or swap the SQLite file. Returns
    the number of conversations removed (0 when on SQLite). Never raises:
    these run inside destructive-op paths that must complete regardless.
    """
    if not _use_postgres():
        return 0
    try:
        from openexecutive.memory import session_store_pg

        removed = session_store_pg.purge_scope(client_slug)
    except Exception:
        logger.exception("session_store: purge_scope(%r) failed", client_slug)
        return 0
    if removed:
        logger.info("session_store: purged %d conversation(s) in scope %r", removed, client_slug)
    return removed
