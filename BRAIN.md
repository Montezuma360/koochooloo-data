# Koochooloo brain: what is in this repository

Everything here is free public data, refreshed automatically by GitHub Actions. Virtual money only; nothing is advice.

## Start here: `data/brain.json`
One consolidated file (about 150 KB), rebuilt after every refresh by `brain.py`. The terminal and Setareh read this first.

| Section | What it holds |
|---|---|
| `summary` | Four plain-language lines: market regime, fear & greed, research-backed setups, warnings |
| `regime` | risk-on / caution / risk-off and why (S&P vs its 200-day average, stress switch, Fear & Greed, VIX, credit spread) with the rules |
| `stocks` | Every stock with evidence today, best first: price, 1-day/1-month change, RSI and the 30-70 rule, 200-day trend, size (average $ traded a day), cash flow vs profit, and each piece of evidence labelled `edge`, `no edge`, `untested`, `warning` or `event` |
| `evidence_notes` | What our research found about each kind of evidence |
| `markets` | Indices, sectors, 20 futures, 8 FX pairs, dollar, yields, movers, Treasury curve |
| `macro` | US (FRED), UK, euro area, next high-impact events, CFTC positioning, market short ratio |
| `crypto` | Market, top coins, trending, sectors, funding rates, crypto fear & greed, Bitcoin network |
| `news`, `sec`, `options`, `fear_greed`, `sentiment` | Latest headlines, SEC red flags and activist stakes, put/call and unusual options, our Fear & Greed index, StockTwits and Reddit |
| `paper` | Paper-trading results vs the S&P 500, the GBP 100 to GBP 10,000 tracker, latest TRADE / NO_TRADE decisions |
| `health` | Age and status of every feed, warnings, repository size |

The one research-backed setup (`research_setup: true`): a smaller company ($1M-$20M traded a day) where 2 or more officers or
directors each bought $25k+ within 30 days and operating cash flow is above reported profit. In tests it averaged about +2.4%
(2014-2022) and +2.7% (2023-2026) per trade above the S&P 500 over 60 days. It is not a guarantee; many trades still lose.

## Refresh
- `live.yml`: one run per tick (an outside timer calls it every 5 minutes in US market hours): prices, SEC insider filings,
  Reddit, intelligence feeds, signals, health, brain, one commit. About 1-2 minutes per run.
- `refresh.yml`, `sec.yml`, `signals.yml`: GitHub's own schedules, as a backup and for the hours outside the market.
- `alt.yml` nightly (short volume, CFTC, FRED, contracts, fundamentals, 13F), `paper.yml` after the close,
  `backfill.yml` nightly (reconciles insider filings from EDGAR's daily index).

## Files
| Path | Built by | Contents |
|---|---|---|
| `data/quotes.json`, `intraday_5m.json`, `prices_daily.json`, `gainers.json`, `losers.json`, `most_active.json`, `sector_board.json`, `treasury_rates.json` | `refresh.py` | 55 symbols (indices, sectors, macro ETFs, futures, FX, crypto, megacaps) |
| `data/insider_trades.json`, `insider_clusters.json` | `refresh_sec.py` (+ `backfill_insiders.py`) | SEC Form 4 open-market buys and sells, 90 days, unique by filing and trade |
| `data/congress_trades.json`, `congress_clusters.json`, `signals.json`, `dashboard.json`, `data/dash/` | `refresh_signals.py` | Congress disclosures, the terminal's signal tally, the dashboard |
| `data/intel/` | `refresh_intel.py` | Crypto, on-chain, futures/FX, macro (BLS, ONS, BoE, ECB, Eurostat), calendar, news headlines, SEC 8-K/10-K/10-Q/13D, options, Fear & Greed, StockTwits, Reddit forums |
| `data/alt/` | `refresh_alt.py` | FINRA short volume, fails-to-deliver, CFTC, FRED, committees, federal contracts, Reddit, fundamentals, 13F |
| `data/paper/` | `paper.py` | Paper trading: state, summary, trades, decisions |
| `data/health.json` | `health.py` | Freshness and failures of every feed |
| `data/history/` | all | Permanent daily history (the brain's long-term memory) |
| `data/research/` | `research/*.py` | Backtest results |

Licence notes: Yahoo, Cboe, FINRA, StockTwits and ApeWisdom data are for personal research; check their terms before charging.
News is stored as headline and link only.
