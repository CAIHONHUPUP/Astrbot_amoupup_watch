# -*- coding: utf-8 -*-
"""城市来源：amo 优先，搬家记录只在 amo 不可用时兜底。"""
import types

from services.city import city_view


class FakePlugin:
    def __init__(self, override="", amo=None):
        self.config = {}
        self.city_override = override
        self.finance = types.SimpleNamespace(_amo=lambda: amo)


class Amo:
    class state:
        worldview = {"city": "杭州"}


def test_amo_wins_over_move_record():
    plugin = FakePlugin(override="东莞", amo=Amo())
    assert city_view(plugin)["city"] == "杭州"


def test_move_record_used_when_amo_missing():
    plugin = FakePlugin(override="东莞", amo=None)
    assert city_view(plugin)["city"] == "东莞"
