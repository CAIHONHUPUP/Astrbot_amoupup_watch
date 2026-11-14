# -*- coding: utf-8 -*-
"""人情礼节：NPC 生日、父母礼物、份子钱、转账尽孝。"""
from __future__ import annotations

import datetime as _dt
import json
import re

_PEOPLE = ("妈妈", "爸爸", "柳如烟", "白凝冰", "季博达", "方圆", "沐幼微")
PARENTS = ("妈妈", "爸爸")
GIFT_POOL = {
    "长辈": {"高档按摩仪": 150000, "丝巾": 30000, "保健品礼盒": 50000, "好酒": 100000},
    "闺蜜": {"大牌口红": 30000, "香水": 60000, "手办": 20000},
    "同事": {"水果篮": 20000, "茶叶礼盒": 40000},
}


def _birthdays(plugin):
    repo = _repo(plugin)
    raw = {}
    try:
        raw = repo.meta_get("npc_birthdays", {}) or {}
        if isinstance(raw, str):
            raw = json.loads(raw or "{}")
    except Exception:
        raw = {}
    if not raw and repo is not None:
        import random
        months = list(range(3, 13))
        random.shuffle(months)
        raw = {name: "%02d-%02d" % (months[i % len(months)], random.randint(1, 28))
               for i, name in enumerate(_PEOPLE)}
        try:
            repo.meta_set("npc_birthdays", json.dumps(raw, ensure_ascii=False))
        except Exception:
            pass
    return raw


def birthday_info(plugin, name, now=None):
    now = now or _dt.datetime.now()
    md = _birthdays(plugin).get(name, "")
    try:
        month, day = [int(x) for x in md.split("-")]
        when = _dt.date(now.year, month, day)
        if when < now.date():
            when = _dt.date(now.year + 1, month, day)
        days = (when - now.date()).days
    except Exception:
        return {"birthday": md, "days": None, "text": "生日：未知"}
    return {"birthday": md, "days": days,
            "text": "生日：%d月%d日（%s）" % (month, day,
                                            "就是今天" if days == 0 else ("还有%d天" % days))}


def _repo(plugin):
    return getattr(plugin, "finance_repo", None)


def _amo(plugin):
    finance = getattr(plugin, "finance", None)
    return finance._amo() if finance is not None and hasattr(finance, "_amo") else None


def _done_key(name, year):
    return "gift:%s:%s" % (name, year)


def upcoming(plugin, now=None):
    now = now or _dt.datetime.now()
    out = []
    for name, md in _birthdays(plugin).items():
        try:
            month, day = [int(x) for x in md.split("-")]
            when = _dt.date(now.year, month, day)
            delta = (when - now.date()).days
            if 0 <= delta <= 3:
                out.append((name, md, delta))
        except Exception:
            continue
    return out


async def tick(plugin, now=None):
    now = now or _dt.datetime.now()
    repo = _repo(plugin)
    finance = getattr(plugin, "finance", None)
    amo = _amo(plugin)
    if repo is None or finance is None:
        return None
    out = {}
    for name, md, delta in upcoming(plugin, now):
        year = now.year
        try:
            done = repo.meta_get(_done_key(name, year), False)
        except Exception:
            done = False
        if done:
            continue
        if amo is not None:
            try:
                cash_now = int(finance.balance_cents() or 0) if finance is not None else 0
                amo.note_gift_event(
                    "%s的生日还有%d天，可用余额 %.2f 元；可以调用 buy_gift 给她挑礼物。"
                    % (name, delta, cash_now / 100.0), now.timestamp() + 3 * 86400)
            except Exception:
                pass
        if delta > 0:
            out.setdefault("warn", []).append(name)
            continue
        cash = int(finance.balance_cents() or 0)
        if cash < 5000:
            if amo is not None:
                try:
                    amo.note_gift_event("妈妈生日快到了，你连买蛋糕的钱都没有，很内疚。",
                                        now.timestamp() + 2 * 86400)
                except Exception:
                    pass
            try:
                finance.small_diary("好穷，只能给%s买个小蛋糕，觉得自己好没用……" % name)
            except Exception:
                pass
            out.setdefault("broke", []).append(name)
            continue
        if name in PARENTS and cash > 80000:
            gift, cost = "金手镯", 120000
        elif name in PARENTS:
            gift, cost = "水果和小蛋糕", 15000
        else:
            gift, cost = "口红", 20000
        try:
            res = await finance.expense(cost, "礼物：%s" % gift, "gift:%s:%s" % (name, year), necessary=True)
        except Exception:
            res = {"ok": False}
        if isinstance(res, dict) and res.get("ok"):
            try:
                finance.small_diary("给%s买了个%s，%s一定很高兴！" % (name, gift, name))
                await finance.permanent_memory("给%s买了个%s。" % (name, gift),
                                               "gift:%s:%s:mem" % (name, year))
            except Exception:
                pass
            if amo is not None:
                try:
                    amo.note_gift_event("%s收到礼物很开心，还问起你了，说下次带你回家吃饭！" % name,
                                        now.timestamp() + 86400)
                except Exception:
                    pass
            out.setdefault("gift", []).append((name, gift, cost))
        try:
            repo.meta_set(_done_key(name, year), "1")
        except Exception:
            pass
    # 每月 8 号红色炸弹
    if now.day == 8:
        try:
            done = repo.meta_get("redbomb:%s-%s" % (now.year, now.month), False)
        except Exception:
            done = False
        if not done:
            try:
                res = await finance.expense(30000, "随份子", "redbomb:%s-%s" % (now.year, now.month),
                                            necessary=True)
            except Exception:
                res = {"ok": False}
            if isinstance(res, dict) and res.get("ok"):
                try:
                    finance.small_diary("这个月又收到一张红色炸弹，随了 300 块份子钱，肉疼。")
                except Exception:
                    pass
                out["redbomb"] = 30000
            try:
                repo.meta_set("redbomb:%s-%s" % (now.year, now.month), "1")
            except Exception:
                pass
    return out


async def maybe_transfer(plugin, text):
    match = re.search(r"转账\]?\s*([0-9]+(?:\.[0-9]+)?)", str(text or ""))
    if not match:
        return None
    cents = int(round(float(match.group(1)) * 100))
    if cents < 50000:
        return None
    amo = _amo(plugin)
    finance = getattr(plugin, "finance", None)
    if amo is None or finance is None:
        return None
    try:
        amo.note_gift_event("妈妈生日，老公给了你 %d 块让你去尽孝心，你感动得不行。" % (cents // 100),
                            float("inf"))
        finance.nudge_mood({"开心": 30, "感动": 30}, "gift:transfer:%s" % cents)
        await finance.permanent_memory("妈妈生日，老公给了我 %d 块让我去尽孝心，他真好。" % (cents // 100),
                                       "gift:transfer:%s:mem" % cents)
    except Exception:
        pass
    return {"ok": True, "message": "谢谢老公，我这就去给妈妈挑礼物！"}
