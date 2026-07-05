#!/usr/bin/env python3
"""Delete a Messenger sender session from PostgreSQL (Railway production or local DATABASE_URL)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import session_store  # noqa: E402


def load_database_url() -> str:
    url = session_store.get_database_url()
    if url:
        return session_store.normalize_database_url(url)
    proc = subprocess.run(
        ["npx", "--yes", "@railway/cli@latest", "variables", "--service", "Postgres", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Could not load Postgres URL from Railway: {proc.stderr.strip()}")
    payload = json.loads(proc.stdout)
    url = (payload.get("DATABASE_PUBLIC_URL") or payload.get("DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError("Postgres DATABASE_URL not found")
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description="Clear one Messenger user session from Postgres.")
    parser.add_argument(
        "sender_id",
        nargs="?",
        default="36173247835655833",
        help="Facebook sender ID (default: Zain Naeem Nini)",
    )
    parser.add_argument(
        "--also-seen",
        action="store_true",
        help="Also clear seen-message dedup rows for this sender (usually not needed)",
    )
    args = parser.parse_args()
    sender_id = args.sender_id.strip()
    if not sender_id:
        print("sender_id required", file=sys.stderr)
        return 1

    url = load_database_url()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    import psycopg

    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM messenger_sessions WHERE sender_id = %s", (sender_id,))
            existed = cur.fetchone() is not None
            cur.execute("DELETE FROM messenger_sessions WHERE sender_id = %s", (sender_id,))
            deleted = cur.rowcount
            seen_deleted = 0
            if args.also_seen:
                cur.execute(
                    "DELETE FROM messenger_seen_messages WHERE message_id LIKE %s",
                    (f"%{sender_id}%",),
                )
                seen_deleted = cur.rowcount
        conn.commit()

    print(f"Cleared session for sender {sender_id} (existed={existed}, deleted={deleted})")
    if args.also_seen:
        print(f"Cleared {seen_deleted} seen-message rows (best-effort)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
