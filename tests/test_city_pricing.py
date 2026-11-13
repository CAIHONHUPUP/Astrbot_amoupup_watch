# -*- coding: utf-8 -*-
"""城市物价：一线 1.5 / 二线 1.0 / 三线 0.75，未设置按 1.0。"""
import datetime as dt

from services.city import city_view, factor_of
from services.consume_engine import FinanceConsume
from services.life_engine import FinanceLife
from services.property_engine import FinanceProperty


class FakeRepo:
    def __init__(self):
        self.meta = {}

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = str(value)

    def asset(self, kind):
        return None

    def loan(self, kind):
        return None

    def positions(self):
        return []

    def loans(self):
        return []

    def assets(self):
        return []


class FakePlugin:
    def __init__(self, city=""):
        self.config = {"city": city}
        self.finance = None


def test_factor_and_tier():
    assert factor_of("") == 1.0            # 没设城市 = 二线基准，不打折
    assert factor_of("北京市") == 1.5
    assert factor_of("杭州") == 1.0
    assert factor_of("某某县") == 0.75


def test_meal_price_scales_with_city():
    prices = {}
    for city in ("", "北京", "杭州", "洛阳"):
        consume = FinanceConsume(FakePlugin(city), FakeRepo(), None)
        prices[city or "未设置"] = consume.price_cents("肠粉")
    assert prices["未设置"] == 600
    assert prices["杭州"] == 600
    assert prices["北京"] == 900
    assert prices["洛阳"] == 450


def test_rent_scales_with_city():
    out = {}
    for city in ("", "北京", "杭州", "洛阳"):
        life = FinanceLife(FakePlugin(city), FakeRepo(), None)
        rent = life.rent_cents("2026-11")
        base = 160000 if city in ("", "杭州") else (240000 if city == "北京" else 120000)
        assert int(base * 0.90) <= rent <= int(base * 1.10), (city, rent)
        out[city or "未设置"] = rent
    assert out["北京"] > out["杭州"] > out["洛阳"]


class _Amo:
    class state:
        worldview = {"city": "东莞"}

    def cfg_str(self, key, default=""):
        return {"weather_city_name": "东莞", "schedule_city": ""}.get(key, default)


class _FinanceLink:
    def _amo(self):
        return _Amo()


class _Config(dict):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.saved = 0

    def save_config(self):
        self.saved += 1


class _MoveAmo:
    def __init__(self):
        self.config = _Config(weather_city="101281601", weather_city_name="东莞")
        self.state = type("S", (), {"worldview": {"city": "东莞"}, "weather_calls": []})()
        self.kv = {}

    def _city_lookup(self, name):
        return {"id": "101281601", "name": "东莞"} if name == "东莞" else None

    def save_config(self):
        self.saved += 1


_WEATHER_CALLS = []


async def _refresh(force=False):
    _WEATHER_CALLS.append(force)


def test_move_updates_weather_worldview_and_priority():
    import asyncio
    from services.city import move_to

    amo = _MoveAmo()
    amo.state.refresh_weather = _refresh
    amo.db = type("DB", (), {"kv_set": lambda self, k, v: amo.kv.update({k: v})})()

    class Fin:
        def _amo(self):
            return amo

    plugin = FakePlugin("")
    plugin.finance = Fin()
    result = asyncio.run(move_to(plugin, "东莞"))
    assert result["ok"] and result["world"] is True
    assert result["weather"]["id"] == "101281601"
    assert amo.config["weather_city_name"] == "东莞" and amo.config.saved == 1
    assert _WEATHER_CALLS == [True]          # 搬完立刻刷新天气
    assert amo.kv["worldview"]["city"] == "东莞"
    # 搬家后本插件以新城市为准
    plugin.city_override = "东莞"
    assert city_view(plugin)["city"] == "东莞"


def test_city_falls_back_to_amo_worldview():
    """面板上的「城市 东莞」来自 amo 世界观，而不是 schedule_city。"""
    plugin = FakePlugin("")
    plugin.finance = _FinanceLink()
    view = city_view(plugin)
    assert view["city"] == "东莞" and view["tier"] == "二线" and view["factor"] == 1.0


def test_house_price_by_city():
    out = {}
    for city in ("", "北京", "杭州", "洛阳"):
        prop = FinanceProperty(FakePlugin(city), FakeRepo(), None, None)
        out[city or "未设置"] = prop.price_cents("house")
    assert out["杭州"] == 96000000      # 96 万
    assert out["北京"] == 144000000     # 144 万
    assert out["洛阳"] == 72000000      # 72 万
    assert out["未设置"] == 96000000


def test_car_price_is_fixed():
    prop = FinanceProperty(FakePlugin("北京"), FakeRepo(), None, None)
    assert prop.price_cents("car") == 12000000


def test_city_view_for_panel():
    view = city_view(FakePlugin(""))
    assert view["tier"] == "未设置" and view["factor"] == 1.0
    view = city_view(FakePlugin("北京市"))
    assert view["city"] == "北京" and view["tier"] == "一线" and view["factor"] == 1.5
