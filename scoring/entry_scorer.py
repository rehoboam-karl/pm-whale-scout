"""Entry Scorer — Quantitative analysis before copying a whale trade.

Evaluates whether to copy a whale's entry based on independent signals.
Called by the Rust bot via HTTP (sidecar) or by a Python pre-filter.

Scores (0.0 → 1.0):
  1. Whale Track Record   — historical win rate for this whale
  2. Price Slippage       — how much price moved since whale's fill
  3. Spread Health        — current bid-ask spread (liquidity proxy)
  4. Volume Momentum      — is the market active or dying?
  5. Time Value           — enough time remaining to profit?
  6. Position Crowding    — are multiple whales piling into same side?
  7. Price Zone           — avoid extremes where EV is negative

Final score = weighted combination → COPY / REDUCE / SKIP

Usage:
  score = evaluate_entry(context)
  if score.action == "SKIP":
      don't copy this trade
"""

from dataclasses import dataclass

import requests

# Market type classification
BLOCKED_MARKET_TYPES = {"CRYPTO_BINARY", "OVER_UNDER"}


def classify_market(outcome_title: str) -> str:
    """Classify market type by title."""
    if not outcome_title:
        return "UNKNOWN"
    t = outcome_title.strip().lower()

    # Crypto Binary: contains "up or down", "down", "up" with parentheses or followed by -
    if "up or down" in t or " up " in t or " down " in t or t.endswith((" up", " down")):
        return "CRYPTO_BINARY"
    # Over/Under: "O/U", "Over", "Under"
    if t.startswith(("o/u", "over ", "under ")):
        return "OVER_UNDER"
    # Sports
    if any(
        x in t
        for x in [
            "fc",
            " vs ",
            "united",
            "city",
            "esports",
            "g2",
            "cybershoke",
            "nba",
            "nfl",
            "nhl",
            "mma",
            "ufc",
            "tennis",
            "basketball",
        ]
    ):
        return "SPORTS"
    return "OTHER"


@dataclass
class EntryContext:
    """All inputs needed to score an entry."""

    token_id: str
    whale_address: str
    whale_price: float  # price whale got filled at
    whale_size: float  # shares whale bought
    current_bid: float  # current best bid
    current_ask: float  # current best ask
    book_depth_usd: float  # total $ in top 5 levels of book
    hours_remaining: float | None  # hours until market resolution
    whale_win_rate: float | None  # whale's historical win rate (0-1)
    whale_total_trades: int  # whale's total historical trades
    our_existing_exposure: float  # $ we already have in this token
    max_market_exposure: float  # max $ per market from config
    # Injected by Rust before calling scorer
    current_position_side: str | None = None  # "YES" | "NO" | None
    # Market title for type classification (from cache or Gamma API)
    outcome_title: str | None = None  # e.g. "Bitcoin Up or Down - March 13"
    # Whale concentration tracking (H3)
    whale_total_exposure_usd: float = 0.0  # total $ exposure for this whale across all markets
    total_bot_exposure_usd: float = 0.0  # total $ exposure across all whales


@dataclass
class EntryScore:
    action: str  # COPY | REDUCE | SKIP
    confidence: float  # 0.0 → 1.0
    size_multiplier: float  # 1.0 = full size, 0.5 = half, 0.0 = skip
    reasons: dict  # individual scores
    summary: str  # human-readable explanation


# ─── Individual Scorers ───────────────────────────────────────────────────────


def score_slippage(whale_price: float, current_bid: float, current_ask: float) -> float:
    """How much price moved since whale's fill. Lower = better entry.
    Score 1.0 = no slippage, 0.0 = massive adverse move."""
    if whale_price <= 0 or current_ask <= 0:
        return 0.5

    mid = (current_bid + current_ask) / 2
    # For BUY: if current price > whale price, we're paying more (bad)
    slippage_pct = (mid - whale_price) / whale_price

    if slippage_pct <= 0:
        return 1.0  # Price hasn't moved or dropped (good for us)
    elif slippage_pct < 0.02:
        return 0.9  # <2% slippage, acceptable
    elif slippage_pct < 0.05:
        return 0.7  # 2-5% slippage, concerning
    elif slippage_pct < 0.10:
        return 0.4  # 5-10% slippage, significant cost
    elif slippage_pct < 0.14:
        return 0.1  # 10-14% slippage, very risky
    else:
        return 0.0  # >14% slippage, definitely skip


def score_spread(current_bid: float, current_ask: float) -> float:
    """Spread health — tight spread = liquid market. Score 1.0 = tight.
    Polymarket spreads are wider than traditional exchanges (LMSR/AMM).
    Typical: 3-8% for active markets, 10-20% for less active."""
    if current_bid <= 0 or current_ask <= 0:
        return 0.1  # No book, very risky but not hard block

    spread_pct = (current_ask - current_bid) / current_ask

    if spread_pct < 0.03:
        return 1.0  # <3%, very tight for Polymarket
    elif spread_pct < 0.08:
        return 0.85  # 3-8%, normal/healthy
    elif spread_pct < 0.15:
        return 0.6  # 8-15%, acceptable
    elif spread_pct < 0.25:
        return 0.3  # 15-25%, wide but still tradeable
    elif spread_pct < 0.40:
        return 0.1  # 25-40%, very thin
    else:
        return 0.0  # >40%, dead market
        return 0.0  # >40% spread, market is dead


def score_book_depth(depth_usd: float) -> float:
    """Book depth — can we exit when needed? Score 1.0 = deep book."""
    if depth_usd >= 5000:
        return 1.0
    elif depth_usd >= 1000:
        return 0.8
    elif depth_usd >= 500:
        return 0.6
    elif depth_usd >= 100:
        return 0.3
    elif depth_usd >= 50:
        return 0.15
    else:
        return 0.0


def score_time_value(hours_remaining: float | None) -> float:
    """Is there enough time to profit? Score 1.0 = plenty of time."""
    if hours_remaining is None:
        return 0.7  # Unknown = moderate caution

    if hours_remaining > 168:  # 7+ days
        return 1.0
    elif hours_remaining > 72:  # 3-7 days
        return 0.85
    elif hours_remaining > 24:  # 1-3 days
        return 0.6
    elif hours_remaining > 6:  # 6-24 hours
        return 0.3  # Risky, spread will widen
    elif hours_remaining > 1:
        return 0.1  # Very risky
    else:
        return 0.0  # Don't enter <1h before resolution


def score_price_zone(price: float) -> float:
    """Avoid buying at extremes where EV is negative.
    Score 1.0 = sweet spot, 0.0 = death zone.

    Updated 2026-03-15: Penaliza favoritos claros em Tennis (0.75-0.90) — onde existe edge real."""
    if price <= 0.05:
        return 0.0  # Lottery ticket zone — hard block already covers
    elif price <= 0.15:
        return 0.10  # Almost never worth it
    elif price < 0.52:
        return 0.0  # Hard block covers this, but reinforce
    elif price <= 0.82:
        return 1.0  # Sweet spot — extended
    elif price <= 0.90:
        return 0.85  # Clear favorite — real edge (was 0.5)
    elif price <= 0.95:
        return 0.50  # High but acceptable (was 0.15)
    else:
        return 0.0  # >95%: spread eats profit


def score_whale_track_record(win_rate: float | None, total_trades: int) -> float:
    """Historical whale performance. Score 1.0 = proven winner."""
    if win_rate is None or total_trades < 10:
        return 0.5  # Unknown → neutral

    # Weight by sample size (more trades = more confidence)
    confidence = min(1.0, total_trades / 100)

    if win_rate >= 0.65:
        return 0.6 + 0.4 * confidence  # Good whale, scale with confidence
    elif win_rate >= 0.55:
        return 0.5 + 0.2 * confidence  # Average whale
    elif win_rate >= 0.45:
        return 0.4  # Below average
    else:
        return 0.2  # Bad whale, but still copy (reduced)


def score_crowding(our_existing_exposure: float, max_exposure: float) -> float:
    """Position crowding — are we already overexposed to this market?
    Score 1.0 = no existing position, 0.0 = at max."""
    if max_exposure <= 0:
        return 0.5

    utilization = our_existing_exposure / max_exposure
    if utilization <= 0:
        return 1.0
    elif utilization < 0.25:
        return 0.9
    elif utilization < 0.50:
        return 0.6
    elif utilization < 0.75:
        return 0.3
    elif utilization < 1.0:
        return 0.1
    else:
        return 0.0  # Already at max


# ─── Weights ──────────────────────────────────────────────────────────────────

WEIGHTS = {
    # Updated 2026-03-15: price_zone now rewards clear favorites (0.75-0.90)
    "price_zone": 0.29,  # Favorites have real edge — was 0.15
    "slippage": 0.21,  # Entry timing is critical
    "time_value": 0.20,  # Time decay risk — was 0.15
    "spread": 0.15,  # Liquidity matters for exit
    "whale_record": 0.05,  # Was 0.15 — less important than price/slippage
    "depth": 0.05,  # Was 0.10 — liquidity less critical
    "crowding": 0.05,  # Was 0.10 — concentration less critical
}


# ─── Main Evaluator ──────────────────────────────────────────────────────────


def evaluate_entry(ctx: EntryContext) -> EntryScore:
    """Score a potential entry and decide whether to copy."""

    # ── Position State Check (runs BEFORE any scoring) ──
    whale_side = "YES" if ctx.whale_price >= 0.50 else "NO"

    if ctx.current_position_side is not None:
        if ctx.current_position_side == whale_side:
            # Same side — only enter if there's still space in the cap
            remaining = ctx.max_market_exposure - ctx.our_existing_exposure
            if remaining < 2.50:
                return EntryScore(
                    action="SKIP",
                    confidence=0.95,
                    size_multiplier=0.0,
                    reasons={},
                    summary="Hard block: already have position, exposure near cap",
                )
            # Still has space — reduce size to not duplicate
            return EntryScore(
                action="REDUCE",
                confidence=0.70,
                size_multiplier=0.50,
                reasons={},
                summary="Position exists same side: entering at 50% size",
            )
        else:
            # Opposite side — always block
            return EntryScore(
                action="SKIP",
                confidence=1.0,
                size_multiplier=0.0,
                reasons={},
                summary="Hard block: opposite side position exists",
            )

    # ── Whale Concentration Block (H3) ──
    # Don't put >40% of total exposure in a single whale
    MAX_WHALE_CONCENTRATION = 0.40
    if ctx.total_bot_exposure_usd > 10.0:  # only check once we have meaningful exposure
        whale_concentration = ctx.whale_total_exposure_usd / ctx.total_bot_exposure_usd
        if whale_concentration > MAX_WHALE_CONCENTRATION:
            return EntryScore(
                action="SKIP",
                confidence=0.80,
                size_multiplier=0.0,
                reasons={},
                summary=f"Hard block: whale concentration {whale_concentration:.0%} > {MAX_WHALE_CONCENTRATION:.0%} (${ctx.whale_total_exposure_usd:.0f}/${ctx.total_bot_exposure_usd:.0f})",
            )

    # ── Degen Whale Block ──
    # Whales with proven bad track record (>=15 trades, <45% win rate) should be skipped
    if (
        ctx.whale_win_rate is not None
        and ctx.whale_total_trades >= 15
        and ctx.whale_win_rate < 0.50
    ):
        return EntryScore(
            action="SKIP",
            confidence=0.85,
            size_multiplier=0.0,
            reasons={},
            summary=f"Hard block: degen whale (win_rate={ctx.whale_win_rate:.0%}, n={ctx.whale_total_trades})",
        )

    # ── Maximum Price Filter ──
    # >88%: spread eats all profit, tiny upside
    if ctx.whale_price > 0.88:
        return EntryScore(
            action="SKIP",
            confidence=0.85,
            size_multiplier=0.0,
            reasons={},
            summary="Hard block: price >90%, spread cost exceeds expected profit",
        )

    # ── Minimum Price Filter ──
    # Data shows 0/19 wins below $0.52
    if ctx.whale_price < 0.52:
        return EntryScore(
            action="SKIP",
            confidence=0.90,
            size_multiplier=0.0,
            reasons={},
            summary="Hard block: price below minimum threshold (0.52)",
        )

    # ── Market Type Block ──
    # Block Crypto Binary (Up/Down) and Over/Under markets
    if ctx.outcome_title is not None:
        market_type = classify_market(ctx.outcome_title)
        if market_type in BLOCKED_MARKET_TYPES:
            return EntryScore(
                action="SKIP",
                confidence=0.95,
                size_multiplier=0.0,
                reasons={},
                summary=f"Hard block: market type {market_type} is blocked",
            )

    # ── Spread Cost Model — block negative EV trades ──
    # EV model — block trades where entry cost exceeds realistic upside
    # For binary options: cost = (whale_entry - mid) for entry spread + fees
    mid_market = (
        (ctx.current_bid + ctx.current_ask) / 2.0
        if ctx.current_bid > 0 and ctx.current_ask > 0
        else ctx.whale_price
    )
    entry_spread_pct = max(0.0, ctx.whale_price - mid_market)
    fee_pct = 0.02  # Polymarket fee (round-trip ~2%)
    round_trip_cost = entry_spread_pct + fee_pct
    # EV = whale entry price minus all costs (entry spread + fees)
    # If whale entered at 0.61 with 0.50 mid: cost = 0.11 (entry spread) + 0.02 (fee) = 0.13
    # EV = 0.61 - 0.13 = 0.48 (positive = profitable entry at resolution)
    ev_vs_entry = ctx.whale_price - round_trip_cost
    if ev_vs_entry < 0.01:
        return EntryScore(
            action="SKIP",
            confidence=0.85,
            size_multiplier=0.0,
            reasons={},
            summary=f"Hard block: negative EV (entry={ctx.whale_price:.2f}, mid={mid_market:.2f}, cost={round_trip_cost:.1%}, net={ev_vs_entry:.1%})",
        )

    scores = {
        "slippage": score_slippage(ctx.whale_price, ctx.current_bid, ctx.current_ask),
        "spread": score_spread(ctx.current_bid, ctx.current_ask),
        "depth": score_book_depth(ctx.book_depth_usd),
        "time_value": score_time_value(ctx.hours_remaining),
        "price_zone": score_price_zone(ctx.whale_price),
        "whale_record": score_whale_track_record(ctx.whale_win_rate, ctx.whale_total_trades),
        "crowding": score_crowding(ctx.our_existing_exposure, ctx.max_market_exposure),
    }

    # Weighted score
    total = sum(scores[k] * WEIGHTS[k] for k in WEIGHTS)

    # ── Hard blocks ──
    # Never enter <3h before resolution
    if ctx.hours_remaining is not None and ctx.hours_remaining < 3:
        return EntryScore(
            action="SKIP",
            confidence=1.0,
            size_multiplier=0.0,
            reasons=scores,
            summary="Hard block: <1h to resolution",
        )

    # Never enter with >20% slippage
    if scores["slippage"] == 0.0:
        return EntryScore(
            action="SKIP",
            confidence=0.95,
            size_multiplier=0.0,
            reasons=scores,
            summary="Hard block: >20% slippage since whale fill",
        )

    # Never enter in death zone spread
    if scores["spread"] == 0.0:
        return EntryScore(
            action="SKIP",
            confidence=0.9,
            size_multiplier=0.0,
            reasons=scores,
            summary="Hard block: spread >20%, market is illiquid",
        )

    # Thin book: can't exit position — "dying market"
    if scores["depth"] < 0.6:  # book_depth < $500
        return EntryScore(
            action="SKIP",
            confidence=0.82,
            size_multiplier=0.0,
            reasons=scores,
            summary="Hard block: book depth <$500, can't exit position safely",
        )

    # Already at max exposure
    if scores["crowding"] == 0.0:
        return EntryScore(
            action="SKIP",
            confidence=0.85,
            size_multiplier=0.0,
            reasons=scores,
            summary="Hard block: already at max exposure for this market",
        )

    # ── Action mapping ──
    if total >= 0.65:
        return EntryScore(
            action="COPY",
            confidence=total,
            size_multiplier=1.0,
            reasons=scores,
            summary=f"Strong entry (score={total:.2f}): good slippage, healthy spread, time OK",
        )
    elif total >= 0.50:
        # Reduce size proportionally
        mult = 0.3 + (total - 0.50) / 0.20 * 0.7  # 0.3 → 1.0 across 0.50-0.70
        return EntryScore(
            action="REDUCE",
            confidence=total,
            size_multiplier=round(mult, 2),
            reasons=scores,
            summary=f"Marginal entry (score={total:.2f}): copying at {mult:.0%} size",
        )
    elif total >= 0.35:
        return EntryScore(
            action="REDUCE",
            confidence=total,
            size_multiplier=0.25,
            reasons=scores,
            summary=f"Weak entry (score={total:.2f}): copying at 25% size",
        )
    else:
        return EntryScore(
            action="SKIP",
            confidence=1.0 - total,
            size_multiplier=0.0,
            reasons=scores,
            summary=f"Skip entry (score={total:.2f}): poor conditions",
        )


# ─── Helper: Build context from market data ──────────────────────────────────


def build_context_from_api(
    token_id: str,
    whale_address: str,
    whale_price: float,
    whale_size: float,
    hours_remaining: float | None = None,
    outcome_title: str | None = None,
    whale_win_rate: float | None = None,
    whale_total_trades: int = 0,
    our_exposure: float = 0,
    max_exposure: float = 60,
) -> EntryContext | None:
    """Fetch book data from CLOB API and build EntryContext."""
    try:
        resp = requests.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=3,
        )
        if not resp.ok:
            return None
        book = resp.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 0

        # Calculate depth (top 5 levels)
        depth = 0
        for level in bids[:5]:
            depth += float(level.get("size", 0)) * float(level.get("price", 0))
        for level in asks[:5]:
            depth += float(level.get("size", 0)) * float(level.get("price", 0))

        return EntryContext(
            token_id=token_id,
            whale_address=whale_address,
            whale_price=whale_price,
            whale_size=whale_size,
            current_bid=best_bid,
            current_ask=best_ask,
            book_depth_usd=depth,
            hours_remaining=hours_remaining,
            outcome_title=outcome_title,
            whale_win_rate=whale_win_rate,
            whale_total_trades=whale_total_trades,
            our_existing_exposure=our_exposure,
            max_market_exposure=max_exposure,
        )
    except (ValueError, OSError):
        return None


# ─── CLI Test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    # Simulate a few scenarios
    scenarios = [
        {
            "name": "Good entry: fresh market, no slippage",
            "ctx": EntryContext(
                token_id="test",
                whale_address="whale1",
                whale_price=0.45,
                whale_size=100,
                current_bid=0.44,
                current_ask=0.46,
                book_depth_usd=2000,
                hours_remaining=120,
                whale_win_rate=0.62,
                whale_total_trades=80,
                our_existing_exposure=0,
                max_market_exposure=60,
            ),
        },
        {
            "name": "Bad entry: massive slippage, thin book",
            "ctx": EntryContext(
                token_id="test",
                whale_address="whale1",
                whale_price=0.28,
                whale_size=50,
                current_bid=0.42,
                current_ask=0.48,
                book_depth_usd=30,
                hours_remaining=4,
                whale_win_rate=0.40,
                whale_total_trades=20,
                our_existing_exposure=40,
                max_market_exposure=60,
            ),
        },
        {
            "name": "Edge case: high price zone, decent otherwise",
            "ctx": EntryContext(
                token_id="test",
                whale_address="whale1",
                whale_price=0.88,
                whale_size=200,
                current_bid=0.87,
                current_ask=0.90,
                book_depth_usd=5000,
                hours_remaining=72,
                whale_win_rate=0.70,
                whale_total_trades=150,
                our_existing_exposure=0,
                max_market_exposure=60,
            ),
        },
    ]

    for s in scenarios:
        result = evaluate_entry(s["ctx"])
        print(f"\n{'=' * 60}")
        print(f"📊 {s['name']}")
        print(
            f"   Action: {result.action} | Confidence: {result.confidence:.2f} | Size: {result.size_multiplier:.0%}"
        )
        print(f"   {result.summary}")
        print(f"   Scores: {json.dumps({k: round(v, 2) for k, v in result.reasons.items()})}")
