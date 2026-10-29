# -*- coding: utf-8 -*-
{
    "name": "Dankbit",
    "version": "18.0.0.0.91",
    "category": "Options Greeks",
    "author": "Farid Shahy <fshahy@gmail.com>",
    "license": "Other OSI approved licence",
    "summary": "",
    "description": "",
    "depends": ["website"],
    "data": [
        "data/ir_rule.xml",
        "data/ir_cron.xml",
        "security/ir.model.access.csv",
        "views/res_config_settings_views.xml",
        "views/trade_views.xml",
        "views/dankbit_templates.xml",
        "views/four_leg_gamma_chart_templates.xml",
        "views/ll_avg_gamma_chart_templates.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
}
