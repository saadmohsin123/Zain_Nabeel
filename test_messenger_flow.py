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
            "ai_compose_turn",
            return_value={
                "fields": {},
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
        r2 = reply_as("stale-user", "How are you doing?")
        self.check("stale_small_talk_human", "well" in r2.lower() or "thanks" in r2.lower() or "yes" in r2.lower())
        self.check("stale_small_talk_not_repeat", r2 != bot.STATIC_OPT_IN_NUDGE)
        r = reply_as("stale-user", "Hello")
        self.check("stale_hello_not_same_nudge", r != bot.STATIC_OPT_IN_NUDGE)

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

        # AI-started qualification without active flag (production bug)
        import json

        ai_state = {
            "sessions": {
                "ai-qual-user": {
                    "search_query": "2 bed 1 bath condo near Ontario",
                    "last_prompt": "Great! When do you need to move in?",
                    "answers": {},
                    "active": False,
                }
            }
        }
        Path(self.state_path).write_text(json.dumps(ai_state), encoding="utf-8")
        r = reply_as("ai-qual-user", "1st pf july")
        ai_answers = json.loads(Path(self.state_path).read_text())["sessions"]["ai-qual-user"]["answers"]
        self.check("typo_july_saved", "july" in ai_answers.get("move_in_date", "").lower())
        self.check("typo_july_no_full_reask", "when are you looking to move" not in r.lower())
        r = reply_as("ai-qual-user", "Me and my brother")
        ai_answers2 = json.loads(Path(self.state_path).read_text())["sessions"]["ai-qual-user"]["answers"]
        self.check("brother_people_count", ai_answers2.get("people_on_lease") == "2")
        self.check("brother_no_move_in_reask", "when are you looking to move" not in r.lower())

        # Ahmed live-test regression: out-of-order answers + double opt-in
        def ahmed_reply(message: str) -> str:
            return reply_as("ahmed-user", message)

        ahmed_reply("Hey")
        ahmed_reply("Im interested in buying proprtie")
        with patch.object(
            bot,
            "ai_compose_turn",
            return_value={"fields": {}, "reply": "Great — I help with rentals. Say yes when you're ready."},
        ):
            ahmed_reply("Yup please go ahead")
        ahmed_state = json.loads(Path(self.state_path).read_text())["sessions"]["ahmed-user"]
        self.check("ahmed_starts_qual_on_first_yes", ahmed_state.get("active") is True)
        self.check("ahmed_not_still_awaiting_opt_in", ahmed_state.get("awaiting_opt_in") is not True)

        r = ahmed_reply("3 adults 1 kid")
        ahmed_answers = json.loads(Path(self.state_path).read_text())["sessions"]["ahmed-user"]["answers"]
        self.check("ahmed_adults_saved", ahmed_answers.get("adults_in_unit") == "3")
        self.check("ahmed_kids_saved", ahmed_answers.get("kids_in_unit") == "1")
        self.check("ahmed_people_inferred", ahmed_answers.get("people_on_lease") == "4")
        self.check("ahmed_asks_move_in_only", "move-in" in r.lower() or "move in" in r.lower())
        self.check("ahmed_no_reask_people", "people on the lease" not in r.lower())

        r = ahmed_reply("1st Jul")
        ahmed_answers2 = json.loads(Path(self.state_path).read_text())["sessions"]["ahmed-user"]["answers"]
        self.check("ahmed_move_in_saved", "jul" in ahmed_answers2.get("move_in_date", "").lower())
        self.check("ahmed_batch2_income", "income" in r.lower() or "work" in r.lower())
        self.check("ahmed_no_reask_lease_after_move_in", "people on the lease" not in r.lower())

        r = ahmed_reply("3 adults")
        ahmed_answers3 = json.loads(Path(self.state_path).read_text())["sessions"]["ahmed-user"]["answers"]
        self.check("ahmed_still_has_people", ahmed_answers3.get("people_on_lease") == "4")
        self.check("ahmed_no_lease_loop", "people on the lease" not in r.lower())

        # One question at a time + resident/agent parsing (screenshot regression)
        reply_as("single-q-user", "looking for a condo in toronto")
        reply_as("single-q-user", "yes")
        reply_as("single-q-user", "June 1")
        reply_as("single-q-user", "2 people")
        reply_as("single-q-user", "2 adults")
        reply_as("single-q-user", "0")
        r = reply_as("single-q-user", "50K")
        self.check("single_q_income_only", "work" in r.lower() or "occupation" in r.lower() or "what do you do" in r.lower())
        self.check("single_q_not_both_res_income", "i still need:" not in r.lower())
        reply_as("single-q-user", "engineer")
        r = reply_as("single-q-user", "Resident")
        sq_answers = json.loads(Path(self.state_path).read_text())["sessions"]["single-q-user"]["answers"]
        self.check("resident_parsed", sq_answers.get("resident_status") == "Permanent Resident")
        self.check("single_q_agent_only", "agent" in r.lower())
        self.check("single_q_not_both_resident_agent", "resident status" not in r.lower())
        r = reply_as("single-q-user", "Nope im not working with an agrny")
        sq_answers2 = json.loads(Path(self.state_path).read_text())["sessions"]["single-q-user"]["answers"]
        self.check("agent_no_parsed", sq_answers2.get("working_with_agent") == "No")
        self.check("single_q_phone_next", "phone" in r.lower())
        self.check("single_q_no_still_need_list", "i still need:" not in r.lower())

        # Anti-bleed regressions from live Messenger tests
        reply_as("agent-loop-user", "looking for a condo in toronto")
        reply_as("agent-loop-user", "yes")
        reply_as("agent-loop-user", "July 1")
        reply_as("agent-loop-user", "1")
        reply_as("agent-loop-user", "1")
        reply_as("agent-loop-user", "0")
        reply_as("agent-loop-user", "100000")
        reply_as("agent-loop-user", "engineer")
        reply_as("agent-loop-user", "Non resident")
        r = reply_as("agent-loop-user", "Yes")
        agent_state = json.loads(Path(self.state_path).read_text())["sessions"]["agent-loop-user"]
        self.check("agent_yes_saved", agent_state["answers"].get("working_with_agent") == "Yes")
        self.check("agent_no_repeat_loop", "working with an agent" not in r.lower() or "phone" in r.lower())

        reply_as("resident-fix-user", "looking for a condo in toronto")
        reply_as("resident-fix-user", "yes")
        reply_as("resident-fix-user", "July 1")
        reply_as("resident-fix-user", "1 person")
        reply_as("resident-fix-user", "100000")
        reply_as("resident-fix-user", "engineer")
        reply_as("resident-fix-user", "Non resident")
        reply_as("resident-fix-user", "Actually I'm a resident")
        resident_state = json.loads(Path(self.state_path).read_text())["sessions"]["resident-fix-user"]["answers"]
        self.check("resident_correction_saved", resident_state.get("resident_status") == "Permanent Resident")

        reply_as("objection-user", "looking for a condo in toronto")
        reply_as("objection-user", "yes")
        reply_as("objection-user", "July 1")
        reply_as("objection-user", "1")
        reply_as("objection-user", "1")
        reply_as("objection-user", "0")
        reply_as("objection-user", "100000")
        r = reply_as("objection-user", "Why do u need this")
        objection_answers = json.loads(Path(self.state_path).read_text())["sessions"]["objection-user"]["answers"]
        self.check("objection_not_occupation", objection_answers.get("occupation") != "Why do u need this")
        self.check("objection_reasks_work", "work" in r.lower())

        reply_as("phone-user", "looking for a condo in toronto")
        reply_as("phone-user", "yes")
        reply_as("phone-user", "July 1")
        reply_as("phone-user", "1")
        reply_as("phone-user", "1")
        reply_as("phone-user", "0")
        reply_as("phone-user", "100000")
        reply_as("phone-user", "engineer")
        reply_as("phone-user", "PR")
        reply_as("phone-user", "No")
        r = reply_as("phone-user", "+1fuckoff")
        phone_answers = json.loads(Path(self.state_path).read_text())["sessions"]["phone-user"]["answers"]
        self.check("invalid_phone_rejected", not phone_answers.get("phone_number"))
        self.check("invalid_phone_reask", "phone" in r.lower())

        three_bed_drafts = SAMPLE_DRAFTS + [
            {
                "ListingKey": "B3",
                "MarketplaceStatus": "Posted",
                "ListingLifecycleStatus": "Active",
                "TransactionType": "For Lease",
                "MarketplaceTitle": "3 Bed | 2 Bath | Condo | For Rent",
                "City": "Toronto",
                "BedroomsTotal": "3",
                "MarketplacePriceDisplay": "$3,200/month",
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
        ]
        matches = bot.rank_drafts("I wanted 3 bedrooms", three_bed_drafts, limit=3)
        self.check("three_bed_filter", len(matches) == 1 and matches[0].get("ListingKey") == "B3")

        qualified_session = {
            "qualified": True,
            "selected_listing_key": "W123",
            "last_shared_listing_keys": ["W123"],
            "answers": {},
            "last_prompt": "Perfect — pick a time here: https://calendly.com/example/nabeel",
        }
        r = bot.build_qualified_reply(
            qualified_session,
            "Hi",
            three_bed_drafts,
            "",
            self.calendly,
            "Nabeel",
            "",
            "gpt-4.1",
            use_ai=False,
        )
        self.check("qualified_hi_no_listing_dump", "2,150" not in r.lower() and "sheppard" not in r.lower())
        self.check("qualified_hi_short", "still here" in r.lower() or "refine" in r.lower())

        # Screenshot regression: hello then search should not double-intro (AI path)
        def reply_ai(sender_id: str, message: str) -> str:
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

        with patch.object(
            bot,
            "ai_compose_turn",
            return_value={
                "fields": {},
                "reply": "Hi! I'm Nabeel's assistant at Durham New Homes—thanks for reaching out. What kind of place, area, or price range are you looking for?",
            },
        ):
            r1 = reply_ai("sync-user", "hello")
        r2 = reply_ai("sync-user", "Actually I am looking for 2bed Condo in Ontario")
        sync_state = json.loads(Path(self.state_path).read_text())["sessions"]["sync-user"]
        self.check("sync_awaiting_opt_in", sync_state.get("awaiting_opt_in") is True)
        self.check("sync_search_saved", "condo" in sync_state.get("search_query", "").lower())
        self.check("sync_acknowledges_search", "condo" in r2.lower() or "2 bedroom" in r2.lower() or "ontario" in r2.lower())
        self.check("sync_no_second_hi_intro", not (r2.lower().startswith("hi,") or r2.lower().startswith("hi!")))
        self.check("sync_has_yes_prompt", "yes" in r2.lower())
        intro_count = sum(1 for t in [r1, r2] if "nabeel's assistant" in t.lower())
        self.check("sync_single_assistant_intro", intro_count == 1)

        # State I/O: atomic JSON + concurrent session updates
        import concurrent.futures

        race_path = Path(self.tmp.name) / "race_state.json"
        race_drafts = SAMPLE_DRAFTS

        def race_turn(index: int) -> str:
            return bot.build_reply(
                "race-user",
                f"hello from thread {index}",
                race_drafts,
                listing_doc_url="",
                calendly_url=self.calendly,
                agent_name="Nabeel",
                lead_state_path=race_path,
                openai_api_key="",
                use_ai=False,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(race_turn, range(24)))

        race_payload = json.loads(race_path.read_text(encoding="utf-8"))
        self.check("race_state_valid_json", isinstance(race_payload.get("sessions"), dict))
        self.check("race_user_persisted", "race-user" in race_payload["sessions"])
        self.check("race_last_prompt_saved", bool(race_payload["sessions"]["race-user"].get("last_prompt")))

        poll_path = Path(self.tmp.name) / "poll_state.json"
        bot.with_poll_state(poll_path, lambda seen: seen.update({"m1", "m2"}))
        poll_payload = json.loads(poll_path.read_text(encoding="utf-8"))
        self.check("poll_state_atomic", "m1" in poll_payload.get("seen_message_ids", []))

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
