# Durham New Homes Messenger Bot — Prompts & Flow Reference

This document lists every prompt the bot uses: **hardcoded templates** (what users actually see in production) and **OpenAI system prompts** (used only when `STABLE_MODE` is off and `OPENAI_API_KEY` is set).

## Production architecture (recommended)

| Setting | Value |
|---------|--------|
| `OPENAI_API_KEY` | **Required** — OpenAI composes every reply in a natural, human voice |
| `STABLE_MODE` | `1` — webhook-only (disables poll; **does not** disable AI) |
| `POLL_CONVERSATIONS_SECONDS` | `0` |
| Reply path | `build_reply` → `_unified_ai_turn` → `AI_MASTER_SYSTEM_PROMPT` |
| Listings | Google Sheet data injected into AI context — never invented |
| Sessions | PostgreSQL `messenger_sessions` per sender |

When **`STABLE_MODE=1`**: no conversation poll (webhook-only). OpenAI stays on if `OPENAI_API_KEY` is set.

When **`OPENAI_API_KEY` is missing**: bot falls back to deterministic templates (`_reply_deterministic`).

---

## Conversation pipeline (strict order)

1. **NEW** — Greet; learn area / budget / unit type
2. **AWAITING_OPT_IN** — Offer free listing help; user must say **yes**
3. **QUALIFYING** — Collect all qualification fields (one batch at a time)
4. **QUALIFIED** — Show sheet listings, refinements, booking (Calendly)

---

## Hardcoded user-facing templates

### Greeting (new user)

```
Hi! I'm {agent_name}'s assistant at Durham New Homes.
Tell me the area, budget, or type of place you're looking for and I'll help from there.
```

### Opt-in (`qualification_opt_in_prompt`)

```
Got it — you're looking for {search_summary}.

That's great. I'm {agent_name}'s assistant and I can help make your search easier.
I have access to rentals beyond Facebook as well, and there is no cost to you.

Would you like me to send you a list of the best active options?
Just say yes and I'll ask a few quick questions first.
```

### Qualification questions (`QUALIFICATION_STEPS`)

| Field | Prompt |
|-------|--------|
| `move_in_date` | What's your expected move-in date? |
| `people_on_lease` | How many people will be on the lease? |
| `adults_in_unit` | How many adults will be living in the unit? |
| `kids_in_unit` | How many kids will be living in the unit? |
| `family_gross_income` | What's your total family gross income? Please do not include cash income. |
| `occupation` | What do you do for work? |
| `resident_status` | What is your resident status in Canada? |
| `working_with_agent` | Are you currently working with an agent? |
| `phone_number` | And what's the best phone number to reach you on? |

### Post-qualification listings

```
Here are a few that fit:
- {title} ({price}, {city})
...
Like one? Send the address or ListingKey and I'll help with next steps.
```

### More options (pagination)

```
Here are a few more options:
...
```

Or when exhausted:

```
Those are all the active options I have for that search right now.
Tell me if you want to change the city, budget, or bedrooms.
```

### Booking (Calendly)

Only when user explicitly asks to book / schedule / viewing (word-boundary match, not substring):

```
Perfect — pick a time here: {calendly_url}
Please add the address or ListingKey for the unit you want in the booking notes.
```

### Fallback (qualified)

```
Tell me the address, ListingKey, or what you'd like to refine —
area, budget, or bedrooms — and I'll pull matching options.
```

---

## OpenAI system prompts (disabled when `STABLE_MODE=1`)

### 1. `AI_MASTER_SYSTEM_PROMPT` (legacy compose path — not used by webhook)

Used only by `_unified_ai_turn` / `ai_compose_turn`. Production webhooks use `_reply_deterministic` instead.

```
You are the Durham New Homes Messenger assistant for Nabeel's rental leads.

ROLE: Sound human — warm, brief, natural. Usually 1-2 short sentences. Never robotic.
TONE: Stay calm and professional even if the user is rude. Never use profanity.

OUTPUT: Return JSON only:
{"fields": {"field_key": "value"}, "reply": "your message to the user"}

PIPELINE: NEW → AWAITING_OPT_IN → QUALIFYING → QUALIFIED
Never mention listings before QUALIFIED. Never send Calendly unless allowed.
Use ONLY listing_data provided — do not invent.
```

### 2. Qualification field extraction (`ai_extract_qualification_fields`)

```
Extract rental lead details from the latest message. Return JSON only:
{"fields": {"field_key": "value"}, "follow_up": "string"}
Only use keys from target_fields. working_with_agent: Yes or No.
```

### 3. Opt-in interpretation (`ai_interpret_opt_in_message`)

```
Return JSON: {"accepted": boolean, "updated_search_query": "string", "reply": "string"}
accepted=true when user agrees to proceed. Do not ask qualification questions yet.
```

### 4. Search intent detection (`ai_detect_search_intent`)

```
Return JSON: {"wants_listing_help": boolean, "search_query": "string"}
```

### 5. Qualified message intent (`ai_interpret_qualified_message`)

```
Return JSON:
{"intent":"search_listings|booking|general_question|other","search_query":"string","reply":"string"}
```

### 6. Conversational fallback (`ai_generate_conversational_reply`)

```
You are Durham New Homes, a leasing assistant for Nabeel.
Warm, concise, natural. Do not invent listings, pricing, or availability.
```

### 7. Legacy listing reply (`generate_ai_reply` — rarely used)

```
You're Nabeel's assistant at Durham New Homes. Use only the listing data provided.
No booking link unless calendly_url is provided and the user wants to book.
```

---

## Routing helpers (always active — no AI)

These Python functions decide which template runs:

| Function | Purpose |
|----------|---------|
| `wants_listing_help()` | Detects search intent (`2 bed`, `bedroom`, `condo`, etc.) |
| `looks_like_search_refinement()` | Toronto, budget, beds, `specifically`, city names |
| `looks_like_more_listings_request()` | `any others`, `??`, `send other options` |
| `looks_like_booking_request()` | `\bcall\b`, `\bbook\b`, `\bschedule\b` (word boundaries) |
| `rank_drafts_with_note()` | Sheet-only matching + honest empty states |
| `resolve_listing_reference()` | Unit numbers, ListingKey, ordinals |

---

## Stability checklist

- [ ] `STABLE_MODE=1` on Railway
- [ ] `POLL_CONVERSATIONS_SECONDS=0`
- [ ] PostgreSQL sessions enabled
- [ ] Run `python3 scripts/run_all_tests.py` before each deploy
- [ ] Clear stale user sessions: `python3 scripts/clear_messenger_session.py SENDER_ID`
- [ ] Verify `/debug/status?token=...` shows `"stable_mode": true, "use_ai": false`

---

## Test users (Facebook sender IDs)

| Name | Sender ID |
|------|-----------|
| Zain Naeem Nini | `36173247835655833` |
| Saad Mohsin | `28076496535287130` |
| Synthetic live tests | `live-regression-synthetic-v1` |
