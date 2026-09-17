"""POST /sessions, PATCH /sessions/{id}, and per-owner access on the by-id routes.

Runs on the SQLite backend (the default). The Postgres twin lives in
``test_session_store_postgres.py``.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import sessions as sessions_route
from openexecutive.memory import episodic, session_store
from openexecutive.people import store as people_store

ALEX = {"x-caller-email": "alex@example.com"}
SABIN = {"x-caller-email": "sabin@example.com"}


def _patch_chat_deps(monkeypatch: pytest.MonkeyPatch, *, title: str | None = None) -> None:
    """Point the chat route at a fake Executive / profile / title generator so
    a turn can be streamed without touching the Anthropic API."""
    from openexecutive.api.routes import chat as chat_route
    from openexecutive.knowledge import retriever
    from openexecutive.memory.company_profile import CompanyProfile
    from openexecutive.onboarding import profile_builder
    from openexecutive.orchestrator import executive as exec_mod
    from openexecutive.utils import session_title as title_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(profile_builder, "load_or_create_profile", lambda: CompanyProfile())
    monkeypatch.setattr(retriever, "retrieve", lambda **_kw: "")
    monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda _req: 1)

    async def _title(*_a: Any, **_k: Any) -> str | None:
        return title

    monkeypatch.setattr(title_mod, "generate_session_title", _title)

    class _Executive:
        _THINKING = exec_mod.Executive._THINKING

        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def stream_chat(self, **_kwargs: Any) -> AsyncIterator[str]:
            yield "Hello world"

    monkeypatch.setattr(exec_mod, "Executive", _Executive)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    episodic.initialize_db(db_path)
    people_store.initialize_db()
    people_store.upsert_person(full_name="Alex", is_principal=True, email="alex@example.com")
    people_store.upsert_person(full_name="Sabin", email="sabin@example.com")

    app = FastAPI()
    app.include_router(sessions_route.router)
    return TestClient(app)


def test_create_session_defaults_title_and_owner(client: TestClient) -> None:
    resp = client.post("/sessions", json={}, headers=ALEX)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "New chat"
    assert body["message_count"] == 0
    assert body["created_at"] == body["updated_at"]

    assert [s["session_id"] for s in client.get("/sessions", headers=ALEX).json()] == [body["session_id"]]
    assert client.get("/sessions", headers=SABIN).json() == []


def test_create_session_with_title(client: TestClient) -> None:
    resp = client.post("/sessions", json={"title": "  Board prep  "}, headers=ALEX)
    assert resp.status_code == 201
    assert resp.json()["title"] == "Board prep"


def test_create_session_requires_a_rostered_caller(client: TestClient) -> None:
    resp = client.post("/sessions", json={}, headers={"x-caller-email": "stranger@example.com"})
    assert resp.status_code == 403


def test_rename_session(client: TestClient) -> None:
    sid = client.post("/sessions", json={}, headers=ALEX).json()["session_id"]
    resp = client.patch(f"/sessions/{sid}", json={"title": " Q3 \n plan "}, headers=ALEX)
    assert resp.status_code == 200
    assert resp.json()["title"] == "Q3 plan"
    assert client.get(f"/sessions/{sid}", headers=ALEX).json()["title"] == "Q3 plan"


@pytest.mark.parametrize("payload", [{}, {"title": ""}, {"title": "   "}, {"title": "x" * 201}])
def test_rename_rejects_bad_titles(client: TestClient, payload: dict) -> None:
    sid = client.post("/sessions", json={}, headers=ALEX).json()["session_id"]
    assert client.patch(f"/sessions/{sid}", json=payload, headers=ALEX).status_code == 422


def test_rename_missing_session_is_404(client: TestClient) -> None:
    assert client.patch("/sessions/nope", json={"title": "x"}, headers=ALEX).status_code == 404


def test_by_id_routes_hide_other_users_sessions(client: TestClient) -> None:
    sid = client.post("/sessions", json={}, headers=ALEX).json()["session_id"]
    session_store.save_message(sid, "user", "private")

    assert client.get(f"/sessions/{sid}", headers=SABIN).status_code == 404
    assert client.get(f"/sessions/{sid}/messages", headers=SABIN).status_code == 404
    assert client.patch(f"/sessions/{sid}", json={"title": "hijack"}, headers=SABIN).status_code == 404
    assert client.delete(f"/sessions/{sid}", headers=SABIN).status_code == 404

    # The owner still sees everything, and the title was not changed.
    assert client.get(f"/sessions/{sid}", headers=ALEX).json()["title"] == "New chat"
    assert client.get(f"/sessions/{sid}/messages", headers=ALEX).json() == [
        {"role": "user", "content": "private"}
    ]
    assert client.delete(f"/sessions/{sid}", headers=ALEX).status_code == 204


def test_unowned_session_is_the_principals_alone(client: TestClient) -> None:
    """A pre-roster row (no owner) can hold the principal's own history, so no
    other rostered user may read, rename or delete it."""
    session_store.create_session("legacy", "old", "2024-01-01T00:00:00")
    stranger = {"x-caller-email": "stranger@example.com"}
    for headers in (SABIN, stranger):
        assert client.get("/sessions/legacy", headers=headers).status_code == 404
        assert client.get("/sessions/legacy/messages", headers=headers).status_code == 404
        assert client.patch("/sessions/legacy", json={"t": 1}, headers=headers).status_code in (404, 422)
        assert client.delete("/sessions/legacy", headers=headers).status_code == 404

    assert client.get("/sessions/legacy", headers=ALEX).status_code == 200
    assert client.patch("/sessions/legacy", json={"title": "renamed"}, headers=ALEX).status_code == 200
    assert client.delete("/sessions/legacy", headers=ALEX).status_code == 204


def test_non_principal_cannot_claim_an_unowned_session_via_chat(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mutation gate must not be bypassable by claiming ownership first:
    continuing an unowned conversation on /chat would late-bind the caller as
    its owner, which would then unlock rename and delete."""
    from openexecutive.api.routes import chat as chat_route

    session_store.create_session("legacy", "old", "2024-01-01T00:00:00")
    session_store.save_message("legacy", "user", "CEO private: acquiring Foo")

    _patch_chat_deps(monkeypatch)
    sabin_id = people_store.find_person_by_email("sabin@example.com")
    assert sabin_id is not None
    monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda _req: sabin_id.id)
    chat_route._sessions.clear()
    app = FastAPI()
    app.include_router(chat_route.router)

    resp = TestClient(app).post(
        "/chat", json={"message": "summarise", "session_id": "legacy"}, headers=SABIN
    )
    assert resp.status_code == 404
    # Ownership unchanged, history untouched, and mutation still refused.
    meta = session_store.get_session_metadata("legacy")
    assert meta is not None
    assert meta["caller_person_id"] is None
    assert [m["content"] for m in session_store.load_messages("legacy")] == [
        "CEO private: acquiring Foo"
    ]
    assert client.delete("/sessions/legacy", headers=SABIN).status_code == 404


def test_chat_route_refuses_another_users_session_id(client: TestClient) -> None:
    """POST /chat with someone else's session_id must not load their history."""
    from openexecutive.api.routes import chat as chat_route

    sid = client.post("/sessions", json={}, headers=ALEX).json()["session_id"]
    session_store.save_message(sid, "user", "private")
    app = FastAPI()
    app.include_router(chat_route.router)
    chat_client = TestClient(app)
    resp = chat_client.post("/chat", json={"message": "summarise", "session_id": sid}, headers=SABIN)
    assert resp.status_code == 404
    # Nothing was appended to the victim's conversation.
    assert session_store.load_messages(sid) == [{"role": "user", "content": "private"}]


def test_first_chat_turn_keeps_a_user_chosen_title(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A title set via POST /sessions (or a later rename) must survive the
    first chat turn — the auto-title only replaces the placeholder."""
    from openexecutive.api.routes import chat as chat_route

    _patch_chat_deps(monkeypatch, title="Generated title")
    sid = client.post("/sessions", json={"title": "Board prep"}, headers=ALEX).json()["session_id"]

    app = FastAPI()
    app.include_router(chat_route.router)
    chat_route._sessions.clear()
    resp = TestClient(app).post("/chat", json={"message": "hello", "session_id": sid})
    assert resp.status_code == 200
    _ = resp.text

    meta = session_store.get_session_metadata(sid)
    assert meta is not None
    assert meta["title"] == "Board prep"
    assert [m["content"] for m in session_store.load_messages(sid)] == ["hello", "Hello world"]


def test_first_chat_turn_names_a_placeholder_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conversation still called "New chat" does get the generated title."""
    from openexecutive.api.routes import chat as chat_route

    _patch_chat_deps(monkeypatch, title="Generated title")
    sid = client.post("/sessions", json={}, headers=ALEX).json()["session_id"]

    app = FastAPI()
    app.include_router(chat_route.router)
    chat_route._sessions.clear()
    assert TestClient(app).post("/chat", json={"message": "hello", "session_id": sid}).text
    meta = session_store.get_session_metadata(sid)
    assert meta is not None
    assert meta["title"] == "Generated title"


def test_deleting_a_session_also_drops_its_cached_history(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session id is not a capability: after a delete, the next caller to
    present that id must not be served the deleted conversation's history
    out of the in-process cache."""
    from openexecutive.api.routes import chat as chat_route

    _patch_chat_deps(monkeypatch)
    chat_route._sessions.clear()
    app = FastAPI()
    app.include_router(chat_route.router)
    chat_client = TestClient(app)

    # Alex holds a live conversation.
    body = chat_client.post("/chat", json={"message": "alex secret"}).text
    sid = next(
        json.loads(line[len("data: "):])["session_id"]
        for line in body.splitlines()
        if line.startswith("data: ") and '"type": "done"' in line
    )
    assert chat_route._sessions  # cached in-process
    assert client.delete(f"/sessions/{sid}", headers=ALEX).status_code == 204
    assert chat_route._sessions == {}

    # Sabin presents the same id: the row is gone, so this starts fresh.
    monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda _req: 2)
    assert chat_client.post("/chat", json={"message": "hi", "session_id": sid}).status_code == 200
    assert [m["content"] for m in session_store.load_messages(sid)] == ["hi", "Hello world"]


def test_stale_cache_entry_is_dropped_when_the_row_is_gone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same protection when the row disappears behind the route's back (a
    scope purge, or a create that failed) rather than via DELETE."""
    from openexecutive.api.routes import chat as chat_route

    _patch_chat_deps(monkeypatch)
    chat_route._sessions.clear()
    app = FastAPI()
    app.include_router(chat_route.router)
    chat_client = TestClient(app)

    body = chat_client.post("/chat", json={"message": "alex secret"}).text
    sid = next(
        json.loads(line[len("data: "):])["session_id"]
        for line in body.splitlines()
        if line.startswith("data: ") and '"type": "done"' in line
    )
    session_store.delete_session(sid)  # straight to the store, cache untouched
    assert chat_route._sessions

    monkeypatch.setattr(chat_route, "_resolve_caller_person_id", lambda _req: 2)
    assert chat_client.post("/chat", json={"message": "hi", "session_id": sid}).status_code == 200
    # Fresh conversation: none of Alex's turns came back.
    assert [m["content"] for m in session_store.load_messages(sid)] == ["hi", "Hello world"]


def test_create_session_normalises_title_whitespace(client: TestClient) -> None:
    resp = client.post("/sessions", json={"title": "Board\n\tprep   notes"}, headers=ALEX)
    assert resp.status_code == 201
    assert resp.json()["title"] == "Board prep notes"
    blank = client.post("/sessions", json={"title": " \n "}, headers=ALEX)
    assert blank.status_code == 201
    assert blank.json()["title"] == "New chat"
