# Implementation plan

Companion to [`edge-scanner-design.md`](edge-scanner-design.md). The design doc
fixes architecture, schema, and phasing; this plan turns it into concrete work and
records the decisions made while scaffolding **phase 0**. Live-API facts and the
design-doc contradictions they create are in [`api-findings.md`](api-findings.md).

## Toolchain

Python managed with **uv**, fully self-contained in a project `.venv` (nothing
touches system packages). Python pinned to **3.11** (`.python-version`) for Pi
parity; `tomllib` is stdlib at that version.

```bash
uv sync                 # .venv + deps + dev group
uv run pytest           # 95 tests, <1s
uv run edge-scanner     # boot the daemon
uv run ruff check .
```

---

## Phase 0 — Scaffold ✅ (delivered)

> **Done when:** `scanner` boots, writes nothing useful yet, restarts cleanly.

Verified: daemon logs `scanner up …`, creates all four tables, idles on empty
`links.yaml`, handles SIGINT/SIGTERM → exit 0 with the WAL checkpointed.

| Design-doc piece | Delivered in |
|---|---|
| Repo + packaging (uv) | `pyproject.toml`, `.python-version`, `uv.lock`, `README.md` |
| Canonical dataclasses | `scanner/models.py` |
| Config loaders (TOML + YAML) | `scanner/config.py`, `config/settings.toml`, `config/links.yaml` |
| SQLite (WAL) + Parquet seam | `scanner/store.py` (DDL verbatim from §4) |
| Edge math (pure, §6) | `scanner/edge.py` |
| `Connector` Protocol | `scanner/connectors/base.py` |
| Three connectors w/ **`fees()` implemented** | `connectors/{manifold,kalshi,polymarket}.py` |
| Daemon (asyncio poll loop, backoff, signals) | `scanner/daemon.py` |
| Dashboard (boots, reads WAL) | `dashboard/app.py` |
| systemd unit (`Restart=always`) | `deploy/scanner.service` |
| Tests: edge math + **per-venue `fees()`** | `tests/test_{edge,fees,config,daemon_boot}.py` |

### Decisions made (and why)

1. **`fees(price, size, side)` — `side` = liquidity role (`taker`/`maker`), not
   buy/sell.** Venue fees turn on maker-vs-taker, not direction. The edge engine
   crosses the spread to buy → always `taker`. Polymarket also honours `sell`
   (exempt). Per-venue config (multiplier/rate/mode) lives on the connector
   instance, so the Protocol signature stays clean.
2. **One fee shape for all three venues:** `k × C × p × (1−p)` (see api-findings).
   Manifold `k=0`; Kalshi `k=`multiplier + centicent round-up; Polymarket
   `k=`per-category/uniform rate. This fell out of the live docs, not the design.
3. **Polymarket `venue_mode` (`intl` default).** Public read endpoints serve the
   international venue, so we default the fee model to match what we actually poll;
   `us` is a one-line switch when/if reading the US DCM.
4. **Kalshi per-series multiplier map.** The market object exposes no category
   field, so non-general multipliers are configured per series ticker.
5. **Connector read paths (`list_markets`/`poll_quotes`) are documented seams**
   that raise `NotImplementedError` with the exact derived endpoints/normalization
   in the docstring. Manifold is phase 1; Kalshi/Polymarket are phase 3 — so phase
   0 deliberately ships `fees()` (required + tested) and leaves the network parsing
   for its phase rather than shipping it untested.
6. **Daemon isolates venues** (per-venue try/except, exponential backoff, links→
   poll-targets grouping) so an unimplemented or flaky venue never stalls others.

---

## Phase 1 — One venue E2E (Manifold) ✅ (delivered)

> **Done when:** a real Manifold market's price history renders in the dashboard.

Verified against the live API: curating two real binary markets and polling 3 cycles
produced market/outcome/quote rows with YES+NO summing to 1.0 and timestamped history
— exactly what the dashboard chart reads.

- `ManifoldConnector.list_markets(ids)` / `poll_quotes(ids)` implemented
  (`scanner/connectors/manifold.py`): metadata via `GET /market/{id}` with a
  `GET /slug/{slug}` fallback (curated id may be an id *or* a slug); quotes via batch
  `GET /market-probs` (`ids` is a **repeated array param**, confirmed live).
- **AMM → quote** mapping: binary → YES `bid=ask=last=prob`, NO `= 1−prob`; multi →
  one outcome per answer at its probability; sizes `None` (no quoted book).
- Daemon now **syncs metadata** (upsert market + outcomes) before polling, with a
  periodic re-sync (`META_REFRESH_CYCLES`) to catch status/close changes; per-venue
  isolation preserved.
- Dashboard: per-outcome **price-history line chart** (`dashboard/app.py`).
- Tests: recorded-fixture normalization + an `httpx.MockTransport` integration test —
  no network in CI (`tests/test_manifold.py`). Suite: **45 passing**.

### Decisions

- **`list_markets` now takes the curated ids** and returns Markets carrying their
  Outcomes (transient `Market.outcomes`). v1 is curated, so fetching specific ids
  beats enumerating an entire venue; the Protocol + the Kalshi/Polymarket seams were
  updated to match.
- **Quotes self-resolve.** `poll_quotes` resolves any uncached id on demand, so it is
  correct even if called before a metadata sync (the daemon still syncs first for the
  FK + dashboard rows).

## Phase 2 — Within-platform arb + paper exec (Manifold) ✅ (delivered)

> **Done when:** edge math *and* the execution harness are both proven at zero risk.

- **Detector** (`scanner/arb.py`): a complete set buyable under $1 is an arb — binary
  `YES_ask + NO_ask < 1`; multi (shouldAnswersSumToOne) `Σ answer_ask < 1`. Pure +
  unit-tested (`tests/test_arb.py`).
- **Tradable asks** (`ManifoldConnector.arb_quotes`): AMM price combined with the
  limit book (bids-only, like Kalshi: YES ask = min(prob, 1 − best NO bid); per-answer
  for multi). Tested with recorded fixtures.
- **Paper-execution harness** (`scanner/paper.py` + `paper_trade` table): fake-money
  fills with a capped stake, locked-in profit, a per-market cooldown, and a positive-net
  guard. Unit-tested (`tests/test_paper.py`).
- **Daemon wiring**: a per-cycle pass over a configured Manifold watchlist
  (`[manifold_harness]` in settings.toml); dashboard shows the paper trades.
- Verified live: both watched markets price a complete set at exactly $1.00 (no arb —
  Manifold is efficient; crossing limit orders get matched away), and the paper path
  records a fill correctly on a synthetic arb. So the math + execution are proven at
  zero risk; live detections are expected to be rare.

## Phase 3 — Add real venues (read-only) ✅ (core complete)

> **Done when:** live quotes for all three venues flow into `quote`.

All three connectors are implemented and verified live, and the first real
cross-venue edge computes end-to-end and renders in the dashboard.

- **Kalshi connector ✅** (`scanner/connectors/kalshi.py`). Top-of-book from one batched
  `GET /markets?tickers=` call: dollar-string prices in [0,1]; book is **bids-only**, so
  NO bid == YES ask orders and NO sizes derive from `yes_bid_size_fp`/`yes_ask_size_fp`.
- **Polymarket connector ✅** (`scanner/connectors/polymarket.py`). Gamma
  `GET /markets?condition_ids=` (clobTokenIds/outcomes are JSON-encoded strings) →
  CLOB `GET /book?token_id=` per YES/NO token; best bid = max price, best ask = min price.
- **Edge wiring ✅** (`Scanner._compute_edges`). Each cycle, after polling, the §6 edge
  is computed from the latest quotes per linked event and persisted to `edge_snapshot`.
  `basis_risk_flag` = 1 on a `suspect` link or a resolution-time mismatch (free-text
  `resolution_source` is **not** compared — it virtually always differs across venues, so
  the curator's `resolution_check` is the source-equivalence signal). Tests in
  `tests/test_edge_wiring.py`.
- **~15 links curated ✅** (`config/links.yaml`, verified 2026-06-16): 6 Fed-decision
  outcomes (July + September FOMC × hold / 25bps cut / 25bps hike) and 8 World Cup
  winner team markets — all confirmed-equivalent, near-dated, liquid on both venues.
  Live: edges compute clean (basis 0, correct ~33–92 day horizons) — mostly slightly
  negative net (efficient), with real divergences surfacing (e.g. `wc26-argentina`
  gross +1.1%).
- **Gotcha fixed:** YAML 1.1 reads unquoted `YES`/`NO` as booleans — the links loader now
  normalizes `buy_outcome` (`scanner/config._norm_outcome`).
- **Data-quality note:** Kalshi `close_time` can be a far-future placeholder
  (`can_close_early`); the connector uses `expected_expiration_time` for
  `resolution_time`, so lockup/horizon stay correct (verified on the World Cup markets).
- **Direction-agnostic edge engine ✅** (`Scanner._compute_edges`). Each cycle evaluates
  BOTH arb directions for a link — the encoded pair (A.YES + B.NO) and its mirror
  (A.NO + B.YES), both of which pay $1 for a binary equivalence — and persists the one
  with the higher net edge (the chosen `leg_*_outcome_id`s record which). This caught
  real after-fee edges the fixed-polarity version missed, e.g. `fed-sep-hold` flips to
  buy-Kalshi-NO + Poly-YES for net **+1.66%** (basis-clean), `wc26-usa` net **+0.48%**.
  The dashboard shows the chosen direction. Tests in `tests/test_edge_wiring.py`.
  **Since 2026-07-02 the losing direction is persisted too** (`mirror_net_edge`,
  `mirror_executable_size`; NULL for older rows and when the mirror is unquotable) —
  the two-sided spread and direction flips can't be backfilled, so they're captured
  as they happen. Idempotent column migration in `store._migrate()`.
- **Do not** start automated semantic matching (design doc §7).

## Phase 4 — Calibration study (running since 2026-06-18)

> **Done when:** `edge_snapshot` has enough history to answer the §1 question.

Collecting on the Pi (systemd, two services — see `docs/deploy-pi.md`) at a 30s
interval over the curated links. Tooling delivered along the way:

- **Analysis tail ✅** (`scanner/analysis.py` + `scripts/analyze.py`, 2026-07-02).
  Extracts positive-net edge WINDOWS (maximal runs of snapshots above a threshold);
  coverage gaps >90s close a window and never bridge one; single-snapshot "blips"
  are counted but excluded from duration stats. The CLI reports per-event
  %-time-positive, the window table, and the §1 answer across thresholds
  (0/0.25%/0.5%/1%) with a `--min-exec` depth floor so *executable* means real
  size held throughout. Built on stdlib (not pandas as sketched) so it runs with
  the daemon's own deps over SSH. First 14 days: screen edges are common, but
  depth — not price — is the binding constraint.
- **Ops hardening** (2026-07-02): fixed a crash-loop where the per-venue backoff
  sleep's `TimeoutError` escaped the error handler and killed the daemon on every
  venue poll failure (98 systemd restarts, ~18 min of coverage lost — measured,
  negligible, and coincident with venue outages so the study is unbiased).
- **Stale-quote guard ✅** (2026-07-02): a leg whose latest quote is older than 3
  poll intervals is treated as unquotable, so a downed venue's last stored quote
  can't keep feeding edge snapshots (phantom windows). The cutoff equals the
  analysis gap rule (90s at 30s cadence): no-row-written and window-closes agree.
- **Auto-retire guard ✅** (2026-07-02): a link with a resolved leg drops out of
  polling and edge computation (logged once; history stays). Needed because a
  resolved Kalshi market keeps quoting $1.00 asks at zero size, which wrote
  plausible-looking junk edge rows after the first World Cup eliminations.
- **Curation assistant ✅** (`scanner/curation.py` + `scripts/curate.py`,
  2026-07-02): as links resolve, suggests replacement Kalshi↔Polymarket pairs
  (title similarity gated by resolution-date proximity) as paste-ready stanzas.
  Emitted `resolution_check: suspect` on purpose — the human verifies equivalence
  and flips it; automated matching stays banned (design doc §7/§9). Venue
  enumeration facts (Kalshi `/events`, Gamma bounds) are in api-findings.md.

- **Quote retention ✅** (2026-07-02): daily in-daemon thinning — `quote` rows
  older than `retention_full_hours` (48) keep one row per outcome per
  `retention_bucket_seconds` (300); `edge_snapshot` untouched; idempotent
  (`store.thin_quotes`); first pass at boot. Freed pages are reused, so the DB
  file plateaus rather than shrinks (manual VACUUM only if space is needed).

Remaining for phase 4: keep the link set replenished as near-dated events resolve
(assistant + hand-verification).

---

## Open TBDs (design doc §10)

| TBD | Status |
|---|---|
| #1 `risk_free_rate` | **Done** — set to **0.0364** (live short T-bill, 2026-06) before collection began; the whole `edge_snapshot` history is on one rate. Inputs are stored separately, so net can be recomputed offline if it moves. |
| #2 ~15 links | **Done** — 14 verified links curated (6 Fed + 8 World Cup) in `config/links.yaml`. |
| #3 Polymarket public CLOB read | **Resolved** — `/book` is public + has depth (api-findings). |
| #4 Kalshi category multipliers | **Resolved/flagged** — schedule appears uniform 0.07; kept per-series configurable. Re-confirm the canonical PDF. |

## Graduation triggers to watch (design doc §2)

- Move `quote`/`edge_snapshot` to partitioned Parquet + DuckDB when history queries
  get sluggish or write volume jumps an order of magnitude — a `store.py` change only.
- WS feeds + execution sidecar are a separate design pass (design doc §9), out of v1.
