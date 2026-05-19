#!/usr/bin/env python3
"""Watch a fixed set of Twitter accounts and push new tweets to Telegram.

Designed for cron every ~1 minute: at-most-once delivery, crash-safe,
zero-spam on first sight of a new account.
"""

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import traceback
import urllib.request
from pathlib import Path

# {handle: numeric user_id} — resolved once via twitter.user_by_screen_name.
ACCOUNTS = {
    "elonmusk":     "44196397",
    "heyibinance":  "1003840309166366721",
    "cz_binance":   "902926941413453824",
}
TWEETS_PER_ACCOUNT = 5

HERE = Path(__file__).resolve().parent
SEEN_FILE = HERE / "seen.json"
SEEN_TMP = HERE / "seen.json.tmp"

# Telegram's hard message limit is 4096 *rendered* chars. HTML entities can
# multiply, so cap the input well below that.
MAX_MSG_CHARS = 3800
SUBPROCESS_TIMEOUT = 30
HTTP_TIMEOUT = 15

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
ID_RE = re.compile(r"^\d+$")

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("twitter_watcher")


# ─── state ────────────────────────────────────────────────────────────────

def load_seen() -> dict[str, str]:
    """Read watermark map. Migrates legacy {handle: [ids]} → {handle: max_id}."""
    if not SEEN_FILE.exists():
        return {}
    raw = json.loads(SEEN_FILE.read_text())
    out: dict[str, str] = {}
    migrated = False
    for handle, val in raw.items():
        if isinstance(val, list):
            migrated = True
            ids = [i for i in val if isinstance(i, str) and ID_RE.match(i)]
            if ids:
                out[handle] = max(ids, key=int)
        elif isinstance(val, str) and ID_RE.match(val):
            out[handle] = val
    if migrated:
        save_seen(out)
        log.info("migrated seen.json from list format to watermark format")
    return out


def save_seen(seen: dict[str, str]) -> None:
    SEEN_TMP.write_text(json.dumps(seen, indent=2, ensure_ascii=False))
    os.replace(SEEN_TMP, SEEN_FILE)


# ─── xapi ─────────────────────────────────────────────────────────────────

def xapi(api_id: str, payload: dict) -> dict:
    """Call xapi-to and return parsed JSON. Raises on transport or API error."""
    proc = subprocess.run(
        ["npx", "xapi-to", "call", api_id, "--input", json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"xapi exit {proc.returncode}: {proc.stderr.strip()[:300]}")
    try:
        body = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"xapi non-JSON output: {proc.stdout[:300]}") from e
    if not body.get("success"):
        raise RuntimeError(f"xapi returned success=false: {str(body)[:300]}")
    return body


def get_recent_tweets(user_id: str, count: int) -> list[dict]:
    resp = xapi("twitter.user_tweets_and_replies", {"user_id": user_id, "count": count})
    tweets = (resp.get("data") or {}).get("tweets") or []
    return tweets[:count]


# ─── formatting ───────────────────────────────────────────────────────────

def html_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


def build_message(handle: str, tweet: dict) -> str:
    if not HANDLE_RE.match(handle):
        raise ValueError(f"invalid handle: {handle!r}")
    tid = tweet.get("id") or ""
    if not ID_RE.match(tid):
        raise ValueError(f"invalid tweet id: {tid!r}")

    text = tweet.get("full_text") or ""
    created = tweet.get("created_at") or ""
    url = f"https://twitter.com/{handle}/status/{tid}"  # built from validated parts

    header = f"<b>@{html_escape(handle)}</b>  <i>{html_escape(created)}</i>"
    body = html_escape(text)
    msg = f"{header}\n\n{body}\n\n{url}"
    if len(msg) > MAX_MSG_CHARS:
        msg = msg[: MAX_MSG_CHARS - 1] + "…"
    return msg


# ─── telegram ─────────────────────────────────────────────────────────────

def send_telegram(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; dry-run:\n%s\n---", text)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        body = json.loads(r.read())
    if not body.get("ok"):
        raise RuntimeError(f"telegram error: {body}")


# ─── main ─────────────────────────────────────────────────────────────────

def process_account(handle: str, user_id: str, seen: dict[str, str]) -> int:
    """Push new tweets for one account. Returns number pushed. Persists watermark."""
    tweets = get_recent_tweets(user_id, TWEETS_PER_ACCOUNT)
    if not tweets:
        log.info("%s: 0 tweets returned", handle)
        return 0

    # Newest-first per API; only consider snowflake-valid ids.
    valid = [t for t in tweets if ID_RE.match(t.get("id") or "")]
    if not valid:
        log.info("%s: no valid ids in response", handle)
        return 0

    newest_id = max((t["id"] for t in valid), key=int)

    if handle not in seen:
        # First sight of this account: snapshot only, never spam.
        seen[handle] = newest_id
        save_seen(seen)
        log.info("%s: initial snapshot at id=%s", handle, newest_id)
        return 0

    watermark = seen[handle]
    new_tweets = [t for t in valid if int(t["id"]) > int(watermark)]
    if not new_tweets:
        return 0

    # Oldest first so Telegram order matches Twitter order.
    new_tweets.sort(key=lambda t: int(t["id"]))

    pushed = 0
    for t in new_tweets:
        msg = build_message(handle, t)
        send_telegram(msg)
        # Advance + flush per successful send → at-most-once on crash.
        seen[handle] = t["id"]
        save_seen(seen)
        pushed += 1

    log.info("%s: pushed %d, watermark=%s", handle, pushed, seen[handle])
    return pushed


def tick() -> int:
    """Run one polling tick. Returns number of tweets pushed."""
    seen = load_seen()
    total = 0
    for handle, user_id in ACCOUNTS.items():
        try:
            total += process_account(handle, user_id, seen)
        except Exception:
            log.error("account %s failed:", handle)
            traceback.print_exc()
            continue
    log.info("done; pushed %d new tweet(s) total", total)
    return total


_stop = threading.Event()


def _handle_signal(signum, _frame):
    log.info("received signal %s, will exit after current tick", signum)
    _stop.set()


def loop(interval: int) -> int:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    log.info("starting loop, interval=%ds", interval)
    while not _stop.is_set():
        try:
            tick()
        except Exception:
            log.error("unexpected tick failure:")
            traceback.print_exc()
        _stop.wait(interval)
    log.info("loop exiting")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true",
                        help="Run forever, polling every --interval seconds")
    parser.add_argument("--interval", type=int, default=60,
                        help="Seconds between ticks in --loop mode (default: 60)")
    args = parser.parse_args()

    if args.loop:
        return loop(args.interval)
    tick()
    return 0


if __name__ == "__main__":
    sys.exit(main())
