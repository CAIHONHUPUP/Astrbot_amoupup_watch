# -*- coding: utf-8 -*-
"""突发事件引擎：每月掷一次，倒霉/惊喜，全链路（钱/记忆/日记/情绪）。"""
from __future__ import annotations

import datetime as _dt

try:
    from ..guard import isolated
    from ..utils.helpers import stable_int, yuan
except ImportError:
    from guard import isolated
    from utils.helpers import stable_int, yuan

BAD = [
    ("手机屏幕摔碎", 80000, "手机啪一下掉地上，屏幕碎了……换屏 800，心也跟着碎了。"),
    ("电脑主板烧了", 150000, "电脑开不了机，修主板要 1500，我的钱啊……"),
    ("钱包被偷", 50000, "钱包被偷了，500 块没了，气到发抖。"),
]
GOOD = [
    ("路边捡到钱", 10000, "路上捡到 100 块！今天运气真好！"),
    ("年会抽中二等奖", 100000, "年会抽中二等奖，1000 块！开心到飞起！"),
    ("旧衣服里翻出钱", 20000, "旧衣服口袋翻出 200 块，像白捡的一样！"),
]


class FortuneEngine:
    def __init__(self, plugin, repo, finance):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance

    async def monthly_roll(self, now=None):
        now = now or _dt.datetime.now()
        month = now.strftime("%Y-%m")
        key = "fortune:" + month
        if self.repo.meta_get(key):
            return {"ok": False, "reason": "done", "dup": True}
        roll = stable_int("fortune-roll:" + month, 0, 1000)
        if roll >= 100:
            self.repo.meta_set(key, "none")
            return {"ok": False, "reason": "none"}
        bad = roll < 60
        table = BAD if bad else GOOD
        name, amount, diary = table[stable_int("fortune-pick:" + month, 0, len(table))]
        mood = {"生气": 12, "委屈": 10, "暴躁": 8} if bad else {"开心": 12, "兴奋": 10}
        word = "崩溃" if bad else "狂喜"
        if bad:
            res = await self.finance.expense(amount, "突发事件：" + name, "fortune:" + month, necessary=True)
        else:
            res = await self.finance.income(amount, "突发事件：" + name, "fortune:" + month)
        ok = bool(isinstance(res, dict) and res.get("ok", True))
        self.finance.nudge_mood(mood, "fortune-mood:" + month)
        self.finance.small_diary("【突发事件】" + diary)
        try:
            await self.finance.permanent_memory(diary, "fortune:" + month)
        except Exception as exc:
            isolated("fortune.memory", exc)
        self.repo.meta_set(key, "%s:%s" % (name, amount))
        return {"ok": ok, "bad": bad, "name": name, "amount_cents": amount,
                "mood": word, "diary": diary}


def build_fortune(plugin):
    return FortuneEngine(plugin, getattr(plugin, "finance_repo", None), getattr(plugin, "finance", None))
