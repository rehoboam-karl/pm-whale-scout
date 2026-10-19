#!/usr/bin/env python3
"""
Whale Scout — Polymarket Leaderboard-powered whale discovery.

Uses Polymarket's public Leaderboard API v2 to find consistently profitable,
high-volume traders. Scores whales across multiple time windows and
recommends list changes for copy-trading bots.

Usage:
    python whale_scout.py                  # Analyze + print report
    python whale_scout.py --json          # Output JSON (for automation)
    python whale_scout.py --top 10        # Show top N whales (default: 15)
    python whale_scout.py --scores         # Print detailed score breakdown

Environment variables:
    LB_API              Polymarket LB API v2 base URL
                        (default: https://data-api.polymarket.com)
    TELEGRAM_BOT_TOKEN Telegram bot token (for --telegram)
    TELEGRAM_CHAT_ID   Telegram chat ID (for --telegram)
    OUTPUT_DIR         Where to write results (default: ./output)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# ─── Config from env ────────────────────────────────────────────────────────

LB_API = os.environ.get("LB_API", "https://data-api.polymarket.com")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "./output"))
OUTPUT_FILE = OUTPUT_DIR / "whale_scout.json"
HISTORY_FILE = OUTPUT_DIR / "whale_scout_history.jsonl"

# ─── Scoring weights ─────────────────────────────────────────────────────────
# Calibrated for Polymarket copy-trading: recent profit dominates

W_PROFIT_30D = 0.40  # Recent profitability matters most
W_PROFIT_7D = 0.30  # Very recent momentum
W_VOLUME = 0.20  # Activity level
W_CONSISTENCY = 0.10  # Appears in both 7d and 30d windows

# ─── Thresholds ─────────────────────────────────────────────────────────────
MIN_PROFIT_30D = 500  # Must have made >$500 in 30d
MIN_VOLUME = 10_000  # Must have >$10k volume
MAX_WHALES = 15  # Target whale count
MIN_SCORE_TO_ADD = 10  # Minimum composite score to add

# Addresses to never include (known bots, market makers, your own wallets)
BLACKLIST = {
    # Add your own wallet addresses here:
    # "0x0000000000000000000000000000000000000001",
}


# ─── API v2 ─────────────────────────────────────────────────────────────────


def _fetch_board(
    time_period: str, sort_by: str = "PNL", limit: int = 200
) -> tuple[list[dict], str | None]:
    """
    Fetch a leaderboard page from Polymarket Data API v2.
    Returns (entries, next_cursor). Cursor is None when no more pages.
    """
    try:
        r = requests.get(
            f"{LB_API}/v2/leaderboard",
            params={"time_period": time_period, "sort_by": sort_by, "limit": limit},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"  ⚠ leaderboard/{time_period}/{sort_by}: HTTP {r.status_code}", file=sys.stderr)
            return [], None
        data = r.json()
        entries = data.get("data", [])
        cursor = data.get("pagination", {}).get("next_cursor")
        return entries, cursor
    except (ValueError, OSError) as e:
        print(f"  ⚠ leaderboard/{time_period}/{sort_by}: {e}", file=sys.stderr)
        return [], None


def _fetch_all_pages(
    time_period: str, sort_by: str = "PNL", limit: int = 200, max_pages: int = 5
) -> list[dict]:
    """Fetch all pages of a leaderboard (capped at max_pages for safety)."""
    all_entries = []
    cursor = None
    for _ in range(max_pages):
        page, cursor = _fetch_board(time_period, sort_by, limit)
        all_entries.extend(page)
        if not cursor:
            break
        if len(all_entries) >= 500:
            break  # Safety cap
    return all_entries


# ─── Normalization helpers ────────────────────────────────────────────────────


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
    Fetch multiple leaderboard windows from Polymarket API v2
    and compute composite whale scores.

    Returns a sorted list of dicts with keys:
        address, name, composite_score, profit_*, volume, windows_present
    """
    print("📡 Fetching Polymarket Leaderboard API v2...", file=sys.stderr)

    # Fetch month (30d equivalent) and week (7d equivalent) windows
    board_month = _fetch_all_pages("month", "PNL", limit=200)
    board_week = _fetch_all_pages("week", "PNL", limit=200)
    board_volume = _fetch_all_pages("month", "VOLUME", limit=200)

    print(
        f"  month: {len(board_month)} | week: {len(board_week)} | volume: {len(board_volume)}",
        file=sys.stderr,
    )

    # Index by normalized address
    def index_by_addr(data: list) -> dict:
        result = {}
        for entry in data:
            addr = _normalize_address(entry.get("user_id", ""))
            if addr:
                result[addr] = entry
        return result

    idx_month = index_by_addr(board_month)
    idx_week = index_by_addr(board_week)
    idx_volume = index_by_addr(board_volume)

    # Collect all unique addresses
    all_addrs = set(idx_month.keys()) | set(idx_week.keys()) | set(idx_volume.keys())
    all_addrs -= {_normalize_address(a) for a in BLACKLIST}
    print(f"  {len(all_addrs)} unique traders found", file=sys.stderr)

    # Precompute max values for normalization
    all_p30d = [float(e.get("pnl", 0)) for e in board_month if float(e.get("pnl", 0)) > 0]
    all_p7d = [float(e.get("pnl", 0)) for e in board_week if float(e.get("pnl", 0)) > 0]
    all_vol = [float(e.get("volume", 0)) for e in board_volume if float(e.get("volume", 0)) > 0]

    scored = []
    for addr in all_addrs:
        e_month = idx_month.get(addr, {})
        e_week = idx_week.get(addr, {})
        e_volume = idx_volume.get(addr, {})

        profit_30d_val = float(e_month.get("pnl", 0)) if e_month else 0.0
        profit_7d_val = float(e_week.get("pnl", 0)) if e_week else 0.0
        volume_val = float(e_volume.get("volume", 0)) if e_volume else 0.0

        # Name
        name = (
            (e_month.get("user_name", "") or e_week.get("user_name", "") or "")
            if e_month or e_week
            else ""
        )
        if not name:
            name = addr[:12] + "..."

        # Filter: must meet minimum thresholds
        if profit_30d_val < MIN_PROFIT_30D:
            continue
        if volume_val < MIN_VOLUME:
            continue
        if profit_7d_val < 0:  # Skip recent losers
            continue

        # Normalize each dimension to 0-100
        s_30d = _percentile(profit_30d_val, all_p30d)
        s_7d = _percentile(profit_7d_val, all_p7d)
        s_vol = _percentile(volume_val, all_vol)

        # Consistency: appears in both month and week?
        in_month = bool(e_month)
        in_week = bool(e_week)
        windows_present = int(in_month) + int(in_week)
        s_consistency = (windows_present / 2) * 100.0

        # Weighted composite score
        composite = (
            W_PROFIT_30D * s_30d
            + W_PROFIT_7D * s_7d
            + W_VOLUME * s_vol
            + W_CONSISTENCY * s_consistency
        )

        scored.append(
            {
                "address": addr,
                "name": name,
                "composite_score": round(composite, 1),
                "profit_30d": round(profit_30d_val, 2),
                "profit_7d": round(profit_7d_val, 2),
                "volume": round(volume_val, 2),
                "windows_present": windows_present,
                "scores": {
                    "s_30d": round(s_30d, 1),
                    "s_7d": round(s_7d, 1),
                    "s_vol": round(s_vol, 1),
                    "s_consistency": round(s_consistency, 1),
                },
            }
        )

    scored.sort(key=lambda x: -x["composite_score"])
    return scored


# ─── Recommendations ───────────────────────────────────────────────────────────


def recommend_changes(
    scored: list[dict], current_whales: list[str]
) -> tuple[list[dict], list[dict]]:
    """
    Recommend adds/removes by comparing scored candidates against current list.
    Returns (to_add, to_remove).
    """
    current_set = {a.lower().replace("0x", "") for a in current_whales}
    current_scores = {w["address"]: w["composite_score"] for w in scored}

    to_remove = []
    for addr in current_whales:
        addr_n = _normalize_address(addr)
        score = current_scores.get(addr_n, 0)
        if score < MIN_SCORE_TO_ADD * 0.5:
            name = next((w["name"] for w in scored if w["address"] == addr_n), addr_n[:12])
            to_remove.append(
                {
                    "address": addr_n,
                    "name": name,
                    "score": round(score, 1),
                    "reason": "low_score",
                }
            )

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
    lines = [
        "🐋 <b>Whale Scout</b>",
        f"📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n",
    ]

    lines.append("<b>Top 10:</b>")
    for i, w in enumerate(scored[:10], 1):
        lines.append(
            f"{i}. {w['name']}: {w['composite_score']:.0f}pts | ${w['profit_30d']:,.0f}/30d"
        )

    if to_remove:
        lines.append(f"\n🔴 <b>Remove ({len(to_remove)}):</b>")
        for w in to_remove:
            lines.append(f"  ❌ {w['name']} ({w['score']:.0f}pts)")

    if to_add:
        lines.append(f"\n🟢 <b>Add ({len(to_add)}):</b>")
        for w in to_add[:5]:
            lines.append(
                f"  ✅ {w['name']} ({w['composite_score']:.0f}pts, ${w['profit_30d']:,.0f}/30d)"
            )

    if not to_add and not to_remove:
        lines.append("\n✅ Whale list is optimal.")

    return "\n".join(lines)


def send_telegram(text: str) -> bool:
    """Send a message via Telegram bot. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  ⚠ Telegram credentials not set", file=sys.stderr)
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        return True
    except (ValueError, OSError) as e:
        print(f"  ⚠ Telegram send failed: {e}", file=sys.stderr)
        return False


# ─── Main ───────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Polymarket Whale Scout")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--top", type=int, default=15, help="Show top N whales (default: 15)")
    parser.add_argument("--scores", action="store_true", help="Print detailed score breakdown")
    parser.add_argument("--telegram", action="store_true", help="Send report to Telegram")
    parser.add_argument(
        "--current", nargs="*", default=[], help="Current whale addresses to compare against"
    )
    args = parser.parse_args()

    scored = score_whales()
    print(f"🎯 {len(scored)} qualified whales found\n", file=sys.stderr)

    to_add, to_remove = recommend_changes(scored, args.current)

    if args.json:
        output = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "whales": scored[: args.top],
            "recommendations": {
                "add": [
                    {"address": w["address"], "name": w["name"], "score": w["composite_score"]}
                    for w in to_add
                ],
                "remove": to_remove,
            },
        }
        print(json.dumps(output, indent=2))
    else:
        report = format_report(scored[: args.top], to_add, to_remove)
        print(report)

        if args.scores:
            print("\n📈 Score Breakdown:")
            for w in scored[: args.top]:
                s = w["scores"]
                print(
                    f"  {w['name']:<24} composite={w['composite_score']:>5.1f}  "
                    f"30d={s['s_30d']:>5.1f} 7d={s['s_7d']:>5.1f} "
                    f"vol={s['s_vol']:>5.1f} cons={s['s_consistency']:>5.1f}"
                )

    # Save to disk
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "whales": scored[:30],
        "recommendations": {
            "add": [{"address": w["address"], "name": w["name"]} for w in to_add],
            "remove": [{"address": w["address"], "name": w["name"]} for w in to_remove],
        },
    }
    OUTPUT_FILE.write_text(json.dumps(output, indent=2))
    with HISTORY_FILE.open("a") as f:
        f.write(
            json.dumps(
                {
                    "timestamp": output["timestamp"],
                    "total_scored": len(scored),
                    "adds": len(to_add),
                    "removes": len(to_remove),
                }
            )
            + "\n"
        )

    if args.telegram and send_telegram(format_telegram(scored, to_add, to_remove)):
        print("\n📱 Sent to Telegram", file=sys.stderr)

    print(f"\n💾 Results: {OUTPUT_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
