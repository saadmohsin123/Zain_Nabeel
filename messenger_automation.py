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
- OPENAI_API_KEY              # recommended for intelligent qualification parsing and natural replies
- OPENAI_MODEL                # default: gpt-4.1
- CALENDLY_URL                # optional booking link for calls/showings
- AGENT_NAME                  # default: Nabeel
- POLL_CONVERSATIONS_SECONDS  # optional fallback when Meta does not deliver webhooks
- POLL_STATE_FILE             # default: messenger_poll_state.json
- DATABASE_URL                # optional Railway PostgreSQL for per-sender session storage
- LEAD_STATE_FILE             # default: lead_intake_state.json (local dev fallback)
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
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests

import session_store


GRAPH_BASE = "https://graph.facebook.com/v20.0"
OPENAI_RESPONSES_API = "https://api.openai.com/v1/responses"
DEFAULT_SHEET_GID = "0"
_DRAFT_CACHE: dict = {"source": "", "fetched_at": 0.0, "drafts": [], "degraded": False}
_LEAD_STATE_LOCK = threading.Lock()
_POLL_STATE_LOCK = threading.Lock()
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
SEARCH_CITIES = [
    "niagara falls",
    "richmond hill",
    "north york",
    "east york",
    "scarborough",
    "mississauga",
    "newmarket",
    "whitby",
    "pickering",
    "oakville",
    "burlington",
    "hamilton",
    "brampton",
    "markham",
    "vaughan",
    "oshawa",
    "ajax",
    "toronto",
]
QUALIFICATION_STEPS = [
    {"key": "move_in_date", "prompt": "What’s your expected move-in date?"},
    {"key": "people_on_lease", "prompt": "How many people will be on the lease?"},
    {"key": "adults_in_unit", "prompt": "How many adults will be living in the unit?"},
    {"key": "kids_in_unit", "prompt": "How many kids will be living in the unit?"},
    {
        "key": "family_gross_income",
        "prompt": "What’s your total family gross income? Please do not include cash income.",
    },
    {"key": "occupation", "prompt": "What do you do for work?"},
    {"key": "resident_status", "prompt": "What is your resident status in Canada?"},
    {"key": "working_with_agent", "prompt": "Are you currently working with an agent?"},
    {"key": "phone_number", "prompt": "And what’s the best phone number to reach you on?"},
]
QUALIFICATION_BATCHES = [
    {
        "keys": ["move_in_date", "people_on_lease"],
        "prompt": "Perfect. What's your expected move-in date?",
    },
    {
        "keys": ["adults_in_unit", "kids_in_unit"],
        "prompt": "Got it. How many adults will be living in the unit?",
    },
    {
        "keys": ["family_gross_income", "occupation"],
        "prompt": "Thanks. What's your total family gross income? Please do not include cash income.",
    },
    {
        "keys": ["resident_status", "working_with_agent"],
        "prompt": "Almost done. What is your resident status in Canada?",
    },
    {
        "keys": ["phone_number"],
        "prompt": "Last one — what's the best phone number to reach you on?",
    },
]
QUALIFICATION_FIELD_KEYS = [step["key"] for step in QUALIFICATION_STEPS]

DEFAULT_OPENAI_MODEL = "gpt-4.1"

AI_MASTER_SYSTEM_PROMPT = """You are the Durham New Homes Messenger assistant for Nabeel's rental leads.

ROLE: Sound human — warm, brief, natural. Usually 1-2 short sentences. Never robotic.

OUTPUT: Return JSON only (no markdown):
{
  "fields": {"field_key": "value"},
  "reply": "your message to the user"
}

=== PIPELINE (strict order) ===
1. NEW → greet; learn what they want
2. AWAITING_OPT_IN → offer free listing help after a few quick questions; need yes to proceed
3. QUALIFYING → collect ALL fields below before any listings
4. QUALIFIED → discuss shared listings, booking, search refinements

=== QUALIFICATION FIELDS (never re-ask if in collected_answers) ===
Batch 1: move_in_date, people_on_lease
Batch 2: adults_in_unit, kids_in_unit
Batch 3: family_gross_income, occupation
Batch 4: resident_status, working_with_agent (Yes or No only)
Batch 5: phone_number

=== PARSING RULES (fields) ===
- Only fill fields clearly stated in the latest user message
- Only use keys listed in allowed_field_keys when provided
- "just me" / "only me" → people_on_lease=1, adults_in_unit=1, kids_in_unit=0
- "me and my brother/sister/partner" → people_on_lease=2
- Adults only → kids_in_unit=0
- adults_in_unit cannot exceed people_on_lease
- Fix typos: "pf" → "of" in dates
- working_with_agent must be exactly Yes or No

=== REPLY RULES ===
- Follow directive exactly — it tells you what stage you're in and what to ask
- NEVER ask about fields already in collected_answers
- NEVER mention listings, prices, or units before stage is QUALIFIED
- NEVER send or mention a booking/Calendly link unless directive allows_booking is true
- When discussing listings after qualification, use ONLY listing_data provided — do not invent
- For first/second/third listing references, use list_position from last_shared_listings
- Do not repeat last_assistant_message verbatim
- If directive says ask for one field, ask exactly one question — never combine multiple qualification questions in one message

=== WHEN fields should be empty ===
Set fields to {} when stage is NEW, AWAITING_OPT_IN (unless user volunteered qual info with yes), or when reply-only turns."""


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
    constraints = extract_search_constraints(query)

    candidate_drafts = customer_visible_drafts(drafts)
    if not candidate_drafts:
        return []

    if constraints:
        filtered = [draft for draft in candidate_drafts if draft_matches_constraints(draft, constraints)]
        if not filtered:
            return []
        candidate_drafts = filtered

    if not q_tokens:
        return candidate_drafts[:limit]

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
        bed_target = constraints.get("bedrooms")
        if bed_target is not None:
            draft_beds = draft_bedroom_count(draft)
            if draft_beds == bed_target:
                score += 20
        city_target = constraints.get("city")
        if city_target and draft_matches_city(draft, city_target):
            score += 25
        max_price = constraints.get("max_price")
        if max_price is not None:
            price = draft_listing_price(draft)
            if price is not None and price <= max_price:
                score += 10
        if score:
            scored.append((score, draft))

    if not scored:
        return candidate_drafts[:limit] if constraints else []

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
        "go ahead",
        "please go ahead",
    }
    if q in affirmatives:
        return True
    if q.startswith(("yes ", "yup ", "yeah ", "sure ", "ok ")):
        return True
    if any(phrase in q for phrase in ("yup sure", "yes sure", "yes please", "yup please", "sure please", "go ahead")):
        return True
    return False


def looks_like_opt_in_acceptance(query: str, session: dict) -> bool:
    if not looks_like_affirmative(query):
        return False
    if session.get("awaiting_opt_in"):
        return True
    last = compact(session.get("last_prompt")).lower()
    hints = (
        "few quick questions",
        "say yes",
        "reply yes",
        "go ahead",
        "is that okay",
        "open to renting",
        "help you find rentals",
        "nabeel's assistant",
        "listing help",
    )
    return any(hint in last for hint in hints)


def parse_household_from_text(text: str) -> Dict[str, str]:
    lowered = normalize_whitespace(text).lower()
    if not lowered:
        return {}
    answers: Dict[str, str] = {}
    adults_match = re.search(r"(\d+)\s*adults?", lowered)
    kids_match = re.search(r"(\d+)\s*(?:kid|kids|child|children)\b", lowered)
    if adults_match:
        answers["adults_in_unit"] = adults_match.group(1)
    if kids_match:
        answers["kids_in_unit"] = kids_match.group(1)
    elif re.search(r"\bno kids\b|\b0 kids\b", lowered):
        answers["kids_in_unit"] = "0"
    if answers.get("adults_in_unit") and answers.get("kids_in_unit"):
        answers["people_on_lease"] = str(int(answers["adults_in_unit"]) + int(answers["kids_in_unit"]))
    elif answers.get("adults_in_unit") and re.search(r"\badults?\b", lowered) and "people" not in lowered and "lease" not in lowered:
        if not kids_match and "kid" not in lowered and "child" not in lowered:
            answers["people_on_lease"] = answers["adults_in_unit"]
            answers.setdefault("kids_in_unit", "0")
    return answers


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

def looks_like_greeting(query: str) -> bool:
    q = query.lower().strip()
    greetings = {
        "hi",
        "hello",
        "hey",
        "hiya",
        "good morning",
        "good afternoon",
        "good evening",
    }
    return q in greetings


def looks_like_small_talk(query: str) -> bool:
    q = query.lower().strip()
    if looks_like_greeting(query):
        return True
    phrases = (
        "how are you",
        "how's it going",
        "hows it going",
        "how are u",
        "what's up",
        "whats up",
        "how do you do",
        "good thanks",
        "thank you",
        "thanks",
    )
    return any(phrase in q for phrase in phrases)


STATIC_OPT_IN_NUDGE = "Whenever you're ready, just reply yes and I'll ask a few quick questions."


def replies_are_similar(left: str, right: str) -> bool:
    left_norm = re.sub(r"\s+", " ", compact(left).lower())
    right_norm = re.sub(r"\s+", " ", compact(right).lower())
    if not left_norm or not right_norm:
        return False
    return left_norm == right_norm or left_norm in right_norm or right_norm in left_norm


def looks_like_user_pushback(text: str) -> bool:
    lowered = normalize_whitespace(text).lower()
    if not lowered:
        return False
    phrases = (
        "why do you need",
        "why do u need",
        "why do i need",
        "none of your business",
        "not telling you",
        "don't want to share",
        "dont want to share",
        "fuck",
        "wtf",
        "piss off",
    )
    return any(phrase in lowered for phrase in phrases)


def looks_like_qualification_objection(text: str) -> bool:
    if looks_like_user_pushback(text):
        return True
    cleaned = normalize_whitespace(text)
    if cleaned.endswith("?"):
        return True
    return False


def looks_like_correction(text: str) -> bool:
    lowered = normalize_whitespace(text).lower()
    return any(
        phrase in lowered
        for phrase in ("actually", "i meant", "correction", "sorry", "wait", "not a ", "not the ")
    )


def looks_like_search_refinement(query: str) -> bool:
    q = query.lower()
    patterns = (
        r"\d+\s*bed",
        r"\bpool\b",
        r"\bpets?\b",
        r"\bparking\b",
        r"\bunder\s*\$",
        r"\bbudget\b",
        r"\bi wanted\b",
        r"\bi need\b",
        r"\blooking for\b",
        r"\bdo you have\b",
    )
    return any(re.search(pattern, q) for pattern in patterns)


def extract_search_constraints(query: str) -> dict:
    normalized = normalize_whitespace(query).lower()
    constraints: dict = {}
    bed_match = re.search(r"\b(\d+)\s*bed(?:room)?s?\b", normalized)
    if bed_match:
        constraints["bedrooms"] = int(bed_match.group(1))
        constraints["exclude_commercial"] = True
        constraints["residential_only"] = True
    if re.search(r"\bpool\b", normalized):
        constraints["pool"] = True
    if re.search(r"\bcommercial\b", normalized):
        constraints["exclude_commercial"] = True

    for city in sorted(SEARCH_CITIES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(city)}\b", normalized):
            constraints["city"] = city
            break

    max_price = extract_max_price_from_query(normalized)
    if max_price is not None:
        constraints["max_price"] = max_price

    return constraints


def extract_max_price_from_query(normalized: str) -> Optional[int]:
    flexible = re.search(r"(?:around|about)\s*\$?\s*([\d,]{3,7})", normalized)
    if flexible:
        return int(int(flexible.group(1).replace(",", "")) * 1.15)

    strict = re.search(
        r"(?:under|below|less than|max(?:imum)?|up to)\s*\$?\s*([\d,]{3,7})",
        normalized,
    )
    if strict:
        return int(strict.group(1).replace(",", ""))

    money = re.search(r"\$\s*([\d,]{3,7})", normalized)
    if money:
        return int(money.group(1).replace(",", ""))

    bare = re.search(r"\b([\d,]{4,7})\b", normalized.replace(",", ""))
    if bare and not re.search(r"\b\d\s*bed", normalized):
        value = int(bare.group(1))
        if value >= 800:
            return value
    return None


def draft_listing_price(draft: dict) -> Optional[int]:
    price = draft.get("MarketplacePrice")
    if isinstance(price, (int, float)) and price > 0:
        return int(price)
    display = compact(draft.get("MarketplacePriceDisplay"))
    match = re.search(r"([\d,]+)", display.replace(",", ""))
    if match:
        return int(match.group(1))
    return None


def draft_matches_city(draft: dict, city: str) -> bool:
    city = compact(city).lower()
    if not city:
        return True
    haystack = " ".join(
        [
            compact(draft.get("City")),
            compact(draft.get("Address")),
            compact(draft.get("MarketplaceTitle")),
        ]
    ).lower()
    return city in haystack


def merge_search_queries(session: dict, query: str) -> str:
    base = compact(session.get("search_query"))
    current = compact(query)
    if not base:
        return current
    if not current:
        return base

    base_constraints = extract_search_constraints(base)
    current_constraints = extract_search_constraints(current)
    parts = [current]
    if base_constraints.get("city") and not current_constraints.get("city"):
        parts.append(base_constraints["city"])
    if base_constraints.get("bedrooms") and not current_constraints.get("bedrooms"):
        parts.append(f"{base_constraints['bedrooms']} bedroom")
    if base_constraints.get("max_price") and not current_constraints.get("max_price"):
        parts.append(f"under {base_constraints['max_price']}")
    return " ".join(parts)


def draft_bedroom_count(draft: dict) -> Optional[int]:
    total = compact(draft.get("BedroomsTotal"))
    if total.isdigit():
        return int(total)
    title = compact(draft.get("MarketplaceTitle")).lower()
    match = re.search(r"(\d+)\s*bed", title)
    return int(match.group(1)) if match else None


def draft_is_commercial(draft: dict) -> bool:
    haystack = draft_text(draft)
    property_type = normalize_status(compact(draft.get("PropertyType")))
    return "commercial" in haystack or property_type == "commercial"


def draft_matches_constraints(draft: dict, constraints: dict) -> bool:
    if not constraints:
        return True
    if constraints.get("exclude_commercial") and draft_is_commercial(draft):
        return False
    if constraints.get("residential_only") and draft_is_commercial(draft):
        return False
    bed_target = constraints.get("bedrooms")
    if bed_target is not None:
        draft_beds = draft_bedroom_count(draft)
        if draft_beds is None or draft_beds != bed_target:
            return False
    if constraints.get("pool"):
        if not re.search(r"\bpool\b", draft_text(draft)):
            return False
    city = constraints.get("city")
    if city and not draft_matches_city(draft, city):
        return False
    max_price = constraints.get("max_price")
    if max_price is not None:
        price = draft_listing_price(draft)
        if price is None or price > max_price:
            return False
    return True


def qualified_conversational_reply(session: dict, agent_name: str, query: str) -> str:
    if looks_like_greeting(query):
        if compact(session.get("selected_listing_key")):
            return (
                f"Hi! I'm still here if you have questions about that unit or want to book a viewing."
            )
        if session.get("last_shared_listing_keys"):
            return (
                "Hi! Want to ask about one of the listings I shared, refine your search, or book a viewing?"
            )
        return f"Hi! I'm {agent_name}'s assistant — tell me what you're looking for and I'll help."
    if "how are you" in query.lower():
        return "Doing well, thanks! What can I help you with on your rental search?"
    return "Happy to help — want to refine your search or ask about a specific listing?"


def guard_against_repeat_reply(reply: str, session: dict, answers: Optional[dict] = None) -> str:
    last = compact(session.get("last_prompt"))
    if not reply or not replies_are_similar(reply, last):
        return reply
    answers = answers or session.get("answers", {})
    missing = first_missing_qualification_key(answers)
    if missing:
        alt = build_missing_field_prompt([missing], answers)
        if not replies_are_similar(alt, last):
            return alt
    return reply


def reset_stale_opt_in_session(session: dict, query: str) -> bool:
    if not session.get("awaiting_opt_in") or session.get("active") or session.get("qualified"):
        return False
    if not looks_like_greeting(query):
        return False
    session["awaiting_opt_in"] = False
    session["search_query"] = ""
    session["last_prompt"] = ""
    return True


def local_conversational_fallback(
    stage: str,
    query: str,
    agent_name: str,
    last_assistant_message: str = "",
    search_query: str = "",
) -> str:
    lowered = query.lower()
    last_lower = last_assistant_message.lower()

    if "how are you" in lowered or "how's it going" in lowered or "hows it going" in lowered:
        if "reply yes" in last_lower or "whenever you're ready" in last_lower:
            return "Doing well, thanks! Say yes whenever you'd like me to find some options."
        return "I'm doing well, thanks! What area or type of rental are you looking for?"

    if looks_like_greeting(query):
        if stage == "awaiting_opt_in":
            return "Hi! Say yes when you're ready and I'll ask a few quick questions to find the best rentals."
        return "Hi! What are you looking for — area, budget, or unit type?"

    if stage == "awaiting_opt_in":
        if STATIC_OPT_IN_NUDGE.lower() in last_lower or "reply yes" in last_lower:
            summary = describe_search_preferences(search_query)
            if summary:
                return f"Still here whenever you're ready. Say yes and I'll pull {summary} options for you."
            return "Still here — just say yes when you want me to get started."
        return "Happy to help. Say yes when you want me to ask a few quick questions first."

    if stage == "qualifying":
        return "Got it — share whatever you have and we'll fill in the rest."

    return "I can help with rentals. What area or unit type are you looking for?"


def save_session_reply(lead_state_path: Path, state: dict, session: dict, reply: str) -> str:
    if session.get("active"):
        reply = guard_against_repeat_reply(reply, session, session.get("answers", {}))
    session["last_prompt"] = reply
    persist_lead_state(lead_state_path, state)
    return reply


def ai_conversational_fallback(
    api_key: str,
    model: str,
    query: str,
    agent_name: str,
    stage: str,
    search_query: str = "",
    last_assistant_message: str = "",
) -> str:
    stage_key = stage if stage in {"new", "awaiting_opt_in", "pre_qual", "qualifying", "qualified"} else "pre_qual"
    if stage_key == "qualifying":
        stage_key = "pre_qual"
    return ai_generate_conversational_reply(
        api_key,
        model,
        query,
        agent_name,
        conversation_stage=stage_key,
        search_query=search_query,
        last_assistant_message=last_assistant_message,
    )


def infer_household_defaults(answers: dict, query: str = "") -> None:
    lowered = normalize_whitespace(query).lower()
    solo_phrases = ("just me", "only me", "me only", "by myself", "just going to be me", "it'll be me", "it will be me")
    if any(phrase in lowered for phrase in solo_phrases):
        if not compact(answers.get("people_on_lease")):
            answers["people_on_lease"] = "1"
        if not compact(answers.get("adults_in_unit")):
            answers["adults_in_unit"] = "1"
        if not compact(answers.get("kids_in_unit")):
            answers["kids_in_unit"] = "0"
    people = compact(answers.get("people_on_lease"))
    if people == "1":
        if not compact(answers.get("adults_in_unit")):
            answers["adults_in_unit"] = "1"
        if not compact(answers.get("kids_in_unit")):
            answers["kids_in_unit"] = "0"
    adults = compact(answers.get("adults_in_unit"))
    kids = compact(answers.get("kids_in_unit"))
    if adults.isdigit() and kids.isdigit() and not compact(answers.get("people_on_lease")):
        answers["people_on_lease"] = str(int(adults) + int(kids))
    validate_household_counts(answers)


def build_next_qualification_reply(
    session: dict,
    answers: dict,
    drafts: List[dict],
    listing_doc_url: str,
) -> str:
    missing_key = first_missing_qualification_key(answers)
    session["batch"] = first_incomplete_batch_index(answers)
    if not missing_key:
        session["active"] = False
        session["qualified"] = True
        session["completed_at"] = int(time.time())
        return build_post_qualification_reply(session, drafts, listing_doc_url)
    return build_missing_field_prompt([missing_key], answers)


def apply_qualification_turn(
    session: dict,
    query: str,
    drafts: List[dict],
    listing_doc_url: str,
    openai_api_key: str,
    openai_model: str,
) -> str:
    extract_all_qualification_fields(session, query, openai_api_key, openai_model)
    return build_next_qualification_reply(session, session.get("answers", {}), drafts, listing_doc_url)


def qualification_turn_reply(
    session: dict,
    query: str,
    lead_state_path: Path,
    state: dict,
    agent_name: str,
    drafts: List[dict],
    listing_doc_url: str,
    openai_api_key: str,
    openai_model: str,
) -> Optional[str]:
    return apply_qualification_turn(
        session,
        query,
        drafts,
        listing_doc_url,
        openai_api_key,
        openai_model,
    )


def finalize_conversation_reply(
    reply: str,
    session: dict,
    query: str,
    lead_state_path: Path,
    state: dict,
    agent_name: str,
    openai_api_key: str,
    openai_model: str,
    drafts: List[dict],
    listing_doc_url: str,
) -> str:
    last_assistant_message = compact(session.get("last_prompt"))
    stage = conversation_stage(session)

    if reply and not replies_are_similar(reply, last_assistant_message):
        return save_session_reply(lead_state_path, state, session, reply)

    if openai_api_key:
        ai_reply = ai_conversational_fallback(
            openai_api_key,
            openai_model,
            query,
            agent_name,
            stage,
            search_query=compact(session.get("search_query")),
            last_assistant_message=last_assistant_message,
        )
        if ai_reply and not replies_are_similar(ai_reply, last_assistant_message):
            return save_session_reply(lead_state_path, state, session, ai_reply)

    if stage == "qualifying":
        qual_reply = qualification_turn_reply(
            session,
            query,
            lead_state_path,
            state,
            agent_name,
            drafts,
            listing_doc_url,
            openai_api_key,
            openai_model,
        )
        if qual_reply and not replies_are_similar(qual_reply, last_assistant_message):
            return save_session_reply(lead_state_path, state, session, qual_reply)

    fallback = local_conversational_fallback(
        stage,
        query,
        agent_name,
        last_assistant_message=last_assistant_message,
        search_query=compact(session.get("search_query")),
    )
    return save_session_reply(lead_state_path, state, session, fallback)

def build_search_opt_in_reply(session: dict, query: str, agent_name: str) -> str:
    session["search_query"] = compact(query)
    session["awaiting_opt_in"] = True
    session["active"] = False
    return qualification_opt_in_prompt(agent_name, describe_search_preferences(query))


def build_awaiting_opt_in_reply(session: dict, query: str, agent_name: str) -> str:
    if wants_listing_help(query):
        session["search_query"] = compact(query)
        summary = describe_search_preferences(query)
        if summary:
            return f"Got it — {summary}. Just reply yes when you're ready and I'll ask a few quick questions."
        return "Got it. Just reply yes when you're ready and I'll ask a few quick questions."
    return local_conversational_fallback(
        "awaiting_opt_in",
        query,
        agent_name,
        last_assistant_message=compact(session.get("last_prompt")),
        search_query=compact(session.get("search_query")),
    )


def qualification_opt_in_prompt(agent_name: str, search_summary: str = "") -> str:
    context = f"Got it — you're looking for {search_summary}.\n\n" if search_summary else ""
    return (
        f"{context}"
        f"That's great. I'm {agent_name}'s assistant and I can help make your search easier. "
        "I have access to rentals beyond Facebook as well, and there is no cost to you.\n\n"
        "Would you like me to send you a list of the best active options? "
        "Just say yes and I'll ask a few quick questions first."
    )


def atomic_write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = ""
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def with_lead_session(sender_id: str, lead_state_path: Path, fn):
    if session_store.use_postgres_sessions():
        session_store.ensure_schema()
        with _LEAD_STATE_LOCK:
            def _run() -> object:
                session = session_store.load_session(sender_id)
                state = {"sessions": {sender_id: session}}
                result = fn(session, state)
                session_store.save_session(sender_id, session)
                return result

            return session_store.with_sender_context(sender_id, _run)

    with _LEAD_STATE_LOCK:
        state = load_lead_state(lead_state_path)
        session = get_lead_session(state, sender_id)
        return fn(session, state)


def with_poll_state(poll_state_path: Path, fn):
    if session_store.use_postgres_sessions():
        with _POLL_STATE_LOCK:
            seen = session_store.load_seen_message_ids()
            result = fn(seen)
            session_store.save_seen_message_ids(seen)
            return result

    with _POLL_STATE_LOCK:
        seen = load_seen_message_ids(poll_state_path)
        result = fn(seen)
        save_seen_message_ids(poll_state_path, seen)
        return result


def load_lead_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_lead_state(path: Path, payload: dict):
    atomic_write_json(path, payload)


def persist_lead_state(path: Path, state: dict, sender_id: str = "") -> None:
    sid = sender_id or session_store.current_sender_id()
    if session_store.use_postgres_sessions() and sid:
        session = state.get("sessions", {}).get(sid)
        if session is not None:
            session_store.save_session(sid, session)
        return
    save_lead_state(path, state)


def get_lead_session(state: dict, sender_id: str) -> dict:
    sessions = state.setdefault("sessions", {})
    return sessions.setdefault(
        sender_id,
        {
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
    for step in QUALIFICATION_STEPS:
        key = step["key"]
        value = compact(answers.get(key))
        if value:
            lines.append(f"{labels[key]}: {value}")
    return "\n".join(lines)


def describe_search_preferences(query: str) -> str:
    normalized = query.lower().replace("torronto", "toronto")
    parts: List[str] = []
    bed_match = re.search(r"\b(\d+)\s*bed(?:room)?s?\b|\b(\d+)bed(?:room)?s?\b", normalized)
    if bed_match:
        parts.append(f"{bed_match.group(1) or bed_match.group(2)} bedroom")
    unit_types = ["condo", "apartment", "house", "townhouse", "studio", "basement"]
    for unit_type in unit_types:
        if re.search(rf"\b{unit_type}\b", normalized):
            parts.append(unit_type)
            break
    if "downtown" in normalized:
        parts.append("in downtown Toronto")
    else:
        for city in sorted(SEARCH_CITIES, key=len, reverse=True):
            if re.search(rf"\b{re.escape(city)}\b", normalized):
                parts.append(f"in {city.title()}")
                break
        else:
            if re.search(r"\bontario\b", normalized):
                parts.append("in Ontario")
            elif re.search(r"\btoronto\b", normalized):
                parts.append("in Toronto")
    max_price = extract_max_price_from_query(normalized)
    if max_price is not None:
        parts.append(f"up to ${max_price:,}")
    return " ".join(parts).strip()


def begin_qualification_flow(agent_name: str) -> str:
    return qualification_opt_in_prompt(agent_name)


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", compact(text)).strip()


def parse_int_from_text(text: str) -> str:
    normalized = normalize_whitespace(text).lower()
    if not normalized:
        return ""
    if any(phrase in normalized for phrase in ("just me", "only me", "me only", "single applicant")):
        return "1"
    if any(phrase in normalized for phrase in ("no kids", "none", "zero")):
        return "0"
    match = re.search(r"\b(\d+)\b", normalized)
    return match.group(1) if match else ""


def extract_phone_number(text: str) -> str:
    digits = re.sub(r"\D", "", compact(text))
    if len(digits) >= 10:
        return digits[-10:]
    return ""


def extract_income_value(text: str) -> str:
    normalized = normalize_whitespace(text)
    if not normalized:
        return ""
    lowered = normalized.lower()
    k_match = re.search(r"(\d[\d,]*)\s*[kK]\b", normalized)
    if k_match:
        return f"${k_match.group(1)}k"
    money = re.search(r"([$€£]?\s?\d[\d,]*(?:\.\d+)?(?:\s*/\s*(?:year|month))?)", normalized, re.I)
    if money:
        return money.group(1).replace(" ", "")
    if lowered.isdigit():
        return normalized
    return ""


def extract_resident_status(text: str) -> str:
    lowered = normalize_whitespace(text).lower()
    if not lowered:
        return ""
    if re.search(r"\bnon[- ]?residents?\b", lowered):
        return "Non-Resident"
    patterns = [
        ("permanent resident", "Permanent Resident"),
        ("permanent", "Permanent Resident"),
        ("citizen", "Canadian Citizen"),
        ("work permit", "Work Permit"),
        ("open work permit", "Open Work Permit"),
        ("closed work permit", "Closed Work Permit"),
        ("student", "Student"),
        ("visitor", "Visitor"),
        ("refugee", "Refugee"),
    ]
    for pattern, label in patterns:
        if pattern in lowered:
            return label
    if re.fullmatch(r"pr", lowered):
        return "Permanent Resident"
    if re.fullmatch(r"residents?", lowered):
        return "Permanent Resident"
    if re.search(r"\b(?:i'?m|i am)\s+(?:a\s+)?resident\b", lowered):
        return "Permanent Resident"
    if re.search(r"\bresident\b", lowered):
        return "Permanent Resident"
    return ""


def scrub_agent_phrases(text: str) -> str:
    scrubbed = normalize_whitespace(text)
    patterns = [
        r"yes[, ]+i am working with an agent.*$",
        r"yes[, ]+i'?m working with an agent.*$",
        r"i am working with an agent.*$",
        r"i'?m working with an agent.*$",
        r"working with an agent.*$",
        r"working with a agent.*$",
    ]
    for pattern in patterns:
        scrubbed = re.sub(pattern, "", scrubbed, flags=re.I).strip(" ,.-")
    return scrubbed


def extract_agent_answer(text: str) -> str:
    lowered = normalize_whitespace(text).lower()
    if not lowered:
        return ""
    positive_patterns = [
        r"\byes\b",
        r"\bworking with an agent\b",
        r"\bworking with a agent\b",
        r"\bi am working with\b",
        r"\bi'?m working with\b",
        r"\bhave an agent\b",
    ]
    negative_patterns = [
        r"\bno agent\b",
        r"\bnot working with\b",
        r"\bnope\b",
        r"\bjust you\b",
        r"\bonly you\b",
        r"\bnot working with an?\s+\w*agent\w*\b",
    ]
    if any(re.search(pattern, lowered) for pattern in positive_patterns):
        return "Yes"
    if any(re.search(pattern, lowered) for pattern in negative_patterns):
        return "No"
    if re.fullmatch(r"no", lowered):
        return "No"
    return ""


def parse_resident_and_agent(text: str) -> Dict[str, str]:
    answers: Dict[str, str] = {}
    agent_answer = extract_agent_answer(text)
    status_text = scrub_agent_phrases(text)
    resident_status = extract_resident_status(status_text) or extract_resident_status(text)
    if resident_status:
        answers["resident_status"] = resident_status
    if agent_answer in ("Yes", "No"):
        answers["working_with_agent"] = agent_answer
    return answers


def parse_people_count(text: str) -> str:
    lowered = normalize_whitespace(text).lower()
    if re.search(
        r"\bme and my (?:brother|sister|parent|parents|wife|husband|partner|friend|roommate|son|daughter)\b",
        lowered,
    ):
        return "2"
    if re.search(r"\bme and (?:my )?(?:brother|sister|wife|husband|partner|friend|roommate)\b", lowered):
        return "2"
    if re.search(r"\b(two|both) of us\b", lowered):
        return "2"
    if re.search(r"\bfamily of (\d+)\b", lowered):
        return re.search(r"\bfamily of (\d+)\b", lowered).group(1)
    match = re.search(r"(\d+)\s*(?:people|person|tenant|tenants)\b", lowered)
    if match:
        return match.group(1)
    return parse_int_from_text(text)


def qualification_in_progress(session: dict) -> bool:
    if session.get("qualified"):
        return False
    if session.get("active"):
        return True
    answers = session.get("answers", {})
    if any(compact(answers.get(key)) for key in QUALIFICATION_FIELD_KEYS):
        return True
    last = compact(session.get("last_prompt")).lower()
    mid_qual_hints = (
        "when do you need",
        "when are you looking to move",
        "how many people will be",
        "how many people on",
        "how many adults",
        "how many kids",
        "gross income",
        "what do you do for work",
        "resident status",
        "phone number to reach",
    )
    if any(hint in last for hint in mid_qual_hints):
        return True
    return False


def begin_structured_qualification(
    session: dict,
    query: str,
    search_query: str = "",
) -> None:
    session["active"] = True
    session["awaiting_opt_in"] = False
    session["qualified"] = False
    session.setdefault("answers", {})
    session.setdefault("raw_answers", {})
    session.setdefault("batch", 0)
    if search_query:
        session["search_query"] = compact(search_query)
    elif wants_listing_help(query):
        session["search_query"] = compact(query)


def run_structured_qualification(
    session: dict,
    query: str,
    lead_state_path: Path,
    state: dict,
    drafts: List[dict],
    listing_doc_url: str,
    openai_api_key: str,
    openai_model: str,
) -> str:
    begin_structured_qualification(session, query)
    qual_reply = apply_qualification_turn(
        session,
        query,
        drafts,
        listing_doc_url,
        openai_api_key,
        openai_model,
    )
    return save_session_reply(lead_state_path, state, session, qual_reply)


def is_plausible_occupation(text: str) -> bool:
    cleaned = normalize_whitespace(text)
    if not cleaned:
        return False
    if len(cleaned) < 2:
        return False
    if looks_like_qualification_objection(cleaned):
        return False
    lowered = cleaned.lower()
    if lowered in {"just me", "only me", "me only", "me", "just", "only"}:
        return False
    if re.fullmatch(r"[kK]", cleaned):
        return False
    if re.fullmatch(r"\$?\d+[kK]?", cleaned):
        return False
    return True


COUNT_FIELDS = {"people_on_lease", "adults_in_unit", "kids_in_unit"}


def extract_move_in_date(text: str) -> str:
    normalized = normalize_whitespace(text)
    if not normalized:
        return ""
    normalized = re.sub(r"\bpf\b", "of", normalized, flags=re.I)
    months = (
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    )
    patterns = [
        rf"\b(?:early|mid|late)\s+(?:{months})\b",
        rf"\b(?:{months})\s+\d{{1,2}}(?:st|nd|rd|th)?\b",
        rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{months})\b",
        r"\b(?:immediately|asap|next month|this month)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, re.I)
        if match:
            return match.group(0)
    return ""


def looks_like_date_only_answer(text: str) -> bool:
    normalized = normalize_whitespace(text)
    if not normalized or not extract_move_in_date(normalized):
        return False
    lowered = normalized.lower()
    if re.search(r"\d+\s*(?:people|person|adult|adults|tenant|tenants|kid|kids|child|children|lease)\b", lowered):
        return False
    if re.search(r"\b(?:people|person|adult|adults|tenant|tenants|kid|kids|child|children|lease)\b", lowered):
        return False
    return True


def parse_missing_fields(missing_keys: List[str], query: str) -> Dict[str, str]:
    if not missing_keys:
        return {}

    normalized = normalize_whitespace(query)
    if not normalized:
        return {}

    answers: Dict[str, str] = {}
    lowered = normalized.lower()

    if len(missing_keys) > 1 and "," in normalized:
        parts = [part.strip() for part in normalized.split(",") if part.strip()]
        parsers = {
            "move_in_date": extract_move_in_date,
            "people_on_lease": parse_int_from_text,
            "adults_in_unit": parse_int_from_text,
            "kids_in_unit": parse_int_from_text,
            "family_gross_income": extract_income_value,
            "occupation": normalize_whitespace,
            "resident_status": extract_resident_status,
            "working_with_agent": extract_agent_answer,
            "phone_number": lambda value: extract_phone_number(value) or normalize_whitespace(value),
        }
        for index, key in enumerate(missing_keys):
            if index >= len(parts):
                break
            parser = parsers.get(key, normalize_whitespace)
            value = compact(parser(parts[index]))
            if value:
                answers[key] = value
        return {k: v for k, v in answers.items() if compact(v)}

    key = missing_keys[0]
    if key in ("people_on_lease", "adults_in_unit"):
        if looks_like_date_only_answer(query):
            count = ""
        else:
            count = parse_people_count(query) if key == "people_on_lease" else parse_int_from_text(query)
        if count != "":
            answers[key] = count
    elif key == "kids_in_unit":
        kids_match = re.search(r"(\d+)\s+(?:kid|kids|child|children)\b", lowered)
        if kids_match:
            answers[key] = kids_match.group(1)
        elif re.search(r"\bno kids\b|\bnone\b", lowered):
            answers[key] = "0"
        elif "adult" not in lowered:
            count = parse_int_from_text(query)
            if count != "":
                answers[key] = count
    elif key == "move_in_date":
        answers[key] = extract_move_in_date(query)
    elif key == "family_gross_income":
        value = extract_income_value(query)
        if value:
            answers[key] = value
    elif key == "occupation":
        if is_plausible_occupation(normalized):
            answers[key] = normalized
    elif key == "resident_status":
        value = extract_resident_status(query)
        if value:
            answers[key] = value
    elif key == "working_with_agent":
        agent_answer = extract_agent_answer(query)
        if agent_answer in ("Yes", "No"):
            answers[key] = agent_answer
    elif key == "phone_number":
        answers[key] = extract_phone_number(query) or normalized

    return {k: v for k, v in answers.items() if compact(v)}


def build_missing_field_prompt(keys: List[str], answers: Optional[dict] = None) -> str:
    answers = answers or {}
    if not keys:
        return "Could you share that detail?"
    key = keys[0]
    adults = compact(answers.get("adults_in_unit"))
    kids = compact(answers.get("kids_in_unit"))
    if key == "move_in_date" and adults:
        household = f"{adults} adult{'s' if adults != '1' else ''}"
        if kids:
            household += f" and {kids} kid{'s' if kids != '1' else ''}"
        return f"Got it — {household}. What's your expected move-in date?"
    if key == "people_on_lease" and compact(answers.get("move_in_date")):
        return (
            f"Got it on move-in ({compact(answers['move_in_date'])}). "
            "How many people will be on the lease?"
        )
    if key == "kids_in_unit" and compact(answers.get("adults_in_unit")):
        return "Thanks. How many kids will be living in the unit?"
    if key == "adults_in_unit" and compact(answers.get("people_on_lease")) == "1":
        return "Got it — just you. How many adults will be living in the unit?"
    if key == "family_gross_income" and compact(answers.get("adults_in_unit")):
        return "Thanks. What's your total family gross income? Please do not include cash income."
    if key == "occupation" and compact(answers.get("family_gross_income")):
        return "Thanks. What do you do for work?"
    if key == "working_with_agent" and compact(answers.get("resident_status")):
        return "Thanks. Are you currently working with an agent?"
    if key == "phone_number":
        return "Last one — what's the best phone number to reach you on?"
    by_key = {step["key"]: step["prompt"] for step in QUALIFICATION_STEPS}
    return by_key.get(key, "Could you share that detail?")


def parse_batch_answers(batch_index: int, query: str) -> Dict[str, str]:
    text = compact(query)
    normalized = normalize_whitespace(text)
    lowered = normalized.lower()
    lines = [normalize_whitespace(line) for line in re.split(r"[\n\r]+", text) if normalize_whitespace(line)]
    cleaned_lines = [re.sub(r"^\s*(?:\d+[\).\-\:]\s*|[-*]\s*)", "", line).strip() for line in lines]
    answers: Dict[str, str] = {}

    if batch_index == 0:
        if len(cleaned_lines) >= 2:
            answers["move_in_date"] = cleaned_lines[0]
            answers["people_on_lease"] = parse_int_from_text(cleaned_lines[1])
            return {k: v for k, v in answers.items() if compact(v)}

        if "," in normalized:
            parts = [part.strip() for part in normalized.split(",") if part.strip()]
            if len(parts) >= 2:
                answers["move_in_date"] = parts[0]
                answers["people_on_lease"] = parse_people_count(parts[1])

        and_parts = re.split(r"\s+and\s+", normalized, maxsplit=1, flags=re.I)
        if len(and_parts) == 2:
            answers["move_in_date"] = extract_move_in_date(and_parts[0])
            answers["people_on_lease"] = parse_people_count(and_parts[1])

        if not answers.get("move_in_date") and normalized:
            answers["move_in_date"] = extract_move_in_date(normalized)

        people_match = re.search(r"(\d+)\s+(?:people|person|tenant|tenants).*(?:lease|living|unit)?", lowered)
        if people_match:
            answers["people_on_lease"] = people_match.group(1)
        elif not answers.get("people_on_lease"):
            if not looks_like_date_only_answer(normalized):
                people_count = parse_people_count(normalized)
                if people_count:
                    answers["people_on_lease"] = people_count

        answers.update(parse_household_from_text(normalized))

    elif batch_index == 1:
        if len(cleaned_lines) >= 2:
            answers["adults_in_unit"] = parse_int_from_text(cleaned_lines[0])
            answers["kids_in_unit"] = parse_int_from_text(cleaned_lines[1])
            return {k: v for k, v in answers.items() if compact(v)}

        if "," in normalized:
            parts = [part.strip() for part in normalized.split(",") if part.strip()]
            if len(parts) >= 2:
                answers["adults_in_unit"] = parse_int_from_text(parts[0])
                answers["kids_in_unit"] = parse_int_from_text(parts[1])

        adults_match = re.search(r"(\d+)\s+adult", lowered)
        kids_match = re.search(r"(\d+)\s+(?:kid|kids|child|children)", lowered)
        if adults_match:
            answers["adults_in_unit"] = adults_match.group(1)
        if kids_match:
            answers["kids_in_unit"] = kids_match.group(1)
        elif "no kids" in lowered:
            answers["kids_in_unit"] = "0"
        elif adults_match and re.search(r"^\d+\s+adults?\b", lowered):
            answers["kids_in_unit"] = "0"
        elif any(phrase in lowered for phrase in ("just me", "only me", "me only", "by myself")):
            answers["adults_in_unit"] = "1"
            answers["kids_in_unit"] = "0"
        elif parse_int_from_text(normalized) == "1" and "me" in lowered:
            answers["adults_in_unit"] = "1"
            answers["kids_in_unit"] = "0"

    elif batch_index == 2:
        if len(cleaned_lines) >= 2:
            answers["family_gross_income"] = extract_income_value(cleaned_lines[0])
            answers["occupation"] = cleaned_lines[1]
            return {k: v for k, v in answers.items() if compact(v)}

        if "," in normalized:
            parts = [part.strip() for part in normalized.split(",") if part.strip()]
            if len(parts) >= 2:
                answers["family_gross_income"] = extract_income_value(parts[0])
                answers["occupation"] = parts[1]

        if not answers.get("family_gross_income"):
            income_value = extract_income_value(normalized)
            if income_value:
                answers["family_gross_income"] = income_value

        if normalized and not answers.get("occupation"):
            occupation_text = normalized
            income_value = compact(answers.get("family_gross_income"))
            if income_value:
                occupation_text = re.sub(re.escape(income_value), "", occupation_text, flags=re.I).strip(" ,.-")
                occupation_text = re.sub(r"\$?\d[\d,]*\s*[kK]\b", "", occupation_text).strip(" ,.-")
            if is_plausible_occupation(occupation_text):
                answers["occupation"] = occupation_text

    elif batch_index == 3:
        if "," in normalized:
            parts = [part.strip() for part in normalized.split(",") if part.strip()]
            if len(parts) >= 2:
                status = extract_resident_status(parts[0])
                if status:
                    answers["resident_status"] = status
                agent_answer = extract_agent_answer(parts[1])
                if agent_answer in ("Yes", "No"):
                    answers["working_with_agent"] = agent_answer
                return {k: v for k, v in answers.items() if compact(v)}

        if len(cleaned_lines) >= 2:
            status = extract_resident_status(cleaned_lines[0])
            if status:
                answers["resident_status"] = status
            agent_answer = extract_agent_answer(cleaned_lines[1])
            if agent_answer in ("Yes", "No"):
                answers["working_with_agent"] = agent_answer
            return {k: v for k, v in answers.items() if compact(v)}

        answers.update(parse_resident_and_agent(normalized))

    elif batch_index == 4:
        if cleaned_lines:
            answers["phone_number"] = extract_phone_number(cleaned_lines[0]) or cleaned_lines[0]
        else:
            phone = extract_phone_number(normalized)
            if phone:
                answers["phone_number"] = phone
            elif normalized:
                answers["phone_number"] = normalized

    return {k: v for k, v in answers.items() if compact(v)}

def summarize_shared_listing(draft: dict) -> str:
    title = compact(draft.get("MarketplaceTitle")) or compact(draft.get("Address")) or "Listing"
    price = compact(draft.get("MarketplacePriceDisplay")) or compact(draft.get("MarketplacePrice"))
    city = compact(draft.get("City"))
    details: List[str] = []
    if price:
        details.append(price)
    if city:
        details.append(city)
    detail_suffix = f" ({', '.join(details)})" if details else ""
    return f"{title}{detail_suffix}"


def listings_from_session(session: dict, drafts: List[dict]) -> List[dict]:
    by_key = {compact(draft.get("ListingKey")): draft for draft in drafts if compact(draft.get("ListingKey"))}
    return [by_key[key] for key in session.get("last_shared_listing_keys", []) if key in by_key]


def draft_by_listing_key(drafts: List[dict], listing_key: str) -> Optional[dict]:
    listing_key = compact(listing_key)
    if not listing_key:
        return None
    for draft in drafts:
        if compact(draft.get("ListingKey")) == listing_key:
            return draft
    return None


def resolve_listing_reference(query: str, session: dict, drafts: List[dict]) -> Optional[dict]:
    shared = listings_from_session(session, drafts)
    if not shared:
        return None

    q = query.lower()
    ordinal_words = (
        ("third", 2),
        ("3rd", 2),
        ("second", 1),
        ("2nd", 1),
        ("first", 0),
        ("1st", 0),
    )
    for word, index in ordinal_words:
        if word in q and index < len(shared):
            if any(token in q for token in ("one", "listing", "option", "that", "this", "here")) or len(shared) > 1:
                return shared[index]

    price_match = re.search(r"\$?\s*(\d{3,5})", q.replace(",", ""))
    if price_match:
        target = price_match.group(1)
        for listing in shared:
            price = (compact(listing.get("MarketplacePriceDisplay")) or compact(listing.get("MarketplacePrice"))).replace(",", "")
            if target in re.sub(r"[^\d]", "", price):
                return listing

    city_matches = []
    for listing in shared:
        city = compact(listing.get("City"))
        title = compact(listing.get("MarketplaceTitle"))
        address = compact(listing.get("Address"))
        listing_key = compact(listing.get("ListingKey"))
        haystacks = [city, title, address, listing_key]
        if any(token and token.lower() in q for token in haystacks):
            city_matches.append(listing)
    if len(city_matches) == 1:
        return city_matches[0]

    return None


def looks_like_listing_detail_request(query: str) -> bool:
    q = query.lower()
    phrases = (
        "tell me about",
        "more about",
        "more details",
        "details on",
        "details about",
        "what about",
        "the second",
        "the first",
        "the third",
        "second one",
        "first one",
        "third one",
        "that one",
        "this one",
        "listing here",
        "what are you talking about",
        "i'm saying",
        "im saying",
        "not the third",
        "not the first",
        "not the second",
    )
    return any(phrase in q for phrase in phrases) or bool(re.search(r"\$\s*\d{3,5}", q))


def looks_like_booking_confirmation(query: str, session: dict) -> bool:
    if not looks_like_affirmative(query):
        return False
    if compact(session.get("selected_listing_key")):
        return True
    last = compact(session.get("last_prompt")).lower()
    return bool(session.get("pending_booking_offer")) or any(
        phrase in last for phrase in ("viewing", "book a", "schedule", "booking", "move forward", "next step")
    )


def user_asking_booking_options(query: str, session: dict) -> bool:
    q = query.lower()
    if "options" not in q:
        return False
    return bool(compact(session.get("selected_listing_key"))) or bool(session.get("pending_booking_offer"))


def validate_household_counts(answers: dict) -> None:
    people = compact(answers.get("people_on_lease"))
    adults = compact(answers.get("adults_in_unit"))
    kids = compact(answers.get("kids_in_unit"))
    if people.isdigit() and adults.isdigit() and int(adults) > int(people):
        answers["adults_in_unit"] = people
    people = compact(answers.get("people_on_lease"))
    adults = compact(answers.get("adults_in_unit"))
    kids = compact(answers.get("kids_in_unit"))
    if people.isdigit() and adults.isdigit() and kids.isdigit():
        overflow = int(adults) + int(kids) - int(people)
        if overflow > 0:
            answers["kids_in_unit"] = str(max(0, int(kids) - overflow))


def build_calendly_booking_reply(
    session: dict,
    calendly_url: str,
    drafts: List[dict],
) -> str:
    if not calendly_url:
        return "I can help with that — Nabeel's booking link isn't set up yet, but I've noted your interest."
    listing_key = compact(session.get("selected_listing_key"))
    listing = draft_by_listing_key(drafts, listing_key) if listing_key else None
    if listing:
        summary = summarize_shared_listing(listing)
        return (
            f"Perfect — pick a time here: {calendly_url}\n"
            f"Please mention {summary} (ListingKey {listing_key}) in the notes."
        )
    return (
        f"Perfect — book here: {calendly_url}\n"
        "Add the address or ListingKey for the unit you want in the notes."
    )


def format_listing_detail_short(draft: dict, position: Optional[int] = None) -> str:
    ctx = listing_context(draft)
    prefix = f"That's option {position} from the list — " if position else ""
    title = summarize_shared_listing(draft)
    bits = [prefix + title + "."]
    for field in ("Address", "BedroomsTotal", "BathroomsTotal", "LivingAreaRange", "PetsAllowed", "MarketplaceDescription"):
        value = compact(ctx.get(field))
        if value:
            bits.append(f"{field.replace('Total', '').replace('Marketplace', '')}: {value}.")
    bits.append("Want to book a viewing?")
    return " ".join(bits)


def generate_listing_detail_reply(
    query: str,
    listing: dict,
    api_key: str,
    model: str,
    agent_name: str,
    position: Optional[int] = None,
) -> str:
    listing_data = listing_context(listing)
    listing_data["list_position"] = position
    system_prompt = (
        "You're Nabeel's assistant at Durham New Homes. "
        "Answer using ONLY listing_data for the listing the user asked about. "
        "2-3 short sentences. Don't mention other listings. Don't invent details."
    )
    user_prompt = {
        "user_message": query,
        "agent_name": agent_name,
        "listing_data": listing_data,
    }
    if api_key:
        try:
            payload = {
                "model": model,
                "input": [
                    {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                    {"role": "user", "content": [{"type": "input_text", "text": json.dumps(user_prompt, ensure_ascii=False)}]},
                ],
                "max_output_tokens": 220,
            }
            resp = requests.post(
                OPENAI_RESPONSES_API,
                headers=build_openai_headers(api_key),
                json=payload,
                timeout=int(os.getenv("OPENAI_REQUEST_TIMEOUT_SECONDS", "20") or "20"),
            )
            resp.raise_for_status()
            reply = compact(extract_response_text(resp.json()))
            if reply:
                return reply
        except Exception as exc:
            print(f"AI listing detail failed: {exc}")
    return format_listing_detail_short(listing, position=position)


def handle_qualified_listing_interest(
    session: dict,
    query: str,
    drafts: List[dict],
    calendly_url: str,
    openai_api_key: str,
    openai_model: str,
    agent_name: str,
) -> Optional[str]:
    if user_asking_booking_options(query, session):
        session["pending_booking_offer"] = False
        return build_calendly_booking_reply(session, calendly_url, drafts)

    if looks_like_booking_confirmation(query, session):
        session["pending_booking_offer"] = False
        return build_calendly_booking_reply(session, calendly_url, drafts)

    shared = listings_from_session(session, drafts)
    listing = resolve_listing_reference(query, session, drafts)
    if not listing and compact(session.get("selected_listing_key")):
        if (
            looks_like_listing_detail_request(query)
            or looks_like_booking_request(query)
            or looks_like_booking_confirmation(query, session)
            or user_asking_booking_options(query, session)
        ):
            listing = draft_by_listing_key(drafts, compact(session.get("selected_listing_key")))

    if listing or (shared and looks_like_listing_detail_request(query)):
        target = listing
        if not target and shared:
            if "second" in query.lower() and len(shared) > 1:
                target = shared[1]
            elif "third" in query.lower() and len(shared) > 2:
                target = shared[2]
            elif "first" in query.lower():
                target = shared[0]
        if target:
            listing_key = compact(target.get("ListingKey"))
            session["selected_listing_key"] = listing_key
            position = shared.index(target) + 1 if target in shared else None
            reply = generate_listing_detail_reply(
                query,
                target,
                openai_api_key,
                openai_model,
                agent_name,
                position=position,
            )
            if any(word in reply.lower() for word in ("viewing", "book", "schedule")):
                session["pending_booking_offer"] = True
            return reply

    if looks_like_booking_request(query) and compact(session.get("selected_listing_key")):
        session["pending_booking_offer"] = False
        return build_calendly_booking_reply(session, calendly_url, drafts)

    return None


def build_post_qualification_reply(
    session: dict,
    drafts: List[dict],
    listing_doc_url: str,
) -> str:
    validate_household_counts(session.setdefault("answers", {}))
    summary = format_lead_summary(session.get("answers", {}))
    search_query = compact(session.get("search_query"))
    matches = rank_drafts(search_query, drafts, limit=3) if search_query else []
    session["last_shared_listing_keys"] = [compact(match.get("ListingKey")) for match in matches if compact(match.get("ListingKey"))]

    intro = "Perfect, I’ve got everything I need and I’ll use this to narrow down the best active listings for you."
    if summary:
        intro += f"\n\nHere’s what I collected:\n{summary}"

    if not matches:
        return (
            intro
            + "\n\nI don’t see any active listings that match closely enough right now. "
            "If you want, I can help refine the area, budget, or unit type and keep an eye out for new options as they come up."
        )

    lines = [intro, "", "Here are a few active options that look relevant:"]
    for match in matches:
        lines.append(f"- {summarize_shared_listing(match)}")
    lines.append("")
    lines.append(
        "If one stands out, send me the address or ListingKey and I'll help you move forward with the next step."
    )
    return "\n".join(lines)


def handle_post_qualification_booking(
    session: dict,
    query: str,
    drafts: List[dict],
    calendly_url: str,
) -> Optional[str]:
    if not session.get("qualified"):
        return None
    if not looks_like_booking_request(query):
        return None
    if not calendly_url:
        return "I can help you move forward on that. Nabeel’s booking link is not configured yet, but I can still note your interest."

    last_shared_keys = [key for key in session.get("last_shared_listing_keys", []) if key]
    if last_shared_keys:
        visible = {compact(draft.get("ListingKey")): draft for draft in customer_visible_drafts(drafts)}
        matches = [visible[key] for key in last_shared_keys if key in visible]
    else:
        search_query = compact(session.get("search_query"))
        matches = rank_drafts(search_query, drafts, limit=3) if search_query else []
    ready_matches = shortlist_for_booking(matches)
    if ready_matches:
        if len(ready_matches) == 1:
            listing = summarize_shared_listing(ready_matches[0])
            return (
                f"Perfect. For {listing}, you can book a time with Nabeel here: {calendly_url} "
                "When you book, please add the property address or ListingKey in the notes so we can prepare properly."
            )
        options = "; ".join(summarize_shared_listing(match) for match in ready_matches[:3])
        return (
            "I can help with that. I found a few matching listings tied to your message: "
            f"{options}. Reply with the address or ListingKey you want, and I’ll point you to the right next step."
        )

    last_shared = [key for key in session.get("last_shared_listing_keys", []) if key]
    if len(last_shared) == 1:
        return (
            f"Perfect. You can book a time with Nabeel here: {calendly_url} "
            f"When you book, please add this ListingKey in the notes: {last_shared[0]}."
        )
    if len(last_shared) > 1:
        return (
            "I can help with that. Please send me the address or ListingKey for the unit you want, "
            "and then I’ll share the booking link for the next step."
        )
    return (
        "I can help with that. Send me the address or ListingKey for the listing you want to move forward with, "
        "and I’ll share the booking link."
    )


def wants_listing_refresh(query: str) -> bool:
    q = query.lower()
    if looks_like_booking_request(query):
        return False
    refresh_markers = [
        "show me listings",
        "show listings",
        "send me listings",
        "listings in",
        "i want you to show",
        "want you to show",
        "show me options",
        "show me places",
        "find me listings",
        "search in",
    ]
    if any(marker in q for marker in refresh_markers):
        return True
    return "listings" in q and any(token in q for token in ("ontario", "toronto", "in ", "area", "city"))


def handle_qualified_listing_search(
    session: dict,
    query: str,
    drafts: List[dict],
    openai_api_key: str = "",
    openai_model: str = DEFAULT_OPENAI_MODEL,
    use_ai: bool = True,
) -> Optional[str]:
    search_query = compact(query)
    should_search = False
    if looks_like_listing_detail_request(query) or resolve_listing_reference(query, session, drafts):
        return None
    if use_ai and openai_api_key:
        interpretation = ai_interpret_qualified_message(
            openai_api_key,
            openai_model,
            query,
            session.get("answers", {}),
            compact(session.get("search_query")),
        )
        intent = compact(interpretation.get("intent")).lower()
        if intent == "search_listings":
            should_search = True
            search_query = compact(interpretation.get("search_query")) or search_query
        elif intent in ("booking", "general_question", "other"):
            return None
        else:
            should_search = wants_listing_refresh(query) or looks_like_search_refinement(query)
    else:
        should_search = wants_listing_refresh(query) or looks_like_search_refinement(query)

    if not should_search:
        return None

    listing_reply = build_qualified_listing_reply(session, search_query, drafts)
    return listing_reply


def should_start_qualification(query: str, calendly_url: str) -> bool:
    q = query.lower()
    if wants_listing_help(query):
        return True
    if "send me" in q and "listing" in q:
        return True
    if "help me" in q and ("find" in q or "search" in q):
        return True
    return False


def maybe_handle_qualification(
    sender_id: str,
    query: str,
    lead_state_path: Path,
    agent_name: str,
    calendly_url: str,
    drafts: List[dict],
    listing_doc_url: str,
    openai_api_key: str = "",
    openai_model: str = DEFAULT_OPENAI_MODEL,
    use_ai: bool = True,
) -> Optional[str]:
    def _handle(session: dict, state: dict) -> Optional[str]:
        return _maybe_handle_qualification_locked(
            session,
            state,
            query,
            lead_state_path,
            agent_name,
            calendly_url,
            drafts,
            listing_doc_url,
            openai_api_key,
            openai_model,
            use_ai,
        )

    return with_lead_session(sender_id, lead_state_path, _handle)


def _maybe_handle_qualification_locked(
    session: dict,
    state: dict,
    query: str,
    lead_state_path: Path,
    agent_name: str,
    calendly_url: str,
    drafts: List[dict],
    listing_doc_url: str,
    openai_api_key: str,
    openai_model: str,
    use_ai: bool,
) -> Optional[str]:
    if qualification_in_progress(session) and not session.get("qualified"):
        begin_structured_qualification(session, query)
        reply = apply_qualification_turn(
            session,
            query,
            drafts,
            listing_doc_url,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
        )
        session["last_prompt"] = reply
        persist_lead_state(lead_state_path, state)
        return reply

    if session.get("active"):
        answers = session.setdefault("answers", {})
        reply = apply_qualification_turn(
            session,
            query,
            drafts,
            listing_doc_url,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
        )
        session["last_prompt"] = reply
        persist_lead_state(lead_state_path, state)
        return reply

    booking_reply = handle_post_qualification_booking(session, query, drafts, calendly_url)
    if booking_reply:
        persist_lead_state(lead_state_path, state)
        return booking_reply

    if session.get("qualified"):
        listing_reply = handle_qualified_listing_search(
            session,
            query,
            drafts,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
            use_ai=use_ai,
        )
        if listing_reply:
            persist_lead_state(lead_state_path, state)
            return listing_reply

    if session.get("awaiting_opt_in"):
        last_assistant_message = compact(session.get("last_prompt"))
        if use_ai and openai_api_key:
            interpretation = ai_interpret_opt_in_message(
                openai_api_key,
                openai_model,
                query,
                agent_name,
                search_query=compact(session.get("search_query")),
                last_assistant_message=last_assistant_message,
            )
            if interpretation.get("accepted") is True:
                session["awaiting_opt_in"] = False
                session["active"] = True
                session["step"] = 0
                session["batch"] = 0
                session["qualified"] = False
                session["answers"] = {}
                session["raw_answers"] = {}
                session["last_shared_listing_keys"] = []
                updated_search = compact(interpretation.get("updated_search_query"))
                if updated_search:
                    session["search_query"] = updated_search
                session["last_prompt"] = QUALIFICATION_BATCHES[0]["prompt"]
                persist_lead_state(lead_state_path, state)
                return QUALIFICATION_BATCHES[0]["prompt"]

            updated_search = compact(interpretation.get("updated_search_query"))
            if updated_search:
                session["search_query"] = updated_search

            ai_reply = compact(interpretation.get("reply"))
            if ai_reply:
                session["last_prompt"] = ai_reply
                persist_lead_state(lead_state_path, state)
                return ai_reply

        if looks_like_affirmative(query):
            session["awaiting_opt_in"] = False
            session["active"] = True
            session["step"] = 0
            session["batch"] = 0
            session["qualified"] = False
            session["answers"] = {}
            session["raw_answers"] = {}
            session["last_shared_listing_keys"] = []
            session["last_prompt"] = QUALIFICATION_BATCHES[0]["prompt"]
            persist_lead_state(lead_state_path, state)
            return QUALIFICATION_BATCHES[0]["prompt"]

        if wants_listing_help(query):
            session["search_query"] = compact(query)
            persist_lead_state(lead_state_path, state)
            summary = describe_search_preferences(query)
            if summary:
                return (
                    f"Got it — {summary}. "
                    "Just reply yes when you're ready and I'll ask a few quick questions."
                )
            return "Got it. Just reply yes when you're ready and I'll ask a few quick questions."

        persist_lead_state(lead_state_path, state)
        return local_conversational_fallback(
            "awaiting_opt_in",
            query,
            agent_name,
            last_assistant_message=last_assistant_message,
            search_query=compact(session.get("search_query")),
        )

    if should_start_qualification(query, calendly_url) and not session.get("qualified"):
        session["awaiting_opt_in"] = True
        session["active"] = False
        session["step"] = 0
        session["batch"] = 0
        session["qualified"] = False
        session["answers"] = {}
        session["raw_answers"] = {}
        session["search_query"] = compact(query)
        session["last_shared_listing_keys"] = []
        intro = qualification_opt_in_prompt(agent_name, describe_search_preferences(query))
        session["last_prompt"] = intro
        persist_lead_state(lead_state_path, state)
        return intro

    if (
        use_ai
        and openai_api_key
        and not session.get("qualified")
        and not session.get("active")
        and not session.get("awaiting_opt_in")
    ):
        intent = ai_detect_search_intent(openai_api_key, openai_model, query)
        if intent.get("wants_listing_help") is True:
            search_query = compact(intent.get("search_query")) or compact(query)
            session["awaiting_opt_in"] = True
            session["active"] = False
            session["step"] = 0
            session["batch"] = 0
            session["qualified"] = False
            session["answers"] = {}
            session["raw_answers"] = {}
            session["search_query"] = search_query
            session["last_shared_listing_keys"] = []
            intro = qualification_opt_in_prompt(agent_name, describe_search_preferences(search_query))
            session["last_prompt"] = intro
            persist_lead_state(lead_state_path, state)
            return intro

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


def parse_json_object(text: str) -> dict:
    text = compact(text)
    if not text:
        return {}
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {}
        try:
            payload = json.loads(match.group(0))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}


def call_openai_json(api_key: str, model: str, system_prompt: str, user_payload: dict, max_tokens: int = 400) -> dict:
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {
                "role": "user",
                "content": [{"type": "input_text", "text": json.dumps(user_payload, ensure_ascii=False)}],
            },
        ],
        "max_output_tokens": max_tokens,
    }
    resp = requests.post(
        OPENAI_RESPONSES_API,
        headers=build_openai_headers(api_key),
        json=payload,
        timeout=int(os.getenv("OPENAI_REQUEST_TIMEOUT_SECONDS", "25") or "25"),
    )
    resp.raise_for_status()
    return parse_json_object(extract_response_text(resp.json()))


def normalize_qualification_value(key: str, value: str) -> str:
    value = compact(value)
    if not value:
        return ""
    if key in COUNT_FIELDS:
        count = parse_int_from_text(value)
        return count if count != "" else value
    if key == "family_gross_income":
        return extract_income_value(value) or value
    if key == "resident_status":
        return extract_resident_status(value) or value
    if key == "working_with_agent":
        agent_answer = extract_agent_answer(value)
        return agent_answer if agent_answer in ("Yes", "No") else value
    if key == "phone_number":
        return extract_phone_number(value) or value
    return value


def unfilled_qualification_fields(answers: dict) -> Dict[str, str]:
    unfilled: Dict[str, str] = {}
    for step in QUALIFICATION_STEPS:
        key = step["key"]
        if not compact(answers.get(key)):
            unfilled[key] = step["prompt"]
    return unfilled


def first_incomplete_batch_index(answers: dict) -> int:
    for index, batch in enumerate(QUALIFICATION_BATCHES):
        if any(not compact(answers.get(key)) for key in batch["keys"]):
            return index
    return len(QUALIFICATION_BATCHES)


def first_missing_qualification_key(answers: dict) -> str:
    for key in QUALIFICATION_FIELD_KEYS:
        if not compact(answers.get(key)):
            return key
    return ""


def is_plausible_field_value(key: str, value: str, query: str, existing_answers: Optional[dict] = None) -> bool:
    value = compact(value)
    query = compact(query)
    if not value or not query:
        return False

    lowered = query.lower()
    existing_answers = existing_answers or {}

    if key == "family_gross_income":
        if value in {"1", "2", "3", "4", "5"} and any(
            token in lowered for token in ("adult", "kid", "people", "person", "lease")
        ):
            return False
        digits = re.sub(r"\D", "", value)
        if "k" in lowered:
            return bool(digits)
        if digits.isdigit() and int(digits) < 1000:
            return False
        return bool(re.search(r"\d", value))
    if key == "occupation":
        if value.lower() in {"adult", "adults", "kid", "kids", "people", "person", "tenant", "tenants"}:
            return False
        if value.lower() in {"yes", "no", "y", "n", "yup", "sure", "ok", "okay"}:
            return False
        if re.fullmatch(r"\$?\d+[kK]?", value):
            return False
        return is_plausible_occupation(value)
    if key == "phone_number":
        if value.lower() in {"yes", "no", "yup", "sure", "ok", "okay"}:
            return False
        if re.search(r"[a-zA-Z]", re.sub(r"[\s\-\(\)\+]", "", value)):
            return False
        digits = re.sub(r"\D", "", value)
        return len(digits) >= 10
    if key == "working_with_agent":
        if value in ("Yes", "No"):
            return True
        agent_answer = extract_agent_answer(query)
        if agent_answer in ("Yes", "No"):
            return True
    if key == "adults_in_unit" and compact(existing_answers.get("adults_in_unit")):
        return False
    if key == "kids_in_unit" and compact(existing_answers.get("kids_in_unit")):
        return False
    if key == "people_on_lease" and "adult" in lowered and "people" not in lowered and "person" not in lowered:
        return False
    if key == "adults_in_unit" and ("people" in lowered or "person" in lowered) and "adult" not in lowered:
        return False
    people_on_lease = compact(existing_answers.get("people_on_lease"))
    if key == "adults_in_unit" and people_on_lease.isdigit() and value.isdigit():
        if int(value) > int(people_on_lease):
            return False
    adults_in_unit = compact(existing_answers.get("adults_in_unit"))
    if key == "people_on_lease" and adults_in_unit.isdigit() and value.isdigit():
        if int(adults_in_unit) > int(value):
            return False
    return True


OVERWRITABLE_QUALIFICATION_FIELDS = {"resident_status", "working_with_agent"}


def merge_parsed_answers(
    answers: dict,
    parsed_answers: Dict[str, str],
    query: str,
    allowed_keys: Optional[Iterable[str]] = None,
) -> None:
    allowed = set(allowed_keys) if allowed_keys is not None else None
    for key, value in parsed_answers.items():
        if allowed is not None and key not in allowed:
            continue
        normalized = normalize_qualification_value(key, compact(value))
        if not normalized or not is_plausible_field_value(key, normalized, query, answers):
            continue
        can_write = not compact(answers.get(key)) or key in OVERWRITABLE_QUALIFICATION_FIELDS
        if looks_like_correction(query) and key in {"resident_status", "working_with_agent", "occupation"}:
            can_write = True
        if can_write:
            answers[key] = normalized
    validate_household_counts(answers)


def ai_extract_qualification_fields(
    api_key: str,
    model: str,
    user_message: str,
    existing_answers: dict,
    target_fields: Dict[str, str],
    last_prompt: str = "",
    search_query: str = "",
) -> Tuple[Dict[str, str], str]:
    if not api_key or not target_fields:
        return {}, ""

    system_prompt = (
        "Extract rental lead details from the latest message. Return JSON only:\n"
        '{"fields": {"field_key": "value"}, "follow_up": "string"}\n'
        "Only use keys from target_fields. Only fill what's clearly stated. "
        "working_with_agent: Yes or No. Adults only → kids_in_unit=0. "
        "follow_up: one short natural question for anything still missing; empty if done."
    )
    user_payload = {
        "user_message": user_message,
        "search_query": search_query,
        "existing_answers": existing_answers,
        "target_fields": target_fields,
        "last_assistant_prompt": last_prompt,
    }
    try:
        result = call_openai_json(api_key, model, system_prompt, user_payload)
    except Exception as exc:
        print(f"AI qualification extraction failed: {exc}")
        return {}, ""

    fields = result.get("fields", {})
    if not isinstance(fields, dict):
        fields = {}
    cleaned = {
        key: normalize_qualification_value(key, compact(value))
        for key, value in fields.items()
        if key in target_fields
        and compact(value)
        and is_plausible_field_value(key, normalize_qualification_value(key, compact(value)), user_message, existing_answers)
    }
    return cleaned, compact(result.get("follow_up"))


def ai_interpret_opt_in_message(
    api_key: str,
    model: str,
    user_message: str,
    agent_name: str,
    search_query: str = "",
    last_assistant_message: str = "",
) -> dict:
    if not api_key:
        return {}
    system_prompt = (
        "You interpret messages while a rental lead is deciding whether to opt in to qualification. "
        "Return JSON only:\n"
        '{"accepted": boolean, "updated_search_query": "string", "reply": "string"}\n'
        "Rules:\n"
        "- accepted=true when the user clearly agrees to proceed (yes, sure, sounds good, go ahead, please, "
        "why not, interested, okay, etc.), including informal or partial phrasing.\n"
        "- updated_search_query: concise rental search phrase if the user states or refines what they want; else empty.\n"
        "- reply: a warm, natural, concise human reply for greetings, small talk, questions, or hesitation.\n"
        "- Answer small talk naturally (for example respond to 'how are you').\n"
        "- Gently invite them to say yes when they want listing help, without sounding robotic.\n"
        "- Never repeat last_assistant_message verbatim or near-verbatim.\n"
        "- Do not ask qualification questions yet; only handle opt-in and conversation.\n"
        "- If accepted=true, reply may be empty."
    )
    user_payload = {
        "user_message": user_message,
        "agent_name": agent_name,
        "search_query": search_query,
        "last_assistant_message": last_assistant_message,
    }
    try:
        return call_openai_json(api_key, model, system_prompt, user_payload, max_tokens=300)
    except Exception as exc:
        print(f"AI opt-in interpretation failed: {exc}")
        return {}


def ai_generate_conversational_reply(
    api_key: str,
    model: str,
    user_message: str,
    agent_name: str,
    conversation_stage: str,
    search_query: str = "",
    last_assistant_message: str = "",
) -> str:
    if not api_key:
        return ""
    system_prompt = (
        "You are Durham New Homes, a leasing assistant for Nabeel. "
        "Reply like a capable human leasing coordinator: warm, concise, natural, and practical. "
        "Never mention internal workflow labels, spreadsheets, packets, or back-office terms. "
        "Do not invent listings, pricing, or availability. "
        "Avoid sounding robotic or repetitive. "
        "Never repeat last_assistant_message verbatim or near-verbatim."
    )
    stage_guidance = {
        "new": (
            "The user has not started the listing flow yet. "
            "Greet naturally and invite them to share area, budget, unit type, or what they are looking for."
        ),
        "pre_qual": (
            "The user is exploring rentals but has not completed qualification. "
            "Help conversationally and guide them toward sharing search preferences."
        ),
        "awaiting_opt_in": (
            "The user was offered listing help and a few quick qualification questions first. "
            "Respond naturally to greetings and small talk. "
            "Gently invite them to say yes when they want to proceed — vary your wording."
        ),
    }
    user_prompt = {
        "user_message": user_message,
        "agent_name": agent_name,
        "conversation_stage": conversation_stage,
        "stage_guidance": stage_guidance.get(conversation_stage, ""),
        "search_query": search_query,
        "last_assistant_message": last_assistant_message,
    }
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": json.dumps(user_prompt, ensure_ascii=False)}]},
        ],
        "max_output_tokens": 250,
    }
    try:
        resp = requests.post(
            OPENAI_RESPONSES_API,
            headers=build_openai_headers(api_key),
            json=payload,
            timeout=int(os.getenv("OPENAI_REQUEST_TIMEOUT_SECONDS", "20") or "20"),
        )
        resp.raise_for_status()
        return compact(extract_response_text(resp.json()))
    except Exception as exc:
        print(f"AI conversational reply failed: {exc}")
        return ""


def ai_detect_search_intent(
    api_key: str,
    model: str,
    user_message: str,
) -> dict:
    if not api_key:
        return {}
    system_prompt = (
        "Detect whether a Messenger user wants help finding rental listings. "
        'Return JSON only: {"wants_listing_help": boolean, "search_query": "string"}\n'
        "wants_listing_help=true for search intent even if phrased casually or indirectly. "
        "search_query should be a concise phrase for inventory matching when true."
    )
    try:
        return call_openai_json(
            api_key,
            model,
            system_prompt,
            {"user_message": user_message},
            max_tokens=150,
        )
    except Exception as exc:
        print(f"AI search-intent detection failed: {exc}")
        return {}


def ai_interpret_qualified_message(
    api_key: str,
    model: str,
    user_message: str,
    existing_answers: dict,
    prior_search_query: str = "",
) -> dict:
    if not api_key:
        return {}
    system_prompt = (
        "You interpret messages from a qualified rental lead. Return JSON only:\n"
        '{"intent":"search_listings|booking|general_question|other","search_query":"string","reply":"string"}\n'
        "Rules:\n"
        "- Use search_listings when the user wants to see, refine, or expand listing options.\n"
        "- search_query should be a concise search phrase for inventory matching.\n"
        "- Use prior_search_query and collected answers as context.\n"
        "- reply should be empty unless intent is general_question."
    )
    user_payload = {
        "user_message": user_message,
        "prior_search_query": prior_search_query,
        "collected_answers": existing_answers,
    }
    try:
        return call_openai_json(api_key, model, system_prompt, user_payload, max_tokens=250)
    except Exception as exc:
        print(f"AI qualified-message interpretation failed: {exc}")
        return {}


def extract_qualification_from_message(
    batch_index: int,
    query: str,
    answers: dict,
    openai_api_key: str = "",
    openai_model: str = DEFAULT_OPENAI_MODEL,
    use_ai: bool = True,
    last_prompt: str = "",
    search_query: str = "",
) -> Tuple[Dict[str, str], str]:
    parsed: Dict[str, str] = {}
    follow_up_hint = ""

    batch_keys = QUALIFICATION_BATCHES[batch_index]["keys"] if batch_index < len(QUALIFICATION_BATCHES) else []
    missing_in_batch = [key for key in batch_keys if not compact(answers.get(key))]
    step_prompts = {step["key"]: step["prompt"] for step in QUALIFICATION_STEPS}
    target_fields = {key: step_prompts[key] for key in missing_in_batch}

    if use_ai and openai_api_key and target_fields:
        ai_fields, follow_up_hint = ai_extract_qualification_fields(
            openai_api_key,
            openai_model,
            query,
            answers,
            target_fields,
            last_prompt=last_prompt,
            search_query=search_query,
        )
        parsed.update(ai_fields)

    regex_fields = parse_batch_answers(batch_index, query)
    for key, value in regex_fields.items():
        if key in missing_in_batch and compact(value) and not compact(parsed.get(key)):
            parsed[key] = compact(value)

    if missing_in_batch:
        for key, value in parse_missing_fields(missing_in_batch, query).items():
            if key in missing_in_batch and compact(value) and not compact(parsed.get(key)) and not compact(answers.get(key)):
                parsed[key] = compact(value)

    normalized: Dict[str, str] = {}
    merge_parsed_answers(normalized, parsed, query, allowed_keys=missing_in_batch)
    return normalized, follow_up_hint


def conversation_stage(session: dict) -> str:
    if session.get("qualified"):
        return "qualified"
    if session.get("active"):
        return "qualifying"
    if session.get("awaiting_opt_in"):
        return "awaiting_opt_in"
    return "new"


def all_qualification_fields_complete(answers: dict) -> bool:
    return all(compact(answers.get(key)) for key in QUALIFICATION_FIELD_KEYS)


def build_qualified_listing_reply(session: dict, search_query: str, drafts: List[dict]) -> str:
    search_query = merge_search_queries(session, compact(search_query) or compact(session.get("search_query")))
    session["search_query"] = search_query
    matches = rank_drafts(search_query, drafts, limit=3)
    session["last_shared_listing_keys"] = [
        compact(match.get("ListingKey")) for match in matches if compact(match.get("ListingKey"))
    ]
    if not matches:
        return (
            "I looked again but nothing active matches that right now. "
            "Tell me the city, budget, or unit type and I'll narrow it down."
        )
    lines = ["Here are a few that fit:"]
    for match in matches:
        lines.append(f"- {summarize_shared_listing(match)}")
    lines.append("")
    lines.append("Like one? Send the address or ListingKey and I'll help with next steps.")
    return "\n".join(lines)


def extract_all_qualification_fields(
    session: dict,
    query: str,
    openai_api_key: str,
    openai_model: str,
) -> None:
    if not session.get("active") or session.get("qualified"):
        return
    answers = session.setdefault("answers", {})
    session.setdefault("raw_answers", {})[f"turn_{len(session.get('raw_answers', {})) + 1}"] = compact(query)
    step_labels = {step["key"]: step["prompt"] for step in QUALIFICATION_STEPS}

    def missing_keys() -> List[str]:
        return [key for key in QUALIFICATION_FIELD_KEYS if not compact(answers.get(key))]

    missing_before = missing_keys()
    batch_index = min(first_incomplete_batch_index(answers), max(len(QUALIFICATION_BATCHES) - 1, 0))

    if batch_index < len(QUALIFICATION_BATCHES):
        parsed = parse_batch_answers(batch_index, query)
        merge_parsed_answers(answers, parsed, query, allowed_keys=missing_before)

    merge_parsed_answers(answers, parse_household_from_text(query), query, allowed_keys=missing_before)

    move_in = extract_move_in_date(query)
    if move_in:
        merge_parsed_answers(answers, {"move_in_date": move_in}, query, allowed_keys=["move_in_date"])

    missing = missing_keys()
    if missing:
        progressed = bool(missing_before) and missing[0] != missing_before[0]
        if not progressed:
            target_keys = missing if "," in normalize_whitespace(query) else [missing[0]]
            for key, value in parse_missing_fields(target_keys, query).items():
                if compact(value):
                    merge_parsed_answers(answers, {key: value}, query, allowed_keys=[key])

    missing = missing_keys()
    if openai_api_key and missing:
        target_fields = {key: step_labels[key] for key in missing}
        ai_fields, _ = ai_extract_qualification_fields(
            openai_api_key,
            openai_model,
            query,
            answers,
            target_fields,
            last_prompt=compact(session.get("last_prompt")),
            search_query=compact(session.get("search_query")),
        )
        merge_parsed_answers(answers, ai_fields, query, allowed_keys=missing)

    infer_household_defaults(answers, query)

    if compact(answers.get("working_with_agent")) and not compact(answers.get("resident_status")):
        answers.pop("working_with_agent", None)

    resident = extract_resident_status(query)
    if resident:
        merge_parsed_answers(answers, {"resident_status": resident}, query, allowed_keys=["resident_status"])

    agent_answer = extract_agent_answer(query)
    if agent_answer in ("Yes", "No"):
        last_prompt = compact(session.get("last_prompt")).lower()
        asking_agent = (
            first_missing_qualification_key(answers) == "working_with_agent"
            or "agent" in last_prompt
            or "working with" in last_prompt
            or re.search(r"\bagent\b", query.lower())
            or re.search(r"\bworking\b", query.lower())
        )
        if asking_agent:
            merge_parsed_answers(
                answers,
                {"working_with_agent": agent_answer},
                query,
                allowed_keys=["working_with_agent"],
            )

    session["batch"] = first_incomplete_batch_index(answers)


def extract_qualification_only(
    session: dict,
    query: str,
    openai_api_key: str,
    openai_model: str,
) -> None:
    extract_all_qualification_fields(session, query, openai_api_key, openai_model)


def compute_conversation_directive(
    session: dict,
    query: str,
    agent_name: str,
    drafts: List[dict],
    calendly_url: str,
) -> dict:
    answers = session.setdefault("answers", {})
    stage = conversation_stage(session)
    missing_all = [key for key in QUALIFICATION_FIELD_KEYS if not compact(answers.get(key))]
    next_field = first_missing_qualification_key(answers) if session.get("active") else ""
    batch_index = first_incomplete_batch_index(answers) if session.get("active") else 0
    batch_keys = (
        QUALIFICATION_BATCHES[batch_index]["keys"]
        if batch_index < len(QUALIFICATION_BATCHES)
        else []
    )
    missing_batch = [key for key in batch_keys if not compact(answers.get(key))]
    step_labels = {step["key"]: step["prompt"] for step in QUALIFICATION_STEPS}
    collected = {key: compact(answers.get(key)) for key in QUALIFICATION_FIELD_KEYS if compact(answers.get(key))}

    directive = ""
    allowed_field_keys: List[str] = []
    allow_booking = False
    allow_listings = False
    ai_stage = stage

    if session.get("qualified"):
        ai_stage = "QUALIFIED"
        allow_listings = True
        allow_booking = bool(
            calendly_url
            and (
                looks_like_booking_confirmation(query, session)
                or looks_like_booking_request(query)
            )
            and bool(compact(session.get("selected_listing_key")) or session.get("last_shared_listing_keys"))
        )
        if allow_booking:
            directive = (
                "User wants to book a viewing. Confirm briefly and tell them you'll share the booking link. "
                "Do not invent times."
            )
        elif looks_like_listing_detail_request(query) or resolve_listing_reference(query, session, drafts):
            directive = (
                "User is asking about a specific listing from last_shared_listings. "
                "Answer using listing_data only. End by asking if they want to book a viewing."
            )
        elif wants_listing_refresh(query) or wants_listing_help(query):
            directive = "User wants to see or refine listings. Acknowledge briefly; server will append matching listings."
        else:
            directive = "Help with their rental question using listing_data. Stay concise."

    elif session.get("active"):
        ai_stage = "QUALIFYING"
        allowed_field_keys = [next_field] if next_field else []
        if not missing_all:
            directive = "All qualification info collected. Brief positive acknowledgment only."
        elif next_field:
            prompt = step_labels[next_field].rstrip("?")
            directive = (
                f"Acknowledge what they said. Ask ONLY this one question: {prompt}. "
                f"Already collected (do NOT re-ask): {json.dumps(collected)}."
            )
        else:
            directive = f"Brief transition. Already collected: {json.dumps(collected)}."

    elif session.get("awaiting_opt_in"):
        ai_stage = "AWAITING_OPT_IN"
        if looks_like_affirmative(query):
            directive = (
                "They said yes. Brief thanks. Ask ONLY for their expected move-in date."
            )
            allowed_field_keys = ["move_in_date"]
        elif wants_listing_help(query):
            directive = (
                "Acknowledge their search preferences. Remind them to say yes when ready for a few quick questions."
            )
        else:
            directive = (
                "Respond naturally. Mention you're Nabeel's assistant and invite them to say yes when ready for listing help."
            )

    elif wants_listing_help(query) or should_start_qualification(query, calendly_url):
        ai_stage = "AWAITING_OPT_IN"
        session["search_query"] = compact(query)
        session["awaiting_opt_in"] = True
        session["active"] = False
        summary = describe_search_preferences(query)
        directive = (
            f"User wants rentals{' for ' + summary if summary else ''}. "
            "Acknowledge their preferences briefly. Ask them to say yes to proceed with a few quick questions. "
            "Do NOT re-introduce yourself if last_assistant_message already mentioned the assistant."
        )

    elif looks_like_greeting(query):
        ai_stage = "NEW"
        directive = (
            f"Greet warmly as {agent_name}'s assistant at Durham New Homes. "
            "Invite them to share area, budget, or unit type. No qualification questions yet."
        )

    else:
        ai_stage = "NEW"
        directive = "Helpful short reply. Invite them to describe the rental they are looking for."

    listing_data = []
    if session.get("qualified"):
        focus = resolve_listing_reference(query, session, drafts)
        if focus:
            listing_data = [listing_context(focus)]
        else:
            listing_data = [listing_context(d) for d in listings_from_session(session, drafts)[:3]]

    return {
        "ai_stage": ai_stage,
        "directive": directive,
        "allowed_field_keys": allowed_field_keys,
        "collected_answers": collected,
        "missing_fields": missing_all,
        "missing_batch_fields": missing_batch,
        "search_query": compact(session.get("search_query")),
        "allow_booking": allow_booking,
        "allow_listings": allow_listings,
        "calendly_url": calendly_url if allow_booking else "",
        "last_shared_listings": [
            {
                "list_position": index + 1,
                "listing_key": compact(listing.get("ListingKey")),
                "summary": summarize_shared_listing(listing),
                "data": listing_context(listing),
            }
            for index, listing in enumerate(listings_from_session(session, drafts))
        ],
        "listing_data": listing_data,
        "selected_listing_key": compact(session.get("selected_listing_key")),
        "last_assistant_message": compact(session.get("last_prompt")),
        "agent_name": agent_name,
    }


def ai_compose_turn(
    api_key: str,
    model: str,
    user_message: str,
    directive_ctx: dict,
) -> dict:
    if not api_key:
        return {}
    payload = {"user_message": user_message, **directive_ctx}
    try:
        return call_openai_json(
            api_key,
            model,
            AI_MASTER_SYSTEM_PROMPT,
            payload,
            max_tokens=500,
        )
    except Exception as exc:
        print(f"AI compose turn failed: {exc}")
        return {}


def reply_reasks_collected_fields(reply: str, answers: dict) -> bool:
    lowered = reply.lower()
    checks = [
        ("move_in_date", ("move in", "move-in", "when are you looking")),
        ("people_on_lease", ("people on the lease", "how many people")),
        ("adults_in_unit", ("how many adults",)),
        ("kids_in_unit", ("how many kids",)),
        ("family_gross_income", ("gross income", "family income")),
        ("occupation", ("what do you do for work", "occupation")),
        ("resident_status", ("resident status",)),
        ("working_with_agent", ("working with an agent",)),
        ("phone_number", ("phone number", "reach you")),
    ]
    for key, phrases in checks:
        if compact(answers.get(key)) and any(p in lowered for p in phrases):
            return True
    return False


def _unified_ai_turn(
    session: dict,
    state: dict,
    query: str,
    lead_state_path: Path,
    agent_name: str,
    calendly_url: str,
    drafts: List[dict],
    listing_doc_url: str,
    openai_api_key: str,
    openai_model: str,
) -> str:
    reset_stale_opt_in_session(session, query)

    if session.get("awaiting_opt_in") and looks_like_affirmative(query):
        begin_structured_qualification(session, query)
        extract_qualification_only(session, query, openai_api_key, openai_model)
        reply = build_next_qualification_reply(
            session, session.get("answers", {}), drafts, listing_doc_url
        )
        return save_session_reply(lead_state_path, state, session, reply)

    if (
        not session.get("active")
        and not session.get("qualified")
        and looks_like_opt_in_acceptance(query, session)
    ):
        begin_structured_qualification(session, query)
        extract_qualification_only(session, query, openai_api_key, openai_model)
        reply = build_next_qualification_reply(
            session, session.get("answers", {}), drafts, listing_doc_url
        )
        return save_session_reply(lead_state_path, state, session, reply)

    if qualification_in_progress(session) and not session.get("active") and not session.get("qualified"):
        begin_structured_qualification(session, query)

    extract_qualification_only(session, query, openai_api_key, openai_model)

    if session.get("active") and all_qualification_fields_complete(session.get("answers", {})):
        session["active"] = False
        session["qualified"] = True
        session["completed_at"] = int(time.time())
        post_reply = build_post_qualification_reply(session, drafts, listing_doc_url)
        return save_session_reply(lead_state_path, state, session, post_reply)

    if session.get("qualified"):
        if (
            (looks_like_greeting(query) or looks_like_small_talk(query))
            and not wants_listing_help(query)
            and not looks_like_search_refinement(query)
            and not resolve_listing_reference(query, session, drafts)
            and not looks_like_booking_request(query)
        ):
            reply = qualified_conversational_reply(session, agent_name, query)
            return save_session_reply(lead_state_path, state, session, reply)

        interest = handle_qualified_listing_interest(
            session, query, drafts, calendly_url, openai_api_key, openai_model, agent_name
        )
        if interest:
            return save_session_reply(lead_state_path, state, session, interest)
        listing = handle_qualified_listing_search(
            session, query, drafts,
            openai_api_key=openai_api_key, openai_model=openai_model, use_ai=True,
        )
        if listing:
            return save_session_reply(lead_state_path, state, session, listing)

    answers = session.get("answers", {})

    if session.get("active"):
        reply = build_next_qualification_reply(session, answers, drafts, listing_doc_url)
        return save_session_reply(lead_state_path, state, session, reply)

    if (
        (wants_listing_help(query) or should_start_qualification(query, calendly_url))
        and not session.get("qualified")
        and not session.get("active")
    ):
        reply = build_search_opt_in_reply(session, query, agent_name)
        return save_session_reply(lead_state_path, state, session, reply)

    if session.get("awaiting_opt_in") and not looks_like_affirmative(query):
        reply = build_awaiting_opt_in_reply(session, query, agent_name)
        return save_session_reply(lead_state_path, state, session, reply)

    if (
        not session.get("qualified")
        and not session.get("active")
        and not session.get("awaiting_opt_in")
        and looks_like_greeting(query)
        and not wants_listing_help(query)
    ):
        reply = local_conversational_fallback(
            "new",
            query,
            agent_name,
            last_assistant_message=compact(session.get("last_prompt")),
        )
        return save_session_reply(lead_state_path, state, session, reply)

    directive_ctx = compute_conversation_directive(session, query, agent_name, drafts, calendly_url)
    result = ai_compose_turn(openai_api_key, openai_model, query, directive_ctx)

    fields = result.get("fields") if isinstance(result.get("fields"), dict) else {}
    allowed = directive_ctx.get("allowed_field_keys") or []
    if fields and allowed:
        merge_parsed_answers(
            session.setdefault("answers", {}),
            fields,
            query,
            allowed_keys=allowed,
        )
        infer_household_defaults(session["answers"], query)
        session["batch"] = first_incomplete_batch_index(session["answers"])

    if session.get("active") and all_qualification_fields_complete(session.get("answers", {})):
        session["active"] = False
        session["qualified"] = True
        session["completed_at"] = int(time.time())
        post_reply = build_post_qualification_reply(session, drafts, listing_doc_url)
        return save_session_reply(lead_state_path, state, session, post_reply)

    reply = compact(result.get("reply"))
    answers = session.get("answers", {})

    if reply and reply_reasks_collected_fields(reply, answers):
        missing_key = first_missing_qualification_key(answers)
        if missing_key:
            reply = build_missing_field_prompt([missing_key], answers)

    if not reply:
        if session.get("active"):
            reply = build_next_qualification_reply(session, answers, drafts, listing_doc_url)
        else:
            reply = local_conversational_fallback(
                directive_ctx.get("ai_stage", "new").lower(),
                query,
                agent_name,
                last_assistant_message=compact(session.get("last_prompt")),
                search_query=compact(session.get("search_query")),
            )

    return save_session_reply(lead_state_path, state, session, reply)


def handle_unified_ai_turn(
    sender_id: str,
    query: str,
    lead_state_path: Path,
    agent_name: str,
    calendly_url: str,
    drafts: List[dict],
    listing_doc_url: str,
    openai_api_key: str,
    openai_model: str,
) -> str:
    return with_lead_session(
        sender_id,
        lead_state_path,
        lambda session, state: _unified_ai_turn(
            session,
            state,
            query,
            lead_state_path,
            agent_name,
            calendly_url,
            drafts,
            listing_doc_url,
            openai_api_key,
            openai_model,
        ),
    )


def ai_route_conversation_turn(
    api_key: str,
    model: str,
    user_message: str,
    context: dict,
) -> dict:
    if not api_key:
        return {}
    try:
        return call_openai_json(
            api_key,
            model,
            AI_MASTER_SYSTEM_PROMPT,
            {"user_message": user_message, **context},
            max_tokens=350,
        )
    except Exception as exc:
        print(f"AI conversation routing failed: {exc}")
        return {}


def handle_ai_conversation(
    sender_id: str,
    query: str,
    lead_state_path: Path,
    agent_name: str,
    calendly_url: str,
    drafts: List[dict],
    listing_doc_url: str,
    openai_api_key: str,
    openai_model: str,
) -> str:
    state = load_lead_state(lead_state_path)
    session = get_lead_session(state, sender_id)
    reset_stale_opt_in_session(session, query)
    stage = conversation_stage(session)

    if session.get("awaiting_opt_in") and looks_like_affirmative(query):
        begin_structured_qualification(session, query)
        return run_structured_qualification(
            session, query, lead_state_path, state, drafts, listing_doc_url, openai_api_key, openai_model
        )

    if wants_listing_help(query) and not session.get("qualified") and not session.get("active"):
        session["search_query"] = compact(query)
        session["awaiting_opt_in"] = True
        session["active"] = False
        intro = qualification_opt_in_prompt(agent_name, describe_search_preferences(query))
        return save_session_reply(lead_state_path, state, session, intro)

    if qualification_in_progress(session) and not session.get("qualified"):
        return run_structured_qualification(
            session, query, lead_state_path, state, drafts, listing_doc_url, openai_api_key, openai_model
        )

    if session.get("active") and not session.get("qualified"):
        return run_structured_qualification(
            session, query, lead_state_path, state, drafts, listing_doc_url, openai_api_key, openai_model
        )

    if stage == "new" and looks_like_greeting(query) and not wants_listing_help(query):
        greeting = local_conversational_fallback("new", query, agent_name, last_assistant_message=compact(session.get("last_prompt")))
        if openai_api_key:
            ai_greeting = ai_generate_conversational_reply(
                openai_api_key,
                openai_model,
                query,
                agent_name,
                conversation_stage="new",
                last_assistant_message=compact(session.get("last_prompt")),
            )
            if ai_greeting:
                greeting = ai_greeting
        return save_session_reply(lead_state_path, state, session, greeting)

    answers = session.setdefault("answers", {})
    batch_index = min(int(session.get("batch", 0)), max(len(QUALIFICATION_BATCHES) - 1, 0))
    batch_keys = (
        QUALIFICATION_BATCHES[batch_index]["keys"]
        if stage == "qualifying" and batch_index < len(QUALIFICATION_BATCHES)
        else []
    )
    missing_fields = [key for key in QUALIFICATION_FIELD_KEYS if not compact(answers.get(key))]

    context = {
        "stage": stage,
        "agent_name": agent_name,
        "search_query": compact(session.get("search_query")),
        "answers": answers,
        "missing_fields": missing_fields,
        "current_batch_keys": batch_keys,
        "last_assistant_message": compact(session.get("last_prompt")),
        "last_shared_listings": [
            {"position": index + 1, "summary": summarize_shared_listing(listing), "listing_key": compact(listing.get("ListingKey"))}
            for index, listing in enumerate(listings_from_session(session, drafts))
        ],
        "selected_listing_key": compact(session.get("selected_listing_key")),
    }
    result = ai_route_conversation_turn(openai_api_key, openai_model, query, context)

    if session.get("qualified"):
        interest_reply = handle_qualified_listing_interest(
            session,
            query,
            drafts,
            calendly_url,
            openai_api_key,
            openai_model,
            agent_name,
        )
        if interest_reply:
            return save_session_reply(lead_state_path, state, session, interest_reply)

    if not result:
        return finalize_conversation_reply(
            "",
            session,
            query,
            lead_state_path,
            state,
            agent_name,
            openai_api_key,
            openai_model,
            drafts,
            listing_doc_url,
        )

    action = compact(result.get("action")).lower() or "chat"
    reply = compact(result.get("reply"))
    updated_search = compact(result.get("search_query"))
    fields = result.get("fields")
    if updated_search:
        session["search_query"] = updated_search

    if action == "qualify" and not session.get("qualified"):
        begin_structured_qualification(session, query, updated_search or compact(session.get("search_query")))
        if isinstance(fields, dict) and fields:
            merge_parsed_answers(
                answers,
                fields,
                query,
                allowed_keys=QUALIFICATION_FIELD_KEYS,
            )
            infer_household_defaults(answers, query)
        return run_structured_qualification(
            session, query, lead_state_path, state, drafts, listing_doc_url, openai_api_key, openai_model
        )

    if isinstance(fields, dict) and stage == "qualifying":
        session.setdefault("raw_answers", {})[f"turn_{len(session.get('raw_answers', {})) + 1}"] = compact(query)
        allowed_keys = batch_keys or missing_fields
        merge_parsed_answers(answers, fields, query, allowed_keys=allowed_keys)
        infer_household_defaults(answers, query)
        session["batch"] = first_incomplete_batch_index(answers)
        if all_qualification_fields_complete(answers):
            session["active"] = False
            session["qualified"] = True
            session["completed_at"] = int(time.time())
            post_reply = build_post_qualification_reply(session, drafts, listing_doc_url)
            return save_session_reply(lead_state_path, state, session, post_reply)
        qual_reply = build_next_qualification_reply(session, answers, drafts, listing_doc_url)
        return save_session_reply(lead_state_path, state, session, qual_reply)

    if action == "accept_opt_in" and stage == "awaiting_opt_in":
        session["awaiting_opt_in"] = False
        session["active"] = True
        session["step"] = 0
        session["batch"] = 0
        session["qualified"] = False
        session["answers"] = {}
        session["raw_answers"] = {}
        session["last_shared_listing_keys"] = []
        if isinstance(fields, dict) and fields:
            merge_parsed_answers(session["answers"], fields, query, allowed_keys=QUALIFICATION_FIELD_KEYS)
            infer_household_defaults(session["answers"], query)
            session["batch"] = first_incomplete_batch_index(session["answers"])
        first_prompt = build_next_qualification_reply(session, session["answers"], drafts, listing_doc_url)
        return save_session_reply(lead_state_path, state, session, first_prompt)

    if action == "accept_opt_in" and stage == "new" and (looks_like_affirmative(query) or wants_listing_help(query)):
        session["awaiting_opt_in"] = False
        session["active"] = True
        session["step"] = 0
        session["batch"] = 0
        session["qualified"] = False
        session["answers"] = {}
        session["raw_answers"] = {}
        session["search_query"] = updated_search or compact(query)
        session["last_shared_listing_keys"] = []
        first_prompt = build_next_qualification_reply(session, session["answers"], drafts, listing_doc_url)
        return save_session_reply(lead_state_path, state, session, first_prompt)

    if action == "accept_opt_in" and stage == "new" and looks_like_greeting(query):
        action = "chat"

    if action == "offer_opt_in" and stage == "new":
        session["awaiting_opt_in"] = True
        session["active"] = False
        session["search_query"] = updated_search or compact(query)
        if not reply:
            reply = qualification_opt_in_prompt(agent_name, describe_search_preferences(session["search_query"]))
        return save_session_reply(lead_state_path, state, session, reply)

    if action == "qualify" and stage == "qualifying":
        if isinstance(fields, dict) and fields:
            merge_parsed_answers(answers, fields, query, allowed_keys=batch_keys or missing_fields)
            infer_household_defaults(answers, query)
            session["batch"] = first_incomplete_batch_index(answers)
        if all_qualification_fields_complete(answers):
            session["active"] = False
            session["qualified"] = True
            session["completed_at"] = int(time.time())
            post_reply = build_post_qualification_reply(session, drafts, listing_doc_url)
            return save_session_reply(lead_state_path, state, session, post_reply)
        qual_reply = build_next_qualification_reply(session, answers, drafts, listing_doc_url)
        return save_session_reply(lead_state_path, state, session, qual_reply)

    if action == "search_listings" and session.get("qualified"):
        if looks_like_listing_detail_request(query) or resolve_listing_reference(query, session, drafts):
            interest_reply = handle_qualified_listing_interest(
                session,
                query,
                drafts,
                calendly_url,
                openai_api_key,
                openai_model,
                agent_name,
            )
            if interest_reply:
                return save_session_reply(lead_state_path, state, session, interest_reply)
        search_query = updated_search or compact(query)
        listing_reply = build_qualified_listing_reply(session, search_query, drafts)
        if reply and reply.lower() not in listing_reply.lower():
            return save_session_reply(lead_state_path, state, session, f"{reply}\n\n{listing_reply}")
        return save_session_reply(lead_state_path, state, session, listing_reply)

    if action in {"listing_detail", "select_listing"} and session.get("qualified"):
        interest_reply = handle_qualified_listing_interest(
            session,
            query,
            drafts,
            calendly_url,
            openai_api_key,
            openai_model,
            agent_name,
        )
        if interest_reply:
            return save_session_reply(lead_state_path, state, session, interest_reply)

    if action == "book" and session.get("qualified"):
        booking_reply = handle_post_qualification_booking(session, query, drafts, calendly_url)
        if not booking_reply:
            booking_reply = build_calendly_booking_reply(session, calendly_url, drafts)
        if booking_reply:
            return save_session_reply(lead_state_path, state, session, booking_reply)

    if stage == "qualified" and action == "chat":
        shared = listings_from_session(session, drafts)
        focus = []
        listing = resolve_listing_reference(query, session, drafts)
        if listing:
            focus = [listing]
        elif shared and looks_like_listing_detail_request(query):
            focus = shared[:1]
        matches = focus or rank_drafts(query, drafts, limit=3)
        try:
            ai_reply = generate_ai_reply(
                query,
                matches,
                listing_doc_url,
                calendly_url,
                agent_name,
                openai_api_key,
                openai_model,
                qualified=True,
                allow_booking=False,
            )
            if ai_reply:
                return save_session_reply(lead_state_path, state, session, ai_reply)
        except Exception as exc:
            print(f"AI qualified chat failed: {exc}")

    return finalize_conversation_reply(
        reply,
        session,
        query,
        lead_state_path,
        state,
        agent_name,
        openai_api_key,
        openai_model,
        drafts,
        listing_doc_url,
    )


def generate_ai_reply(
    query: str,
    matches: List[dict],
    listing_doc_url: str,
    calendly_url: str,
    agent_name: str,
    api_key: str,
    model: str,
    qualified: bool = False,
    allow_booking: bool = False,
) -> Optional[str]:
    listing_payload = [listing_context(match) for match in matches[:3]]
    system_prompt = (
        "You're Nabeel's assistant at Durham New Homes. Sound human — warm, brief, practical. "
        "Use only the listing data provided. Don't invent details. "
        "No internal jargon, spreadsheets, or back-office terms. "
        "No booking link unless calendly_url is provided and the user wants to book."
    )
    user_prompt = {
        "user_message": query,
        "lead_qualified": qualified,
        "calendly_url": calendly_url if qualified and allow_booking else "",
        "agent_name": agent_name,
        "matched_listings": listing_payload if qualified else [],
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


def build_qualified_reply(
    session: dict,
    query: str,
    drafts: List[dict],
    listing_doc_url: str,
    calendly_url: str,
    agent_name: str,
    openai_api_key: str,
    openai_model: str,
    use_ai: bool,
) -> str:
    normalized = query.lower().strip()
    link_only_patterns = [
        "doc", "document", "sheet", "link", "packet",
        "send me the packet", "send packet", "share the packet", "share packet",
        "send me the link", "share the link",
    ]
    if any(pattern == normalized for pattern in link_only_patterns):
        if listing_doc_url:
            return f"Here is the current listing packet: {listing_doc_url}"
        return "I do not have the document URL configured yet."

    booking_reply = handle_post_qualification_booking(session, query, drafts, calendly_url)
    if booking_reply:
        return booking_reply

    if (
        (looks_like_greeting(query) or looks_like_small_talk(query))
        and not wants_listing_help(query)
        and not looks_like_search_refinement(query)
        and not resolve_listing_reference(query, session, drafts)
        and not looks_like_booking_request(query)
    ):
        return qualified_conversational_reply(session, agent_name, query)

    interest_reply = handle_qualified_listing_interest(
        session,
        query,
        drafts,
        calendly_url,
        openai_api_key if use_ai else "",
        openai_model,
        agent_name,
    )
    if interest_reply:
        return interest_reply

    listing_reply = handle_qualified_listing_search(
        session,
        query,
        drafts,
        openai_api_key=openai_api_key if use_ai else "",
        openai_model=openai_model,
        use_ai=use_ai,
    )
    if listing_reply:
        return listing_reply

    if use_ai and openai_api_key:
        matches = rank_drafts(query, drafts, limit=3)
        allow_booking = looks_like_booking_request(query) and bool(session.get("last_shared_listing_keys"))
        try:
            ai_reply = generate_ai_reply(
                query,
                matches,
                listing_doc_url,
                calendly_url,
                agent_name,
                openai_api_key,
                openai_model,
                qualified=True,
                allow_booking=allow_booking,
            )
            if ai_reply:
                return ai_reply
        except Exception as exc:
            print(f"AI qualified reply failed: {exc}")

    return (
        "Tell me the address, ListingKey, or price of the unit you want to know more about, "
        "or say if you'd like to see other options."
    )


def _reply_deterministic(
    session: dict,
    state: dict,
    query: str,
    lead_state_path: Path,
    drafts: List[dict],
    listing_doc_url: str,
    calendly_url: str,
    agent_name: str,
    openai_api_key: str,
    openai_model: str,
    use_ai: bool,
) -> str:
    reset_stale_opt_in_session(session, query)
    extraction_key = openai_api_key if use_ai else ""

    if session.get("qualified"):
        reply = build_qualified_reply(
            session,
            query,
            drafts,
            listing_doc_url,
            calendly_url,
            agent_name,
            openai_api_key,
            openai_model,
            use_ai,
        )
        return save_session_reply(lead_state_path, state, session, reply)

    if qualification_in_progress(session) and not session.get("active"):
        begin_structured_qualification(session, query)

    if (
        not session.get("active")
        and not session.get("qualified")
        and looks_like_opt_in_acceptance(query, session)
    ):
        begin_structured_qualification(session, query)
        reply = apply_qualification_turn(
            session,
            query,
            drafts,
            listing_doc_url,
            openai_api_key=extraction_key,
            openai_model=openai_model,
        )
        return save_session_reply(lead_state_path, state, session, reply)

    if session.get("active"):
        reply = apply_qualification_turn(
            session,
            query,
            drafts,
            listing_doc_url,
            openai_api_key=extraction_key,
            openai_model=openai_model,
        )
        return save_session_reply(lead_state_path, state, session, reply)

    if session.get("awaiting_opt_in"):
        last_assistant_message = compact(session.get("last_prompt"))
        if looks_like_affirmative(query):
            begin_structured_qualification(session, query)
            reply = apply_qualification_turn(
                session,
                query,
                drafts,
                listing_doc_url,
                openai_api_key=extraction_key,
                openai_model=openai_model,
            )
            return save_session_reply(lead_state_path, state, session, reply)

        if wants_listing_help(query):
            session["search_query"] = compact(query)
            summary = describe_search_preferences(query)
            reply = (
                f"Got it — {summary}. Just reply yes when you're ready and I'll ask a few quick questions."
                if summary
                else "Got it. Just reply yes when you're ready and I'll ask a few quick questions."
            )
            return save_session_reply(lead_state_path, state, session, reply)

        reply = local_conversational_fallback(
            "awaiting_opt_in",
            query,
            agent_name,
            last_assistant_message=last_assistant_message,
            search_query=compact(session.get("search_query")),
        )
        return save_session_reply(lead_state_path, state, session, reply)

    if looks_like_greeting(query) and not wants_listing_help(query):
        reply = (
            f"Hi! I'm {agent_name}'s assistant at Durham New Homes. "
            "Tell me the area, budget, or type of place you're looking for and I'll help from there."
        )
        return save_session_reply(lead_state_path, state, session, reply)

    if wants_listing_help(query) or should_start_qualification(query, calendly_url):
        session["search_query"] = compact(query)
        session["awaiting_opt_in"] = True
        session["active"] = False
        intro = qualification_opt_in_prompt(agent_name, describe_search_preferences(query))
        return save_session_reply(lead_state_path, state, session, intro)

    reply = (
        "I can help you find rentals. Tell me the area, budget, and unit type you're looking for."
    )
    return save_session_reply(lead_state_path, state, session, reply)


def build_reply_deterministic(
    sender_id: str,
    query: str,
    drafts: List[dict],
    listing_doc_url: str,
    calendly_url: str = "",
    agent_name: str = "Nabeel",
    lead_state_path: Path = Path("lead_intake_state.json"),
    openai_api_key: str = "",
    openai_model: str = DEFAULT_OPENAI_MODEL,
    use_ai: bool = True,
) -> str:
    return with_lead_session(
        sender_id,
        lead_state_path,
        lambda session, state: _reply_deterministic(
            session,
            state,
            query,
            lead_state_path,
            drafts,
            listing_doc_url,
            calendly_url,
            agent_name,
            openai_api_key,
            openai_model,
            use_ai,
        ),
    )


def build_reply(
    sender_id: str,
    query: str,
    drafts: List[dict],
    listing_doc_url: str,
    calendly_url: str = "",
    agent_name: str = "Nabeel",
    lead_state_path: Path = Path("lead_intake_state.json"),
    openai_api_key: str = "",
    openai_model: str = DEFAULT_OPENAI_MODEL,
    use_ai: bool = True,
) -> str:
    def _dispatch(session: dict, state: dict) -> str:
        if use_ai and openai_api_key:
            return _unified_ai_turn(
                session,
                state,
                query,
                lead_state_path,
                agent_name,
                calendly_url,
                drafts,
                listing_doc_url,
                openai_api_key,
                openai_model,
            )
        return _reply_deterministic(
            session,
            state,
            query,
            lead_state_path,
            drafts,
            listing_doc_url,
            calendly_url,
            agent_name,
            openai_api_key,
            openai_model,
            use_ai,
        )

    return with_lead_session(sender_id, lead_state_path, _dispatch)


def current_drafts(config: MessengerConfig) -> List[dict]:
    now = time.time()
    source = config.drafts_sheet_csv_url or str(config.drafts_path)
    cached_source = compact(_DRAFT_CACHE.get("source"))
    cached_at = float(_DRAFT_CACHE.get("fetched_at") or 0.0)
    cached_drafts = _DRAFT_CACHE.get("drafts") or []
    cache_fresh = cached_source == source and now - cached_at < max(config.drafts_cache_seconds, 1)
    if cache_fresh and cached_drafts:
        return list(cached_drafts)

    if config.drafts_sheet_csv_url:
        try:
            drafts = fetch_sheet_drafts(config.drafts_sheet_csv_url)
            print(f"Loaded {len(drafts)} drafts from sheet CSV")
            _DRAFT_CACHE["source"] = source
            _DRAFT_CACHE["fetched_at"] = now
            _DRAFT_CACHE["drafts"] = list(drafts)
            _DRAFT_CACHE["degraded"] = False
            return drafts
        except Exception as exc:
            print(f"Failed loading sheet drafts: {exc}")
            if cached_drafts and cached_source == source:
                print("Serving last good sheet cache after fetch failure")
                return list(cached_drafts)

    drafts = load_drafts(config.drafts_path)
    print(f"Loaded {len(drafts)} drafts from local JSON fallback")
    if config.drafts_sheet_csv_url:
        _DRAFT_CACHE["source"] = f"fallback:{config.drafts_path}"
        _DRAFT_CACHE["fetched_at"] = now
        _DRAFT_CACHE["drafts"] = list(drafts)
        _DRAFT_CACHE["degraded"] = True
    else:
        _DRAFT_CACHE["source"] = source
        _DRAFT_CACHE["fetched_at"] = now
        _DRAFT_CACHE["drafts"] = list(drafts)
        _DRAFT_CACHE["degraded"] = False
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
        openai_model=os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
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
            if session_store.use_postgres_sessions():
                seen_count = len(session_store.load_seen_message_ids())
            else:
                with _POLL_STATE_LOCK:
                    seen_count = len(load_seen_message_ids(self.config.poll_state_path))
            self._send_json(
                200,
                {
                    "ok": True,
                    "draft_count": len(current_drafts(self.config)),
                    "draft_source": self.config.drafts_sheet_csv_url or str(self.config.drafts_path),
                    "draft_cache_degraded": bool(_DRAFT_CACHE.get("degraded")),
                    "draft_cache_source": compact(_DRAFT_CACHE.get("source")),
                    "has_page_access_token": bool(self.config.page_access_token),
                    "token_source": self.config.token_source,
                    "has_app_secret": bool(self.config.app_secret),
                    "has_listing_doc_url": bool(self.config.listing_doc_url),
                    "has_openai_api_key": bool(self.config.openai_api_key),
                    "openai_model": self.config.openai_model,
                    "page_id": self.config.page_id,
                    "poll_interval_seconds": getattr(self.server, "poll_interval_seconds", 0),
                    "poll_state_file": str(self.config.poll_state_path),
                    "seen_message_count": seen_count,
                    "session_store": session_store.session_store_status(),
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
                if reset_seen:
                    if session_store.use_postgres_sessions():
                        session_store.clear_seen_message_ids()
                    elif self.config.poll_state_path.exists():
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
                message_id = compact(message.get("mid"))
                if not sender_id or not text:
                    continue
                with _POLL_STATE_LOCK:
                    seen = load_seen_message_ids(self.config.poll_state_path)
                    if message_id and message_id in seen:
                        print(f"Skipping duplicate webhook message {message_id}")
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
                    if message_id:
                        with _POLL_STATE_LOCK:
                            seen = load_seen_message_ids(self.config.poll_state_path)
                            seen.add(message_id)
                            save_seen_message_ids(self.config.poll_state_path, seen)
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
    recent = sorted(seen)[-2000:]
    atomic_write_json(path, {"seen_message_ids": recent})


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

    drafts = current_drafts(config)

    def _poll_locked(seen: set[str]) -> dict:
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

        return result

    return with_poll_state(config.poll_state_path, _poll_locked)


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

    if session_store.use_postgres_sessions():
        session_store.ensure_schema()
        migrated = session_store.migrate_json_lead_state(config.lead_state_path)
        if migrated:
            print(f"Migrated {migrated} Messenger sessions from JSON to PostgreSQL")

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
