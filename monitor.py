"""
ultrahype-train-finder: monitors Twitch for hype trains at or above a
configurable level and sends Gmail alerts when one is found.

Authentication note
-------------------
/helix/hypetrain/events requires a *user-scoped* access token with the
channel:read:hype_train scope granted by each broadcaster. An app-access
token (client credentials flow) will return 401 for arbitrary channels.
The tool uses Playwright scraping as the primary discovery mechanism;
the Helix API call is a best-effort confirmation layer.
"""

import os
import sys
import time
import random
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

CONFIG: dict = {
    "client_id": os.getenv("TWITCH_CLIENT_ID", ""),
    "client_secret": os.getenv("TWITCH_CLIENT_SECRET", ""),
    "gmail_address": os.getenv("GMAIL_ADDRESS", ""),
    "gmail_app_pass": os.getenv("GMAIL_APP_PASSWORD", ""),
    "alert_email": os.getenv("ALERT_EMAIL", ""),
    "poll_interval": int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
    "min_level": int(os.getenv("MIN_HYPE_TRAIN_LEVEL", "50")),
}

HYPE_TRAIN_DIRECTORY_URL = "https://www.twitch.tv/directory/collection/hype-train"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_API_BASE = "https://api.twitch.tv/helix"

# Non-channel path segments to filter out when parsing hrefs
_EXCLUDED_PATHS = {
    "directory", "prime", "bits", "subscriptions", "jobs", "p",
    "downloads", "turbo", "store", "drops", "login", "signup",
    "friends", "inventory", "wallet", "settings", "messages", "inbox",
    "moderator", "broadcast", "popout",
}

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

# In-memory deduplication: keyed "{login}:{hype_train_instance_id}"
# Cleared only when the process restarts (one duplicate alert per active train
# at restart is an acceptable trade-off for simpler code).
alerted_trains: set[str] = set()

_token_state: dict = {"access_token": None, "expires_at": 0.0}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {message}", flush=True)

# ---------------------------------------------------------------------------
# OAuth — client credentials (app access token)
# ---------------------------------------------------------------------------

def fetch_app_token(client_id: str, client_secret: str) -> dict:
    """Fetch a fresh app access token from Twitch."""
    resp = requests.post(
        TWITCH_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Token fetch failed: HTTP {resp.status_code} — {resp.text[:200]}"
        )
    return resp.json()


def get_valid_token() -> str:
    """Return a cached app token, refreshing it if it expires within 60 s."""
    if time.time() >= _token_state["expires_at"]:
        data = fetch_app_token(CONFIG["client_id"], CONFIG["client_secret"])
        _token_state["access_token"] = data["access_token"]
        _token_state["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
        log("OAuth token refreshed.")
    return _token_state["access_token"]

# ---------------------------------------------------------------------------
# Twitch API helpers with rate-limit backoff
# ---------------------------------------------------------------------------

def twitch_api_get(
    endpoint: str,
    params: dict,
    max_retries: int = 5,
) -> dict | None:
    """
    GET {TWITCH_API_BASE}{endpoint} with automatic retry on 429 / 5xx.

    Returns the parsed JSON dict on success, or None on permanent failure
    (including expected 401 for channels that haven't authorised the app).
    """
    url = TWITCH_API_BASE + endpoint
    for attempt in range(max_retries):
        token = get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": CONFIG["client_id"],
        }
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
        except requests.RequestException as exc:
            wait = min(2 ** attempt + random.uniform(0, 1), 60.0)
            log(f"Network error on {endpoint}: {exc}. Retrying in {wait:.1f}s …")
            time.sleep(wait)
            continue

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 401:
            if attempt == 0:
                # Force a token refresh and retry once
                _token_state["expires_at"] = 0.0
                continue
            # Still 401 after refresh → channel hasn't granted scope (expected)
            return None

        if resp.status_code == 429:
            reset_header = resp.headers.get("Ratelimit-Reset")
            if reset_header:
                wait = max(float(reset_header) - time.time(), 1.0)
            else:
                wait = min(2 ** attempt + random.uniform(0, 1), 60.0)
            log(f"Rate limited on {endpoint}. Waiting {wait:.1f}s (attempt {attempt + 1}/{max_retries}) …")
            time.sleep(wait)
            continue

        if resp.status_code >= 500:
            wait = min(2 ** attempt + random.uniform(0, 1), 60.0)
            log(f"Server error {resp.status_code} on {endpoint}. Waiting {wait:.1f}s …")
            time.sleep(wait)
            continue

        log(f"Unexpected HTTP {resp.status_code} from {endpoint}: {resp.text[:100]}")
        return None

    log(f"All {max_retries} retries exhausted for {endpoint}.")
    return None


def get_user_ids_by_login(logins: list[str]) -> dict[str, str]:
    """
    Convert a list of channel login names to broadcaster IDs.
    Returns a dict mapping login (lowercased) → user_id.
    Batches requests in groups of 100 (Helix API max).
    """
    result: dict[str, str] = {}
    for i in range(0, len(logins), 100):
        batch = logins[i : i + 100]
        data = twitch_api_get("/users", params=[("login", login) for login in batch])
        if data:
            for user in data.get("data", []):
                result[user["login"].lower()] = user["id"]
    return result


def get_hype_train_event(broadcaster_id: str) -> dict | None:
    """
    Fetch the current hype train status for a broadcaster.
    Returns the status dict (fields: id, broadcaster_id, level, total, goal,
    started_at, expires_at) or None if no active train / no API access.

    Uses /helix/hypetrain/status (replaces the removed /helix/hypetrain/events).
    Note: Returns None for channels that haven't granted channel:read:hype_train.
    """
    data = twitch_api_get(
        "/hypetrain/status",
        params={"broadcaster_id": broadcaster_id, "first": 1},
    )
    if data and data.get("data"):
        return data["data"][0]
    return None

# ---------------------------------------------------------------------------
# Playwright scraping
# ---------------------------------------------------------------------------

def scrape_hype_train_channels() -> list[str] | None:
    """
    Scrape the Twitch hype-train directory page and return a list of channel
    login names currently running hype trains.

    Returns:
        list[str]  — channel logins (may be empty if directory is empty)
        None       — scrape failed; caller should use the fallback
    """
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page = ctx.new_page()

            log(f"Playwright: navigating to {HYPE_TRAIN_DIRECTORY_URL} …")
            page.goto(
                HYPE_TRAIN_DIRECTORY_URL,
                wait_until="domcontentloaded",
                timeout=30_000,
            )

            # Wait for channel cards to appear
            try:
                page.wait_for_selector(
                    '[data-a-target="preview-card-channel-link"]',
                    timeout=15_000,
                )
            except PlaywrightTimeoutError:
                log(
                    "Playwright: selector [data-a-target='preview-card-channel-link'] "
                    "not found. The Twitch DOM may have changed. Triggering fallback."
                )
                browser.close()
                return None

            # Scroll to lazy-load more cards (up to 3 passes)
            prev_height = 0
            for _ in range(3):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2_000)
                new_height = page.evaluate("document.body.scrollHeight")
                if new_height == prev_height:
                    break
                prev_height = new_height

            # Extract channel login names from channel-link hrefs
            elements = page.query_selector_all(
                '[data-a-target="preview-card-channel-link"]'
            )
            logins: list[str] = []
            for el in elements:
                href = el.get_attribute("href") or ""
                # href is like "/channelname" — strip the leading slash
                parts = [p for p in href.split("/") if p]
                if len(parts) == 1 and parts[0].lower() not in _EXCLUDED_PATHS:
                    logins.append(parts[0].lower())

            browser.close()

            unique_logins = list(dict.fromkeys(logins))  # dedupe, preserve order
            if not unique_logins:
                log(
                    "Playwright: scrape succeeded but found 0 channel links. "
                    "The directory may be empty or the selector has changed."
                )
            else:
                log(f"Playwright: found {len(unique_logins)} channel(s) in directory.")
            return unique_logins

    except Exception as exc:
        log(f"Playwright scrape failed: {exc}. Triggering fallback.")
        return None


def scrape_hype_train_channels_fallback() -> list[str]:
    """
    Fallback when Playwright fails: fetch the top 100 live streams and return
    their channel logins as a broad population to check for hype trains.

    Note: This returns all top streams, not just hype-train channels. Most API
    calls to /helix/hypetrain/events will return 401 or empty data for these.
    """
    log("Using fallback: querying /helix/streams for top 100 live streams.")
    data = twitch_api_get("/streams", params={"first": 100})
    if not data:
        log("Fallback also failed — no channels to check this cycle.")
        return []
    logins = [s["user_login"].lower() for s in data.get("data", [])]
    log(f"Fallback: got {len(logins)} live stream(s) to check.")
    return logins

# ---------------------------------------------------------------------------
# Email alerts
# ---------------------------------------------------------------------------

def send_alert_email(
    channel_login: str,
    display_name: str,
    level: int,
    stream_url: str,
) -> bool:
    """
    Send a Gmail alert for a high-level hype train.
    Returns True on success, False on failure (never raises).
    """
    subject = f"[HypeAlert] Level {level} Hype Train on {display_name}!"

    plain_body = (
        f"A level {level} hype train is active!\n\n"
        f"Channel: {display_name}\n"
        f"Stream:  {stream_url}\n\n"
        f"This alert was sent by ultrahype-train-finder."
    )

    html_body = f"""
<html>
  <body style="font-family: Arial, sans-serif; color: #1a1a1a;">
    <h2 style="color: #9147ff;">&#128641; Level {level} Hype Train Active!</h2>
    <table style="border-collapse: collapse;">
      <tr>
        <td style="padding: 4px 12px 4px 0;"><strong>Channel</strong></td>
        <td>{display_name}</td>
      </tr>
      <tr>
        <td style="padding: 4px 12px 4px 0;"><strong>Watch now</strong></td>
        <td><a href="{stream_url}">{stream_url}</a></td>
      </tr>
      <tr>
        <td style="padding: 4px 12px 4px 0;"><strong>Level</strong></td>
        <td>{level}</td>
      </tr>
    </table>
    <p style="color: #666; font-size: 12px; margin-top: 24px;">
      Sent by ultrahype-train-finder
    </p>
  </body>
</html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = CONFIG["gmail_address"]
    msg["To"] = CONFIG["alert_email"]
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(CONFIG["gmail_address"], CONFIG["gmail_app_pass"])
            smtp.sendmail(
                CONFIG["gmail_address"],
                CONFIG["alert_email"],
                msg.as_string(),
            )
        log(f"Alert email sent: {subject}")
        return True
    except smtplib.SMTPException as exc:
        log(f"Failed to send alert email: {exc}")
        return False
    except OSError as exc:
        log(f"Network error sending alert email: {exc}")
        return False

# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _dedup_key(channel_login: str, train_id: str) -> str:
    return f"{channel_login}:{train_id}"


def already_alerted(channel_login: str, train_id: str) -> bool:
    return _dedup_key(channel_login, train_id) in alerted_trains


def mark_alerted(channel_login: str, train_id: str) -> None:
    alerted_trains.add(_dedup_key(channel_login, train_id))

# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------

def check_once() -> None:
    """Run a single monitoring cycle."""
    channels = scrape_hype_train_channels()
    if channels is None:
        channels = scrape_hype_train_channels_fallback()

    if not channels:
        log("No channels to check this cycle.")
        return

    log(f"Checking hype train levels for {len(channels)} channel(s) …")

    login_to_id = get_user_ids_by_login(channels)
    if not login_to_id:
        log("Could not resolve any channel IDs — skipping cycle.")
        return

    alerts_sent = 0
    skipped_auth = 0

    for login in channels:
        user_id = login_to_id.get(login)
        if not user_id:
            continue  # channel not found / suspended

        event = get_hype_train_event(user_id)

        if event is None:
            skipped_auth += 1
            continue

        level = event.get("level", 0)
        train_id = event.get("id", "")
        display_name = event.get("broadcaster_name", login)

        if level < CONFIG["min_level"]:
            continue

        if already_alerted(login, train_id):
            log(f"Already alerted for {login} train {train_id} (level {level}).")
            continue

        log(f"ALERT: {display_name} ({login}) is at level {level}!")
        stream_url = f"https://www.twitch.tv/{login}"
        sent = send_alert_email(display_name, display_name, level, stream_url)
        if sent:
            mark_alerted(login, train_id)
            alerts_sent += 1

        # Pace API calls to avoid burning through the rate limit
        time.sleep(0.5)

    log(
        f"Cycle complete. Alerts sent: {alerts_sent}. "
        f"Channels skipped (no API access): {skipped_auth}."
    )


def _validate_config() -> None:
    """Exit with an informative error if required env vars are missing."""
    required = [
        ("TWITCH_CLIENT_ID", CONFIG["client_id"]),
        ("TWITCH_CLIENT_SECRET", CONFIG["client_secret"]),
        ("GMAIL_ADDRESS", CONFIG["gmail_address"]),
        ("GMAIL_APP_PASSWORD", CONFIG["gmail_app_pass"]),
        ("ALERT_EMAIL", CONFIG["alert_email"]),
    ]
    missing = [name for name, val in required if not val]
    if missing:
        print(
            f"ERROR: Missing required environment variables: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in your credentials.",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> None:
    _validate_config()

    log("ultrahype-train-finder starting up.")
    log(
        f"Config: poll every {CONFIG['poll_interval']}s, "
        f"alert at level >= {CONFIG['min_level']}."
    )

    # Connectivity check — fails fast if credentials are wrong
    log("Testing Twitch API credentials …")
    get_valid_token()
    log("Twitch API credentials OK.")

    while True:
        try:
            check_once()
        except KeyboardInterrupt:
            log("Keyboard interrupt — shutting down.")
            break
        except Exception as exc:
            log(f"Unexpected error in poll cycle: {exc}")

        try:
            time.sleep(CONFIG["poll_interval"])
        except KeyboardInterrupt:
            log("Keyboard interrupt — shutting down.")
            break


if __name__ == "__main__":
    main()
