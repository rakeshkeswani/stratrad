# -*- coding: utf-8 -*-
# core/trading_calendar.py
"""ST-008: NSE equity trading days, from MyInvestIQ's holiday archive (read-only).

MyInvestIQ's 18:00 job writes data/calendar/nse_cm_holidays_<year>.json (its TD-226). stratrad reads it through
CALENDAR_ARCHIVE_DIR until it owns its own calendar (Track F). A trading day is a weekday that is not a listed
holiday; special weekend sessions (Muhurat, Budget Saturdays) are not trading days here.

Every answer that spans a year the archive does not cover is None, never a guess: the guardrails treat None as
unknown and block (fail-closed).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from core.config import CALENDAR_ARCHIVE_DIR

logger = logging.getLogger(__name__)

FILENAME = 'nse_cm_holidays_{year}.json'
DATE_FORMAT = '%d-%b-%Y'   # "15-Jan-2026", as NSE publishes it


class TradingCalendar:
    """Holiday-aware trading days for the years the archive covers."""

    def __init__(self, archive_dir: Path | None = None):
        """`archive_dir` defaults to CALENDAR_ARCHIVE_DIR; None there means no year is covered."""
        self.archive_dir = archive_dir if archive_dir is not None else CALENDAR_ARCHIVE_DIR
        self._years: dict[int, set[date] | None] = {}

    def holidays(self, year: int) -> set[date] | None:
        """The listed holidays of `year`, or None when the archive has no readable file for it."""
        if year not in self._years:
            self._years[year] = self._load(year)
        return self._years[year]

    def _load(self, year: int) -> set[date] | None:
        if self.archive_dir is None:
            return None
        path = Path(self.archive_dir) / FILENAME.format(year=year)
        try:
            doc = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning('trading calendar %s unreadable: %s', path, exc)
            return None
        out = set()
        for entry in doc.get('holidays') or []:
            try:
                out.add(datetime.strptime(entry['tradingDate'], DATE_FORMAT).date())
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning('trading calendar %s: bad entry %r (%s)', path, entry, exc)
                return None
        return out

    def is_trading_day(self, d: date) -> bool | None:
        """True / False, or None when `d`'s year is not covered."""
        if d.weekday() >= 5:
            return False
        holidays = self.holidays(d.year)
        if holidays is None:
            return None
        return d not in holidays

    def sessions_between(self, after: date, before: date) -> int | None:
        """Trading days strictly after `after` and strictly before `before`; None if any year in between is not covered.

        Used as an age: a dataset whose newest day is `after`, checked on `before`, has missed this many sessions.
        Today's own session is never counted, so a dataset current to the previous session has age 0.
        """
        if before <= after:
            return 0
        count, d = 0, after + timedelta(days=1)
        while d < before:
            day = self.is_trading_day(d)
            if day is None:
                return None
            count += int(day)
            d += timedelta(days=1)
        return count

    def previous_trading_day(self, before: date, max_back: int = 15) -> date | None:
        """The last trading day strictly before `before`, or None if an uncovered year is hit first."""
        d = before - timedelta(days=1)
        for _ in range(max_back):
            day = self.is_trading_day(d)
            if day is None:
                return None
            if day:
                return d
            d -= timedelta(days=1)
        return None
