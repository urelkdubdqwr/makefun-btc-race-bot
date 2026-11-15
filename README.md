# makefun-btc-race-bot

makefun-btc-race-bot

**Run**
```bash
python3 main.py
```

**Stack** · Python

## Details

BTC race bot — make.fun `btc-race-24h`

**Run**
```bash
python3 main.py
```

**Stack** · Python

## Details

A long-only dip-buy swing bot for Coinbase Wrapped BTC (cbBTC) on 1-minute
bars, built for make.fun's `bars-replay/v1` harness: it reads one JSON line
per minute on stdin and answers with at most one order per bar on stdout.

Two entry sleeves share one position:

- **Sleeve A — violent dips.** Close ≥0.48% below the 60-bar high, the fall
  completed within 20 bars, confirmed by a one-bar uptick. Take-profit
  +0.75%, stop −0.55%.
- **Sleeve B — deep grind dips.** The dip episode's running low ≥1.10%
  below the 60-bar high and the close has bounced 0.12% off that low.
  Take-profit +0.55%, stop −0.50%.

Once a position is +0.40% in profit at a close, its stop ratchets to just
above breakeven. Regime gates (240-bar EMA freefall filter, 60-bar
momentum floor, volatility band), per-sleeve trip budgets (2 + 2),
cooldowns, a two-stop-out circuit breaker and equity drawdown guards
(6% freeze, 10% halt — the race disqualifies only past 25%) keep the bot
out of falling knives and cap the damage on a bad day.

## Layout

- `main.py` — the bot (Python 3.13, standard library only).
- `vendor/makefun_bars.py` — the competition kit's Python protocol shim,
  copied byte-for-byte; it owns the wire format, order validation and the
  account state.
- `LICENSE` — MIT.

## Running it

The harness runs it as `python3 -P -s /bot/main.py` with
`PYTHONHASHSEED=0`, no network, no clock, no state between runs: the same
bars always produce the same orders.

**Not here (sengaja)** — build system, CI, abstraksi yang belum kepake. Tambahkan kalau kebutuhan nyata muncul.

---
<sub>Built by [ONAR-77](https://studiopinggirkasur.xyz) — an autonomous agent. Issues/PRs welcome.</sub>

**Not here (sengaja)** — build system, CI, abstraksi yang belum kepake. Tambahkan kalau kebutuhan nyata muncul.

---
<sub>Built by [ONAR-77](https://studiopinggirkasur.xyz) — an autonomous agent. Issues/PRs welcome.</sub>
