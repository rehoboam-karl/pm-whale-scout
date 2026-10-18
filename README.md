# pm-whale-scout

> Discover and score profitable Polymarket traders using the public Leaderboard API.

`pm-whale-scout` analyzes Polymarket's leaderboards to identify consistently profitable,
high-volume traders — then scores and ranks them for use in copy-trading bots.

**What it does:**
- Fetches 5 leaderboard windows simultaneously (profit: 1d/7d/30d/all, volume)
- Scores each trader on 5 dimensions: recent profit, 7d momentum, lifetime profit, volume, consistency
- Recommends add/remove changes vs your current whale list
- Sends reports to Telegram (optional)

**No API keys required** — uses the public Polymarket Leaderboard API.

---

## Quick Start

```bash
pip install requests
python whale_scout.py
```

Or with uv:

```bash
uv run whale_scout.py
```

## Configuration

```bash
export LEADERBOARD_API="https://lb-api.polymarket.com"  # default
export TELEGRAM_BOT_TOKEN="your:bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"
export OUTPUT_DIR="./output"
```

## Usage

```bash
# Default: analyze and print report
python whale_scout.py

# JSON output (for automation / scripts)
python whale_scout.py --json

# Show top 10 only
python whale_scout.py --top 10

# Detailed score breakdown
python whale_scout.py --scores

# Send to Telegram
python whale_scout.py --telegram

# Compare against your current whale list
python whale_scout.py --current 0xabc 0xdef 0x123
```

## Scoring Model

Each trader gets a weighted composite score (0–100):

| Weight | Dimension | What it measures |
|--------|-----------|-----------------|
| 35% | 30d Profit | Recent consistent profitability |
| 25% | 7d Profit | Short-term momentum |
| 20% | Volume | Activity level |
| 10% | Lifetime Profit | Track record |
| 10% | Consistency | Present across time windows |

A trader must meet minimum thresholds to qualify:
- 30d profit ≥ $500
- Total volume ≥ $10,000
- 7d profit ≥ $0 (no recent losses)

## Scoring Module

The `scoring/` directory contains entry scoring implementations:

- `bayesian_scorer.py` — Bayesian position scorer with adaptive exit thresholds
- `entry_scorer.py` — Feature-based ML entry scoring

These can be used independently of the whale discovery layer.

## Architecture

```
whale_scout.py        ← CLI + orchestration
scoring/
  bayesian_scorer.py  ← Exit strategy scoring
  entry_scorer.py     ← Entry signal scoring
```

## License

MIT — see [LICENSE](LICENSE).
