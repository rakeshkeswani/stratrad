# -*- coding: utf-8 -*-
# scripts/adjust_lot.py
"""
ST-009 piece 4 -- apply a split or bonus to one stratrad lot and re-protect it (prod; Rakesh runs it).

Kite disables a GTT on a split or bonus; the evening check then alerts. Rakesh confirms the factor (new shares per old
share: a 1:5 split is 5, a 1:1 bonus is 2) and runs this. It writes the `position_adjustments` row, scales the lot's
quantity, entry price and stop, and places a new GTT through the guardrails. Without --yes it only shows the plan.

Usage (prod, repo root):  python -m scripts.adjust_lot --lot N --factor F --ex-date YYYY-MM-DD --type split|bonus [--yes]
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from core.kite import LoginNeeded, load_session
from execution.broker import Broker
from execution.lifecycle import Report, floor_tick
from execution.reconcile import apply_adjustment, from_db

LOGGER = logging.getLogger('adjust_lot')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--lot', type=int, required=True)
    ap.add_argument('--factor', type=float, required=True, help='new shares per old share (1:5 split = 5)')
    ap.add_argument('--ex-date', type=date.fromisoformat, required=True)
    ap.add_argument('--type', choices=('split', 'bonus'), required=True)
    ap.add_argument('--yes', action='store_true', help='apply (default: show the plan only)')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        check = from_db(Broker(load_session()))
    except (LoginNeeded, OSError, ValueError, KeyError) as e:
        LOGGER.error('cannot start: %s', e)
        return 2
    store = check.store
    lot = store.lot(args.lot)
    if lot is None:
        LOGGER.error('no lot %s', args.lot)
        return 1
    stop = store.last_gtt_trigger(args.lot)
    LOGGER.info('lot %s %s (%s, %s): qty %s -> %s, entry %.2f -> %.4f, stop %s -> %s', lot['id'], lot['symbol'],
                lot['strategy'], lot['status'], lot['qty'], lot['qty'] * args.factor, float(lot['entry_price']),
                float(lot['entry_price']) / args.factor, stop, floor_tick(stop / args.factor) if stop else None)
    if not args.yes:
        LOGGER.info('plan only: add --yes to apply')
        return 0
    report = Report()
    ok = apply_adjustment(store, check.lifecycle, args.lot, args.factor, args.ex_date, args.type, report)
    LOGGER.info('RESULT: %s', 'OK' if ok else 'NOT APPLIED / UNPROTECTED -- see alerts')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
