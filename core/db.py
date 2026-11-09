# -*- coding: utf-8 -*-
# core/db.py
"""
ST-012 -- connections to PostgreSQL (psycopg 3).

Three connections, each from its own URL in .env (core/config.py): 'app' (stratrad_app, owner of database stratrad),
'reader' (stratrad_reader, read-only), 'myinvestiq' (stratrad_ro on MyInvestIQ's portfolio_watch: SELECT on corporate_actions,
fii_daily_flow and, since 2026-10-29 for ST-008, stock_master). Every session runs in UTC (the server's default is IST), so stored and compared times are
unambiguous; show them in IST at the edges.
"""

import psycopg

from core.config import MYINVESTIQ_RO_URL, STRATRAD_DB_URL, STRATRAD_READER_URL

URLS = {'app': ('STRATRAD_DB_URL', STRATRAD_DB_URL), 'reader': ('STRATRAD_READER_URL', STRATRAD_READER_URL),
        'myinvestiq': ('MYINVESTIQ_RO_URL', MYINVESTIQ_RO_URL)}


def connect(which: str = 'app', url: str | None = None, autocommit: bool = False) -> psycopg.Connection:
    """Open a connection ('app', 'reader' or 'myinvestiq'), session timezone UTC. `url` overrides the .env value."""
    if url is None:
        var, url = URLS[which]
        if not url:
            raise RuntimeError(f'{var} is not set (prod .env; docs/CONFIG.md)')
    return psycopg.connect(url, autocommit=autocommit, options='-c timezone=UTC', connect_timeout=10)
