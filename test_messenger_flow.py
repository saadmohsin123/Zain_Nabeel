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
