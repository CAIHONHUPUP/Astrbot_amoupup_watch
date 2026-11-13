# -*- coding: utf-8 -*-
"""三餐：每天在窗口内由她自己挑时点，按单品逐笔扣款并写记忆/小日记。

早餐 7:30–9:00，午餐 11:30–13:00，晚餐 17:30–19:30。
每个时段每天只吃一次（meta 幂等）。
"""
from __future__ import annotations

import datetime as _dt
import hashlib

try:
    from ..guard import isolated
except ImportError:  # 按顶层包/单文件加载时
    from guard import isolated

WINDOWS = {
    "breakfast": ((7, 30), (9, 0)),
    "lunch": ((11, 30), (13, 0)),
    "dinner": ((17, 30), (19, 30)),
}

MENUS = {
    "breakfast": (("豆浆", 1, "肠粉", 1), ("包子", 1, "白粥", 1), ("豆浆", 1, "油条", 1)),
    "lunch": (("快餐", 1,), ("面条", 1,), ("米饭套餐", 1,)),
    "dinner": (("米饭套餐", 1,), ("自己做饭", 1,), ("面条", 1,)),
}


#: 省钱模式（被扣钱/降薪后几天）：只吃便宜的
CHEAP_MENUS = {
    "breakfast": (("包子", 1, "白粥", 1), ("白粥", 1), ("包子", 1)),
    "lunch": (("面条", 1), ("自己做饭", 1)),
    "dinner": (("自己做饭", 1), ("面条", 1)),
}


def _hash_int(text, low, high):
    digest = hashlib.sha256(str(text).encode("utf-8")).hexdigest()
    span = max(1, high - low)
    return low + int(digest[:8], 16) % span


def _pick(day, slot, options):
    digest = hashlib.sha256(("%s:%s" % (day, slot)).encode("utf-8")).hexdigest()
    return options[int(digest[:8], 16) % len(options)]


class FinanceDaily:
    def __init__(self, plugin, repo, finance, consume):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance
        self.consume = consume

    def _in_window(self, slot, now):
        start, end = WINDOWS[slot]
        minutes = now.hour * 60 + now.minute
        return start[0] * 60 + start[1] <= minutes < end[0] * 60 + end[1]

    async def tick(self, now=None):
        now = now or _dt.datetime.now()
        day = now.date().isoformat()
        done = []
        thrifty = False
        salary_mod = getattr(self.plugin, "finance_salary", None)
        if salary_mod is not None:
            try:
                thrifty = bool(salary_mod.thrifty_active(now.date()))
            except Exception as exc:
                isolated("daily.thrifty", exc)
        for slot in WINDOWS:
            if not self._in_window(slot, now):
                continue
            flag = "meal:%s:%s" % (day, slot)
            if self.repo.meta_get(flag):
                continue
            menu = (CHEAP_MENUS if thrifty else MENUS)[slot]
            items = _pick(day, slot, menu)
            pairs = [{"item": items[i], "qty": items[i + 1]} for i in range(0, len(items), 2)]
            results = await self.consume.buy_items(pairs, idem="meal:%s:%s" % (day, slot), when=slot)
            exp = getattr(self.plugin, "finance_expense", None)
            if any(r.get("ok") for r in results):
                self.repo.meta_set(flag, "1")
                if exp is not None:
                    exp.reset_meal_failure(day)
            elif exp is not None:
                fails = exp.bump_meal_failure(day)
                if fails >= 2 and not self.repo.meta_get("hungry:" + day):
                    self.repo.meta_set("hungry:" + day, "1")
                    await exp.hungry(day, 1500)   # 一顿面条的钱
            done.append({"slot": slot, "items": pairs, "ok": [r.get("ok") for r in results]})
        done.extend(await self._extras(now, day))
        exp = getattr(self.plugin, "finance_expense", None)
        if exp is not None:
            try:
                await exp.rollup(now)
            except Exception as exc:
                isolated("daily.rollup", exc)
        return done

    # ---------- 交通 / 日用品 / 人情 / 冲动消费 ----------
    async def _extras(self, now, day):
        exp = getattr(self.plugin, "finance_expense", None)
        if exp is None:
            return []
        out = []
        minutes = now.hour * 60 + now.minute
        prop = getattr(self.plugin, "finance_property", None)
        has_car = bool(prop.asset("car")) if prop is not None else False
        # 交通：没车才坐公交地铁；有车按月算养车费
        if not has_car and now.weekday() < 5 and 6 * 60 + 30 <= minutes < 10 * 60:
            flag = "traffic:" + day
            if not self.repo.meta_get(flag):
                name = _pick(day, "traffic", (("公交", 1), ("地铁", 1), ("共享单车", 1)))[0]
                out.append(await self.consume.buy_item(name, 1, idem=flag, when="traffic"))
                self.repo.meta_set(flag, "1")
        # 日用品：隔几天补一次（纸巾 / 牙膏 / 洗衣液）
        if 9 <= now.hour <= 21 and not self.repo.meta_get("daily:" + day) \
                and _hash_int("daily:" + day, 0, 100) < 15:
            name = _pick(day, "daily", (("纸巾", 1), ("牙膏", 1), ("洗衣液", 1)))[0]
            out.append(await self.consume.buy_item(name, 1, idem="daily:" + day, when="daily"))
            self.repo.meta_set("daily:" + day, "1")
        # 人情：朋友聚餐 AA（常见）/ 随份子（少见、金额大）
        if not self.repo.meta_get("social:" + day):
            roll = _hash_int("social:" + day, 0, 1000)
            if 11 * 60 + 30 <= minutes < 13 * 60 and roll < 30:
                amount = _hash_int("meal_aa:" + day, 5000, 15000)
                out.append(await exp.spend("social", "朋友聚餐 AA", amount, day=day,
                                           idem="social:" + day))
                self.repo.meta_set("social:" + day, "1")
            elif 8 <= now.hour <= 20 and roll < 8:
                amount = _hash_int("gift:" + day, 20000, 100000)
                out.append(await exp.spend("social", "随份子", amount, day=day,
                                           idem="social:" + day))
                self.repo.meta_set("social:" + day, "1")
        # 看病：偶尔轻症（30–80）/ 中症（100–200），按现实物价 × 城市系数
        if not self.repo.meta_get("medical:" + day) and _hash_int("medical:" + day, 0, 1000) < 12:
            if 8 <= now.hour <= 21:
                severe = _hash_int("medical-level:" + day, 0, 100) < 30
                base_amount = (_hash_int("medical-mid:" + day, 10000, 20000) if severe
                               else _hash_int("medical-light:" + day, 3000, 8000))
                label = "看医生" if severe else "轻症买药"
                out.append(await exp.spend("medical", label, base_amount, day=day,
                                           idem="medical:" + day))
                if severe:
                    self.repo.meta_set("sick_until", day)
                self.repo.meta_set("medical:" + day, "1")
        # 冲动消费：心情极好或极差时，才可能买奶茶 / 盲盒 / 衣服
        if not self.repo.meta_get("impulse:" + day) and 10 <= now.hour <= 22:
            score = None
            try:
                score = self.plugin.finance.mood().get("score")
            except Exception:
                score = None
            mood_edge = score is not None and (float(score) >= 75 or float(score) <= 35)
            if mood_edge and _hash_int("impulse:" + day, 0, 100) < 25:
                roll = _hash_int("impulse_pick:" + day, 0, 100)
                if roll < 40:
                    out.append(await self.consume.buy_item("奶茶", 1, idem="impulse:" + day, when="impulse"))
                elif roll < 75:
                    out.append(await self.consume.buy_item("盲盒", 1, idem="impulse:" + day, when="impulse"))
                else:
                    amount = _hash_int("clothes:" + day, 10000, 30000)
                    out.append(await exp.spend("impulse", "买衣服", amount, day=day,
                                               idem="impulse:" + day))
                self.repo.meta_set("impulse:" + day, "1")
        return out
