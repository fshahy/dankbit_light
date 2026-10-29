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

    @staticmethod
    def _format_instrument(asset, exp):
        """`asset` + a raw expiration datetime -> Deribit-style instrument
        string (e.g. 'BTC-9JUL26'), the convention every other expiry
        identifier in this addon uses."""
        return f"{asset}-{exp.day}{exp.strftime('%b').upper()}{exp.strftime('%y')}"
