# -*- coding: utf-8 -*-

from odoo import models


class Bands(models.AbstractModel):
    """Compute-only helpers (no table): active-expiry lookups."""
    _name = "dankbit.bands"
    _description = "Dankbit expiry lookups"

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
