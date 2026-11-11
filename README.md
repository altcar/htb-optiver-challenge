# HTB × Optiver Challenge

Strategy work from the **Hack The Box × Optiver** trading hackathon, written in Python against
**Optibook** — the event's simulated exchange, where every stock is listed twice (a primary
`STOCK` book and a `STOCK_DUAL` book) and the job is to make markets, hedge the pair, and take
arbitrage before the basis closes.

The hosted venue and its `optibook.synchronous_client` connector were retired after the event, so the
Python here is now reference material. Every strategy family in this repo has been ported to
TypeScript and runs live against a self-contained synthetic venue:

> **Live arena:** <https://optibook-arena.hheylau.workers.dev>
> **Port source:** [altcar/optibook-arena](https://github.com/altcar/optibook-arena)

**All six strategy families are ported to TypeScript and enabled by default:**

- **Dual Maker** — `arbStrat1` / `arbStrat2`, `final_solution`
- **Pair Hedger** — `hedge_strategy.py`
- **Dual Arbitrage** — `strat_worky`, `arbStrat4`
- **News Sentiment** — `arbStrat2sentiment`, `arbStrat3`
- **Momentum Maker** — `hugoarbones/momentum.py`
- **Adaptive Maker** — `final_solution/controller.py`

---

## The problem

Optibook quotes two books for each name, a primary and a dual listing of the same underlying. Between
them sits a basis that drifts, mean-reverts, and occasionally jumps — which is both the arb and the
risk. On top of that the venue publishes a news wire, and positions are capped per instrument.

So there are three ways to make money, and the strategies below use all three:

1. **Quote the spread** on the primary and stay flat by trading the dual.
2. **Take the basis** when `stock mid − dual mid` clears costs, buying the cheap leg and selling the rich one.
3. **Trade the reaction** when a headline moves fair value faster than the book does.

## Strategy families

| Family | Original code | What it does |
| --- | --- | --- |
| **Dual Maker** | `pregame/arbStrat1`, `pregame/arbStrat2`, `endgame/final_solution` | Quotes the stock around a blended stock/dual mid, skews with inventory, cancels and replaces on drift. |
| **Pair Hedger** | `pregame/arbStrat2/hedge_strategy.py` | Tracks net delta across each stock and its dual, then flattens the pair with IOC orders once the hedge band breaks. |
| **Dual Arbitrage** | `endgame/strat_worky/main.py`, `endgame/arbStrat4/hedge_arbitrage.py` | Buys the cheap leg and sells the rich leg when the basis clears the configured edge. |
| **News Sentiment** | `endgame/arbStrat2sentiment`, `endgame/arbStrat3` | Scores headlines with a local DistilBERT model, shifts fair value per ticker, leans quotes and takes liquidity on strong news. |
| **Momentum Maker** | `endgame/hugoarbones/momentum.py` | Adds a short-horizon momentum bias to the mid, widens the spread with realised volatility, scales size with inventory risk. |
| **Adaptive Maker** | `endgame/final_solution/controller.py` | `tanh` inventory skew, a spread that tracks live market conditions, and batched hedging above a delta threshold. |

The repo also contains the experiments that led there — reconnect handling, edge detection, a raw
WebSocket replicator, MPC and transformer notebooks, and a C++ Optibook client. Most of these are
near-duplicates of each other; the table above is the distilled version.

## Repository layout

```
pregame/                      practice-round strategies
  arbStrat1/                  dual-priced market making, no hedging
  arbStrat2/                  market making + hedge_strategy
  hugoarbones/                early momentum / ML experiments
  your_optiver_workspace/     event scaffold: notebooks, template, data pulls
endgame/                      live-round strategies
  arbStrat1/  arbStrat2/      promoted versions of the pregame code
  arbStrat2sentiment/         sentiment model integrated into the maker
  arbStrat3/                  news + edge controller, raw Optibook WebSocket client
  arbStrat4/                  market making + arbitrage
  final_solution/             the adaptive controller that ran at the end
  strat_worky/                standalone dual-arbitrage loop
  hugoarbones/                momentum maker, C++ client, research scripts
  hi/                         MPC / transformer side experiments
news_feed.py  news_reaction.py  new_scaper.py
websocket_try.py  clas_trade.py  example_news_trade.py
```

Supporting material:

- `REPRODUCE_LARGE_FILES.md` — what the multi-hundred-megabyte blobs are and whether they can be rebuilt.
- `endgame/data/trades_checkpoint_*.csv|json` — trade logs captured during the live round.
- `strat.txt`, `HTB optiver notes.txt`, `HTB optiver news.html` — scratch notes and venue notes.
- `*.db` — SQLite captures of market data, orders and replication state.

## Running the original Python

It will not run as-is. Every controller imports the event connector:

```python
from optibook.synchronous_client import Exchange
```

and that package, the venue URL, and the LZ4-framed MessagePack feed are all gone. The code is kept
unchanged for reference. If you ever get the client back, the entry point is a controller, for example:

```bash
cd endgame/final_solution
python controller.py
```

## The TypeScript port

[`altcar/optibook-arena`](https://github.com/altcar/optibook-arena) keeps the ideas and replaces the dead
venue with a synthetic one: five dual-listed pairs (10 books), a random-walk fair value per stock, an
Ornstein–Uhlenbeck basis with occasional jumps, a synthetic news wire, and a seven-level book rebuilt on
every 250 ms tick.

- React 19 terminal served as static assets by a Cloudflare Worker.
- One Durable Object owns the simulation, positions, P&L, news and strategy state (SQLite-backed).
- WebSocket at `/ws`, REST under `/api/*`, single-origin deploy — no external services or API keys.
- All six engines toggleable and individually tunable, each with its own attribution and P&L.

Run it locally with Node 22+:

```bash
git clone https://github.com/altcar/optibook-arena
cd optibook-arena
npm install
npm run dev          # http://localhost:8787
npm run deploy       # wrangler deploy
```

The port is simulation only: no real money, no real exchange, no financial advice.

## Caveats

- This is a raw working repo, not a cleaned library: `__pycache__`, pip temp files, wheels, PDFs,
  SQLite databases and duplicated folders are all checked in.
- `endgame/PLEASE_KEEP_CODE_PRIVATE.txt` is the event's original instruction, left in place. Treat the
  contents as study material rather than a template to ship.
- The sentiment stacks expect `transformers`, `torch` and local `model-export/` weights; see
  `REPRODUCE_LARGE_FILES.md` before trying to rebuild them.
- There are no tests and there is no dependency lockfile for the Python side.

## Licence

No licence file is included. All rights remain with the original authors; the TypeScript port in
`optibook-arena` is MIT licensed. Ask before reusing anything from this repository.
