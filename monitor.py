#!/usr/bin/env python3
"""
Facebook Page Post Monitor -> Telegram Notifier

Checks a list of public Facebook pages (via the m.facebook.com mobile
interface) for new posts, and sends a Telegram message for each new post
found. Designed to run on a schedule (e.g. every 5-10 min via GitHub Actions).

NOTE: mbasic.facebook.com (the interface this script originally used) was
retired by Facebook in late 2024. This version targets m.facebook.com
instead, and uses curl_cffi (rather than plain `requests`) to impersonate a
real browser's TLS/JA3 fingerprint -- plain `requests` gets bounced to a
login wall almost immediately because Facebook fingerprints the TLS
handshake, not just the User-Agent header. This is a workaround for
Facebook's current anti-bot behavior as best understood right now, not a
guarantee: Facebook can and does change this, and shared CI IPs (like
GitHub Actions runners) are more likely to get flagged than a residential
IP regardless of fingerprinting. If checks start failing again across ALL
pages at once, that's the first thing to suspect -- see the "all pages
failed" alert logic below.

State (last-seen post per page, and failure counters) is persisted to state.json,
which the GitHub Action commits back to the repo between runs.
"""

import os
import re
import json
import sys
import time
import random
import hashlib
import requests  # used only for the Telegram API call
from curl_cffi import requests as cf_requests  # used for Facebook fetches (TLS impersonation)
from curl_cffi.requests.exceptions import RequestException as CFRequestException
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PAGES = [
    {"name": "Antipolo City 1st District Office", "id": "AntipoloCity1stCongressionalDistrictOffice"},
    {"name": "Profile 61553138835377", "id": "profile.php?id=61553138835377"},
    {"name": "Juny Nares Official", "id": "junynaresofficial"},
    {"name": "Love Tayo ni Onza", "id": "LoveTayoniOnza1"},
]

# Reuse one impersonated session across all page checks in a run, the way a
# real browser would keep cookies/connection state rather than starting cold
# on every request.
SESSION = cf_requests.Session()

STATE_FILE = "state.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# How many consecutive failed checks before we send a "this page looks dead" alert.
# At a 5-10 min schedule, ~36 failures ≈ 3-6 hours of continuous failure.
DEAD_THRESHOLD = 36

# How many posts back to look at / notify on first run (avoid spamming old history).
MAX_POSTS_PER_PAGE = 5

# User-Agent should roughly match the curl_cffi `impersonate` profile used in
# fetch_page_html() below (CF_IMPERSONATE) so the TLS fingerprint and the
# declared browser agree with each other.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# curl_cffi impersonation target -- keep this aligned with the User-Agent above.
CF_IMPERSONATE = "safari17_0"

BASE_URL = "https://m.facebook.com"

# How many requests an IP can plausibly make before Facebook gets suspicious.
# Used only to add a small randomized human-ish delay between page checks
# (see main()); this is not a hard rate limiter.
REQUEST_DELAY_RANGE = (3, 8)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_page_state(state, page_id):
    return state.setdefault(page_id, {
        "last_post_ids": [],   # list of recently seen post hashes (small rolling window)
        "consecutive_failures": 0,
        "last_success_ts": None,
        "dead_alert_sent": False,
        "first_run_done": False,
    })


def get_global_state(state):
    """Tracks consecutive runs where EVERY page failed, which usually points
    at the IP/fingerprint being blocked rather than any single page's HTML
    changing -- worth a different, faster alert than the per-page one."""
    return state.setdefault("_global", {
        "consecutive_all_failed_runs": 0,
        "all_failed_alert_sent": False,
    })


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("WARNING: Telegram credentials not set; skipping send. Message was:")
        print(text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"Telegram send failed: {resp.status_code} {resp.text}")
            return False
        return True
    except requests.RequestException as e:
        print(f"Telegram send exception: {e}")
        return False


# ---------------------------------------------------------------------------
# Facebook scraping (m.facebook.com)
# ---------------------------------------------------------------------------

def fetch_page_html(page_id):
    """Fetch the m.facebook.com page for a given page id/username.

    Uses curl_cffi (not plain `requests`) so the TLS/JA3 handshake matches a
    real browser -- a plain `requests.get()` gets redirected to a login wall
    almost immediately regardless of headers, because Facebook fingerprints
    the connection itself, not just the User-Agent string.
    """
    url = f"{BASE_URL}/{page_id}"
    resp = SESSION.get(
        url,
        headers=HEADERS,
        impersonate=CF_IMPERSONATE,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.text


def looks_like_blocked_or_login(html):
    """Heuristic check: did Facebook serve us a login wall / checkpoint instead of content?"""
    lowered = html.lower()
    signals = [
        "log in to facebook",
        "you must log in",
        "checkpoint",
        "id=\"login_form\"",
        "name=\"login\"",
    ]
    # If it's clearly a login page AND has none of the post markers, treat as blocked.
    has_login_signal = any(s in lowered for s in signals)
    has_post_signal = ("/story.php" in lowered) or ("data-ft" in lowered) or ("role=\"article\"" in lowered)
    return has_login_signal and not has_post_signal


def extract_posts(html):
    """
    Parse posts out of an m.facebook.com page HTML.

    Facebook's mobile markup changes periodically and varies by page type,
    so this tries two strategies in order and returns whichever finds posts
    first. Returns a list of dicts: {id, text, link}, most recent first.
    """
    soup = BeautifulSoup(html, "lxml")
    posts = []

    # Strategy 1: look for article-like containers with a permalink to a story
    # m.facebook.com typically has links like /story.php?story_fbid=...&id=...
    # or /<page>/posts/<id>
    story_links = soup.find_all("a", href=re.compile(r"(story\.php\?story_fbid=|/posts/|/photo\.php\?fbid=|/videos/)"))

    seen_ids = set()
    for link in story_links:
        href = link.get("href", "")
        post_id = make_post_id(href)
        if not post_id or post_id in seen_ids:
            continue

        # Walk up to find a reasonably-sized container with text for this post,
        # but stop climbing as soon as we hit another post's story link so we
        # don't bleed text from neighboring posts into this one.
        container = link
        text = ""
        for _ in range(6):
            if container.parent is None:
                break
            container = container.parent
            other_links = container.find_all(
                "a", href=re.compile(r"(story\.php\?story_fbid=|/posts/|/photo\.php\?fbid=|/videos/)")
            )
            if len(other_links) > 1:
                # This container now spans more than one post; use the text
                # gathered so far (from the previous, narrower container) instead.
                break
            candidate_text = container.get_text(separator=" ", strip=True)
            text = candidate_text
            if len(candidate_text) > 80:
                break

        seen_ids.add(post_id)
        posts.append({
            "id": post_id,
            "text": text[:600],
            "link": href if href.startswith("http") else BASE_URL + href,
        })

        if len(posts) >= MAX_POSTS_PER_PAGE:
            break

    if posts:
        return posts

    # Strategy 2 (fallback): m.facebook.com often wraps each timeline post in
    # a container carrying a data-ft="{...}" attribute (used internally by FB
    # for click tracking) even when it has no direct story-permalink anchor
    # matching Strategy 1's patterns. Use that as a post boundary instead.
    for container in soup.find_all(attrs={"data-ft": True}):
        raw_ft = container.get("data-ft", "")
        text = container.get_text(separator=" ", strip=True)
        if not text:
            continue

        link_tag = container.find("a", href=re.compile(r"(story\.php|/posts/|/photo\.php|/videos/|permalink)"))
        href = link_tag.get("href", "") if link_tag else ""

        post_id = make_post_id(href) if href else None
        if not post_id:
            # No usable href; derive an id from the data-ft payload or, last
            # resort, the post text itself so we can still detect "new".
            story_key = re.search(r'"mf_story_key"\s*:\s*"?([\w-]+)"?', raw_ft)
            post_id = story_key.group(1) if story_key else hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]

        if post_id in seen_ids:
            continue
        seen_ids.add(post_id)

        posts.append({
            "id": post_id,
            "text": text[:600],
            "link": (href if href.startswith("http") else BASE_URL + href) if href else BASE_URL,
        })

        if len(posts) >= MAX_POSTS_PER_PAGE:
            break

    return posts


def make_post_id(href):
    """Derive a stable-ish id from a post href (handles relative/absolute, query order)."""
    if not href:
        return None
    fbid_match = re.search(r"(story_fbid|fbid)=(\d+)", href)
    if fbid_match:
        return fbid_match.group(2)
    posts_match = re.search(r"/posts/([\w-]+)", href)
    if posts_match:
        return posts_match.group(1)
    videos_match = re.search(r"/videos/(\d+)", href)
    if videos_match:
        return videos_match.group(1)
    # Fallback: hash the href itself
    return hashlib.sha1(href.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Main per-page check
# ---------------------------------------------------------------------------

def check_page(page, state):
    """Returns True if this page check succeeded (content fetched and
    parsed), False otherwise. Used by main() to track all-pages-failed runs."""
    page_id = page["id"]
    page_name = page["name"]
    pstate = get_page_state(state, page_id)

    try:
        html = fetch_page_html(page_id)
    except CFRequestException as e:
        handle_failure(page_name, pstate, f"network error: {e}")
        return False

    if looks_like_blocked_or_login(html):
        handle_failure(page_name, pstate, "received a login/checkpoint page instead of content")
        return False

    posts = extract_posts(html)

    if not posts:
        handle_failure(page_name, pstate, "no posts found (layout may have changed)")
        return False

    # Success: reset failure tracking
    pstate["consecutive_failures"] = 0
    pstate["last_success_ts"] = int(time.time())
    if pstate["dead_alert_sent"]:
        pstate["dead_alert_sent"] = False
        send_telegram(f"✅ <b>{page_name}</b> monitor is back online and reading posts again.")

    known_ids = set(pstate["last_post_ids"])
    new_posts = [p for p in posts if p["id"] not in known_ids]

    if not pstate["first_run_done"]:
        # Don't spam old history on the very first run; just record a baseline.
        pstate["first_run_done"] = True
        pstate["last_post_ids"] = [p["id"] for p in posts]
        print(f"[{page_name}] First run: recorded {len(posts)} existing posts as baseline.")
        return True

    if new_posts:
        # new_posts came back most-recent-first; notify oldest-first so chat order makes sense
        for p in reversed(new_posts):
            snippet = p["text"] if p["text"] else "(no preview text available)"
            if len(snippet) > 300:
                snippet = snippet[:300] + "…"
            message = (
                f"🔔 <b>New post: {page_name}</b>\n\n"
                f"{snippet}\n\n"
                f"<a href=\"{p['link']}\">View post</a>"
            )
            send_telegram(message)
            print(f"[{page_name}] Notified new post {p['id']}")

    # Update rolling window of known ids (keep most recent MAX_POSTS_PER_PAGE*2)
    combined = [p["id"] for p in posts] + pstate["last_post_ids"]
    deduped = list(dict.fromkeys(combined))
    pstate["last_post_ids"] = deduped[: MAX_POSTS_PER_PAGE * 2]
    return True


def handle_failure(page_name, pstate, reason):
    pstate["consecutive_failures"] += 1
    print(f"[{page_name}] Failure ({pstate['consecutive_failures']}): {reason}")

    if pstate["consecutive_failures"] == 1:
        # Light first-failure note isn't sent to avoid noise from transient blips;
        # we only alert loudly once it crosses the dead threshold below.
        pass

    if pstate["consecutive_failures"] >= DEAD_THRESHOLD and not pstate["dead_alert_sent"]:
        pstate["dead_alert_sent"] = True
        send_telegram(
            f"⚠️ <b>{page_name}</b> monitor has failed {pstate['consecutive_failures']} checks in a row.\n"
            f"Reason (most recent): {reason}\n\n"
            f"This usually means Facebook changed its page layout, is blocking automated requests, "
            f"or the page URL/ID is no longer valid. The script needs a look."
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    state = load_state()
    any_exception = False
    results = []  # True/False per page, success or not

    for page in PAGES:
        try:
            ok = check_page(page, state)
            results.append(bool(ok))
        except Exception as e:
            any_exception = True
            results.append(False)
            print(f"[{page['name']}] Unexpected exception: {e}", file=sys.stderr)
            pstate = get_page_state(state, page["id"])
            handle_failure(page["name"], pstate, f"unexpected exception: {e}")
        # Randomized delay instead of a fixed one -- consistent fixed-interval
        # timing between requests is itself a bot signal.
        time.sleep(random.uniform(*REQUEST_DELAY_RANGE))

    handle_global_failure_tracking(state, results)
    save_state(state)

    if any_exception:
        # Non-zero exit so GitHub Actions can also flag the run as failed
        # (separate safety net on top of Telegram alerts).
        sys.exit(1)


def handle_global_failure_tracking(state, results):
    """If EVERY page failed in this run, that's much more likely to mean the
    runner's IP/fingerprint got blocked than that all 4 pages independently
    changed their HTML at once -- alert on that distinctly, and faster than
    the per-page DEAD_THRESHOLD, since it points at a different fix
    (rotate hosting/IP, re-check the impersonate profile) rather than a
    parser tweak."""
    gstate = get_global_state(state)

    if results and all(r is False for r in results):
        gstate["consecutive_all_failed_runs"] += 1
    else:
        if gstate["consecutive_all_failed_runs"] > 0:
            print("Recovered from an all-pages-failed streak.")
        gstate["consecutive_all_failed_runs"] = 0
        gstate["all_failed_alert_sent"] = False
        return

    # 6 consecutive fully-failed runs (~30-60 min depending on actual
    # schedule timing) before alerting -- enough to rule out one bad request,
    # short of the per-page DEAD_THRESHOLD so this fires first when relevant.
    ALL_FAILED_THRESHOLD = 6
    if gstate["consecutive_all_failed_runs"] >= ALL_FAILED_THRESHOLD and not gstate["all_failed_alert_sent"]:
        gstate["all_failed_alert_sent"] = True
        send_telegram(
            f"🚫 <b>All monitored pages failed</b> for {gstate['consecutive_all_failed_runs']} checks in a row.\n\n"
            f"This usually means the request fingerprint/IP got blocked by Facebook, "
            f"not that every page's layout changed at once. Worth checking before "
            f"waiting on the individual per-page alerts."
        )


if __name__ == "__main__":
    main()
