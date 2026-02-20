# ultrahype-train-finder

Monitors Twitch for hype trains at or above a configurable level (default: 50)
and sends a Gmail alert with the channel name, level, and a link to the stream.

---

## How it works

1. **Scrape** `twitch.tv/directory/collection/hype-train` with Playwright to get
   channels currently running hype trains.
2. **Check levels** by calling `/helix/hypetrain/events` for each channel via the
   Twitch Helix API.
3. **Send an email** via Gmail SMTP whenever a hype train at or above the minimum
   level is found.
4. **Repeat** every `POLL_INTERVAL_SECONDS` (default 60 s).
5. **Deduplicate** — one email per hype train event, tracked by channel + event ID.

If the Playwright scrape fails (e.g. Twitch changes their frontend), the tool
falls back to querying `/helix/streams` for the top 100 live streams and checking
hype trains on those instead.

---

## Important limitation: Twitch API authentication

`/helix/hypetrain/events` requires a **user-scoped** access token with the
`channel:read:hype_train` scope **granted by the broadcaster**. An app-access
token (client credentials) returns 401 for arbitrary channels.

In practice this means the API call will silently return nothing for most
channels — the hype train directory scrape is the primary discovery mechanism,
and the API is a best-effort confirmation layer. If you want full API-level
confirmation for specific channels, those broadcasters would need to individually
authorise your Twitch app.

---

## Prerequisites

- Python 3.10 or later
- A **Twitch Developer Application** (free)
- A **Gmail account** with 2-Step Verification enabled and an App Password

---

## Getting a Twitch Client ID and Secret

1. Go to <https://dev.twitch.tv/console/apps> and log in.
2. Click **Register Your Application**.
3. Fill in:
   - **Name**: any name (e.g. `my-hype-monitor`)
   - **OAuth Redirect URLs**: `http://localhost` (required but not used)
   - **Category**: `Application Integration`
4. Click **Create**.
5. Click **Manage** next to your new app, then click **New Secret**.
6. Copy the **Client ID** and the **Client Secret** into your `.env` file.

---

## Creating a Gmail App Password

App Passwords are separate from your Google account password and work even when
2FA is active. They are the correct way to authenticate SMTP from a script.

1. Go to <https://myaccount.google.com/security> and make sure
   **2-Step Verification** is turned on.
2. Go to <https://myaccount.google.com/apppasswords>.
3. Under **Select app**, choose **Mail**; under **Select device**, choose
   **Other (custom name)** and type `hype-monitor`.
4. Click **Generate**. Copy the 16-character password shown.
5. Paste it (without spaces) into `GMAIL_APP_PASSWORD` in your `.env` file.

> **Note:** If you don't see the App Passwords page, make sure 2-Step
> Verification is fully enabled and you are not using Google Workspace with
> App Passwords disabled by your admin.

---

## Installation

```bash
# 1. Clone or download this repo
cd ultrahype-train-finder

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Install the Playwright browser (Chromium)
playwright install chromium

# 4. Set up your config
cp .env.example .env
# Open .env in your editor and fill in all the values
```

---

## Configuration (`.env`)

| Variable | Required | Default | Description |
|---|---|---|---|
| `TWITCH_CLIENT_ID` | Yes | — | From your Twitch Developer app |
| `TWITCH_CLIENT_SECRET` | Yes | — | From your Twitch Developer app |
| `GMAIL_ADDRESS` | Yes | — | Your Gmail address |
| `GMAIL_APP_PASSWORD` | Yes | — | 16-character App Password (no spaces) |
| `ALERT_EMAIL` | Yes | — | Email address to receive alerts |
| `POLL_INTERVAL_SECONDS` | No | `60` | Seconds between checks |
| `MIN_HYPE_TRAIN_LEVEL` | No | `50` | Minimum level to trigger an alert |

---

## Running

```bash
python monitor.py
```

Press **Ctrl-C** to stop.

Example log output:
```
[2026-02-20T14:32:01Z] ultrahype-train-finder starting up.
[2026-02-20T14:32:01Z] Config: poll every 60s, alert at level >= 50.
[2026-02-20T14:32:01Z] Testing Twitch API credentials …
[2026-02-20T14:32:02Z] OAuth token refreshed.
[2026-02-20T14:32:02Z] Twitch API credentials OK.
[2026-02-20T14:32:02Z] Playwright: navigating to https://www.twitch.tv/directory/collection/hype-train …
[2026-02-20T14:32:06Z] Playwright: found 12 channel(s) in directory.
[2026-02-20T14:32:06Z] Checking hype train levels for 12 channel(s) …
[2026-02-20T14:32:08Z] ALERT: StreamerName (streamername) is at level 52!
[2026-02-20T14:32:09Z] Alert email sent: [HypeAlert] Level 52 Hype Train on StreamerName!
[2026-02-20T14:32:09Z] Cycle complete. Alerts sent: 1. Channels skipped (no API access): 9.
```

---

## Testing your setup

### 1. Verify Twitch credentials
```bash
python - <<'EOF'
from dotenv import load_dotenv; load_dotenv()
from monitor import get_valid_token
print("Token:", get_valid_token()[:10], "…")
EOF
```

### 2. Send a test email
```bash
python - <<'EOF'
from dotenv import load_dotenv; load_dotenv()
from monitor import send_alert_email
ok = send_alert_email("testchannel", "TestChannel", 52, "https://www.twitch.tv/testchannel")
print("Email sent:", ok)
EOF
```
Check your inbox (and spam folder).

### 3. Test the scraper
```bash
python - <<'EOF'
from dotenv import load_dotenv; load_dotenv()
from monitor import scrape_hype_train_channels
channels = scrape_hype_train_channels()
print("Channels found:", channels)
EOF
```

### 4. Lower the threshold to catch any hype train
Set `MIN_HYPE_TRAIN_LEVEL=1` in `.env` and run `python monitor.py`. You should
receive an alert for any active hype train at any level, confirming the full
pipeline works.

---

## Known limitations

- **Deduplication is in-memory only.** If the monitor restarts while a high-level
  hype train is active, it may send one duplicate alert for that train.
- **Most API calls return no data.** See the authentication limitation section
  above. The number of "skipped" channels in the logs is expected and normal.
- **Playwright scraping may break.** If Twitch ships a frontend update that
  changes the `data-a-target="preview-card-channel-link"` attribute, the scraper
  will fall back to the top-100-streams API automatically. Check the logs for
  a warning and update the selector in `scrape_hype_train_channels()` if needed.
