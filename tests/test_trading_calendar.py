# -*- coding: utf-8 -*-
# tests/test_trading_calendar.py
"""ST-008: the trading calendar read from MyInvestIQ's holiday archive. Uncovered years are None, never a guess."""

from __future__ import annotations

import json
from datetime import date

from core.trading_calendar import TradingCalendar


def _archive(tmp_path, year, days):
    doc = {'year': year, 'holidays': [{'tradingDate': d, 'description': 'x'} for d in days]}
    (tmp_path / f'nse_cm_holidays_{year}.json').write_text(json.dumps(doc), encoding='utf-8')
    return tmp_path


def test_weekdays_and_holidays(tmp_path):
    cal = TradingCalendar(_archive(tmp_path, 2026, ['26-Jan-2026']))
    assert cal.is_trading_day(date(2026, 1, 23)) is True      # Friday
    assert cal.is_trading_day(date(2026, 1, 24)) is False     # Saturday
    assert cal.is_trading_day(date(2026, 1, 26)) is False     # Republic Day


def test_uncovered_year_is_unknown(tmp_path):
    cal = TradingCalendar(_archive(tmp_path, 2026, []))
    assert cal.is_trading_day(date(2025, 8, 26)) is None
    assert cal.sessions_between(date(2025, 8, 25), date(2026, 1, 5)) is None


def test_no_archive_configured_is_unknown(tmp_path):
    assert TradingCalendar(tmp_path / 'missing').is_trading_day(date(2026, 1, 5)) is None


def test_sessions_between_counts_missed_sessions_only(tmp_path):
    cal = TradingCalendar(_archive(tmp_path, 2026, ['26-Jan-2026']))
    # Data to Friday 23 Jan, checked Tuesday 27 Jan: Monday 26 is a holiday, Tuesday is today -> nothing missed.
    assert cal.sessions_between(date(2026, 1, 23), date(2026, 1, 27)) == 0
    # Checked Wednesday 28 Jan: Tuesday 27 was missed.
    assert cal.sessions_between(date(2026, 1, 23), date(2026, 1, 28)) == 1
    assert cal.sessions_between(date(2026, 1, 28), date(2026, 1, 28)) == 0


def test_previous_trading_day(tmp_path):
    cal = TradingCalendar(_archive(tmp_path, 2026, ['26-Jan-2026']))
    assert cal.previous_trading_day(date(2026, 1, 27)) == date(2026, 1, 23)
    assert cal.previous_trading_day(date(2026, 1, 1)) is None   # 31 Dec 2025: year not covered


def test_bad_entry_makes_the_year_unknown(tmp_path):
    (tmp_path / 'nse_cm_holidays_2026.json').write_text(json.dumps({'holidays': [{'tradingDate': '2026/01/26'}]}),
                                                        encoding='utf-8')
    assert TradingCalendar(tmp_path).is_trading_day(date(2026, 1, 5)) is None
