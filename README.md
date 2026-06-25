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

```env
POLL_CONVERSATIONS_SECONDS=15
POLL_STATE_FILE=messenger_poll_state.json
```

Use the polling fallback only until the Meta app is Live/approved and normal webhook delivery is confirmed.

## Local run

```bash
python3 -m pip install -r requirements.txt
python3 messenger_automation.py
```

## Endpoints

- `GET /healthz`
- `GET /webhook`
- `POST /webhook`
