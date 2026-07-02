# edge-scanner

Read-only **cross-venue prediction market edge scanner** for Manifold, Kalshi, and
Polymarket. It exists to answer one question:

> How often does a *genuine, after-fee, executable, near-dated* cross-venue edge
> actually appear, and how long does each window stay open?

This is a **study, not a trading bot** — **zero real-money risk in v1**. It polls
public market data, normalizes it into one canonical schema, computes an honest
after-fee/after-lockup edge over hand-curated linked events, and logs the result so
the frequency and duration of real edges can be measured.

- Design: [`docs/edge-scanner-design.md`](docs/edge-scanner-design.md)
- Build plan: [`docs/implementation-plan.md`](docs/implementation-plan.md)
- Live-API facts & where the design doc is now stale: [`docs/api-findings.md`](docs/api-findings.md)

## Status

**Phase 4 (calibration study) — running.** All three connectors (**Manifold**
AMM probabilities, **Kalshi** dollar-string top-of-book, **Polymarket** Gamma + CLOB)
are implemented and verified live. Each cycle the daemon polls the curated set,
writes normalized quotes, and computes the §6 cross-venue edge per linked event into
`edge_snapshot`. The dashboard renders both the per-outcome price history and a
**net-edge-over-time** view (with the basis-risk flag broken out). 14 links are
curated in `config/links.yaml` (6 Fed-decision outcomes + 8 World Cup winners, all
Kalshi↔Polymarket), and the study has been collecting on a Raspberry Pi since
2026-06-18. As near-dated links resolve (World Cup ~July 19, Fed July 29) the daemon
**auto-retires** them (polling and edge rows stop; history stays), and
`scripts/curate.py` suggests replacement pairs — which you verify by hand before
they enter `links.yaml`.

A **within-platform arb harness** (phase 2) also runs on Manifold: it detects a
complete set buyable under $1 (binary YES+NO, or multi buy-all) and paper-executes
with fake money — proving the edge math and execution loop at zero risk.

Phases (see the design doc §7): **0** scaffold ✅ → **1** Manifold end-to-end ✅ →
**2** within-platform arb + paper execution ✅ → **3** add Kalshi ✅ / Polymarket ✅ +
curate ~15 links ✅ → **4** multi-week calibration study ⏳ (in progress).

## How it works

```
Manifold ─┐
Kalshi   ─┤ REST poll → normalize → edge engine (fees + lockup) → SQLite (WAL) → Streamlit
Polymarket┘                              ▲
                              config/links.yaml (hand-curated event links)
```

Two processes on one box (designed for a Raspberry Pi under systemd):

1. **`scanner`** — async poll → normalize → compute edges → write SQLite.
2. **`dashboard`** — Streamlit, reads the SQLite WAL directly (no write contention).

Edge model (per linked binary event): buy YES on venue A and NO on venue B, so
`gross_edge = 1 − (ask_a + ask_b)`, then subtract modeled per-venue fees and the
annualized lockup cost of tied-up capital. Long-dated "edges" and thin books are
surfaced, not hidden. All three venues share one fee shape, `k × C × p × (1−p)`
(derived from the live docs — see `docs/api-findings.md`).

## Prerequisites

- [**uv**](https://docs.astral.sh/uv/) — manages the Python version and packages in a
  local project venv, so nothing touches system packages.
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh   # if you don't have it
  ```
  uv reads `.python-version` (pinned to 3.11) and provisions that interpreter for you.

## Setup

```bash
git clone git@github.com:ckloote/edge-scanner.git
cd edge-scanner
uv sync          # creates .venv and installs deps + the dev group
```

## Usage

```bash
uv run edge-scanner                                       # boot the scanner daemon (Ctrl-C to stop)
uv run --extra dashboard streamlit run dashboard/app.py   # dashboard (installs streamlit/pandas on demand)
uv run python scripts/analyze.py                          # phase-4 report: edge-window frequency + duration
uv run python scripts/curate.py                           # suggest new near-dated links (you verify + paste)
uv run pytest                                             # run the test suite
uv run ruff check .                                       # lint
```

The daemon logs to stderr and writes to the SQLite file at `config/settings.toml`'s
`db_path` (default `data/edge_scanner.db`, git-ignored). With an empty `links.yaml`
it boots and idles — expected in phase 0.

## Configuration

- **`config/settings.toml`** — poll interval, `db_path`, `risk_free_rate` (drives the
  lockup-cost term — set this to the current short T-bill yield), and per-venue fee
  parameters. Fee constants were derived from the live API docs; re-verify at build.
- **`config/links.yaml`** — hand-curated cross-venue event links (the research input).
  Ships empty-but-valid. Each event has exactly two legs and names which outcome you
  *buy* on each venue, so polarity is explicit. Example shape is in the file. These are
  config, not data — edit by hand, keep in git.

## Deployment (Raspberry Pi / systemd)

A unit file is provided in [`deploy/scanner.service`](deploy/scanner.service)
(`Restart=always`). Edit the `User`/`WorkingDirectory`/`ExecStart` paths to match your
checkout, then:

```bash
sudo cp deploy/scanner.service /etc/systemd/system/edge-scanner.service
sudo systemctl daemon-reload
sudo systemctl enable --now edge-scanner.service
journalctl -u edge-scanner -f
```

## Layout

```
config/        settings.toml (risk-free rate, poll interval, fees) + links.yaml
scanner/       models, config loader, SQLite store, edge math, daemon (auto-retires
               resolved links), analysis (edge windows), curation (pair matching)
  connectors/  Connector Protocol + manifold / kalshi / polymarket (fees implemented)
dashboard/     Streamlit app (reads SQLite WAL directly)
scripts/       report.py (SSH status), analyze.py (§1 answer), curate.py (link candidates)
tests/         edge math, per-venue fees(), config/boot, wiring, analysis, curation
deploy/        systemd units for scanner + dashboard (Restart=always)
docs/          design doc, implementation plan, live-API findings, Pi deploy guide
```

## Safety & scope

- **Read-only, zero real-money risk in v1.** No order placement; the execution seam is
  left unimplemented on purpose.
- Cross-venue edge calibration is **binary-only**; multi-outcome handling exists solely
  for the Manifold within-platform harness (phase 2).
- Market matches are **hand-curated** — a wrong auto-match manufactures fake edges that
  would poison the study, so v1 does no automated semantic matching.
