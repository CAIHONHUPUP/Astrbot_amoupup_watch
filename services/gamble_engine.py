# -*- coding: utf-8 -*-
"""赌徒补仓：连跌 3 天 + 情绪崩溃 → 割肉/贷款加仓，日记写死嘴硬的话。"""
from __future__ import annotations

import datetime as _dt

try:
    from ..guard import isolated
    from ..utils.helpers import stable_int, yuan
except ImportError:
    from guard import isolated
    from utils.helpers import stable_int, yuan

CRASH_WORDS = ("崩溃", "生气", "委屈", "暴躁")


class GambleEngine:
    def __init__(self, plugin, repo, finance, trading, help_engine=None):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance
        self.trading = trading
        self.help = help_engine

    async def maybe_gamble(self, quotes, positions, mood_word, now=None):
        now = now or _dt.datetime.now()
        day = now.date().isoformat()
        key = "gamble:" + day
        if self.repo.meta_get(key):
            return {"ok": False, "reason": "done"}
        if not any(word in str(mood_word) for word in CRASH_WORDS):
            return {"ok": False, "reason": "mood"}
        losers = []
        for pos in positions or []:
            code = str(pos.get("code") or "")
            quote = (quotes or {}).get(code) or {}
            hist = []
            try:
                hist = list(self.repo.meta_get("hist:" + code) or [])
            except Exception:
                hist = []
            if len(hist) >= 3 and hist[-1] < hist[-2] < hist[-3]:
                losers.append(pos)
        if not losers:
            return {"ok": False, "reason": "no_drop"}
        if stable_int(key + ":roll", 0, 100) >= 50:
            self.repo.meta_set(key, "skip")
            return {"ok": False, "reason": "roll"}
        target = max(losers, key=lambda p: int(p.get("cost_cents") or 0))
        code = str(target.get("code") or "")
        # 先割肉其他持仓
        for pos in positions or []:
            if str(pos.get("code")) == code:
                continue
            qty = int(pos.get("qty") or 0)
            if qty > 0:
                try:
                    await self.trading.sell(pos["code"], qty, None, key + ":cut")
                except Exception as exc:
                    isolated("gamble.sell", exc)
        cash = int(self.finance.balance_cents() or 0)
        if cash < 100000 and self.help is not None:
            try:
                await self.help.take_loan(100000, 12, 0.06, key + ":loan")
            except Exception as exc:
                isolated("gamble.loan", exc)
        bought = None
        try:
            bought = await self.trading.buy(code, 100, None, key + ":add")
        except Exception as exc:
            isolated("gamble.buy", exc)
        self.finance.nudge_mood({"兴奋": 10, "生气": 8}, key + ":mood")
        self.finance.small_diary("【投资】我不信它不涨！我要把亏的都赚回来！")
        try:
            await self.finance.permanent_memory("补仓 %s 上头了，割肉加借钱也要加。" % code, key)
        except Exception as exc:
            isolated("gamble.memory", exc)
        self.repo.meta_set(key, code)
        return {"ok": True, "code": code, "bought": bought}


def build_gamble(plugin):
    return GambleEngine(plugin, getattr(plugin, "finance_repo", None),
                        getattr(plugin, "finance", None), getattr(plugin, "trading", None),
                        getattr(plugin, "finance_help", None))
