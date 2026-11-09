# -*- coding: utf-8 -*-
# scripts/check_database.py
"""
ST-012 acceptance checks on prod -- every permission is tested by ATTEMPTING the action, not by reading grants.

1. SCHEMA: the 14 tables of the approved model exist; every migration file is applied (schema_migrations).
2. UTC: sessions of all three roles report timezone UTC.
3. READER: stratrad_reader can SELECT every table; INSERT / UPDATE / DELETE / TRUNCATE on every table and CREATE TABLE
   fail (tested with read-only mode switched off, so only the grants can stop it).
4. MYINVESTIQ: stratrad_ro can SELECT corporate_actions, fii_daily_flow and stock_master (ST-008, 2026-10-29); SELECT
   on every other portfolio_watch table, any write on the three, and connecting to stratrad all fail. stratrad_app cannot read portfolio_watch tables.
5. AUDIT LOG: stratrad_app can INSERT into audit_log; UPDATE, DELETE and TRUNCATE fail (rolled back, nothing kept).

Read-only apart from the rolled-back audit_log insert. Writes LOG_DIR/check_database_<date>.log.
Exit code 0 only when RESULT: PASS. Usage (repo root): python -m scripts.check_database
"""

import logging
import sys
from datetime import date

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from core.config import LOG_DIR, MYINVESTIQ_RO_URL, STRATRAD_DB_URL, STRATRAD_READER_URL
from core.db import connect
from scripts.migrate import discover

logger = logging.getLogger('check_database')
TABLES = ['strategies', 'models', 'signals', 'proposals', 'positions', 'orders', 'order_charges', 'gtt_orders',
          'position_adjustments', 'order_events', 'guardrail_events', 'daily_snapshots', 'reconciliations', 'audit_log']
MIQ_ALLOWED = {'corporate_actions', 'fii_daily_flow', 'stock_master'}   # stock_master: ST-008 sectors (interim, ST-005)


def refused(conn: psycopg.Connection, statement) -> bool:
    """True when `statement` fails (permission or trigger); always rolled back."""
    try:
        with conn.transaction():
            conn.execute(statement)
            raise _Allowed()
    except _Allowed:
        return False
    except psycopg.Error:
        return True


class _Allowed(Exception):
    """Raised inside the transaction so a statement that succeeded is rolled back."""


def writes(table: str) -> list:
    """The write statements tried on `table`."""
    t = sql.Identifier(table)
    return [sql.SQL('INSERT INTO {} DEFAULT VALUES').format(t), sql.SQL('UPDATE {} SET id = id WHERE false').format(t),
            sql.SQL('DELETE FROM {} WHERE false').format(t), sql.SQL('TRUNCATE {}').format(t)]


def main() -> int:
    """CLI entry point."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(LOG_DIR / f'check_database_{date.today():%Y%m%d}.log',
                                                      encoding='utf-8')])
    fails = []

    def check(ok: bool, what: str) -> None:
        logger.info(f"{'ok  ' if ok else 'FAIL'} {what}")
        if not ok:
            fails.append(what)

    with connect('app') as app:
        have = {r[0] for r in app.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        check(set(TABLES) <= have, f'1 schema: 14 tables present (missing: {sorted(set(TABLES) - have)})')
        applied = {r[0] for r in app.execute('SELECT version FROM schema_migrations')}
        files = {v for v, *_ in discover()}
        check(applied == files, f'1 schema: migrations applied {sorted(applied)} = files {sorted(files)}')
        check(app.execute('SHOW timezone').fetchone()[0] == 'UTC', '2 utc: stratrad_app session in UTC')
        check(not refused(app, "INSERT INTO audit_log (actor, action) VALUES ('system', 'st012_check')"),
              '5 audit: stratrad_app can insert (rolled back)')
        for op in ('UPDATE audit_log SET action = action', 'DELETE FROM audit_log', 'TRUNCATE audit_log'):
            with app.transaction():                      # a row to act on; the whole block is rolled back
                app.execute("INSERT INTO audit_log (actor, action) VALUES ('system', 'st012_check')")
                check(refused(app, op), f'5 audit: refused for the owner: {op}')
                raise psycopg.Rollback()
        app.rollback()
        try:
            with connect(url=make_conninfo(STRATRAD_DB_URL, dbname='portfolio_watch')) as cross:
                check(refused(cross, 'SELECT 1 FROM corporate_actions LIMIT 0'),
                      '4 myinvestiq: stratrad_app cannot read portfolio_watch.corporate_actions')
        except psycopg.OperationalError:
            check(True, '4 myinvestiq: stratrad_app cannot even connect to portfolio_watch')

    with connect('reader') as rd:
        check(rd.execute('SHOW timezone').fetchone()[0] == 'UTC', '2 utc: stratrad_reader session in UTC')
        rd.execute('SET default_transaction_read_only = off')
        rd.commit()
        for t in TABLES:
            check(not refused(rd, sql.SQL('SELECT * FROM {} LIMIT 1').format(sql.Identifier(t))), f'3 reader: SELECT {t}')
            bad = [str(i) for i, w in enumerate(writes(t)) if not refused(rd, w)]
            check(not bad, f'3 reader: every write refused on {t}' + (f' (allowed: {bad})' if bad else ''))
        check(refused(rd, 'CREATE TABLE st012_probe (x int)'), '3 reader: CREATE TABLE refused')

    with connect('myinvestiq') as ro:
        check(ro.execute('SHOW timezone').fetchone()[0] == 'UTC', '2 utc: stratrad_ro session in UTC')
        ro.execute('SET default_transaction_read_only = off')
        ro.commit()
        tables = {r[0] for r in ro.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        check(MIQ_ALLOWED <= tables, f'4 myinvestiq: {sorted(MIQ_ALLOWED)} exist')
        for t in sorted(MIQ_ALLOWED):
            check(not refused(ro, sql.SQL('SELECT * FROM {} LIMIT 1').format(sql.Identifier(t))), f'4 myinvestiq: SELECT {t}')
            bad = [str(i) for i, w in enumerate(writes(t)) if not refused(ro, w)]
            check(not bad, f'4 myinvestiq: every write refused on {t}' + (f' (allowed: {bad})' if bad else ''))
        readable = [t for t in sorted(tables - MIQ_ALLOWED)
                    if not refused(ro, sql.SQL('SELECT 1 FROM {} LIMIT 0').format(sql.Identifier(t)))]
        check(not readable, f'4 myinvestiq: the other {len(tables - MIQ_ALLOWED)} tables refused'
                            + (f' (readable: {readable})' if readable else ''))
    try:
        with connect(url=make_conninfo(MYINVESTIQ_RO_URL, dbname='stratrad')):
            check(False, '4 myinvestiq: stratrad_ro must not connect to stratrad')
    except psycopg.OperationalError:
        check(True, '4 myinvestiq: stratrad_ro cannot connect to stratrad')

    logger.info('RESULT: ' + (f'FAIL -- {len(fails)}' if fails else 'PASS'))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
