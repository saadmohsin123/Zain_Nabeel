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


def compact(value) -> str:
    if value in (None, "", []):
        return ""
    if isinstance(value, list):
        return ", ".join(compact(v) for v in value if compact(v))
    return str(value).strip()


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


def rank_drafts(query: str, drafts: List[dict], limit: int = 3) -> List[dict]:
    q_tokens = tokenize(query)
    if not q_tokens:
        return []

    scored: List[Tuple[int, dict]] = []
    for draft in drafts:
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
    status = compact(draft.get("MarketplaceStatus")) or "Unknown"
    lifecycle = compact(draft.get("ListingLifecycleStatus")) or "Unknown"
    tx = compact(draft.get("TransactionType"))
    city = compact(draft.get("City"))
    parts = [title]
    if price:
        parts.append(f"Price: {price}")
    if tx:
        parts.append(f"Transaction: {tx}")
    if city:
        parts.append(f"City: {city}")
    parts.append(f"Marketplace: {status}")
    parts.append(f"MLS: {lifecycle}")
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
        "ListingLifecycleStatus",
        "MarketplaceStatus",
    ]
    return {field: draft.get(field) for field in fields if draft.get(field) not in (None, "", [])}


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
        "Answer like a high-quality leasing coordinator: concise, helpful, natural, and action-oriented. "
        "Use only the provided listing data and provided packet link. "
        "Do not invent features, pricing, amenities, policies, or availability. "
        "If the user asks something not present in the data, say that it is not confirmed yet and ask a targeted follow-up. "
        "If the query is generic and no exact answer is available, guide the user to share the address, ListingKey, or unit number. "
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


def build_reply(
    query: str,
    drafts: List[dict],
    listing_doc_url: str,
    openai_api_key: str = "",
    openai_model: str = "gpt-4.1-mini",
) -> str:
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
    if openai_api_key:
        try:
            ai_reply = generate_ai_reply(query, matches, listing_doc_url, openai_api_key, openai_model)
            if ai_reply:
                return ai_reply
        except Exception as exc:
            print(f"AI reply generation failed: {exc}")

    if not matches:
        return (
            "I could not match that listing yet. Send me the address, ListingKey, or unit number, "
            "and I will pull the closest draft."
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
                    "draft_count": len(load_drafts(self.config.drafts_path)),
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
            if token != self.config.verify_token:
                self.send_error(403, "Forbidden")
                return
            try:
                replies = poll_conversations_once(self.config, initialize_only=initialize_only)
                self._send_json(200, {"ok": True, "reply_count": replies, "initialize_only": initialize_only})
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

    for message in list_recent_messages(config.page_id, config.page_access_token):
        message_id = compact(message.get("id"))
        if not message_id or message_id in seen:
            continue
        seen.add(message_id)

        sender = message.get("from", {}) or {}
        sender_id = compact(sender.get("id"))
        text = compact(message.get("message"))
        if initialize_only or not sender_id or sender_id == config.page_id or not text:
            continue

        reply = build_reply(
            text,
            drafts,
            config.listing_doc_url,
            config.openai_api_key,
            config.openai_model,
        )
        try:
            send_message(config.page_access_token, sender_id, reply)
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
