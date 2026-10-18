# Scoring Model

## Overview

The whale scoring model assigns each Polymarket trader a **composite score (0–100)**
based on 5 weighted dimensions derived from the public Leaderboard API.

## Dimensions

### 1. 30-Day Profit (35%)

The most important signal. A trader who made $5,000 in the last 30 days
is more valuable to follow than one who made $500.

The raw 30d profit is normalized to a 0–100 score using the **maximum ratio method**:

```
score_30d = min(100, (profit_30d / max_profit_in_dataset) * 100)
```

This means the top trader in the fetched dataset gets 100, everyone else is
proportional.

### 2. 7-Day Profit (25%)

Very recent momentum. A trader who made $1,000 in the last 7 days but lost
$3,000 in the prior 23 days may be on a hot streak — or may be on a
losing streak. This dimension captures the difference.

Traders with **negative 7d profit are excluded entirely**.

### 3. Volume (20%)

Activity level. A profitable trader who only traded once is less reliable
than one with 100 trades. Volume normalizes by the max volume in the dataset.

### 4. Lifetime Profit (10%)

Track record. A trader with $50k lifetime profit and solid 30d numbers
is more trustworthy than a new trader with a lucky month.

### 5. Consistency (10%)

How many time windows does this trader appear in?

```
windows_present = sum(1 for window in [all, 30d, 7d, 1d] if trader appears)
score = (windows_present / 4) * 100
```

A trader in all 4 windows is more consistently active than one who only
appears in "all time."

## Filters (before scoring)

These are **hard gates** — traders who don't pass are excluded before scoring:

| Filter | Threshold | Reason |
|--------|-----------|--------|
| MIN_PROFIT_30D | $500 | Must be meaningfully profitable |
| MIN_VOLUME | $10,000 | Must be actively trading |
| 7d profit | > $0 | No recent losers |

## Blacklist

Known market makers, bots, and your own wallets are excluded from results:

```python
BLACKLIST = {
    "0x...",  # Your Safe wallet
    "0x...",  # Your EOA
    "0x...",  # Known market maker
}
```

## Why Percentile Scores?

Using raw dollar amounts would make the score dominated by whale traders
with massive volumes. Percentile normalization makes the score **relative** —
a $500 profit from a small trader and a $5,000 profit from a large trader
can both score well on their own scale.

## Customizing Weights

Edit the constants at the top of `whale_scout.py`:

```python
W_PROFIT_30D = 0.35    # Increase for faster-moving strategies
W_PROFIT_7D  = 0.25    # Increase for momentum-focused strategies
W_PROFIT_ALL  = 0.10   # Decrease for new traders
W_VOLUME      = 0.20    # Increase for activity-focused strategies
W_CONSISTENCY = 0.10   # Increase for steady traders
```

## Future improvements

- **Sharpe ratio**: risk-adjusted returns instead of raw P&L
- **Drawdown tracking**: penalize traders with large peak-to-trough losses
- **Direction betting**: penalize traders who bet on both sides of the same market
- **Cross-market correlation**: avoid following multiple whales who trade the same markets
