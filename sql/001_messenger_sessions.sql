-- Railway PostgreSQL schema for Durham New Homes Messenger bot.
-- Run once in the Railway Postgres query tab, or let the app auto-create on startup.

CREATE TABLE IF NOT EXISTS messenger_sessions (
    sender_id TEXT PRIMARY KEY,
    session_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS messenger_seen_messages (
    message_id TEXT PRIMARY KEY,
    seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS messenger_sessions_updated_at_idx
    ON messenger_sessions (updated_at DESC);

CREATE INDEX IF NOT EXISTS messenger_seen_messages_seen_at_idx
    ON messenger_seen_messages (seen_at DESC);
