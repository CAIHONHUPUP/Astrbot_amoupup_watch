# -*- coding: utf-8 -*-
"""日常开销：逐笔扣款 + 分类记账 + 日终汇总。

规则（2026-11-13 用户明确）：

- 三餐、交通、日用品、人情娱乐、冲动消费全部**逐笔**扣 amo 可用余额，不月底合并；
- 余额不够当笔失败（买不起就不买），连续吃不上饭要触发严重情绪下降并推高求助优先级；
- 每笔只写小日记，不写长篇；长期记忆按**天分类汇总**（“今日餐饮 ¥35，交通 ¥4”）；
- 心情：日用品「平静」、冲动消费「开心」、大额人情「难过、心疼」。
"""
from __future__ import annotations

import datetime as _dt

try:
    from ..guard import isolated
except ImportError:  # 按顶层包/单文件加载时
    from guard import isolated

#: 分类顺序（面板按这个顺序展示）
CATEGORY_ORDER = ("food", "transport", "daily", "social", "impulse", "medical")
CATEGORY_LABELS = {
    "food": "餐饮饮食",
    "transport": "交通出行",
    "daily": "日用品",
    "social": "人情娱乐",
    "impulse": "冲动消费",
    "medical": "医疗健康",
}

#: 商品 → 分类
ITEM_CATEGORY = {
    "豆浆": "food", "肠粉": "food", "包子": "food", "油条": "food", "白粥": "food",
    "快餐": "food", "面条": "food", "米饭套餐": "food", "自己做饭": "food",
    "水果": "food", "牛奶": "food", "黄焖鸡": "food",
    "公交": "transport", "地铁": "transport", "共享单车": "transport", "打车": "transport",
    "洗衣液": "daily", "牙膏": "daily", "纸巾": "daily", "洗发水": "daily",
    "奶茶": "impulse", "盲盒": "impulse", "衣服": "impulse",
    "聚餐": "social", "份子钱": "social", "礼物": "social",
    "轻症买药": "medical", "看医生": "medical", "住院": "medical",
}


def category_for(item):
    return ITEM_CATEGORY.get(str(item or "").strip(), "daily")


class FinanceExpense:
    def __init__(self, plugin, repo, finance):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance

    # ---------- 记账 ----------
    def record(self, day, category, label, amount_cents, idem):
        try:
            return self.repo.add_expense(str(idem), str(day), str(category),
                                         str(label), int(amount_cents))
        except Exception as exc:
            isolated("expense.record", exc)
            return False

    def summary(self, period):
        """按月或按天（传 '2026-11-13'）汇总分类。"""
        period = str(period or "")
        try:
            rows = self.repo.expense_totals(period)
        except Exception as exc:
            isolated("expense.summary", exc)
            rows = []
        totals = {str(r.get("category") or ""): int(r.get("total") or 0) for r in rows}
        items = [{"key": key, "label": CATEGORY_LABELS[key], "amount_cents": totals.get(key, 0)}
                 for key in CATEGORY_ORDER]
        extra = [{"key": k, "label": k or "其他", "amount_cents": v}
                 for k, v in totals.items() if k not in CATEGORY_LABELS and v]
        total = sum(totals.values())
        return {"period": period, "total_cents": total, "categories": items + extra,
                "months": None}

    def month_summary(self, now=None):
        now = now or _dt.date.today()
        return self.summary(now.strftime("%Y-%m"))

    def day_summary(self, day):
        return self.summary(day.isoformat() if hasattr(day, "isoformat") else str(day))

    # ---------- 花钱 ----------
    def _mood_for(self, category, amount_cents):
        if category == "impulse":
            return {"开心": 6, "兴奋": 3}
        if category == "social" and int(amount_cents) >= 50000:
            return {"委屈": 8, "难过": 6}      # 随份子 500+
        return None                            # 日用品等：平静

    def _sick_guard(self, category, amount_cents, label, day=None):
        """生病期间：禁冲动消费，三餐换成白粥/面条（10-15 元档）。"""
        medical = getattr(self.plugin, "medical", None)
        if medical is None or not medical.is_sick(day):
            return label, int(amount_cents), None
        if category == "impulse":
            return label, int(amount_cents), "生病了别乱花钱"
        if category == "food" and int(amount_cents) > 1500:
            return "白粥", 800, None
        return label, int(amount_cents), None

    async def spend(self, category, label, amount_cents, day=None, idem="", necessary=False):
        """扣一笔日常开销：先扣钱，成功才记账。买不起就返回失败，不记账。"""
        label, amount_cents, blocked = self._sick_guard(category, amount_cents, label, day)
        if blocked:
            return {"ok": False, "reason": blocked, "skipped": True}
        day_text = day.isoformat() if hasattr(day, "isoformat") else str(day or _dt.date.today())
        amount = int(amount_cents)
        if amount <= 0:
            return {"ok": False, "reason": "zero"}
        key = str(idem or ("spend:%s:%s:%d" % (day_text, label, amount)))
        result = await self.finance.expense(amount, label, key, necessary=necessary, quiet=True)
        if not result.get("ok"):
            return {"ok": False, "reason": result.get("reason") or "insufficient", "label": label}
        self.record(day_text, category, label, amount, key)
        deltas = self._mood_for(category, amount)
        if deltas:
            try:
                self.finance.nudge_mood(deltas, key + ":mood")
            except Exception as exc:
                isolated("expense.mood", exc)
        return {"ok": True, "label": label, "amount_cents": amount, "category": category}

    # ---------- 日终汇总记忆 ----------
    async def rollup(self, now=None):
        now = now or _dt.datetime.now()
        days = []
        today = now.date()
        if now.hour >= 22:
            days.append(today)
        days.append(today - _dt.timedelta(days=1))
        done = []
        for day in days:
            flag = "expense_rollup:" + day.isoformat()
            if self.repo.meta_get(flag):
                continue
            summary = self.day_summary(day)
            if not summary["total_cents"]:
                self.repo.meta_set(flag, "empty")
                continue
            parts = ["%s %s" % (item["label"], "¥%.2f" % (item["amount_cents"] / 100.0))
                     for item in summary["categories"] if item["amount_cents"]]
            text = "%s 日常开销：%s，合计 ¥%.2f。" % (
                day.strftime("%m-%d"), "，".join(parts), summary["total_cents"] / 100.0)
            try:
                await self.finance.long_memory(text, "expense_day:" + day.isoformat())
                self.repo.meta_set(flag, "1")
                done.append(text)
            except Exception as exc:
                isolated("expense.rollup", exc)
        return {"rolled": done}

    # ---------- 饿肚子 ----------
    def bump_meal_failure(self, day):
        key = "meal_fail:" + str(day)
        try:
            count = int(self.repo.meta_get(key, 0) or 0) + 1
        except Exception:
            count = 1
        self.repo.meta_set(key, count)
        return count

    def reset_meal_failure(self, day):
        self.repo.meta_set("meal_fail:" + str(day), 0)

    async def hungry(self, day, need_cents=0):
        """连续吃不上饭：严重情绪下降 + 推高求助优先级。"""
        text = "今天饿得头晕，连着几顿都没吃上，委屈得想哭。"
        try:
            await self.finance.low_balance(max(0, int(need_cents)), "hungry:%s" % day)
        except Exception as exc:
            isolated("expense.hungry.help", exc)
        try:
            self.finance.nudge_mood({"委屈": 15, "难过": 10, "郁闷": 8}, "hunger:%s" % day)
        except Exception as exc:
            isolated("expense.hungry.mood", exc)
        try:
            await self.finance.remember_event("饥饿", int(need_cents), text, "hunger:%s" % day)
        except Exception as exc:
            isolated("expense.hungry.memory", exc)
        self.repo.meta_set("hungry_until", str(day))
        return text
