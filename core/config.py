# -*- coding: utf-8 -*-
# core/config.py
"""Central place for paths and settings read from the environment (.env at repo root)."""

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=REPO_ROOT / '.env')



def _dir_from_env(var: str, default: Path | None = None) -> Path | None:
    """Return the directory named by env var `var`, or `default` when unset/empty."""
    value = (os.getenv(var) or '').strip()
    return Path(value) if value else default


# Logs. Default <repo>/logs; on prod that is readable from Windows at X:\\logs (docs/CONFIG.md).
LOG_DIR = _dir_from_env('LOG_DIR', REPO_ROOT / 'logs')

# Pre-2024 legacy-format bhavcopy archive written once by ingestion/fetch_legacy_bhavcopy.py.
# Prod: default (<repo>/data/bhavcopy_legacy). Dev: point at prod's share (read-only).
LEGACY_BHAVCOPY_DIR = _dir_from_env('LEGACY_BHAVCOPY_DIR', REPO_ROOT / 'data' / 'bhavcopy_legacy')

# MyInvestIQ's UDiFF archive (2024-01-01 onward). READ-ONLY for this project. No default:
# this project must never silently read or create a local copy.
BHAVCOPY_ARCHIVE_DIR = _dir_from_env('BHAVCOPY_ARCHIVE_DIR')
INDEX_ARCHIVE_DIR = _dir_from_env('INDEX_ARCHIVE_DIR')

# This project's own UDiFF files for dates MyInvestIQ's archive does not hold (2024-01-03 to
# 2025-08-27; docs/CONFIG.md "one-off operations"). Read-only. Prod: default (<repo>/data/bhavcopy_udiff).
UDIFF_GAP_DIR = _dir_from_env('UDIFF_GAP_DIR', REPO_ROOT / 'data' / 'bhavcopy_udiff')

# ST-003: corporate-actions history 2014..2025-08-25 (raw/<YYYY>.json + history.csv), written once by
# ingestion/fetch_corporate_actions_history.py. Prod: default (<repo>/data/corporate_actions).
CORPORATE_ACTIONS_DIR = _dir_from_env('CORPORATE_ACTIONS_DIR', REPO_ROOT / 'data' / 'corporate_actions')

# ST-003 (for ST-013 too): NSE symbol-change list snapshot, written by ingestion/fetch_symbol_changes.py.
# Prod: default (<repo>/data/symbol_changes).
SYMBOL_CHANGES_DIR = _dir_from_env('SYMBOL_CHANGES_DIR', REPO_ROOT / 'data' / 'symbol_changes')

# ST-004: price panel (one Parquet table, partitioned by year) written by ingestion/build_price_panel.py.
# Prod: default (<repo>/data/panel). Schema: docs/DATA_MODEL.md.
PANEL_DIR = _dir_from_env('PANEL_DIR', REPO_ROOT / 'data' / 'panel')

# ST-004: point-in-time top-500 universe (membership.csv) written by ingestion/build_universe.py.
# Prod: default (<repo>/data/universe).
UNIVERSE_DIR = _dir_from_env('UNIVERSE_DIR', REPO_ROOT / 'data' / 'universe')

# ST-010: benchmark index history (nifty500.csv) written by ingestion/fetch_benchmark.py.
# Prod: default (<repo>/data/benchmark).
BENCHMARK_DIR = _dir_from_env('BENCHMARK_DIR', REPO_ROOT / 'data' / 'benchmark')

# ST-012: PostgreSQL connection strings (libpq URLs, passwords included -- prod .env only, never in git).
# STRATRAD_DB_URL     postgresql://stratrad_app:...@localhost:5432/stratrad      (owner: the live system, migrations)
# STRATRAD_READER_URL postgresql://stratrad_reader:...@localhost:5432/stratrad   (read-only reporting)
# MYINVESTIQ_RO_URL   postgresql://stratrad_ro:...@localhost:5432/portfolio_watch (corporate_actions, fii_daily_flow)
STRATRAD_DB_URL = (os.getenv('STRATRAD_DB_URL') or '').strip()
STRATRAD_READER_URL = (os.getenv('STRATRAD_READER_URL') or '').strip()
MYINVESTIQ_RO_URL = (os.getenv('MYINVESTIQ_RO_URL') or '').strip()

# ST-006: Kite Connect (stratrad's own Personal app). Key and secret only in prod .env; never printed or logged.
# The day's session (access token) lives OUTSIDE the repo and the backup mirror, mode 600; it expires at 06:00 IST.
KITE_API_KEY = (os.getenv('KITE_API_KEY') or '').strip()
KITE_API_SECRET = (os.getenv('KITE_API_SECRET') or '').strip()
KITE_SESSION_FILE = Path(os.getenv('KITE_SESSION_FILE') or (Path.home() / '.stratrad' / 'kite_session.json'))
KITE_CALLBACK_PORT = int(os.getenv('KITE_CALLBACK_PORT') or 8765)
# Cloudflare Access in front of the callback (not secrets): team domain and the Access app's AUD tag. The callback
# verifies every Cf-Access-Jwt-Assertion against them and refuses all requests when either is missing.
KITE_ACCESS_TEAM = (os.getenv('KITE_ACCESS_TEAM') or '').strip()
KITE_ACCESS_AUD = (os.getenv('KITE_ACCESS_AUD') or '').strip()
# ST-007: Kite ORDER calls go through the static-IP exit VM (Lightsail Mumbai, 13.205.57.58) over Tailscale; login,
# profile, holdings and margins stay direct. Prod: http://100.70.129.17:8888 . Empty = no proxy (order calls then fail
# Kite's static-IP check, by design).
KITE_ORDER_PROXY = (os.getenv('KITE_ORDER_PROXY') or '').strip()
