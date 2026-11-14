# -*- coding: utf-8 -*-
"""搬家：只由用户指令触发；扣搬家费；欠费待转账；记忆/日记/日程/情绪一次联动。"""
import asyncio
import types

from services.city import moving_fee_cents, relocate, city_view


class FakeAmo:
    class state:
        worldview = {"city": "东莞"}

    config = {"weather_city": "", "weather_city_name": "", "schedule_city": ""}
    db = types.SimpleNamespace(kv_set=lambda *a, **k: None)

    def _city_lookup(self, name):
        return {"id": "1011", "name": name}

    class _Cfg(dict):
        def save_config(self):
            pass


class FakeRepo:
    def __init__(self):
        self.meta = {}

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = value


class FakeFinance:
    def __init__(self, enough=True):
        self.enough = enough
        self.expenses = []
        self.memories = []
        self.diaries = []
        self.schedules = []
        self.moods = []

    async def expense(self, cents, reason, idem, necessary=False):
        self.expenses.append((cents, reason))
        return {"ok": self.enough}

    async def permanent_memory(self, text, idem):
        self.memories.append(text)
        return True

    def long_diary(self, text):
        self.diaries.append(text)
        return True

    def add_schedule(self, day, activity, detail="", place="家", category="leave"):
        self.schedules.append((activity, detail))
        return True

    def nudge_mood(self, deltas, idem):
        self.moods.append(deltas)
        return True


class FakePlugin:
    def __init__(self, finance):
        self.finance = finance
        self.finance_repo = FakeRepo()
        self.city_override = ""
        self.config = {}
        self.finance = finance

    def _amo(self):
        return None


def make(enough=True):
    fin = FakeFinance(enough)
    plugin = FakePlugin(fin)
    plugin.finance._amo = lambda: None
    return plugin, fin


def test_moving_fee_within_range_and_zero_same_city():
    assert moving_fee_cents("东莞", "东莞") == 0
    for target in ("杭州", "北京", "洛阳"):
        fee = moving_fee_cents("东莞", target)
        assert 50000 <= fee <= 200000, (target, fee)


def test_move_charges_fee_and_writes_memory_diary_schedule_mood():
    plugin, fin = make(enough=True)
    result = asyncio.run(relocate(plugin, "杭州"))
    assert result["ok"] and result["paid"] is True and result["pending_cents"] == 0
    assert fin.expenses and fin.expenses[0][0] == result["fee_cents"]
    assert any("搬到了杭州" in m for m in fin.memories)
    assert fin.diaries and "杭州" in fin.diaries[0]
    assert fin.schedules and fin.schedules[0][0] == "今日与老公搬家"
    assert fin.moods and fin.moods[0].get("兴奋", 0) > 0
    assert plugin.finance_repo.meta.get("move_pending_fee") is None


def test_move_with_short_balance_marks_pending_fee():
    plugin, fin = make(enough=False)
    result = asyncio.run(relocate(plugin, "北京"))
    assert result["ok"] and result["paid"] is False
    assert result["pending_cents"] == result["fee_cents"]
    assert int(plugin.finance_repo.meta["move_pending_fee"]) == result["fee_cents"]
    # 搬家照常完成：城市已切到北京
    assert city_view(plugin)["city"] == "北京"
