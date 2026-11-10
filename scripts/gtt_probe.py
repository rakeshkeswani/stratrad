# -*- coding: utf-8 -*-
# scripts/gtt_probe.py
"""
ST-009 piece 2 -- the dry run against Kite (prod only). Proves the sell path end to end without selling anything.

On a stock Rakesh holds (default RELIANCE), never a stratrad lot:
  1. place a quantity-1 GTT, trigger at ~45% of the last price, MARKET sell leg with market protection
  2. read it back and check symbol, quantity, trigger and order shape
  3. raise the trigger to ~46% and read it back
  4. try to LOWER it: check_before_sell must block (S06), and nothing is sent
  5. delete it and confirm Kite no longer shows it active
  6. count this run's guardrail_events rows: expect 4 (3 allow, 1 block), all stage before_gtt

Every Kite write goes check_before_sell -> clearance -> execution/broker.py. The trigger can never fire: NSE price bands
are at most 20% a day. If any step fails after the place, the script still tries the guarded delete and prints the GTT
id, so nothing is left behind. Exit 0 = every check passed; 1 = a check failed (details logged); 2 = could not start.

Usage (prod, repo root, after the morning Kite login):  python -m scripts.gtt_probe [--symbol RELIANCE]
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import date

from core.config import LOG_DIR
from core.kite import LoginNeeded, load_session
from execution.broker import Broker
from execution.guardrail_service import CONFIG_PATH, DbSources, check_before_sell
from execution.guardrails import ALLOW, BLOCK, GTT_DELETE, GTT_MODIFY, GTT_PLACE, SellRequest, load_limits

LOGGER = logging.getLogger('gtt_probe')
PLACE_FRACTION = 0.45
RAISE_FRACTION = 0.46
LOWER_FRACTION = 0.44
STEP = 0.1                      # triggers in whole 10 paise: a multiple of every NSE tick size in use


def _floor(price: float) -> float:
    return round(math.floor(price / STEP) * STEP, 2)


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    handlers = [logging.StreamHandler(sys.stdout),
                logging.FileHandler(LOG_DIR / f'gtt_probe_{date.today():%Y%m%d}.log', encoding='utf-8')]
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)


class Probe:
    def __init__(self, symbol: str, broker: Broker, sources: DbSources, limits: dict):
        self.symbol, self.broker, self.sources, self.limits = symbol, broker, sources, limits
        self.failures: list[str] = []
        self.gtt_id: int | None = None
        self.deleted = False

    def check(self, ok: bool, what: str) -> bool:
        (LOGGER.info if ok else LOGGER.error)('%s %s', 'PASS' if ok else 'FAIL', what)
        if not ok:
            self.failures.append(what)
        return ok

    def guarded(self, req: SellRequest, write):
        decision, clearance = check_before_sell(req, sources=self.sources, market=self.broker, limits=self.limits)
        LOGGER.info('  guardrails %s %s: %s', req.action, req.trigger_price or '', decision.detail)
        if decision.outcome != ALLOW:
            return decision, None
        return decision, write(clearance)

    def readback(self, trigger: float) -> None:
        g = self.broker.gtt(self.gtt_id)
        cond, legs = g.get('condition', {}), g.get('orders', [])
        leg = legs[0] if legs else {}
        LOGGER.info('  kite GTT %s: status %s, condition %s, order %s', self.gtt_id, g.get('status'), cond, leg)
        self.check(g.get('status') == 'active', f'GTT {self.gtt_id} active')
        self.check(cond.get('tradingsymbol') == self.symbol, 'symbol matches')
        self.check(abs(float(cond.get('trigger_values', [0])[0]) - trigger) < 0.001, f'trigger is {trigger}')
        self.check(int(leg.get('quantity', 0)) == 1, 'quantity 1')
        self.check(leg.get('transaction_type') == 'SELL', 'leg is a SELL')
        self.check(leg.get('order_type') == 'MARKET', 'leg is MARKET')
        self.check(leg.get('product') == 'CNC', 'leg is CNC')
        LOGGER.info('  market_protection echoed by Kite: %r (sent %r)', leg.get('market_protection'),
                    self.limits['sell']['market_protection'])

    def run(self) -> None:
        held = self.broker.holding_qty(self.symbol)
        ltp = self.broker.last_price(self.symbol)
        LOGGER.info('%s: held %s, last price %s', self.symbol, held, ltp)
        if not self.check(bool(held) and ltp is not None, f'{self.symbol} held and priced'):
            return
        place, up, down = _floor(ltp * PLACE_FRACTION), _floor(ltp * RAISE_FRACTION), _floor(ltp * LOWER_FRACTION)

        LOGGER.info('1. place at %s', place)
        d, gid = self.guarded(SellRequest(GTT_PLACE, self.symbol, 1, trigger_price=place, probe=True),
                              self.broker.place_gtt)
        if not self.check(d.outcome == ALLOW and gid is not None, 'place allowed and sent'):
            return
        self.gtt_id = gid
        LOGGER.info('  GTT id %s', gid)

        LOGGER.info('2. read back')
        self.readback(place)

        LOGGER.info('3. raise to %s', up)
        d, _ = self.guarded(SellRequest(GTT_MODIFY, self.symbol, 1, trigger_price=up, gtt_id=gid, probe=True),
                            self.broker.modify_gtt)
        if self.check(d.outcome == ALLOW, 'raise allowed and sent'):
            self.readback(up)

        LOGGER.info('4. try to lower to %s (must be refused)', down)
        d, sent = self.guarded(SellRequest(GTT_MODIFY, self.symbol, 1, trigger_price=down, gtt_id=gid, probe=True),
                               self.broker.modify_gtt)
        self.check(d.outcome == BLOCK and sent is None, 'lower refused by check_before_sell, nothing sent')
        now = self.broker.gtt_trigger(gid)
        self.check(now is not None and abs(now - up) < 0.001, f'Kite trigger still {up} (is {now})')

    def delete(self) -> None:
        if self.gtt_id is None:
            return
        LOGGER.info('5. delete GTT %s', self.gtt_id)
        try:
            d, _ = self.guarded(SellRequest(GTT_DELETE, self.symbol, 1, gtt_id=self.gtt_id, probe=True),
                                self.broker.delete_gtt)
            self.deleted = d.outcome == ALLOW
        except Exception as e:                       # noqa: BLE001 -- report and fall through to the manual note
            LOGGER.error('  delete failed: %s', e)
        status = self.broker.gtt_status(self.gtt_id)
        if not self.check(self.deleted and status != 'active', f'GTT {self.gtt_id} gone (Kite status {status})'):
            LOGGER.error('  DELETE GTT %s BY HAND in Kite (Orders > GTT): it is quantity 1 on %s, far below the '
                         'market, and cannot fire today', self.gtt_id, self.symbol)


EXPECTED_EVENTS = [('before_gtt', 'allow'), ('before_gtt', 'allow'), ('before_gtt', 'block'), ('before_gtt', 'allow')]


def event_shape_ok(rows: list[tuple]) -> bool:
    """(id, stage, decision, rule) rows of one clean run: one row per check, in order."""
    return [(r[1], r[2]) for r in rows] == EXPECTED_EVENTS


def _max_event_id(sources: DbSources) -> int:
    with sources._app_conn().cursor() as cur:
        cur.execute('SELECT coalesce(max(id), 0) FROM guardrail_events')
        return cur.fetchone()[0]


def _events_since(sources: DbSources, after: int) -> list[tuple]:
    with sources._app_conn().cursor() as cur:
        cur.execute('SELECT id, stage, decision, rule FROM guardrail_events WHERE id > %s ORDER BY id', (after,))
        return cur.fetchall()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--symbol', default='RELIANCE')
    args = ap.parse_args(argv)
    _setup_logging()
    try:
        broker = Broker(load_session())
        limits = load_limits(CONFIG_PATH)
    except (LoginNeeded, OSError, ValueError, KeyError) as e:
        LOGGER.error('cannot start: %s', e)
        return 2
    if not broker._proxy:
        LOGGER.error('cannot start: KITE_ORDER_PROXY not set (prod .env)')
        return 2
    LOGGER.info('gtt_probe %s via %r; market_protection %r', args.symbol, broker, limits['sell']['market_protection'])

    with DbSources() as sources:
        start = _max_event_id(sources)
        probe = Probe(args.symbol, broker, sources, limits)
        try:
            probe.run()
        except Exception as e:                       # noqa: BLE001 -- always reach the delete
            probe.check(False, f'unexpected error: {type(e).__name__}: {e}')
        finally:
            probe.delete()

        LOGGER.info('6. guardrail_events for this run')
        rows = _events_since(sources, start)
        for r in rows:
            LOGGER.info('  event %s %s %s %s', *r)
        if probe.gtt_id is not None and not probe.failures:
            probe.check(event_shape_ok(rows), 'events: place allow, raise allow, lower block, delete allow')

    if probe.failures:
        LOGGER.error('RESULT: FAIL (%d): %s', len(probe.failures), '; '.join(probe.failures))
        return 1
    LOGGER.info('RESULT: OK -- the sell path works end to end on Kite')
    return 0


if __name__ == '__main__':
    sys.exit(main())
