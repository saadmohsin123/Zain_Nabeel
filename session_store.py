"""PostgreSQL-backed Messenger session and poll-state storage for Railway."""

from __future__ import annotations

import json
import os
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Callable, Optional, Set, TypeVar

T = TypeVar("T")

_current_sender_id: ContextVar[str] = ContextVar("current_sender_id", default="")
_schema_ready = False

DEFAULT_SESSION = {
    "active": False,
    "awaiting_opt_in": False,
    "step": 0,
    "batch": 0,
    "answers": {},
    "raw_answers": {},
    "search_query": "",
    "qualified": False,
    "last_shared_listing_keys": [],
    "selected_listing_key": "",
    "pending_booking_offer": False,
    "last_prompt": "",
    "messaging_paused": False,
}

SCHEMA_SQL = """
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
"""


def compact(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def get_database_url() -> str:
    return compact(os.getenv("DATABASE_URL")) or compact(os.getenv("POSTGRES_URL"))


def use_postgres_sessions() -> bool:
    return bool(get_database_url())


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


def get_connection():
    import psycopg

    return psycopg.connect(normalize_database_url(get_database_url()))


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready or not use_postgres_sessions():
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()
    _schema_ready = True


def merge_session_defaults(session: dict) -> dict:
    merged = dict(DEFAULT_SESSION)
    if isinstance(session, dict):
        merged.update(session)
    merged.setdefault("answers", {})
    merged.setdefault("raw_answers", {})
    merged.setdefault("last_shared_listing_keys", [])
    return merged


def load_session(sender_id: str) -> dict:
    ensure_schema()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT session_data FROM messenger_sessions WHERE sender_id = %s",
                (sender_id,),
            )
            row = cur.fetchone()
    if not row:
        return merge_session_defaults({})
    payload = row[0]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return merge_session_defaults(payload)


def save_session(sender_id: str, session: dict) -> None:
    ensure_schema()
    payload = json.dumps(session, ensure_ascii=False)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO messenger_sessions (sender_id, session_data, updated_at)
                VALUES (%s, %s::jsonb, NOW())
                ON CONFLICT (sender_id) DO UPDATE SET
                    session_data = EXCLUDED.session_data,
                    updated_at = NOW()
                """,
                (sender_id, payload),
            )
        conn.commit()


def load_seen_message_ids() -> Set[str]:
    ensure_schema()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT message_id
                FROM messenger_seen_messages
                ORDER BY seen_at DESC
                LIMIT 2000
                """
            )
            rows = cur.fetchall()
    return {str(row[0]) for row in rows if row and row[0]}


def save_seen_message_ids(seen: Set[str]) -> None:
    ensure_schema()
    recent = sorted(seen)[-2000:]
    with get_connection() as conn:
        with conn.cursor() as cur:
            for message_id in recent:
                cur.execute(
                    """
                    INSERT INTO messenger_seen_messages (message_id, seen_at)
                    VALUES (%s, NOW())
                    ON CONFLICT (message_id) DO UPDATE SET seen_at = NOW()
                    """,
                    (message_id,),
                )
            cur.execute(
                """
                DELETE FROM messenger_seen_messages
                WHERE message_id NOT IN (
                    SELECT message_id
                    FROM messenger_seen_messages
                    ORDER BY seen_at DESC
                    LIMIT 2000
                )
                """
            )
        conn.commit()


def migrate_json_lead_state(lead_state_path: Path) -> int:
    if not use_postgres_sessions() or not lead_state_path.exists():
        return 0
    try:
        payload = json.loads(lead_state_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    sessions = payload.get("sessions", {}) if isinstance(payload, dict) else {}
    if not isinstance(sessions, dict):
        return 0

    migrated = 0
    for sender_id, session in sessions.items():
        if not compact(sender_id) or not isinstance(session, dict):
            continue
        existing = load_session(sender_id)
        if existing.get("last_prompt") or existing.get("answers"):
            continue
        save_session(sender_id, merge_session_defaults(session))
        migrated += 1
    return migrated


def clear_seen_message_ids() -> None:
    ensure_schema()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM messenger_seen_messages")
        conn.commit()


def session_store_status() -> dict:
    if not use_postgres_sessions():
        return {"enabled": False, "backend": "json_file"}
    ensure_schema()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM messenger_sessions")
                session_count = int(cur.fetchone()[0])
                cur.execute("SELECT COUNT(*) FROM messenger_seen_messages")
                seen_count = int(cur.fetchone()[0])
        return {
            "enabled": True,
            "backend": "postgresql",
            "session_count": session_count,
            "seen_message_count": seen_count,
        }
    except Exception as exc:
        return {"enabled": True, "backend": "postgresql", "error": str(exc)}


def with_sender_context(sender_id: str, fn: Callable[[], T]) -> T:
    token = _current_sender_id.set(sender_id)
    try:
        return fn()
    finally:
        _current_sender_id.reset(token)


def current_sender_id() -> str:
    return _current_sender_id.get()
