import hashlib
import hmac
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = int(os.environ.get("PORT") or os.environ.get("MESSENGER_PORT") or 8080)
DEFAULT_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "coagent_messenger_verify_2026")
PAGE_ACCESS_TOKEN = os.environ.get("META_PAGE_ACCESS_TOKEN", "")
APP_SECRET = os.environ.get("META_APP_SECRET", "")
PAGE_ID = os.environ.get("META_PAGE_ID", "")
DRAFTS_JSON = Path(os.environ.get("MARKETPLACE_DRAFTS_JSON", BASE_DIR / "marketplace_drafts.json"))
DOC_URL = os.environ.get("LISTING_DOC_URL", "")
GRAPH_BASE = os.environ.get("META_GRAPH_BASE", "https://graph.facebook.com/v21.0")


def load_drafts():
    if not DRAFTS_JSON.exists():
        return []
    try:
        data = json.loads(DRAFTS_JSON.read_text())
    except Exception:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "listings", "rows", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def normalize_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def signature_valid(raw_body, signature):
    if not APP_SECRET or not signature:
        return True
    if not signature.startswith("sha1="):
        return False
    expected = hmac.new(APP_SECRET.encode(), raw_body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(expected, signature.split("=", 1)[1])


def send_message(psid, text):
    if not PAGE_ACCESS_TOKEN:
        return {"ok": False, "error": "META_PAGE_ACCESS_TOKEN missing"}
    payload = {
        "recipient": {"id": psid},
        "message": {"text": text},
        "messaging_type": "RESPONSE",
    }
    url = f"{GRAPH_BASE}/me/messages"
    r = requests.post(url, params={"access_token": PAGE_ACCESS_TOKEN}, json=payload, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"text": r.text}
    return {"ok": r.ok, "status": r.status_code, "body": body}


def format_listing_summary(listing):
    fields = listing if isinstance(listing, dict) else {}
    parts = []
    for key in ("MarketplaceTitle", "MarketplacePriceDisplay", "Address", "PropertyType", "Bedrooms", "Bathrooms", "SquareFeet", "Parking", "Laundry", "Heating", "Cooling", "Garage", "Status"):
        value = fields.get(key)
        if value not in (None, "", [], {}):
            parts.append(f"{key}: {value}")
    url = fields.get("SourceURL") or fields.get("ListingURL") or fields.get("Url")
    if url:
        parts.append(f"URL: {url}")
    return "\n".join(parts[:12]) if parts else "No summary available."


def find_best_listing(query):
    drafts = load_drafts()
    if not drafts:
        return None
    q = normalize_text(query)
    if not q:
        return drafts[0]
    for draft in drafts:
        hay = normalize_text(" ".join(str(draft.get(k, "")) for k in ["MarketplaceTitle", "Address", "MLSNumber", "ListingKey", "PropertyType"]))
        if q in hay:
            return draft
    return drafts[0]


def handle_incoming_events(payload):
    events = []
    for entry in payload.get("entry", []):
        for messaging in entry.get("messaging", []):
            sender = messaging.get("sender", {}).get("id")
            message = messaging.get("message", {})
            text = message.get("text", "")
            if sender and text:
                listing = find_best_listing(text)
                summary = format_listing_summary(listing)
                reply = summary
                if DOC_URL:
                    reply += f"\n\nDoc: {DOC_URL}"
                events.append({"sender": sender, "reply": reply, "listing": listing})
                send_message(sender, reply)
    return events


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _json(self, status, body):
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._json(200, {"ok": True, "service": "messenger-automation"})
            return
        if parsed.path == "/webhook":
            qs = parse_qs(parsed.query)
            mode = qs.get("hub.mode", [""])[0]
            token = qs.get("hub.verify_token", [""])[0]
            challenge = qs.get("hub.challenge", [""])[0]
            if mode == "subscribe" and token == DEFAULT_VERIFY_TOKEN:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(challenge.encode("utf-8"))
            else:
                self._json(403, {"error": "verification failed"})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/webhook":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if not signature_valid(raw, self.headers.get("X-Hub-Signature", "")):
            self._json(403, {"error": "bad signature"})
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self._json(400, {"error": f"invalid json: {exc}"})
            return
        events = handle_incoming_events(payload)
        self._json(200, {"ok": True, "events": len(events), "handled": events})


def run_server():
    server = HTTPServer(("0.0.0.0", DEFAULT_PORT), Handler)
    print(f"Messenger automation listening on :{DEFAULT_PORT}", flush=True)
    server.serve_forever()


def diag_conversations_api():
    if not PAGE_ID or not PAGE_ACCESS_TOKEN:
        return {"ok": False, "error": "META_PAGE_ID and META_PAGE_ACCESS_TOKEN are required"}
    url = f"{GRAPH_BASE}/{PAGE_ID}/conversations"
    r = requests.get(url, params={"access_token": PAGE_ACCESS_TOKEN}, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"text": r.text}
    return {"ok": r.ok, "status": r.status_code, "body": body}


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "conversations":
        print(json.dumps(diag_conversations_api(), indent=2))
        return 0
    run_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
