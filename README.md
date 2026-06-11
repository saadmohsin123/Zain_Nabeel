# Zain_Nabeel

Messenger webhook automation for Meta pages and marketplace listing summaries.

## Run locally

```bash
python3 -m pip install -r requirements.txt
export META_PAGE_ACCESS_TOKEN=...
export META_VERIFY_TOKEN=coagent_messenger_verify_2026
python3 messenger_automation.py
```

## Endpoints

- `GET /healthz`
- `GET /webhook` for Meta verification
- `POST /webhook` for Messenger events

## Railway

This repo is ready to deploy as a Python web service with the included `Procfile`.
