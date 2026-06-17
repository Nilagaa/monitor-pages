# Facebook Page Monitor → Telegram

Notifies you on Telegram within ~5-10 minutes when any of your tracked
Facebook pages post something new. Runs for free, forever, on GitHub Actions
— no laptop or server needs to stay on.

## How it works (quick version)

Every ~7 minutes, GitHub spins up a temporary machine, runs `monitor.py`,
which visits the lightweight `mbasic.facebook.com` version of each page,
checks for posts it hasn't seen before, and messages you on Telegram if it
finds any. It remembers what it's already seen in `state.json`, which gets
committed back to this repo after every run.

---

## Setup (do this once)

### 1. Create a Telegram bot

1. Open Telegram, search for **@BotFather**, start a chat.
2. Send `/newbot`, follow the prompts (give it any name/username).
3. BotFather replies with a **token** that looks like
   `123456789:AAH...`. Save this — it's your `TELEGRAM_BOT_TOKEN`.

### 2. Get your Telegram chat ID

1. Search for **@userinfobot** on Telegram and start a chat with it (or send
   any message to your new bot first, then use the method below).
2. Easiest method: send any message to your new bot, then visit this URL in
   your browser (replace `<TOKEN>` with your bot token):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Look for `"chat":{"id":` in the response — that number (can be negative)
   is your `TELEGRAM_CHAT_ID`.

### 3. Create a GitHub repository

1. Go to github.com, create a new repository (private is fine — your free
   minutes easily cover a 7-minute cron; see "Costs" below).
2. Upload all the files in this folder (`monitor.py`, `requirements.txt`,
   `state.json`, and the `.github/workflows/monitor.yml` file — keep that
   folder structure exactly as-is) to the repo. Easiest way: use GitHub's
   "Add file → Upload files" in the web UI, or `git push` if you're
   comfortable with git.

### 4. Add your secrets to the repo

1. In your repo, go to **Settings → Secrets and variables → Actions**.
2. Click **New repository secret**, add:
   - Name: `TELEGRAM_BOT_TOKEN`, Value: (your bot token)
3. Add another:
   - Name: `TELEGRAM_CHAT_ID`, Value: (your chat id)

### 5. Turn it on

1. Go to the **Actions** tab in your repo. GitHub sometimes disables
   workflows on upload — click "I understand my workflows, go ahead and
   enable them" if you see that banner.
2. Click into **Facebook Page Monitor** on the left, then **Run workflow**
   (top right) to trigger it manually the first time — don't wait for the
   schedule.
3. Watch it run. Click into the run → the "Run monitor" step to see the
   logs. On this first run it will just record a baseline of existing posts
   per page (no notifications) — that's expected, so you don't get
   spammed with old posts.
4. After that first baseline run, it'll check again every ~7 minutes
   automatically and notify you of anything genuinely new.

That's it — close the tab, go about your day. Your phone gets the Telegram
notification whenever something posts.

---

## Adding or removing pages later

Open `monitor.py`, find the `PAGES` list near the top:

```python
PAGES = [
    {"name": "Antipolo City 1st District Office", "id": "AntipoloCity1stCongressionalDistrictOffice"},
    {"name": "Profile 61553138835377", "id": "profile.php?id=61553138835377"},
    {"name": "Juny Nares Official", "id": "junynaresofficial"},
    {"name": "Love Tayo ni Onza", "id": "LoveTayoniOnza1"},
]
```

- `name` is just the label used in your notifications — change it to
  whatever's readable to you.
- `id` is whatever comes after `facebook.com/` in the page's URL. For
  pages without a custom username, use the full `profile.php?id=...` part.

Add a new page = add a new `{"name": ..., "id": ...}` line. Commit the
change. The next scheduled run picks it up automatically (and will do a
one-time baseline run for just that new page, same as setup).

---

## How you'll know if something breaks

You explicitly asked for this, so to be clear about what's built in:

- **Per-page "looks dead" alert**: if a specific page fails to load/parse
  for ~36 consecutive checks (roughly 3-6 hours depending on actual
  schedule timing), you get a Telegram message like:
  > ⚠️ [Page Name] monitor has failed 36 checks in a row... Facebook
  > changed its layout, is blocking requests, or the URL is invalid.

  It only alerts once per outage (not every single failed cycle), and
  sends a "✅ back online" message once it recovers.

- **GitHub email alerts**: if the script crashes outright (not just "found
  zero posts" but an unhandled error), GitHub Actions marks the run as
  failed and — by default — emails the address on your GitHub account.
  You can check this anytime under the repo's **Actions** tab; failed runs
  are marked with a red ✗.

- **What you should do if you get either alert**: it almost always means
  Facebook tweaked the mbasic page's HTML. Tell me (or whoever's helping)
  what the failure reason said, and the parsing logic in `extract_posts()`
  in `monitor.py` gets adjusted. It's a small fix, not a rebuild.

There's no alert that's 100% guaranteed (e.g. if GitHub Actions itself has
an outage, or you stop having internet/email access), but between the two
above you'll know within hours, not days, if a page monitor goes dark.

---

## Costs / limits (why this is free indefinitely)

- **Private repos**: 2,000 free Action minutes/month. This job takes
  roughly 30-45 seconds per run. At every 7 minutes, that's about
  200 runs/day × ~40 sec ≈ 130 minutes/day... which is actually *over*
  the free private-repo allowance if run 24/7 every 7 minutes for a
  full month (2,000 min/month ÷ 30 days ≈ 66 min/day budget).
- **Public repos**: unlimited free Action minutes, no issue at all.

**Practical recommendation**: either make the repo public (fine since
there's no sensitive data in it — page IDs and post text are already
public), or widen the schedule slightly (e.g. every 10 minutes instead of
7) to comfortably fit the private free tier. I set it to 7 minutes assuming
you might go public; tell me which you'd prefer and I'll adjust the cron
if needed.

---

## Known limitations (being upfront)

- This relies on scraping Facebook's mobile site, which is against
  Facebook's Terms of Service. No Facebook account/login is used, so
  there's no account-ban risk to you personally — but Facebook can block
  the requesting IP (GitHub's shared IPs) if it gets suspicious, which
  would show up as the "looks dead" alert above.
- Facebook periodically changes its HTML, which can break the parser.
  This is expected to happen occasionally — see the alert system above.
- "Real-time" here means within one polling cycle (~5-10 min), not
  instant. True instant would require Facebook's own push
  infrastructure, which isn't available for free for third-party page
  monitoring.
