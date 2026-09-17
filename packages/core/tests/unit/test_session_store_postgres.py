"""Postgres backend for chat history (``memory/session_store_pg`` + migrations).

Opt-in: set ``TEST_DATABASE_URL`` to a scratch Postgres database, e.g.::

    TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/openexecutive_test \\
        uv run pytest tests/unit/test_session_store_postgres.py

Every test starts from empty ``conversations`` / ``messages`` tables. The
suite skips cleanly when the variable is unset (CI runs the SQLite path).
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set; Postgres tests are opt-in"
)


@pytest.fixture()
def pg(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the store at the scratch database with the schema applied and
    the chat tables emptied; close the pool afterwards so the next test (or
    the SQLite-only tests) never reuse it."""
    from openexecutive.memory import postgres
    from openexecutive.memory.migrate import run_migrations

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    postgres.close_pool()
    run_migrations()
    with postgres.connection() as conn:
        conn.execute("TRUNCATE messages, conversations")
    # Default single-company scope regardless of the developer's local state.
    from openexecutive.memory import session_store

    monkeypatch.setattr(session_store, "current_client_scope", lambda: None)
    try:
        yield
    finally:
        postgres.close_pool()


OWNER = 1
OTHER = 2


def test_migrations_are_idempotent(pg: None) -> None:
    from openexecutive.memory.migrate import pending_migrations, run_migrations

    assert run_migrations() == []
    assert pending_migrations() == []


def test_migrations_from_scratch_and_check_is_read_only(pg: None) -> None:
    from openexecutive.memory import postgres
    from openexecutive.memory.migrate import pending_migrations, run_migrations

    with postgres.connection() as conn:
        conn.execute("DROP TABLE IF EXISTS messages, conversations, schema_migrations")
    assert pending_migrations() == ["0001_chat_history"]
    with postgres.connection() as conn:
        assert conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0] is None
    assert run_migrations() == ["0001_chat_history"]
    assert pending_migrations() == []
    # The advisory lock was released: a second run in the same process must
    # not block.
    assert run_migrations() == []


def test_create_list_and_metadata(pg: None) -> None:
    from openexecutive.memory import session_store as store

    store.create_session("s1", "What is our strategy?", "2024-01-01T00:00:00", caller_person_id=OWNER)
    sessions = store.list_sessions(OWNER)
    assert [s["session_id"] for s in sessions] == ["s1"]
    assert sessions[0]["title"] == "What is our strategy?"
    assert sessions[0]["message_count"] == 0
    assert sessions[0]["created_at"].startswith("2024-01-01T00:00:00")
    assert store.list_sessions(OTHER) == []

    meta = store.get_session_metadata("s1")
    assert meta is not None
    assert meta["caller_person_id"] == OWNER
    assert store.get_session_metadata("nope") is None


def test_create_is_idempotent_and_late_binds_owner(pg: None) -> None:
    from openexecutive.memory import session_store as store

    store.create_session("s1", "first", "2024-01-01T00:00:00", caller_person_id=None)
    assert store.list_sessions(OWNER) == []
    # Turn 2 resolves the caller: the NULL owner is bound, title untouched.
    store.create_session("s1", "second", "2024-02-02T00:00:00", caller_person_id=OWNER)
    store.create_session("s1", "third", "2024-03-03T00:00:00", caller_person_id=OTHER)
    sessions = store.list_sessions(OWNER)
    assert len(sessions) == 1
    assert sessions[0]["title"] == "first"
    assert store.list_sessions(OTHER) == []


def test_create_returns_effective_title_and_protects_a_user_title(pg: None) -> None:
    """create_session upgrades the "New chat" placeholder but never a title
    the user chose, and reports which one the row ended up with."""
    from openexecutive.memory import session_store as store

    # Pre-created empty, then the first turn supplies a placeholder title.
    assert store.create_session("s1", store.NEW_CHAT_TITLE, "2024-01-01T00:00:00",
                                caller_person_id=OWNER) == store.NEW_CHAT_TITLE
    assert store.create_session("s1", "hello there", "2024-01-01T00:00:00",
                                caller_person_id=OWNER) == "hello there"

    # Pre-created with a real title: the first turn must not overwrite it.
    assert store.create_session("s2", "Board prep", "2024-01-01T00:00:00",
                                caller_person_id=OWNER) == "Board prep"
    assert store.create_session("s2", "hello there", "2024-01-01T00:00:00",
                                caller_person_id=OWNER) == "Board prep"
    meta = store.get_session_metadata("s2")
    assert meta is not None and meta["title"] == "Board prep"


def test_rename_reports_whether_a_row_matched(pg: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.memory import session_store as store

    store.create_session("s1", "t", "2024-01-01T00:00:00", caller_person_id=OWNER)
    assert store.update_session_title("s1", "New title") is True
    assert store.update_session_title("missing", "x") is False
    monkeypatch.setattr(store, "current_client_scope", lambda: "acme")
    assert store.update_session_title("s1", "cross-scope") is False


def test_messages_round_trip_with_action_chips(pg: None) -> None:
    from openexecutive.memory import session_store as store

    store.create_session("s1", "Hello", "2024-01-01T00:00:00", caller_person_id=OWNER)
    chips = [{"type": "action_taken", "tool": "send_dm", "summary": "DM sent"}]
    store.save_message("s1", "user", "Hello there")
    store.save_message("s1", "assistant", "Hi!", action_chips=json.dumps(chips))
    store.save_message("s1", "user", [{"type": "text", "text": "block"}])

    msgs = store.load_messages("s1")
    assert msgs[0] == {"role": "user", "content": "Hello there"}
    assert msgs[1] == {"role": "assistant", "content": "Hi!", "actions": chips}
    assert msgs[2]["role"] == "user"
    assert store.list_sessions(OWNER)[0]["message_count"] == 3
    assert store.load_messages("missing") == []


def test_rename_and_touch_update_updated_at(pg: None) -> None:
    from openexecutive.memory import session_store as store

    store.create_session("s1", "Old", "2024-01-01T00:00:00", caller_person_id=OWNER)
    before = store.get_session_metadata("s1")
    assert before is not None
    store.update_session_title("s1", "New title")
    after = store.get_session_metadata("s1")
    assert after is not None
    assert after["title"] == "New title"
    assert after["updated_at"] > before["updated_at"]

    store.update_session_timestamp("s1")
    touched = store.get_session_metadata("s1")
    assert touched is not None
    assert touched["updated_at"] >= after["updated_at"]


def test_delete_cascades_messages(pg: None) -> None:
    from openexecutive.memory import postgres
    from openexecutive.memory import session_store as store

    store.create_session("s1", "t", "2024-01-01T00:00:00", caller_person_id=OWNER)
    store.save_message("s1", "user", "q")
    store.save_message("s1", "assistant", "a")
    assert store.delete_session("s1") is True
    assert store.delete_session("s1") is False
    assert store.get_session_metadata("s1") is None
    with postgres.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_every_operation_is_scoped_by_client_slug(pg: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Person ids and channel thread ids repeat across client slots, so a
    conversation must be invisible — by id too — from any other scope."""
    from openexecutive.memory import session_store as store

    store.create_session("default-1", "d", "2024-01-01T00:00:00", caller_person_id=OWNER)
    store.save_message("default-1", "user", "default scope")
    monkeypatch.setattr(store, "current_client_scope", lambda: "acme")
    store.create_session("acme-1", "a", "2024-01-02T00:00:00", caller_person_id=OWNER)
    assert [s["session_id"] for s in store.list_sessions(OWNER)] == ["acme-1"]
    # By-id access from the wrong scope: nothing readable, nothing writable.
    assert store.get_session_metadata("default-1") is None
    assert store.load_messages("default-1") == []
    store.update_session_title("default-1", "hijacked")
    store.save_message("default-1", "user", "leak?")
    assert store.delete_session("default-1") is False
    monkeypatch.setattr(store, "current_client_scope", lambda: None)
    assert [s["session_id"] for s in store.list_sessions(OWNER)] == ["default-1"]
    assert store.get_session_metadata("acme-1") is None
    meta = store.get_session_metadata("default-1")
    assert meta is not None and meta["title"] == "d"
    assert store.load_messages("default-1") == [{"role": "user", "content": "default scope"}]


def test_same_channel_id_is_a_separate_conversation_per_scope(
    pg: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Telegram thread id under client A and under client B never shares
    history — the uniqueness key is (client_slug, id)."""
    from openexecutive.memory import session_store as store

    monkeypatch.setattr(store, "current_client_scope", lambda: "acme")
    store.create_session("telegram:42", "A", "2024-01-01T00:00:00", caller_person_id=OWNER)
    store.save_message("telegram:42", "user", "acme secret")
    monkeypatch.setattr(store, "current_client_scope", lambda: "globex")
    store.create_session("telegram:42", "B", "2024-01-02T00:00:00", caller_person_id=OWNER)
    assert store.load_messages("telegram:42") == []
    store.save_message("telegram:42", "user", "globex hello")
    assert store.load_messages("telegram:42") == [{"role": "user", "content": "globex hello"}]
    assert store.purge_scope("acme") == 1
    assert store.load_messages("telegram:42") == [{"role": "user", "content": "globex hello"}]


def test_purge_scope_only_touches_that_scope(pg: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.memory import session_store as store

    store.create_session("default-1", "d", "2024-01-01T00:00:00", caller_person_id=OWNER)
    monkeypatch.setattr(store, "current_client_scope", lambda: store.fixture_scope("demo"))
    store.create_session("demo-1", "x", "2024-01-02T00:00:00", caller_person_id=OWNER)
    store.save_message("demo-1", "user", "hi")

    assert store.fixture_scope("demo") == "fixture:demo"
    assert store.purge_scope("fixture:demo") == 1
    assert store.get_session_metadata("demo-1") is None
    monkeypatch.setattr(store, "current_client_scope", lambda: None)
    assert store.get_session_metadata("default-1") is not None
    assert store.purge_scope(None) == 1
    assert store.purge_scope(None) == 0


def test_sessions_routes_crud_against_postgres(pg: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """create → rename → messages → delete through the HTTP layer."""
    from openexecutive.api.routes import chat as chat_route
    from openexecutive.api.routes import sessions as sessions_route
    from openexecutive.memory import session_store as store

    monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda _req: OWNER)
    monkeypatch.setattr(sessions_route, "_resolve_caller_person_id", lambda _req: OWNER)
    app = FastAPI()
    app.include_router(sessions_route.router)
    client = TestClient(app)

    created = client.post("/sessions", json={})
    assert created.status_code == 201, created.text
    sid = created.json()["session_id"]
    assert created.json()["title"] == "New chat"

    renamed = client.patch(f"/sessions/{sid}", json={"title": "  Q3   plan "})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Q3 plan"

    store.save_message(sid, "user", "hello")
    store.save_message(sid, "assistant", "hi there")
    listed = client.get("/sessions").json()
    assert [s["session_id"] for s in listed] == [sid]
    assert listed[0]["message_count"] == 2
    assert client.get(f"/sessions/{sid}/messages").json() == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]

    assert client.delete(f"/sessions/{sid}").status_code == 204
    assert client.get(f"/sessions/{sid}").status_code == 404
    assert client.get("/sessions").json() == []


def test_chat_turn_persists_to_postgres(pg: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A streamed /chat turn lands both messages in Postgres and survives a
    process 'restart' (in-memory session cache cleared) via load_messages."""
    from openexecutive.api.routes import chat as chat_route
    from openexecutive.knowledge import retriever
    from openexecutive.memory.company_profile import CompanyProfile
    from openexecutive.onboarding import profile_builder
    from openexecutive.orchestrator import executive as exec_mod
    from openexecutive.utils import session_title as title_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())
    monkeypatch.setattr(retriever, "retrieve", lambda **_kw: "")
    monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda _req: OWNER)

    async def _title(*_a: Any, **_k: Any) -> str:
        return "Generated title"

    monkeypatch.setattr(title_mod, "generate_session_title", _title)

    class _Executive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            yield "Hello "
            yield "world"

    monkeypatch.setattr(exec_mod, "Executive", _Executive)
    chat_route._sessions.clear()

    app = FastAPI()
    app.include_router(chat_route.router)
    client = TestClient(app)
    resp = client.post("/chat", json={"message": "hi exec"})
    assert resp.status_code == 200
    body = resp.text
    session_id = next(
        json.loads(line[len("data: "):])["session_id"]
        for line in body.splitlines()
        if line.startswith("data: ") and '"type": "done"' in line
    )

    from openexecutive.memory import session_store as store

    chat_route._sessions.clear()  # simulate a redeploy: nothing cached in-process
    assert store.load_messages(session_id) == [
        {"role": "user", "content": "hi exec"},
        {"role": "assistant", "content": "Hello world"},
    ]
    # Another rostered user cannot continue (or read) this conversation by id.
    monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda _req: OTHER)
    denied = client.post("/chat", json={"message": "summarise", "session_id": session_id})
    assert denied.status_code == 404
    monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda _req: OWNER)
    listed = store.list_sessions(OWNER)
    assert listed[0]["session_id"] == session_id
    assert listed[0]["title"] == "Generated title"
    # Second turn on the reloaded session appends, not restarts.
    resp = client.post("/chat", json={"message": "again", "session_id": session_id})
    assert resp.status_code == 200
    _ = resp.text
    assert [m["content"] for m in store.load_messages(session_id)] == [
        "hi exec", "Hello world", "again", "Hello world",
    ]
