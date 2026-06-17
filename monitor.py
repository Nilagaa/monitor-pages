#!/usr/bin/env python3
"""
Facebook Page Post Monitor -> Telegram Notifier

Checks a list of public Facebook pages (via the lightweight mbasic.facebook.com
mobile interface) for new posts, and sends a Telegram message for each new post
found. Designed to run on a schedule (e.g. every 5-10 min via GitHub Actions).

State (last-seen post per page, and failure counters) is persisted to state.json,
which the GitHub Action commits back to the repo between runs.
"""

import os
import re
import json
import sys
import time
import hashlib
import requests
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

STATE_FILE = "state.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# How many consecutive failed checks before we send a "this page looks dead" alert.
# At a 5-10 min schedule, ~36 failures ≈ 3-6 hours of continuous failure.
DEAD_THRESHOLD = 36

# How many posts back to look at / notify on first run (avoid spamming old history).
MAX_POSTS_PER_PAGE = 5

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; SM-G960F) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/115.0 Mobile Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

BASE_URL = "https://mbasic.facebook.com"


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
# Facebook scraping (mbasic)
# ---------------------------------------------------------------------------

def fetch_page_html(page_id):
    """Fetch the mbasic page for a given page id/username."""
    if page_id.startswith("profile.php"):
        url = f"{BASE_URL}/{page_id}"
    else:
        url = f"{BASE_URL}/{page_id}"
    resp = requests.get(url, headers=HEADERS, timeout=20)
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
    Parse posts out of an mbasic Facebook page HTML.

    mbasic's structure changes periodically, so this uses a few fallback
    strategies. Returns a list of dicts: {id, text, link}, most recent first.
    """
    soup = BeautifulSoup(html, "lxml")
    posts = []

    # Strategy 1: look for article-like containers with a permalink to a story
    # mbasic typically has links like /story.php?story_fbid=...&id=...
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
    page_id = page["id"]
    page_name = page["name"]
    pstate = get_page_state(state, page_id)

    try:
        html = fetch_page_html(page_id)
    except requests.RequestException as e:
        handle_failure(page_name, pstate, f"network error: {e}")
        return

    if looks_like_blocked_or_login(html):
        handle_failure(page_name, pstate, "received a login/checkpoint page instead of content")
        return

    posts = extract_posts(html)

    if not posts:
        handle_failure(page_name, pstate, "no posts found (layout may have changed)")
        return

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
        return

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

    for page in PAGES:
        try:
            check_page(page, state)
        except Exception as e:
            any_exception = True
            print(f"[{page['name']}] Unexpected exception: {e}", file=sys.stderr)
            pstate = get_page_state(state, page["id"])
            handle_failure(page["name"], pstate, f"unexpected exception: {e}")
        time.sleep(2)  # small courtesy delay between page requests

    save_state(state)

    if any_exception:
        # Non-zero exit so GitHub Actions can also flag the run as failed
        # (separate safety net on top of Telegram alerts).
        sys.exit(1)


if __name__ == "__main__":
    main()
