# -*- coding: utf-8 -*-
"""时区工具：configure/restore/换算/兜底分支。"""
import datetime as dt
import os
import re

import pytest

from utils import clock


@pytest.fixture(autouse=True)
def _clean_clock():
    yield
    try:
        clock.restore()
    except Exception:
        pass


def test_normalize_values():
    assert clock.normalize("") == ""
    assert clock.normalize("auto") == ""
    assert clock.normalize("System") == ""
    assert clock.normalize(" Asia/Tokyo ") == "Asia/Tokyo"


def test_read_etc_timezone_is_string():
    assert isinstance(clock._read_etc_timezone(), str)


def test_system_name_prefers_env(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert clock._system_name() == "Asia/Tokyo"


def test_resolve_name_precedence(monkeypatch):
    assert clock.resolve_name("Asia/Tokyo") == "Asia/Tokyo"
    monkeypatch.setitem(clock._state, "name", "Asia/Seoul")
    assert clock.resolve_name("") == "Asia/Seoul"
    monkeypatch.setitem(clock._state, "name", "")
    monkeypatch.delenv("TZ", raising=False)
    assert clock.resolve_name("") == clock.DEFAULT_TIMEZONE


def test_get_zone_and_fixed_offset(monkeypatch):
    zone = clock.get_zone("Asia/Shanghai")
    assert zone is not None
    assert clock.get_zone("") is None
    assert clock.get_zone("Not/AZone") is None
    monkeypatch.setattr(clock, "ZoneInfo", None)
    fixed = clock.get_zone("Asia/Shanghai")
    assert isinstance(fixed, clock._FixedOffset)
    assert fixed.utcoffset(None) == dt.timedelta(hours=8)
    assert fixed.dst(None) == dt.timedelta(0)
    assert "+8" in fixed.tzname(None)
    assert clock.get_zone("Asia/Tokyo") is None


def test_configure_without_tzset(monkeypatch):
    monkeypatch.delattr(clock._time, "tzset", raising=False)
    assert clock.configure("Asia/Tokyo") == "Asia/Tokyo"
    assert clock.applied() is False


def test_configure_and_restore_with_tzset(monkeypatch):
    monkeypatch.setattr(clock._time, "tzset", lambda: None, raising=False)
    monkeypatch.setenv("TZ", "Asia/Seoul")
    assert clock.configure("Asia/Tokyo") == "Asia/Tokyo"
    assert clock.applied() is True
    assert os.environ.get("TZ") == "Asia/Tokyo"
    clock.restore()
    assert clock.applied() is False
    assert clock._state["name"] == ""
    assert os.environ.get("TZ") == "Asia/Seoul"


def test_configure_failure_marks_not_applied(monkeypatch):
    def boom():
        raise OSError("tzset failed")

    monkeypatch.setattr(clock._time, "tzset", boom, raising=False)
    assert clock.configure("Asia/Tokyo") == "Asia/Tokyo"
    assert clock.applied() is False


def test_configure_failure_rolls_back_tz(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Seoul")

    def boom():
        raise OSError("tzset failed")

    monkeypatch.setattr(clock._time, "tzset", boom, raising=False)
    assert clock.configure("Asia/Tokyo") == "Asia/Tokyo"
    assert clock.applied() is False
    assert os.environ.get("TZ") == "Asia/Seoul"   # 失败不能把进程时区留在错的值上


def test_configure_failure_removes_tz_when_unset(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)

    def boom():
        raise OSError("tzset failed")

    monkeypatch.setattr(clock._time, "tzset", boom, raising=False)
    clock.configure("Asia/Tokyo")
    assert "TZ" not in os.environ


def test_now_when_already_applied(monkeypatch):
    monkeypatch.setitem(clock._applied, "ok", True)
    value = clock.now()
    assert value.tzinfo is None


def test_now_and_date_helpers():
    value = clock.now("Asia/Shanghai")
    assert value.tzinfo is None
    assert isinstance(clock.today("Asia/Shanghai"), dt.date)
    assert re.match(r"\d{4}-\d{2}-\d{2}", clock.today_str("Asia/Shanghai"))
    assert clock.utc_offset_minutes("Asia/Shanghai") == 480
    assert clock.sql_offset_modifier("Asia/Shanghai") == "+480 minutes"
    stamp = clock.from_timestamp(0, "Asia/Shanghai")
    assert isinstance(stamp, dt.datetime)
    assert stamp.year == 1970


def test_helpers_fallback_when_zone_missing(monkeypatch):
    monkeypatch.setattr(clock, "get_zone", lambda name: None)
    assert isinstance(clock.now(), dt.datetime)
    assert isinstance(clock.utc_offset_minutes(), int)
    assert isinstance(clock.from_timestamp(0), dt.datetime)
    assert clock.current_name()


def test_utc_offset_minutes_handles_bad_zone(monkeypatch):
    class Bad:
        def utcoffset(self, value):
            raise RuntimeError("bad zone")

    monkeypatch.setattr(clock, "get_zone", lambda name: Bad())
    assert clock.utc_offset_minutes("X") == 0
