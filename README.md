# pm-whale-scout

> Discover and score profitable Polymarket traders using the public Leaderboard API.

[![CI](https://github.com/rehoboam-karl/pm-whale-scout/actions/workflows/ci.yml/badge.svg)](https://github.com/rehoboam-karl/pm-whale-scout/actions)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

`pm-whale-scout` analyzes Polymarket's public leaderboards to identify
consistently profitable, high-volume traders — then scores and ranks them
for use in copy-trading bots.

**No API keys required** — uses the public Polymarket Leaderboard API.

---

## Install

```bash
pip install -e .
```

Or with uv:

```bash
uv sync
```

## Usage

```bash
# Analyze and print report
python whale_scout.py

# JSON output (for automation)
python whale_scout.py --json

# Show top 10
python whale_scout.py --top 10

# Detailed score breakdown
python whale_scout.py --scores

# Send report to Telegram
TELEGRAM_BOT_TOKEN="..." TELEGRAM_CHAT_ID="..." python whale_scout.py --telegram
```

## Live Output

```
$ python whale_scout.py --top 10
📡 Fetching Polymarket Leaderboard API v2...
  month: 600 | week: 600 | volume: 600
  424 unique traders found
🎯 49 qualified whales found

🐋 WHALE SCOUT — Polymarket Leaderboard Analysis
📅 2026-10-18 16:07 UTC
=================================================================

📊 Top 10 Whale Candidates:
 # Name                     Score      30d P&L     7d P&L         Volume
------------------------------------------------------------------------
 1 joseph00677               83.9 $  5,592,482 $4,010,924 $   39,443,463
 2 BreakTheBank              75.7 $  5,368,753 $2,957,285 $   52,195,013
 3 Diabolical-Prize          50.2 $  2,835,815 $1,959,470 $   53,130,876
 4 UpTheBlues                36.3 $  1,583,754 $  162,126 $  138,579,555
 5 HomeRunHazard             35.7 $  1,422,467 $  566,679 $  113,041,637

🟢 Add (15):
  ✅ joseph00677              score: 83.9 | 30d: $5,592,482 | 7d: $4,010,924
  ✅ BreakTheBank             score: 75.7 | 30d: $5,368,753 | 7d: $2,957,285
```

## Scoring Model

Each trader gets a **composite score (0–100)** weighted across 5 dimensions:

| Weight | Dimension | What it measures |
|--------|-----------|-----------------|
| 40% | 30d Profit | Recent consistent profitability |
| 30% | 7d Profit | Short-term momentum |
| 20% | Volume | Activity level |
| 10% | Consistency | Present in both 7d and 30d windows |

**Minimum thresholds to qualify:**
- 30d profit ≥ $500
- Total volume ≥ $10,000
- 7d profit ≥ $0 (no recent losses)

## Programmatic Usage

```python
from whale_scout import score_whales, recommend_changes

# Get scored whales
scored = score_whales()
print(f"Found {len(scored)} qualified traders")
print(f"Top whale: {scored[0]['name']} — score {scored[0]['composite_score']}")

# Compare against your current whale list
to_add, to_remove = recommend_changes(scored, current_whales=["0xabc...", "0xdef..."])
print(f"Should add: {len(to_add)}, remove: {len(to_remove)}")
```

## Output

```
🐋 WHALE SCOUT — Polymarket Leaderboard Analysis
📅 2026-10-18 14:00 UTC
=================================================================

📊 Top 15 Whale Candidates:
 # Name                     Score    30d P&L      7d P&L        Volume
------------------------------------------------------------------------
 1 trader_name_here           87.3   $12,450     $3,200     $1,234,500
 2 another_trader             81.1    $8,900      $1,100       $890,000
...

🟢 Add (2):
  ✅ whale_name     score: 55.3 | 30d: $2,100 | 7d: $800
```

## Scoring Modules

The `scoring/` directory contains reusable scoring implementations:

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

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LB_API` | `https://data-api.polymarket.com` | Polymarket Data API v2 base |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID |
| `OUTPUT_DIR` | `./output` | Where to write results |

## License

MIT — see [LICENSE](LICENSE).
