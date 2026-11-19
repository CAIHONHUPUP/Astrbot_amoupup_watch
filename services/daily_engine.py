# -*- coding: utf-8 -*-
"""三餐：每天在窗口内由她自己挑时点，按单品逐笔扣款并写记忆/小日记。

早餐 7:30–9:00，午餐 11:00–13:00，晚餐 18:00–19:30（2026-11-17 按用户口径对齐）。
每个时段每天只吃一次（meta 幂等）。
"""
from __future__ import annotations
from typing import Any

import datetime as _dt
import time
import hashlib

try:
    from ..guard import isolated
    from ..utils.helpers import stable_int
    from ..utils.money import idem as idem_key
except ImportError:  # 按顶层包/单文件加载时
    from guard import isolated
    from utils.helpers import stable_int
    from utils.money import idem as idem_key

WINDOWS = {
    "breakfast": ((7, 30), (9, 0)),
    "lunch": ((11, 0), (13, 0)),
    "dinner": ((18, 0), (19, 30)),
}

MENUS = {
    "breakfast": (("豆浆", 1, "肠粉", 1), ("包子", 1, "白粥", 1), ("豆浆", 1, "油条", 1)),
    "lunch": (("快餐", 1,), ("面条", 1,), ("米饭套餐", 1,)),
    "dinner": (("米饭套餐", 1,), ("自己做饭", 1,), ("面条", 1,)),
}


#: 省钱模式（被扣钱/降薪后几天）：只吃便宜的
CHEAP_MENUS = {
    "breakfast": (("包子", 1, "白粥", 1), ("白粥", 1), ("包子", 1)),
    "lunch": (("面条", 1), ("自己做饭", 1), ("食堂套餐", 1)),
    "dinner": (("自己做饭", 1), ("面条", 1), ("食堂套餐", 1)),
}


#: 他/朋友点名的具体吃的（2026-11-19）：日程或活动里写了就直接按这个记
DISH_WORDS = ("酸菜鱼", "火锅", "寿司", "烤肉", "烧烤", "日料", "西餐",
              "小龙虾", "麻辣烫", "披萨")

#: 跟朋友/同事/亲戚吃饭（没点名吃什么时可以抽一个饭店菜）
FRIEND_WORDS = ("朋友", "同事", "表姐", "表妹", "闺蜜", "同学", "聚餐",
                "一起吃饭", "约我", "请我")

#: 吃饭地点关键词（2026-11-19）
HOME_WORDS = ("家", "宿舍", "出租屋", "住处", "公寓")
CANTEEN_WORDS = ("食堂", "饭堂")


def _hash_int(text, low, high):
    """稳定随机；实现已并入 utils.helpers（Phase 15 去重）。"""
    return stable_int(text, low, high)


def _pick(day, slot, options):
    digest = hashlib.sha256(("%s:%s" % (day, slot)).encode("utf-8")).hexdigest()
    return options[int(digest[:8], 16) % len(options)]


class FinanceDaily:
    def __init__(self, plugin, repo, finance, consume):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance
        self.consume = consume

    def _amo(self):
        getter = getattr(self.finance, "_amo", None)
        try:
            return getter() if callable(getter) else None
        except Exception:
            return None

    def _feed_hunger(self, slot, item_name):
        """三餐成功后喂饥饿值：补到 ~86 并 mark_meal；失败不影响记账。"""
        amo = self._amo()
        if amo is None:
            return False
        try:
            state = amo.hunger_state() or {}
            cur = float(state.get("value") or 0)
        except Exception:
            cur = 0.0
        try:
            if cur < 86.0:
                amo.add_hunger("amo", 86.0 - cur)
            amo.mark_meal(item_name or slot, 3.0, "满足")
            return True
        except Exception:
            return False

    def _where_text(self):
        """她此刻在哪、在干什么（读 amo 日程）；地点和活动拼在一起判断，拿不到返回空串。"""
        amo = self._amo()
        if amo is None:
            return ""
        try:
            act = amo.state.current_activity() or {}
        except Exception as exc:
            isolated("daily.place", exc)
            return ""
        return "%s %s" % (act.get("place") or "", act.get("activity") or "")

    def _meal_zone(self, place):
        """吃饭地点分三档：家里 / 食堂 / 外面；读不到地点返回空串。"""
        if not place:
            return ""
        if any(word in place for word in HOME_WORDS):
            return "home"
        if any(word in place for word in CANTEEN_WORDS):
            return "canteen"
        return "outside"

    def _menu_for_place(self, slot, menu):
        """按地点筛菜单（2026-11-19）：

        食堂只有食堂套餐/米饭套餐/面条；在家不出现食堂套餐；
        在外面（跟人下馆子）既不自己做饭、也不吃食堂。
        """
        if slot not in ("lunch", "dinner"):
            return menu
        where = self._where_text()
        for dish in DISH_WORDS:
            if dish in where:
                return ((dish, 1),)
        # 朋友/同事约饭但没点名吃什么：从饭店菜里抽一个（约会不抽，等他说具体吃什么）
        if any(word in where for word in FRIEND_WORDS):
            index = _hash_int(where + slot, 0, len(DISH_WORDS) - 1)
            return ((DISH_WORDS[index], 1),)
        zone = self._meal_zone(where)
        if not zone:
            return menu
        if zone == "canteen":
            allow = ("食堂套餐", "米饭套餐", "面条")
            filtered = tuple(opt for opt in menu
                             if any(name in allow for name in opt if isinstance(name, str)))
        elif zone == "home":
            filtered = tuple(opt for opt in menu if "食堂套餐" not in opt)
        else:
            filtered = tuple(opt for opt in menu
                             if "食堂套餐" not in opt and "自己做饭" not in opt)
        return filtered or menu

    def _in_window(self, slot, now):
        start, end = WINDOWS[slot]
        minutes = now.hour * 60 + now.minute
        return start[0] * 60 + start[1] <= minutes < end[0] * 60 + end[1]

    def _amo_meal_today(self, day, slot, now):
        """这个饭点共享钱包里是不是已经有一顿了（陪伴/别的路径先买过）。

        2026-11-18：守护 11:01 买了面条，陪伴 12:53 又抽到外卖；反过来
        陪伴先点外卖时守护也会再买一份。两边都只读同一本 wallet_ledger，
        谁先记账，另一边就跳过。
        """
        amo = self._amo()
        wallet = getattr(amo, "wallet", None) if amo is not None else None
        if wallet is None:
            return False
        start, end = WINDOWS[slot]
        low, high = start[0] * 60 + start[1], end[0] * 60 + end[1]
        try:
            rows = list(wallet.ledger(200) or [])
        except Exception:
            return False
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                ts = float(row.get("ts") or 0)
                delta = int(row.get("delta_cents") or 0)
            except (TypeError, ValueError):
                continue
            if delta >= 0:
                continue
            try:
                moment = _dt.datetime.fromtimestamp(ts)
            except (OverflowError, OSError, ValueError):
                continue
            if moment.strftime("%Y-%m-%d") != day:
                continue
            minute = moment.hour * 60 + moment.minute
            if not (low <= minute < high):
                continue
            idem = str(row.get("idem") or "")
            if idem.startswith("meal:"):
                continue  # 守护自己的三餐
            reason = str(row.get("reason") or "")
            if any(word in reason for word in ("奶茶", "果茶", "咖啡", "果汁", "饮料")) \
                    and not any(word in reason for word in ("饭", "面", "套餐", "包子", "粥")):
                continue
            if idem.startswith("food:micro:") or any(
                    word in reason for word in ("面条", "米饭", "快餐", "包子", "豆浆",
                                                "粥", "外卖", "家常菜", "泡面", "饭")):
                return True
        return False

    def _amo_claimed(self, day, low, high, prefixes=(), words=()):
        """共享钱包里今天这个时间窗是否已有指定来源/说法的支出。"""
        amo = self._amo()
        wallet = getattr(amo, "wallet", None) if amo is not None else None
        if wallet is None:
            return False
        try:
            rows = list(wallet.ledger(200) or [])
        except Exception:
            return False
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                ts = float(row.get("ts") or 0)
                raw = row.get("delta_cents")
                delta = int(raw if raw is not None else (row.get("delta") or 0))
            except (TypeError, ValueError):
                continue
            if delta >= 0:
                continue
            try:
                moment = _dt.datetime.fromtimestamp(ts)
            except (OverflowError, OSError, ValueError):
                continue
            if moment.strftime("%Y-%m-%d") != day:
                continue
            minute = moment.hour * 60 + moment.minute
            if not (low <= minute < high):
                continue
            idem = str(row.get("idem") or "")
            reason = str(row.get("reason") or "")
            if any(idem.startswith(prefix) for prefix in prefixes):
                return True
            if any(word in reason for word in words):
                return True
        return False

    def _amo_commute_mode(self, day, now):
        """今天的通勤方式，以陪伴日程为唯一口径。"""
        amo = self._amo()
        if amo is None:
            return ""
        getter = getattr(amo, "current_commute_mode", None)
        if not callable(getter):
            return ""
        try:
            return str(getter(now) or "")
        except Exception:
            return ""

    async def tick(self, now=None):
        now = now or _dt.datetime.now()
        day = now.date().isoformat()
        done: list[Any] = []
        try:
            from .health_bridge import is_asleep
        except ImportError:
            from services.health_bridge import is_asleep
        if is_asleep(self._amo()):
            return done
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
            flag = idem_key("meal", day, slot)
            if self.repo.meta_get(flag):
                continue
            if self._amo_meal_today(day, slot, now):
                self.repo.meta_set(flag, "1")
                continue
            menu = (CHEAP_MENUS if thrifty else MENUS)[slot]
            menu = self._menu_for_place(slot, menu)
            items = _pick(day, slot, menu)
            pairs = [{"item": items[i], "qty": items[i + 1]} for i in range(0, len(items), 2)]
            results = await self.consume.buy_items(pairs, idem=idem_key("meal", day, slot), when=slot)
            exp = getattr(self.plugin, "finance_expense", None)
            if any(r.get("ok") for r in results):
                self.repo.meta_set(flag, "1")
                self.repo.meta_set("last_meal_at", time.time())
                self._feed_hunger(slot, "、".join(str(p["item"]) for p in pairs))
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
        out: list[Any] = []
        try:
            from .health_bridge import is_asleep, is_sick
        except ImportError:
            from services.health_bridge import is_asleep, is_sick
        amo = self._amo()
        if is_asleep(amo):
            return out
        sick = is_sick(amo)
        minutes = now.hour * 60 + now.minute
        prop = getattr(self.plugin, "finance_property", None)
        has_car = bool(prop.asset("car")) if prop is not None else False
        # 交通：没车才坐公交地铁；有车按月算养车费
        if not has_car and not sick and now.weekday() < 5 and 6 * 60 + 30 <= minutes < 10 * 60:
            flag = "traffic:" + day
            if not self.repo.meta_get(flag):
                mode = self._amo_commute_mode(day, now)
                claimed = self._amo_claimed(day, 6 * 60 + 30, 10 * 60,
                                            prefixes=("micro:%s:subway" % day,),
                                            words=("地铁", "公交", "共享单车"))
                if claimed:
                    self.repo.meta_set(flag, "1")
                elif mode in ("地铁", "公交", "共享单车", "打车"):
                    out.append(await self.consume.buy_item(mode, 1, idem=flag, when="traffic"))
                    self.repo.meta_set(flag, "1")
                elif mode:
                    self.repo.meta_set(flag, "1")
                else:
                    out.append(await self.consume.buy_item("地铁", 1, idem=flag, when="traffic"))
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
        if self._amo() is None and not self.repo.meta_get("medical:" + day) and _hash_int("medical:" + day, 0, 1000) < 12:
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
            early_until = ""
            try:
                early_until = str(self.repo.meta_get("salary_early_until", "") or "")
            except Exception:
                early_until = ""
            # 提前发工资后的几天，冲动消费/逛街概率明显提高
            boost = bool(early_until and day <= early_until)
            # 2026-11-18 用户反馈盲盒太频繁：75 分只是「开心」的日常水平，不该天天算冲动；
            # 抬到 80/30 才算极端情绪，日常概率 25% -> 20%。
            mood_edge = score is not None and (float(score) >= 80 or float(score) <= 30)
            if (mood_edge or boost) and _hash_int("impulse:" + day, 0, 100) < (40 if boost else 20):
                roll = _hash_int("impulse_pick:" + day, 0, 100)
                if roll < 40:
                    if not self._amo_claimed(day, max(0, minutes - 120), minutes + 1,
                                             words=("奶茶", "果茶")):
                        out.append(await self.consume.buy_item("奶茶", 1, idem="impulse:" + day, when="impulse"))
                elif roll < 75:
                    out.append(await self.consume.buy_item("盲盒", 1, idem="impulse:" + day, when="impulse"))
                else:
                    amount = _hash_int("clothes:" + day, 10000, 30000)
                    out.append(await exp.spend("impulse", "买衣服", amount, day=day,
                                               idem="impulse:" + day))
                self.repo.meta_set("impulse:" + day, "1")
        return out
