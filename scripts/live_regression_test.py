#!/usr/bin/env python3
"""Multi-scenario live production Messenger regression tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests

GRAPH = "https://graph.facebook.com/v20.0"
WEBHOOK = "https://messenger-webhook-bot-production.up.railway.app/webhook"
STATUS = "https://messenger-webhook-bot-production.up.railway.app/debug/status"
PROJECT = Path(__file__).resolve().parents[1]

FAILURES: list[str] = []


def compact(value) -> str:
    return str(value or "").strip()


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


@dataclass
class Scenario:
    name: str
    sender_id: str = ""
    reset: bool = True
    steps: list[tuple[str, Optional[Callable[[str], None]]]] = field(default_factory=list)


def load_env() -> dict:
    proc = subprocess.run(
        ["npx", "--yes", "@railway/cli@latest", "variables", "--json"],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


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


def reset_session(sender_id: str) -> None:
    database_url = load_postgres_url()
    if not database_url or not sender_id:
        return
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://") :]
    import psycopg

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM messenger_sessions WHERE sender_id = %s", (sender_id,))
        conn.commit()


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
    mid = f"mid.live.{uuid.uuid4().hex}"
    sent_at = int(time.time())
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

    for _ in range(6):
        time.sleep(4)
        try:
            msgs = requests.get(
                f"{GRAPH}/{conv_id}/messages",
                params={
                    "access_token": token,
                    "limit": 8,
                    "fields": "from,message,created_time",
                },
                timeout=60,
            ).json().get("data", [])
        except requests.RequestException:
            continue
        for item in msgs:
            if item.get("from", {}).get("id") != page_id:
                continue
            created_ts = None
            try:
                import messenger_automation as bot_mod

                created_ts = bot_mod.parse_graph_time(compact(item.get("created_time")))
            except Exception:
                pass
            if created_ts is not None and created_ts < sent_at - 10:
                continue
            reply = compact(item.get("message"))
            if reply:
                return reply
    return ""


def qual_steps_toronto() -> list[tuple[str, Optional[Callable[[str], None]]]]:
    return [
        ("March 15", None),
        ("1", None),
        ("85000", None),
        ("Software engineer", None),
        ("Canadian citizen", None),
        ("No", None),
        ("6475551234", None),
    ]


def build_scenarios(primary_sender: str) -> list[Scenario]:
    return [
        Scenario(
            name="oshawa_no_inventory",
            sender_id=primary_sender,
            steps=[
                ("Hi", lambda b: check("greeting", "looking for" in b.lower() or "help" in b.lower())),
                (
                    "Looking for a 3 bedroom in Oshawa around 2500",
                    lambda b: (
                        check("opt_in_once", b.lower().count("nabeel's assistant") == 1),
                        check("oshawa_ack", "oshawa" in b.lower()),
                    ),
                ),
                ("yes", lambda b: check("qual_started", "move-in" in b.lower() or "lease" in b.lower())),
                *qual_steps_toronto(),
                (
                    "Do you have anything in Oshawa under 2500?",
                    lambda b: (
                        check("no_wrong_cities", not any(c in b.lower() for c in ("newmarket", "niagara falls"))),
                        check("honest_empty", "nothing active matches" in b.lower() or "looked again" in b.lower()),
                    ),
                ),
            ],
        ),
        Scenario(
            name="toronto_2bed_listings",
            sender_id=primary_sender,
            steps=[
                (
                    "2 bedroom downtown toronto under 2500",
                    lambda b: check("toronto_search_ack", "toronto" in b.lower() or "2 bedroom" in b.lower()),
                ),
                ("yes", None),
                ("April 1", None),
                ("2", None),
                ("90000", None),
                ("Teacher", None),
                ("Permanent Resident", None),
                ("No", None),
                ("4165559876", lambda b: (
                    check("toronto_summary", "what i collected" in b.lower()),
                    check("toronto_listings_shown", "toronto" in b.lower() or "2,150" in b.lower() or "2,280" in b.lower()),
                    check("toronto_not_oshawa_empty", "nothing active matches" not in b.lower()),
                )),
            ],
        ),
        Scenario(
            name="opt_in_decline",
            sender_id=primary_sender,
            steps=[
                ("condo in toronto under 2000", None),
                ("no thanks", lambda b: (
                    check("decline_no_qual", "move-in" not in b.lower()),
                    check("decline_polite", len(b) > 10),
                )),
            ],
        ),
        Scenario(
            name="agent_yes_no_loop",
            sender_id=primary_sender,
            steps=[
                ("looking for condo in toronto", None),
                ("yes", None),
                ("July 1", None),
                ("1", None),
                ("100000", None),
                ("engineer", None),
                ("Non resident", None),
                ("Yes", lambda b: check("agent_yes_advances", "phone" in b.lower() or "agent" not in b.lower())),
            ],
        ),
        Scenario(
            name="qualification_objection",
            sender_id=primary_sender,
            steps=[
                ("looking for 2 bed in toronto under 2500", None),
                ("yes", None),
                ("August 1", None),
                ("1", None),
                ("95000", None),
                ("Why do you need this", lambda b: (
                    check("objection_handled", "work" in b.lower() or "occupation" in b.lower() or "income" in b.lower() or "why" in b.lower() or "question" in b.lower()),
                )),
            ],
        ),
        Scenario(
            name="booking_after_qual",
            sender_id=primary_sender,
            steps=[
                ("2 bedroom in toronto under 2500", None),
                ("yes", None),
                ("May 1", None),
                ("1", None),
                ("80000", None),
                ("Analyst", None),
                ("Canadian citizen", None),
                ("No", None),
                ("6471112222", None),
                ("book a viewing", lambda b: check("booking_calendly", "calendly.com" in b.lower())),
            ],
        ),
        Scenario(
            name="postgres_session_isolation",
            sender_id="",
            reset=False,
            steps=[],
        ),
    ]


def run_isolation_test(env: dict) -> None:
    print("\n=== Scenario: postgres_session_isolation ===")
    sender_a = f"isolation-test-{uuid.uuid4().hex[:8]}"
    sender_b = f"isolation-test-{uuid.uuid4().hex[:8]}"
    reset_session(sender_a)
    reset_session(sender_b)

    page_id = env["META_PAGE_ID"]
    secret = env["META_APP_SECRET"]

    def webhook(sender_id: str, text: str) -> None:
        mid = f"mid.iso.{uuid.uuid4().hex}"
        payload = {
            "object": "page",
            "entry": [{
                "id": page_id,
                "time": int(time.time() * 1000),
                "messaging": [{
                    "sender": {"id": sender_id},
                    "recipient": {"id": page_id},
                    "timestamp": int(time.time() * 1000),
                    "message": {"mid": mid, "text": text},
                }],
            }],
        }
        body = json.dumps(payload).encode()
        requests.post(
            WEBHOOK,
            data=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(secret, body)},
            timeout=120,
        )
        time.sleep(3)

    webhook(sender_a, "looking for 2 bed in toronto under 2500")
    webhook(sender_b, "looking for 3 bed in oshawa under 2500")
    time.sleep(6)

    database_url = load_postgres_url()
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://") :]
    import psycopg

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT session_data FROM messenger_sessions WHERE sender_id = %s", (sender_a,))
            row_a = cur.fetchone()
            cur.execute("SELECT session_data FROM messenger_sessions WHERE sender_id = %s", (sender_b,))
            row_b = cur.fetchone()

    data_a = row_a[0] if row_a else {}
    data_b = row_b[0] if row_b else {}
    if isinstance(data_a, str):
        data_a = json.loads(data_a)
    if isinstance(data_b, str):
        data_b = json.loads(data_b)
    if not isinstance(data_a, dict):
        data_a = {}
    if not isinstance(data_b, dict):
        data_b = dict()

    sq_a = compact(data_a.get("search_query")).lower()
    sq_b = compact(data_b.get("search_query")).lower()
    check("isolation_a_toronto", "toronto" in sq_a)
    check("isolation_b_oshawa", "oshawa" in sq_b)
    check("isolation_queries_differ", sq_a != sq_b)

    reset_session(sender_a)
    reset_session(sender_b)


def run_scenario(env: dict, conv_id: str, scenario: Scenario) -> list[dict]:
    print(f"\n=== Scenario: {scenario.name} ===")
    if scenario.reset and scenario.sender_id:
        reset_session(scenario.sender_id)
        print(f"  (session reset for {scenario.sender_id})")

    transcript: list[dict] = []
    for text, validator in scenario.steps:
        bot = send_turn(env, scenario.sender_id, conv_id, text)
        transcript.append({"user": text, "bot": bot})
        print(f"  YOU: {text}")
        print(f"  BOT: {bot[:500]}\n")
        if validator:
            validator(bot)
    return transcript


def main() -> int:
    env = load_env()
    verify = env.get("META_VERIFY_TOKEN", "")
    primary_sender, conv_id = pick_user(env)
    print(f"Live multi-scenario regression (primary sender {primary_sender})")

    all_transcripts: dict = {}
    for scenario in build_scenarios(primary_sender):
        if scenario.name == "postgres_session_isolation":
            run_isolation_test(env)
            continue
        all_transcripts[scenario.name] = run_scenario(env, conv_id, scenario)

    out = PROJECT / "live_test_transcripts.json"
    out.write_text(json.dumps(all_transcripts, indent=2, ensure_ascii=False), encoding="utf-8")

    status = requests.get(STATUS, params={"token": verify}, timeout=30).json()
    store = status.get("session_store", {})
    check("postgres_enabled", store.get("backend") == "postgresql", str(store))
    check("postgres_has_sessions", int(store.get("session_count", 0)) >= 1, str(store))

    if FAILURES:
        print("\nFAILED:", ", ".join(FAILURES))
        return 1
    print(f"\nALL {len(build_scenarios(primary_sender))} LIVE SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
