#!/usr/bin/env python3
"""Sync Messenger lead qualification answers to a "Leads" tab in Google Sheets.

Design goals:
- Fully isolated from bot logic: every public function swallows its own errors,
  so a Google/API failure can never affect Messenger replies.
- Writes ONLY to the Leads tab (upsert by Facebook sender ID). It never clears
  or rewrites the listing/workflow tabs.
- No new dependencies: uses plain `requests` against the Sheets REST API with
  OAuth token refresh, so it runs on Railway with the existing requirements.

Enablement (all optional; if credentials are missing the module is a no-op):
- GOOGLE_TOKEN_JSON_CONTENT: raw authorized-user token JSON (preferred on Railway)
- GOOGLE_TOKEN_JSON: path to a token.json file (local use)
- LEADS_SPREADSHEET_ID: target workbook (defaults to the LISTING_DOC_URL workbook)
- LEADS_SHEET_TAB: tab title (default "Leads")
- LEADS_SHEET_ENABLED: set to "0" to disable entirely

CLI:
  python3 leads_sheet_sync.py --backfill --dry-run     # preview rows, writes nothing
  python3 leads_sheet_sync.py --backfill               # upsert all Postgres leads
  python3 leads_sheet_sync.py --backfill --spreadsheet-id <id>   # target a test sheet
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

import requests

import session_store

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
GRAPH_API = "https://graph.facebook.com/v21.0"

DEFAULT_TAB = "Leads"

HEADERS = [
    "Lead Name",
    "Facebook ID",
    "Status",
    "Move-in Date",
    "People on Lease",
    "Adults",
    "Kids",
    "Family Income",
    "Occupation",
    "Resident Status",
    "Working with Agent",
    "Phone Number",
    "Search Preferences",
    "Listings Shared",
    "Last Updated",
]

ANSWER_COLUMNS = [
    ("Move-in Date", "move_in_date"),
    ("People on Lease", "people_on_lease"),
    ("Adults", "adults_in_unit"),
    ("Kids", "kids_in_unit"),
    ("Family Income", "family_gross_income"),
    ("Occupation", "occupation"),
    ("Resident Status", "resident_status"),
    ("Working with Agent", "working_with_agent"),
    ("Phone Number", "phone_number"),
]

_token_lock = threading.Lock()
_access_token: str = ""
_access_token_expiry: float = 0.0
_name_cache: Dict[str, str] = {}
_fingerprints: Dict[str, str] = {}


def compact(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


# ---------------------------------------------------------------------------
# Configuration / enablement
# ---------------------------------------------------------------------------

def load_token_payload() -> dict:
    raw = compact(os.getenv("GOOGLE_TOKEN_JSON_CONTENT"))
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    path = compact(os.getenv("GOOGLE_TOKEN_JSON")) or ".google_token.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def target_spreadsheet_id() -> str:
    explicit = compact(os.getenv("LEADS_SPREADSHEET_ID"))
    if explicit:
        return explicit
    doc_url = compact(os.getenv("LISTING_DOC_URL"))
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", doc_url)
    return match.group(1) if match else ""


def leads_tab_title() -> str:
    return compact(os.getenv("LEADS_SHEET_TAB")) or DEFAULT_TAB


def is_enabled() -> bool:
    if compact(os.getenv("LEADS_SHEET_ENABLED")) == "0":
        return False
    token = load_token_payload()
    return bool(token.get("refresh_token") and target_spreadsheet_id())


# ---------------------------------------------------------------------------
# Google auth (plain requests, no google-api client needed)
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    global _access_token, _access_token_expiry
    with _token_lock:
        if _access_token and time.time() < _access_token_expiry - 60:
            return _access_token
        token = load_token_payload()
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": token.get("client_id", ""),
                "client_secret": token.get("client_secret", ""),
                "refresh_token": token.get("refresh_token", ""),
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        _access_token = payload["access_token"]
        _access_token_expiry = time.time() + int(payload.get("expires_in", 3600))
        return _access_token


def sheets_request(method: str, url: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {get_access_token()}"
    resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp.json() if resp.text else {}


# ---------------------------------------------------------------------------
# Lead row construction
# ---------------------------------------------------------------------------

def lead_status(session: dict) -> str:
    if session.get("qualified"):
        return "Qualified"
    answers = session.get("answers") or {}
    answered = sum(1 for key in answers.values() if compact(key))
    if answered:
        return f"In Progress ({answered} answered)"
    return "New"


def _load_name_map() -> None:
    """Populate the name cache from the page's conversation participants."""
    page_token = compact(os.getenv("META_PAGE_ACCESS_TOKEN"))
    page_id = compact(os.getenv("META_PAGE_ID"))
    if not page_token or not page_id:
        return
    url = f"{GRAPH_API}/{page_id}/conversations"
    params = {"fields": "participants", "limit": "100", "access_token": page_token}
    for _ in range(5):  # up to 5 pages of conversations
        resp = requests.get(url, params=params, timeout=20)
        if not resp.ok:
            return
        payload = resp.json()
        for convo in payload.get("data", []):
            for participant in (convo.get("participants") or {}).get("data", []):
                pid = compact(participant.get("id"))
                if pid and pid != page_id:
                    _name_cache.setdefault(pid, compact(participant.get("name")))
        url = (payload.get("paging") or {}).get("next") or ""
        params = {}
        if not url:
            return


def fetch_lead_name(sender_id: str) -> str:
    if sender_id in _name_cache:
        return _name_cache[sender_id]
    try:
        _load_name_map()
    except Exception:
        pass
    return _name_cache.setdefault(sender_id, "")


def build_lead_row(sender_id: str, session: dict, updated_at: str = "") -> List[str]:
    answers = session.get("answers") or {}
    shared = session.get("last_shared_listing_keys") or []
    row = [
        fetch_lead_name(sender_id),
        compact(sender_id),
        lead_status(session),
    ]
    for _, key in ANSWER_COLUMNS:
        row.append(compact(answers.get(key)))
    row.append(compact(session.get("search_query")))
    row.append(", ".join(compact(k) for k in shared if compact(k)))
    row.append(compact(updated_at))
    return row


def session_fingerprint(session: dict) -> str:
    return json.dumps(
        {
            "answers": session.get("answers") or {},
            "qualified": bool(session.get("qualified")),
            "search_query": compact(session.get("search_query")),
            "shared": session.get("last_shared_listing_keys") or [],
        },
        sort_keys=True,
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Sheet operations (Leads tab only)
# ---------------------------------------------------------------------------

def ensure_leads_tab(spreadsheet_id: str) -> None:
    tab = leads_tab_title()
    meta = sheets_request("GET", f"{SHEETS_API}/{spreadsheet_id}?fields=sheets.properties")
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if tab not in titles:
        sheets_request(
            "POST",
            f"{SHEETS_API}/{spreadsheet_id}:batchUpdate",
            json={"requests": [{"addSheet": {"properties": {"title": tab, "gridProperties": {"frozenRowCount": 1}}}}]},
        )
    existing = sheets_request(
        "GET",
        f"{SHEETS_API}/{spreadsheet_id}/values/{requests.utils.quote(tab)}!A1:{chr(64 + len(HEADERS))}1",
    )
    values = existing.get("values") or []
    if not values or values[0] != HEADERS:
        sheets_request(
            "PUT",
            f"{SHEETS_API}/{spreadsheet_id}/values/{requests.utils.quote(tab)}!A1?valueInputOption=RAW",
            json={"values": [HEADERS]},
        )


def upsert_rows(spreadsheet_id: str, rows: List[List[str]]) -> Tuple[int, int]:
    """Upsert rows keyed by Facebook ID (column B). Returns (updated, appended)."""
    tab = leads_tab_title()
    ensure_leads_tab(spreadsheet_id)
    existing = sheets_request(
        "GET", f"{SHEETS_API}/{spreadsheet_id}/values/{requests.utils.quote(tab)}!B:B"
    )
    id_column = [compact(v[0]) if v else "" for v in existing.get("values") or []]
    updated = appended = 0
    appends: List[List[str]] = []
    for row in rows:
        sender_id = row[1]
        try:
            row_number = id_column.index(sender_id) + 1
        except ValueError:
            appends.append(row)
            continue
        rng = f"{requests.utils.quote(tab)}!A{row_number}:{chr(64 + len(HEADERS))}{row_number}"
        sheets_request(
            "PUT",
            f"{SHEETS_API}/{spreadsheet_id}/values/{rng}?valueInputOption=RAW",
            json={"values": [row]},
        )
        updated += 1
    if appends:
        sheets_request(
            "POST",
            f"{SHEETS_API}/{spreadsheet_id}/values/{requests.utils.quote(tab)}!A:A:append"
            "?valueInputOption=RAW&insertDataOption=INSERT_ROWS",
            json={"values": appends},
        )
        appended = len(appends)
    return updated, appended


# ---------------------------------------------------------------------------
# Realtime hook used by the bot (safe no-op unless configured)
# ---------------------------------------------------------------------------

def schedule_lead_upsert(sender_id: str, session: dict) -> None:
    """Fire-and-forget upsert after a session save. Never raises."""
    try:
        if not is_enabled():
            return
        sender_id = compact(sender_id)
        if not sender_id.isdigit():
            return  # skip synthetic/test sender ids
        fingerprint = session_fingerprint(session)
        if _fingerprints.get(sender_id) == fingerprint:
            return
        _fingerprints[sender_id] = fingerprint
        snapshot = json.loads(json.dumps(session, ensure_ascii=False))

        def worker() -> None:
            try:
                row = build_lead_row(
                    sender_id, snapshot, time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
                )
                upsert_rows(target_spreadsheet_id(), [row])
            except Exception as exc:
                print(f"[leads-sheet] upsert failed for {sender_id}: {exc}", flush=True)

        threading.Thread(target=worker, daemon=True).start()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Backfill / demo CLI
# ---------------------------------------------------------------------------

def load_all_sessions() -> List[Tuple[str, dict, str]]:
    session_store.ensure_schema()
    with session_store.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sender_id, session_data, updated_at FROM messenger_sessions ORDER BY updated_at DESC"
            )
            results = []
            for sender_id, payload, updated_at in cur.fetchall():
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError:
                        payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                results.append(
                    (
                        compact(sender_id),
                        session_store.merge_session_defaults(payload),
                        updated_at.strftime("%Y-%m-%d %H:%M:%S UTC") if updated_at else "",
                    )
                )
            return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Messenger leads into the Leads sheet tab.")
    parser.add_argument("--backfill", action="store_true", help="Sync all Postgres sessions")
    parser.add_argument("--dry-run", action="store_true", help="Print rows without writing to any sheet")
    parser.add_argument("--spreadsheet-id", default="", help="Override target spreadsheet (e.g. a test copy)")
    args = parser.parse_args()

    if not args.backfill:
        parser.print_help()
        return 1

    if args.spreadsheet_id:
        os.environ["LEADS_SPREADSHEET_ID"] = args.spreadsheet_id

    sessions = load_all_sessions()
    # Real Facebook PSIDs are numeric; skip synthetic/test sessions.
    sessions = [(sid, sess, updated) for sid, sess, updated in sessions if sid.isdigit()]
    rows = [build_lead_row(sid, sess, updated) for sid, sess, updated in sessions]
    print(f"Loaded {len(rows)} lead(s) from Postgres")

    if args.dry_run:
        for row in rows:
            print(json.dumps(dict(zip(HEADERS, row)), ensure_ascii=False, indent=2))
        return 0

    if not is_enabled():
        print("Leads sheet sync is not configured (missing Google token or spreadsheet id).")
        return 1

    updated, appended = upsert_rows(target_spreadsheet_id(), rows)
    print(f"Done: {updated} updated, {appended} appended in tab '{leads_tab_title()}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
