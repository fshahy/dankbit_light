# -*- coding: utf-8 -*-
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np

# ✅ Server-safe Matplotlib (NO pyplot!)
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
from matplotlib.ticker import MultipleLocator

from . import gamma as gamma_lib

# Every "trades since 00:00" trade window in this addon (dankbit.bands.
# _compute_asset's default hours=None window, the chat tools) resets at UTC
# midnight. This briefly reset at Iran (Asia/Tehran)
# midnight instead — confirmed at the time against Thales's own numbers,
# whose R/S (high_resistance/low_support) only matched ours under that
# zone — but Thales dev has since asked to revert this back to UTC
# midnight, so this is UTC-anchored again.


def day_window_start(as_of):
    """The most recent UTC midnight at or before `as_of`, returned as a
    naive UTC datetime — the actual day-boundary reference behind every
    "trades since 00:00" window in this addon (see comment above).
    `as_of` may be naive (assumed UTC, matching the convention every caller
    already uses, e.g. dankbit.bands._compute_asset's own `as_of`) or
    tz-aware."""
    as_of_utc = as_of.replace(tzinfo=timezone.utc) if as_of.tzinfo is None else as_of.astimezone(timezone.utc)
    return as_of_utc.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


class OptionStrat:
    def __init__(self, name, S0, from_price, to_price, step):
        self.name = name
        self.S0 = S0
        self.STs = np.arange(from_price, to_price, step, dtype=np.float64)
        self.payoffs = np.zeros_like(self.STs, dtype=np.float64)

    def long_call(self, K, C, Q=1):
        self.payoffs += (np.maximum(self.STs - K, 0) - C) * Q

    def short_call(self, K, C, Q=1):
        self.payoffs += (-np.maximum(self.STs - K, 0) + C) * Q

    def long_put(self, K, P, Q=1):
        self.payoffs += (np.maximum(K - self.STs, 0) - P) * Q

    def short_put(self, K, P, Q=1):
        self.payoffs += (-np.maximum(K - self.STs, 0) + P) * Q

    # =========================================================
    # BASELINE PLOT — SERVER-SAFE (NO pyplot), EXPLICIT FIG/AXES
    # =========================================================
    def plot(
        self,
        index_price,
        market_delta,
        market_gammas,
        show_red_line,
        title="-",
        width=18,
        height=8,
    ):
        # ✅ Create a fresh figure per request (no global state)
        fig = Figure(figsize=(width, height), dpi=120)
        FigureCanvas(fig)  # attach Agg canvas so fig.canvas.draw() works reliably
        ax = fig.add_subplot(111)

        # Rotate x tick labels (pyplot-free)
        ax.tick_params(axis="x", labelrotation=90)

        # Tick spacing
        if self.name.startswith("BTC"):
            ax.xaxis.set_major_locator(MultipleLocator(1000))
        elif self.name.startswith("ETH"):
            ax.xaxis.set_major_locator(MultipleLocator(100))

        ax.grid(True)

        md = np.asarray(market_delta, dtype=float) if market_delta is not None else np.zeros_like(self.STs)
        mg = np.asarray(market_gammas, dtype=float) if market_gammas is not None else np.zeros_like(self.STs)

        # Resample if sizes mismatch
        if md.size != self.STs.size:
            md = np.interp(self.STs, np.linspace(self.STs.min(), self.STs.max(), md.size), md)
        if mg.size != self.STs.size:
            mg = np.interp(self.STs, np.linspace(self.STs.min(), self.STs.max(), mg.size), mg)

        pnl_curve = self.payoffs
        delta_curve = md
        gamma_curve = mg
        pnl_label = "Taker P&L"

        # Clip x-range so nothing from outside the domain “sticks”
        ax.set_xlim(float(self.STs.min()), float(self.STs.max()))

        # =====================================================
        # DELTA AXIS (PRIMARY) — ZERO CENTERED
        # =====================================================
        ax.plot(self.STs, delta_curve, color="green", label="Delta")

        if np.any(np.isfinite(delta_curve)):
            dmax = float(np.max(np.abs(delta_curve)))
            if dmax > 0:
                ax.set_ylim(-dmax, dmax)

        ax.axhline(0, color="black", linewidth=1)
        ax.axvline(x=index_price, color="blue")
        ax.tick_params(axis="y", labelcolor="green")

        # =====================================================
        # GAMMA AXIS (SECONDARY) — ZERO CENTERED
        # =====================================================
        axg = ax.twinx()
        axg.plot(
            self.STs,
            gamma_curve,
            color="violet",
            linewidth=2.0,
            alpha=0.9,
            label="Gamma",
        )
        axg.tick_params(axis="y", labelcolor="violet")

        if np.any(np.isfinite(gamma_curve)):
            gmax = float(np.max(np.abs(gamma_curve)))
            if gmax > 0:
                axg.set_ylim(-gmax, gmax)

        axg.fill_between(
            self.STs,
            gamma_curve,
            0,
            where=(gamma_curve < 0),
            color="violet",
            alpha=0.25,
            interpolate=True,
        )

        # =====================================================
        # P&L AXIS (THIRD) — ZERO CENTERED
        # =====================================================
        axp = None
        if show_red_line:
            axp = ax.twinx()
            axp.spines["right"].set_position(("outward", 60))
            axp.plot(self.STs, pnl_curve, color="red", label=pnl_label)
            axp.tick_params(axis="y", labelcolor="red")

            if np.any(np.isfinite(pnl_curve)):
                pmax = float(np.max(np.abs(pnl_curve)))
                if pmax > 0:
                    axp.set_ylim(-pmax, pmax)

        # axg (and axp, if present) are twin axes created after ax, so by
        # default they stack visually on top of ax — washing out anything
        # drawn on ax afterward (e.g. the caller's axvline/text overlays)
        # wherever their alpha-blended fills overlap. Bring ax back to the
        # top of the stack and hide its opaque background so axg/axp still
        # show through everywhere else.
        top_zorder = max(a.get_zorder() for a in (axg, axp) if a is not None)
        ax.set_zorder(top_zorder + 1)
        ax.patch.set_visible(False)

        now = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M")
        ax.set_title(f"{self.name} | {now} UTC | {title}")
        ax.set_xlabel(f"${self.S0:,.0f}", fontsize=10, color="blue")

        # Combine legends from all axes
        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = axg.get_legend_handles_labels()
        h = h1 + h2
        l = l1 + l2
        if show_red_line and axp is not None:
            h3, l3 = axp.get_legend_handles_labels()
            h += h3
            l += l3

        # ax.legend(h, l, loc="upper right", framealpha=0.85)

        # self.add_dankbit_signature(ax)
        return fig, ax


def find_zero_crossings(STs, curve):
    """Return the list of prices where `curve` crosses zero, via linear
    interpolation on each sign change (STs must be ascending). Same technique
    used by the delta=0 finders elsewhere in this codebase, and by
    build_zone_curves()'s own Longs-vs-Shorts crossing below."""
    crossings = []
    for i in range(len(curve) - 1):
        a, b = curve[i], curve[i + 1]
        if not (np.isfinite(a) and np.isfinite(b)):
            continue
        if a * b < 0:
            px = float(STs[i] - a * (STs[i + 1] - STs[i]) / (b - a))
            crossings.append(px)
    return crossings


# A zone narrower than this share of index price (e.g. a single-curve
# zone drawn as one line) is widened to it, centered, before measuring
# its gamma share — otherwise a one-price zone always scores 0%.
ZONE_STRENGTH_MIN_WIDTH_PCT = 0.005


def zone_gamma_shares(trades, index_price, ranges, r=0.0, points=600):
    """Strength of each zone box: the share (0..1) of the trades' gross
    dollar gamma (|long gamma| + |short gamma|, i.e. every trade counted
    as positive) whose area lies inside that zone's price range.

    `ranges` is a list of (min, max) pairs, 0.0 meaning absent (same
    sentinel dankbit.bands uses); an absent zone gets None. The total is
    taken over index price ±20%, widened to cover every zone, so a zone
    far from the money can't look strong just because the curve was
    clipped. Shares are independent — overlapping zones (the Middle Zone
    often sits between the other two) don't sum to 1. Returns all None
    when there's no gamma at all."""
    present = [p for rng in ranges for p in rng if p and p > 0]
    if not index_price or not trades:
        return [None] * len(ranges)
    lo = min([index_price * 0.8] + present)
    hi = max([index_price * 1.2] + present)
    S = np.linspace(lo, hi, points)
    longs = trades.filtered(lambda t: t.direction == "buy")
    shorts = trades.filtered(lambda t: t.direction == "sell")
    gross = np.abs(gamma_lib.portfolio_gamma(S, longs, r)) + np.abs(gamma_lib.portfolio_gamma(S, shorts, r))
    total = float(np.trapz(gross, S))
    if not np.isfinite(total) or total <= 0:
        return [None] * len(ranges)

    min_width = index_price * ZONE_STRENGTH_MIN_WIDTH_PCT
    shares = []
    for a, b in ranges:
        vals = [p for p in (a, b) if p and p > 0]
        if not vals:
            shares.append(None)
            continue
        z_lo, z_hi = min(vals), max(vals)
        if z_hi - z_lo < min_width:
            mid = (z_lo + z_hi) / 2.0
            z_lo, z_hi = mid - min_width / 2.0, mid + min_width / 2.0
        xs = np.linspace(max(z_lo, lo), min(z_hi, hi), 100)
        area = float(np.trapz(np.interp(xs, S, gross), xs))
        shares.append(min(1.0, max(0.0, area / total)))
    return shares


# Which numpy extremum-finder each leg's gamma curve uses — long positions
# carry positive gamma (peak, argmax), short positions negative gamma
# (trough, argmin).
_GAMMA_VEGA_ARGFN = {"long_call": np.argmax, "long_put": np.argmax, "short_call": np.argmin, "short_put": np.argmin}


def per_leg_gamma(STs, trades, r=0.0):
    """For each of the 4 legs split out of `trades` (long_call, long_put,
    short_call, short_put — buy/sell x call/put), where that leg's own
    portfolio dollar-gamma curve peaks (long) or bottoms out (short).
    Returns {leg_name: {"trades", "gamma_price", "gamma_value"}}, raw and
    unscaled. A leg with zero trades has no curve at all: gamma_price None,
    gamma_value 0.0 (np.argmax/argmin on an all-zero curve would otherwise
    report STs[0], the price-range floor, as a fake extremum). Used by
    four_leg_gamma_json/ll_dominant_legs (main.py) and the chat tools.
    `trades` should already be filtered to one expiry/time window."""
    legs = {
        "long_call": trades.filtered(lambda t: t.direction == "buy" and t.option_type == "call"),
        "long_put": trades.filtered(lambda t: t.direction == "buy" and t.option_type == "put"),
        "short_call": trades.filtered(lambda t: t.direction == "sell" and t.option_type == "call"),
        "short_put": trades.filtered(lambda t: t.direction == "sell" and t.option_type == "put"),
    }

    result = {}
    for leg_name, leg_trades in legs.items():
        if not leg_trades:
            result[leg_name] = {"trades": leg_trades, "gamma_price": None, "gamma_value": 0.0}
            continue

        gamma_curve = gamma_lib.portfolio_gamma(STs, leg_trades, r=r)
        gamma_idx = int(_GAMMA_VEGA_ARGFN[leg_name](gamma_curve))
        result[leg_name] = {
            "trades": leg_trades,
            "gamma_price": float(STs[gamma_idx]),
            "gamma_value": float(gamma_curve[gamma_idx]),
        }
    return result


def split_open_interest(oi_map, instrument):
    """Splits dankbit.trade.get_open_interest_by_currency()'s
    {full_instrument_name: open_interest} map down to one expiry's own
    ({strike: call_oi}, {strike: put_oi}) — the input max_pain_price()
    expects. Same anchored left-prefix match every other instrument
    lookup in this addon uses (f"{instrument}-" — oi_map's keys span every
    expiry for the asset, so this can't pull in another expiry's
    strikes). Shared by four_leg_gamma_json (main.py) and the chat
    panel's max_pain tool (chat_tools.py) so the two can't disagree."""
    prefix = f"{instrument}-"
    call_oi, put_oi = {}, {}
    for oi_instrument, oi in (oi_map or {}).items():
        if not oi or not oi_instrument.startswith(prefix):
            continue
        parts = oi_instrument.split("-")
        if len(parts) != 4:
            continue
        try:
            strike = int(parts[2])
        except ValueError:
            continue
        if parts[3] == "C":
            call_oi[strike] = call_oi.get(strike, 0.0) + oi
        elif parts[3] == "P":
            put_oi[strike] = put_oi.get(strike, 0.0) + oi
    return call_oi, put_oi


def max_pain_price(call_oi, put_oi):
    """Standard "Max Pain" computation for a single expiry: the strike at
    which the aggregate intrinsic-value payout option WRITERS would owe
    option HOLDERS at settlement is smallest — i.e. the price where option
    buyers collectively lose the most and sellers collectively pay out the
    least. Candidate settlement prices are restricted to the expiry's own
    actual listed strikes (the union of call_oi's and put_oi's keys), the
    standard Max Pain convention, rather than a continuous price grid —
    unlike every Greek curve elsewhere in this addon (delta.py/gamma.py/
    etc.), which evaluate over a synthetic STs grid.

    call_oi/put_oi: {strike: open_interest} dicts — current open interest
    (contracts outstanding), NOT trade volume/count, since Max Pain is
    about existing positions at expiration, not recent trade flow. Sourced
    by the caller from dankbit.trade.get_open_interest_by_currency(),
    filtered down to one expiry's own strikes. May be sparse, only-partially
    overlapping (a strike traded as a call but never as a put, or vice
    versa), or both empty.

    For each candidate strike S, a call struck at K owes
    OI * max(S - K, 0) and a put struck at K owes OI * max(K - S, 0) —
    the same combined-portfolio intrinsic-value formula chart_png_zones'
    own Longs/Shorts payoff curves use per-leg, just aggregated by open
    interest here instead of per-trade signed amount.

    Returns (price, total_payout_by_strike) — price is a plain float
    (never a synthetic price-grid-edge value), or (None, {}) with no
    usable OI on either side at all (this addon's usual "no curve, no
    fake price" convention, same as per_leg_gamma()'s None gamma price
    for an empty leg)."""
    strikes = sorted(set(k for k, oi in call_oi.items() if oi) | set(k for k, oi in put_oi.items() if oi))
    if not strikes:
        return None, {}

    total_payout = {}
    for candidate in strikes:
        payout = 0.0
        for strike, oi in call_oi.items():
            if oi:
                payout += oi * max(candidate - strike, 0.0)
        for strike, oi in put_oi.items():
            if oi:
                payout += oi * max(strike - candidate, 0.0)
        total_payout[candidate] = payout

    best_strike = min(total_payout, key=total_payout.get)
    return float(best_strike), total_payout


def build_zone_curves(instrument_name, index_price, trades, from_price, to_price, steps):
    """Build the Longs/Shorts OptionStrat curves from `trades` (any iterable of
    objects with .direction/.option_type/.strike/.price/.index_price — an Odoo
    recordset works directly), then re-center on the crossing-based zoom
    exactly as the live /<instrument>/zones PNG chart does: ±$2000 for BTC,
    ±$100 for ETH (ETH's much smaller price scale made the ±$2000 margin blow
    out the auto-zoom). Falls back to the wide [from_price, to_price] range
    if the curves never cross.

    Shared by the /<instrument>/zones route (controllers/main.py) and
    dankbit.bands's cron (models/bands.py) so the two can
    never compute different extrema for the same trades — one implementation,
    not two copies that could quietly drift apart."""
    def build(fp, tp, st):
        longs = OptionStrat(instrument_name, index_price, fp, tp, st)
        shorts = OptionStrat(instrument_name, index_price, fp, tp, st)
        for trade in trades:
            if trade.direction == "buy":
                if trade.option_type == "call":
                    longs.long_call(trade.strike, trade.price * trade.index_price)
                elif trade.option_type == "put":
                    longs.long_put(trade.strike, trade.price * trade.index_price)
            elif trade.direction == "sell":
                if trade.option_type == "call":
                    shorts.short_call(trade.strike, trade.price * trade.index_price)
                elif trade.option_type == "put":
                    shorts.short_put(trade.strike, trade.price * trade.index_price)
        return longs, shorts

    longs_obj, shorts_obj = build(from_price, to_price, steps)

    STs = longs_obj.STs
    diff = longs_obj.payoffs - shorts_obj.payoffs
    crossings = find_zero_crossings(STs, diff)

    if crossings:
        if instrument_name.startswith("ETH"):
            margin_below, margin_above = 100, 100
        else:
            margin_below, margin_above = 2000, 2000
        zoom_from = min(crossings) - margin_below
        zoom_to = max(crossings) + margin_above
        longs_obj, shorts_obj = build(zoom_from, zoom_to, steps)

    return longs_obj, shorts_obj

