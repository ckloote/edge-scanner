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

## Phase 1 — One venue E2E (Manifold)

> **Done when:** a real Manifold market's price history renders in the dashboard.

- Implement `ManifoldConnector.list_markets` / `poll_quotes` (endpoints in the
  docstrings): `GET /market/{id}` or `/slug/{slug}`; batch `GET /market-probs`.
- Map the **AMM** to the quote schema: `last = probability`, `bid = ask =
  probability`, sizes `None` (api-findings §Manifold).
- Wire the daemon to upsert market/outcome on discovery and insert quotes per cycle
  (code paths already present in `store.py`/`daemon.py`).
- Dashboard: add a price-history line chart from `quote_history(outcome_id)`.
- Tests: a recorded-fixture normalization test (no live network in CI).

## Phase 2 — Within-platform arb + paper exec (Manifold)

> **Done when:** edge math *and* the execution harness are both proven at zero risk.

- Detector: binary `YES+NO < $1`; multi `Σ answer prob ≠ 100%` (the only place the
  multi path is used — design doc §10).
- Paper-execution loop with fake money against the AMM; reuse `fees()` (=0) so the
  harness mirrors the real interface.
- Tests: arb detection on synthetic books.

## Phase 3 — Add real venues (read-only)

> **Done when:** live quotes for all three venues flow into `quote`.

- Implement `poll_quotes` for Kalshi (dollar strings; orderbook **bids-only** → YES
  ask = 1 − best NO bid) and Polymarket (Gamma discovery → `json.loads` clobTokenIds;
  CLOB `/book` for depth). All public, no auth.
- Hand-curate ~15 **near-dated** links in `config/links.yaml` (TBD #2); encode
  polarity per leg (`buy_outcome`).
- Compute and persist `edge_snapshot` per link per cycle via `scanner/edge.py`; set
  `basis_risk_flag` from resolution-source/time mismatch or a `suspect` link.
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
