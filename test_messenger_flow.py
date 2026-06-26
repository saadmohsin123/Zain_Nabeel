#!/usr/bin/env python3
"""Regression checks for the Messenger qualification and booking flow."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import messenger_automation as bot

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
        "MarketplacePriceDisplay": "$2,400/month",
        "MarketplaceDescription": "Bright downtown rental",
    },
    {
        "ListingKey": "W999",
        "MarketplaceStatus": "Pending Seller Action",
        "ListingLifecycleStatus": "Active",
        "TransactionType": "For Lease",
        "MarketplaceTitle": "Hidden Listing",
        "Address": "999 Secret St",
        "City": "Toronto",
        "MarketplacePriceDisplay": "$1,000/month",
    },
]


class FlowTest:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "lead_state.json"
        self.sender = "verification-user"
        self.calendly = "https://calendly.com/example/nabeel"
        self.failures: list[str] = []

    def reply(self, message: str) -> str:
        return bot.build_reply(
            self.sender,
            message,
            SAMPLE_DRAFTS,
            listing_doc_url="",
            calendly_url=self.calendly,
            agent_name="Nabeel",
            lead_state_path=self.state_path,
            openai_api_key="",
            use_ai=False,
        )

    def check(self, name: str, condition: bool, detail: str = ""):
        if not condition:
            self.failures.append(f"{name}: {detail}")

    def run(self):
        r = self.reply("hello")
        self.check("greeting_no_calendly", "calendly" not in r.lower())
        self.check("greeting_no_listings", "matching listings" not in r.lower())

        r = self.reply("looking for a 2 bedroom downtown toronto under 2500")
        self.check("intro_uses_assistant", "assistant" in r.lower())
        self.check("intro_has_opt_in", "would you like" in r.lower())
        self.check("intro_no_early_qual", "move-in" not in r.lower())
        self.check("intro_no_contradiction", "few quick details first" not in r.lower())

        r = self.reply("yes")
        self.check("qual_starts_after_yes", "move-in" in r.lower() or "lease" in r.lower())
        self.check("qual_no_double_preface", r.lower().count("perfect.") <= 1)

        self.reply("June 1, 2 people")
        self.reply("2 adults, 0 kids")
        self.reply("120000, engineer")
        self.reply("PR, no agent")
        r = self.reply("4165551234")
        self.check("post_qual_summary", "collected" in r.lower() or "got everything" in r.lower())
        self.check("post_qual_no_calendly", "calendly" not in r.lower())
        self.check("post_qual_shows_listing", "2 bed downtown condo" in r.lower())
        self.check("post_qual_hides_internal_status", "pending seller action" not in r.lower())
        self.check("post_qual_no_packet", "packet" not in r.lower())

        r = self.reply("book a viewing for 123 King")
        self.check("booking_sends_calendly", "calendly.com" in r.lower())

        hidden = bot.customer_visible_drafts(SAMPLE_DRAFTS)
        self.check("internal_listing_filtered", len(hidden) == 1 and hidden[0]["ListingKey"] == "W123")

        # Screenshot regressions
        def reply_as(sender_id: str, message: str) -> str:
            return bot.build_reply(
                sender_id,
                message,
                SAMPLE_DRAFTS,
                listing_doc_url="",
                calendly_url=self.calendly,
                agent_name="Nabeel",
                lead_state_path=self.state_path,
                openai_api_key="",
                use_ai=False,
            )

        r = reply_as("condo-user", "condo in torronto")
        self.check("condo_summary", "condo" in r.lower() and "toronto" in r.lower())
        self.check("condo_no_fake_bedroom", "2 bedroom" not in r.lower())
        r_repeat = reply_as("condo-user", "condo in torronto")
        self.check("no_full_intro_repeat", "nabeel's assistant" not in r_repeat.lower())
        self.check("repeat_is_short", "reply yes" in r_repeat.lower())

        reply_as("partial-user", "looking for a condo in toronto")
        reply_as("partial-user", "yes")
        r = reply_as("partial-user", "1st of july")
        self.check("partial_move_in_ack", "lease" in r.lower())
        r = reply_as("partial-user", "3")
        self.check("single_number_people_count", "missing" not in r.lower() and "adult" in r.lower())

        # Screenshot batch parsing
        def full_qual(sender_id: str, messages: list[str]) -> str:
            last = ""
            for msg in messages:
                last = reply_as(sender_id, msg)
            return last

        full_qual("parse-user", [
            "condo in toronto", "yes", "1st july and 3 people", "3 adults", "60k", "engineer",
            "Permanent yes i am working woth an agent right now", "4165551234",
        ])
        import json
        answers = json.loads(Path(self.state_path).read_text())["sessions"]["parse-user"]["answers"]
        self.check("kids_default_zero", answers.get("kids_in_unit") == "0")
        self.check("income_60k", answers.get("family_gross_income") == "$60k")
        self.check("agent_yes", answers.get("working_with_agent") == "Yes")
        self.check("resident_clean", answers.get("resident_status") == "Permanent Resident")

        r = reply_as("parse-user", "I want you to show me listings in Ontario")
        self.check("qualified_no_restart", "nabeel's assistant" not in r.lower())
        self.check("qualified_search_reply", "looked again" in r.lower() or "active options" in r.lower())

        # AI must not map income-batch replies into wrong fields
        from unittest.mock import patch

        answers = {"move_in_date": "1st july", "people_on_lease": "3", "adults_in_unit": "3", "kids_in_unit": "0"}
        with patch.object(
            bot,
            "ai_extract_qualification_fields",
            return_value=({"family_gross_income": "3", "occupation": "adults"}, ""),
        ):
            parsed, _ = bot.extract_qualification_from_message(
                2,
                "3 adults",
                dict(answers),
                openai_api_key="fake",
                use_ai=True,
            )
        self.check("ai_wrong_income_rejected", "family_gross_income" not in parsed)
        self.check("ai_wrong_occupation_rejected", "occupation" not in parsed)

        # AI opt-in small talk should not repeat the static nudge
        def reply_with_ai(sender_id: str, message: str) -> str:
            return bot.build_reply(
                sender_id,
                message,
                SAMPLE_DRAFTS,
                listing_doc_url="",
                calendly_url=self.calendly,
                agent_name="Nabeel",
                lead_state_path=self.state_path,
                openai_api_key="fake",
                use_ai=True,
            )

        reply_with_ai("ai-optin-user", "looking for a condo in toronto")
        with patch.object(
            bot,
            "ai_route_conversation_turn",
            return_value={
                "action": "chat",
                "reply": "I'm doing well, thanks! Say yes whenever you'd like me to pull some options.",
            },
        ):
            r = reply_with_ai("ai-optin-user", "How are you doing?")
        self.check("ai_opt_in_small_talk", "well" in r.lower() or "thanks" in r.lower())
        self.check("ai_opt_in_not_static_nudge", "whenever you're ready, just reply yes" not in r.lower())

        # Stale opt-in + greeting should not repeat the static nudge
        import json

        stale_state = {
            "sessions": {
                "stale-user": {
                    "awaiting_opt_in": True,
                    "last_prompt": bot.STATIC_OPT_IN_NUDGE,
                    "search_query": "condo in toronto",
                }
            }
        }
        Path(self.state_path).write_text(json.dumps(stale_state), encoding="utf-8")
        r = reply_as("stale-user", "Hello")
        self.check("stale_hello_not_same_nudge", r != bot.STATIC_OPT_IN_NUDGE)
        r2 = reply_as("stale-user", "How are you doing?")
        self.check("stale_small_talk_human", "well" in r2.lower() or "thanks" in r2.lower())
        self.check("stale_small_talk_not_repeat", r2 != bot.STATIC_OPT_IN_NUDGE)

        shared_listings = [
            {
                "ListingKey": "L1",
                "MarketplaceStatus": "Posted",
                "ListingLifecycleStatus": "Active",
                "TransactionType": "For Lease",
                "MarketplaceTitle": "1 Bed | 1 Bath | Freehold | For Rent | Unit Apt 2",
                "City": "Toronto E01",
                "MarketplacePriceDisplay": "$2,200/month",
            },
            {
                "ListingKey": "L2",
                "MarketplaceStatus": "Posted",
                "ListingLifecycleStatus": "Active",
                "TransactionType": "For Lease",
                "MarketplaceTitle": "1 Bath | Freehold | For Rent | Unit Basement | < 700 sqft",
                "City": "Toronto W05",
                "MarketplacePriceDisplay": "$1,400/month",
                "Address": "55 Basement Lane",
                "BedroomsTotal": "1",
                "BathroomsTotal": "1",
            },
            {
                "ListingKey": "L3",
                "MarketplaceStatus": "Posted",
                "ListingLifecycleStatus": "Active",
                "TransactionType": "For Lease",
                "MarketplaceTitle": "1 Bath | Condo | For Rent | Unit 528 | 0-499 sqft",
                "City": "Toronto C08",
                "MarketplacePriceDisplay": "$1,690/month",
            },
        ]
        session = {
            "qualified": True,
            "last_shared_listing_keys": ["L1", "L2", "L3"],
            "answers": {},
        }
        second = bot.resolve_listing_reference("Tell me about the second one", session, shared_listings)
        self.check("second_listing_resolves", second and second.get("ListingKey") == "L2")
        detail = bot.handle_qualified_listing_interest(
            session,
            "Tell me about the second one",
            shared_listings,
            self.calendly,
            "",
            "gpt-4.1-mini",
            "Nabeel",
        )
        self.check("second_listing_detail", detail and "1,400" in detail and "W05" in detail)
        session["selected_listing_key"] = "L2"
        booking = bot.handle_qualified_listing_interest(
            session,
            "Sure",
            shared_listings,
            self.calendly,
            "",
            "gpt-4.1-mini",
            "Nabeel",
        )
        self.check("sure_sends_calendly", booking and "calendly.com" in booking.lower())

        answers = {"people_on_lease": "1", "adults_in_unit": "2", "kids_in_unit": "0"}
        bot.validate_household_counts(answers)
        self.check("household_counts_fixed", answers.get("adults_in_unit") == "1")

        reply_as("solo-user", "looking for a condo in toronto")
        reply_as("solo-user", "yes")
        r = reply_as("solo-user", "Mid August and it's just going to be me")
        solo_state = json.loads(Path(self.state_path).read_text())["sessions"]["solo-user"]
        solo_answers = solo_state["answers"]
        self.check("solo_move_in", "august" in solo_answers.get("move_in_date", "").lower())
        self.check("solo_people", solo_answers.get("people_on_lease") == "1")
        self.check("solo_adults_inferred", solo_answers.get("adults_in_unit") == "1")
        self.check("solo_skips_household_batch", solo_state.get("batch", 0) >= 2)
        self.check("solo_no_reask_move_in", "when are you looking to move" not in r.lower())
        r2 = reply_as("solo-user", "Just me")
        solo_answers2 = json.loads(Path(self.state_path).read_text())["sessions"]["solo-user"]["answers"]
        self.check("just_me_not_occupation", solo_answers2.get("occupation") != "Just me")
        self.check("just_me_no_move_in_reask", "when are you looking to move" not in r2.lower())

        return self.failures


def main() -> int:
    failures = FlowTest().run()
    if failures:
        print("FAILED")
        for item in failures:
            print(f"- {item}")
        return 1

    print("PASSED: full qualification + booking flow")
    print("PASSED: pre-qual listing gate")
    print("PASSED: internal listing status filter")
    return 0


if __name__ == "__main__":
    sys.exit(main())
