import base64
import json
import numpy as np
from datetime import datetime, timedelta, timezone
from io import BytesIO
from matplotlib import transforms as mtransforms
from odoo import http
from odoo.http import request
from . import options
from . import delta
from . import gamma

# Trailing-hours values /api/four-leg-gamma accepts (/4l sends
# 1/2/3/4/8/24/48/72 or "all").
FOUR_LEG_WINDOW_HOURS_CHOICES = tuple(range(1, 73)) + (96, 120, 144, 168, 192, 216, 240, 480, 720)

# Fallback window when `?hours=` is missing/malformed on
# /api/four-leg-gamma and /api/ll-gamma (the pages always send one).
FOUR_LEG_DEFAULT_WINDOW_HOURS = 24
# /ll/<asset> (the LL chart — ll_avg_gamma_json) computes each active
# expiry's own dominant leg (the present leg with the biggest |gamma
# value|) and draws one line per expiry that has usable trades. It has no
# "Expiry" dropdown — every active expiry is considered on every poll,
# each an isolated options.per_leg_gamma() curve build, so the loop is
# capped at the soonest N expiries to bound worst-case cost (Deribit
# realistically lists ~18-21 active BTC/ETH expiries at once, and the
# far-dated ones carry negligible flow anyway).
LL_MAX_EXPIRIES = 24

# /ll/<asset>'s own "Window" control is a free-form number + Hours/Days
# unit picker (not a fixed dropdown, unlike /4l/<asset>'s own Window
# select), so ll_avg_gamma_json's `?hours=` no longer validates against
# the discrete FOUR_LEG_WINDOW_HOURS_CHOICES set (silently substituting
# FOUR_LEG_DEFAULT_WINDOW_HOURS for anything off that list would make a
# typed-in value like "100" hours silently render as 24h) — any positive
# integer is accepted and just clamped into 1..LL_MAX_WINDOW_HOURS
# instead. 8760h = 1 year, comfortably past this addon's own ~9-month
# trade retention (see _delete_expired_trades), so the clamp only ever
# guards against a malformed/absurd value, not a legitimate one.
LL_MAX_WINDOW_HOURS = 8760

# /ll/BTC's "Other Expiries" cutoff, in millions of |gamma| (ETH stays 100M,
# hardcoded in the template).
LL_OTHER_LINES_MIN_GAMMA_DEFAULT = 500


def ll_other_lines_min_gamma(env):
    try:
        return float(env["ir.config_parameter"].sudo().get_param(
            "dankbit.ll_other_lines_min_gamma", LL_OTHER_LINES_MIN_GAMMA_DEFAULT))
    except (TypeError, ValueError):
        return float(LL_OTHER_LINES_MIN_GAMMA_DEFAULT)


_LL_LEG_CODES = {"long_call": "LC", "long_put": "LP", "short_call": "SC", "short_put": "SP"}


def ll_dominant_legs(env, asset, STs, as_of, window_start, historical=False):
    """Each of the soonest LL_MAX_EXPIRIES expiries' own dominant leg (the
    present leg with the largest |gamma value|, via options.per_leg_gamma())
    over that expiry's trades in [window_start, as_of], sorted by
    |gamma value| descending — what /ll/<asset> draws
    (ll_avg_gamma_json).
    `historical` switches the active-expiry lookup to
    _distinct_expirations_asof() (expiries that had already traded as of
    that moment)."""
    bands_model = env["dankbit.bands"]
    if historical:
        expirations = bands_model._distinct_expirations_asof(asset, as_of, LL_MAX_EXPIRIES)
    else:
        expirations = bands_model._distinct_expirations(asset, as_of, LL_MAX_EXPIRIES)

    trade_model = env["dankbit.trade"].with_context(active_test=False)
    expiries = []
    for exp in expirations:
        instrument = bands_model._format_instrument(asset, exp)
        domain = [
            ("name", "=ilike", f"{instrument}-%"),
            ("iv", "!=", 0), ("deribit_ts", "<=", as_of),
        ]
        if window_start is not None:
            domain.append(("deribit_ts", ">=", window_start))
        trades = trade_model.search(domain)
        if not trades:
            continue

        legs = options.per_leg_gamma(STs, trades)
        present = [
            (_LL_LEG_CODES[name], leg["gamma_price"], leg["gamma_value"])
            for name, leg in legs.items()
            if leg["gamma_price"] is not None
        ]
        if not present:
            continue

        dominant_code, dominant_price, dominant_value = max(present, key=lambda t: abs(t[2]))
        expiries.append({
            "instrument": instrument,
            "expiration": int(exp.replace(tzinfo=timezone.utc).timestamp() * 1000),
            "dominant_price": dominant_price,
            "dominant_value": dominant_value,
            "abs_value": abs(dominant_value),
            "dominant_leg": dominant_code,
            "trade_count": len(trades),
        })

    expiries.sort(key=lambda e: e["abs_value"], reverse=True)
    return expiries


class _AggTrade:
    """SQL-aggregated trade row — duck-typed for portfolio_delta/gamma."""
    __slots__ = ("strike", "option_type", "direction", "amount", "iv", "_expiration")

    def __init__(self, strike, option_type, direction, expiration, amount, iv):
        self.strike = strike
        self.option_type = option_type
        self.direction = direction
        self.amount = amount
        self.iv = iv
        self._expiration = expiration

    def get_hours_to_expiry(self):
        if not self._expiration:
            return 0.0
        now = datetime.now(timezone.utc)
        exp = self._expiration if self._expiration.tzinfo else self._expiration.replace(tzinfo=timezone.utc)
        return max((exp - now).total_seconds() / 3600.0, 0.0)



class ChartController(http.Controller):
    def find_gamma_peaks(self, STs, gamma_curve, min_fraction=0.15):
        STs = np.asarray(STs, dtype=float)
        g = np.asarray(gamma_curve, dtype=float)

        if g.size < 3:
            return []

        finite = np.isfinite(g)
        if not np.any(finite):
            return []

        g_max = np.max(np.abs(g[finite]))
        if g_max == 0:
            return []

        threshold = min_fraction * g_max
        extrema = []

        for i in range(1, len(g) - 1):
            if not np.isfinite(g[i]):
                continue
            if g[i] > g[i - 1] and g[i] > g[i + 1] and g[i] > threshold:
                extrema.append((float(STs[i]), float(g[i])))

        return extrema

    def find_gamma_bottoms(self, STs, gamma_curve, min_fraction=0.15):
        STs = np.asarray(STs, dtype=float)
        g = np.asarray(gamma_curve, dtype=float)

        if g.size < 3:
            return []

        finite = np.isfinite(g)
        if not np.any(finite):
            return []

        g_max = np.max(np.abs(g[finite]))
        if g_max == 0:
            return []

        threshold = min_fraction * g_max
        extrema = []

        for i in range(1, len(g) - 1):
            if not np.isfinite(g[i]):
                continue
            if g[i] < g[i - 1] and g[i] < g[i + 1] and g[i] < -threshold:
                extrema.append((float(STs[i]), float(g[i])))

        return extrema

    @http.route("/<dankbit_symbol:instrument>", type="http", auth="user", website=True)
    def chart_png_all(self, instrument):
        icp = request.env["ir.config_parameter"].sudo()

        from_price = 0
        to_price = 1000
        steps = 1
        if instrument.startswith("BTC"):
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        if instrument.startswith("ETH"):
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))

        cr = request.env.cr
        cr.execute("""
            SELECT
                strike,
                option_type,
                direction,
                expiration,
                SUM(amount)                                AS total_amount,
                SUM(iv * amount) / NULLIF(SUM(amount), 0) AS weighted_iv,
                COUNT(*)                                   AS trade_count
            FROM dankbit_trade
            WHERE name ILIKE %s
              AND expiration >= NOW()
              AND active = TRUE
              AND iv <> 0
            GROUP BY strike, option_type, direction, expiration
        """, (f'%{instrument}%',))
        rows = cr.fetchall()

        agg_trades = [
            _AggTrade(
                strike=row[0],
                option_type=row[1],
                direction=row[2],
                expiration=row[3],
                amount=float(row[4]),
                iv=float(row[5] or 0.01),
            )
            for row in rows
        ]
        trade_count = sum(int(row[6]) for row in rows)

        index_price = request.env["dankbit.trade"].get_index_price(instrument)
        obj = options.OptionStrat(instrument, index_price, from_price, to_price, steps)

        STs = np.arange(from_price, to_price, steps)
        market_deltas = delta.portfolio_delta(STs, agg_trades, 0.05)
        market_gammas = gamma.portfolio_gamma(STs, agg_trades, 0.05)
        fig, ax = obj.plot(index_price,
                           market_deltas,
                           market_gammas,
                           False,
                           title="Structure",
                           width=18,
                           height=8)

        trans = mtransforms.blended_transform_factory(ax.transData, ax.transAxes)
        d_arr = np.asarray(market_deltas, dtype=float)
        g_arr = np.asarray(market_gammas, dtype=float)
        d_lim = float(np.max(np.abs(d_arr[np.isfinite(d_arr)]))) if np.any(np.isfinite(d_arr)) else 1.0
        g_lim = float(np.max(np.abs(g_arr[np.isfinite(g_arr)]))) if np.any(np.isfinite(g_arr)) else 1.0

        for px, gval in self.find_gamma_peaks(STs, market_gammas):
            ax.axvline(x=px, color="black", linewidth=1.2, linestyle="--", alpha=0.8)

            # normalised positions of gamma and delta at this x (0=bottom, 1=top of axes)
            g_norm = 0.5 + 0.5 * (gval / g_lim) if g_lim else 0.5
            d_val = float(np.interp(px, STs, d_arr)) if STs.size else 0.0
            d_norm = 0.5 + 0.5 * (d_val / d_lim) if d_lim else 0.5

            # pick the y fraction furthest from both curves
            occupied_top = max(g_norm, d_norm)
            occupied_bot = min(g_norm, d_norm)
            y = 0.04 if (1.0 - occupied_top) < (occupied_bot - 0.0) else 0.96

            ax.text(px, y, f"${px:,.0f}", transform=trans, color="black",
                    fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                    rotation=90)

        for px, gval in self.find_gamma_bottoms(STs, market_gammas):
            ax.axvline(x=px, color="black", linewidth=1.2, linestyle="--", alpha=0.8)

            # normalised positions of gamma and delta at this x (0=bottom, 1=top of axes)
            g_norm = 0.5 + 0.5 * (gval / g_lim) if g_lim else 0.5
            d_val = float(np.interp(px, STs, d_arr)) if STs.size else 0.0
            d_norm = 0.5 + 0.5 * (d_val / d_lim) if d_lim else 0.5

            # pick the y fraction furthest from both curves
            occupied_top = max(g_norm, d_norm)
            occupied_bot = min(g_norm, d_norm)
            y = 0.04 if (1.0 - occupied_top) < (occupied_bot - 0.0) else 0.96

            ax.text(px, y, f"${px:,.0f}", transform=trans, color="black",
                    fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                    rotation=90)

        for i in range(len(d_arr) - 1):
            if not (np.isfinite(d_arr[i]) and np.isfinite(d_arr[i + 1])):
                continue
            if d_arr[i] * d_arr[i + 1] < 0:
                px = float(STs[i] - d_arr[i] * (STs[i + 1] - STs[i]) / (d_arr[i + 1] - d_arr[i]))
                demand = d_arr[i] > 0
                color = "red" if demand else "green"
                ax.axvline(x=px, color=color, linewidth=1.2, linestyle="-", alpha=0.8)
                g_norm = 0.5 + 0.5 * (float(np.interp(px, STs, g_arr)) / g_lim) if g_lim else 0.5
                y = 0.04 if g_norm > 0.5 else 0.96
                ax.text(px, y, f"${px:,.0f}", transform=trans, color=color,
                        fontsize=9, ha="right", va="top" if y > 0.5 else "bottom",
                        rotation=90)

        last_trade = request.env["dankbit.trade"].get_last_trade(instrument)
        last_ts = last_trade.deribit_ts.strftime('%Y-%m-%d %H:%M') if last_trade else "—"
        ax.text(
            0.01, 0.04,
            f"{trade_count} Trades",
            transform=ax.transAxes,
            fontsize=14,
        )
        ax.text(
            0.01, 0.01,
            f"Last trade: {last_ts}",
            transform=ax.transAxes,
            fontsize=14,
        )

        buf = BytesIO()
        fig.savefig(buf, format="png")
        del fig

        buf.seek(0)
        image_b64 = base64.b64encode(buf.read()).decode("ascii")
        return request.render(
            "dankbit.dankbit_page",
            {
                "plot_name": "All",
                "plot_title": f"{instrument} - All",
                "refresh_interval": refresh_interval*5,
                "image_b64": image_b64,
            }
        )


    # ------------------------------------------------------------------
    # JSON API endpoints
    # ------------------------------------------------------------------

    @http.route("/api/last-trade/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def last_trade_json(self, asset):
        """The most recent trade's own deribit_ts for `asset`, asset-wide
        (dankbit.trade.get_last_trade()'s plain `ilike` match, not
        restricted to any one instrument/expiry) — a cheap standalone
        lookup, no curve-building, feeding the TradingView footer's
        "Last trade:" text so a stalled dankbit_ws ingestion service (its
        DB connection can die without crashing the container, silently
        dropping every trade — see dankbit_ws_batch.py) shows up as a
        growing gap between this timestamp and now, at a glance, without
        needing to check the container logs or query the DB directly.
        `last_trade_ts` is `None` if this asset has no trades at all."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )
        last_trade = request.env["dankbit.trade"].get_last_trade(asset)
        last_trade_ts = (
            last_trade.deribit_ts.replace(tzinfo=timezone.utc).isoformat()
            if last_trade and last_trade.deribit_ts else None
        )
        payload = {"asset": asset, "last_trade_ts": last_trade_ts}
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/klines-coinbase/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def klines_coinbase_proxy(self, asset, interval="4h", limit="500"):
        """Coinbase spot candles (dankbit.trade.get_candles_coinbase())
        for /4l/<asset>'s and /ll/<asset>'s candle series."""
        candles = request.env["dankbit.trade"].get_candles_coinbase(
            asset, interval=interval, limit=int(limit),
        )
        candles = candles[::-1]  # newest-first for frontend
        return request.make_response(
            json.dumps({"result": candles}),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/expiries/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def expiries_json(self, asset):
        """Every non-expired (active) instrument for `asset`, soonest-first
        — dankbit.bands._distinct_expirations()/_format_instrument(), the
        same helpers the rest of this file uses. Backs /4l/<asset>'s
        own "Expiry" dropdown (four_leg_gamma_chart_templates.xml), which
        used to offer a fixed Nearest/Weekly/Monthly option set — replaced
        per product decision with this dynamically-loaded list of every
        actually-active expiry, so a real expiry can be picked directly
        instead of only through the weekly_expiry/monthly_expiry settings
        indirection. `limit=200` is generous headroom over the dozen or so
        expiries actually active at once in practice. This route only
        lists instruments — `/api/four-leg-gamma/<asset>`'s own
        `?expiry=nearest`/`nearest_plus_1`/etc. resolutions are
        unchanged and still reachable by a direct API call, just no
        longer offered from this page's dropdown."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )
        bands_model = request.env["dankbit.bands"]
        as_of = datetime.now(timezone.utc).replace(tzinfo=None)
        expirations = bands_model._distinct_expirations(asset, as_of, 200)
        expiries = [bands_model._format_instrument(asset, exp) for exp in expirations]
        payload = {"asset": asset, "expiries": expiries}
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    def _resolve_four_leg_trades(self, asset):
        """Trade-domain resolution for /api/four-leg-gamma: `?instrument=`
        (what /4l's Expiry dropdown sends; missing/unknown -> the nearest
        active expiry) and `?hours=` (an int from
        FOUR_LEG_WINDOW_HOURS_CHOICES, or "all" for no trailing window;
        anything else -> FOUR_LEG_DEFAULT_WINDOW_HOURS). Trades are ISOLATED
        to that one instrument via an anchored `=ilike` domain, `iv != 0`,
        capped at `deribit_ts <= as_of` (now), with `active_test=False`. Returns
        {trades, instrument, hours (int|"all"), STs, from_price, to_price,
        step, as_of}. `asset` must already be upper-cased/validated.

        The `?expiry=` modes, `?cumulative=`, `?from_instrument=`/
        `?to_instrument=` and the "auto"/"midnight" windows were removed
        2026-10-24 — no page sent them any more."""
        icp = request.env["ir.config_parameter"].sudo()
        if asset == "BTC":
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            step = float(icp.get_param("dankbit.steps", default=100))
        else:
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            step = float(icp.get_param("dankbit.eth_steps", default=50))
        as_of = datetime.now(timezone.utc).replace(tzinfo=None)

        instrument = (request.httprequest.args.get("instrument") or "").upper() or None
        if not instrument or instrument == "NULL":
            bands_model = request.env["dankbit.bands"]
            expirations = bands_model._distinct_expirations(asset, as_of, 1)
            instrument = bands_model._format_instrument(asset, expirations[0]) if expirations else None

        hours_param = request.httprequest.args.get("hours")
        if hours_param == "all":
            hours = "all"
        else:
            try:
                hours = int(hours_param)
            except (TypeError, ValueError):
                hours = None
            if hours not in FOUR_LEG_WINDOW_HOURS_CHOICES:
                hours = FOUR_LEG_DEFAULT_WINDOW_HOURS

        trades = request.env["dankbit.trade"]
        if instrument:
            domain = [("name", "=ilike", f"{instrument}-%"), ("iv", "!=", 0), ("deribit_ts", "<=", as_of)]
            if hours != "all":
                domain.append(("deribit_ts", ">=", as_of - timedelta(hours=hours)))
            trades = trades.with_context(active_test=False).search(domain)
        STs = np.arange(from_price, to_price, step, dtype=np.float64)
        return {
            "trades": trades, "instrument": instrument, "hours": hours, "STs": STs,
            "from_price": from_price, "to_price": to_price, "step": step, "as_of": as_of,
        }

    @http.route("/api/four-leg-gamma/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def four_leg_gamma_json(self, asset):
        """/4l/<asset>'s data: for one expiry's trades (see
        _resolve_four_leg_trades for `?instrument=`/`?hours=`),
        the 4 per-leg dollar-gamma extrema BCG/BPG/SCG/SPG
        (options.per_leg_gamma(); `0.0` = a leg with no trades), the
        combined portfolio delta's zero crossings (`delta_zero_prices`, or
        `delta_zero_sign` when it never crosses), the combined taker-signed
        gamma's zero crossings (`gamma_flips`) and its 2 strongest peaks/
        bottoms (`gamma_peaks`), all at r=0.05 over the configured price
        grid, plus Max Pain (`max_pain_price`/`max_pain_total_oi`) from live
        open interest — independent of the Window. Nothing persisted. Returns {asset, instrument,
        window_hours, points: [one reading, or empty], max_pain_price,
        max_pain_total_oi}."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        resolved = self._resolve_four_leg_trades(asset)
        trades = resolved["trades"]
        STs = resolved["STs"]
        instrument = resolved["instrument"]
        hours = resolved["hours"]
        as_of = resolved["as_of"]

        points = []
        if trades:
            legs = options.per_leg_gamma(STs, trades)
            # A leg with zero trades reports gamma_price as None (no
            # curve to peak/bottom at) — collapsed to 0.0 here, same
            # "0.0 = absent" sentinel the client's own
            # `if (latest.bcg_price)` falsy checks already expect
            # (dankbit_four_leg_gamma_chart_templates.xml).
            point = {
                "t": int(as_of.replace(tzinfo=timezone.utc).timestamp() * 1000),
                "trade_count": len(trades),
                "bcg_price": legs["long_call"]["gamma_price"] or 0.0, "bcg_value": legs["long_call"]["gamma_value"],
                "bpg_price": legs["long_put"]["gamma_price"] or 0.0, "bpg_value": legs["long_put"]["gamma_value"],
                "scg_price": legs["short_call"]["gamma_price"] or 0.0, "scg_value": legs["short_call"]["gamma_value"],
                "spg_price": legs["short_put"]["gamma_price"] or 0.0, "spg_value": legs["short_put"]["gamma_value"],
            }
            # Every price where the COMBINED portfolio delta of these
            # same trades (all 4 legs together, not per-leg) crosses
            # zero — drawn as /4l's dotted green "Δ0" lines.
            # r=0.05, same as every other delta=0 finder in this file
            # (delta_zero_json etc.). Empty list if it never crosses.
            d_arr = np.asarray(delta.portfolio_delta(STs, trades, 0.05), dtype=float)
            point["delta_zero_prices"] = options.find_zero_crossings(STs, d_arr)
            # When there's no crossing at all, which side of zero the
            # curve stays on across the whole configured price range —
            # "positive"/"negative" (by the sign of its largest-|delta|
            # finite point, since with no sign change every nonzero
            # point shares one sign), or "zero" if it's flat/non-finite
            # throughout. None whenever at least one crossing exists.
            # /4l shows this in a small on-chart box instead of lines.
            point["delta_zero_sign"] = None
            if not point["delta_zero_prices"]:
                finite = d_arr[np.isfinite(d_arr)]
                extreme = finite[np.argmax(np.abs(finite))] if finite.size else 0.0
                point["delta_zero_sign"] = (
                    "positive" if extreme > 0 else "negative" if extreme < 0 else "zero"
                )
            # Gamma flip — every price where the COMBINED taker-signed
            # portfolio dollar-gamma of these same trades (long legs +,
            # short legs -, all 4 together) crosses zero. Drawn as /4l's
            # dotted violet "γ flip" lines. Same r=0.05 as the delta
            # curve above. The dealer-signed curve (the negation) crosses
            # zero at the same prices, just with each side's sign
            # flipped. Each entry also says which sign gamma has just
            # below/above that price ("positive"/"negative" — opposite
            # by definition, since it's a sign change), so the chart can
            # label which side of the flip is long- vs short-gamma.
            # Same interpolation as options.find_zero_crossings(), kept
            # inline because that helper returns bare prices only.
            # Empty list if it never crosses.
            # Built from the 4 per-leg curves (their sum IS the combined
            # curve, portfolio_gamma() being a plain signed sum per trade)
            # so the Gamma Peak lines below can say which leg drives each
            # peak without a second pass over the trades.
            leg_curves = {}
            for code, opt_type, direction in (("LC", "call", "buy"), ("LP", "put", "buy"),
                                              ("SC", "call", "sell"), ("SP", "put", "sell")):
                leg_trades = trades.filtered(lambda t: t.option_type == opt_type and t.direction == direction)
                leg_curves[code] = np.asarray(gamma.portfolio_gamma(STs, leg_trades, 0.05), dtype=float)
            g_arr = sum(leg_curves.values())
            gamma_flips = []
            for i in range(len(g_arr) - 1):
                ga, gb = g_arr[i], g_arr[i + 1]
                if not (np.isfinite(ga) and np.isfinite(gb)) or ga * gb >= 0:
                    continue
                gamma_flips.append({
                    "price": float(STs[i] - ga * (STs[i + 1] - STs[i]) / (gb - ga)),
                    "below": "positive" if ga > 0 else "negative",
                    "above": "positive" if gb > 0 else "negative",
                })
            point["gamma_flips"] = gamma_flips
            # Gamma Peaks — the 2 strongest extrema (by |gamma|) of that
            # same combined taker-signed curve, drawn as /4l's "Gamma Peak"
            # lines. Same peaks+bottoms (local extrema beyond 15% of max
            # |gamma|) the /<instrument>/<hours> PNG marks with dashed lines
            # (chart_png_hours, r=0.05). A peak (> 0) is long-gamma driven,
            # a bottom (< 0) short-gamma driven; `leg` is whichever leg
            # contributes most gamma of that sign at that price. Empty list
            # when the curve has no such extremum.
            extrema = self.find_gamma_peaks(STs, g_arr) + self.find_gamma_bottoms(STs, g_arr)
            extrema.sort(key=lambda e: abs(e[1]), reverse=True)
            gamma_peaks = []
            for price, value in extrema[:2]:
                i = int(np.argmin(np.abs(STs - price)))
                pick = max if value > 0 else min
                leg = pick(leg_curves, key=lambda c: leg_curves[c][i])
                gamma_peaks.append({"price": price, "value": value, "leg": leg})
            point["gamma_peaks"] = gamma_peaks
            points.append(point)

        # Max Pain — independent of `hours`/the trades Window above (see
        # this route's own docstring): a function of currently
        # outstanding open interest, not recent trade flow. Only
        # meaningful for a single resolved instrument (the ordinal/
        # explicit-?instrument= families) — the cumulative
        # "all"/from-to-range scopes leave `instrument` unset, so this
        # stays at the absent-price default (0.0).
        mp_price = 0.0
        max_pain_total_oi = 0.0
        if instrument:
            oi_map = request.env["dankbit.trade"].get_open_interest_by_currency(asset)
            call_oi, put_oi = options.split_open_interest(oi_map, instrument)
            resolved_max_pain, _ = options.max_pain_price(call_oi, put_oi)
            if resolved_max_pain is not None:
                mp_price = resolved_max_pain
                max_pain_total_oi = sum(call_oi.values()) + sum(put_oi.values())

        payload = {
            "asset": asset, "instrument": instrument, "window_hours": hours, "points": points,
            "max_pain_price": mp_price, "max_pain_total_oi": max_pain_total_oi,
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/api/ll-gamma/<string:asset>", type="http", auth="user", website=False, csrf=False)
    def ll_avg_gamma_json(self, asset, **kw):
        """/ll/<asset>'s data: each active expiry's dominant
        gamma leg (ll_dominant_legs — the present leg with the largest
        |gamma value|) over the trailing `?hours=` window (any integer,
        clamped to 1..LL_MAX_WINDOW_HOURS; missing/malformed ->
        FOUR_LEG_DEFAULT_WINDOW_HOURS), measured back from now. Returns {asset, window_hours,
        trade_count, expiries: [{instrument, expiration, dominant_price,
        dominant_value, abs_value, dominant_leg, trade_count}, ...]} sorted
        by abs_value, strongest first. Nothing persisted."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.make_response(
                json.dumps({"error": "Unknown asset"}),
                headers=[("Content-Type", "application/json")],
            )

        icp = request.env["ir.config_parameter"].sudo()
        if asset == "BTC":
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            step = float(icp.get_param("dankbit.steps", default=100))
        else:
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            step = float(icp.get_param("dankbit.eth_steps", default=50))
        STs = np.arange(from_price, to_price, step, dtype=np.float64)

        try:
            hours = int(request.httprequest.args.get("hours"))
        except (TypeError, ValueError):
            hours = FOUR_LEG_DEFAULT_WINDOW_HOURS
        hours = max(1, min(hours, LL_MAX_WINDOW_HOURS))

        as_of = datetime.now(timezone.utc).replace(tzinfo=None)
        window_start = as_of - timedelta(hours=hours)

        expiries = ll_dominant_legs(request.env, asset, STs, as_of, window_start)

        payload = {
            "asset": asset,
            "window_hours": hours,
            "trade_count": sum(e["trade_count"] for e in expiries),
            "expiries": expiries,
        }
        return request.make_response(
            json.dumps(payload),
            headers=[("Content-Type", "application/json"), ("Cache-Control", "no-cache")],
        )

    @http.route("/4l/<string:asset>", type="http", auth="user", website=True)
    def four_leg_gamma_chart(self, asset):
        """/4l/<asset>: a standalone TradingView page (template
        dankbit_four_leg_gamma_chart) — Coinbase spot candles from
        /api/klines-coinbase/<asset>, plus the selected expiry's 4 gamma-leg
        lines (BCG/BPG/SCG/SPG), Dominant Leg, Δ0, γ flip, γ peak and Max Pain
        lines from /api/four-leg-gamma/<asset>, polled on
        dankbit.refresh_interval. Expiry and Window are chosen client-side;
        nothing is persisted."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()

        icp = request.env["ir.config_parameter"].sudo()
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        ctx = {"asset": asset, "refresh_interval": refresh_interval}
        return request.render("dankbit.dankbit_four_leg_gamma_chart", ctx)

    @http.route("/ll/<string:asset>", type="http", auth="user", website=True)
    def ll_avg_gamma_chart(self, asset):
        """Standalone TradingView page — structurally a stripped-down
        /4l/<asset> (four_leg_gamma_chart): same Coinbase-spot candle
        source, same Timeframe/Window/Theme/Window Line/Ruler/Settings/
        Refresh controls, same single vertical Window reference line.
        Differences: NO "Expiry" dropdown, and NO "Gamma Legs"/"AVG"/
        "N+1 AVG" checkboxes. Instead of 4 per-leg gamma lines for one
        chosen expiry, it draws ONE horizontal line per active expiry, at
        that expiry's own dominant leg's price (the present leg — one
        with at least one trade — whose |gamma value| is largest),
        recomputed live on every poll (dankbit.refresh_interval) via
        /api/ll-gamma/<asset> (no model/table behind this page). Renders
        its own standalone template (dankbit_ll_avg_gamma_chart). 404 for
        an unrecognized asset."""
        asset = asset.upper()
        if not (asset.startswith("BTC") or asset.startswith("ETH")):
            return request.not_found()

        icp = request.env["ir.config_parameter"].sudo()
        refresh_interval = int(icp.get_param("dankbit.refresh_interval", default=60))
        ctx = {
            "asset": asset,
            "refresh_interval": refresh_interval,
            "other_lines_min_gamma": ll_other_lines_min_gamma(request.env),
        }
        return request.render("dankbit.dankbit_ll_avg_gamma_chart", ctx)
