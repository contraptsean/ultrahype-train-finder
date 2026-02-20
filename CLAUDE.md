# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the tool

```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Configure credentials
cp .env.example .env
# edit .env with your values

# Run
python monitor.py
```

## Ad-hoc testing (no test framework)

```bash
# Test Twitch OAuth
python -c "from dotenv import load_dotenv; load_dotenv(); from monitor import get_valid_token; print(get_valid_token()[:10], '…')"

# Test email delivery
python -c "from dotenv import load_dotenv; load_dotenv(); from monitor import send_alert_email; print(send_alert_email('test', 'Test', 52, 'https://twitch.tv/test'))"

# Test Playwright scraper
python -c "from dotenv import load_dotenv; load_dotenv(); from monitor import scrape_hype_train_channels; print(scrape_hype_train_channels())"

# Force any-level alert (smoke test)
# Set MIN_HYPE_TRAIN_LEVEL=1 in .env, then run python monitor.py
```

## Architecture

Everything lives in a single file: [monitor.py](monitor.py). The call flow for each poll cycle is:

```
main()
  └── check_once()
        ├── scrape_hype_train_channels()          # Playwright → twitch.tv/directory/collection/hype-train
        │     └── [on failure] scrape_hype_train_channels_fallback()  # GET /helix/streams?first=100
        ├── get_user_ids_by_login()               # GET /helix/users (batch, 100/req)
        └── per channel:
              └── get_hype_train_event()           # GET /helix/hypetrain/events
                    └── [level ≥ min] send_alert_email() + mark_alerted()
```

All Helix calls go through `twitch_api_get()`, which handles 401 token refresh, 429 rate-limit backoff (reads `Ratelimit-Reset` header, falls back to `min(2^n + jitter, 60)`), and 5xx exponential backoff.

OAuth is client credentials (app token). The token is cached in `_token_state` and refreshed 60 s before expiry by `get_valid_token()`.

## Key constraints

**`/helix/hypetrain/events` requires a per-broadcaster user token** (`channel:read:hype_train` scope). An app token returns 401 for arbitrary channels. The scraper is the primary discovery mechanism; the API is best-effort confirmation. High "skipped (no API access)" counts in logs are normal and expected.

**Deduplication is in-memory** (`alerted_trains: set[str]`, keyed `"{login}:{train_id}"`). A process restart will re-alert for any train still active at that moment.

**Playwright selector to watch**: `[data-a-target="preview-card-channel-link"]`. If Twitch changes their frontend and this selector stops matching, `scrape_hype_train_channels()` returns `None` and the fallback fires. Update the selector in that function when this happens.
