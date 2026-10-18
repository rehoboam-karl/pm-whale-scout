#!/usr/bin/env python3
"""
Whale Scout — Polymarket Leaderboard-powered whale discovery.

Uses Polymarket's public Leaderboard API to find consistently profitable,
high-volume traders. Scores whales across multiple time windows and
recommends list changes for copy-trading bots.

Usage:
    python whale_scout.py                  # Analyze + print report
    python whale_scout.py --json          # Output JSON (for automation)
    python whale_scout.py --top 10        # Show top N whales (default: 15)
    python whale_scout.py --scores         # Print detailed score breakdown

Environment variables:
    LEADERBOARD_API      Polymarket LB API base URL
                         (default: https://lb-api.polymarket.com)
    TELEGRAM_BOT_TOKEN   Telegram bot token (for --telegram)
    TELEGRAM_CHAT_ID     Telegram chat ID (for --telegram)
    OUTPUT_DIR           Where to write results (default: ./output)
"""

import json
import os
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

import requests

# ─── Config from env ────────────────────────────────────────────────────────

LB_API = os.environ.get("LEADERBOARD_API", "https://lb-api.polymarket.com")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "./output"))
OUTPUT_FILE = OUTPUT_DIR / "whale_scout.json"
HISTORY_FILE = OUTPUT_DIR / "whale_scout_history.jsonl"

# ─── Scoring weights ─────────────────────────────────────────────────────────
# Calibrated for Polymarket copy-trading: recent profit dominates

W_PROFIT_30D = 0.35      # Recent profitability matters most
W_PROFIT_7D  = 0.25      # Very recent momentum
W_PROFIT_ALL  = 0.10      # Lifetime track record
W_VOLUME      = 0.20      # Activity level
W_CONSISTENCY = 0.10     # Appears in multiple time windows

# ─── Thresholds ─────────────────────────────────────────────────────────────
MIN_PROFIT_30D = 500      # Must have made >$500 in 30d
MIN_VOLUME     = 10_000   # Must have >$10k volume
MAX_WHALES     = 15       # Target whale count
MIN_SCORE_TO_ADD = 10      # Minimum composite score to add

# Addresses to never include (known bots, market makers, your own wallets)
BLACKLIST = {
    # Add your own wallet addresses here:
    # "0x0000000000000000000000000000000000000001",
}


# ─── API ────────────────────────────────────────────────────────────────────

def fetch_leaderboard(metric: str = "profit", window: str = "all", limit: int = 200):
    """Fetch leaderboard data from Polymarket public LB API."""
    try:
        r = requests.get(
            f"{LB_API}/{metric}",
            params={"window": window, "limit": limit},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"  ⚠ {metric}/{window}: HTTP {r.status_code}", file=sys.stderr)
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"  ⚠ {metric}/{window}: {e}", file=sys.stderr)
        return []


def _normalize_address(addr: str) -> str:
    """Normalize address: lowercase, no 0x prefix."""
    return addr.lower().replace("0x", "")


def _percentile(val: float, all_vals: list[float]) -> float:
    """Rank-based percentile (0-100) within the fetched set."""
    if not all_vals or max(all_vals) <= 0:
        return 0.0
    return min(100.0, (val / max(all_vals)) * 100.0)


# ─── Scoring ─────────────────────────────────────────────────────────────────

def score_whales() -> list[dict]:
    """
    Fetch multiple leaderboards and compute composite whale scores.

    Returns a sorted list of dicts with keys:
        address, name, composite_score, profit_*, volume, windows_present, scores
    """
    print("📡 Fetching Polymarket leaderboards...", file=sys.stderr)

    # Fetch all windows in parallel-ish (sequential for simplicity)
    profit_all = fetch_leaderboard("profit", "all", 200)
    profit_30d = fetch_leaderboard("profit", "30d", 200)
    profit_7d  = fetch_leaderboard("profit", "7d", 200)
    profit_1d  = fetch_leaderboard("profit", "1d", 100)
    volume_all  = fetch_leaderboard("volume", "all", 200)

    print(f"  profit/all: {len(profit_all)} | profit/30d: {len(profit_30d)} | "
          f"profit/7d: {len(profit_7d)} | profit/1d: {len(profit_1d)} | "
          f"volume/all: {len(volume_all)}", file=sys.stderr)

    # Index by normalized address
    def index_by_addr(data: list) -> dict:
        result = {}
        for entry in data:
            addr = _normalize_address(entry.get("proxyWallet", "") or entry.get("address", ""))
            if addr:
                result[addr] = entry
        return result

    idx_all  = index_by_addr(profit_all)
    idx_30d  = index_by_addr(profit_30d)
    idx_7d   = index_by_addr(profit_7d)
    idx_1d   = index_by_addr(profit_1d)
    idx_vol  = index_by_addr(volume_all)

    all_addrs = set()
    for idx in [idx_all, idx_30d, idx_7d, idx_vol]:
        all_addrs.update(idx.keys())

    all_addrs -= {_normalize_address(a) for a in BLACKLIST}
    print(f"  {len(all_addrs)} unique traders found", file=sys.stderr)

    # Precompute max values for normalization
    all_p30d = [float(e.get("amount", 0)) for e in profit_30d if float(e.get("amount", 0)) > 0]
    all_p7d  = [float(e.get("amount", 0)) for e in profit_7d  if float(e.get("amount", 0)) > 0]
    all_pall = [float(e.get("amount", 0)) for e in profit_all if float(e.get("amount", 0)) > 0]
    all_vol  = [float(e.get("amount", 0)) for e in volume_all if float(e.get("amount", 0)) > 0]

    scored = []
    for addr in all_addrs:
        p_all = idx_all.get(addr, {})
        p_30d = idx_30d.get(addr, {})
        p_7d  = idx_7d.get(addr, {})
        p_1d  = idx_1d.get(addr, {})
        v_all = idx_vol.get(addr, {})

        profit_all_val = float(p_all.get("amount", 0))
        profit_30d_val = float(p_30d.get("amount", 0))
        profit_7d_val  = float(p_7d.get("amount", 0))
        profit_1d_val  = float(p_1d.get("amount", 0))
        volume_val      = float(v_all.get("amount", 0))

        # Name from any source
        name = ""
        for src in [p_30d, p_7d, p_all, v_all, p_1d]:
            name = src.get("pseudonym", "") or src.get("name", "") or ""
            if name:
                break
        if not name:
            name = addr[:12] + "..."

        # Filter: must meet minimum thresholds
        if profit_30d_val < MIN_PROFIT_30D:
            continue
        if volume_val < MIN_VOLUME:
            continue
        if profit_7d_val < 0:   # Skip losing traders
            continue

        # Normalize each dimension to 0-100
        s_30d = _percentile(profit_30d_val, all_p30d)
        s_7d  = _percentile(profit_7d_val,  all_p7d)
        s_all = _percentile(profit_all_val, all_pall)
        s_vol = _percentile(volume_val,      all_vol)

        # Consistency: appears in how many windows?
        windows_present = sum(1 for x in [p_all, p_30d, p_7d, p_1d] if x)
        s_consistency = (windows_present / 4) * 100.0

        # Weighted composite score
        composite = (
            W_PROFIT_30D * s_30d +
            W_PROFIT_7D  * s_7d  +
            W_PROFIT_ALL * s_all +
            W_VOLUME     * s_vol +
            W_CONSISTENCY * s_consistency
        )

        scored.append({
            "address":          addr,
            "name":            name,
            "composite_score":  round(composite, 1),
            "profit_all":      round(profit_all_val, 2),
            "profit_30d":     round(profit_30d_val, 2),
            "profit_7d":       round(profit_7d_val, 2),
            "profit_1d":       round(profit_1d_val, 2),
            "volume":          round(volume_val, 2),
            "windows_present": windows_present,
            "scores": {
                "s_30d":        round(s_30d, 1),
                "s_7d":         round(s_7d, 1),
                "s_all":        round(s_all, 1),
                "s_vol":        round(s_vol, 1),
                "s_consistency": round(s_consistency, 1),
            },
        })

    scored.sort(key=lambda x: -x["composite_score"])
    return scored


# ─── Recommendations ───────────────────────────────────────────────────────────

def recommend_changes(scored: list[dict], current_whales: list[str]) -> tuple[list[dict], list[dict]]:
    """
    Recommend adds/removes by comparing scored candidates against current list.
    Returns (to_add, to_remove).
    """
    current_set = {a.lower().replace("0x", "") for a in current_whales}

    # Find underperformers in current list
    current_scores = {w["address"]: w["composite_score"] for w in scored}

    to_remove = []
    for addr in current_whales:
        addr_n = _normalize_address(addr)
        score = current_scores.get(addr_n, 0)
        if score < MIN_SCORE_TO_ADD * 0.5:
            name = next((w["name"] for w in scored if w["address"] == addr_n), addr_n[:12])
            to_remove.append({
                "address": addr_n,
                "name": name,
                "score": round(score, 1),
                "reason": "low_score",
            })

    # Find new candidates not in current list
    to_add = []
    for whale in scored:
        if whale["address"] not in current_set and whale["composite_score"] >= MIN_SCORE_TO_ADD:
            to_add.append(whale)
            if len(current_whales) - len(to_remove) + len(to_add) >= MAX_WHALES:
                break

    return to_add, to_remove


# ─── Output formatters ────────────────────────────────────────────────────────

def format_report(scored: list[dict], to_add: list[dict], to_remove: list[dict]) -> str:
    """Plain-text report for terminal output."""
    lines = [
        "🐋 WHALE SCOUT — Polymarket Leaderboard Analysis",
        f"📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 65,
        f"\n📊 Top {min(15, len(scored))} Whale Candidates:",
        f"{'#':>2} {'Name':<24} {'Score':>5} {'30d P&L':>12} {'7d P&L':>10} {'Volume':>14}",
        "-" * 72,
    ]

    for i, w in enumerate(scored[:15], 1):
        lines.append(
            f"{i:>2} {w['name']:<24} {w['composite_score']:>5.1f} "
            f"${w['profit_30d']:>11,.0f} ${w['profit_7d']:>9,.0f} "
            f"${w['volume']:>13,.0f}"
        )

    if to_remove:
        lines.append(f"\n🔴 Remove ({len(to_remove)}):")
        for w in to_remove:
            lines.append(f"  ❌ {w['name']:<24} score: {w['score']:.1f} — {w['reason']}")

    if to_add:
        lines.append(f"\n🟢 Add ({len(to_add)}):")
        for w in to_add:
            lines.append(
                f"  ✅ {w['name']:<24} score: {w['composite_score']:.1f} | "
                f"30d: ${w['profit_30d']:,.0f} | 7d: ${w['profit_7d']:,.0f}"
            )

    if not to_add and not to_remove:
        lines.append("\n✅ Whale list is optimal — no changes recommended.")

    return "\n".join(lines)


def format_telegram(scored: list[dict], to_add: list[dict], to_remove: list[dict]) -> str:
    """Compact HTML report for Telegram."""
    lines = ["🐋 <b>Whale Scout</b>", f"📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"]

    lines.append("<b>Top 10:</b>")
    for i, w in enumerate(scored[:10], 1):
        lines.append(f"{i}. {w['name']}: {w['composite_score']:.0f}pts | ${w['profit_30d']:,.0f}/30d")

    if to_remove:
        lines.append(f"\n🔴 <b>Remove ({len(to_remove)}):</b>")
        for w in to_remove:
            lines.append(f"  ❌ {w['name']} ({w['score']:.0f}pts)")

    if to_add:
        lines.append(f"\n🟢 <b>Add ({len(to_add)}):</b>")
        for w in to_add[:5]:
            lines.append(f"  ✅ {w['name']} ({w['composite_score']:.0f}pts, ${w['profit_30d']:,.0f}/30d)")

    if not to_add and not to_remove:
        lines.append("\n✅ Whale list is optimal.")

    return "\n".join(lines)


def send_telegram(text: str) -> bool:
    """Send message via Telegram bot. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  ⚠ Telegram credentials not set (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)", file=sys.stderr)
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }, timeout=10)
        return True
    except Exception as e:
        print(f"  ⚠ Telegram send failed: {e}", file=sys.stderr)
        return False


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Polymarket Whale Scout")
    parser.add_argument("--json",    action="store_true", help="Output raw JSON")
    parser.add_argument("--top",     type=int, default=15,  help="Show top N whales (default: 15)")
    parser.add_argument("--scores",  action="store_true", help="Print detailed score breakdown")
    parser.add_argument("--telegram", action="store_true", help="Send report to Telegram")
    parser.add_argument("--current", nargs="*", default=[], help="Current whale addresses to compare against")
    args = parser.parse_args()

    scored = score_whales()
    print(f"🎯 {len(scored)} qualified whales found\n", file=sys.stderr)

    to_add, to_remove = recommend_changes(scored, args.current)

    if args.json:
        output = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "whales": scored[:args.top],
            "recommendations": {
                "add":    [{"address": w["address"], "name": w["name"], "score": w["composite_score"]} for w in to_add],
                "remove": to_remove,
            },
        }
        print(json.dumps(output, indent=2))
    else:
        report = format_report(scored[:args.top], to_add, to_remove)
        print(report)

        if args.scores:
            print("\n📈 Score Breakdown:")
            for w in scored[:args.top]:
                s = w["scores"]
                print(f"  {w['name']:<24} composite={w['composite_score']:>5.1f}  "
                      f"30d={s['s_30d']:>5.1f} 7d={s['s_7d']:>5.1f} "
                      f"all={s['s_all']:>5.1f} vol={s['s_vol']:>5.1f} "
                      f"cons={s['s_consistency']:>5.1f}")

    # Save to disk
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "whales": scored[:30],
        "recommendations": {
            "add":    [{"address": w["address"], "name": w["name"]} for w in to_add],
            "remove": [{"address": w["address"], "name": w["name"]} for w in to_remove],
        },
    }
    OUTPUT_FILE.write_text(json.dumps(output, indent=2))
    with HISTORY_FILE.open("a") as f:
        f.write(json.dumps({
            "timestamp": output["timestamp"],
            "total_scored": len(scored),
            "adds": len(to_add),
            "removes": len(to_remove),
        }) + "\n")

    # Telegram
    if args.telegram:
        if send_telegram(format_telegram(scored, to_add, to_remove)):
            print("\n📱 Sent to Telegram", file=sys.stderr)

    print(f"\n💾 Results: {OUTPUT_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
