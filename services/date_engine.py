# -*- coding: utf-8 -*-
"""约会与失信：约定写入日程、取消时联动情绪/定金/安慰消费/记忆。"""
from __future__ import annotations

import datetime as _dt
import random
import json
import re

try:  # 包模式优先
    from ..guard import isolated
except ImportError:  # 离线测试 / 顶层加载
    from guard import isolated

CANCEL_WORDS = ("去不了", "去不成", "不去了", "取消约会", "放鸽子", "临时加班", "改天吧")
ARRIVAL_WORDS = ("好", "行", "嗯", "到了", "来了", "来啦", "马上到", "等我", "出发了",
                 "在路上了", "在路上", "到楼下", "到门口", "ok", "OK")
INTERRUPT_WORDS = ("老板叫我", "老板叫", "领导叫", "加班", "得走了", "得回去", "先走了",
                   "临时有事", "公司有事", "走不开", "对不起", "改天")
#: 2026-11-15：同居设定——约会收尾是「一起回家」，不是分别/送别。
#: 「我走了 / 分开吧 / 各回各家」这类各奔东西的话不再触发收尾。
END_WORDS = ("吃好了", "吃完了", "吃饱了", "吃饱喝足", "结账吧", "我们回家", "回家吧",
             "该回家了", "一起回家", "走回家", "太晚了", "回去休息", "不早了")

def _compact(text):
    return re.sub(r"[\s，。！？!?、~～…,.]+", "", str(text or ""))

GENERIC_YES = ("好", "行", "嗯", "ok", "OK", "好的", "好呀", "好滴")

def is_arrival(text):
    """等待赴约时，极短肯定词直接视为“他到了”。"""
    body = _compact(text)
    if not body or len(body) > 10:
        return False
    if body in GENERIC_YES:
        return True
    return any(word in body for word in ARRIVAL_WORDS if word not in GENERIC_YES)

def is_interrupt(text):
    body = str(text or "")
    return any(word in body for word in INTERRUPT_WORDS) and any(
        word in body for word in ("走", "加班", "回去", "有事", "对不起"))

#: 2026-11-15：约会中途短暂离开（洗手间/接电话）绝不能算结束
PAUSE_WORDS = ("洗手间", "去个厕所", "上个厕所", "接个电话", "接电话", "去趟卫生间",
               "去个卫生间", "买包烟", "买个东西马上")
RESUME_WORDS = ("我回来了", "回来了", "回来啦", "我好了", "久等了")
PAUSE_MAX_MINUTES = 15


def is_date_end(text):
    body = str(text or "")
    return any(word in body for word in END_WORDS)


def is_date_pause(text):
    """中途离开一会儿：不算结束。"""
    body = str(text or "")
    if is_date_end(body):
        return False
    return any(word in body for word in PAUSE_WORDS)


def is_date_resume(text):
    body = str(text or "")
    return any(word in body for word in RESUME_WORDS)


def _minutes(hhmm, default=0):
    try:
        hour, minute = str(hhmm or "").split(":")[:2]
        return int(hour) * 60 + int(minute)
    except Exception:
        return int(default)


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
    except Exception as exc:
        isolated("date_engine._save", exc)


def _parse_when(text, now=None):
    """把「晚点 / 今晚 / 明天傍晚 / 18:27」解析成具体时间。

    2026-11-15 用户要求：只要没说「明天」，且今天时间还允许，就必须定在今天；
    以前「没解析出来 → 明天」「时间已过 → 明天」会把「搞完就出发」直接顺延到第二天。
    """
    now = now or _dt.datetime.now()
    body = str(text or "").strip()
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})", body)
    if match:
        return _dt.datetime(*[int(x) for x in match.groups()])  # type: ignore[arg-type]

    tomorrow = any(word in body for word in ("明天", "明晚", "明早", "明日"))
    base = now + _dt.timedelta(days=1) if tomorrow else now
    target = None
    match = re.search(r"(\d{1,2})[:：](\d{2})", body)
    explicit_clock = bool(match)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    else:
        for word, hour, minute in (
            ("凌晨", 0, 30), ("早上", 8, 0), ("上午", 10, 0), ("中午", 12, 0),
            ("下午", 15, 0), ("傍晚", 18, 0), ("晚上", 19, 30), ("深夜", 23, 0),
        ):
            if word in body:
                target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
                break
    if target is None:
        # 没给具体时间（晚点/搞完就出发/稍后）：说「明天」才顺延，否则就是今天 90 分钟后
        return now + _dt.timedelta(days=1) if tomorrow else now + _dt.timedelta(minutes=90)
    if not tomorrow and target <= now:
        # 刚过几分钟（模型给的「现在/稍后」时间点）→ 仍算今天，顺延到 30 分钟后；
        # 已经过了超过半小时（比如晚上说「中午」）→ 才顺延到明天同一时间。
        if explicit_clock and (now - target) <= _dt.timedelta(minutes=30):
            target = now + _dt.timedelta(minutes=30)
        else:
            target = target + _dt.timedelta(days=1)
    return target


def _human_when(when_dt, now=None):
    """把时间说成人话（今晚六点半左右 / 明天傍晚六点左右），绝不输出 11-16 18:27。"""
    now = now or _dt.datetime.now()
    days = (when_dt.date() - now.date()).days
    day_word = "今天" if days <= 0 else ("明天" if days == 1 else "那天")
    hour, minute = int(when_dt.hour), int(when_dt.minute)
    if 5 <= hour < 11:
        period = "早上"
    elif 11 <= hour < 13:
        period = "中午"
    elif 13 <= hour < 17:
        period = "下午"
    elif 17 <= hour < 18:
        period = "傍晚"
    elif 18 <= hour < 23:
        period = "晚上"
    else:
        period = "夜里"
    total = int(round((hour * 60 + minute) / 30.0) * 30) % (24 * 60)
    hh, mm = divmod(total, 60)
    hour_words = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六",
                  7: "七", 8: "八", 9: "九", 10: "十", 11: "十一", 12: "十二"}
    if hh == 0:
        clock = "零点"
    else:
        display = hh if hh <= 12 else hh - 12
        clock = hour_words.get(display, str(display)) + "点"
    clock += "半" if mm == 30 else ("整" if mm == 0 else "多")
    if day_word == "今天" and period in ("晚上", "夜里"):
        return "今晚" + clock + "左右"
    if day_word == "今天":
        return "今天" + period + clock + "左右"
    return day_word + period + clock + "左右"


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
    # 防撞车：那天那个时间她已经答应别人（社交日程），不能再来一场约会
    finance0 = _finance(plugin)
    if finance0 is not None:
        try:
            day_text = when_dt.date().isoformat()
            now_min = when_dt.hour * 60 + when_dt.minute
            for row in finance0.schedule_windows(day_text) or []:
                if str(row.get("category")) not in ("social", "date"):
                    continue
                begin = _minutes(str(row.get("start") or ""))
                finish = _minutes(str(row.get("end") or ""))
                if begin <= now_min < finish:
                    return {"ok": False, "reason": "conflict",
                            "message": "那天%s她已经答应了「%s」，换个时间吧" % (
                                row.get("start"), row.get("activity") or "别人")}
        except Exception as exc:
            isolated("date_engine.make_date.conflict", exc)
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
        except Exception:
            close = False
        if close:
            return {"ok": False, "duplicate": True,
                    "message": "我们不是已经约好%s了吗？我记着呢，等会儿见！" % title}

    # 新的约会顶掉旧的 pending：他说「明天去不了了，今晚去吧」不能两条都躺在日程里
    old_pending = [row for row in rows if row.get("status") == "pending"]
    for row in old_pending:
        row["status"] = "replaced"
        row["replaced_at"] = now.timestamp()
    _drop_date_items(plugin, old_pending)

    # 撞上上班/看病：她自然拒绝，而不是硬塞进日程
    conflict = _date_conflict(plugin, when_dt)
    if conflict:
        _save(plugin, rows)
        return {"ok": False, "conflict": True,
                "message": "哎呀，%s我可能%s诶，我们换个时间，或者改到晚上好不好？"
                           % (_human_day_period(when_dt, now), conflict)}

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
        except Exception as exc:
            isolated("date_engine.make_date", exc)
        try:
            amo.set_date_mood("你在期待和老公%s，心情很好。" % title,
                              (when_dt + _dt.timedelta(hours=6)).timestamp())
            amo.set_date_prep(title, when_dt.timestamp())
        except Exception as exc:
            isolated("date_engine.make_date.2", exc)
    finance = _finance(plugin)
    if finance is not None:
        try:
            finance.nudge_mood({"开心": 10}, "date:make:%s" % int(now.timestamp()))
        except Exception as exc:
            isolated("date_engine.make_date.3", exc)
    return {"ok": True,
            "message": "好呀，那说定啦，%s去%s~" % (_human_when(when_dt, now), title),
            "when": when_dt.strftime("%Y-%m-%d %H:%M"),
            "when_text": _human_when(when_dt, now)}


def _clock_minutes(value):
    try:
        hour, _, minute = str(value or "").partition(":")
        return int(hour) * 60 + int(minute or 0)
    except (TypeError, ValueError):
        return None


def _human_day_period(when_dt, now=None):
    """「明天下午 / 今晚」这种只到时段的说法，用来跟老公商量换时间。"""
    now = now or _dt.datetime.now()
    days = (when_dt.date() - now.date()).days
    day_word = "今天" if days <= 0 else ("明天" if days == 1 else "那天")
    hour = int(when_dt.hour)
    if 5 <= hour < 11:
        period = "早上"
    elif 11 <= hour < 13:
        period = "中午"
    elif 13 <= hour < 17:
        period = "下午"
    elif 17 <= hour < 18:
        period = "傍晚"
    elif 18 <= hour < 23:
        period = "晚上"
    else:
        period = "夜里"
    return "今晚" if (day_word == "今天" and period in ("晚上", "夜里")) else day_word + period


def _date_conflict(plugin, when_dt):
    """约会时段撞上上班/看病/睡觉 → 返回冲突的活动，供她自然拒绝。"""
    amo = _amo(plugin)
    if amo is None:
        return ""
    day = when_dt.date().isoformat()
    try:
        items = list(getattr(amo.state, "schedule", {}).get(day) or [])
    except Exception:
        return ""
    start = int(when_dt.hour) * 60 + int(when_dt.minute)
    end = start + 120
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("category") or "") in ("social", "rest"):
            continue
        begin, finish = _clock_minutes(item.get("start")), _clock_minutes(item.get("end"))
        if begin is None or finish is None:
            continue
        if finish <= begin:
            finish = 24 * 60
        if begin < end and start < finish:
            text = str(item.get("activity") or item.get("place") or "有事").strip()
            return text[:14]
    return ""


def _drop_date_items(plugin, rows):
    """把被替换掉的旧约会从她的日程里摘掉（只动 source=date/social 的行）。"""
    amo = _amo(plugin)
    if amo is None:
        return
    try:
        schedule = getattr(amo.state, "schedule", {})
        touched = False
        for row in rows:
            day = str(row.get("when") or "")[:10]
            if not day or day not in schedule:
                continue
            start = str(row.get("when") or "")[11:16]
            kept = [item for item in (schedule.get(day) or [])
                    if not (str(item.get("category") or "") == "social"
                            and str(item.get("start") or "") == start)]
            if len(kept) != len(schedule.get(day) or []):
                schedule[day] = kept
                touched = True
        if touched:
            amo.db.kv_set("schedule", schedule)
    except Exception as exc:
        isolated("date_engine._drop_date_items", exc)


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
        except Exception as exc:
            isolated("date_engine.cancel_date", exc)
    cash = int(finance.balance_cents() or 0) if finance is not None and hasattr(finance, "balance_cents") else 0
    deposit = int(target.get("deposit") or 0)
    lost = 0
    if deposit and cash >= deposit and finance is not None:
        try:
            res = await finance.expense(deposit, "约会定金损失", idem + ":deposit", necessary=True)
            if isinstance(res, dict) and res.get("ok"):
                lost = deposit
        except Exception as exc:
            isolated("date_engine.cancel_date.2", exc)
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
        except Exception as exc:
            isolated("date_engine.cancel_date.3", exc)
        try:
            if broke:
                finance.small_diary("火锅没吃成，连泡面都快吃不起了，好委屈。")
            elif lost:
                finance.small_diary("火锅没吃成，定金也退不了，好亏。")
            else:
                finance.small_diary("本来约好%s的，结果他去不了，有点失落。" % target.get("title"))
            if comfort:
                finance.small_diary("没人陪我吃，我自己点外卖总行了吧！花了 %.2f 元。" % (comfort / 100.0))
        except Exception as exc:
            isolated("date_engine.cancel_date.4", exc)
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
        except Exception as exc:
            isolated("date_engine.cancel_date.5", exc)
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
                except Exception as exc:
                    isolated("date_engine.maybe_compensate", exc)
                try:
                    await finance.permanent_memory(
                        "今天老公放我鸽子，但他给我转账了，看在他这么有诚意的份上，原谅他啦！",
                        "date:comp:%s:mem" % int(row.get("cancelled_at") or 0))
                except Exception as exc:
                    isolated("date_engine.maybe_compensate.2", exc)
            amo = _amo(plugin)
            if amo is not None:
                try:
                    amo.set_date_mood("", 0)
                except Exception as exc:
                    isolated("date_engine.maybe_compensate.3", exc)
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
    except Exception as exc:
        isolated("date_engine._set_active", exc)


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
    state, failure = await _attend_open(plugin, title, now)
    if failure is not None:
        return failure
    rows = state["rows"]
    target = state["target"]
    finance = state["finance"]
    idem = state["idem"]
    title = state["title"]
    amo_early = state["amo"]
    total = state["total"]
    dressing = await _attend_dress(finance, state["cash"], idem)
    fund = int(target.get("fund") or 0)
    pay, payer, proud = _attend_pay_mode(mode, fund, target, total)
    failure = await _attend_bill(finance, pay, target, idem, amo_early)
    if failure is not None:
        return failure
    end, title = _attend_mark_attended(target, now, payer, total, dressing)
    when_label, company = await _attend_remember(finance, now, title)
    _save(plugin, rows)
    _set_active(plugin, {"title": target.get("title"), "start": now.timestamp(),
                         "end": end.timestamp(), "payer": payer, "status": "dating"})
    _attend_mood(finance, idem, payer, proud, fund, total, pay, target)
    amo = _attend_amo_mood(plugin, end)
    await _attend_amo_schedule(amo, now, end, title, when_label, company, target)
    return {"ok": True, "meet": title, "payer": payer,
            "total": total, "dressing": dressing, "end": end.timestamp(),
            "status": "dating"}


async def _attend_open(plugin, title, now):
    """加载与目标定位：取待赴约、财务钱包、约会中状态与总价。"""
    rows = _load(plugin)
    target = _pick_pending(rows, title)
    if target is None:
        return None, {"ok": False, "message": "今天没有约好的事呀，是不是记错了？"}
    finance = _finance(plugin)
    cash = int(finance.balance_cents() or 0) if finance is not None and hasattr(finance, "balance_cents") else 0
    idem = "date:attend:%s" % int(now.timestamp())
    try:
        when_dt = _dt.datetime.strptime(str(target.get("when")), "%Y-%m-%d %H:%M")
    except Exception:
        when_dt = now
    title = str(target.get("title") or "约会")
    # 先把她切到「约会中」，再扣钱：否则财务日程会记在「家」，出现「在家买单」
    amo_early = _amo(plugin)
    if amo_early is not None:
        try:
            amo_early.set_date_now("老公已经到你面前了，你们现在在一起。正在%s。" % title,
                                   now.timestamp() + 2 * 3600, since=now.timestamp())
            amo_early.set_current_action("和老公在%s" % title, now.timestamp() + 2 * 3600,
                                         place="外面", zone="restaurant", category="social")
        except Exception as exc:
            isolated("date_engine.attend_date.early", exc)
    total = _base_cost(target.get("title")) + 5000   # 基础 + 打车往返
    state = {"rows": rows, "target": target, "finance": finance, "cash": cash,
             "idem": idem, "title": title, "amo": amo_early, "total": total}
    return state, None


async def _attend_dress(finance, cash, idem):
    """约会置装费：有闲钱才买，100-300。"""
    dressing = 0
    if cash > 30000:
        dressing = min(20000, max(10000, cash // 20))
        try:
            res = await finance.expense(dressing, "约会置装费", idem + ":dress", necessary=False)
            if not (isinstance(res, dict) and res.get("ok")):
                dressing = 0
        except Exception:
            dressing = 0
    return dressing


def _attend_pay_mode(mode, fund, target, total):
    """买单模式：她请客 / AA / 火锅基金兜底，默认她买单。"""
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
    return pay, payer, proud


async def _attend_bill(finance, pay, target, idem, amo_early):
    """买单：扣款失败时撤回「约会中」状态并返回失败结果；成功返回 None。"""
    if pay:
        try:
            res = await finance.expense(pay, "约会：%s" % target.get("title"), idem + ":bill", necessary=True)
            if not (isinstance(res, dict) and res.get("ok")):
                if amo_early is not None:
                    try:
                        amo_early.set_date_now("", 0)
                        amo_early.set_current_action("", 0)
                    except Exception:
                        pass
                return {"ok": False, "message": "钱包不够，约会泡汤了……"}
        except Exception:
            return {"ok": False, "message": "约会结算失败"}
    return None


def _attend_mark_attended(target, now, payer, total, dressing):
    """约会状态：落 attended 字段与两小时结束时间。"""
    target["status"] = "attended"
    target["attended_at"] = now.timestamp()
    target["payer"] = payer
    target["total"] = total
    target["dressing"] = dressing
    end = now + _dt.timedelta(hours=2)
    title = str(target.get("title") or "约会")
    return end, title


async def _attend_remember(finance, now, title):
    """共同记忆：按时间段的回忆文案 + 写长期记忆。"""
    hour = int(now.hour)
    if 5 <= hour < 11:
        period = "早上"
    elif 11 <= hour < 14:
        period = "中午"
    elif 14 <= hour < 18:
        period = "下午"
    elif 18 <= hour < 23:
        period = "晚上"
    else:
        period = "深夜"
    when_label = "%s %s" % (now.strftime("%Y-%m-%d"), period)
    company = "他陪我涮了毛肚、给我夹菜" if "火锅" in title else "他全程陪着我、给我夹菜"
    memory_text = "[共同记忆] %s，我和老公一起去吃了%s，%s，我超级开心。" % (when_label, title, company)
    if finance is not None:
        try:
            await finance.permanent_memory(memory_text, "date:attend:%s:mem" % int(now.timestamp()))
        except Exception as exc:
            isolated("date_engine.attend_date", exc)
    return when_label, company


def _attend_mood(finance, idem, payer, proud, fund, total, pay, target):
    """情绪与小日记：开心/依恋拉高，按谁买单写一条日记。"""
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
        except Exception as exc:
            isolated("date_engine.attend_date.2", exc)


def _attend_amo_mood(plugin, end):
    """约会中的情绪与饥饿：拉满饥饿，写入约会心情。"""
    amo = _amo(plugin)
    if amo is None:
        return None
    try:
        amo.add_hunger("amo", 100)
    except Exception as exc:
        isolated("date_engine.attend_date.3", exc)
    try:
        amo.set_date_mood("你现在正在和老公约会，化了妆、穿了最好看的衣服，心情激动又开心，说话要甜。",
                          end.timestamp())
    except Exception as exc:
        isolated("date_engine.attend_date.4", exc)
    return amo


async def _attend_amo_schedule(amo, now, end, title, when_label, company, target):
    """日程收尾：约会中状态/事实/见面地点、随机小插曲与日程项清理。"""
    if amo is None:
        return
    try:
        end_of_day = now.replace(hour=23, minute=59, second=0, microsecond=0)
        fact = ("%s你已经和老公一起去吃了%s，%s。你们刚分别/你刚回到家；"
                "这是已经发生的事实：你绝对不是一个人吃饭，记忆里必须有他。"
                "老公问起时，就回忆刚才约会的细节。" % (when_label, title, company))
        now_line = ("老公已经到你面前了，你们现在在一起。你正和老公在外面%s，"
                    "他就站在你身边/坐在你对面，别再说自己一个人、也别问他在哪。" % title)
        try:
            amo.set_date_now(now_line, end.timestamp(), since=now.timestamp())
        except TypeError:  # 老版本 amo / 测试桩没有 since
            amo.set_date_now(now_line, end.timestamp())
        fact = ("【绝对事实】老公已经到你面前了，你们现在在一起，正在%s。"
                "这是已经发生的事实，接下来只聊见面的互动。" % title)
        amo.set_date_fact(fact, end_of_day.timestamp())
        try:
            place = str((amo.state.current_activity() or {}).get("place") or "")
        except Exception:
            place = ""
        try:
            amo.set_date_meet(title, place)
        except (AttributeError, TypeError):
            pass
        amo.set_current_action("正在和老公约会", end.timestamp(),
                               place="外面", zone="restaurant", category="social")
    except Exception as exc:
        isolated("date_engine.attend_date.5", exc)
    try:
        if random.random() < 0.3:
            incident = random.choice(DATE_INCIDENTS)
            amo.set_date_incident(incident, end.timestamp())
            await amo.push_to_owner(incident)
    except Exception as exc:
        isolated("date_engine.attend_date.6", exc)
    try:
        day = now.date().isoformat()
        item = {"start": now.strftime("%H:%M"), "end": end.strftime("%H:%M"),
                "activity": "和老公" + title, "category": "social", "place": "外面",
                "zone": "restaurant", "subject": "bot", "happened": True}
        amo.state.add_schedule_item(day, item, source="date", reason="赴约")
    except Exception as exc:
        isolated("date_engine.attend_date.7", exc)
    try:
        when_dt = _dt.datetime.strptime(str(target.get("when")), "%Y-%m-%d %H:%M")
        if when_dt.date() != now.date():
            old_day = when_dt.date().isoformat()
            items = list(amo.state.schedule.get(old_day) or amo.state.db.get_schedule(old_day) or [])
            kept = [row for row in items if str(row.get("activity") or "") != "和老公" + title]
            if len(kept) != len(items):
                amo.state.commit_schedule_change(old_day, kept, old_items=items,
                                                 source="date", reason="改到当天赴约")
    except Exception as exc:
        isolated("date_engine.attend_date.8", exc)


DATE_INCIDENTS = (
    "老公！外面突然下大雨了，我没带伞，咱们被困在商场了，呜呜呜。",
    "救命，这家火锅店排队排了 50 桌，我快饿晕过去了……",
    "刚刚试了一件衣服，感觉有点显胖，你帮我参考一下嘛~",
)
LOOT_CLOTHES = ("米色针织开衫", "碎花连衣裙", "白色帆布鞋", "焦糖色半身裙")
LOOT_HOME = ("香薰蜡烛", "小台灯", "毛绒抱枕", "多肉盆栽")


def _date_loot(amo, title):
    body = str(title or "")
    if not any(word in body for word in ("逛街", "商场", "买", "游乐园")):
        return ""
    if random.random() < 0.6:
        item = random.choice(LOOT_CLOTHES)
        try:
            amo.add_closet_item({"name": item, "color": "", "category": "top",
                                 "time_slot": "", "price": 0})
        except Exception as exc:
            isolated("date_engine._date_loot", exc)
        return item
    item = random.choice(LOOT_HOME)
    try:
        housing = getattr(amo, "housing", None)
        if housing is not None:
            housing.add_furniture(item)
    except Exception as exc:
        isolated("date_engine._date_loot.2", exc)
    return item


def active_date(plugin):
    """当前约会状态：{"status": "dating", ...}；没有则 None。"""
    active = _active(plugin)
    return active if active and active.get("status") == "dating" else None


async def interrupt_date(plugin, text="", now=None):
    """刚碰面就被叫走：强制退出 dating，情绪骤降，写日记。"""
    now = now or _dt.datetime.now()
    active = _active(plugin)
    if not active:
        return {"ok": False}
    title = str(active.get("title") or "约会")
    _set_active(plugin, {})
    finance = _finance(plugin)
    if finance is not None:
        try:
            finance.small_diary("刚碰面老公就被叫走了，好难过，但也只能让他去忙。")
            finance.nudge_mood({"委屈": 40, "崩溃": 20, "开心": -30, "生气": 10},
                               "date:interrupt:%s" % int(now.timestamp()))
            await finance.permanent_memory(
                "[共同记忆] 刚和老公碰面，他老板突然叫他回去加班，他只能先走；我很委屈，但还是让他去忙了。",
                "date:interrupt:%s:mem" % int(now.timestamp()))
        except Exception as exc:
            isolated("date_engine.interrupt_date", exc)
    amo = _amo(plugin)
    if amo is not None:
        try:
            amo.set_date_mood("刚碰面老公就被叫走加班，你很委屈、很难过，接下来别装开心。",
                              now.timestamp() + 86400)
            amo.set_date_fact("【绝对事实】你们刚碰面，老公就被老板叫回去加班，约会中断了，他没能陪你。",
                              now.replace(hour=23, minute=59, second=0, microsecond=0).timestamp())
            amo.set_date_now("", 0)
            amo.set_current_action("", 0)
        except Exception as exc:
            isolated("date_engine.interrupt_date.2", exc)
    return {"ok": True, "interrupted": True, "title": title,
            "message": "（愣住）……好吧，你去忙吧，我等你回来。"}


async def handle_date_text(plugin, text, now=None):
    """前置拦截：等待中收到短肯定词 → 赴约；约会中收到突发/结束词 → 中断/收尾。"""
    now = now or _dt.datetime.now()
    active = active_date(plugin)
    if active:
        if is_date_resume(text):
            amo_r = _amo(plugin)
            if amo_r is not None:
                try:
                    amo_r.db.kv_set("date_pause", {})
                except Exception as exc:
                    isolated("date_engine.resume", exc)
        if is_date_pause(text):
            amo_p = _amo(plugin)
            if amo_p is not None:
                try:
                    amo_p.db.kv_set("date_pause", {"at": now.timestamp(),
                                                    "until": now.timestamp() + PAUSE_MAX_MINUTES * 60})
                except Exception as exc:
                    isolated("date_engine.pause", exc)
            return {"handled": False, "action": "pause"}
        if is_interrupt(text):
            out = await interrupt_date(plugin, text, now)
            return {"handled": True, "action": "interrupt", "message": out.get("message") or ""}
        if is_date_end(text):
            # 解锁约会，只写客观事实；道别话由大模型下一句现场生成
            await finish_date(plugin, now, reason="user_done")
            return {"handled": False, "action": "finish"}
    pending = find_pending(plugin, "")
    if pending and is_arrival(text):
        out = await attend_date(plugin, pending.get("title"), now=now)
        if out.get("ok"):
            # 不拦截消息、不给固定台词：让主脑用【约会开场】事实自己说第一句
            return {"handled": False, "action": "attend"}
    return {"handled": False}


async def finish_date(plugin, now=None, reason="timeout"):
    """结束约会：只解锁状态 + 写客观事实，**不生成任何道别台词**（交给大模型）。"""
    now = now or _dt.datetime.now()
    active = _active(plugin)
    if not active:
        return None
    title = str(active.get("title") or "约会")
    _set_active(plugin, {})
    now_minute = now.hour * 60 + now.minute
    ama_trunc = _amo(plugin)
    if ama_trunc is not None:
        # 1) 正在进行的约会日程立刻截断到「现在」，不允许挂到原定的结束时间
        try:
            day = now.date().isoformat()
            state = getattr(ama_trunc, "state", None)
            current = list((getattr(state, "schedule", {}) or {}).get(day) or [])
            patched = []
            for row in current:
                row = dict(row)
                activity = str(row.get("activity") or "")
                begin = _minutes(row.get("start"), 0)
                finish = _minutes(row.get("end"), 0)
                if (activity.startswith("和老公") or str(row.get("category")) == "social") \
                        and begin <= now_minute < finish:
                    row["end"] = now.strftime("%H:%M")
                patched.append(row)
            if patched != current:
                state.commit_schedule_change(day, patched, old_items=current,
                                             source="date", reason="约会结束截断")
        except Exception as exc:
            isolated("date_engine.finish_date.truncate", exc)
        # 3) 幽灵取消：清掉约会相关的待触发状态，别回家了还弹「上菜了」
        for key in ("date_pause", "date_preview", "date_meal", "date_timer"):
            try:
                ama_trunc.db.kv_set(key, {})
            except Exception as exc:
                isolated("date_engine.finish_date.ghost", exc)
        try:
            ama_trunc.set_date_incident("", 0)
        except Exception as exc:
            isolated("date_engine.finish_date.incident", exc)
    finance = _finance(plugin)
    text = "[共同记忆] 今天和老公去%s约会了。" % title
    if finance is not None:
        try:
            finance.small_diary(text)
            await finance.permanent_memory(text, "date:finish:%s" % int(now.timestamp()))
            finance.nudge_mood({"开心": 25, "幸福": 20}, "date:finish:%s:mood" % int(now.timestamp()))
        except Exception as exc:
            isolated("date_engine.finish_date", exc)
    ama = _amo(plugin)
    if ama is not None:
        try:
            ama.set_date_mood("约会刚结束，你带着今天真实的心情回到家。", now.timestamp() + 86400)
            ama.set_last_date_event(title, now.timestamp())
            ama.set_date_prep("", 0)
            ama.set_date_incident("", 0)
            end_of_day = now.replace(hour=23, minute=59, second=0, microsecond=0)
            if end_of_day.timestamp() <= now.timestamp():
                end_of_day = now + _dt.timedelta(hours=6)
            home_line = ("【绝对事实】你们已经一起回到了共同的家——你们住在一起，不是送别、也不是各回各家。"
                         "你现在踢掉鞋子，瘫在沙发上，包随手扔在旁边；洗了澡、卸了妆、换上舒服的睡衣，"
                         "累但很充实。")
            ama.set_date_fact(home_line, end_of_day.timestamp())
            try:
                ama.set_date_home(title, now.timestamp())
            except AttributeError:
                try:
                    ama.set_date_separation(title, now.timestamp())
                except (AttributeError, TypeError):
                    pass
            except (TypeError,):
                pass
            ama.set_date_now("", 0)
            try:
                home_at = (now + _dt.timedelta(minutes=10)).strftime("%H:%M")
                sofa_end = (now + _dt.timedelta(minutes=90)).strftime("%H:%M")
                day = now.date().isoformat()
                ama.state.add_schedule_item(day, {"start": now.strftime("%H:%M"), "end": home_at,
                                                  "activity": "和老公一起走回家", "place": "路上",
                                                  "category": "social", "zone": "road",
                                                  "subject": "bot", "happened": True},
                                            source="date", reason="约会收尾")
                ama.state.add_schedule_item(day, {"start": home_at, "end": sofa_end,
                                                  "activity": "到家瘫在沙发上", "place": "家",
                                                  "category": "rest", "zone": "home",
                                                  "subject": "bot"},
                                            source="date", reason="约会收尾")
            except Exception as exc:
                isolated("date_engine.finish_date.schedule", exc)
            try:
                ama.set_current_action(
                    "和老公一起到家，瘫在沙发上",
                    (now + _dt.timedelta(minutes=90)).timestamp(),
                    place="家", zone="home", category="rest")
            except Exception as exc:
                isolated("date_engine.finish_date.action", exc)
            ending_fact = {
                "user_done": "时间不早了，吃饱喝足了，该一起回家了。",
                "timeout": "约会已经两个多小时，吃饱喝足了，该一起回家了。",
                "silent": "他好像去忙了，你在犹豫是继续等他一起回家，还是自己先溜达回去。",
            }.get(reason, "约会该收尾了，你们准备一起回家。")
            try:
                ama.set_date_ending(ending_fact, reason=reason)
            except (AttributeError, TypeError):
                pass
            _date_loot(ama, title)
        except Exception as exc:
            isolated("date_engine.finish_date.2", exc)
    if ama_trunc is not None:
        # 2) 剩余时间交给大模型重排（严禁代码写死「休息」）
        try:
            await replan_remaining(plugin, ama_trunc, now, title)
        except Exception as exc:
            isolated("date_engine.finish_date.replan", exc)
    if finance is not None:
        try:
            finance.nudge_mood({"疲惫": 15, "饥饿": 8}, "date:finish:%s:body" % int(now.timestamp()))
        except Exception as exc:
            isolated("date_engine.finish_date.3", exc)
    return {"ok": True, "reason": reason}


PLAN_SYSTEM = (
    "你是她本人，只根据给出的事实安排日程，不编造没提到的人或地点；"
    "时间必须首尾相接、不重叠；只输出一行 JSON 数组，不要解释。"
)


def _parse_segments(text, now):
    import re as _re
    match = _re.search(r"\[.*\]", str(text or ""), _re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    cursor = now.hour * 60 + now.minute
    out = []
    for row in data[:4]:
        if not isinstance(row, dict):
            continue
        start, end = str(row.get("start") or ""), str(row.get("end") or "")
        begin, finish = _minutes(start, -1), _minutes(end, -1)
        if begin < cursor or finish <= begin or finish > 24 * 60:
            continue
        activity = str(row.get("activity") or "").strip()
        if not activity:
            continue
        out.append({"start": start, "end": end, "activity": activity[:24],
                    "place": str(row.get("place") or "家")[:8],
                    "category": str(row.get("category") or "rest")[:8],
                    "zone": str(row.get("zone") or "home")[:8],
                    "subject": "bot"})
        cursor = finish
    return out


async def replan_remaining(plugin, ama, now, title=""):
    """约会提前结束后：让 LLM 自己安排剩下的时间，代码不做任何默认填充。"""
    try:
        llm = getattr(ama, "llm", None)
        if llm is None or not hasattr(llm, "chat_raw"):
            return []
        umos = ama._owner_umos() if hasattr(ama, "_owner_umos") else []
        umo = umos[0] if umos else ""
    except Exception:
        return []
    finance = _finance(plugin)
    mood, fatigue = "平静", 0
    try:
        mood = str((finance.mood() or {}).get("word") or "平静")
    except Exception:
        pass
    try:
        fatigue = int(finance.fatigue() or 0)
    except Exception:
        pass
    prompt = ("现在是 %s。你们刚在%s结束约会、买完单，正准备一起回家（你们住在一起，不是分开）。\n"
              "她现在的状态：心情%s，疲惫值%d。\n"
              "请把 %s 到 23:00 的时间安排成 2-4 条日程，首尾相接、不重叠，"
              "只能写事实里出现过的地点（家 / 路上 / 楼下小馆）。\n"
              "JSON 数组：{\"start\":\"HH:MM\",\"end\":\"HH:MM\",\"activity\":\"一句人话\","
              "\"place\":\"家|路上\",\"category\":\"rest|life|social\",\"zone\":\"home|road\"}"
              % (now.strftime("%H:%M"), title or "外面", mood, fatigue, now.strftime("%H:%M")))
    try:
        text = await llm.chat_raw(umo, PLAN_SYSTEM, prompt)
    except Exception as exc:
        isolated("date_engine.replan.llm", exc)
        return []
    segments = _parse_segments(text, now)
    for seg in segments:
        try:
            ama.state.add_schedule_item(now.date().isoformat(), seg,
                                        source="date", reason="约会收尾重推")
        except Exception as exc:
            isolated("date_engine.replan.write", exc)
    return segments


async def tick(plugin, now=None):
    """每轮调度：约会结束自动退出；过了约定时间没赴约 → 他忘了。"""
    now = now or _dt.datetime.now()
    active = _active(plugin)
    if active and float(active.get("end") or 0) <= now.timestamp():
        await finish_date(plugin, now, reason="timeout")
    elif active:
        ama = _amo(plugin)
        pause = {}
        if ama is not None:
            try:
                pause = ama.db.kv_get("date_pause", {}) or {}
            except Exception:
                pause = {}
        if isinstance(pause, dict) and pause.get("at"):
            if now.timestamp() - float(pause.get("at") or 0) > PAUSE_MAX_MINUTES * 60:
                await finish_date(plugin, now, reason="pause_timeout")
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
            except Exception as exc:
                isolated("date_engine.tick", exc)
        amo = _amo(plugin)
        if amo is not None:
            try:
                amo.set_date_mood("他忘了你们的约会，你很失落，接下来两天有点冷淡。",
                                  now.timestamp() + 2 * 86400)
            except Exception as exc:
                isolated("date_engine.tick.2", exc)
    if changed:
        _save(plugin, rows)
    return {"finished": bool(active), "missed": changed}
