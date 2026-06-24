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
- POLL_CONVERSATIONS_SECONDS  # optional fallback when Meta does not deliver webhooks
- POLL_STATE_FILE             # default: messenger_poll_state.json
- MESSENGER_PORT              # default: 8000
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import hmac
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests


GRAPH_BASE = "https://graph.facebook.com/v20.0"
OPENAI_RESPONSES_API = "https://api.openai.com/v1/responses"
LEAD_QUESTION_GROUPS = [
    {
        "fields": ["move_in_date", "people_on_lease"],
        "prompt": "Perfect. Before we proceed, please let me know your move-in date and how many people will be on the lease.",
    },
    {
        "fields": ["adults_in_unit", "kids_in_unit"],
        "prompt": "Thanks. How many adults and how many kids will be living in the unit?",
    },
    {
        "fields": ["gross_income"],
        "prompt": "What is your total family gross income before taxes? Please do not include cash income.",
    },
    {
        "fields": ["occupation", "resident_status"],
        "prompt": "What is your occupation, and what is your resident status in Canada?",
    },
    {
        "fields": ["working_with_agent", "phone_number"],
        "prompt": "Last two: are you currently working with an agent, and what is the best phone number to reach you on?",
    },
]
QUALIFICATION_POSITIVE_SIGNALS = [
    "yes",
    "yeah",
    "yup",
    "sure",
    "okay",
    "ok",
    "please",
    "sounds good",
    "go ahead",
    "send me listings",
    "send listings",
    "show me",
    "help me",
    "move forward",
    "interested",
    "schedule",
    "meeting",
    "viewing",
    "showing",
    "book",
    "call",
    "tour",
    "find me",
]
QUALIFICATION_NEGATIVE_SIGNALS = [
    "no",
    "not now",
    "later",
    "no thanks",
    "stop",
    "skip",
]
DOWNTOWN_TORONTO_CODES = {"c01", "c08", "c02"}
GREETING_ONLY_MESSAGES = {
    "hi",
    "hello",
    "hey",
    "yo",
    "sup",
    "good morning",
    "good afternoon",
    "good evening",
}


def normalize_text(value: str) -> str:
    return compact(value).lower()


def search_request_detected(query: str) -> bool:
    normalized = normalize_text(query)
    if not normalized:
        return False
    if normalized in GREETING_ONLY_MESSAGES:
        return False
    if re.search(r"\b\d+\s*bed(room)?\b", normalized):
        return True
    if re.search(r"\b\d+\s*bath(room)?\b", normalized):
        return True
    search_markers = [
        "looking for",
        "need a",
        "need an",
        "want a",
        "want an",
        "apartment",
        "condo",
        "house",
        "townhouse",
        "studio",
        "basement",
        "lease",
        "rent",
        "downtown",
        "toronto",
        "mississauga",
        "scarborough",
        "north york",
        "etobicoke",
        "markham",
        "brampton",
        "oakville",
        "vaughan",
        "richmond hill",
    ]
    return any(marker in normalized for marker in search_markers)


def parse_price_ceiling(query: str) -> Optional[int]:
    normalized = normalize_text(query).replace(",", "")
    match = re.search(r"\$?\s*(\d{3,6})\s*(?:/month|monthly|month)?", normalized)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except Exception:
        return None
    if value < 400:
        return None
    return value


def extract_search_preferences(query: str) -> dict:
    normalized = normalize_text(query)
    prefs: dict = {"raw_query": compact(query)}

    bed_match = re.search(r"\b(\d+)\s*bed(?:room)?s?\b", normalized)
    if bed_match:
        prefs["bedrooms"] = int(bed_match.group(1))

    bath_match = re.search(r"\b(\d+)\s*bath(?:room)?s?\b", normalized)
    if bath_match:
        prefs["bathrooms"] = int(bath_match.group(1))

    price_ceiling = parse_price_ceiling(query)
    if price_ceiling:
        prefs["max_price"] = price_ceiling

    if "downtown toronto" in normalized or "downtown" in normalized:
        prefs["city"] = "toronto"
        prefs["downtown"] = True
    else:
        city_aliases = [
            "toronto",
            "mississauga",
            "scarborough",
            "north york",
            "etobicoke",
            "markham",
            "brampton",
            "oakville",
            "vaughan",
            "richmond hill",
        ]
        for city in city_aliases:
            if city in normalized:
                prefs["city"] = city
                break

    unit_types = ["apartment", "condo", "house", "townhouse", "studio", "basement"]
    for unit_type in unit_types:
        if unit_type in normalized:
            prefs["property_type"] = unit_type
            break

    return prefs


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


def load_lead_state(path: Path) -> Dict[str, dict]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_lead_state(path: Path, state: Dict[str, dict]):
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


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
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


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


def is_customer_visible(draft: dict) -> bool:
    marketplace_status = compact(draft.get("MarketplaceStatus")).lower()
    lifecycle_status = compact(draft.get("ListingLifecycleStatus")).lower()
    transaction_type = compact(draft.get("TransactionType")).lower()

    if marketplace_status != "posted":
        return False
    if lifecycle_status and lifecycle_status not in {"active", "new", "price change", "extension"}:
        return False
    if transaction_type and not any(token in transaction_type for token in ("lease", "rent")):
        return False
    return True


def customer_visible_drafts(drafts: List[dict]) -> List[dict]:
    return [draft for draft in drafts if is_customer_visible(draft)]


def matches_downtown_toronto(draft: dict) -> bool:
    haystack = " ".join(
        [
            normalize_text(draft.get("Address")),
            normalize_text(draft.get("City")),
            normalize_text(draft.get("MarketplaceDescription")),
        ]
    )
    if any(code in haystack for code in DOWNTOWN_TORONTO_CODES):
        return True
    downtown_markers = [
        "downtown",
        "cityplace",
        "king west",
        "queen west",
        "entertainment district",
        "waterfront",
        "fort york",
        "liberty village",
    ]
    return any(marker in haystack for marker in downtown_markers)


def draft_price_value(draft: dict) -> Optional[int]:
    for value in (draft.get("MarketplacePrice"), draft.get("MarketplacePriceDisplay")):
        text = normalize_text(value).replace(",", "")
        if not text:
            continue
        match = re.search(r"(\d{3,6})", text)
        if match:
            try:
                return int(match.group(1))
            except Exception:
                pass
    return None


def preference_filtered_drafts(drafts: List[dict], prefs: dict) -> List[dict]:
    filtered = list(drafts)

    bedrooms = prefs.get("bedrooms")
    if bedrooms is not None:
        exact = [draft for draft in filtered if compact(draft.get("BedroomsTotal")) == str(bedrooms)]
        filtered = exact
        if not filtered:
            return []

    bathrooms = prefs.get("bathrooms")
    if bathrooms is not None and filtered:
        exact = [draft for draft in filtered if compact(draft.get("BathroomsTotal")) == str(bathrooms)]
        filtered = exact
        if not filtered:
            return []

    city = normalize_text(prefs.get("city"))
    if city and filtered:
        exact = [
            draft
            for draft in filtered
            if city in normalize_text(draft.get("City")) or city in normalize_text(draft.get("Address"))
        ]
        filtered = exact
        if not filtered:
            return []

    if prefs.get("downtown") and filtered:
        exact = [draft for draft in filtered if matches_downtown_toronto(draft)]
        filtered = exact
        if not filtered:
            return []

    property_type = normalize_text(prefs.get("property_type"))
    if property_type and filtered:
        mapped_type = "condo" if property_type == "apartment" else property_type
        exact = [
            draft
            for draft in filtered
            if mapped_type in normalize_text(draft.get("PropertyType"))
            or mapped_type in normalize_text(draft.get("MarketplaceTitle"))
            or mapped_type in normalize_text(draft.get("MarketplaceDescription"))
        ]
        filtered = exact
        if not filtered:
            return []

    max_price = prefs.get("max_price")
    if max_price is not None and filtered:
        exact = [draft for draft in filtered if (draft_price_value(draft) or 10**9) <= max_price]
        filtered = exact
        if not filtered:
            return []

    return filtered


def rank_drafts(query: str, drafts: List[dict], limit: int = 3, prefs: Optional[dict] = None) -> List[dict]:
    prefs = prefs or extract_search_preferences(query)
    pool = preference_filtered_drafts(drafts, prefs)
    q_tokens = tokenize(query)
    if not q_tokens:
        return []

    scored: List[Tuple[int, dict]] = []
    for draft in pool:
        haystack = draft_text(draft)
        score = 0
        for token in q_tokens:
            if token in haystack:
                score += 3
            if token in compact(draft.get("ListingKey")).lower():
                score += 5
            if token in compact(draft.get("Address")).lower():
                score += 4
        if prefs.get("bedrooms") is not None and compact(draft.get("BedroomsTotal")) == str(prefs["bedrooms"]):
            score += 20
        if prefs.get("bathrooms") is not None and compact(draft.get("BathroomsTotal")) == str(prefs["bathrooms"]):
            score += 10
        if normalize_text(prefs.get("city")) and (
            normalize_text(prefs.get("city")) in normalize_text(draft.get("City"))
            or normalize_text(prefs.get("city")) in normalize_text(draft.get("Address"))
        ):
            score += 12
        if prefs.get("downtown") and matches_downtown_toronto(draft):
            score += 20
        if score:
            scored.append((score, draft))

    scored.sort(key=lambda item: (-item[0], compact(item[1].get("MarketplacePriceDisplay"))))
    return [draft for _, draft in scored[:limit]]


def summarize_draft(draft: dict) -> str:
    title = compact(draft.get("MarketplaceTitle")) or compact(draft.get("Address")) or "Listing"
    price = compact(draft.get("MarketplacePriceDisplay")) or compact(draft.get("MarketplacePrice"))
    tx = compact(draft.get("TransactionType"))
    city = compact(draft.get("City"))
    beds = compact(draft.get("BedroomsTotal"))
    baths = compact(draft.get("BathroomsTotal"))
    area = compact(draft.get("LivingAreaRange"))
    parts = [title]
    if price:
        parts.append(f"Price: {price}")
    if beds or baths:
        label = " / ".join(part for part in [f"{beds} bed" if beds else "", f"{baths} bath" if baths else ""] if part)
        if label:
            parts.append(label)
    if area:
        parts.append(f"Size: {area}")
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


def list_intent_detected(query: str) -> bool:
    normalized = compact(query).lower()
    if not normalized:
        return False
    return any(trigger in normalized for trigger in QUALIFICATION_POSITIVE_SIGNALS)


def qualification_declined(query: str) -> bool:
    normalized = compact(query).lower()
    if not normalized:
        return False
    return any(trigger == normalized or trigger in normalized for trigger in QUALIFICATION_NEGATIVE_SIGNALS)


def qualification_intro(search_summary: str = "") -> str:
    context_line = f"Got it — you're looking for {search_summary}.\n\n" if search_summary else ""
    return (
        f"{context_line}"
        "I can help with that. Before I shortlist the best active options for you, "
        "I just need a few quick details so I can qualify you properly."
    )


def qualification_group_index(state: dict) -> int:
    try:
        return max(0, int(state.get("group_index", 0)))
    except Exception:
        return 0


def current_qualification_group(state: dict) -> Optional[dict]:
    index = qualification_group_index(state)
    if index >= len(LEAD_QUESTION_GROUPS):
        return None
    return LEAD_QUESTION_GROUPS[index]


def advance_qualification_group(state: dict) -> Optional[str]:
    state["group_index"] = qualification_group_index(state) + 1
    next_group = current_qualification_group(state)
    if next_group is None:
        state["awaiting_fields"] = []
        return None
    state["awaiting_fields"] = list(next_group["fields"])
    return next_group["prompt"]


def start_qualification_questions(state: dict, prefaced: bool = True) -> str:
    state["started"] = True
    state["intro_offered"] = True
    state["group_index"] = 0
    first_group = current_qualification_group(state)
    state["awaiting_fields"] = list(first_group["fields"]) if first_group else []
    intro = "Perfect. Before we proceed, I just need a few quick details so I can qualify you properly and send the best options.\n\n" if prefaced else ""
    return f"{intro}{first_group['prompt']}"


def parse_group_answers(raw_text: str, expected_fields: List[str]) -> Dict[str, str]:
    lines = [line.strip(" -•\t") for line in raw_text.splitlines() if compact(line)]
    if len(lines) >= len(expected_fields):
        return {field: lines[i] for i, field in enumerate(expected_fields)}

    text = compact(raw_text)
    if len(expected_fields) == 2:
        parts = [part.strip() for part in re.split(r"\s*(?:,|/| and | & )\s*", text, maxsplit=1) if compact(part)]
        if len(parts) >= 2:
            return {expected_fields[0]: parts[0], expected_fields[1]: parts[1]}

    return {expected_fields[0]: text} if expected_fields else {}


def qualification_completion_message(calendly_url: str) -> str:
    base = (
        "Perfect, thank you. I have all the details I need.\n\n"
        "The next step is to book a quick call with Nabeel so he can review the best active listings for you and guide you on the next step."
    )
    if calendly_url:
        return f"{base}\n\nYou can book here: {calendly_url}"
    return base


def describe_preferences(prefs: dict) -> str:
    parts: List[str] = []
    if prefs.get("bedrooms") is not None:
        parts.append(f"{prefs['bedrooms']} bedroom")
    if prefs.get("bathrooms") is not None:
        parts.append(f"{prefs['bathrooms']} bathroom")
    if prefs.get("property_type"):
        parts.append(compact(prefs["property_type"]))
    location = "downtown Toronto" if prefs.get("downtown") else compact(prefs.get("city"))
    if location:
        parts.append(f"in {location}")
    if prefs.get("max_price") is not None:
        parts.append(f"up to ${prefs['max_price']:,}")
    return " ".join(parts).strip()


def handle_qualification_turn(query: str, sender_id: str, lead_state: Dict[str, dict], calendly_url: str = "") -> Optional[str]:
    if not sender_id:
        return None

    prefs = extract_search_preferences(query)
    search_detected = search_request_detected(query)
    state = lead_state.setdefault(
        sender_id,
        {
            "answers": {},
            "awaiting_fields": [],
            "qualified": False,
            "started": False,
            "intro_offered": False,
            "group_index": 0,
            "search_query": "",
            "search_preferences": {},
        },
    )
    if search_detected:
        state["search_query"] = compact(query)
        state["search_preferences"] = prefs
    awaiting_fields = state.get("awaiting_fields", []) or []

    if awaiting_fields:
        parsed_answers = parse_group_answers(query, awaiting_fields)
        answers = state.setdefault("answers", {})
        for field in awaiting_fields:
            if compact(parsed_answers.get(field)):
                answers[field] = compact(parsed_answers.get(field))
        next_prompt = advance_qualification_group(state)
        if next_prompt:
            return next_prompt
        state["qualified"] = True
        return qualification_completion_message(calendly_url)

    if state.get("qualified"):
        return None

    if state.get("intro_offered") and not state.get("started"):
        if qualification_declined(query):
            state["intro_offered"] = False
            return (
                "No problem. If you want, send me your preferred area, budget, and property type, "
                "and I can still point you in the right direction."
            )
        if list_intent_detected(query) or search_detected:
            return start_qualification_questions(state)
        return "If you'd like me to shortlist the best active rentals for you, just say yes and I'll ask a few quick questions first."

    if search_detected:
        summary = describe_preferences(prefs)
        first_question = start_qualification_questions(state, prefaced=False)
        intro = qualification_intro(summary)
        return f"{intro}\n\n{first_question}"

    if list_intent_detected(query):
        state["intro_offered"] = True
        return qualification_intro()

    if normalize_text(query) in GREETING_ONLY_MESSAGES:
        return (
            "Hi there! I can help with rentals in Toronto and nearby areas. "
            "Send me your preferred area, budget, and unit type, and I’ll take it from there."
        )

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
    api_key: str,
    model: str,
) -> Optional[str]:
    listing_payload = [listing_context(match) for match in matches[:3]]
    system_prompt = (
        "You are Durham New Homes, an AI real-estate leasing and sales assistant. "
        "Answer like a high-quality leasing coordinator: concise, helpful, natural, warm, and action-oriented. "
        "Use only the provided listing data and provided packet link. "
        "Do not invent features, pricing, amenities, policies, or availability. "
        "Never mention internal workflow labels, internal approval stages, or operational statuses such as Pending Seller Action, "
        "Needs Review, Posted, Archived, or MLS lifecycle codes. Those are internal only. "
        "If no active customer-ready listing matches, simply say there is no active match available right now and offer to refine the search. "
        "If the user asks something not present in the data, say that it is not confirmed yet and ask a targeted follow-up. "
        "If the query is generic and no exact answer is available, guide the user to share the address, ListingKey, or unit number. "
        "If the user wants help finding a rental, scheduling a viewing, or moving forward, speak as Nabeel's assistant and naturally ask qualifying questions. "
        "Prefer short paragraphs, not bullets, unless the user explicitly asks for a list."
    )
    user_prompt = {
        "user_message": query,
        "listing_packet_url": listing_doc_url,
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
        timeout=60,
    )
    resp.raise_for_status()
    output_text = extract_response_text(resp.json())
    return output_text or None


def build_match_response(matches: List[dict], listing_doc_url: str, qualified: bool, calendly_url: str = "") -> str:
    if not matches:
        return (
            "I could not find an active customer-ready match for that exact search right now. "
            "Send me your preferred area, budget, and unit type, and I’ll refine it for you."
        )

    lines = ["Here are the best active matches I found for you:"]
    for draft in matches:
        lines.append("")
        lines.append(summarize_draft(draft))
        desc = compact(draft.get("MarketplaceDescription"))
        if desc:
            lines.append(desc[:500])
    if listing_doc_url:
        lines.append("")
        lines.append(f"Packet: {listing_doc_url}")
    if calendly_url:
        lines.append("")
        lines.append(f"If you'd like, you can also book a quick call with Nabeel here: {calendly_url}")
    elif not qualified:
        lines.append("")
        lines.append(
            "If you'd like, I can help narrow this down for you and shortlist the best active options. "
            "Just tell me if you want to move forward and I’ll ask a few quick questions."
        )
    return "\n".join(lines)


def build_reply(
    query: str,
    drafts: List[dict],
    listing_doc_url: str,
    openai_api_key: str = "",
    openai_model: str = "gpt-4.1-mini",
    sender_id: str = "",
    lead_state_path: Optional[Path] = None,
    calendly_url: str = "",
) -> str:
    lead_state = load_lead_state(lead_state_path) if lead_state_path else {}
    existing_state = lead_state.get(sender_id, {}) if sender_id else {}
    was_qualified = bool(existing_state.get("qualified"))
    qualification_reply = handle_qualification_turn(query, sender_id, lead_state, calendly_url)
    if qualification_reply is not None:
        updated_state = lead_state.get(sender_id, {}) if sender_id else {}
        just_qualified = bool(updated_state.get("qualified")) and not was_qualified
        if lead_state_path:
            save_lead_state(lead_state_path, lead_state)
        if just_qualified:
            search_query = compact(updated_state.get("search_query")) or compact(query)
            prefs = updated_state.get("search_preferences") or extract_search_preferences(search_query)
            visible_drafts = customer_visible_drafts(drafts)
            matches = rank_drafts(search_query, visible_drafts, limit=3, prefs=prefs)
            return build_match_response(matches, listing_doc_url, qualified=True, calendly_url=calendly_url)
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

    active_search_query = compact(existing_state.get("search_query")) if existing_state.get("started") else ""
    effective_query = active_search_query or query
    prefs = existing_state.get("search_preferences") if active_search_query else extract_search_preferences(query)
    visible_drafts = customer_visible_drafts(drafts)
    matches = rank_drafts(effective_query, visible_drafts, limit=3, prefs=prefs)
    if openai_api_key:
        try:
            ai_reply = generate_ai_reply(effective_query, matches, listing_doc_url, openai_api_key, openai_model)
            if ai_reply:
                return ai_reply
        except Exception as exc:
            print(f"AI reply generation failed: {exc}")

    return build_match_response(matches, listing_doc_url, qualified=bool(existing_state.get("qualified")), calendly_url=calendly_url)


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
    listing_doc_url: str = ""
    page_id: str = ""
    poll_state_path: Path = Path("messenger_poll_state.json")
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    token_source: str = "page"
    bootstrap_reply_lookback_seconds: int = 86400
    lead_state_path: Path = Path("messenger_lead_state.json")
    calendly_url: str = ""


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

    return MessengerConfig(
        page_access_token=page_access_token,
        verify_token=must_env("META_VERIFY_TOKEN"),
        app_secret=os.getenv("META_APP_SECRET", ""),
        drafts_path=Path(os.getenv("MARKETPLACE_DRAFTS_JSON", "marketplace_drafts.json")),
        listing_doc_url=os.getenv("LISTING_DOC_URL", ""),
        page_id=page_id,
        poll_state_path=Path(os.getenv("POLL_STATE_FILE", "messenger_poll_state.json")),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        token_source=token_source,
        bootstrap_reply_lookback_seconds=int(os.getenv("POLL_BOOTSTRAP_LOOKBACK_SECONDS", "86400") or "86400"),
        lead_state_path=Path(os.getenv("LEAD_STATE_FILE", "messenger_lead_state.json")),
        calendly_url=os.getenv("CALENDLY_URL", ""),
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
        if parsed.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        params = parse_qs(parsed.query)
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
        drafts = load_drafts(self.config.drafts_path)
        entry_list = payload.get("entry", [])
        for entry in entry_list:
            messaging = entry.get("messaging", [])
            for event in messaging:
                sender_id = event.get("sender", {}).get("id")
                message = event.get("message", {})
                text = compact(message.get("text"))
                if not sender_id or not text:
                    continue

                reply = build_reply(
                    text,
                    drafts,
                    self.config.listing_doc_url,
                    self.config.openai_api_key,
                    self.config.openai_model,
                    sender_id,
                    self.config.lead_state_path,
                    self.config.calendly_url,
                )
                try:
                    send_message(self.config.page_access_token, sender_id, reply)
                    print(f"Replied to {sender_id}: {text[:80]}")
                except Exception as exc:
                    print(f"Failed sending to {sender_id}: {exc}")


def run_server(config: MessengerConfig, port: int):
    httpd = ThreadingHTTPServer(("0.0.0.0", port), MessengerWebhookHandler)
    httpd.config = config  # type: ignore[attr-defined]
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


def list_recent_messages(page_id: str, page_access_token: str, limit: int = 10) -> List[dict]:
    conversations = list_conversations(page_id, page_access_token, limit=limit).get("data", [])
    messages: List[dict] = []
    for conversation in conversations:
        conversation_id = conversation.get("id")
        if not conversation_id:
            continue
        resp = requests.get(
            f"{GRAPH_BASE}/{conversation_id}/messages",
            params={
                "access_token": page_access_token,
                "limit": 5,
                "fields": "id,created_time,from,message",
            },
            timeout=30,
        )
        resp.raise_for_status()
        messages.extend(resp.json().get("data", []))
    return messages


def poll_conversations_once(config: MessengerConfig, initialize_only: bool = False) -> int:
    if not config.page_id:
        raise RuntimeError("META_PAGE_ID is required when polling is enabled")

    seen = load_seen_message_ids(config.poll_state_path)
    drafts = load_drafts(config.drafts_path)
    reply_count = 0
    now_ts = int(time.time())
    bootstrap_cutoff_ts = now_ts - max(config.bootstrap_reply_lookback_seconds, 0)

    for message in list_recent_messages(config.page_id, config.page_access_token):
        message_id = compact(message.get("id"))
        if not message_id or message_id in seen:
            continue

        sender = message.get("from", {}) or {}
        sender_id = compact(sender.get("id"))
        text = compact(message.get("message"))
        created_ts = parse_graph_time(compact(message.get("created_time")))

        if initialize_only:
            # On cold start, keep recent inbound messages unseen so the next poll can answer them.
            is_old = created_ts is not None and created_ts < bootstrap_cutoff_ts
            if not sender_id or sender_id == config.page_id or not text or is_old:
                seen.add(message_id)
            continue

        if not sender_id or sender_id == config.page_id or not text:
            seen.add(message_id)
            continue

        reply = build_reply(
            text,
            drafts,
            config.listing_doc_url,
            config.openai_api_key,
            config.openai_model,
            sender_id,
            config.lead_state_path,
            config.calendly_url,
        )
        try:
            send_message(config.page_access_token, sender_id, reply)
            seen.add(message_id)
            reply_count += 1
            print(f"Poll replied to {sender_id}: {text[:80]}")
        except Exception as exc:
            print(f"Poll failed sending to {sender_id}: {exc}")

    save_seen_message_ids(config.poll_state_path, seen)
    return reply_count


def start_conversation_poller(config: MessengerConfig, interval_seconds: int):
    def worker():
        try:
            poll_conversations_once(config, initialize_only=True)
            print("Conversation poller initialized existing messages as seen")
        except Exception as exc:
            print(f"Conversation poller init failed: {exc}")

        while True:
            time.sleep(interval_seconds)
            try:
                poll_conversations_once(config)
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
