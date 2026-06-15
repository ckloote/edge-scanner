# Live-API findings & design-doc contradictions

**Derived 2026-06-15** by fetching the current venue API docs (the design doc's
prime directive, §10). The design doc fixes the *design*; it deliberately does not
freeze the venue API contracts, which drift. Everything below is what the **live
docs** say — where they disagree with the design doc, that is called out as
**⚠ CONTRADICTION**. Re-verify at build time; fee schedules especially move.

Sources fetched:
- Manifold: `docs.manifold.markets/api`
- Kalshi: `docs.kalshi.com` (`/getting_started/quick_start_market_data`,
  `/getting_started/fee_rounding`, `/api-reference/market/*`), `kalshi.com/fee-schedule`,
  `help.kalshi.com/.../fees`
- Polymarket: `docs.polymarket.com` (Gamma + CLOB references),
  `help.polymarket.com/.../trading-fees`, `docs.polymarket.us/fees`

---

## TL;DR — what the design doc gets wrong now

| # | Area | Design doc says | Live API says | Impact |
|---|------|-----------------|---------------|--------|
| 1 | **Polymarket US fee** | flat `max(0.001, 0.0010 × premium)` (0.10%) | `Fee = 0.05 × C × p × (1−p)`, maker rebate −0.0125, self-caps $1.25/100 @ 50¢ (eff. 2026‑04‑03) | **High** — different formula shape |
| 2 | **Polymarket crypto fee** | `0.0625 × p(1−p)` on a subset | Fee Structure V2 (eff. 2026‑03‑30): per-category `feeRate × C × p(1−p)`, crypto 0.07 | **High** |
| 3 | **Which Polymarket?** | "the venue you'd trade" = US DCM | Public read endpoints (Gamma + `clob.polymarket.com`) serve the **international** venue; US is separate | **Med** — read ≠ trade venue |
| 4 | **Kalshi price units** | "integer cents → divide by 100" | **dollar strings** (`yes_bid_dollars="0.5600"`) | **Med** — parse as float, no /100 |
| 5 | **Kalshi fee rounding** | "round up to whole cent, aggregate order" | round up to **centicent ($0.0001)**, per-fill accumulator | **Med** |
| 6 | **Kalshi category multipliers** | "some categories higher; pull the table" | current schedule appears **uniform 0.07**; API exposes **no** category field | **Low/Med** |
| 7 | **Kalshi auth** | "RSA key + session tokens ~30 min" | market data is **public (no auth)**; trading uses per-request **RSA‑PSS signing**, not session tokens | **Med** — v1 needs no auth |
| 8 | **Manifold book** | quote schema has bid/ask/size | Manifold is a **CPMM AMM**, no native top-of-book book | **Low** — map probability→price |

---

## Manifold

- **Base URL:** `https://api.manifold.markets/v0`
- **Auth:** none for reads; `Authorization: Key <key>` for writes. **v1 read-only → no auth.**
- **Rate limit:** 500 req/min/IP.
- **Discovery:** `GET /markets` (paginated, `limit≤1000`), `GET /market/{id}`, `GET /slug/{slug}`.
- **Prices:** `GET /market/{id}/prob` → `{"prob": 0.62}`; batch `GET /market-probs?ids=`.
  Multiple-choice: `FullMarket.answers[]`, each with its own `probability` in `[0,1]`.
- **Market fields:** `outcomeType` (BINARY | MULTIPLE_CHOICE | FREE_RESPONSE | NUMERIC | …),
  `mechanism` (cpmm-1 | cpmm-multi-1 | dpm-2), `probability`, `pool`, `closeTime`/
  `resolutionTime` (epoch **ms**), `isResolved`, `resolution`, `token` (MANA | CASH).
- **Fees:** play money. Real bets carry `fees{creatorFee, platformFee, liquidityFee}` in
  **mana**. Modeled as **0**; seam kept (design doc §10).

**⚠ Contradiction (8 — schema impedance, not strictly an error):** Manifold is an **AMM**,
not a CLOB. There is no quoted bid/ask/size. v1 maps `probability` → `quote.last` and
`bid = ask = probability`; `bid_size/ask_size = None`. Resting limit orders
(`GET /bets?kinds=open-limit`) could deepen the book in a later phase.

**Note (MANA vs CASH):** markets now carry a `token` field. v1 treats Manifold as
play-money (fee 0); revisit if ever reading CASH (sweepstakes) markets.

---

## Kalshi

- **Base URL:** `https://external-api.kalshi.com/trade-api/v2`
- **Discovery:** `GET /markets` (`series_ticker`, `event_ticker`, `status`),
  `GET /markets/{ticker}`, `GET /markets/{ticker}/orderbook`.
- **Status enum:** initialized | inactive | active | closed | determined | disputed |
  amended | finalized. Map: active→`open`; closed/determined→`closed`; finalized→`resolved`.

**⚠ Contradiction 4 — price units.** Design doc §4/§5: "Kalshi sends integer cents →
divide to [0,1]." **Live:** the market object returns **dollar strings** —
`yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars`,
`last_price_dollars` (e.g. `"0.5600"`). Parse to float directly; **do not divide by 100.**
(Legacy integer-cent fields may persist, but the documented current fields are `*_dollars`.)

**⚠ Contradiction 7 — auth.** Design doc §5: "RSA key auth, session tokens (~30 min,
refresh)." **Live:** (a) market-data endpoints are **public** — the v1 read-only path needs
**no auth at all**; (b) authenticated (trading, deferred) requests use **per-request
RSA‑PSS signing** via `KALSHI-ACCESS-KEY` / `-SIGNATURE` / `-TIMESTAMP` headers — not an
email/password session-token flow.

**Orderbook gotcha (normalization).** `GET /markets/{ticker}/orderbook` returns **bids
only** (`yes` + `no`), as dollar strings. There are no quoted asks: a YES ask is the
complement of a NO bid → **YES ask = 1 − best NO bid**, and **YES-ask depth = NO-bid
depth**. The top-of-book `*_ask_dollars` fields on the market object are these derived
values. (Consistent with design doc §10's "top-of-book only" caveat.)

**Fee formula (derived):**
```
fee = ceil_to_centicent( multiplier × C × p × (1 − p) )
```
- `multiplier` = **0.07** general. Maker = **25% of taker**. No settlement fee.
- Max per contract at p=0.50: `0.07 × 0.25 = 0.0175` ($0.0175).

**⚠ Contradiction 5 — rounding.** Design doc §10: "rounds **up to the next whole cent**
on the aggregate order." **Live** (`/getting_started/fee_rounding`): the trade fee is
rounded **up to the centicent ($0.0001)**, applied **per fill** with an accumulator so the
total converges to a single-equivalent-fill cost. Implementation uses
`rounding_increment = 0.0001`, **not** 0.01.

**⚠ Contradiction 6 — category multipliers.** Design doc §10 / TBD #4: "some categories
carry a higher multiplier — pull the per-category table." **Live:** the current schedule
(help center + secondary sources, ~Apr 2026) appears **uniform at 0.07**; no category
premium was found. Critically, the **market object exposes no category/multiplier field**,
so any non-general multiplier must be configured **per series ticker** out-of-band
(`[venues.kalshi.series_multipliers]`). Re-confirm against `kalshi.com/docs/kalshi-fee-schedule.pdf`
(was rate-limited at fetch time; it is the canonical source).

---

## Polymarket

**Two distinct venues.** The public read endpoints we poll —
Gamma (`gamma-api.polymarket.com`) and CLOB (`clob.polymarket.com`) — serve the
**international / crypto-native** venue. The **CFTC-regulated Polymarket US DCM**
(`polymarket.us`) is a *separate* venue with its own (different) fee schedule.

**⚠ Contradiction 3 — venue confusion.** Design doc §5/§10 anchors the fee model on
"Polymarket US (the venue you'd actually trade)", but what's *readable* via the public
APIs is the **international** venue. `venue_mode` selects which fee schedule the connector
applies; it defaults to `intl` (honest about what we actually poll), with `us` a one-line
config switch.

- **Gamma `GET /markets`** (public): `?closed=false&active=true&slug=…`. Fields: `id`,
  `question`, `conditionId`, `slug`, `clobTokenIds`, `outcomes`, `outcomePrices`, `endDate`,
  `closed`, `active`, `volume`. **Gotcha:** `clobTokenIds`/`outcomes`/`outcomePrices` are
  **JSON-encoded strings** — `json.loads` them. `outcomes == ["Yes","No"]`; `clobTokenIds`
  index 0 = YES token, 1 = NO token.
- **CLOB `GET /book?token_id=<id>`** (public, `security: []`): returns
  `{bids:[{price,size}…], asks:[{price,size}…], tick_size, min_order_size, neg_risk,
  last_trade_price}`. Prices are **0–1 fraction strings**; bids desc, asks asc. Best YES
  ask = `asks[0]`. Also `/price`, `/midpoint`.

**✅ TBD #3 resolved.** Design doc TBD #3 asks to confirm public CLOB read access suffices
without a paid feed. It does: `/book` is public and returns full depth (price + size) on
both sides — and unlike Kalshi it quotes real asks, so executable size could be deepened
beyond top-of-book later.

**Fee formula (derived) — same shape on both venues:**
```
fee = feeRate × C × p × (1 − p)
```
- **intl** (Fee Structure V2, eff. 2026‑03‑30): per-category `feeRate` — crypto 0.07,
  sports 0.03, finance/politics/tech/mentions 0.04, economics/culture/weather/other 0.05,
  **geopolitics/world 0.0 (fee-free)**. Makers pay 0 (+ rebates). **Sells are exempt** from
  taker fee. Min fee 0.0001 pUSD (immaterial).
- **us** (eff. 2026‑04‑03): uniform taker `feeRate = 0.05`, maker rebate **−0.0125**. The
  `p(1−p)` parabola **self-caps at p=0.50 = $1.25 / 100 contracts** (so the stated cap is
  the natural max, not a separate clamp).

**⚠ Contradiction 1 — Polymarket US fee.** Design doc §10: `fee = max(0.001, 0.0010 ×
premium)` (flat 0.10% taker, $0.001 min, 0% maker). **Live US:** `0.05 × C × p × (1−p)`
with a −0.0125 maker rebate. The flat-percentage model is **gone**; US now uses the same
probability-weighted curve as Kalshi/intl.

**⚠ Contradiction 2 — Polymarket crypto fee.** Design doc §10: `0.0625 × p(1−p)` on "a
subset of markets" + Polygon gas. **Live intl:** Fee Structure V2 applies a per-category
`feeRate × C × p(1−p)` across most categories (crypto = **0.07**, not 0.0625), with
geopolitics fee-free; sells exempt. The `0.0625` constant and "0-fee for most" framing are
stale.

---

## Net effect on the implementation

- **All three venues now share one fee shape:** `k × C × p × (1−p)` (Manifold k=0). The
  connectors implement exactly that, with `k` resolved per venue (Kalshi multiplier,
  Polymarket per-category/uniform rate) and Kalshi adding centicent round-up.
- **`side` is the liquidity role** (`taker`/`maker`), not buy/sell — that's the axis the
  fees turn on. The edge engine always buys (crosses the spread) → passes `taker`.
  Polymarket additionally exempts `sell`.
- **v1 needs no venue auth** — every read path (Manifold reads, Kalshi market data,
  Polymarket Gamma + CLOB `/book`) is public.
