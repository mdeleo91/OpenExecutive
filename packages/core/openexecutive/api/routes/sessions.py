from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from openexecutive.api.models import SessionCreateRequest, SessionRenameRequest, SessionSummary
from openexecutive.api.routes.chat import (
    _resolve_caller_person_id,
    forget_cached_session,
    may_access_session,
)
from openexecutive.memory.session_store import (
    NEW_CHAT_TITLE,
    create_session,
    delete_session,
    get_session_metadata,
    list_sessions,
    load_messages,
    update_session_title,
)

router = APIRouter()


def _load_owned(session_id: str, request: Request) -> dict[str, Any]:
    """Fetch a session's metadata, enforcing access.

    A conversation is only visible to the Person who owns it, and a
    pre-roster row with no owner only to the principal (see
    ``chat.may_access_session``). Every refusal is a 404, never a 403, so the
    id space never leaks.
    """
    meta = get_session_metadata(session_id)
    if meta is None:
        # Nothing behind the id any more; make sure no live Session lingers
        # under it either (same rule as the chat route's gate).
        forget_cached_session(session_id)
        raise HTTPException(status_code=404, detail="Session not found")
    caller_person_id = _resolve_caller_person_id(request)
    if not may_access_session(meta, caller_person_id, request):
        raise HTTPException(status_code=404, detail="Session not found")
    return meta


@router.get("/sessions", response_model=list[SessionSummary])
def get_sessions(request: Request) -> list[SessionSummary]:
    caller_person_id = _resolve_caller_person_id(request)
    if caller_person_id is None:
        # Either a signed-in user whose email isn't in the roster, or no
        # principal is configured yet (fresh install). Either way they
        # have no chats to see — return empty rather than leaking the
        # legacy NULL-owner rows.
        return []
    return [SessionSummary(**s) for s in list_sessions(caller_person_id)]


@router.post("/sessions", response_model=SessionSummary, status_code=status.HTTP_201_CREATED)
def create_session_route(body: SessionCreateRequest, request: Request) -> SessionSummary:
    """Create an empty conversation up front.

    The chat route also creates the row lazily on the first turn, so the
    UI may either call this first and pass the id as ``session_id`` or just
    start chatting. The title defaults to "New chat" until the first user
    message (and its generated topic title) replaces it.
    """
    caller_person_id = _resolve_caller_person_id(request)
    if caller_person_id is None:
        raise HTTPException(
            status_code=403,
            detail="No matching person for the signed-in user; chats cannot be created.",
        )
    session_id = str(uuid.uuid4())
    title = (body.title or "").strip() or NEW_CHAT_TITLE
    create_session(
        session_id,
        title,
        datetime.now(UTC).isoformat(),
        caller_person_id=caller_person_id,
    )
    meta = get_session_metadata(session_id)
    if meta is None:  # pragma: no cover - the insert just succeeded
        raise HTTPException(status_code=500, detail="Session was not persisted")
    return SessionSummary(**meta)


@router.get("/sessions/{session_id}", response_model=SessionSummary)
def get_session(session_id: str, request: Request) -> SessionSummary:
    return SessionSummary(**_load_owned(session_id, request))


@router.get("/sessions/{session_id}/messages")
def get_session_messages(session_id: str, request: Request) -> list[dict]:
    _load_owned(session_id, request)
    return load_messages(session_id)


@router.patch("/sessions/{session_id}", response_model=SessionSummary)
def rename_session_route(
    session_id: str, body: SessionRenameRequest, request: Request
) -> SessionSummary:
    _load_owned(session_id, request)
    update_session_title(session_id, body.title)
    meta = get_session_metadata(session_id)
    if meta is None:  # pragma: no cover - deleted between the two reads
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionSummary(**meta)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session_route(session_id: str, request: Request) -> Response:
    _load_owned(session_id, request)
    if not delete_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    # Deleted means gone: drop the live Session too, so its history cannot be
    # served from memory if the id is presented again.
    forget_cached_session(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
