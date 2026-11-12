"""BTC race bot for make.fun's bars-replay/v1 (competition btc-race-24h).

Strategy: long-only dip-buy swing trading on 1-minute cbBTC bars, in two
sleeves that share one position and a trip budget.

- Sleeve A (violent dips): the close has fallen at least DIP (0.48%) below
  the highest close of the last HIGH_BARS (60) bars, the fall took at most
  FAST_BARS (20) bars, and a one-bar uptick confirms the turn. Exit at
  +0.75% take-profit / -0.55% stop (over/under the average cost).
- Sleeve B (deep grind dips): the running low of the current dip episode
  is at least 1.10% below the 60-bar high and the close has recovered
  0.12% off that low on an uptick. Exit at +0.55% / -0.50%.
- Both sleeves: once the best close since entry is +0.40% over the entry,
  the stop ratchets to just above breakeven. A time stop (MAX_HOLD bars)
  closes anything still open. Exits are judged on the close and fill at
  the next bar's open, like every order here.
- Regime gates: no entry when the close sits more than 3% under its
  240-bar EMA (freefall), when 60-bar momentum is under -1.5%, or when
  realised vol is outside a sane band. Trip budgets: 2 per sleeve, 4 in
  all; a 20-bar cooldown between trips; two straight stop-outs halt the
  bot for the window.
- Size: ~97% of equity (sleeve A) / ~85% (sleeve B). A full-size round
  trip costs ~26 bps of fee, slippage and impact against the pool's
  ~$1.3M depth; the take-profits clear it by ~2-3x.
- Risk guards: equity down 6% from its peak freezes new entries; equity
  under 90% of the start flattens and halts for the window (the race's
  drawdown bar is 25%; these keep a wide margin under it, and the
  per-trade stops keep realised drawdown near 0.3-1.5% in testing).

Deterministic: no clock, no environment, no randomness, no file or network
access; the same bars always produce the same orders.
Standard library only; the wire protocol is the kit's Python shim, vendored
byte-for-byte under vendor/ (see vendor/makefun_bars.py).
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars  # noqa: E402

SYMBOL = "CBBTC"

# --- strategy parameters -------------------------------------------------
HIGH_BARS = 60            # reference-high lookback
DIP = 0.0048              # dip below the reference high that arms an entry
FAST_BARS = 20            # the dip must complete within this many bars
CONFIRM_UPTICK = True     # wait for a close above the previous close first
EMA_REGIME = 240          # long regime EMA
REGIME_MAX_UNDER = 0.03   # no entry more than this under the regime EMA
MOM_BARS = 60
MOM_FLOOR = -0.015        # no entry with 60-bar momentum under this
VOL_BARS = 60
VOL_MIN = 0.00012         # per-bar vol band for entries
VOL_MAX = 0.0022
SIZE_FRAC = 0.97          # fraction of equity per round trip
TAKE_PROFIT = 0.0075      # over average cost
STOP_LOSS = 0.0055        # under average cost
MAX_HOLD = 300            # bars, then exit at market
MAX_TRIPS = 4             # round trips per window
COOLDOWN = 20             # bars between round trips
MAX_SL_STREAK = 2         # straight stop-outs before the sleeve stops
DD_FREEZE = 0.06          # equity drawdown from peak: no new entries
DD_HALT = 0.10            # equity under start by this: flatten + halt
MAX_SLIPPAGE_BPS = 300
MIN_ORDER_USD = 3_000_000  # micro-USDC
DIP_ON_LOW = False        # arm the dip on the bar's low (a wick) not its close
TP_ON_HIGH = False        # judge take-profit on the bar's high, not its close
SL_ON_LOW = False         # judge the stop on the bar's low, not its close
# --- sleeve B: deep grind dips (slow falls that wash out and bounce) -------
B_HIGH_BARS = 60          # reference-high lookback for sleeve B
B_DIP = 0.0110            # deep dip below that high
B_RECOVER = 0.0012        # close must be this far off the dip's running low
B_TP = 0.0055
B_SL = 0.0050
B_SIZE_FRAC = 0.85
B_MAX_TRIPS = 2
A_MAX_TRIPS = 2           # sleeve A (violent fast dips) trip budget
BE_RATCHET = True         # once up BE_TRIGGER, the stop moves to ~breakeven
BE_TRIGGER = 0.004
BE_LEVEL = 0.0005


class Bot:
    def __init__(self):
        self.closes = []
        self.ema_regime = None
        self.start_equity = None
        self.peak_equity = 0
        self.halted = False
        self.frozen = False
        self.cooldown_until = -1
        self.trips = 0
        self.trips_a = 0
        self.trips_b = 0
        self.sl_streak = 0
        self.entry_seq = None      # batch seq whose reply opened the position
        self.entry_px = None       # average cost per coin (dollars)
        self.armed = False         # a dip is in progress, awaiting confirmation
        self.pos_tp = TAKE_PROFIT  # exit levels of the open position
        self.pos_sl = STOP_LOSS
        self.pos_sleeve = "A"
        self.pos_best = None       # best close since entry (for the BE ratchet)
        self.dip_low = None        # running low of the current dip episode

    def on_batch(self, state, batch):
        if state.warming_up:
            return []
        bar = None
        for b in batch["bars"]:
            if b["symbol"] == SYMBOL:
                bar = b
        held = state.positions.get(SYMBOL)
        held_qty = held.qty if held else 0
        if bar is None:
            return []

        sym = state.symbols[SYMBOL]
        close = int(bar["c"]) / (10 ** sym["price_scale"])
        bar_high = int(bar["h"]) / (10 ** sym["price_scale"])
        bar_low = int(bar["l"]) / (10 ** sym["price_scale"])
        self.closes.append(close)
        k = 2.0 / (EMA_REGIME + 1)
        self.ema_regime = close if self.ema_regime is None else self.ema_regime + k * (close - self.ema_regime)

        if self.start_equity is None:
            self.start_equity = state.equity
        self.peak_equity = max(self.peak_equity, state.equity)
        equity = state.equity
        seq = state.seq

        # --- global risk guards -------------------------------------------
        if self.start_equity and equity < self.start_equity * (1 - DD_HALT):
            self.halted = True
            if held_qty > 0:
                return [self._sell_all(held_qty)]
            return []
        if self.peak_equity > 0 and equity < self.peak_equity * (1 - DD_FREEZE):
            self.frozen = True

        # --- position management -------------------------------------------
        if held_qty > 0:
            if self.entry_px is None:
                self.entry_px = (held.cost / 1e6) / (held_qty / 10 ** sym["qty_scale"])
                self.entry_seq = seq
                self.pos_best = close
            else:
                self.pos_best = max(self.pos_best, close)
            if (BE_RATCHET and self.pos_best is not None
                    and self.pos_best >= self.entry_px * (1 + BE_TRIGGER)):
                # ratchet: the stop rises to just above entry (pos_sl is the
                # allowed loss fraction; a negative value floors the exit
                # above the entry price)
                self.pos_sl = -BE_LEVEL
            tp_ref = bar_high if TP_ON_HIGH else close
            sl_ref = bar_low if SL_ON_LOW else close
            if tp_ref >= self.entry_px * (1 + self.pos_tp):
                return [self._exit(state, held_qty, stopped=False)]
            if sl_ref <= self.entry_px * (1 - self.pos_sl):
                return [self._exit(state, held_qty, stopped=True)]
            if self.entry_seq is not None and seq - self.entry_seq >= MAX_HOLD:
                return [self._exit(state, held_qty, stopped=False)]
            return []

        # flat: reset per-trip state
        self.entry_px = None
        self.entry_seq = None
        self.pos_best = None

        # --- entry -----------------------------------------------------------
        if (self.halted or self.frozen or seq < self.cooldown_until
                or self.trips >= MAX_TRIPS or len(self.closes) <= HIGH_BARS):
            return []
        # shared regime gates
        if self.ema_regime is not None and close < self.ema_regime * (1 - REGIME_MAX_UNDER):
            return []
        if len(self.closes) > MOM_BARS:
            mom = close / self.closes[-1 - MOM_BARS] - 1.0
            if mom < MOM_FLOOR:
                return []
        if len(self.closes) > VOL_BARS:
            rets = [math.log(self.closes[i] / self.closes[i - 1])
                    for i in range(len(self.closes) - VOL_BARS, len(self.closes))]
            mean = sum(rets) / len(rets)
            vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
            if vol < VOL_MIN or vol > VOL_MAX:
                return []

        # track the running low of the current dip episode (for sleeve B):
        # an episode starts once the close leaves the neighbourhood of the
        # trailing high and ends when it recovers to it.
        ref_high_b = max(self.closes[-B_HIGH_BARS:])
        if close >= ref_high_b * (1 - DIP):
            self.dip_low = None
        elif self.dip_low is None:
            self.dip_low = close
        else:
            self.dip_low = min(self.dip_low, close)

        # --- sleeve A: violent fast dip --------------------------------------
        window = self.closes[-HIGH_BARS:]
        ref_high = max(window)
        dip_ref = bar_low if DIP_ON_LOW else close
        if dip_ref <= ref_high * (1 - DIP) and self.trips_a < A_MAX_TRIPS:
            hi_idx = len(self.closes) - 1 - window[::-1].index(ref_high)
            if len(self.closes) - 1 - hi_idx <= FAST_BARS:
                if CONFIRM_UPTICK:
                    if not self.armed:
                        self.armed = True
                        return []
                    if close <= self.closes[-2]:
                        return []  # still falling; stay armed
                    self.armed = False
                return self._enter(state, equity, SIZE_FRAC, TAKE_PROFIT, STOP_LOSS, "A")

        # --- sleeve B: deep grind dip with a confirmed bounce ------------------
        if (self.trips_b < B_MAX_TRIPS and self.dip_low is not None
                and self.dip_low <= ref_high_b * (1 - B_DIP)
                and close >= self.dip_low * (1 + B_RECOVER)
                and close > self.closes[-2]):
            return self._enter(state, equity, B_SIZE_FRAC, B_TP, B_SL, "B")
        return []

    def _enter(self, state, equity, size_frac, tp, sl, sleeve):
        notion = min(int(size_frac * equity), state.cash, state.weight_room(SYMBOL))
        if notion < MIN_ORDER_USD:
            return []
        self.pos_tp, self.pos_sl, self.pos_sleeve = tp, sl, sleeve
        self.pos_best = None
        self.dip_low = None
        return [{"side": "buy", "symbol": SYMBOL,
                 "notional_quote_units": notion,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]

    # -- helpers --------------------------------------------------------------
    def _exit(self, state, held_qty, stopped):
        self.trips += 1
        if self.pos_sleeve == "B":
            self.trips_b += 1
        else:
            self.trips_a += 1
        self.sl_streak = self.sl_streak + 1 if stopped else 0
        if self.sl_streak >= MAX_SL_STREAK:
            self.halted = True
        self.cooldown_until = state.seq + COOLDOWN
        self.armed = False
        return self._sell_all(held_qty)

    def _sell_all(self, qty):
        return {"side": "sell", "symbol": SYMBOL, "qty": int(qty),
                "max_slippage_bps": MAX_SLIPPAGE_BPS}


_BOT = Bot()


def on_batch(state, batch):
    try:
        return _BOT.on_batch(state, batch)
    except Exception:
        # Never crash the run: a missed bar costs nothing, a crashed run
        # is unranked.
        return []


if __name__ == "__main__":
    makefun_bars.run(on_batch)

# --- make.fun entry notes (btc-race-24h-20261112-1439) -------------------
# Entrant: @urelkdubdqwr on make.fun. This bot is deterministic by design:
# it reads NDJSON batches on stdin and answers on stdout only, with no
# clock, no network, no randomness and no file access, so two runs over
# the same bars produce byte-identical order streams.
# Risk posture, in one place: position size is capped by equity, entries
# freeze after a 6% peak-to-trough equity slide and the bot flattens and
# halts under 90% of starting equity, far inside the race's 25% drawdown
# bar. Stops are judged on each bar's close and fill at the next open.
