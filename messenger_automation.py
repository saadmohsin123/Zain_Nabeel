#!/usr/bin/env python3
"""
Messenger automation service for Phase 2.

What it does:
- Verifies the Meta webhook challenge
- Receives Messenger webhook events
- Matches incoming text against current marketplace drafts
- Replies via the Messenger Send API

What it does not do:
- It does not create new outbound conversations on its own.
- It does not bypass Meta policy or review requirements.

Required env vars:
- META_VERIFY_TOKEN
- one of:
  - META_PAGE_ACCESS_TOKEN
  - META_USER_ACCESS_TOKEN

Optional env vars:
- META_APP_SECRET            # for webhook signature verification
- META_PAGE_ID                # for Conversations API / diagnostics
- MARKETPLACE_DRAFTS_JSON     # default: marketplace_drafts.json
- LISTING_DOC_URL             # optional URL to send when user asks for the doc
- META_USER_ACCESS_TOKEN      # optional fallback to derive a page token for META_PAGE_ID
- OPENAI_API_KEY              # optional conversational answer generation
- OPENAI_MODEL                # default: gpt-4.1-mini
- CALENDLY_URL                # optional booking link for calls/showings
- AGENT_NAME                  # default: Nabeel
- POLL_CONVERSATIONS_SECONDS  # optional fallback when Meta does not deliver webhooks
- POLL_STATE_FILE             # default: messenger_poll_state.json
- MESSENGER_PORT              # default: 8000
"""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import hmac
import io
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests


GRAPH_BASE = "https://graph.facebook.com/v20.0"
OPENAI_RESPONSES_API = "https://api.openai.com/v1/responses"
DEFAULT_SHEET_GID = "0"
_DRAFT_CACHE: dict = {"source": "", "fetched_at": 0.0, "drafts": []}
QUERY_STOPWORDS = {
    "a",
    "an",
    "any",
    "are",
    "available",
    "book",
    "can",
    "could",
    "for",
    "find",
    "help",
    "i",
    "in",
    "is",
    "listings",
    "listing",
    "me",
    "please",
    "properties",
    "property",
    "rentals",
    "search",
    "show",
    "tell",
    "the",
    "there",
    "to",
    "want",
    "what",
    "you",
}
QUALIFICATION_QUESTIONS = [
    ("move_in_date", "Perfect. First, what’s your expected move-in date?"),
    ("people_on_lease", "How many people will be on the lease?"),
    ("adults_in_unit", "How many adults will be living in the unit?"),
    ("kids_in_unit", "How many kids will be living in the unit?"),
    (
        "family_gross_income",
        "What’s your total family gross income? Please do not include cash income.",
    ),
    ("occupation", "What do you do for work?"),
    ("resident_status", "What is your resident status in Canada?"),
    ("working_with_agent", "Are you currently working with an agent?"),
    ("phone_number", "And what’s the best phone number to reach you on?"),
]


def must_env(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if value in (None, ""):
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def optional_env(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    return "" if value is None else value


def resolve_page_access_token(user_access_token: str, page_id: str) -> str:
    resp = requests.get(
        f"{GRAPH_BASE}/me/accounts",
        params={"access_token": user_access_token},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    for page in payload.get("data", []):
        if compact(page.get("id")) == page_id:
            page_token = compact(page.get("access_token"))
            if page_token:
                return page_token
    raise RuntimeError(f"Could not derive a page access token for META_PAGE_ID={page_id}")


def load_drafts(path: Path) -> List[dict]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else []


def parse_spreadsheet_id(listing_doc_url: str) -> str:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", listing_doc_url)
    return match.group(1) if match else ""


def parse_sheet_gid(listing_doc_url: str) -> str:
    parsed = urlparse(listing_doc_url)
    params = parse_qs(parsed.query)
    gid = compact(params.get("gid", [""])[0])
    if gid:
        return gid
    fragment_params = parse_qs(parsed.fragment)
    gid = compact(fragment_params.get("gid", [""])[0])
    return gid or DEFAULT_SHEET_GID


def build_sheet_csv_url(listing_doc_url: str) -> str:
    spreadsheet_id = parse_spreadsheet_id(listing_doc_url)
    if not spreadsheet_id:
        return ""
    gid = parse_sheet_gid(listing_doc_url)
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={gid}"


def fetch_sheet_drafts(csv_url: str) -> List[dict]:
    resp = requests.get(csv_url, timeout=30)
    resp.raise_for_status()
    rows = csv.DictReader(io.StringIO(resp.text))
    drafts: List[dict] = []
    for row in rows:
        normalized = {compact(key): value for key, value in row.items() if compact(key)}
        if compact(normalized.get("ListingKey")):
            drafts.append(normalized)
    return drafts


def compact(value) -> str:
    if value in (None, "", []):
        return ""
    if isinstance(value, list):
        return ", ".join(compact(v) for v in value if compact(v))
    return str(value).strip()


def parse_graph_time(value: str) -> Optional[int]:
    value = compact(value)
    if not value:
        return None
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%S%z"))
    except Exception:
        return None


def tokenize(text: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t and t not in QUERY_STOPWORDS]


def draft_text(draft: dict) -> str:
    bits = [
        compact(draft.get("ListingKey")),
        compact(draft.get("MarketplaceTitle")),
        compact(draft.get("Address")),
        compact(draft.get("MarketplaceDescription")),
        compact(draft.get("Amenities")),
        compact(draft.get("HeatingDetails")),
        compact(draft.get("CoolingDetails")),
        compact(draft.get("GarageDetails")),
        compact(draft.get("LaundryDetails")),
    ]
    return " ".join(bits).lower()


def normalize_status(value: str) -> str:
    return compact(value).strip().lower()


def is_rental_listing(draft: dict) -> bool:
    transaction_type = normalize_status(compact(draft.get("TransactionType")))
    allowed = {
        "for lease",
        "for rent",
        "lease",
        "rent",
    }
    return transaction_type in allowed


def is_listing_ready(draft: dict) -> bool:
    marketplace_status = normalize_status(compact(draft.get("MarketplaceStatus")))
    lifecycle_status = normalize_status(compact(draft.get("ListingLifecycleStatus")))
    allowed_marketplace = {
        "posted",
        "active",
        "approved",
    }
    blocked_lifecycle = {
        "expired",
        "terminated",
        "closed",
        "suspended",
        "leased",
    }
    if marketplace_status not in allowed_marketplace:
        return False
    if lifecycle_status and lifecycle_status in blocked_lifecycle:
        return False
    return is_rental_listing(draft)


def customer_visible_drafts(drafts: List[dict]) -> List[dict]:
    return [draft for draft in drafts if is_listing_ready(draft)]


def rank_drafts(query: str, drafts: List[dict], limit: int = 3) -> List[dict]:
    q_tokens = tokenize(query)
    if not q_tokens:
        return []

    candidate_drafts = customer_visible_drafts(drafts)
    if not candidate_drafts:
        return []

    scored: List[Tuple[int, dict]] = []
    for draft in candidate_drafts:
        haystack = draft_text(draft)
        score = 0
        for token in q_tokens:
            if token in haystack:
                score += 3
            if token in compact(draft.get("ListingKey")).lower():
                score += 5
            if token in compact(draft.get("Address")).lower():
                score += 4
        if score:
            scored.append((score, draft))

    scored.sort(key=lambda item: (-item[0], compact(item[1].get("MarketplacePriceDisplay"))))
    return [draft for _, draft in scored[:limit]]


def summarize_draft(draft: dict) -> str:
    title = compact(draft.get("MarketplaceTitle")) or compact(draft.get("Address")) or "Listing"
    price = compact(draft.get("MarketplacePriceDisplay")) or compact(draft.get("MarketplacePrice"))
    tx = compact(draft.get("TransactionType"))
    city = compact(draft.get("City"))
    parts = [title]
    if price:
        parts.append(f"Price: {price}")
    if tx:
        parts.append(f"Transaction: {tx}")
    if city:
        parts.append(f"City: {city}")
    return " | ".join(parts)


def listing_context(draft: dict) -> dict:
    fields = [
        "ListingKey",
        "Address",
        "MarketplaceTitle",
        "MarketplacePriceDisplay",
        "TransactionType",
        "PropertyType",
        "City",
        "BedroomsTotal",
        "BathroomsTotal",
        "LivingAreaRange",
        "LivingAreaUnits",
        "Basement",
        "PetsAllowed",
        "HeatingDetails",
        "CoolingDetails",
        "GarageDetails",
        "LaundryDetails",
        "Amenities",
        "ParkingFeatures",
        "ParkingSpaces",
        "MarketplaceDescription",
    ]
    return {field: draft.get(field) for field in fields if draft.get(field) not in (None, "", [])}


def looks_like_booking_request(query: str) -> bool:
    q = query.lower()
    phrases = [
        "book",
        "booking",
        "schedule",
        "meeting",
        "viewing",
        "showing",
        "call",
        "available tomorrow",
        "available today",
        "availability",
    ]
    return any(phrase in q for phrase in phrases)


def shortlist_for_booking(matches: List[dict]) -> List[dict]:
    return [draft for draft in matches if is_listing_ready(draft)]


def looks_like_affirmative(query: str) -> bool:
    q = query.lower().strip()
    affirmatives = {
        "yes",
        "y",
        "yeah",
        "yup",
        "sure",
        "okay",
        "ok",
        "please do",
        "sounds good",
        "why not",
        "interested",
    }
    return q in affirmatives or q.startswith("yes ") or q.startswith("yup ")


def wants_listing_help(query: str) -> bool:
    q = query.lower()
    phrases = [
        "looking for",
        "show me",
        "send me listings",
        "send listings",
        "properties",
        "rent",
        "lease",
        "apartment",
        "condo",
        "house",
        "bedroom",
    ]
    return any(phrase in q for phrase in phrases)


def load_lead_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_lead_state(path: Path, payload: dict):
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def get_lead_session(state: dict, sender_id: str) -> dict:
    sessions = state.setdefault("sessions", {})
    return sessions.setdefault(
        sender_id,
        {
            "active": False,
            "awaiting_opt_in": False,
            "step": 0,
            "answers": {},
        },
    )


def format_lead_summary(answers: dict) -> str:
    labels = {
        "move_in_date": "Move-in date",
        "people_on_lease": "No. of people on lease",
        "adults_in_unit": "No. of adults in the unit",
        "kids_in_unit": "No. of kids in the unit",
        "family_gross_income": "Total family gross income",
        "occupation": "Occupation",
        "resident_status": "Resident status",
        "working_with_agent": "Working with an agent",
        "phone_number": "Phone number",
    }
    lines = []
    for key, _ in QUALIFICATION_QUESTIONS:
        value = compact(answers.get(key))
        if value:
            lines.append(f"{labels[key]}: {value}")
    return "\n".join(lines)


def begin_qualification_flow(agent_name: str) -> str:
    return (
        f"That’s great! Allow me to introduce myself, I’m {agent_name}, a local realtor here in Toronto. "
        "If you’re open to it, I can actually make your home search a lot easier as I have access to all the rentals on the market "
        "(including ones not on Facebook), and the best part is there’s no cost to you at all. The landlord pays my fee!\n\n"
        "Would you like me to send you a list of the best active listings in your area and price range? "
        "You can pick your favourites and we can go from there."
    )


def should_start_qualification(query: str, calendly_url: str) -> bool:
    q = query.lower()
    if looks_like_booking_request(query):
        return True
    if wants_listing_help(query):
        return True
    if "send me" in q and "listing" in q:
        return True
    if "help me" in q and ("find" in q or "search" in q):
        return True
    if calendly_url and ("call" in q or "viewing" in q or "meeting" in q):
        return True
    return False


def maybe_handle_qualification(
    sender_id: str,
    query: str,
    lead_state_path: Path,
    agent_name: str,
    calendly_url: str,
) -> Optional[str]:
    state = load_lead_state(lead_state_path)
    session = get_lead_session(state, sender_id)

    if session.get("awaiting_opt_in"):
        if looks_like_affirmative(query):
            session["awaiting_opt_in"] = False
            session["active"] = True
            session["step"] = 0
            session["answers"] = {}
            save_lead_state(lead_state_path, state)
            return (
                "Perfect. Before we proceed ahead please let me know the following details.\n\n"
                + QUALIFICATION_QUESTIONS[0][1]
            )

        session["awaiting_opt_in"] = False
        save_lead_state(lead_state_path, state)
        return (
            "No problem. Whenever you’re ready, just tell me your preferred area, budget, and unit type, "
            "and I’ll help from there."
        )

    if session.get("active"):
        step = int(session.get("step", 0))
        answers = session.setdefault("answers", {})
        if step < len(QUALIFICATION_QUESTIONS):
            key, _ = QUALIFICATION_QUESTIONS[step]
            answers[key] = compact(query)
            step += 1
            session["step"] = step
            if step < len(QUALIFICATION_QUESTIONS):
                save_lead_state(lead_state_path, state)
                return QUALIFICATION_QUESTIONS[step][1]

            session["active"] = False
            session["completed_at"] = int(time.time())
            save_lead_state(lead_state_path, state)
            summary = format_lead_summary(answers)
            closing = "Perfect, I’ve got everything I need. I’ll use this to narrow down the best active listings for you."
            if calendly_url:
                closing += f"\n\nIf you’d like to book a call as well, here’s the link: {calendly_url}"
            return f"{closing}\n\nHere’s what I collected:\n{summary}"

    if should_start_qualification(query, calendly_url):
        session["awaiting_opt_in"] = True
        session["active"] = False
        session["step"] = 0
        session["answers"] = {}
        save_lead_state(lead_state_path, state)
        return begin_qualification_flow(agent_name)

    return None


def build_openai_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def extract_response_text(response_json: dict) -> str:
    output_text = compact(response_json.get("output_text"))
    if output_text:
        return output_text

    chunks: List[str] = []
    for item in response_json.get("output", []):
        for content in item.get("content", []):
            text = compact(content.get("text"))
            if text:
                chunks.append(text)
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def generate_ai_reply(
    query: str,
    matches: List[dict],
    listing_doc_url: str,
    calendly_url: str,
    agent_name: str,
    api_key: str,
    model: str,
) -> Optional[str]:
    listing_payload = [listing_context(match) for match in matches[:3]]
    system_prompt = (
        "You are Durham New Homes, a real-estate leasing and sales assistant for Nabeel. "
        "Answer like a capable human leasing coordinator: warm, concise, natural, and practical. "
        "Use only the provided listing data and provided packet link. "
        "Do not invent features, pricing, amenities, policies, or availability. "
        "Never mention internal workflow labels, back-office statuses, review states, or marketplace pipeline terms to the customer. "
        "If the user asks something not present in the data, say that it is not confirmed yet and ask a targeted follow-up. "
        "If the query is generic and no exact answer is available, guide the user to share the address, ListingKey, or unit number. "
        "Prefer short conversational paragraphs, not bullets, unless the user explicitly asks for a list. "
        "Avoid sounding robotic, overly formal, or repetitive. "
        "Only describe listings as available if the provided data clearly shows they are customer-ready active rental listings. "
        "If there are no customer-ready matches, say there are no active matches ready to share right now and offer to refine the search or follow up later. "
        "If the user wants to book a call, meeting, or showing and a Calendly URL is provided, offer that booking link naturally."
    )
    user_prompt = {
        "user_message": query,
        "listing_packet_url": listing_doc_url,
        "calendly_url": calendly_url,
        "agent_name": agent_name,
        "matched_listings": listing_payload,
    }
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": json.dumps(user_prompt, ensure_ascii=False)}],
            },
        ],
        "max_output_tokens": 350,
    }
    resp = requests.post(
        OPENAI_RESPONSES_API,
        headers=build_openai_headers(api_key),
        json=payload,
        timeout=int(os.getenv("OPENAI_REQUEST_TIMEOUT_SECONDS", "20") or "20"),
    )
    resp.raise_for_status()
    output_text = extract_response_text(resp.json())
    return output_text or None


def build_reply(
    sender_id: str,
    query: str,
    drafts: List[dict],
    listing_doc_url: str,
    calendly_url: str = "",
    agent_name: str = "Nabeel",
    lead_state_path: Path = Path("lead_intake_state.json"),
    openai_api_key: str = "",
    openai_model: str = "gpt-4.1-mini",
    use_ai: bool = True,
) -> str:
    qualification_reply = maybe_handle_qualification(
        sender_id,
        query,
        lead_state_path,
        agent_name,
        calendly_url,
    )
    if qualification_reply:
        return qualification_reply

    normalized = query.lower().strip()
    link_only_patterns = [
        "doc",
        "document",
        "sheet",
        "link",
        "packet",
        "send me the packet",
        "send packet",
        "share the packet",
        "share packet",
        "send me the link",
        "share the link",
    ]
    if any(pattern == normalized for pattern in link_only_patterns):
        if listing_doc_url:
            return f"Here is the current listing packet: {listing_doc_url}"
        return "I do not have the document URL configured yet."

    matches = rank_drafts(query, drafts, limit=3)
    ready_matches = shortlist_for_booking(matches)

    if looks_like_booking_request(query):
        if ready_matches:
            chosen = ready_matches[0]
            title = compact(chosen.get("MarketplaceTitle")) or compact(chosen.get("Address")) or "that listing"
            if calendly_url:
                return (
                    f"Absolutely. The best next step is to book a time with {agent_name} here: {calendly_url} "
                    f"and mention {title}. If you want, I can also share a quick summary of the listing before you book."
                )
            return (
                f"Absolutely. I can help with {title}. I do not have the booking link configured yet, "
                f"but if you send your preferred day and time I can note it for {agent_name}."
            )
        if calendly_url:
            return (
                f"I can help with that. Before booking, please send the exact address or unit you want so I point you to the right listing. "
                f"If you already know it, you can also book directly here: {calendly_url}"
            )

    if openai_api_key and use_ai:
        try:
            ai_reply = generate_ai_reply(
                query,
                matches,
                listing_doc_url,
                calendly_url,
                agent_name,
                openai_api_key,
                openai_model,
            )
            if ai_reply:
                return ai_reply
        except Exception as exc:
            print(f"AI reply generation failed: {exc}")

    if not matches:
        if customer_visible_drafts(drafts):
            return (
                "I do not have an exact active match for that search yet. Send me the area, address, price range, "
                "or unit type you want, and I will narrow it down."
            )
        return (
            "I do not have any active rental listings ready to share right now. If you tell me the area, budget, "
            "and unit type you want, I can note your search and help refine it."
        )

    lines = ["I found these matching listings:"]
    for draft in matches:
        lines.append("")
        lines.append(summarize_draft(draft))
        desc = compact(draft.get("MarketplaceDescription"))
        if desc:
            lines.append(desc[:500])
    if listing_doc_url:
        lines.append("")
        lines.append(f"Packet: {listing_doc_url}")
    return "\n".join(lines)


def current_drafts(config: MessengerConfig) -> List[dict]:
    now = time.time()
    source = config.drafts_sheet_csv_url or str(config.drafts_path)
    cached_source = compact(_DRAFT_CACHE.get("source"))
    cached_at = float(_DRAFT_CACHE.get("fetched_at") or 0.0)
    cached_drafts = _DRAFT_CACHE.get("drafts") or []
    if cached_source == source and now - cached_at < max(config.drafts_cache_seconds, 1):
        return list(cached_drafts)

    drafts: List[dict] = []
    if config.drafts_sheet_csv_url:
        try:
            drafts = fetch_sheet_drafts(config.drafts_sheet_csv_url)
            print(f"Loaded {len(drafts)} drafts from sheet CSV")
        except Exception as exc:
            print(f"Failed loading sheet drafts: {exc}")

    if not drafts:
        drafts = current_drafts(config)
        print(f"Loaded {len(drafts)} drafts from local JSON fallback")

    _DRAFT_CACHE["source"] = source
    _DRAFT_CACHE["fetched_at"] = now
    _DRAFT_CACHE["drafts"] = list(drafts)
    return drafts


def send_message(page_access_token: str, recipient_id: str, text: str) -> dict:
    url = f"{GRAPH_BASE}/me/messages"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text},
    }
    resp = requests.post(
        url,
        params={"access_token": page_access_token},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def send_sender_action(page_access_token: str, recipient_id: str, action: str) -> dict:
    url = f"{GRAPH_BASE}/me/messages"
    payload = {
        "recipient": {"id": recipient_id},
        "sender_action": action,
    }
    resp = requests.post(
        url,
        params={"access_token": page_access_token},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def verify_signature(app_secret: str, raw_body: bytes, signature_header: Optional[str]) -> bool:
    if not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), msg=raw_body, digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.split("=", 1)[1])


@dataclass
class MessengerConfig:
    page_access_token: str
    verify_token: str
    app_secret: str = ""
    drafts_path: Path = Path("marketplace_drafts.json")
    drafts_sheet_csv_url: str = ""
    drafts_cache_seconds: int = 30
    listing_doc_url: str = ""
    calendly_url: str = ""
    agent_name: str = "Nabeel"
    lead_state_path: Path = Path("lead_intake_state.json")
    page_id: str = ""
    poll_state_path: Path = Path("messenger_poll_state.json")
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    token_source: str = "page"
    bootstrap_reply_lookback_seconds: int = 86400


def make_config() -> MessengerConfig:
    page_id = optional_env("META_PAGE_ID", "")
    page_access_token = optional_env("META_PAGE_ACCESS_TOKEN", "")
    user_access_token = optional_env("META_USER_ACCESS_TOKEN", "")
    token_source = "page"

    if not page_access_token:
        if not user_access_token:
            raise RuntimeError(
                "Missing token configuration: set META_PAGE_ACCESS_TOKEN or META_USER_ACCESS_TOKEN"
            )
        if not page_id:
            raise RuntimeError("META_PAGE_ID is required when using META_USER_ACCESS_TOKEN")
        page_access_token = resolve_page_access_token(user_access_token, page_id)
        token_source = "derived-from-user"

    listing_doc_url = os.getenv("LISTING_DOC_URL", "")
    drafts_sheet_csv_url = optional_env("MARKETPLACE_DRAFTS_SHEET_CSV_URL", "") or build_sheet_csv_url(listing_doc_url)

    return MessengerConfig(
        page_access_token=page_access_token,
        verify_token=must_env("META_VERIFY_TOKEN"),
        app_secret=os.getenv("META_APP_SECRET", ""),
        drafts_path=Path(os.getenv("MARKETPLACE_DRAFTS_JSON", "marketplace_drafts.json")),
        drafts_sheet_csv_url=drafts_sheet_csv_url,
        drafts_cache_seconds=int(os.getenv("DRAFTS_CACHE_SECONDS", "30") or "30"),
        listing_doc_url=listing_doc_url,
        calendly_url=os.getenv("CALENDLY_URL", ""),
        agent_name=os.getenv("AGENT_NAME", "Nabeel"),
        lead_state_path=Path(os.getenv("LEAD_STATE_FILE", "lead_intake_state.json")),
        page_id=page_id,
        poll_state_path=Path(os.getenv("POLL_STATE_FILE", "messenger_poll_state.json")),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        token_source=token_source,
        bootstrap_reply_lookback_seconds=int(os.getenv("POLL_BOOTSTRAP_LOOKBACK_SECONDS", "86400") or "86400"),
    )


class MessengerWebhookHandler(BaseHTTPRequestHandler):
    server_version = "MessengerAutomation/1.0"

    @property
    def config(self) -> MessengerConfig:
        return self.server.config  # type: ignore[attr-defined]

    def log_message(self, format: str, *args):  # noqa: A003
        print(format % args)

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if parsed.path == "/debug/status":
            token = params.get("token", [""])[0]
            if token != self.config.verify_token:
                self.send_error(403, "Forbidden")
                return
            seen = load_seen_message_ids(self.config.poll_state_path)
            self._send_json(
                200,
                {
                    "ok": True,
                    "draft_count": len(current_drafts(self.config)),
                    "draft_source": self.config.drafts_sheet_csv_url or str(self.config.drafts_path),
                    "has_page_access_token": bool(self.config.page_access_token),
                    "token_source": self.config.token_source,
                    "has_app_secret": bool(self.config.app_secret),
                    "has_listing_doc_url": bool(self.config.listing_doc_url),
                    "page_id": self.config.page_id,
                    "poll_interval_seconds": getattr(self.server, "poll_interval_seconds", 0),
                    "poll_state_file": str(self.config.poll_state_path),
                    "seen_message_count": len(seen),
                },
            )
            return
        if parsed.path == "/poll-once":
            token = params.get("token", [""])[0]
            initialize_only = params.get("initialize_only", ["0"])[0] in ("1", "true", "yes")
            reset_seen = params.get("reset_seen", ["0"])[0] in ("1", "true", "yes")
            use_ai = params.get("use_ai", ["1"])[0] not in ("0", "false", "no")
            conversation_limit = int(params.get("conversation_limit", ["10"])[0] or "10")
            per_conversation_limit = int(params.get("per_conversation_limit", ["5"])[0] or "5")
            if token != self.config.verify_token:
                self.send_error(403, "Forbidden")
                return
            try:
                if reset_seen and self.config.poll_state_path.exists():
                    self.config.poll_state_path.unlink()
                result = poll_conversations_once(
                    self.config,
                    initialize_only=initialize_only,
                    conversation_limit=conversation_limit,
                    per_conversation_limit=per_conversation_limit,
                    use_ai=use_ai,
                )
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "reset_seen": reset_seen,
                        "use_ai": use_ai,
                        "conversation_limit": conversation_limit,
                        "per_conversation_limit": per_conversation_limit,
                        **result,
                    },
                )
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if parsed.path != "/webhook":
            self.send_error(404, "Not found")
            return

        mode = params.get("hub.mode", [""])[0]
        token = params.get("hub.verify_token", [""])[0]
        challenge = params.get("hub.challenge", [""])[0]
        if mode == "subscribe" and token == self.config.verify_token:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(challenge.encode("utf-8"))
            return

        self.send_error(403, "Verification failed")

    def do_POST(self):
        if self.path != "/webhook":
            self.send_error(404, "Not found")
            return

        raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.config.app_secret:
            signature = self.headers.get("X-Hub-Signature-256")
            if not verify_signature(self.config.app_secret, raw_body, signature):
                self.send_error(403, "Invalid signature")
                return

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            self.send_error(400, "Invalid JSON")
            return

        self.process_webhook(payload)
        self._send_json(200, {"status": "ok"})

    def process_webhook(self, payload: dict):
        drafts = current_drafts(self.config)
        entry_list = payload.get("entry", [])
        for entry in entry_list:
            messaging = entry.get("messaging", [])
            for event in messaging:
                sender_id = event.get("sender", {}).get("id")
                message = event.get("message", {})
                text = compact(message.get("text"))
                if not sender_id or not text:
                    continue

                try:
                    send_sender_action(self.config.page_access_token, sender_id, "typing_on")
                except Exception as exc:
                    print(f"Failed sending typing indicator to {sender_id}: {exc}")

                reply = build_reply(
                    sender_id,
                    text,
                    drafts,
                    self.config.listing_doc_url,
                    calendly_url=self.config.calendly_url,
                    agent_name=self.config.agent_name,
                    lead_state_path=self.config.lead_state_path,
                    openai_api_key=self.config.openai_api_key,
                    openai_model=self.config.openai_model,
                    use_ai=True,
                )
                try:
                    send_message(self.config.page_access_token, sender_id, reply)
                    print(f"Replied to {sender_id}: {text[:80]}")
                except Exception as exc:
                    print(f"Failed sending to {sender_id}: {exc}")


def run_server(config: MessengerConfig, port: int):
    httpd = ThreadingHTTPServer(("0.0.0.0", port), MessengerWebhookHandler)
    httpd.config = config  # type: ignore[attr-defined]
    httpd.poll_interval_seconds = int(os.getenv("POLL_CONVERSATIONS_SECONDS", "0") or "0")  # type: ignore[attr-defined]
    print(f"Messenger webhook listening on http://0.0.0.0:{port}/webhook")
    httpd.serve_forever()


def list_conversations(page_id: str, page_access_token: str, limit: int = 25) -> dict:
    # Conversations API is useful for diagnostics/sync, but replies should still use Send API.
    url = f"{GRAPH_BASE}/{page_id}/conversations"
    resp = requests.get(
        url,
        params={
            "access_token": page_access_token,
            "limit": limit,
            "fields": "updated_time,participants,senders,message_count",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def load_seen_message_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    values = payload.get("seen_message_ids", []) if isinstance(payload, dict) else []
    return {str(value) for value in values}


def save_seen_message_ids(path: Path, seen: set[str]):
    # Keep state bounded; old message IDs are only needed to avoid duplicate replies.
    recent = sorted(seen)[-2000:]
    path.write_text(json.dumps({"seen_message_ids": recent}, indent=2), encoding="utf-8")


def list_recent_messages(
    page_id: str,
    page_access_token: str,
    conversation_limit: int = 10,
    per_conversation_limit: int = 5,
) -> List[dict]:
    conversations = list_conversations(page_id, page_access_token, limit=conversation_limit).get("data", [])
    messages: List[dict] = []
    for conversation in conversations:
        conversation_id = conversation.get("id")
        if not conversation_id:
            continue
        resp = requests.get(
            f"{GRAPH_BASE}/{conversation_id}/messages",
            params={
                "access_token": page_access_token,
                "limit": per_conversation_limit,
                "fields": "id,created_time,from,message",
            },
            timeout=30,
        )
        resp.raise_for_status()
        messages.extend(resp.json().get("data", []))
    return messages


def poll_conversations_once(
    config: MessengerConfig,
    initialize_only: bool = False,
    conversation_limit: int = 10,
    per_conversation_limit: int = 5,
    use_ai: bool = True,
) -> dict:
    if not config.page_id:
        raise RuntimeError("META_PAGE_ID is required when polling is enabled")

    seen = load_seen_message_ids(config.poll_state_path)
    drafts = current_drafts(config)
    result = {
        "reply_count": 0,
        "initialize_only": initialize_only,
        "processed_count": 0,
        "seen_before_count": 0,
        "skipped_page_or_empty_count": 0,
        "initialized_seen_count": 0,
        "errors": [],
    }
    now_ts = int(time.time())
    bootstrap_cutoff_ts = now_ts - max(config.bootstrap_reply_lookback_seconds, 0)

    for message in list_recent_messages(
        config.page_id,
        config.page_access_token,
        conversation_limit=conversation_limit,
        per_conversation_limit=per_conversation_limit,
    ):
        message_id = compact(message.get("id"))
        if not message_id or message_id in seen:
            result["seen_before_count"] += 1
            continue

        result["processed_count"] += 1
        sender = message.get("from", {}) or {}
        sender_id = compact(sender.get("id"))
        text = compact(message.get("message"))
        created_ts = parse_graph_time(compact(message.get("created_time")))

        if initialize_only:
            # On cold start, keep recent inbound messages unseen so the next poll can answer them.
            is_old = created_ts is not None and created_ts < bootstrap_cutoff_ts
            if not sender_id or sender_id == config.page_id or not text or is_old:
                seen.add(message_id)
                result["initialized_seen_count"] += 1
            continue

        if not sender_id or sender_id == config.page_id or not text:
            seen.add(message_id)
            result["skipped_page_or_empty_count"] += 1
            continue

        try:
            send_sender_action(config.page_access_token, sender_id, "typing_on")
        except Exception as exc:
            print(f"Poll failed sending typing indicator to {sender_id}: {exc}")

        reply = build_reply(
            sender_id,
            text,
            drafts,
            config.listing_doc_url,
            calendly_url=config.calendly_url,
            agent_name=config.agent_name,
            lead_state_path=config.lead_state_path,
            openai_api_key=config.openai_api_key,
            openai_model=config.openai_model,
            use_ai=use_ai,
        )
        try:
            send_message(config.page_access_token, sender_id, reply)
            seen.add(message_id)
            result["reply_count"] += 1
            print(f"Poll replied to {sender_id}: {text[:80]}")
        except Exception as exc:
            print(f"Poll failed sending to {sender_id}: {exc}")
            result["errors"].append(
                {
                    "message_id": message_id,
                    "sender_id": sender_id,
                    "text_preview": text[:120],
                    "error": str(exc),
                }
            )

    save_seen_message_ids(config.poll_state_path, seen)
    return result


def start_conversation_poller(config: MessengerConfig, interval_seconds: int):
    def worker():
        try:
            init_result = poll_conversations_once(config, initialize_only=True)
            print(f"Conversation poller initialized existing messages as seen: {json.dumps(init_result)}")
            print("Conversation poller initialized existing messages as seen")
        except Exception as exc:
            print(f"Conversation poller init failed: {exc}")

        while True:
            time.sleep(interval_seconds)
            try:
                result = poll_conversations_once(config)
                if result.get("reply_count") or result.get("errors"):
                    print(f"Conversation poll result: {json.dumps(result)}")
            except Exception as exc:
                print(f"Conversation poller failed: {exc}")

    thread = threading.Thread(target=worker, name="conversation-poller", daemon=True)
    thread.start()
    print(f"Conversation polling fallback enabled every {interval_seconds}s")


def main():
    parser = argparse.ArgumentParser(description="Messenger automation webhook for MLS listings.")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "conversations"], help="Action to run")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", os.getenv("MESSENGER_PORT", "8000"))))
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()

    config = make_config()

    if args.command == "conversations":
        page_id = must_env("META_PAGE_ID")
        payload = list_conversations(page_id, config.page_access_token, limit=args.limit)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    poll_interval = int(os.getenv("POLL_CONVERSATIONS_SECONDS", "0") or "0")
    if poll_interval > 0:
        start_conversation_poller(config, poll_interval)

    run_server(config, args.port)


if __name__ == "__main__":
    main()
