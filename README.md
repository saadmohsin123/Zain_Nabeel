# Zain_Nabeel

Messenger automation for the Durham New Homes Meta Page.

## What it does

- Verifies Meta webhook challenges at `GET /webhook`.
- Receives Messenger webhook events at `POST /webhook`.
- Matches incoming messages against the shared Google Sheet `Overview` tab when available, with `marketplace_drafts.json` as a fallback.
- Replies with the matching listing summary and the seller packet link.
- Optionally polls Page conversations as a fallback when Meta does not deliver production webhooks while the app is still in development/review.

## Railway environment

Required:

```env
META_PAGE_ACCESS_TOKEN=
META_VERIFY_TOKEN=coagent_messenger_verify_2026
META_APP_SECRET=
META_PAGE_ID=803463962847979
LISTING_DOC_URL=https://docs.google.com/spreadsheets/d/13u__qGNeV46Q9rREPbbDnzhZdNeNvxID4FGaH7Y47xo/edit
MARKETPLACE_DRAFTS_JSON=marketplace_drafts.json
DRAFTS_CACHE_SECONDS=30
```

Optional but recommended:

```env
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini
```

`OPENAI_API_KEY` powers intelligent qualification parsing (understanding messy natural answers), post-qualification search intent, and natural listing replies. Without it, the bot falls back to basic rule-based parsing.

**Recommended for production stability:**

```env
STABLE_MODE=1
POLL_CONVERSATIONS_SECONDS=0
OPENAI_API_KEY=sk-...
```

With `OPENAI_API_KEY` set, the bot uses **OpenAI to compose natural, human-like replies** via `AI_MASTER_SYSTEM_PROMPT` (see `PROMPTS.md`). Listing facts still come from the Google Sheet — the model phrases them conversationally but must not invent units or prices. `STABLE_MODE=1` disables the poll fallback only (webhook-only delivery).

```env
POLL_CONVERSATIONS_SECONDS=0
POLL_STATE_FILE=messenger_poll_state.json
```

Use the polling fallback only until the Meta app is Live/approved and normal webhook delivery is confirmed. Disable poll entirely when `STABLE_MODE=1`.

See **`PROMPTS.md`** for all bot prompts, templates, and the conversation pipeline.

### Clear a user's session (fresh retest)

```bash
python3 scripts/clear_messenger_session.py 36173247835655833
```

### PostgreSQL session storage (recommended on Railway)

Add a **PostgreSQL** plugin to the Railway project. Railway injects `DATABASE_URL` automatically. On startup the bot:

- Creates `messenger_sessions` and `messenger_seen_messages` tables if missing
- Stores each Messenger sender's qualification state as a JSON row (no whole-file races across replicas)
- Migrates any existing `lead_intake_state.json` sessions once

Local dev works without Postgres — sessions fall back to `LEAD_STATE_FILE` (default `lead_intake_state.json`).

Check production storage at `GET /debug/status?token=YOUR_VERIFY_TOKEN` — look for `"session_store": {"backend": "postgresql", ...}`.

Manual schema (optional): `sql/001_messenger_sessions.sql`

## Local run

```bash
python3 -m pip install -r requirements.txt
python3 messenger_automation.py
```

## Endpoints

- `GET /healthz`
- `GET /webhook`
- `POST /webhook`
