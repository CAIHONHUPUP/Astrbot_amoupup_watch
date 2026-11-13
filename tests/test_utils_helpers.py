# -*- coding: utf-8 -*-
"""工具层：城市系数、房租房价、金额取整、节假日与交易时段。"""
import datetime as dt

import utils.helpers as cal
from utils.helpers import (
    CAR_PRICE_CENTS, HOUSE_AREA_M2, cents, city_factor, city_tier, day_key,
    house_price_cents, in_session, is_holiday, is_trading_day, normalize_city,
    period_key, pick_stable, rent_cents, round_cents, stable_int, yuan,
)


def test_city_tiers_and_factor():
    assert city_tier("北京") == "一线" and city_factor("北京") == 1.5
    assert city_tier("杭州市") == "二线" and city_factor("杭州") == 1.0
    assert city_tier("洛阳") == "三线" and city_factor("洛阳") == 0.75
    assert city_tier("某某县") == "三线"          # 生僻城市回落三线
    assert normalize_city("北京市") == "北京"
    assert normalize_city("四川省") == "四川"


def test_house_price_and_rent_by_city():
    # 二线 1.2 万/平 × 80 平 = 96 万
    assert house_price_cents("杭州") == 96000000
    assert house_price_cents("北京") == 144000000     # ×1.5
    assert house_price_cents("洛阳") == 72000000      # ×0.75
    assert rent_cents("杭州") == 160000
    assert rent_cents("北京") == 240000
    assert rent_cents("洛阳") == 120000
    assert CAR_PRICE_CENTS == 12000000 and HOUSE_AREA_M2 == 80


def test_money_rounding_keeps_cents():
    assert cents(12.345) == 1235
    assert cents("0.1") == 10
    assert round_cents(1500.6) == 1501
    assert yuan(1500) == "¥15.00"
    assert yuan(0) == "¥0.00"


def test_day_keys_and_stable_random():
    assert day_key(dt.date(2026, 11, 13)) == "2026-11-13"
    assert period_key(dt.date(2026, 11, 13)) == "2026-11"
    # 同样的种子永远给同样的数（刷新不会跳）
    assert stable_int("rent:2026-11", 80000, 250000) == stable_int("rent:2026-11", 80000, 250000)
    assert 10 <= stable_int("x", 10, 20) < 20
    assert pick_stable("day", ("a", "b", "c")) == pick_stable("day", ("a", "b", "c"))


def test_holidays_and_sessions_share_one_source():
    assert cal.HOLIDAYS_2026 == __import__("utils.helpers", fromlist=["HOLIDAYS_2026"]).HOLIDAYS_2026
    assert is_holiday("2026-10-01") is True
    assert is_trading_day("2026-10-01") is False       # 国庆休市
    assert is_trading_day("2026-11-14") is False       # 周六
    assert is_trading_day("2026-11-13") is True        # 周五
    assert in_session(dt.datetime(2026, 11, 13, 10, 0)) is True
    assert in_session(dt.datetime(2026, 11, 13, 12, 0)) is False
    assert in_session(dt.datetime(2026, 11, 13, 14, 59)) is True
    assert in_session(dt.datetime(2026, 10, 1, 10, 0)) is False   # 节假日
    # 资金/基金：15:00 后顺延
    assert cal.fund_cutoff(dt.datetime(2026, 11, 13, 14, 30)) is True
    assert cal.fund_cutoff(dt.datetime(2026, 11, 13, 15, 1)) is False
