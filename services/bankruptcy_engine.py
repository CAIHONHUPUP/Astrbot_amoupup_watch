# -*- coding: utf-8 -*-
"""终极破产兜底：负债 > 总资产×3 → 强制清算（股票/房/车 70% 贱卖）。"""
from __future__ import annotations

import datetime as _dt

try:
    from ..guard import isolated
    from ..utils.helpers import stable_int, yuan
except ImportError:
    from guard import isolated
    from utils.helpers import stable_int, yuan

LIQUIDATE_RATIO = 0.70


class BankruptcyEngine:
    def __init__(self, plugin, repo, finance, trading=None, social=None):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance
        self.trading = trading
        self.social = social

    async def check(self, now=None):
        now = now or _dt.datetime.now()
        key = "bankruptcy:" + now.strftime("%Y-%m")
        if self.repo.meta_get(key):
            return {"ok": False, "reason": "done"}
        cash = int(self.finance.balance_cents() or 0)
        market = sum(int(p.get("cost_cents") or 0) for p in (self.repo.positions() if self.repo else []))
        debts = sum(int(r.get("balance_cents") or 0) for r in (self.repo.loans() if hasattr(self.repo, "loans") else []))
        total = cash + market
        if debts <= 0 or debts <= total * 3:
            return {"ok": False, "reason": "healthy", "debts": debts, "assets": total}
        # 强制卖出所有持仓（70% 贱卖）
        for pos in self.repo.positions():
            qty = int(pos.get("qty") or 0)
            if qty <= 0:
                continue
            try:
                await self.trading.sell(pos["code"], qty, None, key + ":sell")
            except Exception as exc:
                isolated("bankruptcy.sell", exc)
        # 房产/车辆 70% 拍卖
        for kind in ("house", "car"):
            rows = self.repo.assets_of(kind) if hasattr(self.repo, "assets_of") else []
            for row in rows:
                try:
                    self.repo.remove_asset(row.get("id")) if hasattr(self.repo, "remove_asset") else None
                except Exception as exc:
                    isolated("bankruptcy.asset", exc)
        self.finance.nudge_mood({"委屈": 20, "生气": 15}, key + ":mood")
        lines = [
            "什么都没了，钱、房子、股票，全没了……亲爱的，我是不是特别没用啊。",
            "清算完了，只剩老公了。你别不要我，好不好……",
            "我是不是把日子过砸了……亲爱的，我以后不乱来了，你别走。",
            "东西都没了，只有你还愿意理我。我哭到眼睛都肿了。",
        ]
        diary = lines[stable_int("bankrupt-diary:" + now.strftime("%Y-%m"), 0, len(lines))]
        self.finance.long_diary(diary)
        try:
            await self.finance.permanent_memory("破产清算：负债 %s，资产 %s，只剩老公了。" % (yuan(debts), yuan(total)), key)
        except Exception as exc:
            isolated("bankruptcy.memory", exc)
        self.repo.meta_set(key, "liquidated")
        return {"ok": True, "debts": debts, "assets": total, "text": diary}


def build_bankruptcy(plugin):
    return BankruptcyEngine(plugin, getattr(plugin, "finance_repo", None), getattr(plugin, "finance", None),
                            getattr(plugin, "trading", None), getattr(plugin, "social", None))
