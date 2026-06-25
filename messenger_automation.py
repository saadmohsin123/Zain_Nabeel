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
        "prompt": "Perfect. First, what’s your expected move-in date, and how many people will be on the lease?",
    },
    {
        "keys": ["adults_in_unit", "kids_in_unit"],
        "prompt": "Got it. How many adults will be living in the unit, and how many kids will be living there?",
    },
    {
        "keys": ["family_gross_income", "occupation"],
        "prompt": "Thanks. What’s your total family gross income excluding cash income, and what do you do for work?",
    },
    {
        "keys": ["resident_status", "working_with_agent"],
        "prompt": "Almost done. What's your resident status in Canada, and are you currently working with an agent?",
    },
    {
        "keys": ["phone_number"],
        "prompt": "Last one — what's the best phone number to reach you on?",
    },
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


def qualification_opt_in_prompt(agent_name: str, search_summary: str = "") -> str:
    context = f"Got it — you're looking for {search_summary}.\n\n" if search_summary else ""
    return (
        f"{context}"
        f"That's great. I'm {agent_name}'s assistant and I can help make your search easier. "
        "I have access to rentals beyond Facebook as well, and there is no cost to you.\n\n"
        "Would you like me to send you a list of the best active options? "
        "Just say yes and I'll ask a few quick questions first."
    )


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
            "batch": 0,
            "answers": {},
            "raw_answers": {},
            "search_query": "",
            "qualified": False,
            "last_shared_listing_keys": [],
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
    bed_match = re.search(r"\b(\d+)\s*bed(?:room)?s?\b", normalized)
    if bed_match:
        parts.append(f"{bed_match.group(1)} bedroom")
    unit_types = ["condo", "apartment", "house", "townhouse", "studio", "basement"]
    for unit_type in unit_types:
        if re.search(rf"\b{unit_type}\b", normalized):
            parts.append(unit_type)
            break
    if "downtown" in normalized:
        parts.append("in downtown Toronto")
    elif re.search(r"\btoronto\b", normalized):
        parts.append("in Toronto")
    price_match = re.search(r"\$?\s*(\d{3,6})", normalized.replace(",", ""))
    if price_match:
        parts.append(f"up to ${int(price_match.group(1)):,}")
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
    match = re.search(r"(\d+)\s*(?:people|person|tenant|tenants)\b", lowered)
    if match:
        return match.group(1)
    return parse_int_from_text(text)


def is_plausible_occupation(text: str) -> bool:
    cleaned = normalize_whitespace(text)
    if not cleaned:
        return False
    if len(cleaned) < 2:
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
    date_match = re.search(
        r"\b(?:\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?[A-Za-z]+|[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?(?:\s+of)?|[A-Za-z]+\s+\d{4}|immediately|asap|next month|this month)\b",
        normalized,
        re.I,
    )
    return date_match.group(0) if date_match else normalized


def parse_missing_fields(missing_keys: List[str], query: str) -> Dict[str, str]:
    if not missing_keys:
        return {}

    normalized = normalize_whitespace(query)
    if not normalized:
        return {}

    answers: Dict[str, str] = {}
    lowered = normalized.lower()
    if len(missing_keys) == 1:
        key = missing_keys[0]
        if key in ("people_on_lease", "adults_in_unit"):
            count = parse_int_from_text(query)
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


def build_missing_field_prompt(keys: List[str], answers: Optional[dict] = None) -> str:
    answers = answers or {}
    if len(keys) == 1:
        key = keys[0]
        if key == "people_on_lease" and compact(answers.get("move_in_date")):
            return (
                f"Got it on move-in ({compact(answers['move_in_date'])}). "
                "How many people will be on the lease?"
            )
        if key == "kids_in_unit" and compact(answers.get("adults_in_unit")):
            return "Thanks. How many kids will be living in the unit?"
        if key == "occupation" and compact(answers.get("family_gross_income")):
            return "Thanks. What do you do for work?"
        if key == "phone_number":
            return "Last one — what's the best phone number to reach you on?"
        by_key = {step["key"]: step["prompt"] for step in QUALIFICATION_STEPS}
        return by_key.get(key, "Could you share that detail?")

    prompts = []
    by_key = {step["key"]: step["prompt"] for step in QUALIFICATION_STEPS}
    for key in keys:
        prompt = by_key.get(key, "").rstrip("?")
        if prompt:
            prompts.append(f"- {prompt}")
    if not prompts:
        return "I still need one more detail from you."
    return "I still need:\n" + "\n".join(prompts)


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


def build_post_qualification_reply(
    session: dict,
    drafts: List[dict],
    listing_doc_url: str,
) -> str:
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

    matches = rank_drafts(query, drafts, limit=3)
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
) -> Optional[str]:
    if not wants_listing_refresh(query):
        return None

    session["search_query"] = compact(query)
    matches = rank_drafts(query, drafts, limit=3)
    session["last_shared_listing_keys"] = [
        compact(match.get("ListingKey")) for match in matches if compact(match.get("ListingKey"))
    ]

    if not matches:
        return (
            "I looked again, but I don't see active listings that match that search closely right now. "
            "If you want, tell me the city, budget, or unit type and I'll narrow it further."
        )

    lines = ["Here are a few active options that match what you asked for:"]
    for match in matches:
        lines.append(f"- {summarize_shared_listing(match)}")
    lines.append("")
    lines.append("If one stands out, send me the address or ListingKey and I'll help with the next step.")
    return "\n".join(lines)


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
) -> Optional[str]:
    state = load_lead_state(lead_state_path)
    session = get_lead_session(state, sender_id)

    if session.get("active"):
        batch_index = int(session.get("batch", 0))
        answers = session.setdefault("answers", {})
        raw_answers = session.setdefault("raw_answers", {})

        if batch_index < len(QUALIFICATION_BATCHES):
            raw_answers[f"batch_{batch_index + 1}"] = compact(query)
            parsed_answers = parse_batch_answers(batch_index, query)
            for key, value in parsed_answers.items():
                if compact(value):
                    answers[key] = compact(value)

            batch_keys = QUALIFICATION_BATCHES[batch_index]["keys"]
            missing_keys = [key for key in batch_keys if not compact(answers.get(key))]
            if missing_keys:
                followup_answers = parse_missing_fields(missing_keys, query)
                for key, value in followup_answers.items():
                    if key in missing_keys and compact(value):
                        answers[key] = compact(value)
                missing_keys = [key for key in batch_keys if not compact(answers.get(key))]
            if missing_keys:
                save_lead_state(lead_state_path, state)
                return build_missing_field_prompt(missing_keys, answers)

            batch_index += 1
            session["batch"] = batch_index
            if batch_index < len(QUALIFICATION_BATCHES):
                save_lead_state(lead_state_path, state)
                return QUALIFICATION_BATCHES[batch_index]["prompt"]

            session["active"] = False
            session["qualified"] = True
            session["completed_at"] = int(time.time())
            reply = build_post_qualification_reply(session, drafts, listing_doc_url)
            save_lead_state(lead_state_path, state)
            return reply

    booking_reply = handle_post_qualification_booking(session, query, drafts, calendly_url)
    if booking_reply:
        save_lead_state(lead_state_path, state)
        return booking_reply

    if session.get("qualified"):
        listing_reply = handle_qualified_listing_search(session, query, drafts)
        if listing_reply:
            save_lead_state(lead_state_path, state)
            return listing_reply

    if session.get("awaiting_opt_in"):
        if looks_like_affirmative(query):
            session["awaiting_opt_in"] = False
            session["active"] = True
            session["step"] = 0
            session["batch"] = 0
            session["qualified"] = False
            session["answers"] = {}
            session["raw_answers"] = {}
            session["last_shared_listing_keys"] = []
            save_lead_state(lead_state_path, state)
            return QUALIFICATION_BATCHES[0]["prompt"]

        if wants_listing_help(query):
            session["search_query"] = compact(query)
            save_lead_state(lead_state_path, state)
            summary = describe_search_preferences(query)
            if summary:
                return (
                    f"Got it — {summary}. "
                    "Just reply yes when you're ready and I'll ask a few quick questions."
                )
            return "Got it. Just reply yes when you're ready and I'll ask a few quick questions."

        save_lead_state(lead_state_path, state)
        return "Whenever you're ready, just reply yes and I'll ask a few quick questions."

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
        save_lead_state(lead_state_path, state)
        return qualification_opt_in_prompt(agent_name, describe_search_preferences(query))

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
    qualified: bool = False,
    allow_booking: bool = False,
) -> Optional[str]:
    listing_payload = [listing_context(match) for match in matches[:3]]
    system_prompt = (
        "You are Durham New Homes, a leasing assistant for Nabeel. "
        "Answer like a capable human leasing coordinator: warm, concise, natural, and practical. "
        "Use only the provided listing data. "
        "Do not invent features, pricing, amenities, policies, or availability. "
        "Never mention internal workflow labels, back-office statuses, review states, spreadsheets, packets, or marketplace pipeline terms to the customer. "
        "If the user asks something not present in the data, say that it is not confirmed yet and ask a targeted follow-up. "
        "If the query is generic and no exact answer is available, guide the user to share the address, ListingKey, or unit number. "
        "Prefer short conversational paragraphs, not bullets, unless the user explicitly asks for a list. "
        "Avoid sounding robotic, overly formal, or repetitive. "
        "Only describe listings as available if the provided data clearly shows they are customer-ready active rental listings. "
        "If there are no customer-ready matches, say there are no active matches ready to share right now and offer to refine the search. "
        "Do not offer or send any booking link unless the lead is qualified and explicitly wants to move forward on a listing."
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
    state = load_lead_state(lead_state_path)
    session = get_lead_session(state, sender_id)
    if (
        not session.get("active")
        and not session.get("awaiting_opt_in")
        and not session.get("qualified")
        and looks_like_greeting(query)
    ):
        return (
            "Hi there! How can I assist you with your home search today? If you have a particular area, budget, address, ListingKey, or unit type in mind, I can help find the best options for you."
        )

    qualification_reply = maybe_handle_qualification(
        sender_id,
        query,
        lead_state_path,
        agent_name,
        calendly_url,
        drafts,
        listing_doc_url,
    )
    if qualification_reply:
        return qualification_reply

    is_qualified = bool(session.get("qualified"))
    allow_booking = is_qualified and looks_like_booking_request(query) and bool(session.get("last_shared_listing_keys"))

    if not is_qualified:
        state = load_lead_state(lead_state_path)
        session = get_lead_session(state, sender_id)
        if session.get("awaiting_opt_in") or session.get("active"):
            save_lead_state(lead_state_path, state)
            return (
                "Whenever you're ready, just reply yes and I'll ask a few quick questions."
            )
        if looks_like_booking_request(query) or wants_listing_help(query):
            session["awaiting_opt_in"] = True
            session["search_query"] = compact(query)
            save_lead_state(lead_state_path, state)
            return qualification_opt_in_prompt(agent_name, describe_search_preferences(query))
        return (
            "I can help with that. Tell me your preferred area, budget, and unit type, "
            "and I'll take it from there."
        )

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
                qualified=is_qualified,
                allow_booking=allow_booking,
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

    lines = ["Here are a few active options that look relevant:"]
    for draft in matches:
        lines.append("")
        lines.append(summarize_draft(draft))
    lines.append("")
    lines.append("If one stands out, send me the address or ListingKey and I'll help with the next step.")
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
        drafts = load_drafts(config.drafts_path)
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
        seen = load_seen_message_ids(self.config.poll_state_path)
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
