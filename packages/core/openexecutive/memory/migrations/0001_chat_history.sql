-- Chat history: one row per conversation, one row per message.
--
-- `conversations.id` is the caller-facing session id and is TEXT rather than
-- UUID on purpose: the web UI mints uuid4 strings, but the channel
-- integrations key their threads by stable external ids
-- (`telegram:<chat_id>`, `discord:thread:<id>`, `slack:<chan>:<ts>`,
-- `email:<thread>`), and every one of them shares this store.
--
-- `client_slug` scopes a conversation to a client-company slot (or a demo
-- fixture as `fixture:<name>`); '' is the default single-company scope. The
-- SQLite store swapped whole database files per client, so this column is
-- what keeps one client's chats separate from another's: the uniqueness key
-- is (client_slug, id), so the same Telegram thread — or the same web session
-- id — under two clients is two independent conversations, and every lookup
-- is scoped. `pk` is the surrogate the messages table references.
--
-- `owner_person_id` is the Person row (people table, SQLite) of the caller
-- who started the chat; NULL for pre-roster sessions. It is not a foreign
-- key because people live in a different database, and it is only meaningful
-- together with `client_slug` (each client slot has its own people table).

CREATE TABLE IF NOT EXISTS conversations (
    pk              BIGSERIAL PRIMARY KEY,
    id              TEXT NOT NULL,
    client_slug     TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT 'New chat',
    owner_person_id INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT conversations_scope_id_key UNIQUE (client_slug, id)
);

-- Sidebar list: "this owner's conversations in this scope, newest first".
CREATE INDEX IF NOT EXISTS conversations_scope_owner_updated_idx
    ON conversations (client_slug, owner_person_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL PRIMARY KEY,
    conversation_pk BIGINT NOT NULL REFERENCES conversations (pk) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content         TEXT NOT NULL,
    -- Inline action chips for assistant turns (see chat_messages.action_chips).
    action_chips    JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- History loads are always "all messages of one conversation, oldest first".
-- `id` breaks ties for the user/assistant pair persisted in the same instant.
CREATE INDEX IF NOT EXISTS messages_conversation_created_idx
    ON messages (conversation_pk, created_at, id);
