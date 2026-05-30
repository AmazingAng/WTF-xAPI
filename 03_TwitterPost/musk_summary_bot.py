#!/usr/bin/env python3
"""
Elon Musk Daily Summary Bot — fetch + filter (24h) + AI summarize + dedupe + post.

Run:
    python3 musk_summary_bot.py           # full pipeline, posts the summary
    python3 musk_summary_bot.py --dry-run # do everything except posting

State file: ~/.xapi/musk_bot_state.json (tweet ids + summary hashes for dedupe)
"""
import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

USER_ID = "44196397"          # @elonmusk
SCREEN_NAME = "elonmusk"
FETCH_COUNT = 100
LOOKBACK_HOURS = 24
CHAR_LIMIT = 200
AI_MODEL = "z-ai/glm-4.6"  # 中文模型,非推理,长 prompt 稳定
STATE_FILE = Path.home() / ".xapi" / "musk_bot_state.json"


def run_xapi(args: list[str]) -> dict:
    """Run an xapi CLI command and return parsed JSON."""
    cmd = ["npx", "xapi-to"] + args
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"xapi failed: {p.stderr or p.stdout}")
    return json.loads(p.stdout)


def fetch_recent_tweets() -> list[dict]:
    res = run_xapi([
        "call", "twitter.user_tweets",
        "--input", json.dumps({"user_id": USER_ID, "count": FETCH_COUNT}),
    ])
    return res.get("data", {}).get("tweets", [])


def filter_last_24h(tweets: list[dict]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    out = []
    for t in tweets:
        ts_str = t.get("created_at")
        if not ts_str:
            continue
        try:
            ts = datetime.strptime(ts_str, "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            continue
        if ts >= cutoff:
            out.append({
                "id": str(t.get("id")),
                "ts": ts.isoformat(),
                "text": (t.get("full_text") or "").strip(),
                "is_retweet": bool(t.get("is_retweet")),
                "lang": t.get("lang"),
                "favs": t.get("favorite_count", 0),
                "views": t.get("views_count", 0),
            })
    return sorted(out, key=lambda x: x["ts"])


def summarize_with_ai(tweets: list[dict]) -> str:
    if not tweets:
        return ""
    lines = []
    for t in tweets:
        tag = "[RT]" if t["is_retweet"] else "[原创]"
        lines.append(f"{tag} {t['text'][:280]}")
    digest = "\n---\n".join(lines)

    today = datetime.now().strftime("%-m月%-d日")
    prompt = (
        f"把以下马斯克 24h 内 {len(tweets)} 条推文总结成一条中文 X 推文。\n\n"
        f"格式:\n"
        f"#马斯克日报 {today}\n"
        f"\n"
        f"1️⃣ 主题A:核心事件 + 马斯克的态度\n"
        f"2️⃣ 主题B:核心事件 + 马斯克的态度\n"
        f"3️⃣ ...\n\n"
        f"要求:\n"
        f"- 同主题合并,突出产品/人名/数字/事件\n"
        f"- 必须体现马斯克的态度(力挺/吐槽/反驳/质疑/官宣/分享 等)\n"
        f"- 多主题用 1️⃣ 2️⃣ 3️⃣ emoji 数字编号,每个主题独占一行(用换行)\n"
        f"- 单主题可不分点直接写\n"
        f"- 客观提炼,不替马斯克下结论\n"
        f"- 全文 ≤200 中文字符\n"
        f"- 只输出推文正文,不要引号、Markdown、解释、思考过程、开场白\n\n"
        f"素材:\n{digest}"
    )

    payload = {"model": AI_MODEL, "messages": [{"role": "user", "content": prompt}]}
    content = ""
    last_err = None
    for attempt in range(3):
        try:
            res = run_xapi(["call", "ai.text.chat.fast", "--input", json.dumps(payload)])
            msg = res["data"]["choices"][0]["message"]
            content = (msg.get("content") or "").strip()
            if content:
                break
            print(f"  ↻ AI returned empty content (attempt {attempt+1}/3), retrying...")
        except Exception as e:
            last_err = e
            print(f"  ↻ AI call failed (attempt {attempt+1}/3): {str(e)[:120]}, retrying...")
    if not content:
        raise RuntimeError(f"AI failed after 3 attempts. last_err={last_err}")
    # strip any wrapping quotes the model might add
    if content.startswith(("「", "「", '"', "「")) and content.endswith(("」", "」", '"')):
        content = content[1:-1].strip()
    return content


def truncate_chars(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"posted_hashes": [], "last_tweet_ids": [], "history": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def post_tweet(text: str) -> dict:
    res = run_xapi([
        "call", "x-official.2_tweets", "--method", "POST",
        "--input", json.dumps({"body": {"text": text}}),
    ])
    return res["data"]["data"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Skip posting; just print the summary.")
    ap.add_argument("--force", action="store_true", help="Bypass dedupe gates (for testing).")
    args = ap.parse_args()

    print(f"=== Musk Summary Bot @ {datetime.now().isoformat(timespec='seconds')} ===")

    # 1) Fetch + filter
    raw = fetch_recent_tweets()
    print(f"[1/3] Fetched {len(raw)} raw tweets from @{SCREEN_NAME}.")
    recent = filter_last_24h(raw)
    print(f"[1/3] {len(recent)} tweets in last {LOOKBACK_HOURS}h.")
    if not recent:
        print("→ Nothing to summarize. Exit.")
        return 0

    # Dedupe gate 1: same tweet set as previous run?
    state = load_state()
    new_ids = [t["id"] for t in recent]
    if not args.force and set(new_ids) == set(state.get("last_tweet_ids", [])):
        print("→ Same tweet set as last run. Skip to avoid dup post. (use --force to override)")
        return 0

    # 2) Summarize
    summary = summarize_with_ai(recent)
    print(f"\n[2/3] AI summary ({len(summary)} chars):\n{summary}\n")
    summary = truncate_chars(summary, CHAR_LIMIT)
    if len(summary) < len(summary):
        print(f"[2/3] Truncated to {CHAR_LIMIT} chars.")

    # Dedupe gate 2: identical summary text already posted?
    h = hashlib.sha256(summary.encode("utf-8")).hexdigest()[:16]
    if not args.force and h in state.get("posted_hashes", []):
        print(f"→ Summary hash {h} already posted before. Skip. (use --force to override)")
        return 0

    # 3) Post
    if args.dry_run:
        print("[3/3] --dry-run: not posting.")
        return 0

    posted = post_tweet(summary)
    print(f"[3/3] ✅ Posted tweet id={posted['id']}")
    print(f"      https://x.com/i/web/status/{posted['id']}")

    # Save state
    state["posted_hashes"] = (state.get("posted_hashes", []) + [h])[-50:]
    state["last_tweet_ids"] = new_ids
    state["last_run_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["last_posted_id"] = posted["id"]
    state.setdefault("history", []).append({
        "ts": state["last_run_utc"],
        "tweet_id": posted["id"],
        "summary": summary,
        "source_count": len(recent),
    })
    state["history"] = state["history"][-30:]
    save_state(state)
    print(f"      State → {STATE_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
