# -*- coding: utf-8 -*-
"""逛街购物：周末/情绪/发薪触发，按余额档位消费，衣服同步陪伴衣橱。"""
from __future__ import annotations

import datetime as _dt
import json

try:
    from ..utils.helpers import city_factor, stable_int
    from .city import current_city, factor_of
except ImportError:
    from utils.helpers import city_factor, stable_int
    from services.city import current_city, factor_of

SHOP_MALL = (
    ("一条绿色碎花连衣裙", "skirt", "day", "绿色"),
    ("米白色针织开衫", "coat", "day", "米白色"),
    ("黑色高腰牛仔裤", "bottom", "day", "黑色"),
    ("真丝白色睡裙", "top", "night", "白色"),
    ("粉色珊瑚绒睡衣套装", "top", "night", "粉色"),
)
SHOP_CHEAP = (
    ("浅蓝色T恤", "top", "day", "浅蓝色"),
    ("白色帆布鞋", "shoes", "day", "白色"),
    ("灰色卫裤", "bottom", "day", "灰色"),
)


def _amo(plugin):
    finance = getattr(plugin, "finance", None)
    return finance._amo() if finance is not None and hasattr(finance, "_amo") else None


async def tick(plugin, now=None):
    now = now or _dt.datetime.now()
    amo = _amo(plugin)
    finance = getattr(plugin, "finance", None)
    repo = getattr(plugin, "finance_repo", None)
    if amo is None or finance is None or not hasattr(amo, "shopping_trigger"):
        return None
    try:
        reason = amo.shopping_trigger(now)
    except Exception:
        reason = ""
    if not reason:
        return None
    day = now.date().isoformat()
    try:
        if repo is not None and repo.meta_get("shopping:" + day, False):
            return None
    except Exception:
        pass
    cash = int(finance.balance_cents() or 0)
    try:
        factor = float(factor_of(current_city(plugin)) or 1.0)
    except Exception:
        factor = 1.0
    spent = []
    for label, base in (("逛街交通", 3000), ("奶茶", 2000), ("逛街午饭", 6000)):
        amount = max(500, int(base * factor))
        try:
            res = await finance.expense(amount, label, "shop:%s:%s" % (day, label), necessary=False)
        except Exception:
            res = {"ok": False}
        if isinstance(res, dict) and res.get("ok"):
            spent.append((label, amount))
    item = None
    buy = 0
    if cash > 300000:
        name, cat, slot, color = SHOP_MALL[stable_int(day + ":m", 0, len(SHOP_MALL))]
        buy = 50000 + stable_int(day + ":p", 0, 150000)
    elif cash > 50000:
        name, cat, slot, color = SHOP_CHEAP[stable_int(day + ":c", 0, len(SHOP_CHEAP))]
        buy = 10000 + stable_int(day + ":p", 0, 20000)
    if buy:
        buy = max(500, int(buy * factor))
        item = {"name": name, "color": color, "category": cat,
                "time_slot": slot, "price": buy // 100}
        try:
            repo.meta_set("shop:pending", json.dumps({
                "name": name, "color": color, "category": cat, "time_slot": slot,
                "price": buy, "cash": cash, "due": now.timestamp() + 15 * 60,
                "created": now.timestamp()}, ensure_ascii=False))
        except Exception:
            pass
        try:
            if cash < buy:
                await amo.push_to_owner(
                    "老公！我试了一件%s%s，超好看，要%d块，我的钱不够，能赞助我一点吗？"
                    % (color, name, buy // 100))
            else:
                await amo.push_to_owner(
                    "老公！我试了一件%s%s，超好看，只要%d块，我能买吗？"
                    % (color, name, buy // 100))
            if hasattr(amo, "note_shopping_pending"):
                amo.note_shopping_pending("你正在商场试%s，等老公回复，语气要充满期待。" % name,
                                          now.timestamp() + 15 * 60)
        except Exception:
            pass
    try:
        if repo is not None:
            repo.meta_set("shopping:" + day, "1")
            if item:
                events = _pending_events(repo)
                base = now.timestamp()
                events.append({"due": base + 90 * 60, "kind": "tryon",
                               "name": item["name"], "color": item["color"],
                               "price": item["price"], "cash": cash - buy})
                events.append({"due": base + 180 * 60, "kind": "home",
                               "name": item["name"], "color": item["color"],
                               "price": item["price"], "cash": cash - buy})
                repo.meta_set("shop:events", json.dumps(events[-20:], ensure_ascii=False))
    except Exception:
        pass
    except Exception:
        pass
    if finance is not None:
        try:
            if buy:
                finance.small_diary("今天%s，试了一件%s，等老公点头再买，好期待！" % (reason, item["name"] if item else "衣服"))
            else:
                finance.small_diary("今天%s，只逛不买，看到一条好漂亮的裙子但买不起，好难过。" % reason)
        except Exception:
            pass
    return {"reason": reason, "item": item, "spent": spent}


def _pending_events(repo):
    try:
        raw = repo.meta_get("shop:events", []) or []
        if isinstance(raw, str):
            raw = json.loads(raw or "[]")
        return list(raw)
    except Exception:
        return []


async def flush(plugin, now=None):
    """到点主动发：试穿问好看 / 回家炫耀（大额且余额低时心虚）。"""
    now = now or _dt.datetime.now()
    repo = getattr(plugin, "finance_repo", None)
    amo = _amo(plugin)
    if repo is None or amo is None or not hasattr(amo, "push_to_owner"):
        return None
    pending = _pending_order(repo)
    if pending and float(pending.get("due") or 0) <= now.timestamp():
        try:
            await amo.push_to_owner("算啦，你肯定在忙，我先放回衣架了。")
        except Exception:
            pass
        _clear_pending(repo)
    events = _pending_events(repo)
    keep, sent = [], []
    for event in events:
        try:
            due = float(event.get("due") or 0)
        except Exception:
            continue
        if due > now.timestamp():
            keep.append(event)
            continue
        kind = event.get("kind")
        name = str(event.get("name") or "衣服")
        color = str(event.get("color") or "")
        price = int(event.get("price") or 0)
        cash = int(event.get("cash") or 0)
        if kind == "tryon":
            text = "老公你看！我试了这件%s%s，好看吗？" % (color, name)
        else:
            text = "我回来啦！今天买了%s，花了%d元，穿上真的超好看！" % (name, price)
            if price > 500 and cash < 200000:
                text += "不过今天一冲动花了这么多，你不会骂我吧……接下来我要吃土了，求老公夸夸我。"
        try:
            await amo.push_to_owner(text)
            sent.append(text)
        except Exception:
            keep.append(event)
    try:
        repo.meta_set("shop:events", json.dumps(keep[-20:], ensure_ascii=False))
    except Exception:
        pass
    return {"sent": sent}


def _pending_order(repo):
    try:
        raw = repo.meta_get("shop:pending", {}) or {}
        if isinstance(raw, str):
            raw = json.loads(raw or "{}")
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _clear_pending(repo):
    try:
        repo.meta_set("shop:pending", {})
    except Exception:
        pass


async def handle_reply(plugin, text, now=None):
    """用户在 pending 期间回复：买/好看/转账 → 结算；太贵/不好看 → 放弃。"""
    now = now or _dt.datetime.now()
    repo = getattr(plugin, "finance_repo", None)
    finance = getattr(plugin, "finance", None)
    amo = _amo(plugin)
    pending = _pending_order(repo) if repo is not None else {}
    if not pending or finance is None or amo is None:
        return None
    body = str(text or "")
    buy = int(pending.get("price") or 0)
    import re
    transfer = re.search(r"转账\]?\s*([0-9]+(?:\.[0-9]+)?)", body)
    yes = bool(transfer) or any(w in body for w in ("买", "好看", "可以", "要", "好呀", "行"))
    no = any(w in body for w in ("太贵", "不好看", "不买", "算了", "别买", "不要"))
    if no:
        _clear_pending(repo)
        try:
            finance.nudge_mood({"委屈": 6, "开心": -5}, "shop:reject:%s" % int(now.timestamp()))
            await amo.push_to_owner("好吧，听你的，那不买了……")
        except Exception:
            pass
        return {"ok": True, "bought": False}
    if yes:
        cash = int(finance.balance_cents() or 0)
        if cash < buy:
            try:
                await amo.push_to_owner("啊哦，我的钱不够了，老公你能转我一点吗？")
            except Exception:
                pass
            return {"ok": False, "reason": "insufficient"}
        try:
            res = await finance.expense(buy, "买衣服：%s" % pending.get("name"),
                                        "shop:pending:%s" % int(pending.get("created") or 0), necessary=True)
        except Exception:
            res = {"ok": False}
        if not isinstance(res, dict) or not res.get("ok"):
            return {"ok": False, "reason": "expense"}
        try:
            amo.add_closet_item({"name": pending.get("name"), "color": pending.get("color"),
                                 "category": pending.get("category"), "time_slot": pending.get("time_slot"),
                                 "price": buy // 100})
            finance.small_diary("老公同意我买%s啦，超开心！" % pending.get("name"))
            await finance.permanent_memory("我买了%s（%s），%d 元。" % (pending.get("name"), pending.get("color"), buy // 100),
                                           "shop:pending:%s:mem" % int(pending.get("created") or 0))
            await amo.push_to_owner("谢谢老公！我买啦，等我回家穿给你看~")
        except Exception:
            pass
        _clear_pending(repo)
        try:
            events = _pending_events(repo)
            events.append({"due": now.timestamp() + 60 * 60, "kind": "home",
                           "name": pending.get("name"), "color": pending.get("color"),
                           "price": buy // 100, "cash": cash - buy})
            repo.meta_set("shop:events", json.dumps(events[-20:], ensure_ascii=False))
        except Exception:
            pass
        return {"ok": True, "bought": True}
    return None
