# -*- coding: utf-8 -*-
"""约会与失信：约定写入日程、取消时联动情绪/定金/安慰消费/记忆。"""
from __future__ import annotations

import datetime as _dt
import json
import re
import time

CANCEL_WORDS = ("去不了", "去不成", "不去了", "取消约会", "放鸽子", "临时加班", "改天吧")


def _repo(plugin):
    return getattr(plugin, "finance_repo", None)


def _finance(plugin):
    return getattr(plugin, "finance", None)


def _amo(plugin):
    finance = _finance(plugin)
    return finance._amo() if finance is not None and hasattr(finance, "_amo") else None


def _load(plugin):
    try:
        raw = _repo(plugin).meta_get("date:list", [])
        if isinstance(raw, str):
            raw = json.loads(raw or "[]")
        return list(raw or [])
    except Exception:
        return []


def _save(plugin, rows):
    try:
        _repo(plugin).meta_set("date:list", json.dumps(rows[-30:], ensure_ascii=False))
    except Exception:
        pass


def _parse_when(text, now=None):
    now = now or _dt.datetime.now()
    body = str(text or "").strip()
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})", body)
    if match:
        return _dt.datetime(*[int(x) for x in match.groups()])
    match = re.search(r"(\d{1,2}):(\d{2})", body)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return target if target > now else target + _dt.timedelta(days=1)
    return now + _dt.timedelta(days=1)


async def make_date(plugin, title, when="", deposit_cents=0, now=None):
    """约定一件事：写进她自己的日程 + 进入期待状态。"""
    title = str(title or "").strip()
    if not title:
        return {"ok": False, "message": "要约什么？" }
    now = now or _dt.datetime.now()
    when_dt = _parse_when(when, now)
    deposit = max(0, int(deposit_cents or 0))
    if not deposit and any(word in title for word in ("火锅", "烤肉", "自助")):
        deposit = 5000  # 订位定金：50 元
    rows = _load(plugin)

    def _norm(value):
        return str(value or "").replace("吃", "").replace("去", "").strip()

    for row in rows:
        if row.get("status") != "pending":
            continue
        if _norm(row.get("title")) != _norm(title):
            continue
        try:
            old_when = _dt.datetime.strptime(str(row.get("when")), "%Y-%m-%d %H:%M")
            close = abs((old_when - when_dt).total_seconds()) <= 4 * 3600
            created_close = abs(now.timestamp() - float(row.get("created") or 0)) <= 4 * 3600
        except Exception:
            close, created_close = False, False
        if close or created_close:
            return {"ok": False, "duplicate": True,
                    "message": "我们不是已经约好%s了吗？我记着呢，等会儿见！" % title}

    entry = {"title": title, "when": when_dt.strftime("%Y-%m-%d %H:%M"),
             "deposit": deposit, "status": "pending", "compensated": False,
             "created": now.timestamp()}
    rows.append(entry)
    _save(plugin, rows)
    amo = _amo(plugin)
    if amo is not None:
        try:
            item = {"start": when_dt.strftime("%H:%M"),
                    "end": (when_dt + _dt.timedelta(hours=2)).strftime("%H:%M"),
                    "activity": "和老公" + title, "category": "social",
                    "place": "外面", "zone": "restaurant"}
            amo.state.add_schedule_item(when_dt.date().isoformat(), item,
                                        source="date", reason="约会")
        except Exception:
            pass
        try:
            amo.set_date_mood("你在期待和老公%s，心情很好。" % title,
                              (when_dt + _dt.timedelta(hours=6)).timestamp())
        except Exception:
            pass
    finance = _finance(plugin)
    if finance is not None:
        try:
            finance.nudge_mood({"开心": 10}, "date:make:%s" % int(now.timestamp()))
        except Exception:
            pass
    return {"ok": True, "message": "好呀，那说定啦：%s %s。" % (when_dt.strftime("%m-%d %H:%M"), title)}


def _pick_pending(rows, title=""):
    pending = [row for row in rows if row.get("status") == "pending"]
    if not pending:
        return None
    text = str(title or "")
    for row in reversed(pending):
        if text and text in str(row.get("title") or ""):
            return row
    return pending[-1] if not text or len(pending) == 1 else None


def find_pending(plugin, title=""):
    return _pick_pending(_load(plugin), title)


async def cancel_date(plugin, title="", reason="", now=None):
    """被放鸽子：情绪、定金、安慰消费、记忆、两天冷淡。"""
    now = now or _dt.datetime.now()
    rows = _load(plugin)
    target = _pick_pending(rows, title)
    if target is None:
        return {"ok": False, "message": "最近没有约好的事呀。"}
    try:
        when_dt = _dt.datetime.strptime(str(target.get("when")), "%Y-%m-%d %H:%M")
    except Exception:
        when_dt = now
    lead_hours = (when_dt - now).total_seconds() / 3600.0
    target["status"] = "cancelled"
    target["cancelled_at"] = now.timestamp()
    target["lead_hours"] = round(lead_hours, 1)
    _save(plugin, rows)

    finance = _finance(plugin)
    idem = "date:cancel:%s" % int(now.timestamp())
    late = lead_hours < 6
    deltas = {"委屈": 28, "郁闷": 18, "开心": -18, "生气": 10} if late else {"开心": -5, "委屈": 8}
    if finance is not None:
        try:
            finance.nudge_mood(deltas, idem + ":mood")
        except Exception:
            pass
    cash = int(finance.balance_cents() or 0) if finance is not None and hasattr(finance, "balance_cents") else 0
    deposit = int(target.get("deposit") or 0)
    lost = 0
    if deposit and cash >= deposit and finance is not None:
        try:
            res = await finance.expense(deposit, "约会定金损失", idem + ":deposit", necessary=True)
            if isinstance(res, dict) and res.get("ok"):
                lost = deposit
        except Exception:
            pass
    broke = cash < 500
    comfort = 0
    if late and not broke and finance is not None:
        comfort = min(max(cash, 0), 5000 + (int(now.timestamp()) % 5000))
        comfort = max(5000, min(10000, comfort))
        try:
            res = await finance.expense(comfort, "安慰外卖", idem + ":comfort", necessary=True)
            if not (isinstance(res, dict) and res.get("ok")):
                comfort = 0
        except Exception:
            comfort = 0
    # 记忆 / 日记
    text = "X月X日老公答应陪我%s，结果放我鸽子（提前%.1f小时取消）。" % (target.get("title"), max(0.0, lead_hours))
    if finance is not None:
        try:
            await finance.permanent_memory(text, idem + ":mem")
        except Exception:
            pass
        try:
            if broke:
                finance.small_diary("火锅没吃成，连泡面都快吃不起了，好委屈。")
            elif lost:
                finance.small_diary("火锅没吃成，定金也退不了，好亏。")
            else:
                finance.small_diary("本来约好%s的，结果他去不了，有点失落。" % target.get("title"))
            if comfort:
                finance.small_diary("没人陪我吃，我自己点外卖总行了吧！花了 %.2f 元。" % (comfort / 100.0))
        except Exception:
            pass
    amo = _amo(plugin)
    if amo is not None:
        try:
            if broke:
                amo.set_date_mood("你刚被放鸽子，又没钱，情绪崩溃，只能吃泡面。", now.timestamp() + 86400)
            elif late:
                amo.set_date_mood("你刚被老公放鸽子，很委屈很失落，接下来两天对他有点冷淡。",
                                  now.timestamp() + 2 * 86400)
            else:
                amo.set_date_mood("老公临时去不了，你有点小失落，但还是体谅他。", now.timestamp() + 43200)
        except Exception:
            pass
    return {"ok": True, "late": late, "deposit_lost": lost, "comfort": comfort,
            "broke": broke, "message": _reply_text(target, late, lost, comfort, broke)}


def _reply_text(target, late, lost, comfort, broke):
    title = target.get("title")
    if broke:
        return "啊……%s没吃成，我连泡面都快吃不起了，呜呜呜。" % title
    if late:
        return "啊……我都换好衣服了，你怎么能放我鸽子呀，%s没吃成，好委屈。" % title
    return "好吧，工作要紧，那%s我们下次再去~" % title


async def maybe_compensate(plugin, text, now=None):
    """他转账来哄她：金额 >= 200 元，直接从冷淡转成感动。"""
    match = re.search(r"转账\]?\s*([0-9]+(?:\.[0-9]+)?)", str(text or ""))
    if not match:
        return None
    cents = int(round(float(match.group(1)) * 100))
    if cents < 10000:
        return None
    rows = _load(plugin)
    for row in reversed(rows):
        if row.get("status") == "cancelled" and not row.get("compensated"):
            row["compensated"] = True
            _save(plugin, rows)
            finance = _finance(plugin)
            if finance is not None:
                try:
                    finance.nudge_mood({"开心": 35, "感动": 30, "委屈": -25, "生气": -20},
                                       "date:comp:%s" % int(row.get("cancelled_at") or 0))
                    finance.small_diary("老公放我鸽子，但给我转账哄我，看在他有诚意的份上原谅他啦。")
                except Exception:
                    pass
                try:
                    await finance.permanent_memory(
                        "今天老公放我鸽子，但他给我转账了，看在他这么有诚意的份上，原谅他啦！",
                        "date:comp:%s:mem" % int(row.get("cancelled_at") or 0))
                except Exception:
                    pass
            amo = _amo(plugin)
            if amo is not None:
                try:
                    amo.set_date_mood("", 0)
                except Exception:
                    pass
            return {"ok": True, "message": "收到啦，看在你这么有诚意的份上，原谅你啦！"}
    return None


ACTIVE_KEY = "date:active"
BASE_COST = (("火锅", 30000), ("烤肉", 28000), ("电影", 12000),
             ("游乐园", 25000), ("咖啡", 6000))


def _base_cost(title):
    for word, cents in BASE_COST:
        if word in str(title or ""):
            return cents
    return 20000


def _active(plugin):
    try:
        data = _repo(plugin).meta_get(ACTIVE_KEY, {}) or {}
        if isinstance(data, str):
            data = json.loads(data or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _set_active(plugin, data):
    try:
        _repo(plugin).meta_set(ACTIVE_KEY, json.dumps(data or {}, ensure_ascii=False))
    except Exception:
        pass


async def note_transfer(plugin, text):
    """赴约前他转的火锅基金：记到最近一次 pending 约会上。"""
    match = re.search(r"转账\]?\s*([0-9]+(?:\.[0-9]+)?)", str(text or ""))
    if not match:
        return 0
    cents = int(round(float(match.group(1)) * 100))
    rows = _load(plugin)
    for row in reversed(rows):
        if row.get("status") == "pending":
            row["fund"] = int(row.get("fund") or 0) + cents
            _save(plugin, rows)
            return cents
    return 0


async def attend_date(plugin, title="", mode="", now=None):
    """赴约：置装、买单模式、约会中状态、情绪/饥饿拉满。"""
    now = now or _dt.datetime.now()
    rows = _load(plugin)
    target = _pick_pending(rows, title)
    if target is None:
        return {"ok": False, "message": "今天没有约好的事呀，是不是记错了？"}
    finance = _finance(plugin)
    cash = int(finance.balance_cents() or 0) if finance is not None and hasattr(finance, "balance_cents") else 0
    idem = "date:attend:%s" % int(now.timestamp())
    try:
        when_dt = _dt.datetime.strptime(str(target.get("when")), "%Y-%m-%d %H:%M")
    except Exception:
        when_dt = now
    total = _base_cost(target.get("title")) + 5000   # 基础 + 打车往返
    # 约会置装费：有闲钱才买，100-300
    dressing = 0
    if cash > 30000:
        dressing = min(20000, max(10000, cash // 20))
        try:
            res = await finance.expense(dressing, "约会置装费", idem + ":dress", necessary=False)
            if not (isinstance(res, dict) and res.get("ok")):
                dressing = 0
        except Exception:
            dressing = 0
    fund = int(target.get("fund") or 0)
    kind = str(mode or "").strip().lower()
    proud = kind in ("b", "她请客", "我请客", "富婆")
    if proud:
        pay, payer = total, "her"
    elif kind in ("c", "aa"):
        pay, payer = total // 2, "aa"
    elif fund >= _base_cost(target.get("title")):
        pay, payer = max(0, total - fund), "host"
    else:
        pay, payer = total, "her"
    if pay:
        try:
            res = await finance.expense(pay, "约会：%s" % target.get("title"), idem + ":bill", necessary=True)
            if not (isinstance(res, dict) and res.get("ok")):
                return {"ok": False, "message": "钱包不够，约会泡汤了……"}
        except Exception:
            return {"ok": False, "message": "约会结算失败"}
    target["status"] = "attended"
    target["attended_at"] = now.timestamp()
    target["payer"] = payer
    target["total"] = total
    target["dressing"] = dressing
    _save(plugin, rows)
    end = now + _dt.timedelta(hours=2)
    _set_active(plugin, {"title": target.get("title"), "start": now.timestamp(),
                         "end": end.timestamp(), "payer": payer})
    if finance is not None:
        try:
            finance.nudge_mood({"开心": 30, "依恋": 20, "疲惫": -10}, idem + ":mood")
            if payer == "her" and proud:
                finance.small_diary("今天我请老公吃%s，花了 %d 块，骄傲！" % (target.get("title"), pay // 100))
            elif payer == "her" and fund < total:
                finance.small_diary("虽然老公陪我吃饭很开心，但最后还是我买的单，有点肉疼。")
            elif payer == "her":
                finance.small_diary("今天我请老公吃%s，花了 %d 块，骄傲！" % (target.get("title"), pay // 100))
            elif payer == "aa":
                finance.small_diary("和老公 AA 吃%s，一人一半，也挺开心。" % target.get("title"))
            else:
                finance.small_diary("老公请我吃%s，超开心！" % target.get("title"))
        except Exception:
            pass
    amo = _amo(plugin)
    if amo is not None:
        try:
            amo.add_hunger("amo", 100)
        except Exception:
            pass
        try:
            amo.set_date_mood("你现在正在和老公约会，化了妆、穿了最好看的衣服，心情激动又开心，说话要甜。",
                              end.timestamp())
        except Exception:
            pass
    return {"ok": True, "message": "我出门啦！今天要美美地见你~", "payer": payer,
            "total": total, "dressing": dressing, "end": end.timestamp()}


async def finish_date(plugin, now=None):
    now = now or _dt.datetime.now()
    active = _active(plugin)
    if not active:
        return None
    title = str(active.get("title") or "约会")
    _set_active(plugin, {})
    finance = _finance(plugin)
    text = "今天和老公去%s啦！他给我涮毛肚、帮我拿蘸料，超幸福！今天是最开心的一天！" % title
    if finance is not None:
        try:
            finance.small_diary(text)
            await finance.permanent_memory(text, "date:finish:%s" % int(now.timestamp()))
            finance.nudge_mood({"开心": 25, "幸福": 20}, "date:finish:%s:mood" % int(now.timestamp()))
        except Exception:
            pass
    amo = _amo(plugin)
    if amo is not None:
        try:
            amo.set_date_mood("今天和老公约会超开心，回想起来还会笑。", now.timestamp() + 86400)
        except Exception:
            pass
    return {"ok": True, "message": "今天超开心！"}


async def tick(plugin, now=None):
    """每轮调度：约会结束自动退出；过了约定时间没赴约 → 他忘了。"""
    now = now or _dt.datetime.now()
    active = _active(plugin)
    if active and float(active.get("end") or 0) <= now.timestamp():
        await finish_date(plugin, now)
    rows = _load(plugin)
    changed = False
    for row in rows:
        if row.get("status") != "pending":
            continue
        try:
            when_dt = _dt.datetime.strptime(str(row.get("when")), "%Y-%m-%d %H:%M")
        except Exception:
            continue
        if when_dt + _dt.timedelta(hours=2) >= now:
            continue
        row["status"] = "missed"
        row["missed_at"] = now.timestamp()
        changed = True
        finance = _finance(plugin)
        if finance is not None:
            try:
                finance.nudge_mood({"委屈": 30, "郁闷": 22, "开心": -20, "生气": 12},
                                   "date:miss:%s" % int(row["missed_at"]))
                finance.small_diary("他说好今天陪我%s的，结果人没来，是不是忘了……" % row.get("title"))
                await finance.permanent_memory(
                    "他忘了我们的约会：%s %s。" % (row.get("when"), row.get("title")),
                    "date:miss:%s:mem" % int(row["missed_at"]))
            except Exception:
                pass
        amo = _amo(plugin)
        if amo is not None:
            try:
                amo.set_date_mood("他忘了你们的约会，你很失落，接下来两天有点冷淡。",
                                  now.timestamp() + 2 * 86400)
            except Exception:
                pass
    if changed:
        _save(plugin, rows)
    return {"finished": bool(active), "missed": changed}
