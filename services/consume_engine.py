# -*- coding: utf-8 -*-
"""单品级消费：按现实物价扣款，一项一条，不合并、不虚构。"""
from __future__ import annotations

import logging
logger = logging.getLogger("amo.watch")

import time

from .city import current_city, factor_of
from .expense_engine import category_for

#: 通用商品名 → 单价（分）。可按城市微调，但不离谱。
CATALOG = {
    "豆浆": 150, "肠粉": 600, "包子": 200, "油条": 200, "白粥": 200,
    "快餐": 1800, "面条": 1500, "米饭套餐": 2200, "自己做饭": 1500, "食堂套餐": 1500,
    "酸菜鱼": 6800, "火锅": 9800, "寿司": 7800, "烤肉": 8800, "烧烤": 5800,
    "日料": 7600, "西餐": 8800, "小龙虾": 9800, "麻辣烫": 2500, "披萨": 6800,
    "公交": 200, "地铁": 500, "共享单车": 150, "打车": 2500,
    "洗衣液": 3000, "牙膏": 1500, "纸巾": 1200,
    "水果": 1200, "牛奶": 500,
    "奶茶": 1500, "盲盒": 5900,
}

#: 三餐时段 → 账本分类。显式给，别让「面条 × 1」被文字规则猜成午餐。
WHEN_META = {
    "breakfast": ("餐饮", "早餐"),
    "lunch": ("餐饮", "午餐"),
    "dinner": ("餐饮", "晚餐"),
}

#: 允许的区间（分），用于校验价格没跑偏
RANGES = {
    "快餐": (1500, 2500), "面条": (1200, 1800), "米饭套餐": (1800, 2800),
    "自己做饭": (1000, 2000), "食堂套餐": (1200, 1800), "地铁": (300, 800), "打车": (2000, 3500),
    "酸菜鱼": (5000, 9000), "火锅": (7000, 15000), "寿司": (6000, 12000), "烤肉": (6000, 12000),
    "烧烤": (4000, 8000), "日料": (6000, 12000), "西餐": (6000, 15000), "小龙虾": (6000, 15000),
    "麻辣烫": (1500, 4000), "披萨": (5000, 9000),
    "洗衣液": (2000, 4000), "牙膏": (1000, 2000), "纸巾": (1000, 2000),
}


class FinanceConsume:
    def __init__(self, plugin, repo, finance):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance

    def price_cents(self, item):
        """单价 × 城市物价系数（一线 1.5、二线 1.0、三线 0.75）。"""
        name = str(item or "").strip()
        if name not in CATALOG:
            return None
        factor = factor_of(current_city(self.plugin))
        return int(round(int(CATALOG[name]) * factor))

    async def buy_item(self, item, qty=1, idem="", when=""):
        """买一样东西：单独一笔扣款 + 单独一条记忆/小日记。"""
        name = str(item or "").strip()
        qty = max(1, int(float(qty or 1)))
        price = self.price_cents(name)
        if price is None:
            return {"ok": False, "message": "不认识这个商品：%s" % name}
        amount = price * qty
        idem = str(idem or ("consume:%s:%s:%d" % (name, when or __import__("time").strftime("%Y%m%d%H%M"), qty)))
        label = "%s × %d" % (name, qty)
        meta = WHEN_META.get(str(when or "").strip().lower())
        extra = {} if meta is None else {
            "category": meta[0], "sub_category": meta[1],
            "item_name": name, "trigger_reason": label,
        }
        try:
            result = await self.finance.expense(amount, label, idem, necessary=False,
                                                quiet=True, **extra)
        except TypeError:
            # 测试替身/老适配器不接受分类参数：退回最小调用（生产实现始终支持）
            result = await self.finance.expense(amount, "%s × %d" % (name, qty), idem,
                                                necessary=False, quiet=True)
        if not result.get("ok"):
            return {"ok": False, "message": result.get("reason") or "余额不足"}
        # 逐笔记账（分类汇总用）；长期记忆留给日终 rollup
        mod = getattr(self.plugin, "finance_expense", None)
        if mod is not None:
            try:
                mod.record(time.strftime("%Y-%m-%d"), category_for(name),
                           "%s × %d" % (name, qty), amount, idem)
            except Exception as exc:
                logger.debug("[WATCH] optional step failed: %r", exc)
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
        base = idem or ("basket:%s" % __import__("time").strftime("%Y%m%d%H%M%S"))
        for index, entry in enumerate(items or []):
            name = entry.get("item")
            qty = entry.get("qty", 1)
            results.append(await self.buy_item(name, qty, idem="%s:%d" % (base, index), when=when))
        return results
