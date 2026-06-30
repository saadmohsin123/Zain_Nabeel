#!/usr/bin/env python3
"""Additional Messenger bot tests across search, sessions, security, and edge cases."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import messenger_automation as bot
import session_store

SAMPLE_DRAFTS = [
    {
        "ListingKey": "W123",
        "MarketplaceStatus": "Posted",
        "ListingLifecycleStatus": "Active",
        "TransactionType": "For Lease",
        "MarketplaceTitle": "2 Bed Downtown Condo",
        "Address": "123 King St W, Toronto",
        "City": "Toronto",
        "BedroomsTotal": "2",
        "MarketplacePrice": 2400,
        "MarketplacePriceDisplay": "$2,400/month",
    },
    {
        "ListingKey": "O1",
        "MarketplaceStatus": "Posted",
        "ListingLifecycleStatus": "Active",
        "TransactionType": "For Lease",
        "MarketplaceTitle": "3 Bed | 2 Bath | Freehold | For Rent",
        "Address": "10 Bond St, Oshawa, ON",
        "City": "Oshawa",
        "BedroomsTotal": "3",
        "MarketplacePrice": 2200,
        "MarketplacePriceDisplay": "$2,200/month",
    },
    {
        "ListingKey": "C1",
        "MarketplaceStatus": "Posted",
        "ListingLifecycleStatus": "Active",
        "TransactionType": "For Lease",
        "MarketplaceTitle": "Commercial | For Rent | Unit LL-D",
        "City": "Markham",
        "MarketplacePriceDisplay": "$1,400/month",
    },
    {
        "ListingKey": "W999",
        "MarketplaceStatus": "Pending Seller Action",
        "ListingLifecycleStatus": "Active",
        "TransactionType": "For Lease",
        "MarketplaceTitle": "Hidden Listing",
        "City": "Toronto",
        "MarketplacePriceDisplay": "$1,000/month",
    },
]


class AspectTest:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "lead_state.json"
        self.calendly = "https://calendly.com/example/nabeel"
        self.failures: list[str] = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if not condition:
            self.failures.append(f"{name}: {detail}")

    def reply(
        self,
        sender_id: str,
        message: str,
        *,
        use_ai: bool = False,
        drafts: list | None = None,
    ) -> str:
        return bot.build_reply(
            sender_id,
            message,
            drafts or SAMPLE_DRAFTS,
            listing_doc_url="",
            calendly_url=self.calendly,
            agent_name="Nabeel",
            lead_state_path=self.state_path,
            openai_api_key="fake" if use_ai else "",
            use_ai=use_ai,
        )

    def session(self, sender_id: str) -> dict:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        return payload.get("sessions", {}).get(sender_id, {})

    def run_search_tests(self) -> None:
        c = bot.extract_search_constraints("3 bedroom in Oshawa under 2500")
        self.check("constraint_bedrooms", c.get("bedrooms") == 3)
        self.check("constraint_city", c.get("city") == "oshawa")
        self.check("constraint_max_price", c.get("max_price") == 2500)

        c2 = bot.extract_search_constraints("condo around $2200 in Mississauga")
        self.check("constraint_around_price", c2.get("max_price") == int(2200 * 1.15))
        self.check("constraint_mississauga", c2.get("city") == "mississauga")

        self.check(
            "describe_oshawa",
            "oshawa" in bot.describe_search_preferences("3 bed in Oshawa under 2500").lower(),
        )
        self.check(
            "describe_bedroom",
            "3 bedroom" in bot.describe_search_preferences("3 bed in Oshawa under 2500").lower(),
        )

        toronto = bot.rank_drafts("2 bedroom in Toronto under 2500", SAMPLE_DRAFTS, limit=3)
        self.check("toronto_match", len(toronto) == 1 and toronto[0]["ListingKey"] == "W123")
        oshawa = bot.rank_drafts("3 bedroom in Oshawa under 2500", SAMPLE_DRAFTS, limit=3)
        self.check("oshawa_match", len(oshawa) == 1 and oshawa[0]["ListingKey"] == "O1")
        wrong_city = bot.rank_drafts("3 bedroom in Oshawa under 2500", SAMPLE_DRAFTS, limit=3)
        self.check("no_toronto_in_oshawa", all("oshawa" in bot.draft_text(d) for d in wrong_city))

        commercial = bot.rank_drafts("3 bedroom", SAMPLE_DRAFTS, limit=5)
        self.check("commercial_excluded_with_beds", all(d["ListingKey"] != "C1" for d in commercial))

        merged = bot.merge_search_queries(
            {"search_query": "3 bedroom in Oshawa under 2500"},
            "anything available?",
        )
        self.check("merge_keeps_city", "oshawa" in merged.lower())
        self.check("merge_keeps_beds", "3 bedroom" in merged.lower())

        self.check("draft_price_parse", bot.draft_listing_price(SAMPLE_DRAFTS[0]) == 2400)
        self.check("draft_city_match", bot.draft_matches_city(SAMPLE_DRAFTS[1], "oshawa"))

    def run_session_tests(self) -> None:
        self.reply("user-a", "hello")
        self.reply("user-b", "looking for a 2 bedroom in toronto")
        a = self.session("user-a")
        b = self.session("user-b")
        self.check("isolated_sessions", a.get("search_query", "") != b.get("search_query", ""))
        self.check("user_b_has_search", "toronto" in b.get("search_query", "").lower())
        self.check("user_a_no_qual", not a.get("active") and not a.get("awaiting_opt_in"))

        defaults = session_store.merge_session_defaults({})
        self.check("session_defaults_active", defaults.get("active") is False)
        self.check("session_defaults_answers", isinstance(defaults.get("answers"), dict))

    def run_opt_in_tests(self) -> None:
        self.reply("decline-user", "2 bed condo in toronto")
        r = self.reply("decline-user", "no thanks")
        state = self.session("decline-user")
        self.check("decline_not_active", not state.get("active"))
        self.check("decline_no_move_in", "move-in" not in r.lower())

        self.reply("pushback-user", "2 bed in toronto")
        r = self.reply("pushback-user", "maybe later")
        self.check("pushback_no_qual", "move-in" not in r.lower() or "lease" not in r.lower())

    def run_qual_edge_tests(self) -> None:
        self.reply("book-early-user", "hello")
        r = self.reply("book-early-user", "book a viewing tomorrow")
        self.check("no_early_booking", "calendly.com" not in r.lower())

        self.reply("income-edge-user", "condo toronto")
        self.reply("income-edge-user", "yes")
        self.reply("income-edge-user", "July 1")
        self.reply("income-edge-user", "2")
        self.reply("income-edge-user", "2")
        self.reply("income-edge-user", "0")
        r = self.reply("income-edge-user", "60k")
        answers = self.session("income-edge-user").get("answers", {})
        self.check("income_60k_saved", answers.get("family_gross_income") == "$60k")
        self.check("income_rejects_5", not bot.is_plausible_field_value("family_gross_income", "5", "5", {}))

        self.reply("household-user", "condo toronto")
        self.reply("household-user", "yes")
        self.reply("household-user", "July 1")
        self.reply("household-user", "2")
        self.reply("household-user", "2")
        self.reply("household-user", "0")
        household = self.session("household-user").get("answers", {})
        self.check("household_adults_saved", household.get("adults_in_unit") == "2")
        self.check("household_kids_saved", household.get("kids_in_unit") == "0")

    def run_booking_tests(self) -> None:
        sender = "booking-user"
        self.reply(sender, "2 bedroom downtown toronto under 2500")
        self.reply(sender, "yes")
        self.reply(sender, "June 1")
        self.reply(sender, "1")
        self.reply(sender, "120000")
        self.reply(sender, "engineer")
        self.reply(sender, "PR")
        self.reply(sender, "No")
        self.reply(sender, "4165551234")
        r = self.reply(sender, "book a viewing for 123 King")
        self.check("post_qual_booking", "calendly.com" in r.lower())

        sender2 = "booking-generic-user"
        self.reply(sender2, "2 bedroom downtown toronto under 2500")
        self.reply(sender2, "yes")
        self.reply(sender2, "June 1")
        self.reply(sender2, "1")
        self.reply(sender2, "120000")
        self.reply(sender2, "engineer")
        self.reply(sender2, "PR")
        self.reply(sender2, "No")
        self.reply(sender2, "4165551234")
        r2 = self.reply(sender2, "I'd like to schedule a call")
        self.check("generic_booking_intent", "calendly.com" in r2.lower())

    def run_security_tests(self) -> None:
        secret = "test-secret"
        body = b'{"object":"page"}'
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.check("signature_valid", bot.verify_signature(secret, body, sig))
        self.check("signature_invalid", not bot.verify_signature(secret, body, "sha256=deadbeef"))
        self.check("signature_missing", not bot.verify_signature(secret, body, None))

    def run_ai_path_tests(self) -> None:
        with patch.object(
            bot,
            "ai_compose_turn",
            return_value={"fields": {}, "reply": "Sounds good — say yes when ready."},
        ):
            self.reply("ai-path-user", "hello", use_ai=True)
            r = self.reply("ai-path-user", "2 bed condo in toronto", use_ai=True)
        state = self.session("ai-path-user")
        self.check("ai_path_opt_in", state.get("awaiting_opt_in") is True)
        self.check("ai_path_search_saved", "toronto" in state.get("search_query", "").lower())
        self.check("ai_path_no_double_hi", r.lower().count("nabeel's assistant") <= 1)

    def run_opt_out_tests(self) -> None:
        self.reply("pause-user", "looking for condo in toronto")
        r = self.reply("pause-user", "Stop messaging me")
        state = self.session("pause-user")
        self.check("opt_out_pauses", state.get("messaging_paused") is True)
        self.check("opt_out_ack", "stop" in r.lower())
        r2 = self.reply("pause-user", "random follow up")
        self.check("opt_out_suppresses", r2 == "")

        self.check(
            "profanity_sanitized",
            bot.sanitize_bot_reply("Bhen ke laude") == "I'm here to help with rentals. Tell me the area, budget, or unit type you're looking for.",
        )

    def run_poll_state_tests(self) -> None:
        poll_path = Path(self.tmp.name) / "poll.json"

        def add_ids(seen: set[str]) -> None:
            seen.update({"alpha", "beta", "gamma"})

        bot.with_poll_state(poll_path, add_ids)
        payload = json.loads(poll_path.read_text(encoding="utf-8"))
        self.check("poll_persists_ids", "alpha" in payload.get("seen_message_ids", []))

        def noop(seen: set[str]) -> None:
            seen.update({"alpha"})

        bot.with_poll_state(poll_path, noop)
        payload2 = json.loads(poll_path.read_text(encoding="utf-8"))
        self.check("poll_idempotent_add", payload2.get("seen_message_ids", []).count("alpha") == 1)

    def run(self) -> list[str]:
        self.run_search_tests()
        self.run_session_tests()
        self.run_opt_in_tests()
        self.run_qual_edge_tests()
        self.run_booking_tests()
        self.run_security_tests()
        self.run_opt_out_tests()
        self.run_ai_path_tests()
        self.run_poll_state_tests()
        return self.failures


def main() -> int:
    failures = AspectTest().run()
    if failures:
        print("FAILED")
        for item in failures:
            print(f"- {item}")
        return 1
    print("PASSED: search constraints and ranking")
    print("PASSED: session isolation and defaults")
    print("PASSED: opt-in decline and edge cases")
    print("PASSED: booking and security checks")
    print("PASSED: AI path and poll state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
