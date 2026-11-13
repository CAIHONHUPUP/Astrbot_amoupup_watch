# -*- coding: utf-8 -*-
"""A 股 / 场外基金的时间规则（纯函数，方便单测）。"""
from __future__ import annotations

import datetime as _dt

#: 2026 年 A 股休市日（周末之外；可在 config.finance_holidays 覆盖）
HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-02",
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-04-06",
    "2026-05-01", "2026-05-04", "2026-05-05",
    "2026-06-19",
    "2026-09-25",
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
}

MORNING = ((9 * 60 + 30), (11 * 60 + 30))
AFTERNOON = ((13 * 60), (15 * 60))


def parse_day(text) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(str(text)[:10])
    except Exception:
        return None


def is_weekend(day: _dt.date) -> bool:
    return day.weekday() >= 5


def is_trading_day(day, holidays=None) -> bool:
    d = parse_day(day) if not isinstance(day, _dt.date) else day
    if d is None or is_weekend(d):
        return False
    return d.isoformat() not in (holidays or HOLIDAYS_2026)


def minutes_of(now: _dt.datetime) -> int:
    return now.hour * 60 + now.minute


def in_session(now: _dt.datetime, holidays=None) -> bool:
    if not is_trading_day(now.date(), holidays):
        return False
    m = minutes_of(now)
    return (MORNING[0] <= m < MORNING[1]) or (AFTERNOON[0] <= m < AFTERNOON[1])


def fund_cutoff(now: _dt.datetime, holidays=None) -> bool:
    """15:00 前申购按当日净值；15:00 后顺延下一交易日。"""
    return is_trading_day(now.date(), holidays) and minutes_of(now) < 15 * 60


def next_trading_day(day, holidays=None) -> str:
    d = parse_day(day) if not isinstance(day, _dt.date) else day
    d = d or _dt.date.today()
    for _ in range(30):
        d = d + _dt.timedelta(days=1)
        if is_trading_day(d, holidays):
            return d.isoformat()
    return d.isoformat()


def prev_trading_day(day, holidays=None) -> str:
    d = parse_day(day) if not isinstance(day, _dt.date) else day
    d = d or _dt.date.today()
    for _ in range(30):
        d = d - _dt.timedelta(days=1)
        if is_trading_day(d, holidays):
            return d.isoformat()
    return d.isoformat()
