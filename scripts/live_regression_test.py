#!/usr/bin/env python3
"""Live production Messenger regression test via signed webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import requests

GRAPH = "https://graph.facebook.com/v20.0"
WEBHOOK = "https://messenger-webhook-bot-production.up.railway.app/webhook"
STATUS = "https://messenger-webhook-bot-production.up.railway.app/debug/status"
PROJECT = Path(__file__).resolve().parents[1]

FLOW = [
    ("Hi", {"greeting": True}),
    (
        "Looking for a 3 bedroom in Oshawa around 2500",
        {"opt_in": True, "no_double_intro": True},
    ),
    ("yes", {"qualification": True}),
    ("March 15", {}),
    ("1", {}),
    ("85000", {}),
    ("Software engineer", {}),
    ("Canadian citizen", {}),
    ("No", {}),
    ("6475551234", {"qualified_summary": True}),
    ("Hi", {"no_listing_redump": True}),
    (
        "Do you have anything in Oshawa under 2500?",
        {"no_wrong_cities": True},
    ),
]


def load_env() -> dict:
    proc = subprocess.run(
        ["npx", "--yes", "@railway/cli@latest", "variables", "--json"],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def pick_user(env: dict) -> tuple[str, str]:
    page_id = env["META_PAGE_ID"]
    token = env["META_PAGE_ACCESS_TOKEN"]
    resp = requests.get(
        f"{GRAPH}/{page_id}/conversations",
        params={"access_token": token, "limit": 15, "fields": "participants"},
        timeout=30,
    )
    resp.raise_for_status()
    for conv in resp.json().get("data", []):
        participants = (conv.get("participants") or {}).get("data", [])
        users = [p.get("id") for p in participants if p.get("id") != page_id]
        if users:
            return users[0], conv.get("id", "")
    raise RuntimeError("No user conversation found")


def send_turn(env: dict, sender_id: str, conv_id: str, text: str) -> str:
    page_id = env["META_PAGE_ID"]
    secret = env["META_APP_SECRET"]
    token = env["META_PAGE_ACCESS_TOKEN"]
    mid = f"mid.regression.{uuid.uuid4().hex}"
    payload = {
        "object": "page",
        "entry": [
            {
                "id": page_id,
                "time": int(time.time() * 1000),
                "messaging": [
                    {
                        "sender": {"id": sender_id},
                        "recipient": {"id": page_id},
                        "timestamp": int(time.time() * 1000),
                        "message": {"mid": mid, "text": text},
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()
    resp = requests.post(
        WEBHOOK,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sign(secret, body),
        },
        timeout=120,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Webhook failed {resp.status_code}: {resp.text[:200]}")
    time.sleep(5)
    msgs = requests.get(
        f"{GRAPH}/{conv_id}/messages",
        params={"access_token": token, "limit": 4, "fields": "from,message"},
        timeout=30,
    ).json().get("data", [])
    for item in msgs:
        if item.get("from", {}).get("id") == page_id:
            return compact(item.get("message"))
    return ""


def compact(value) -> str:
    return str(value or "").strip()


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


FAILURES: list[str] = []


def load_postgres_url() -> str:
    proc = subprocess.run(
        ["npx", "--yes", "@railway/cli@latest", "variables", "--service", "Postgres", "--json"],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    return compact(payload.get("DATABASE_PUBLIC_URL")) or compact(payload.get("DATABASE_URL"))


def reset_live_session(sender_id: str) -> None:
    database_url = load_postgres_url()
    if not database_url:
        return
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://") :]
    import psycopg

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM messenger_sessions WHERE sender_id = %s", (sender_id,))
        conn.commit()


def main() -> int:
    env = load_env()
    verify = env.get("META_VERIFY_TOKEN", "")
    sender_id, conv_id = pick_user(env)
    reset_live_session(sender_id)
    print(f"Live regression on sender {sender_id} (session reset)\n")

    transcript = []
    for text, expectations in FLOW:
        bot = send_turn(env, sender_id, conv_id, text)
        transcript.append({"user": text, "bot": bot})
        print(f"YOU: {text}")
        print(f"BOT: {bot[:700]}\n")

        if expectations.get("greeting"):
            check("greeting", "looking for" in bot.lower() or "help" in bot.lower())
        if expectations.get("opt_in"):
            check("opt_in_once", bot.lower().count("nabeel's assistant") == 1)
            check("search_ack", "oshawa" in bot.lower() or "3 bedroom" in bot.lower())
        if expectations.get("no_double_intro"):
            check("no_double_intro", "that's great" not in bot.lower()[:30].replace("got it", ""))
        if expectations.get("qualification"):
            check("qualification_started", "move-in" in bot.lower() or "lease" in bot.lower())
        if expectations.get("qualified_summary"):
            check("qualification_summary", "what i collected" in bot.lower())
            check("income_not_single_digit", "income: 1" not in bot.lower())
            check("no_wrong_city_post_qual", "newmarket" not in bot.lower() and "niagara" not in bot.lower())
        if expectations.get("no_listing_redump"):
            check("hi_no_redump", "what i collected" not in bot.lower())
            check(
                "hi_conversational",
                any(
                    phrase in bot.lower()
                    for phrase in (
                        "refine",
                        "viewing",
                        "still here",
                        "tell me what you're looking for",
                        "tell me what you’re looking for",
                    )
                ),
            )
        if expectations.get("no_wrong_cities"):
            wrong = any(city in bot.lower() for city in ("newmarket", "niagara falls", "toronto w08"))
            check("oshawa_search_no_wrong_cities", not wrong)
            check(
                "oshawa_search_honest_empty_or_local",
                wrong is False and ("nothing active matches" in bot.lower() or "oshawa" in bot.lower() or "looked again" in bot.lower()),
            )

    out = PROJECT / "live_test_transcript.json"
    out.write_text(json.dumps(transcript, indent=2, ensure_ascii=False), encoding="utf-8")

    status = requests.get(STATUS, params={"token": verify}, timeout=30).json()
    store = status.get("session_store", {})
    check("postgres_enabled", store.get("backend") == "postgresql", str(store))
    check("postgres_sessions_saved", int(store.get("session_count", 0)) >= 1, str(store))

    if FAILURES:
        print("\nFAILED:", ", ".join(FAILURES))
        return 1
    print("\nALL LIVE REGRESSION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
