"""
ultrahype-train-finder: monitors Twitch for hype trains at or above a
configurable level and sends Gmail alerts when one is found.

How level detection works
-------------------------
1. Playwright scrapes twitch.tv/directory/collection/hype-train for channels
   currently running hype trains.
2. For each channel found, Playwright opens the stream page and reads the
   hype train level directly from the page widget — no Twitch API auth needed.
3. The Twitch API (client credentials) is only used for the fallback path
   when the directory scrape fails (/helix/streams to get a broad list of
   live channels to check).
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
from playwright.sync_api import sync_playwright, BrowserContext, TimeoutError as PlaywrightTimeoutError

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

_CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

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

# In-memory deduplication: keyed "{login}:{hour_bucket}"
# Cleared only when the process restarts.
alerted_trains: set[str] = set()

_token_state: dict = {"access_token": None, "expires_at": 0.0}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {message}", flush=True)

# ---------------------------------------------------------------------------
# OAuth — client credentials (app access token, used for fallback only)
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
# Twitch API helpers (used only for the fallback channel list)
# ---------------------------------------------------------------------------

def twitch_api_get(
    endpoint: str,
    params: dict,
    max_retries: int = 5,
) -> dict | None:
    """
    GET {TWITCH_API_BASE}{endpoint} with automatic retry on 429 / 5xx.
    Returns the parsed JSON dict on success, or None on permanent failure.
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
                _token_state["expires_at"] = 0.0
                continue
            return None

        if resp.status_code == 429:
            reset_header = resp.headers.get("Ratelimit-Reset")
            if reset_header:
                wait = max(float(reset_header) - time.time(), 1.0)
            else:
                wait = min(2 ** attempt + random.uniform(0, 1), 60.0)
            log(f"Rate limited on {endpoint}. Waiting {wait:.1f}s …")
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

# ---------------------------------------------------------------------------
# Playwright scraping
# ---------------------------------------------------------------------------

def scrape_hype_train_channels() -> list[str] | None:
    """
    Scrape the Twitch hype-train directory page for channel login names.

    Returns:
        list[str]  — channel logins (may be empty if directory is empty)
        None       — scrape failed; caller should use the fallback
    """
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=_CHROME_UA)
            page = ctx.new_page()

            log(f"Playwright: navigating to {HYPE_TRAIN_DIRECTORY_URL} …")
            page.goto(
                HYPE_TRAIN_DIRECTORY_URL,
                wait_until="domcontentloaded",
                timeout=30_000,
            )

            try:
                page.wait_for_selector(
                    '[data-a-target="preview-card-channel-link"]',
                    timeout=15_000,
                )
            except PlaywrightTimeoutError:
                log(
                    "Playwright: [data-a-target='preview-card-channel-link'] not found. "
                    "The Twitch DOM may have changed. Triggering fallback."
                )
                browser.close()
                return None

            # Twitch uses a virtualised list renderer and a custom SimpleBar
            # scroll container, so window.scrollTo() has no effect.
            # Instead, scroll the last visible card into view — Playwright
            # resolves the real scroll container automatically.
            # Harvest cards before each scroll so virtualised (removed) cards
            # are still captured.
            seen: set[str] = set()
            logins: list[str] = []

            def _harvest() -> None:
                for el in page.query_selector_all(
                    '[data-a-target="preview-card-channel-link"]'
                ):
                    href = el.get_attribute("href") or ""
                    parts = [p for p in href.split("/") if p]
                    if len(parts) == 1 and parts[0].lower() not in _EXCLUDED_PATHS:
                        login = parts[0].lower()
                        if login not in seen:
                            seen.add(login)
                            logins.append(login)

            # Simulate mouse-wheel scrolling over the centre of the viewport.
            # This reliably fires Twitch's IntersectionObserver-based lazy
            # loader regardless of which DOM element owns the scroll container.
            vw = page.viewport_size or {"width": 1280, "height": 800}
            cx, cy = vw["width"] // 2, vw["height"] // 2
            page.mouse.move(cx, cy)

            prev_count = 0
            consecutive_no_change = 0
            for _ in range(20):
                _harvest()
                page.mouse.wheel(0, 3000)   # scroll down ~3 screen-heights
                page.wait_for_timeout(3_000)

                if len(logins) == prev_count:
                    consecutive_no_change += 1
                    if consecutive_no_change >= 2:
                        break
                else:
                    consecutive_no_change = 0
                prev_count = len(logins)

            _harvest()  # capture the final viewport after the last scroll

            browser.close()

            if not logins:
                log(
                    "Playwright: directory scrape found 0 channels. "
                    "Directory may be empty or selector has changed."
                )
            else:
                log(f"Playwright: found {len(logins)} channel(s) in directory.")
            return logins

    except Exception as exc:
        log(f"Playwright directory scrape failed: {exc}. Triggering fallback.")
        return None


def scrape_hype_train_channels_fallback() -> list[str]:
    """
    Fallback when Playwright fails: fetch the top 100 live streams via
    /helix/streams and return their channel logins as a population to check.
    """
    log("Using fallback: querying /helix/streams for top 100 live streams.")
    data = twitch_api_get("/streams", params={"first": 100})
    if not data:
        log("Fallback also failed — no channels to check this cycle.")
        return []
    logins = [s["user_login"].lower() for s in data.get("data", [])]
    log(f"Fallback: got {len(logins)} live stream(s) to check.")
    return logins


def _visit_channel_for_level(ctx: BrowserContext, login: str) -> int | None:
    """
    Open the channel's Twitch stream page in a new tab and extract the current
    hype train level from the page widget.

    Returns the level as an int, or None if no active hype train is visible.
    """
    page = ctx.new_page()
    try:
        url = f"https://www.twitch.tv/{login}"
        page.goto(url, wait_until="domcontentloaded", timeout=20_000)

        # Wait for React to render the stream page and hype train widget.
        page.wait_for_timeout(5_000)

        level_val = page.evaluate(r"""() => {
            const levelRe = /(?:Lvl|Level)\s*[:\-]?\s*(\d+)/i;

            // Strategy 1: look inside known hype-train widget elements.
            const htSelectors = [
                '[data-a-target*="hype"]',
                '[data-test-selector*="hype"]',
                '[class*="HypeTrain"]',
                '[class*="hype-train"]',
            ];
            for (const sel of htSelectors) {
                for (const el of document.querySelectorAll(sel)) {
                    const text = el.innerText || el.textContent || '';
                    const m = text.match(levelRe);
                    if (m) {
                        const lvl = parseInt(m[1], 10);
                        if (lvl > 0 && lvl < 1000) return lvl;
                    }
                }
            }

            // Strategy 2: find "Hype Train" text anywhere on the page, then
            // look for "Lvl X" or "Level X" within a 400-character window.
            const body = document.body.innerText || '';
            const idx = body.toLowerCase().indexOf('hype train');
            if (idx >= 0) {
                const window = body.slice(Math.max(0, idx - 50), idx + 400);
                const m = window.match(levelRe);
                if (m) {
                    const lvl = parseInt(m[1], 10);
                    if (lvl > 0 && lvl < 1000) return lvl;
                }
            }

            return null;
        }""")

        level = int(level_val) if level_val is not None else None
        if level is not None:
            log(f"  {login}: hype train level {level}")
        return level

    except PlaywrightTimeoutError:
        log(f"  {login}: page load timed out — skipping.")
        return None
    except Exception as exc:
        log(f"  {login}: page check failed — {exc}")
        return None
    finally:
        page.close()

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

    log(f"Visiting {len(channels)} stream page(s) to read hype train levels …")

    # One hour bucket for dedup — at most one alert per channel per hour.
    hour_bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    alerts_sent = 0
    no_train_found = 0

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=_CHROME_UA)

            for login in channels:
                level = _visit_channel_for_level(ctx, login)

                if level is None:
                    no_train_found += 1
                    continue

                if level < CONFIG["min_level"]:
                    continue

                train_id = hour_bucket
                if already_alerted(login, train_id):
                    log(f"Already alerted for {login} (level {level}) this hour.")
                    continue

                log(f"ALERT: {login} is at level {level}!")
                stream_url = f"https://www.twitch.tv/{login}"
                sent = send_alert_email(login, login, level, stream_url)
                if sent:
                    mark_alerted(login, train_id)
                    alerts_sent += 1

            browser.close()

    except Exception as exc:
        log(f"Playwright level-check session failed: {exc}")

    log(
        f"Cycle complete. Alerts sent: {alerts_sent}. "
        f"Channels with no visible hype train: {no_train_found}."
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
