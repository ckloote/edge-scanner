# Cross-Venue Prediction Market Edge Scanner — Design Doc (v1)

**Status:** design, pre-implementation
**Target runtime:** Raspberry Pi (home network), single user
**Intended next step:** hand to Claude Code for scaffolding from this spec

---

## 1. Purpose & requirements

### The one question this project answers
> How often does a *genuine, after-fee, executable, near-dated* cross-venue edge actually appear, and how long does each window stay open?

Everything in v1 is plumbing in service of producing that calibration dataset. The deliverable is a study, not a trading bot. A clean path to execution is preserved but explicitly out of scope for v1.

### Functional requirements
- Ingest market data from Manifold, Kalshi, and Polymarket (read-only).
- Normalize heterogeneous venue data into one canonical schema.
- Map hand-curated equivalent markets across venues (event linking).
- Compute and log a cross-venue edge metric over time per linked event.
- Detect within-platform arb (YES+NO < $1, multi-outcome ≠ 100%) on Manifold for harness validation.
- Surface live state and history in a single-user dashboard.

### Non-functional requirements
- Runs persistently on a Pi with modest memory; survives restarts and flaky network.
- Zero real-money risk in v1.
- Low operational overhead (this is a personal project, not a service).

### Explicit non-goals for v1
- No execution against real money.
- No automated semantic market matching — links are hand-curated.
- No multi-runtime — pure Python.
- No WebSockets in v1 (see §3 rationale); REST polling only.

### Constraints
- Single developer, intermittent hobby time.
- Pi-class hardware: favor lightweight, embedded, low-ceremony tooling.

---

## 2. Key design decisions (and reversals)

**Ingestion: REST polling, not WebSockets — for now.**
Earlier framing assumed a WebSocket "firehose." That was the wrong mental model for *this* scope. v1 watches ~15 hand-curated markets, so polling each venue every 2–5s is ~90 quote rows per cycle — a trickle. Polling sidesteps WS auth, reconnect logic, and the 5-connection-per-IP limits. **WS is a deferred optimization**, justified only if/when we move toward latency-sensitive execution.

**Storage: SQLite (WAL) for all of v1.**
This reverses the earlier "drop SQLite" call — and the reversal is deliberate, because the binding constraint changed. The SQLite knock was about single-writer contention under a *continuous WS firehose*. At polling-15-markets volume that pressure doesn't exist, and WAL mode gives concurrent reads for the dashboard. SQLite wins on zero ops, single file, and Claude-Code-friendliness.
- **Graduation trigger:** move the `quote` / `edge_snapshot` time-series to partitioned Parquet + DuckDB *when* either (a) analytical queries over history get sluggish, or (b) you add WS feeds and write volume jumps an order of magnitude. The schema below is written so this is a migration of two tables, not a rewrite.

**Language: Python.**
Official SDKs (`py-clob-client`, Kalshi Python examples) live here; the analysis tail (calibration, backtesting) is pandas/numpy; workload is I/O-bound so the GIL never bites. A Go/Rust execution sidecar is a *future* fork, not a v1 concern.

**Dashboard: Streamlit.**
Single-user internal analytics tool that's mostly charts and tables — Streamlit's sweet spot, Python-native, no build step. React is reserved for if this ever needs a polished or shareable UI.

---

## 3. High-level architecture

```
                    ┌─────────────────────────────────────────┐
                    │            Raspberry Pi (systemd)         │
                    │                                           │
   Manifold REST ──▶│  ┌──────────┐   ┌──────────────┐         │
   Kalshi REST   ──▶│  │ poller   │──▶│ normalizer   │         │
   Polymarket    ──▶│  │ (asyncio)│   │ raw→canonical│         │
   (Gamma+CLOB   ──▶│  └──────────┘   └──────┬───────┘         │
    REST)           │                        │                 │
                    │                        ▼                 │
                    │                 ┌──────────────┐         │
                    │                 │ edge engine  │◀─ links │
                    │                 │ (fees+lockup)│   (YAML) │
                    │                 └──────┬───────┘         │
                    │                        ▼                 │
                    │                 ┌──────────────┐         │
                    │                 │ SQLite (WAL) │         │
                    │                 └──────┬───────┘         │
                    │                        │ read           │
                    │                 ┌──────▼───────┐         │
                    │                 │  Streamlit   │         │
                    │                 └──────────────┘         │
                    └─────────────────────────────────────────┘
```

Two processes, one box:
1. **`scanner` daemon** — async poll → normalize → compute edges → write SQLite. Run under systemd with restart-on-failure.
2. **`dashboard`** — Streamlit, reads SQLite (WAL) directly. No write contention.

Hand-curated event links live in a **version-controlled YAML file**, not the DB — they're config, they're edited by hand, and you want them in git.

---

## 4. Data model

Canonical schema. Prices normalized to float in `[0, 1]` at the connector boundary (Kalshi sends integer cents; Polymarket sends fractions; Manifold sends probabilities — normalize all to the same thing).

```sql
-- Canonical market registry (one row per venue market)
CREATE TABLE market (
    market_id        TEXT PRIMARY KEY,   -- canonical: f"{venue}:{venue_market_id}"
    venue            TEXT NOT NULL,       -- 'manifold' | 'kalshi' | 'polymarket'
    venue_market_id  TEXT NOT NULL,
    title            TEXT NOT NULL,
    market_type      TEXT NOT NULL,       -- 'binary' | 'multi'
    close_time       TIMESTAMP,
    resolution_time  TIMESTAMP,           -- expected; nullable
    resolution_source TEXT,               -- free text; feeds basis-risk flag
    status           TEXT NOT NULL,       -- 'open' | 'closed' | 'resolved'
    UNIQUE (venue, venue_market_id)
);

CREATE TABLE outcome (
    outcome_id  TEXT PRIMARY KEY,         -- f"{market_id}:{label}"
    market_id   TEXT NOT NULL REFERENCES market(market_id),
    label       TEXT NOT NULL,            -- 'YES'/'NO' for binary; option text for multi
    UNIQUE (market_id, label)
);

-- Time-series. Highest-volume table → first candidate for Parquet/DuckDB later.
CREATE TABLE quote (
    ts          TIMESTAMP NOT NULL,
    outcome_id  TEXT NOT NULL REFERENCES outcome(outcome_id),
    bid         REAL,                     -- [0,1]
    ask         REAL,                     -- [0,1]
    bid_size    REAL,                     -- in shares / contracts
    ask_size    REAL,
    last        REAL
);
CREATE INDEX idx_quote_outcome_ts ON quote(outcome_id, ts);

-- Computed cross-venue edges over time. The actual research output.
CREATE TABLE edge_snapshot (
    ts                 TIMESTAMP NOT NULL,
    event_id           TEXT NOT NULL,     -- from the links YAML
    leg_a_outcome_id   TEXT NOT NULL,
    leg_b_outcome_id   TEXT NOT NULL,
    gross_edge         REAL,              -- 1 - (price_a + price_b)
    modeled_fees       REAL,
    lockup_cost        REAL,              -- annualized opp cost of locked capital
    net_edge           REAL,              -- gross - fees - lockup
    executable_size    REAL,              -- min(depth on each leg)
    days_to_resolution REAL,
    basis_risk_flag    INTEGER NOT NULL   -- 0/1, see §6
);
CREATE INDEX idx_edge_event_ts ON edge_snapshot(event_id, ts);
```

### Event links (hand-curated, YAML, in git)
Polarity is the subtle bit: "YES" on one venue can be the *complement* of the other venue's question. The link must encode which outcome pairs with which, and whether it's a same-side or opposite-side mapping.

```yaml
# links.yaml
events:
  - event_id: fed-cut-june-2026
    note: "Verify both resolve on the same FOMC announcement date."
    resolution_check: confirmed-equivalent      # or 'suspect' → forces basis_risk_flag
    legs:
      - venue: kalshi
        venue_market_id: FED-26JUN
        buy_outcome: YES        # buy YES here ...
      - venue: polymarket
        venue_market_id: "0xabc..."
        buy_outcome: NO         # ... and NO there = the arb pair
```

---

## 5. Connector interface

One Protocol, three implementations. Each connector owns its own auth, rate-limit etiquette, raw→canonical normalization, and **its own fee model** (fees are venue-specific and central to the edge math, so they belong with the connector).

```python
from typing import Protocol, AsyncIterator

class Connector(Protocol):
    venue: str

    async def list_markets(self) -> list[Market]:
        """Discover + normalize market/outcome metadata."""

    async def poll_quotes(self, venue_market_ids: list[str]) -> list[Quote]:
        """Fetch current book for the curated set. v1 ingestion path."""

    def fees(self, price: float, size: float, side: str) -> float:
        """Venue-specific cost for a fill. Used by the edge engine."""

    # Deferred (execution phase) — leave the seam, don't implement:
    # async def stream_quotes(...) -> AsyncIterator[Quote]: ...   # WS
    # async def place_order(...): ...
```

### Per-venue notes
- **Manifold** — REST, generous limits, play money. Has binary + multi-outcome and real YES/NO and multi mispricings, so it's the harness for both the edge math *and* a paper-execution loop. Mirrors many real Kalshi/Polymarket events (useful for matching practice).
- **Kalshi** — REST market-data endpoints; RSA key auth, session tokens (~30 min, refresh). Prices are integer cents → divide to `[0,1]`. CFTC-regulated. Has a real taker fee schedule — model it, don't assume zero.
- **Polymarket** — Gamma API (read-only, no auth) for discovery; CLOB for order book. Prices are fractions already. ~2% on winnings + gas in the fee model. *Verify* whether the public CLOB read endpoints meet v1 needs without a paid feed before committing.

---

## 6. The edge model (the part that matters)

For a binary event linked across two venues, the arb pair is: **buy YES on venue A at `ask_a`, buy NO on venue B at `ask_b`** (per the links polarity). At resolution exactly one leg pays $1, so:

```
gross_edge      = 1 - (ask_a + ask_b)          # arb exists when ask_a + ask_b < 1
capital         = ask_a + ask_b                # tied up until resolution
modeled_fees    = connector_A.fees(...) + connector_B.fees(...)
lockup_cost     = risk_free_rate * (days_to_resolution / 365) * capital
net_edge        = gross_edge - modeled_fees - lockup_cost
executable_size = min(ask_size_a, ask_size_b)  # quoted edge is only real up to here
```

Three things this makes honest that a naive scanner hides:
1. **Lockup cost** kills long-dated "edges." A 2% gross spread resolving in 6 months is ~4% annualized *before* fees — worse than T-bills. The model surfaces this directly, which is why `days_to_resolution` and `lockup_cost` are first-class columns.
2. **Executable size** ≠ quoted edge. Thin books mean the spread evaporates above `min(depth)`. Log it so the study measures *capturable* edge, not screen edge.
3. **Basis risk** is unquantifiable but flaggable. Set `basis_risk_flag = 1` when `resolution_source` differs across legs, `resolution_time` differs, or the link is marked `suspect`. These are the trades that look like arb and resolve as a loss (cf. the Cardi B Super Bowl divergence). The study should report edges *with the flag broken out*.

`risk_free_rate` is a single config value (e.g., current T-bill ~ set in config).

---

## 7. Phased build plan

| Phase | Goal | Done when |
|---|---|---|
| **0. Scaffold** | Repo, `Connector` Protocol, canonical schema, SQLite layer, links.yaml loader, systemd unit. | `scanner` boots, writes nothing useful yet, restarts cleanly. |
| **1. One venue E2E** | Manifold connector → poll → normalize → store → one Streamlit chart. | A real Manifold market's price history renders in the dashboard. |
| **2. Within-platform arb + paper exec** | Detect YES+NO<$1 / multi≠100% on Manifold; execute with fake money. | Edge math *and* the execution harness are both proven at zero risk. |
| **3. Add real venues (read-only)** | Kalshi + Polymarket connectors; hand-curate ~15 links in YAML. | Live quotes for all three venues flowing into `quote`. |
| **4. Calibration study** | Run the §6 edge model over the linked set for several weeks. | `edge_snapshot` has enough history to answer the §1 question. |

**Do not** start phase 3 with automated semantic matching — that's a separable hard problem. Hand-curate 15 pairs; the schema's `event_id` seam means swapping in automated matching later is additive, not a rewrite.

---

## 8. Suggested repo layout

```
edge-scanner/
├── pyproject.toml
├── config/
│   ├── settings.toml          # risk_free_rate, poll interval, db path
│   └── links.yaml             # hand-curated event links
├── scanner/
│   ├── __init__.py
│   ├── models.py              # Market, Outcome, Quote, EdgeSnapshot dataclasses
│   ├── store.py               # SQLite (WAL) read/write; the Parquet seam lives here
│   ├── edge.py                # §6 math; pure functions, unit-tested
│   ├── daemon.py              # asyncio poll loop, orchestration
│   └── connectors/
│       ├── base.py            # Connector Protocol
│       ├── manifold.py
│       ├── kalshi.py
│       └── polymarket.py
├── dashboard/
│   └── app.py                 # Streamlit
├── tests/
│   └── test_edge.py           # edge math is the thing most worth testing
└── deploy/
    └── scanner.service        # systemd unit, Restart=always
```

---

## 9. Scale, reliability, trade-offs

- **Reliability:** the only real engineering risk in v1 is the poll loop surviving network blips and venue hiccups — per-venue try/except so one bad venue doesn't stall the others; exponential backoff on errors; idempotent writes. systemd `Restart=always` covers process death.
- **Rate limits:** Manifold generous; Kalshi tiered; Polymarket CLOB ~100+/min. Polling 15 markets every few seconds is well under all of them. Centralize interval in config.
- **Pi housekeeping:** keep the daemon's live state small; let SQLite be the durable store; add a retention/rotation job once `quote` grows; `logrotate` on logs.
- **Trade-offs made explicit:**
  - REST polling trades latency for simplicity — correct for a *study*, wrong for *execution*. Revisit at the execution fork.
  - SQLite trades analytical horsepower for zero ops — correct at this volume, revisit per §2 graduation trigger.
  - Hand-curated links trade coverage for correctness — correct, because a wrong auto-match produces fake edges that poison the study.

### What I'd revisit as it grows
1. **WS feeds + Parquet/DuckDB** together, if/when you move toward execution (the two constraints rise together).
2. **Automated semantic matching** once the hand-curated study proves edges exist worth scaling to.
3. **Go/Rust execution sidecar** only if calibration shows capturable, near-dated, after-fee edges *and* you decide to act on them — at which point latency and the legal/tax/KYC questions become real and need their own design pass.

---

## 10. Handoff notes (read first)

### Prime directive for implementation
Before writing any connector, **fetch the current API docs for each venue and derive endpoints, auth, response shapes, rate limits, and fee formulas from those live docs.** This document fixes the *design*; it deliberately does not freeze venue API contracts, which drift. Treat any endpoint or field name implied here as illustrative, not authoritative. Verify against:
- **Manifold:** docs.manifold.markets (API section)
- **Kalshi:** docs.kalshi.com / trading-api.readme.io + kalshi.com/fee-schedule
- **Polymarket:** docs.polymarket.com (Gamma + CLOB)

### Fee formulas — verify, then implement as each connector's `fees()` method with a unit test
Current as of the doc date; re-confirm at build time.

**Kalshi (taker):**
```
fee = roundup_to_cent( multiplier × C × P × (1 − P) )
```
- `C` = contract count, `P` = price in `[0,1]`. Rounding is **up to the next whole cent on the aggregate order**, not per share.
- `multiplier` = 0.07 for most categories; some categories carry a higher multiplier — pull the per-category table from kalshi.com/fee-schedule rather than hardcoding 0.07.
- Maker ≈ 25% of taker. No settlement fee.
- **Rounding gotcha:** because it rounds up to the cent on the whole order, small fills pay a disproportionate effective rate. Apply the fee at the actual intended `executable_size`, not per-share, or the model understates cost on thin fills.

**Polymarket US (the venue you'd actually trade):**
```
fee = max( 0.001 , 0.0010 × premium )     # flat 0.10% taker, $0.001 min/trade, 0% maker
```
- This is the CFTC-regulated US DCM — flat and simple. Confirm whether any per-transaction settlement cost applies on top; the US venue most likely does **not** carry the crypto-native Polygon gas model.

**Polymarket (crypto-native / international) — reference only, not your path:**
```
fee = 0.0625 × P × (1 − P)                 # taker, and only on a subset of markets
```
- Most markets are 0-fee; the formula applies to specific categories (short-duration crypto direction, certain post-Feb-2026 NCAAB / Serie A), plus Polygon gas. Included only so the model is venue-complete.

**Manifold:** play money — model fee as 0, but keep the `fees()` seam so the harness mirrors the real-venue interface exactly.

### Stated limitations (so phase-4 numbers aren't over-read)
- **`executable_size` is top-of-book only.** The schema stores one price level, so it measures depth at the best bid/ask, not true capturable size across the book. Fine for a frequency/duration study; not a position-sizing tool. Deepening the book is a known follow-up if results warrant.
- **The cross-venue edge model is binary-only.** Multi-outcome handling exists solely for the Manifold within-platform harness (phase 2). Phase-4 calibration covers binary linked events only.
- **Basis risk is flagged, not quantified.** `basis_risk_flag` is a boolean from resolution-source/time mismatch or a `suspect` link. Always report edges with the flag broken out — a flagged "edge" is not a clean arb.

### Open TBDs to resolve before / at build
1. **`risk_free_rate`** — set a concrete value in `settings.toml` (current short T-bill yield). One number; drives the entire lockup-cost term.
2. **The ~15 markets** — hand-pick and write `links.yaml`. Bias toward *near-dated* events; lockup cost is smallest there, which is the only regime where edges plausibly survive.
3. **Polymarket CLOB read access** — confirm public read endpoints cover v1 needs without a paid real-time feed. Five-minute doc check before phase 3.
4. **Kalshi category multipliers** — pull the current per-category fee multiplier table.

### Suggested handoff prompt
> "Here's a design doc for a read-only cross-venue prediction market edge scanner. Produce an implementation plan and scaffold phase 0, following the doc's architecture, schema, and phasing. **Before writing any connector, fetch and read the current Manifold, Kalshi, and Polymarket API docs and derive endpoints, auth, and the exact fee formulas from them** — the doc's fee formulas are a starting point to verify, not gospel. Write a unit test per venue `fees()` method. Flag anything in the doc the live API contradicts."
