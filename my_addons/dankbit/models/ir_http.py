# -*- coding: utf-8 -*-

from werkzeug.routing import BaseConverter

from odoo import models


class DankbitSymbolConverter(BaseConverter):
    """Matches only a bare Dankbit asset path segment — exactly "BTC" or
    "ETH" (case-sensitive). Backs the /BTC and /ETH PNG route
    (controllers/main.py's chart_png_all); per-expiry segments like
    "BTC-30OCT26" deliberately don't match, so those URLs 404.

    A plain ``string`` converter would match ANY single path segment and
    swallow website's own pages living at a bare path — e.g. ``/contactus``
    is a ``website.page`` record served by ir.http's fallback, which only
    runs when no controller matched the path at all.
    """
    regex = r"(?:BTC|ETH)"


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _get_converters(cls):
        converters = super()._get_converters()
        converters["dankbit_symbol"] = DankbitSymbolConverter
        return converters
