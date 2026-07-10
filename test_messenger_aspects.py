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
        self.check("constraint_around_price", c2.get("max_price") == int(2200 * 1.2))
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

        self.reply("objection-user", "2 bed toronto")
        self.reply("objection-user", "yes")
        self.reply("objection-user", "July 1")
        self.reply("objection-user", "1")
        self.reply("objection-user", "90000")
        r_obj = self.reply("objection-user", "Why do you need this")
        self.check("objection_explained", "fair question" in r_obj.lower() or "match" in r_obj.lower())
        self.check("objection_reasks", "work" in r_obj.lower() or "occupation" in r_obj.lower())

        self.reply("greeting-qual-user", "2 bed toronto")
        self.reply("greeting-qual-user", "yes")
        self.reply("greeting-qual-user", "July 1")
        self.reply("greeting-qual-user", "1")
        self.reply("greeting-qual-user", "90000")
        r_hi = self.reply("greeting-qual-user", "Hello")
        self.check("greeting_during_qual", "still here" in r_hi.lower() or "work" in r_hi.lower())

    def run_search_change_tests(self) -> None:
        sender = "search-change-user"
        self.reply(sender, "2 bedroom in Toronto under 2500")
        self.reply(sender, "yes")
        self.reply(sender, "May 1")
        self.reply(sender, "1")
        r = self.reply(sender, "I'm looking for a 2 bedroom in Markham")
        state = self.session(sender)
        self.check("search_change_restarts_qual", state.get("active") is True)
        self.check("search_change_markham_saved", "markham" in state.get("search_query", "").lower())
        self.check("search_change_clears_stale_answers", not state.get("answers", {}).get("move_in_date"))
        self.check("search_change_asks_move_in", "move-in" in r.lower() or "move in" in r.lower())

    def run_special_search_tests(self) -> None:
        query = (
            "cheapest condo in Ontario where pets are allowed and "
            "it's a gated community with security"
        )
        c = bot.extract_search_constraints(query)
        self.check("special_condo", c.get("property_type") == "condo")
        self.check("special_pets", c.get("pets_allowed") is True)
        self.check("special_security", c.get("gated_or_security") is True)
        self.check("special_cheapest", c.get("sort_cheapest") is True)
        matches, note = bot.rank_drafts_with_note(query, SAMPLE_DRAFTS, limit=3)
        self.check("special_no_commercial", all(d.get("ListingKey") != "C1" for d in matches))
        if note:
            self.check("special_honest_note", "gated" in note.lower() or "security" in note.lower())

        soft = bot.extract_search_constraints("2 bedroom apartment in Mississauga budget $2100")
        self.check("soft_budget_above_target", int(soft.get("max_price") or 0) >= 2400)
        self.check("soft_budget_city", soft.get("city") == "mississauga")

        tight_drafts = SAMPLE_DRAFTS + [
            {
                "ListingKey": "M1809",
                "MarketplaceStatus": "Posted",
                "ListingLifecycleStatus": "Active",
                "TransactionType": "For Lease",
                "MarketplaceTitle": "2 Bed | 2 Bath | Condo | For Rent | Unit 1809",
                "Address": "100 Burnhamthorpe Rd, Mississauga",
                "City": "Mississauga",
                "BedroomsTotal": "2",
                "MarketplacePrice": 2499,
                "MarketplacePriceDisplay": "$2,499/month",
            },
            {
                "ListingKey": "T715",
                "MarketplaceStatus": "Posted",
                "ListingLifecycleStatus": "Active",
                "TransactionType": "For Lease",
                "MarketplaceTitle": "2 Bed | 2 Bath | Condo | For Rent | Unit 715",
                "Address": "3429 Sheppard Ave E, Toronto",
                "City": "Toronto E05",
                "BedroomsTotal": "2",
                "MarketplacePrice": 2150,
                "MarketplacePriceDisplay": "$2,150/month",
            },
        ]
        miss_matches, _ = bot.rank_drafts_with_note(
            "2 bedroom apartment in Mississauga budget $2100",
            tight_drafts,
            limit=3,
        )
        self.check("mississauga_soft_budget_finds_listing", any(d.get("ListingKey") == "M1809" for d in miss_matches))

        session = {"search_query": "2 bedroom apartment in Mississauga budget $2100"}
        toronto_merged = bot.merge_search_queries(
            session,
            "show me listings in Toronto for the same requirements",
        )
        self.check("toronto_merge_replaces_city", "toronto" in toronto_merged.lower())
        self.check("toronto_merge_drops_mississauga", "mississauga" not in toronto_merged.lower())
        toronto_matches, _ = bot.rank_drafts_with_note(toronto_merged, tight_drafts, limit=3)
        self.check("toronto_same_requirements_finds_listing", any(d.get("ListingKey") == "T715" for d in toronto_matches))

        broaden_merged = bot.merge_search_queries(session, "Broaden the search and send me options")
        self.check("broaden_drops_city", "mississauga" not in broaden_merged.lower())
        broaden_matches, _ = bot.rank_drafts_with_note(broaden_merged, tight_drafts, limit=3)
        self.check("broaden_returns_listings", len(broaden_matches) >= 1)

        hard_empty, hard_note = bot.rank_drafts_with_note(
            "2 bedroom in Mississauga under 2100",
            tight_drafts,
            limit=3,
        )
        self.check(
            "hard_budget_still_shows_nearest",
            any(d.get("ListingKey") == "M1809" for d in hard_empty),
        )
        self.check(
            "hard_budget_nearest_note",
            "closest" in hard_note.lower() or "budget" in hard_note.lower(),
        )

        idle_session = {
            "qualified": True,
            "last_shared_listing_keys": ["N12664602"],
            "selected_listing_key": "N12664602",
            "last_sent_at": int(__import__("time").time()) - (13 * 3600),
            "last_prompt": "Here is the Newmarket listing for $3400",
        }
        self.check("fresh_day_greeting_detected", bot.is_fresh_day_greeting("Hey", idle_session))
        fresh_reply = bot.qualified_conversational_reply(idle_session, "Nabeel", "Hey")
        self.check(
            "fresh_day_greeting_generic",
            "newmarket" not in fresh_reply.lower() and "3400" not in fresh_reply and "listing" not in fresh_reply.lower(),
        )
        self.check("fresh_day_clears_listing_focus", not idle_session.get("last_shared_listing_keys"))
        recent_session = {
            "qualified": True,
            "last_shared_listing_keys": ["N12664602"],
            "last_sent_at": int(__import__("time").time()) - 60,
        }
        self.check("recent_greeting_not_fresh", not bot.is_fresh_day_greeting("Hey", recent_session))
        self.check(
            "greeting_skips_prior_context",
            not bot.needs_prior_conversation_context("Hey", recent_session),
        )
        self.check(
            "new_search_skips_prior_context",
            not bot.needs_prior_conversation_context("2 bed Toronto under 2500", recent_session),
        )
        self.check(
            "pet_followup_needs_prior_context",
            bot.needs_prior_conversation_context("Ok cool, does it allow pets?", recent_session),
        )
        self.check(
            "resume_needs_prior_context",
            bot.needs_prior_conversation_context("What were we talking about?", recent_session),
        )

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
        self.check("opt_out_still_responds", len(r2) > 10)

        self.check(
            "profanity_sanitized",
            bot.sanitize_bot_reply("Bhen ke laude") == "I'm here to help with rentals. Tell me the area, budget, or unit type you're looking for.",
        )

    def run_qual_resume_tests(self) -> None:
        sender = "mid-qual-user"
        self.reply(sender, "2 bed toronto")
        self.reply(sender, "yes")
        self.reply(sender, "July 1")
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        payload["sessions"][sender]["active"] = False
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")
        r = self.reply(sender, "Hello")
        state = self.session(sender)
        self.check("mid_qual_no_opt_in_restart", "just say yes" not in r.lower())
        self.check("mid_qual_resumes_active", state.get("active") is True)
        self.check(
            "mid_qual_asks_next_field",
            "people" in r.lower() or "lease" in r.lower() or "still here" in r.lower(),
        )

        profanity_sender = "profanity-qual-user"
        self.reply(profanity_sender, "2 bed toronto")
        self.reply(profanity_sender, "yes")
        self.reply(profanity_sender, "July 1")
        r_profanity = self.reply(profanity_sender, "laude tumhari tameez")
        self.check("profanity_not_mirrored", not bot.contains_profanity(r_profanity))
        self.check("profanity_stays_on_qual", "people" in r_profanity.lower() or "lease" in r_profanity.lower())

    def run_qualified_refinement_tests(self) -> None:
        sender = "refine-user"
        self.reply(sender, "2 bedroom downtown toronto under 2500")
        self.reply(sender, "yes")
        self.reply(sender, "June 1")
        self.reply(sender, "1")
        self.reply(sender, "120000")
        self.reply(sender, "engineer")
        self.reply(sender, "PR")
        self.reply(sender, "No")
        self.reply(sender, "4165551234")
        r = self.reply(sender, "I wanted 3 bedrooms")
        self.check("refinement_returns_listings", "here are" in r.lower() or "nothing active" in r.lower())
        self.check("refinement_no_commercial", "commercial" not in r.lower())

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

    def run_zain_regression_tests(self) -> None:
        toronto_drafts = [
            {
                "ListingKey": "T715",
                "MarketplaceStatus": "Posted",
                "ListingLifecycleStatus": "Active",
                "TransactionType": "For Lease",
                "MarketplaceTitle": "2 Bed | 2 Bath | Condo | For Rent | Unit 715",
                "Address": "100 Front St, Toronto",
                "City": "Toronto",
                "BedroomsTotal": "2",
                "MarketplacePrice": 2150,
                "MarketplacePriceDisplay": "$2,150/month",
            },
            {
                "ListingKey": "T501",
                "MarketplaceStatus": "Posted",
                "ListingLifecycleStatus": "Active",
                "TransactionType": "For Lease",
                "MarketplaceTitle": "2 Bed | 1 Bath | Condo | For Rent | Unit 501",
                "Address": "200 King St E, Toronto",
                "City": "Toronto",
                "BedroomsTotal": "2",
                "MarketplacePrice": 2300,
                "MarketplacePriceDisplay": "$2,300/month",
            },
            {
                "ListingKey": "T902",
                "MarketplaceStatus": "Posted",
                "ListingLifecycleStatus": "Active",
                "TransactionType": "For Lease",
                "MarketplaceTitle": "2 Bed | 1 Bath | Condo | For Rent | Unit 902",
                "Address": "300 Queen St W, Toronto",
                "City": "Toronto",
                "BedroomsTotal": "2",
                "MarketplacePrice": 2450,
                "MarketplacePriceDisplay": "$2,450/month",
            },
            {
                "ListingKey": "T2612",
                "MarketplaceStatus": "Posted",
                "ListingLifecycleStatus": "Active",
                "TransactionType": "For Lease",
                "MarketplaceTitle": "2 Bed | 2 Bath | Condo | For Rent | Unit 2612",
                "Address": "400 Bay St, Toronto",
                "City": "Toronto",
                "BedroomsTotal": "2",
                "MarketplacePrice": 2550,
                "MarketplacePriceDisplay": "$2,550/month",
            },
        ]
        sender = "zain-regression-user"
        for step in (
            "2 bed toronto",
            "yes",
            "June 1",
            "1",
            "120000",
            "engineer",
            "PR",
            "No",
            "4165551234",
        ):
            self.reply(sender, step, drafts=toronto_drafts)

        r_toronto = self.reply(sender, "I'm looking specifically in Toronto", drafts=toronto_drafts)
        self.check(
            "zain_toronto_not_calendly",
            "calendly.com" not in r_toronto.lower(),
            r_toronto[:120],
        )
        self.check(
            "zain_toronto_shows_listings",
            "here are" in r_toronto.lower() or r_toronto.count("- ") >= 1,
            r_toronto[:120],
        )

        r_list = self.reply(sender, "Send me listings of 2 bedrooms from toronto", drafts=toronto_drafts)
        lines1 = [line for line in r_list.splitlines() if line.strip().startswith("-")]
        self.check("zain_three_listings", len(lines1) >= 3, f"got {len(lines1)}")

        r_more = self.reply(sender, "Send other options", drafts=toronto_drafts)
        lines2 = [line for line in r_more.splitlines() if line.strip().startswith("-")]
        overlap = set(lines1).intersection(lines2)
        self.check(
            "zain_more_options_new",
            len(lines2) >= 1 and (not overlap or "all the active" in r_more.lower()),
            f"overlap={len(overlap)}",
        )

        r_q = self.reply(sender, "??", drafts=toronto_drafts)
        self.check(
            "zain_question_marks_helpful",
            len([line for line in r_q.splitlines() if line.strip().startswith("-")]) >= 1
            or "all the active" in r_q.lower()
            or "refine" in r_q.lower(),
            r_q[:120],
        )
        self.check(
            "specifically_not_booking",
            not bot.looks_like_booking_request("I'm looking specifically in Toronto"),
        )

    def run_family_search_tests(self) -> None:
        q = "Can you help me find new homes for my family?"
        self.check("family_search_intent", bot.wants_listing_help(q))
        self.check("family_search_refinement", bot.looks_like_search_refinement(q))

        sender = "family-search-user"
        for step in (
            "2 bed toronto", "yes", "June 1", "1", "120000",
            "engineer", "PR", "No", "4165551234",
        ):
            self.reply(sender, step, use_ai=False)
        r = self.reply(sender, q, use_ai=False)
        self.check("family_search_gets_listings", "here are" in r.lower() or "help" in r.lower())

    def run_listing_followup_tests(self) -> None:
        session = {
            "qualified": True,
            "selected_listing_key": "N13249904",
            "last_shared_listing_keys": ["N13249904"],
        }
        self.check(
            "pet_question_not_booking_confirmation",
            not bot.looks_like_booking_confirmation("Ok cool, does it allows pet?", session),
        )
        self.check(
            "ok_only_is_booking_confirmation_with_selection",
            bot.looks_like_booking_confirmation("ok cool", session),
        )
        self.check(
            "pet_question_is_listing_followup",
            bot.looks_like_listing_followup_question("Ok cool, does it allows pet?"),
        )
        self.check(
            "listing_search_not_followup",
            not bot.looks_like_listing_followup_question("Send me listings of 2 bedrooms from toronto"),
        )
        self.check(
            "refinement_not_followup",
            not bot.looks_like_listing_followup_question("I wanted 3 bedrooms"),
        )

        pet_listing = {
            "ListingKey": "N13249904",
            "MarketplaceStatus": "Posted",
            "ListingLifecycleStatus": "Active",
            "TransactionType": "For Lease",
            "MarketplaceTitle": "4 Bed House Newmarket",
            "Address": "71 Gail Parks Crescent, Newmarket, ON",
            "City": "Newmarket",
            "BedroomsTotal": "4",
            "PetsAllowed": "Yes",
            "MarketplacePriceDisplay": "$3,500/month",
        }
        interest = bot.handle_qualified_listing_interest(
            session,
            "Ok cool, does it allows pet?",
            SAMPLE_DRAFTS + [pet_listing],
            self.calendly,
            "",
            "",
            "Nabeel",
        )
        self.check("pet_followup_not_calendly_first", interest and "calendly.com" not in interest.lower())
        self.check("pet_followup_mentions_pets", interest and "pet" in interest.lower())

        sender = "listing-followup-user"
        self.reply(sender, "Hi", use_ai=False)
        r = self.reply(sender, "Toronto 2 bed", use_ai=False)
        s2 = self.session(sender)
        history = s2.get("message_history") or []
        self.check("message_history_saved", len(history) >= 2)
        self.check("message_history_has_user_turn", any(item.get("role") == "user" for item in history))
        self.check("message_history_has_assistant_turn", any(item.get("role") == "assistant" for item in history))

    def run_stable_mode_tests(self) -> None:
        cfg = bot.MessengerConfig(
            page_access_token="token",
            verify_token="verify",
            stable_mode=True,
            openai_api_key="sk-fake-key",
        )
        self.check("stable_mode_allows_ai", bot.resolve_use_ai(cfg))
        self.check("stable_mode_overrides_explicit_false", bot.resolve_use_ai(cfg, explicit=False) is False)

        cfg_normal = bot.MessengerConfig(
            page_access_token="token",
            verify_token="verify",
            stable_mode=False,
            openai_api_key="sk-fake-key",
        )
        self.check("normal_mode_uses_ai_when_key_set", bot.resolve_use_ai(cfg_normal))

        sender = "stable-flow-user"
        self.reply(sender, "2 bed in Toronto under 2500", use_ai=False)
        s = self.session(sender)
        self.check("stable_flow_opt_in", s.get("awaiting_opt_in") and "toronto" in s.get("search_query", "").lower())
        r = self.reply(sender, "yes", use_ai=False)
        self.check("stable_flow_qual_start", "move-in" in r.lower() or "move in" in r.lower())

        with patch.object(
            bot,
            "call_openai_json",
            side_effect=AssertionError("OpenAI must not be called when use_ai=False"),
        ):
            self.reply("stable-no-ai-user", "2 bed toronto", use_ai=False)
            self.reply("stable-no-ai-user", "yes", use_ai=False)
            self.reply("stable-no-ai-user", "June 1", use_ai=False)
        self.check("stable_no_openai_calls", True)

    def run(self) -> list[str]:
        self.run_search_tests()
        self.run_session_tests()
        self.run_opt_in_tests()
        self.run_qual_edge_tests()
        self.run_booking_tests()
        self.run_security_tests()
        self.run_opt_out_tests()
        self.run_ai_path_tests()
        self.run_qual_resume_tests()
        self.run_qualified_refinement_tests()
        self.run_search_change_tests()
        self.run_special_search_tests()
        self.run_zain_regression_tests()
        self.run_family_search_tests()
        self.run_listing_followup_tests()
        self.run_stable_mode_tests()
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
    print("PASSED: qual resume, profanity, and post-qual refinement")
    print("PASSED: Zain Toronto refine and more-options flow")
    print("PASSED: listing follow-up questions and message history")
    print("PASSED: STABLE_MODE keeps AI enabled with poll off")
    return 0


if __name__ == "__main__":
    sys.exit(main())
