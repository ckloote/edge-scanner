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
uv run pytest           # 33 tests, ~0.2s
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

## Phase 2 — Within-platform arb + paper exec (Manifold)

> **Done when:** edge math *and* the execution harness are both proven at zero risk.

- Detector: binary `YES+NO < $1`; multi `Σ answer prob ≠ 100%` (the only place the
  multi path is used — design doc §10).
- Paper-execution loop with fake money against the AMM; reuse `fees()` (=0) so the
  harness mirrors the real interface.
- Tests: arb detection on synthetic books.

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
- **First real link curated** (`config/links.yaml`): `fed-hold-jul-2026` — Kalshi
  `KXFEDDECISION-26JUL-H0` YES ≡ Polymarket "No change" NO, both resolving 2026-07-29.
  Live result: gross ≈ 0, net ≈ −0.013 after fees + lockup (an honest no-arb).
- **Gotcha fixed:** YAML 1.1 reads unquoted `YES`/`NO` as booleans — the links loader now
  normalizes `buy_outcome` (`scanner/config._norm_outcome`).
- Curating the rest of the ~15 near-dated links (TBD #2) is the remaining phase-3 work.
- **Do not** start automated semantic matching (design doc §7).

## Phase 4 — Calibration study

> **Done when:** `edge_snapshot` has enough history to answer the §1 question.

- Run for several weeks; dashboard view of net-edge-over-time per event, **with the
  basis-risk flag broken out** (design doc §10).
- Analysis tail (pandas): frequency + duration of genuine, after-fee, executable,
  near-dated edges.

---

## Open TBDs (design doc §10)

| TBD | Status |
|---|---|
| #1 `risk_free_rate` | Placeholder **0.043** in `settings.toml` — **confirm** live short T-bill at build. |
| #2 ~15 links | Deferred to phase 3; `links.yaml` ships empty-but-valid (loader enforces exactly 2 legs, unique ids). |
| #3 Polymarket public CLOB read | **Resolved** — `/book` is public + has depth (api-findings). |
| #4 Kalshi category multipliers | **Resolved/flagged** — schedule appears uniform 0.07; kept per-series configurable. Re-confirm the canonical PDF. |

## Graduation triggers to watch (design doc §2)

- Move `quote`/`edge_snapshot` to partitioned Parquet + DuckDB when history queries
  get sluggish or write volume jumps an order of magnitude — a `store.py` change only.
- WS feeds + execution sidecar are a separate design pass (design doc §9), out of v1.
