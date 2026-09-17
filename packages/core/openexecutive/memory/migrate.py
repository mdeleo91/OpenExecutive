"""Forward-only SQL migrations for the Postgres chat-history schema.

Migrations are plain ``.sql`` files in ``memory/migrations/``, applied in
filename order and recorded in ``schema_migrations`` so each one runs
exactly once. Every file runs inside its own transaction (Postgres DDL is
transactional), and the whole run holds a session-level advisory lock so two
processes — a Fly ``release_command`` racing a booting machine, say — can
never apply the same file twice.

Entry points:

- ``run_migrations()`` — called from the API lifespan when ``DATABASE_URL``
  is set, and by ``openexecutive migrate`` / ``python -m
  openexecutive.memory.migrate`` (the Fly release command).
- ``pending_migrations()`` — what a run would apply, for ``--check`` style
  inspection.

Never hand-edit a live database: add a new numbered file instead.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any

from openexecutive.memory import postgres

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
# Arbitrary but fixed 64-bit key for pg_advisory_lock; only this runner uses it.
_ADVISORY_LOCK_KEY = 0x4F45_4D49_4752  # "OEMIGR"
# How long to wait for another process's migration run before giving up. The
# lock is taken with pg_try_advisory_lock rather than the blocking form so a
# stuck holder surfaces as an error instead of hanging the API lifespan (which
# awaits run_migrations in a thread) or a Fly release_command forever.
_LOCK_WAIT_TIMEOUT_S = 60.0
_LOCK_POLL_INTERVAL_S = 1.0


def _migration_files() -> list[Path]:
    return sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.is_file())


def _applied_versions(conn: Any) -> set[str]:
    """Versions recorded in ``schema_migrations``; empty when the table does
    not exist yet (read-only — safe for ``--check``)."""
    exists = conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0]
    if exists is None:
        return set()
    cur = conn.execute("SELECT version FROM schema_migrations")
    return {row[0] for row in cur.fetchall()}


def _ensure_ledger(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def pending_migrations() -> list[str]:
    """Versions (file stems) that :func:`run_migrations` would apply now."""
    with postgres.connection() as conn:
        applied = _applied_versions(conn)
    return [p.stem for p in _migration_files() if p.stem not in applied]


def _acquire_lock(conn: Any) -> None:
    """Take the runner's advisory lock, or raise after ``_LOCK_WAIT_TIMEOUT_S``."""
    deadline = time.monotonic() + _LOCK_WAIT_TIMEOUT_S
    while True:
        got = conn.execute(
            "SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,)
        ).fetchone()[0]
        conn.commit()
        if got:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "another process has held the chat-history migration lock for "
                f"over {_LOCK_WAIT_TIMEOUT_S:.0f}s; not proceeding"
            )
        logger.info("waiting for the chat-history migration lock…")
        time.sleep(_LOCK_POLL_INTERVAL_S)


def _release_lock(conn: Any) -> None:
    """Best-effort unlock that never masks the error that brought us here.

    A failed migration can leave the transaction aborted (every later
    statement then errors) or the connection dead. Either way the original
    exception is the useful one, so failures here are logged, not raised —
    and the rollback runs first so the unlock itself can execute.
    """
    try:
        conn.rollback()
    except Exception:
        logger.warning("migration: rollback before unlock failed", exc_info=True)
    try:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
        conn.commit()
    except Exception:
        # The lock is session-scoped, so it is released when this pooled
        # connection is eventually closed.
        logger.warning("migration: advisory unlock failed", exc_info=True)


def run_migrations() -> list[str]:
    """Apply every unapplied migration; return the versions applied (in order)."""
    applied_now: list[str] = []
    with postgres.connection() as conn:
        _acquire_lock(conn)
        try:
            _ensure_ledger(conn)
            applied = _applied_versions(conn)
            conn.commit()
            for path in _migration_files():
                version = path.stem
                if version in applied:
                    continue
                sql = path.read_text(encoding="utf-8")
                try:
                    conn.execute(sql)  # type: ignore[arg-type]
                    conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s)",
                        (version,),
                    )
                    conn.commit()
                except Exception:
                    logger.exception("migration %s failed; rolling back", version)
                    raise
                applied_now.append(version)
                logger.info("applied migration %s", version)
        finally:
            _release_lock(conn)
    return applied_now


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python -m openexecutive.memory.migrate [--check]``.

    Exit 0 with a note when ``DATABASE_URL`` is unset so the Fly release
    command is a no-op before the database has been attached.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in args
    if not postgres.is_configured():
        print(
            f"{postgres.ENV_DATABASE_URL} is not set — chat history stays on SQLite; "
            "nothing to migrate."
        )
        return 0
    try:
        if check_only:
            pending = pending_migrations()
            if pending:
                print("pending migrations: " + ", ".join(pending))
                return 1
            print("schema is up to date")
            return 0
        applied = run_migrations()
    except Exception as exc:  # pragma: no cover - exercised manually
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        postgres.close_pool()
    if applied:
        print("applied migrations: " + ", ".join(applied))
    else:
        print("schema is up to date")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
