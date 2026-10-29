# -*- coding: utf-8 -*-

import logging
from datetime import datetime, timedelta, timezone

import numpy as np

from odoo import models

from ..controllers import options as options_lib

_logger = logging.getLogger(__name__)


class Bands(models.AbstractModel):
    """Compute-only helpers (no table): active-expiry lookups and the
    Longs/Shorts zone computation for one expiry. Rows were once stored
    by an hourly confirmation cron for the Delta Chart; both are gone."""
    _name = "dankbit.bands"
    _description = "Dankbit expiry and zone computations"

    def _distinct_expirations(self, asset, as_of, limit):
        """The `limit` soonest distinct active expirations for `asset`,
        soonest-first. Raw SQL DISTINCT (not search_read+limit, and not
        read_group, which buckets Datetime fields by month by default) — a
        plain limit=N on trade rows could return N rows that all share the
        same nearest expiration (100k+ trades on the nearest expiry alone
        isn't unusual), silently breaking "Nth expiry" semantics;
        DISTINCT+ORDER BY+LIMIT is also far cheaper than fetching enough rows
        to dedupe in Python on a live, frequently-polled route."""
        self.env.cr.execute(
            """
            SELECT DISTINCT expiration FROM dankbit_trade
            WHERE name ILIKE %s AND expiration >= %s
            ORDER BY expiration ASC
            LIMIT %s
            """,
            (f"{asset}-%", as_of, limit),
        )
        return [row[0] for row in self.env.cr.fetchall()]

    def _distinct_expirations_asof(self, asset, as_of, limit):
        """Like _distinct_expirations, but for a historical `as_of`
        instead of "now": only returns an expiration if some trade for it
        had already happened at/before `as_of` (`deribit_ts <= as_of`), not
        merely that the expiration itself hasn't happened yet
        (`expiration >= as_of`, _distinct_expirations' own only
        condition). Without the trade-activity check, a still-far-future
        expiration that in reality wasn't listed on Deribit yet at that
        historical moment (options are typically listed weeks/months
        ahead, not the full 9-month depth this addon retains trades for)
        would still show up as "available" — this addon's own trade rows
        are the only record of when an expiry actually started trading, so
        that's the check used here rather than a fixed listing-lead-time
        assumption.

        Used only by /tm/<asset> (the Time Machine page — see
        controllers/main.py's time_machine_chart/expiries_json's own
        `?as_of=` branch) to populate its "Expiry" dropdown for a
        user-picked past date; every live (as_of=now) caller keeps using
        plain _distinct_expirations unchanged, since for "now" every
        currently-active expiration has, by definition, already had the
        chance to trade."""
        self.env.cr.execute(
            """
            SELECT DISTINCT expiration FROM dankbit_trade
            WHERE name ILIKE %s AND expiration >= %s AND deribit_ts <= %s
            ORDER BY expiration ASC
            LIMIT %s
            """,
            (f"{asset}-%", as_of, as_of, limit),
        )
        return [row[0] for row in self.env.cr.fetchall()]

    @staticmethod
    def _format_instrument(asset, exp):
        """`asset` + a raw expiration datetime -> Deribit-style instrument
        string (e.g. 'BTC-9JUL26'), the convention every other expiry
        identifier in this addon uses."""
        return f"{asset}-{exp.day}{exp.strftime('%b').upper()}{exp.strftime('%y')}"

    def _compute_asset(self, asset, expiry_index=0, hours=None):
        """The Longs/Shorts payoff zones of one active expiry of `asset`
        (`expiry_index` 0 = nearest, 1 = the next, ...), from that expiry's
        trades since 00:00 UTC (options.day_window_start), or over the
        trailing `hours` when given: High/Low Zone (each curve's highest/
        lowest zero-crossing), Middle Zone (between Seller Max Profit and
        Buyer Max Loss), buy/sell trade counts, index price, the expiry's
        real `expiration`, and the window's `trades` (for
        _attach_zone_strengths). None when there's nothing computable (no
        index price, no expiry at that index, or no trades in the window)."""
        icp = self.env["ir.config_parameter"].sudo()
        as_of = datetime.now(timezone.utc).replace(tzinfo=None)

        if asset == "BTC":
            from_price = float(icp.get_param("dankbit.from_price", default=100000))
            to_price = float(icp.get_param("dankbit.to_price", default=150000))
            steps = int(icp.get_param("dankbit.steps", default=100))
        else:
            from_price = float(icp.get_param("dankbit.eth_from_price", default=2000))
            to_price = float(icp.get_param("dankbit.eth_to_price", default=5000))
            steps = int(icp.get_param("dankbit.eth_steps", default=50))

        index_price = self.env["dankbit.trade"].get_index_price(asset)
        if not index_price:
            _logger.warning("_compute_asset: no index price for %s, skipping", asset)
            return None

        Trade = self.env["dankbit.trade"].with_context(active_test=False)

        expirations = self._distinct_expirations(asset, as_of, expiry_index + 1)
        if len(expirations) <= expiry_index:
            _logger.warning(
                "_compute_asset: no active expiry at index %s for %s, skipping",
                expiry_index, asset,
            )
            return None
        target_expiration = expirations[expiry_index]
        instrument = (
            f"{asset}-{target_expiration.day}"
            f"{target_expiration.strftime('%b').upper()}{target_expiration.strftime('%y')}"
        )

        window_start = (
            as_of - timedelta(hours=hours) if hours is not None
            else options_lib.day_window_start(as_of)
        )
        domain = [
            ("name", "=ilike", f"{asset}-%"),
            ("expiration", "=", target_expiration),
            ("deribit_ts", ">=", window_start),
            ("deribit_ts", "<=", as_of),
            ("iv", "!=", 0),
        ]
        trades = Trade.search(domain=domain)
        if not trades:
            # No trades in the trade window for this expiry (e.g. thin/no
            # activity right before it rolls off) — an all-zero payoffs
            # curve has no real extrema, and argmax/argmin would trivially
            # return index 0 (the configured price-range floor), a
            # meaningless value that looks like real data. Skip instead.
            _logger.warning(
                "_compute_asset: no trades for %s expiry index %s as of %s, skipping",
                asset, expiry_index, as_of,
            )
            return None

        # Long/Short (buy/sell) trade counts — the chat's `zones` tool reports
        # their sum as the window's trade count.
        long_trade_count = len(trades.filtered(lambda t: t.direction == "buy"))
        short_trade_count = len(trades.filtered(lambda t: t.direction == "sell"))

        longs_obj, shorts_obj = options_lib.build_zone_curves(
            asset, index_price, trades, from_price, to_price, steps
        )

        STs = longs_obj.STs

        # Where the Shorts curve peaks and the Longs curve bottoms out.
        seller_max_profit = float(STs[int(np.argmax(shorts_obj.payoffs))])
        buyer_max_loss = float(STs[int(np.argmin(longs_obj.payoffs))])

        # Zero-crossings of each curve. Current price is deliberately not a
        # factor here: a zone boundary is a property of where a curve crosses
        # zero, not of where the index price sits. Each curve's own highest
        # crossing feeds the High Zone, its lowest the Low Zone; a curve with
        # only one crossing contributes that same value to both.
        short_crossings = options_lib.find_zero_crossings(STs, shorts_obj.payoffs)
        long_crossings = options_lib.find_zero_crossings(STs, longs_obj.payoffs)
        short_above = [max(short_crossings)] if short_crossings else []
        short_below = [min(short_crossings)] if short_crossings else []
        long_above = [max(long_crossings)] if long_crossings else []
        long_below = [min(long_crossings)] if long_crossings else []

        # High Zone / Low Zone: the two curves' highest / lowest crossings.
        # 0.0/0.0 means neither curve ever crossed zero.
        high_zone_prices = short_above + long_above
        low_zone_prices = short_below + long_below
        high_zone_min = min(high_zone_prices) if high_zone_prices else 0.0
        high_zone_max = max(high_zone_prices) if high_zone_prices else 0.0
        low_zone_min = min(low_zone_prices) if low_zone_prices else 0.0
        low_zone_max = max(low_zone_prices) if low_zone_prices else 0.0

        # Middle Zone — bounded by seller_max_profit/buyer_max_loss (min/max
        # of the two). Always
        # defined, unlike high_zone/low_zone, since seller_max_profit/
        # buyer_max_loss are argmax/argmin over the full curve, not
        # zero-crossings.
        middle_zone_min = min(seller_max_profit, buyer_max_loss)
        middle_zone_max = max(seller_max_profit, buyer_max_loss)

        return {
            "asset": asset,
            "instrument": instrument,
            "computed_at": as_of,
            "expiration": target_expiration,
            "index_price": index_price,
            "long_trade_count": long_trade_count,
            "short_trade_count": short_trade_count,
            "high_zone_min": high_zone_min,
            "high_zone_max": high_zone_max,
            "low_zone_min": low_zone_min,
            "low_zone_max": low_zone_max,
            "middle_zone_min": middle_zone_min,
            "middle_zone_max": middle_zone_max,
            "seller_max_profit": seller_max_profit,
            "buyer_max_loss": buyer_max_loss,
            # The window's trades, for _attach_zone_strengths() — not
            # persisted or serialized; callers read named keys only.
            "trades": trades,
        }

    def get_box(self, asset, hours=None, expiry_index=0):
        """Zones for one expiry (`expiry_index` 0 = nearest), computed live
        from that expiry's trades since 00:00 UTC, or over the trailing
        `hours` when given: _compute_asset()'s result plus the 3 zone
        strengths (_attach_zone_strengths). None when nothing is computable.
        Used by the chat's `zones` tool."""
        data = self._compute_asset(asset, expiry_index=expiry_index, hours=hours)
        if data:
            self._attach_zone_strengths(data)
        return data

    @staticmethod
    def _attach_zone_strengths(data):
        """Adds high_zone_strength/low_zone_strength/middle_zone_strength
        (0..1, or None when that zone is absent or there's no gamma) to
        `data`, for whatever zone boundaries `data` holds at this point —
        see options.zone_gamma_shares()."""
        high, low, middle = options_lib.zone_gamma_shares(
            data.get("trades"), data.get("index_price"),
            [(data["high_zone_min"], data["high_zone_max"]),
             (data["low_zone_min"], data["low_zone_max"]),
             (data["middle_zone_min"], data["middle_zone_max"])],
        )
        data.update({"high_zone_strength": high, "low_zone_strength": low, "middle_zone_strength": middle})
        return data
