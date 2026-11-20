# -*- coding: utf-8 -*-
"""约会与失信：约定写入日程、取消时联动情绪/定金/安慰消费/记忆。"""
from __future__ import annotations

import datetime as _dt
import random
import json
import re

try:  # 包模式优先
    from ..guard import isolated
    from ..core.amo_bridge import AmoBridge
except ImportError:  # 离线测试 / 顶层加载
    from guard import isolated
    from core.amo_bridge import AmoBridge

from .date_rules import (AGREE_EXACT, CANCEL_WORDS, CLAIM_WINDOWS, GENERIC_YES,
                         PAUSE_MAX_MINUTES, base_cost, claim_slot, clock_minutes, compact,
                         human_day_period, human_when, is_agreement, is_arrival,
                         is_date_end, is_date_pause, is_date_resume, is_interrupt,
                         is_meal_text, meeting_period, minutes, parse_when, pay_mode,
                         pick_pending, schedule_conflict, truncate_finished_blocks)

# 纯规则在 services/date_rules.py；这里保留旧名，老代码/测试继续可用。
_compact = compact
_parse_when = parse_when
_human_when = human_when
_human_day_period = human_day_period
_minutes = minutes
_clock_minutes = clock_minutes
_base_cost = base_cost
_pick_pending = pick_pending
_AGREE_EXACT = AGREE_EXACT
GENERIC_YES = GENERIC_YES


def _repo(plugin):
    return getattr(plugin, "finance_repo", None)


def _finance(plugin):
    return getattr(plugin, "finance", None)


def _bridge(plugin, finance=None):
    """跨插件调用门面（Phase 14）：业务代码不再直摸 finance._amo()。"""
    return AmoBridge(plugin, finance=finance, scope="date")


def _amo_has(bridge, name):
    """约会会话单一源（Phase 2）：新 amo 是否有会话 API；老 amo/测试桩没有。"""
    return callable(getattr(bridge.resolve(), name, None))


def _clear_ending(bridge):
    """清掉收尾事实：优先会话 API，老 amo 回退 kv。"""
    if _amo_has(bridge, "clear_date_ending"):
        if bridge.call("clear_date_ending").ok:
            return True
    bridge.call("kv_set", "date_ending", {})
    return False


def _pause_session(bridge, now):
    """他中途走开：优先会话 API，老 amo 回退 kv date_pause。"""
    if _amo_has(bridge, "set_date_pause"):
        if bridge.call("set_date_pause", PAUSE_MAX_MINUTES).ok:
            return True
    bridge.call("kv_set", "date_pause", {"at": now.timestamp(),
                                         "until": now.timestamp() + PAUSE_MAX_MINUTES * 60})
    return False


def _resume_session(bridge):
    """他回来了：优先会话 API，老 amo 回退 kv。"""
    if _amo_has(bridge, "clear_date_pause"):
        if bridge.call("clear_date_pause").ok:
            return True
    bridge.call("kv_set", "date_pause", {})
    return False


def _finish_session(bridge, title, reason, now):
    """一次收尾：新 amo 走会话；老 amo 回退 set_date_now("",0)+set_date_home。"""
    if _amo_has(bridge, "finish_date_session"):
        result = bridge.call_compat("finish_date_session", [
            ((title, reason, now.timestamp()), {}),
            ((title, reason), {}),
        ])
        if result.ok:
            return True
    bridge.call("set_date_now", "", 0)
    result = bridge.call("set_date_home", title, now.timestamp())
    if not result.ok and isinstance(result.error, AttributeError):
        bridge.call("set_date_separation", title, now.timestamp())
    return False


def _cancel_session(bridge, reason=""):
    """约会中断：优先关会话；老 amo 回退 set_date_now("", 0)。"""
    if _amo_has(bridge, "cancel_date_session"):
        if bridge.call("cancel_date_session", reason).ok:
            return True
    bridge.call("set_date_now", "", 0)
    return False


def _pause_expired(bridge, now):
    """暂停是否超时：读会话单一源；老 amo 读 kv date_pause。"""
    if _amo_has(bridge, "date_session"):
        result = bridge.call_compat("date_session", [((now,), {}), ((), {})])
        digest = result.value if result.ok else {}
        if not isinstance(digest, dict) or digest.get("phase") != "paused":
            return False
        # 会话还停在 paused，但暂停读数已经过期 → 走散超时
        return not digest.get("paused")
    pause = bridge.call("kv_get", "date_pause", {}).value or {}
    if isinstance(pause, dict) and pause.get("at"):
        try:
            return now.timestamp() - float(pause.get("at") or 0) > PAUSE_MAX_MINUTES * 60
        except (TypeError, ValueError):
            return False
    return False


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


def _meal_clash(plugin, when_dt):
    """同一个饭点已经有一顿饭 → 返回那条活动文字（2026-11-20）。

    现场：19:09 已经和柳如烟约了火锅，20:39 又凭空多出一场「和老公吃饭」。
    schedule_conflict 特意 skip 了 social（怕「约会撞社交」误报），所以这里单独判：
    只认餐饮类活动，逛街/电影/咖啡不算占位。
    """
    slot = claim_slot(when_dt)
    if not slot:
        return ""
    bridge = _bridge(plugin)
    if not bridge.ready:
        return ""
    day = when_dt.date().isoformat()
    low, high = CLAIM_WINDOWS[slot]
    try:
        rows = list(bridge.schedule().get(day) or [])
    except Exception as exc:
        isolated("date_engine.meal_clash", exc)
        return ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("category") or "") not in ("meal", "social", "date"):
            continue
        begin = clock_minutes(row.get("start"))
        finish = clock_minutes(row.get("end"))
        if begin is None or finish is None or finish <= begin:
            continue
        if not (begin < high and low < finish):
            continue
        text = str(row.get("activity") or "")
        if is_meal_text(text, row.get("category")):
            return text
    return ""


async def make_date(plugin, title, when="", deposit_cents=0, now=None):
    """约定一件事：写进她自己的日程 + 进入期待状态。"""
    title = str(title or "").strip()
    if not title:
        return {"ok": False, "message": "要约什么？" }
    now = now or _dt.datetime.now()
    when_dt = _parse_when(when, now)
    # 一个饭点只允许一顿：已经约了饭局就不能再排一场（餐饮类才判）
    if is_meal_text(title):
        clash = _meal_clash(plugin, when_dt)
        if clash:
            return {"ok": False, "reason": "meal_conflict",
                    "message": "那天%s已经约了「%s」，一天就一顿正餐，换个时间吧"
                               % (human_day_period(when_dt, now), clash)}
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
    bridge = _bridge(plugin)
    if bridge.ready:
        item = {"start": when_dt.strftime("%H:%M"),
                "end": (when_dt + _dt.timedelta(hours=2)).strftime("%H:%M"),
                "activity": "和老公" + title, "category": "social",
                "place": "外面", "zone": "restaurant"}
        bridge.call("add_schedule_item", when_dt.date().isoformat(), item,
                    source="date", reason="约会")
        bridge.call("set_date_mood", "你在期待和老公%s，心情很好。" % title,
                    (when_dt + _dt.timedelta(hours=6)).timestamp())
        bridge.call("set_date_prep", title, when_dt.timestamp())
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


def _date_conflict(plugin, when_dt):
    """约会时段撞上上班/看病/睡觉 → 返回冲突的活动，供她自然拒绝。"""
    bridge = _bridge(plugin)
    if not bridge.ready:
        return ""
    day = when_dt.date().isoformat()
    try:
        items = list(bridge.schedule().get(day) or [])
    except Exception:
        return ""
    return schedule_conflict(items, when_dt)


def _drop_date_items(plugin, rows):
    """把被替换掉的旧约会从她的日程里摘掉（只动 source=date/social 的行）。"""
    bridge = _bridge(plugin)
    if not bridge.ready:
        return
    try:
        schedule = bridge.schedule()
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
            bridge.call("kv_set", "schedule", schedule)
    except Exception as exc:
        isolated("date_engine._drop_date_items", exc)


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
    bridge = _bridge(plugin)
    if broke:
        bridge.call("set_date_mood", "你刚被放鸽子，又没钱，情绪崩溃，只能吃泡面。",
                    now.timestamp() + 86400)
    elif late:
        bridge.call("set_date_mood", "你刚被老公放鸽子，很委屈很失落，接下来两天对他有点冷淡。",
                    now.timestamp() + 2 * 86400)
    else:
        bridge.call("set_date_mood", "老公临时去不了，你有点小失落，但还是体谅他。",
                    now.timestamp() + 43200)
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
            _bridge(plugin).call("set_date_mood", "", 0)
            return {"ok": True, "message": "收到啦，看在你这么有诚意的份上，原谅你啦！"}
    return None


ACTIVE_KEY = "date:active"


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
    bridge = state["bridge"]
    total = state["total"]
    dressing = await _attend_dress(finance, state["cash"], idem)
    fund = int(target.get("fund") or 0)
    pay, payer, proud = _attend_pay_mode(mode, fund, target, total)
    failure = await _attend_bill(finance, pay, target, idem, bridge)
    if failure is not None:
        return failure
    end, title = _attend_mark_attended(target, now, payer, total, dressing)
    when_label, company = await _attend_remember(finance, now, title)
    _save(plugin, rows)
    _set_active(plugin, {"title": target.get("title"), "start": now.timestamp(),
                         "end": end.timestamp(), "payer": payer, "status": "dating"})
    _attend_mood(finance, idem, payer, proud, fund, total, pay, target)
    bridge = _attend_amo_mood(plugin, end)
    await _attend_amo_schedule(bridge, now, end, title, when_label, company, target)
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
    title = str(target.get("title") or "约会")
    # 先把她切到「约会中」，再扣钱：否则财务日程会记在「家」，出现「在家买单」
    bridge = _bridge(plugin)
    if bridge.ready:
        now_line = "老公已经到你面前了，你们现在在一起。正在%s。" % title
        until = now.timestamp() + 2 * 3600
        bridge.call_compat("set_date_now", [
            ((now_line, until), {"since": now.timestamp()}),
            ((now_line, until), {}),
        ])
        bridge.call("set_current_action", "和老公在%s" % title, until,
                    place="外面", zone="restaurant", category="social")
    total = _base_cost(target.get("title")) + 5000   # 基础 + 打车往返
    state = {"rows": rows, "target": target, "finance": finance, "cash": cash,
             "idem": idem, "title": title, "bridge": bridge, "total": total}
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
    """买单模式：她请客 / AA / 火锅基金兜底，默认她买单（纯规则在 date_rules）。"""
    return pay_mode(mode, fund, target.get("title"), total)


async def _attend_bill(finance, pay, target, idem, bridge):
    """买单：扣款失败时撤回「约会中」状态并返回失败结果；成功返回 None。"""
    if pay:
        try:
            res = await finance.expense(pay, "约会：%s" % target.get("title"), idem + ":bill", necessary=True)
            if not (isinstance(res, dict) and res.get("ok")):
                if bridge is not None and bridge.ready:
                    _cancel_session(bridge, "bill_failed")
                    bridge.call("set_current_action", "", 0)
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
    period = meeting_period(int(now.hour))
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
    bridge = _bridge(plugin)
    if not bridge.ready:
        return None
    bridge.call("add_hunger", "amo", 100)
    bridge.call("set_date_mood",
                "你现在正在和老公约会，化了妆、穿了最好看的衣服，心情激动又开心，说话要甜。",
                end.timestamp())
    return bridge


async def _attend_amo_schedule(bridge, now, end, title, when_label, company, target):
    """日程收尾：约会中状态/事实/见面地点、随机小插曲与日程项清理。"""
    if bridge is None:
        return
    end_of_day = now.replace(hour=23, minute=59, second=0, microsecond=0)
    now_line = ("老公已经到你面前了，你们现在在一起。你正和老公在外面%s，"
                "他就站在你身边/坐在你对面，别再说自己一个人、也别问他在哪。" % title)
    bridge.call_compat("set_date_now", [
        ((now_line, end.timestamp()), {"since": now.timestamp()}),
        ((now_line, end.timestamp()), {}),
    ])
    fact = ("【绝对事实】老公已经到你面前了，你们现在在一起，正在%s。"
            "这是已经发生的事实，接下来只聊见面的互动。" % title)
    bridge.call("set_date_fact", fact, end_of_day.timestamp())
    act = bridge.call("current_activity").value or {}
    place = str(act.get("place") or "") if isinstance(act, dict) else ""
    bridge.call("set_date_meet", title, place)
    bridge.call("set_current_action", "正在和老公约会", end.timestamp(),
                place="外面", zone="restaurant", category="social")
    if random.random() < 0.3:
        incident = random.choice(DATE_INCIDENTS)
        bridge.call("set_date_incident", incident, end.timestamp())
        gate = getattr(bridge.resolve(), "request_push", None)
        if callable(gate):
            await gate(incident, "date_incident")
        else:
            await bridge.call_async("push_to_owner", incident)
    day = now.date().isoformat()
    item = {"start": now.strftime("%H:%M"), "end": end.strftime("%H:%M"),
            "activity": "和老公" + title, "category": "social", "place": "外面",
            "zone": "restaurant", "subject": "bot", "happened": True}
    bridge.call("add_schedule_item", day, item, source="date", reason="赴约")
    try:
        when_dt = _dt.datetime.strptime(str(target.get("when")), "%Y-%m-%d %H:%M")
        if when_dt.date() != now.date():
            old_day = when_dt.date().isoformat()
            items = list(bridge.schedule().get(old_day) or [])
            if not items:
                items = list(bridge.call("get_schedule", old_day).value or [])
            kept = [row for row in items if str(row.get("activity") or "") != "和老公" + title]
            if len(kept) != len(items):
                bridge.call("commit_schedule_change", old_day, kept, old_items=items,
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


def _date_loot(bridge, title):
    body = str(title or "")
    if not any(word in body for word in ("逛街", "商场", "买", "游乐园")):
        return ""
    if random.random() < 0.6:
        item = random.choice(LOOT_CLOTHES)
        bridge.call("add_closet_item", {"name": item, "color": "", "category": "top",
                                        "time_slot": "", "price": 0})
        return item
    item = random.choice(LOOT_HOME)
    housing = bridge.get("housing")
    if housing is not None:
        try:
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
    bridge = _bridge(plugin)
    if bridge.ready:
        bridge.call("set_date_mood", "刚碰面老公就被叫走加班，你很委屈、很难过，接下来别装开心。",
                    now.timestamp() + 86400)
        bridge.call("set_date_fact", "【绝对事实】你们刚碰面，老公就被老板叫回去加班，约会中断了，他没能陪你。",
                    now.replace(hour=23, minute=59, second=0, microsecond=0).timestamp())
        _cancel_session(bridge, "interrupted")
        bridge.call("set_current_action", "", 0)
    return {"ok": True, "interrupted": True, "title": title,
            "message": "（愣住）……好吧，你去忙吧，我等你回来。"}


def _agreed_to_end(plugin, text, now=None):
    """她刚提过「该回家了」（date_ending sent），他回短肯定词 → 视为同意收尾。"""
    if not is_agreement(text):
        return False
    bridge = _bridge(plugin)
    if not bridge.ready:
        return False
    now = now or _dt.datetime.now()
    if _amo_has(bridge, "date_ending"):
        result = bridge.call_compat("date_ending", [((now,), {}), ((), {})])
        ending = result.value if result.ok else {}
        # 会话 API 存在时只认会话，不再二次读老 kv
        return bool(isinstance(ending, dict) and ending.get("sent"))
    ending = bridge.call("kv_get", "date_ending", {}).value or {}
    if not isinstance(ending, dict) or not ending.get("sent"):
        return False
    try:
        return float(ending.get("until") or 0) > now.timestamp()
    except (TypeError, ValueError):
        return False


async def handle_date_text(plugin, text, now=None):
    """前置拦截：等待中收到短肯定词 → 赴约；约会中收到突发/结束词 → 中断/收尾。"""
    now = now or _dt.datetime.now()
    active = active_date(plugin)
    if active:
        bridge = _bridge(plugin)
        if is_date_resume(text):
            if bridge.ready:
                _resume_session(bridge)
        if is_date_pause(text):
            if bridge.ready:
                _pause_session(bridge, now)
            return {"handled": False, "action": "pause"}
        if is_interrupt(text):
            out = await interrupt_date(plugin, text, now)
            return {"handled": True, "action": "interrupt", "message": out.get("message") or ""}
        if _agreed_to_end(plugin, text, now):
            # 她先提「该回家了」，他回「好/走吧」：也算收尾，不能继续挂着约会
            await finish_date(plugin, now, reason="agreed")
            if bridge.ready:
                _clear_ending(bridge)
            return {"handled": False, "action": "finish"}
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


#: 收尾幂等（Phase 14）：同进程重入用内存集合，跨轮重复用 repo meta 记 start。
_FINISHING: set = set()
FINISH_MARK_KEY = "date:finish_mark"


def _finish_mark(plugin):
    try:
        return _repo(plugin).meta_get(FINISH_MARK_KEY, None)
    except Exception:
        return None


def _set_finish_mark(plugin, start):
    try:
        _repo(plugin).meta_set(FINISH_MARK_KEY, start or "")
    except Exception as exc:
        isolated("date_engine.finish_mark", exc)


def _truncate_date_schedule(bridge, now):
    """1) 正在进行的约会块截断到「现在」，还没开始的直接去掉（约会结束后不该再挂）。"""
    try:
        day = now.date().isoformat()
        current = list(bridge.schedule().get(day) or [])
        patched, changed = truncate_finished_blocks(current, now)
        if changed:
            bridge.call("commit_schedule_change", day, patched, old_items=current,
                        source="date", reason="约会结束截断")
    except Exception as exc:
        isolated("date_engine.finish_date.truncate", exc)


def _clear_ghost_state(bridge):
    """3) 幽灵取消：清掉约会相关的待触发状态，别回家了还弹「上菜了」。"""
    for key in ("date_pause", "date_preview", "date_meal", "date_timer"):
        bridge.call("kv_set", key, {})
    _clear_ending(bridge)
    bridge.call("set_date_incident", "", 0)


async def _finish_home(plugin, bridge, title, now, reason):
    """日记/长期记忆/情绪 + 回家事实 + 会话收尾 + 回家日程（顺序固定）。"""
    finance = _finance(plugin)
    text = "[共同记忆] 今天和老公去%s约会了。" % title
    if finance is not None:
        try:
            finance.small_diary(text)
            await finance.permanent_memory(text, "date:finish:%s" % int(now.timestamp()))
            finance.nudge_mood({"开心": 25, "幸福": 20}, "date:finish:%s:mood" % int(now.timestamp()))
        except Exception as exc:
            isolated("date_engine.finish_date", exc)
    if not bridge.ready:
        return
    bridge.call("set_date_mood", "约会刚结束，你带着今天真实的心情回到家。", now.timestamp() + 86400)
    bridge.call("set_last_date_event", title, now.timestamp())
    bridge.call("set_date_prep", "", 0)
    end_of_day = now.replace(hour=23, minute=59, second=0, microsecond=0)
    if end_of_day.timestamp() <= now.timestamp():
        end_of_day = now + _dt.timedelta(hours=6)
    home_line = ("【绝对事实】约会已经结束，他不在你身边；你一个人回到出租屋，接下来只能手机/视频联系。"
                 "他已经回他自己那边去了，你现在洗了澡、卸了妆、换上舒服的睡衣，"
                 "累但很充实。")
    bridge.call("set_date_fact", home_line, end_of_day.timestamp())
    # 顺序：日程截断（上面）→ 关会话写 date_home → 再写「一个人走回家」，
    # 避免回家日程被截断逻辑误伤，也避免 date_home 先写导致块还在。
    _finish_session(bridge, title, reason, now)
    home_at = (now + _dt.timedelta(minutes=10)).strftime("%H:%M")
    sofa_end = (now + _dt.timedelta(minutes=90)).strftime("%H:%M")
    day = now.date().isoformat()
    bridge.call("add_schedule_item", day,
                {"start": now.strftime("%H:%M"), "end": home_at,
                 "activity": "一个人走回家（耳机里还连着）", "place": "路上",
                 "category": "life", "zone": "road", "subject": "bot", "happened": True},
                source="date", reason="约会收尾")
    bridge.call("add_schedule_item", day,
                {"start": home_at, "end": sofa_end,
                 "activity": "到家给他发消息/视频道晚安", "place": "家",
                 "category": "rest", "zone": "home", "subject": "bot"},
                source="date", reason="约会收尾")
    bridge.call("set_current_action", "一个人到家，给他发消息",
                (now + _dt.timedelta(minutes=90)).timestamp(),
                place="家", zone="home", category="rest")
    ending_fact = {
        "user_done": "时间不早了，吃饱喝足了，该回去了；约会结束，他不在你身边，你自己先回家。",
        "timeout": "约会已经两个多小时，吃饱喝足了，该回去了；他不在你身边，你自己先回家。",
        "silent": "他好像去忙了，你在犹豫是继续等他，还是自己先溜达回去；他不在你身边。",
    }.get(reason, "约会该收尾了，该回去了；他不在你身边，你自己先回家。")
    bridge.call("set_date_ending", ending_fact, reason=reason)
    _date_loot(bridge, title)


async def _finish_date_once(plugin, now, reason, title, bridge):
    _truncate_date_schedule(bridge, now)
    _clear_ghost_state(bridge)
    await _finish_home(plugin, bridge, title, now, reason)
    if bridge.ready:
        # 2) 剩余时间交给大模型重排（严禁代码写死「休息」）
        try:
            await replan_remaining(plugin, bridge, now, title)
        except Exception as exc:
            isolated("date_engine.finish_date.replan", exc)
    finance = _finance(plugin)
    if finance is not None:
        try:
            finance.nudge_mood({"疲惫": 15, "饥饿": 8}, "date:finish:%s:body" % int(now.timestamp()))
        except Exception as exc:
            isolated("date_engine.finish_date.3", exc)


async def finish_date(plugin, now=None, reason="timeout"):
    """结束约会：只解锁状态 + 写客观事实，**不生成任何道别台词**（交给大模型）。

    Phase 14 幂等：tick 与聊天可能同时判定结束，重复收尾只生效一次——
    同进程用 _FINISHING 挡住，跨轮用 repo meta 里的 date:finish_mark（记这次约会的
    start）挡住，不会多写一份记忆/日记/回家日程。
    """
    now = now or _dt.datetime.now()
    active = _active(plugin)
    if not active:
        return None
    key = id(plugin)
    if key in _FINISHING:
        return {"ok": True, "duplicate": True, "reason": reason}
    start = active.get("start")
    if start and _finish_mark(plugin) == start:
        isolated("date_engine.finish_date.duplicate", RuntimeError("重复收尾已忽略"))
        return {"ok": True, "duplicate": True, "reason": reason}
    title = str(active.get("title") or "约会")
    _set_active(plugin, {})
    bridge = _bridge(plugin)
    _FINISHING.add(key)
    try:
        await _finish_date_once(plugin, now, reason, title, bridge)
    finally:
        _FINISHING.discard(key)
    if start:
        _set_finish_mark(plugin, start)
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


async def replan_remaining(plugin, bridge, now, title=""):
    """约会提前结束后：让 LLM 自己安排剩下的时间，代码不做任何默认填充。"""
    if not isinstance(bridge, AmoBridge):      # 兼容老调用：直接传 amo
        bridge = AmoBridge(plugin, amo=bridge)
    llm = bridge.get("llm")
    if llm is None or not callable(getattr(llm, "chat_raw", None)):
        return []
    umos = bridge.owner_umos()
    umo = umos[0] if umos else ""
    finance = _finance(plugin)
    mood, fatigue = "平静", 0
    try:
        mood = str((finance.mood() or {}).get("word") or "平静")
    except Exception:
        pass  # cleanup: best-effort
    try:
        fatigue = int(finance.fatigue() or 0)
    except Exception:
        pass  # cleanup: best-effort
    prompt = ("现在是 %s。你们刚在%s结束约会、买完单；约会结束他不在她身边，她一个人回去，"
              "接下来只能手机/视频联系。\n"
              "她现在的状态：心情%s，疲惫值%d。\n"
              "请把 %s 到 23:00 的时间安排成 2-4 条日程，首尾相接、不重叠，"
              "只能写事实里出现过的地点（家 / 路上 / 楼下小馆）。\n"
              "JSON 数组：{\"start\":\"HH:MM\",\"end\":\"HH:MM\",\"activity\":\"一句人话\","
              "\"place\":\"家|路上\",\"category\":\"rest|life|social\",\"zone\":\"home|road\"}"
              % (now.strftime("%H:%M"), title or "外面", mood, fatigue, now.strftime("%H:%M")))
    result = await bridge.call_async("chat_raw", umo, PLAN_SYSTEM, prompt)
    if not result.ok:
        return []
    segments = _parse_segments(result.value, now)
    for seg in segments:
        bridge.call("add_schedule_item", now.date().isoformat(), seg,
                    source="date", reason="约会收尾重推")
    return segments


async def tick(plugin, now=None):
    """每轮调度：约会结束自动退出；过了约定时间没赴约 → 他忘了。"""
    now = now or _dt.datetime.now()
    active = _active(plugin)
    if active and float(active.get("end") or 0) <= now.timestamp():
        await finish_date(plugin, now, reason="timeout")
    elif active and _pause_expired(_bridge(plugin), now):
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
        _bridge(plugin).call("set_date_mood", "他忘了你们的约会，你很失落，接下来两天有点冷淡。",
                             now.timestamp() + 2 * 86400)
    if changed:
        _save(plugin, rows)
    return {"finished": bool(active), "missed": changed}
