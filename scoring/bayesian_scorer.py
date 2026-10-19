"""Bayesian Position Scorer — Dynamic exit thresholds based on evidence.

Replaces fixed TP/SL/trailing with adaptive scoring using:
1. Whale Conviction Score (how much whale still believes)
2. Time Decay Pressure (urgency from market resolution)
3. Liquidity Risk Score (spread risk at price extremes)
4. Posterior Exit Probability (Bayesian weighted combination)

Weights calibrated for Polymarket copy-trade:
  whale: 0.50 | time: 0.30 | liquidity: 0.20
"""

from dataclasses import dataclass
from enum import Enum


class Action(str, Enum):
    HOLD = "HOLD"
    TIGHTEN_TRAILING = "TIGHTEN_TRAILING"
    PARTIAL_SELL_25 = "PARTIAL_SELL_25"
    PARTIAL_SELL_50 = "PARTIAL_SELL_50"
    SELL_ALL = "SELL_ALL"


class HardRule(str, Enum):
    WHALE_EXIT = "WHALE_EXIT"
    TIME_CRITICAL = "TIME_CRITICAL"
    LIQUIDITY_EXTREME = "LIQUIDITY_EXTREME"
    TRAILING_TRIGGERED = "TRAILING_TRIGGERED"
    SPIKE_PROFIT_80 = "SPIKE_PROFIT_80"
    SPIKE_PROFIT_50 = "SPIKE_PROFIT_50"
    SPIKE_PROFIT_30 = "SPIKE_PROFIT_30"


@dataclass
class PositionContext:
    position_entry_price: float
    current_price: float
    whale_current_shares: float
    whale_entry_shares: float
    hours_remaining: float | None  # None if no end date
    high_water_mark: float
    trailing_stop_pct: float = 15.0  # current trailing %
    original_tp_price: float | None = None


@dataclass
class ScorerReason:
    whale_conviction_score: float
    time_decay_pressure: float
    liquidity_risk_score: float
    hours_remaining: float | None
    whale_sold_pct: float
    current_gain_pct: float


@dataclass
class ScorerOutput:
    action: Action
    p_exit: float
    sell_percentage: float
    new_trailing_stop_pct: float
    new_stop_loss_price: float
    new_take_profit_price: float
    reason: ScorerReason
    hard_rule_triggered: HardRule | None = None

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "p_exit": round(self.p_exit, 4),
            "sell_percentage": round(self.sell_percentage, 1),
            "new_trailing_stop_pct": round(self.new_trailing_stop_pct, 1),
            "new_stop_loss_price": round(self.new_stop_loss_price, 4),
            "new_take_profit_price": round(self.new_take_profit_price, 4),
            "reason": {
                "whale_conviction_score": round(self.reason.whale_conviction_score, 4),
                "time_decay_pressure": round(self.reason.time_decay_pressure, 4),
                "liquidity_risk_score": round(self.reason.liquidity_risk_score, 4),
                "hours_remaining": round(self.reason.hours_remaining, 1)
                if self.reason.hours_remaining is not None
                else None,
                "whale_sold_pct": round(self.reason.whale_sold_pct, 4),
                "current_gain_pct": round(self.reason.current_gain_pct, 4),
            },
            "hard_rule_triggered": self.hard_rule_triggered.value
            if self.hard_rule_triggered
            else None,
        }


# ─── Scoring Functions ───────────────────────────────────────────────────────


def whale_conviction_score(whale_sold_pct: float) -> float:
    """How much the whale still believes (1.0 = max conviction)."""
    if whale_sold_pct <= 0.0:
        return 1.0
    elif whale_sold_pct <= 0.10:
        return 0.85
    elif whale_sold_pct <= 0.25:
        return 0.60
    elif whale_sold_pct <= 0.50:
        return 0.30
    else:
        return 0.05


def time_decay_pressure(hours_remaining: float | None) -> float:
    """Urgency of exit based on time to resolution (0.0 = no pressure)."""
    if hours_remaining is None:
        return 0.0  # No end date known, no pressure
    if hours_remaining > 168:
        return 0.0
    elif hours_remaining > 72:
        return 0.2
    elif hours_remaining > 24:
        return 0.5
    elif hours_remaining > 6:
        return 0.8
    else:
        return 1.0


def liquidity_risk_score(price: float) -> float:
    """Spread risk at price extremes (LMSR softmax)."""
    if price <= 0.10:
        return 1.0
    elif price <= 0.20:
        return 0.7
    elif price <= 0.80:
        return 0.0
    elif price <= 0.90:
        return 0.5
    else:
        return 0.9


def dynamic_tp_multiplier(current_gain_pct: float) -> float:
    """Adjust take-profit based on how much we've already gained."""
    if current_gain_pct < 0.20:
        return 1.0
    elif current_gain_pct < 0.40:
        return 1.15
    elif current_gain_pct < 0.60:
        return 1.25
    else:
        return 0.80  # already very profitable, protect


def check_spike_profit(ctx: PositionContext, current_gain_pct: float) -> ScorerOutput | None:
    """Check for spike profit - runs BEFORE Bayesian scoring.

    Spike profit rules:
    - If gain > 80%: SELL_ALL (spike before event, realize everything)
    - If gain > 50%: PARTIAL_SELL_50 + move SL to +20%
    - If gain > 30%: TIGHTEN_TRAILING to 8%

    This catches liquidity spikes where a single large order moves the price
    dramatically before the event, making the position show unreal gains that
    will likely disappear when liquidity normalizes.
    """
    reason = ScorerReason(
        whale_conviction_score=0.0,
        time_decay_pressure=0.0,
        liquidity_risk_score=0.0,
        hours_remaining=ctx.hours_remaining,
        whale_sold_pct=0.0,
        current_gain_pct=current_gain_pct,
    )

    if current_gain_pct > 0.80:
        # Spike of 80%+ before event - impossible fundamentals, sell everything
        return ScorerOutput(
            action=Action.SELL_ALL,
            p_exit=1.0,
            sell_percentage=100,
            new_trailing_stop_pct=0,
            new_stop_loss_price=ctx.current_price,
            new_take_profit_price=ctx.current_price,
            reason=reason,
            hard_rule_triggered=HardRule.SPIKE_PROFIT_80,
        )

    if current_gain_pct > 0.50:
        # Spike of 50-80% - sell 50%, set hard stop at breakeven + 20%
        return ScorerOutput(
            action=Action.PARTIAL_SELL_50,
            p_exit=1.0,
            sell_percentage=50,
            new_trailing_stop_pct=0,
            new_stop_loss_price=ctx.position_entry_price * 1.20,  # +20%
            new_take_profit_price=ctx.current_price,
            reason=reason,
            hard_rule_triggered=HardRule.SPIKE_PROFIT_50,
        )

    if current_gain_pct > 0.30:
        # Spike of 30-50% - tighten trailing to 8%
        return ScorerOutput(
            action=Action.TIGHTEN_TRAILING,
            p_exit=0.8,
            sell_percentage=0,
            new_trailing_stop_pct=8.0,
            new_stop_loss_price=ctx.position_entry_price * 1.10,  # +10%
            new_take_profit_price=ctx.position_entry_price * 1.50,
            reason=reason,
            hard_rule_triggered=HardRule.SPIKE_PROFIT_30,
        )

    return None  # No spike, continue to Bayesian scoring


# ─── Main Scorer ──────────────────────────────────────────────────────────────

W_WHALE = 0.50
W_TIME = 0.30
W_LIQUIDITY = 0.20


def score_position(ctx: PositionContext) -> ScorerOutput:
    """Score a position and return recommended action with dynamic thresholds."""

    entry = ctx.position_entry_price
    cur = ctx.current_price
    current_gain_pct = (cur - entry) / entry if entry > 0 else 0

    # ── Spike Profit Check (runs FIRST, before any other logic) ──
    spike_decision = check_spike_profit(ctx, current_gain_pct)
    if spike_decision is not None:
        return spike_decision

    # Whale conviction
    whale_sold_pct = 0.0
    if ctx.whale_entry_shares > 0:
        whale_sold_pct = max(
            0, (ctx.whale_entry_shares - ctx.whale_current_shares) / ctx.whale_entry_shares
        )
    wcs = whale_conviction_score(whale_sold_pct)

    # Time decay
    tdp = time_decay_pressure(ctx.hours_remaining)

    # Liquidity risk
    lrs = liquidity_risk_score(cur)

    reason = ScorerReason(
        whale_conviction_score=wcs,
        time_decay_pressure=tdp,
        liquidity_risk_score=lrs,
        hours_remaining=ctx.hours_remaining,
        whale_sold_pct=whale_sold_pct,
        current_gain_pct=current_gain_pct,
    )

    # ── Hard rules (short-circuit) ──

    # Combination: loss + whale reducing = immediate exit
    # This is MORE dangerous than either factor alone
    if current_gain_pct < -0.25 and whale_sold_pct > 0.20:
        return ScorerOutput(
            action=Action.SELL_ALL,
            p_exit=1.0,
            sell_percentage=100,
            new_trailing_stop_pct=0,
            new_stop_loss_price=cur,
            new_take_profit_price=cur,
            reason=reason,
            hard_rule_triggered=HardRule.WHALE_EXIT,
        )

    # Catastrophic loss: hard stop (check FIRST — more severe)
    if current_gain_pct <= -0.35:
        return ScorerOutput(
            action=Action.SELL_ALL,
            p_exit=1.0,
            sell_percentage=100,
            new_trailing_stop_pct=0,
            new_stop_loss_price=cur,
            new_take_profit_price=cur,
            reason=reason,
        )

    # Significant loss: tighten trailing to protect remaining capital
    if current_gain_pct <= -0.30:
        return ScorerOutput(
            action=Action.TIGHTEN_TRAILING,
            p_exit=0.7,
            sell_percentage=0,
            new_trailing_stop_pct=5.0,  # very tight trailing
            new_stop_loss_price=cur * 0.95,  # hard SL 5% below current
            new_take_profit_price=entry,  # TP at breakeven
            reason=reason,
        )

    if whale_sold_pct > 0.50:
        return ScorerOutput(
            action=Action.SELL_ALL,
            p_exit=1.0,
            sell_percentage=100,
            new_trailing_stop_pct=0,
            new_stop_loss_price=cur,
            new_take_profit_price=cur,
            reason=reason,
            hard_rule_triggered=HardRule.WHALE_EXIT,
        )

    if ctx.hours_remaining is not None and ctx.hours_remaining < 6 and cur > entry:
        return ScorerOutput(
            action=Action.SELL_ALL,
            p_exit=1.0,
            sell_percentage=100,
            new_trailing_stop_pct=0,
            new_stop_loss_price=cur,
            new_take_profit_price=cur,
            reason=reason,
            hard_rule_triggered=HardRule.TIME_CRITICAL,
        )

    if cur > 0.91 or cur < 0.09:
        return ScorerOutput(
            action=Action.SELL_ALL,
            p_exit=1.0,
            sell_percentage=100,
            new_trailing_stop_pct=0,
            new_stop_loss_price=cur,
            new_take_profit_price=cur,
            reason=reason,
            hard_rule_triggered=HardRule.LIQUIDITY_EXTREME,
        )

    # Trailing stop check
    if ctx.high_water_mark > 0:
        trail_price = ctx.high_water_mark * (1 - ctx.trailing_stop_pct / 100)
        if cur < trail_price and current_gain_pct > 0:
            return ScorerOutput(
                action=Action.SELL_ALL,
                p_exit=1.0,
                sell_percentage=100,
                new_trailing_stop_pct=0,
                new_stop_loss_price=cur,
                new_take_profit_price=cur,
                reason=reason,
                hard_rule_triggered=HardRule.TRAILING_TRIGGERED,
            )

    # ── Bayesian posterior ──
    wcs_inverted = 1.0 - wcs
    p_exit = (wcs_inverted * W_WHALE) + (tdp * W_TIME) + (lrs * W_LIQUIDITY)

    # ── Dynamic TP ──
    tp_mult = dynamic_tp_multiplier(current_gain_pct)
    original_tp = ctx.original_tp_price or (entry * 1.50)  # default +50%
    adjusted_tp = original_tp * tp_mult

    # ── Action mapping ──
    if p_exit < 0.20:
        return ScorerOutput(
            action=Action.HOLD,
            p_exit=p_exit,
            sell_percentage=0,
            new_trailing_stop_pct=ctx.trailing_stop_pct,
            new_stop_loss_price=entry * (1 - 0.40),  # -40% SL
            new_take_profit_price=adjusted_tp,
            reason=reason,
        )
    elif p_exit < 0.40:
        return ScorerOutput(
            action=Action.TIGHTEN_TRAILING,
            p_exit=p_exit,
            sell_percentage=0,
            new_trailing_stop_pct=8.0,  # tighten from 15% to 8%
            new_stop_loss_price=entry * (1 - 0.40),
            new_take_profit_price=adjusted_tp,
            reason=reason,
        )
    elif p_exit < 0.60:
        return ScorerOutput(
            action=Action.PARTIAL_SELL_25,
            p_exit=p_exit,
            sell_percentage=25,
            new_trailing_stop_pct=8.0,
            new_stop_loss_price=entry,  # breakeven
            new_take_profit_price=adjusted_tp,
            reason=reason,
        )
    elif p_exit < 0.80:
        return ScorerOutput(
            action=Action.PARTIAL_SELL_50,
            p_exit=p_exit,
            sell_percentage=50,
            new_trailing_stop_pct=5.0,
            new_stop_loss_price=entry * 1.05,  # entry + 5%
            new_take_profit_price=adjusted_tp,
            reason=reason,
        )
    else:
        return ScorerOutput(
            action=Action.SELL_ALL,
            p_exit=p_exit,
            sell_percentage=100,
            new_trailing_stop_pct=0,
            new_stop_loss_price=cur,
            new_take_profit_price=cur,
            reason=reason,
        )
