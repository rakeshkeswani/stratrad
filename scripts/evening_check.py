# -*- coding: utf-8 -*-
# scripts/evening_check.py
"""
ST-009 piece 4 -- the evening check (prod; piece 5 schedules it after the close).

Syncs Kite's GTT and order states, re-protects any open lot without a GTT (at its last stop, never lower), reconciles
Kite against stratrad's rows (`reconciliations`), refreshes the `manual` lots (database only) and, on a trading day,
writes the `daily_snapshots` row that ST-008's G11 reads. See execution/reconcile.py.

Exit 0 = clean; 3 = done, with alerts (printed and logged); 2 = could not start (no Kite login, no proxy, no config).
Usage (prod, repo root, after the day's Kite login):  python -m scripts.evening_check [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from core.config import LOG_DIR
from core.kite import LoginNeeded, load_session
from execution.broker import Broker
from execution.guardrail_service import today_ist
from execution.reconcile import from_db

LOGGER = logging.getLogger('evening_check')


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stdout),
              logging.FileHandler(LOG_DIR / f'evening_check_{date.today():%Y%m%d}.log', encoding='utf-8')):
        h.setFormatter(fmt)
        root.addHandler(h)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--date', type=date.fromisoformat, help='the day to check (default: today, IST)')
    args = ap.parse_args(argv)
    _setup_logging()
    try:
        broker = Broker(load_session())
        check = from_db(broker)
    except (LoginNeeded, OSError, ValueError, KeyError) as e:
        LOGGER.error('cannot start: %s', e)
        return 2
    on = args.date or today_ist()
    report = check.run(on)
    LOGGER.info('RESULT: %s -- %d done, %d alert(s)', 'ALERTS' if report.alerts else 'OK', len(report.done),
                len(report.alerts))
    for a in report.alerts:
        LOGGER.info('  ALERT %s', a)
    return 3 if report.alerts else 0


if __name__ == '__main__':
    sys.exit(main())
