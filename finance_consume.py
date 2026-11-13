# -*- coding: utf-8 -*-
"""单品级消费：按现实物价扣款，一项一条，不合并、不虚构。"""
from __future__ import annotations

#: 通用商品名 → 单价（分）。可按城市微调，但不离谱。
CATALOG = {
    "豆浆": 150, "肠粉": 600, "包子": 200, "油条": 200, "白粥": 200,
    "快餐": 1800, "面条": 1500, "米饭套餐": 2200, "自己做饭": 1500,
    "公交": 200, "地铁": 500, "共享单车": 150,
    "洗衣液": 3000, "牙膏": 1500, "纸巾": 1200,
    "水果": 1200, "牛奶": 500,
}

#: 允许的区间（分），用于校验价格没跑偏
RANGES = {
    "快餐": (1500, 2500), "面条": (1200, 1800), "米饭套餐": (1800, 2800),
    "自己做饭": (1000, 2000), "地铁": (300, 800),
    "洗衣液": (2000, 4000), "牙膏": (1000, 2000), "纸巾": (1000, 2000),
}


class FinanceConsume:
    def __init__(self, plugin, repo, finance):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance

    def price_cents(self, item):
        name = str(item or "").strip()
        if name in CATALOG:
            return int(CATALOG[name])
        return None

    async def buy_item(self, item, qty=1, idem="", when=""):
        """买一样东西：单独一笔扣款 + 单独一条记忆/小日记。"""
        name = str(item or "").strip()
        qty = max(1, int(float(qty or 1)))
        price = self.price_cents(name)
        if price is None:
            return {"ok": False, "message": "不认识这个商品：%s" % name}
        amount = price * qty
        idem = str(idem or ("consume:%s:%s:%d" % (name, when or "", qty)))
        result = await self.finance.expense(amount, "%s × %d" % (name, qty), idem, necessary=False)
        if not result.get("ok"):
            return {"ok": False, "message": result.get("reason") or "余额不足"}
        return {
            "ok": True,
            "item": name, "qty": qty, "unit_cents": price, "amount_cents": amount,
            "mood": result.get("mood"),
            "text": "买了 %s × %d，单价 %.2f 元，一共 %.2f 元" % (
                name, qty, price / 100.0, amount / 100.0),
        }

    async def buy_items(self, items, idem="", when=""):
        """一次买多样：逐项扣款、逐项记录，不合并成一条。"""
        results = []
        for index, entry in enumerate(items or []):
            name = entry.get("item")
            qty = entry.get("qty", 1)
            results.append(await self.buy_item(name, qty, idem="%s:%d" % (idem or "basket", index), when=when))
        return results
