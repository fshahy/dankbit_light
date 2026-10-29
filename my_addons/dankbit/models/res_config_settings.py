from odoo import models, fields

class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    from_price = fields.Float(
        string="From price",
        config_parameter="dankbit.from_price"
    )

    to_price = fields.Float(
        string="To price",
        config_parameter="dankbit.to_price"
    )

    eth_from_price = fields.Float(
        string="ETH From price",
        config_parameter="dankbit.eth_from_price"
    )

    eth_to_price = fields.Float(
        string="ETH To price",
        config_parameter="dankbit.eth_to_price"
    )

    steps = fields.Integer(
        string="Steps",
        config_parameter="dankbit.steps"
    )

    eth_steps = fields.Integer(
        string="ETH Steps",
        config_parameter="dankbit.eth_steps"
    )

    refresh_interval = fields.Integer(
        string="Refresh interval (s)",
        config_parameter="dankbit.refresh_interval"
    )

    ll_other_lines_min_gamma = fields.Integer(
        string="LL Other Expiries Min |Gamma| (M)",
        config_parameter="dankbit.ll_other_lines_min_gamma",
        default=500,
        help="/ll/BTC only: with \"Other Expiries\" checked, an expiry's line is drawn only when its "
             "|dominant gamma value| is above this many millions. ETH stays at 100M. Defaults to 500.",
    )


    deribit_timeout = fields.Float(
        string="Deribit API timeout (s)",
        config_parameter="dankbit.deribit_timeout",
        help="Timeout in seconds for calls to Deribit public APIs."
    )

    chat_model = fields.Char(
        string="Dankbit AI model",
        config_parameter="dankbit.chat_model",
        default="qwen3:14b",
        help="Ollama model name the Dankbit AI chat panel uses (/api/chat/<asset>), e.g. \"qwen3:14b\". "
             "Must already be pulled on the Ollama host (ollama pull <name>). Left empty, falls back to qwen3:14b.",
    )

    deribit_cache_ttl = fields.Float(
        string="Deribit cache TTL (s)",
        config_parameter="dankbit.deribit_cache_ttl",
        help="Time-to-live in seconds for cached Deribit responses (index/instruments)."
    )
