# -*- coding: utf-8 -*-
"""逛街购物：周末/情绪/发薪触发，按余额档位消费，衣服同步陪伴衣橱。

2026-11-22 task-24：把「试穿/购买」从两条演出事件改成一条状态机
（services/shopping_state.py）：
- interest → tryon → approved → purchased，只有 purchased 才允许说「买了/花了」；
- tick 只排 tryon 事件，不再预先排 +180min 的 home（以前到点必说买了，是假购买源头）；
- 用户同意后走一笔事务：扣款（账本）→ 衣橱入库 → home 事件入队，三步都成功才置
  purchased；任一步失败退款并把衣服放回衣架（不说买了）；
- home 事件必须带 purchase_id，flush 时去 shop:purchases 对账，没有完整购买记录一律
  不发「买了」；
- 颜色+单品名统一走 shopping_state.item_label()（修「米白色米白色」）；
- shopping_prompt / 主动推送只用「快一千了 / 卡里余额还够」这种人话，不写精确金额。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import re

try:
    from ..utils.helpers import stable_int
    from ..utils.money import idem as idem_key
    from .city import current_city, factor_of
except ImportError:
    from utils.helpers import stable_int
    from utils.money import idem as idem_key
    from services.city import current_city, factor_of

try:  # 包模式优先
    from ..guard import isolated
except ImportError:  # 离线测试 / 顶层加载
    from guard import isolated

try:  # 统一日志（离线单测没有 astrbot 时退回标准库）
    from astrbot.api import logger
except Exception:  # pragma: no cover - 离线单测
    import logging

    logger = logging.getLogger("watch")

try:  # 跨插件只读状态快照（task-27）：余额/地点与陪伴唯一同源
    from ..core import life_state as _life_state
except ImportError:
    from core import life_state as _life_state

try:  # 购物状态机纯规则（task-24）
    from . import shopping_state as _state
except ImportError:
    from services import shopping_state as _state

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

#: 购买事务日志（shop:purchases）：只有三步都 True 的记录才允许说「买了」。
PURCHASES_KEY = "shop:purchases"
PURCHASES_LIMIT = 20
#: 统一失败话术：不出现技术词，也不冒充「买了」。
FAILURE_LINES = {
    "expense": "唔…卡里的钱不太够，这件先不买了，衣服放回衣架啦。",
    "wardrobe": "唔…试衣间那边说这件没合适的码，今天没买成，衣服先放回去了。",
    "event": "唔…这件先没买成，衣服放回去了，明天再来看。",
    "refund": "唔…这次没买成，钱退回来了，衣服放回去了。",
}
#: 同一笔 pending 的结算串行锁（重复回复/重复 tick 不会扣两次钱）。
_LOCKS: dict = {}


def _amo(plugin):
    finance = getattr(plugin, "finance", None)
    return finance._amo() if finance is not None and hasattr(finance, "_amo") else None


def _can_shop(amo):
    """唯一在场策略在陪伴侧 amo_presence.can_shop：不在商场/街上/景区就不逛街。

    直接调用 amo.can_shop()（跨插件契约测试会核对对面真的有这个方法）；
    旧宿主没有统一策略时宁可不逛街，也不凭空说在商场。
    """
    try:
        return bool(amo.can_shop())
    except Exception:
        return False


def _can_engage(amo, kind=""):
    """统一主动闸（陪伴侧 amo_presence.can_proactive）：沉默 6 小时不催问/不逛街/不追问。"""
    try:
        return bool(amo.can_engage(str(kind or "")))
    except Exception:
        return False


def _cash_cents(finance, life=None):
    """余额：优先 LifeStateSnapshot（task-27），再退回本地钱包。"""
    cash = 0
    if life is not None and getattr(life, "available", False):
        try:
            cash = int(life.wallet_balance_cents() or 0)
        except Exception:
            cash = 0
    if cash <= 0 and finance is not None:
        try:
            cash = int(finance.balance_cents() or 0)
        except Exception:
            cash = 0
    return cash


def _scene(life):
    """试穿/购买事实带场景（task-27/25）：place + 场景账本 event_id。"""
    data = {"place": "", "event_id": "", "snapshot": "local"}
    if life is not None and getattr(life, "available", False):
        data["snapshot"] = "life_state"
        try:
            data["place"] = str(life.place() or "")
            data["event_id"] = str(life.get("location.event_id") or "")
        except Exception:  # cleanup: 场景快照读不到时退回空场景，不阻塞主流程
            pass
    return data


def _lock_for(key):
    key = str(key or "")
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


# ---------------------------------------------------------------- 记录读写

def _pending_events(repo):
    try:
        raw = repo.meta_get("shop:events", []) or []
        if isinstance(raw, str):
            raw = json.loads(raw or "[]")
        return list(raw)
    except Exception:
        return []


def _save_events(repo, events) -> bool:
    try:
        repo.meta_set("shop:events", json.dumps(list(events)[-20:], ensure_ascii=False))
        return True
    except Exception as exc:
        isolated("shopping_engine.save_events", exc)
        return False


def _pending_order(repo):
    try:
        raw = repo.meta_get("shop:pending", {}) or {}
        if isinstance(raw, str):
            raw = json.loads(raw or "{}")
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_pending(repo, pending) -> bool:
    try:
        repo.meta_set("shop:pending", json.dumps(pending, ensure_ascii=False))
        return True
    except Exception as exc:
        isolated("shopping_engine.save_pending", exc)
        return False


def _clear_pending(repo):
    try:
        repo.meta_set("shop:pending", {})
    except Exception as exc:
        isolated("shopping_engine._clear_pending", exc)


def _purchases(repo):
    try:
        raw = repo.meta_get(PURCHASES_KEY, []) or []
        if isinstance(raw, str):
            raw = json.loads(raw or "[]")
        return [dict(item) for item in raw if isinstance(item, dict)]
    except Exception:
        return []


def _save_purchases(repo, rows) -> bool:
    try:
        repo.meta_set(PURCHASES_KEY, json.dumps(list(rows)[-PURCHASES_LIMIT:], ensure_ascii=False))
        return True
    except Exception as exc:
        isolated("shopping_engine.save_purchases", exc)
        return False


def _find_purchase(repo, purchase_id):
    key = str(purchase_id or "").strip()
    if not key:
        return None
    for row in reversed(_purchases(repo)):
        if str(row.get("id") or "") == key:
            return row
    return None


def _purchase_idem(pending):
    return idem_key("shop:purchase", int(float(pending.get("created") or 0)))


def _drop_open_events(repo, pending):
    """放弃/超时/失败时，把这件衣服未发生的 tryon 事件清掉；已买的 home 事件不动。"""
    name = str((pending or {}).get("name") or "")
    events = _pending_events(repo)
    keep = [event for event in events
            if not (str(event.get("kind")) == "tryon" and str(event.get("name")) == name)]
    return _save_events(repo, keep)


# ---------------------------------------------------------------- 事务辅助

def _closet_ok(result) -> bool:
    """衣橱入库结果：新宿主返回 {ok:...}；老宿主返回 None 时按成功处理。"""
    if isinstance(result, dict):
        return bool(result.get("ok"))
    return result is not False


def _add_to_wardrobe(amo, pending, price) -> bool:
    add = getattr(amo, "add_closet_item", None)
    if not callable(add):
        return False
    try:
        result = add({
            "name": pending.get("name"),
            "color": pending.get("color"),
            "category": pending.get("category"),
            "time_slot": pending.get("time_slot"),
            "price": int(price or 0) // 100,
        })
    except Exception as exc:
        isolated("shopping_engine.add_closet", exc)
        return False
    return _closet_ok(result)


def _wallet_has_idem(amo, idem) -> bool:
    """钱包流水里已经有这笔（崩溃后重放时避免二次扣款）。只看公开 ledger()。"""
    wallet = getattr(amo, "wallet", None)
    ledger = getattr(wallet, "ledger", None)
    if not callable(ledger):
        return False
    try:
        rows = ledger(80) or []
    except Exception:
        return False
    key = str(idem or "")
    return any(str((row or {}).get("idem") or "") == key for row in rows
               if isinstance(row, dict))


def _refund(amo, idem, price, label) -> bool:
    """事务回滚：原路退回这笔衣服钱（refund: 前缀，不计入消费）。"""
    wallet = getattr(amo, "wallet", None)
    credit = getattr(wallet, "credit", None)
    if not callable(credit):
        return False
    try:
        return bool(credit(int(price or 0), "退款：%s 没买成" % label,
                           "refund:" + str(idem or ""), ref=str(idem or "")))
    except Exception as exc:
        isolated("shopping_engine.refund", exc)
        return False


async def _push(amo, text):
    fn = getattr(amo, "push_to_owner", None)
    if not callable(fn):
        return False
    try:
        return bool(await fn(text))
    except Exception as exc:
        isolated("shopping_engine.push", exc)
        return False


async def _push_failure(amo, kind="generic"):
    await _push(amo, FAILURE_LINES.get(str(kind or ""), FAILURE_LINES["event"]))


def _pending_allows(state) -> bool:
    return _state.normalize_state(state) in _state.OPEN_STATES


# ---------------------------------------------------------------- 定时触发

async def tick(plugin, now=None):
    now = now or _dt.datetime.now()
    # 逛街/交通只发生在白天：凌晨 0 点抽到"逛街交通"是排程 bug，不是生活。
    if not (9 <= int(now.hour) < 22):
        return None
    amo = _amo(plugin)
    finance = getattr(plugin, "finance", None)
    repo = getattr(plugin, "finance_repo", None)
    if amo is None or finance is None or not hasattr(amo, "shopping_trigger"):
        return None
    # task-27：余额/地点读 LifeStateSnapshot（能陪唯一事实），旧版再退回本地读法
    life = _life_state.read(plugin)
    # 唯一在场策略：只有人真的在商场/街上/景区才逛街；在家不触发（2026-11-22）。
    if not _can_shop(amo):
        return None
    try:
        reason = amo.shopping_trigger(now)
    except Exception:
        reason = ""
    if not reason:
        try:
            early_until = str(repo.meta_get("salary_early_until", "") or "") if repo is not None else ""
        except Exception:
            early_until = ""
        # 提前发工资后的周末：主动去逛街犒劳自己
        if early_until and now.date().isoformat() <= early_until and now.weekday() >= 5:
            reason = "提前发工资，周末犒劳自己"
    if not reason:
        return None
    day = now.date().isoformat()
    try:
        if repo is not None and repo.meta_get("shopping:" + day, False):
            return None
    except Exception as exc:
        isolated("shopping_engine.tick", exc)
    if repo is not None:
        # 上一笔还没走完（等他回复/结算中）：不覆盖 pending，交给 flush 收尾或退回
        current = _pending_order(repo)
        if current and _pending_allows(current.get("state")):
            return None
    cash = _cash_cents(finance, life)
    try:
        factor = float(factor_of(current_city(plugin)) or 1.0)
    except Exception:
        factor = 1.0
    spent = []
    for label, base in (("逛街交通", 3000), ("奶茶", 2000), ("逛街午饭", 6000)):
        amount = max(500, int(base * factor))
        try:
            res = await finance.expense(amount, label, idem_key("shop", day, label), necessary=False)
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
    pending = {}
    if buy:
        buy = max(500, int(buy * factor))
        item = {"name": name, "color": color, "category": cat,
                "time_slot": slot, "price": buy}
        # 状态机从 interest 开始：先在试衣间看到，再把 tryon 事件排到 90 分钟后。
        pending = _state.pending_record(
            name, color, cat, slot, buy, cash, now.timestamp(), now.timestamp() + 15 * 60,
            scene=_scene(life))
        if repo is None or not _save_pending(repo, pending):
            # pending 落不了盘：宁可不演这出，也不能出现"试了/想买"却没有可结算状态
            logger.info("[WATCH][SHOP] pending 落盘失败，本次不发起试穿")
            pending, item, buy = {}, None, 0
    if item:
        label = _state.item_label(name, color)
        try:
            # 人话口径：不要报“985/4072”这种精确数字，正常人说“快一千了、手里还有点钱”
            facts = "在商场试了一件%s，觉得挺好看，就是好贵，%s，手里还有点钱" % (
                label, _state.money_words(buy))
            if hasattr(amo, "note_recent_topic"):
                # 即使这次推送没发出去，他追问「差多少」也能接住
                amo.note_recent_topic(facts, now.timestamp() + 2 * 3600,
                                      key="shop:%s" % now.date().isoformat())
            style = "钱不够，想让他赞助一点" if cash < buy else "很喜欢，想问能不能买"
            line = ""
            try:
                from . import social_ai
                line = await social_ai.phrase(plugin, facts, style)
            except Exception as exc:
                isolated("shopping_engine.phrase", exc)
            if line:
                await _push(amo, line)
                if hasattr(amo, "note_recent_topic"):
                    amo.note_recent_topic(facts + "；你刚跟他说：" + line,
                                          now.timestamp() + 2 * 3600,
                                          key="shop:%s" % now.date().isoformat())
            if hasattr(amo, "note_shopping_pending"):
                # 追问口径也是人话：价格/余额只说大概，绝不注入精确金额（真实飞书 985/4072）
                amo.note_shopping_pending(
                    "你正在商场试%s，价格%s，%s，等老公回复"
                    % (label, _state.money_words(buy), _state.cash_phrase(cash, buy)),
                    now.timestamp() + 15 * 60,
                    name=name, color=color, price_cents=buy)
        except Exception as exc:
            isolated("shopping_engine.tick.2", exc)
        # 她已经在试了：interest -> tryon（失败也保留 tryon，tick 只排 tryon 事件）
        pending["state"] = _state.advance(pending.get("state"), _state.TRYON)
        _save_pending(repo, pending)
    try:
        if repo is not None:
            repo.meta_set("shopping:" + day, "1")
            if item:
                events = _pending_events(repo)
                events.append(_state.tryon_event(
                    item, now.timestamp() + 90 * 60, cash - buy, scene=_scene(life)))
                _save_events(repo, events)
    except Exception as exc:
        isolated("shopping_engine.tick.3", exc)
    if finance is not None:
        try:
            if item:
                finance.small_diary(
                    "今天%s，试了一件%s，等老公点头再买，好期待！"
                    % (reason, _state.item_label(item.get("name"), item.get("color"))))
            else:
                finance.small_diary("今天%s，只逛不买，看到一条好漂亮的裙子但买不起，好难过。" % reason)
        except Exception as exc:
            isolated("shopping_engine.tick.4", exc)
    return {"reason": reason, "item": item, "spent": spent}


# ---------------------------------------------------------------- 购买事务

async def _settle_purchase(plugin, repo, finance, amo, pending, record, now):
    """approved 之后的唯一结算入口：账本→事件→衣橱→purchased（任一步失败就退款）。

    顺序是刻意的：home 事件只是先排队（flush 必须对账到完整购买记录才允许发），
    衣橱放最后入库。这样任何一步失败只需要"退款 + 撤事件"，不用回滚衣橱。
    幂等：事务日志 shop:purchases 记住每步是否完成，崩溃/重复回复都不会扣两次钱。
    """
    label = str(record.get("label") or _state.item_label(pending.get("name"), pending.get("color")))
    price = int(record.get("price") or pending.get("price") or 0)
    idem = str(record.get("id") or _purchase_idem(pending))
    record["id"] = idem
    steps = record.setdefault("steps", {step: False for step in _state.PURCHASE_STEPS})
    # 1) 账本扣款
    if not steps.get("debited"):
        if _wallet_has_idem(amo, idem):
            steps["debited"] = True   # 上次已经扣过（崩溃重放）
        else:
            try:
                res = await finance.expense(price, "买衣服：%s" % label, idem, necessary=True)
            except Exception as exc:
                isolated("shopping_engine.buy.expense", exc)
                res = {"ok": False}
            if not isinstance(res, dict) or not res.get("ok"):
                # 扣款没发生：退回 tryon，不改衣橱/事件，可以等他转账后再试
                record["state"] = _state.FAILED
                record["reason"] = "expense_failed"
                _merge_purchase(repo, record)
                pending["state"] = _state.advance(pending.get("state"), _state.TRYON)
                _save_pending(repo, pending)
                await _push_failure(amo, "expense")
                return {"ok": False, "bought": False, "reason": "expense"}
            steps["debited"] = True
        record["steps"] = steps
        _merge_purchase(repo, record)
    # 2) home 事件入队（"回家炫耀"必须带 purchase_id，flush 据此对账；此时还不会发）
    if not steps.get("event"):
        events = _pending_events(repo)
        events = [item for item in events
                  if str(item.get("purchase_id") or "") != idem]
        events.append(_state.home_event(pending, now.timestamp() + 60 * 60,
                                        _cash_cents(finance), idem,
                                        scene=_scene(_life_state.read(plugin))))
        if not _save_events(repo, events):
            refunded = _refund(amo, idem, price, label)
            steps["debited"] = not refunded   # 退款失败：保留 debited，flush 会重试退款
            record["steps"] = steps
            _fail_purchase(repo, pending, record, "event_failed")
            await _push_failure(amo, "event" if refunded else "refund")
            return {"ok": False, "bought": False, "reason": "event"}
        steps["event"] = True
        record["steps"] = steps
        _merge_purchase(repo, record)
    # 3) 衣橱入库（最后一步：失败只退款+撤事件，不用回滚衣橱）
    if not steps.get("wardrobe"):
        if not _add_to_wardrobe(amo, pending, price):
            refunded = _refund(amo, idem, price, label)
            steps["debited"] = not refunded
            steps["event"] = False
            record["steps"] = steps
            _drop_event_by_purchase(repo, idem)
            _fail_purchase(repo, pending, record, "wardrobe_failed")
            await _push_failure(amo, "wardrobe" if refunded else "refund")
            return {"ok": False, "bought": False, "reason": "wardrobe"}
        steps["wardrobe"] = True
        record["steps"] = steps
        _merge_purchase(repo, record)
    # 4) 提交：三步都完成才允许 purchased
    if not all(bool(steps.get(step)) for step in _state.PURCHASE_STEPS):
        isolated("shopping_engine.buy.incomplete", RuntimeError(str(steps)))
        return {"ok": False, "bought": False, "reason": "incomplete"}
    record["state"] = _state.PURCHASED
    _merge_purchase(repo, record)
    pending["state"] = _state.PURCHASED
    _save_pending(repo, pending)
    _clear_pending(repo)
    # 5) 人话副作用（买到已成立；这里失败不影响"已买"事实）
    try:
        if hasattr(amo, "note_purchase"):
            amo.note_purchase(pending.get("name"), price)
        hook = getattr(finance, "furniture_bought", None)
        if callable(hook):
            hook(pending.get("name"))
        finance.small_diary("老公同意我买%s啦，超开心！" % label)
        await finance.permanent_memory(
            "我买了%s（%s），%d 元。" % (pending.get("name"), pending.get("color"), price // 100),
            idem_key("shop:purchase", int(float(pending.get("created") or 0)), "mem"))
    except Exception as exc:
        isolated("shopping_engine.buy.side_effects", exc)
    await _push(amo, "谢谢老公！我买啦，等我回家穿给你看~")
    return {"ok": True, "bought": True, "purchase_id": idem}


def _fail_purchase(repo, pending, record, reason):
    """事务失败收尾：日志记 FAILED、退掉 pending、把这件衣服的未发生事件作废。"""
    record["state"] = _state.FAILED
    record["reason"] = str(reason or "")
    _merge_purchase(repo, record)
    pending["state"] = _state.FAILED
    _save_pending(repo, pending)
    _drop_open_events(repo, pending)
    _clear_pending(repo)


def _drop_event_by_purchase(repo, purchase_id):
    key = str(purchase_id or "")
    if not key:
        return False
    events = _pending_events(repo)
    keep = [event for event in events
            if str(event.get("purchase_id") or "") != key]
    return _save_events(repo, keep)


def _retry_refunds(repo, amo):
    """退款失败的事务：下次 flush 继续原路退，直到钱退回（幂等 refund: 前缀）。"""
    rows = _purchases(repo)
    changed = False
    for row in rows:
        if _state.normalize_state(row.get("state")) != _state.FAILED:
            continue
        if str(row.get("reason") or "") not in ("wardrobe_failed", "event_failed"):
            continue
        steps = row.get("steps") if isinstance(row.get("steps"), dict) else {}
        if not steps.get("debited"):
            continue
        label = str(row.get("label") or _state.item_label(row.get("name"), row.get("color")))
        if _refund(amo, row.get("id"), int(row.get("price") or 0), label):
            steps["debited"] = False
            row["steps"] = steps
            row["refunded"] = True
            changed = True
    if changed:
        _save_purchases(repo, rows)
    return changed


def _merge_purchase(repo, record):
    rows = _purchases(repo)
    key = str(record.get("id") or "")
    merged = [row for row in rows if str(row.get("id") or "") != key]
    merged.append(dict(record))
    merged.sort(key=lambda row: float(row.get("created") or 0))
    return _save_purchases(repo, merged)


async def _recover_purchase(plugin, repo, finance, amo, now):
    """收尾半截事务：approved 未完成 → 继续；已 purchased 但 pending 没清 → 清掉。"""
    pending = _pending_order(repo)
    if not pending:
        return None
    state = _state.normalize_state(pending.get("state"))
    if state == _state.PURCHASED:
        _clear_pending(repo)
        return {"ok": True, "bought": True, "recovered": True}
    if state != _state.APPROVED:
        return None
    idem = _purchase_idem(pending)
    record = _find_purchase(repo, idem)
    if record is None:
        record = _state.purchase_record(pending, idem, now.timestamp())
        if not _save_purchases(repo, _purchases(repo) + [record]):
            return {"ok": False, "reason": "journal"}
    return await _settle_purchase(plugin, repo, finance, amo, pending, record, now)


# ---------------------------------------------------------------- 到点推送

async def flush(plugin, now=None):
    """到点主动发：试穿问好看 / 买到后回家炫耀；未入账的 home 事件一律不发。"""
    now = now or _dt.datetime.now()
    repo = getattr(plugin, "finance_repo", None)
    amo = _amo(plugin)
    if repo is None or amo is None or not hasattr(amo, "push_to_owner"):
        return None
    finance = getattr(plugin, "finance", None)
    # task-27：文案里的“卡里还剩多少”用快照实时余额，不用事件里写死的旧数
    life = _life_state.read(plugin)
    try:
        _retry_refunds(repo, amo)
    except Exception as exc:
        isolated("shopping_engine.retry_refunds", exc)
    try:
        await _recover_purchase(plugin, repo, finance, amo, now)
    except Exception as exc:
        isolated("shopping_engine.recover", exc)
    pending = _pending_order(repo)
    state = _state.normalize_state(pending.get("state")) if pending else ""
    if pending and _state.is_open(state) and float(pending.get("due") or 0) <= now.timestamp():
        # 统一主动闸：用户沉默 6 小时后不再追问，这条 pending 直接作废。
        if state != _state.APPROVED and not _can_engage(amo, "nudge"):
            _expire_pending(repo, pending, state)
        elif state != _state.APPROVED:
            try:
                nudges = int(pending.get("nudges") or 0)
            except (TypeError, ValueError):
                nudges = 0
            if nudges <= 0:
                # 第一次到点：还在商场犹豫，不把衣服放回去（真正的"试/没试"由 tryon 事件决定）
                line = ""
                try:
                    from . import social_ai as _social_ai
                    line = await _social_ai.phrase(
                        plugin, "试穿后等了十五分钟没等到回复，她还在犹豫要不要买", "有点失落")
                except Exception as exc:
                    isolated("shopping_engine.flush.phrase", exc)
                if line:
                    await _push(amo, line)
                pending["nudges"] = 1
                # 同一次逛街的收尾时间：tick 后 180 分钟还没回音才真的放回衣架
                pending["due"] = float(pending.get("created") or 0) + 180 * 60
                _save_pending(repo, pending)
            else:
                line = ""
                try:
                    from . import social_ai as _social_ai
                    line = await _social_ai.phrase(
                        plugin, "等了半天没等到回复，她把衣服放回衣架了", "有点失落")
                except Exception as exc:
                    isolated("shopping_engine.flush.phrase", exc)
                if line:
                    await _push(amo, line)
                _expire_pending(repo, pending, state)
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
        kind = str(event.get("kind") or "")
        name = str(event.get("name") or "衣服")
        color = str(event.get("color") or "")
        price = int(event.get("price") or 0)
        cash = int(event.get("cash") or 0)
        if life.available and life.wallet_balance_cents() > 0:
            cash = life.wallet_balance_cents()
        label = _state.item_label(name, color)
        if kind == "tryon":
            # 统一主动闸：人不在商场 / 用户沉默 6 小时 → 不再发「在商场试」类。
            if not _can_engage(amo, "shopping"):
                continue
            # 快照明确“不在商场/在家”时，试穿推送与日程直接矛盾，作废
            if life.available and life.in_shop() is False:
                continue
            current = _pending_order(repo)
            if not _state.claim_allowed("tryon", current.get("state")):
                # 没有在试的 pending（已放弃/超时/已买）→ 不许再声称在试
                continue
            facts = "在商场试了%s，想问好不好看" % label
            style = "期待他夸"
        elif kind == "home":
            # 回家炫耀是「已经买了」的事实：必须对账到完整的购买事务记录。
            if not _can_engage(amo, "share"):
                continue
            record = _find_purchase(repo, event.get("purchase_id"))
            if not _state.claim_allowed("home", event.get("state"), record):
                # 没有完整购买事务（账本+衣橱+事件）：宁可丢掉这条炫耀，也不说「买了」
                logger.info("[WATCH][SHOP] 未入账的回家事件不生成「买了」文案（%s）",
                            str(event.get("name") or "-")[:24])
                continue
            wallet = "卡里不多了，有点心虚" if cash < 200000 else "手里还有点钱"
            facts = "逛街回来，买了%s，花了%s，%s" % (
                label, _state.money_words(price), wallet)
            style = "有点心虚，怕他说乱花钱" if price > 50000 and cash < 200000 else "开心炫耀"
        else:
            continue
        line = ""
        try:
            from . import social_ai as _social_ai
            line = await _social_ai.phrase(plugin, facts, style)
        except Exception as exc:
            isolated("shopping_engine.flush.phrase", exc)
        if not line:
            keep.append(event)   # 模型不可用：留到下次再试，不塞模板
            continue
        try:
            await amo.push_to_owner(line)
            sent.append(line)
        except Exception:
            keep.append(event)
    _save_events(repo, keep)
    return {"sent": sent}


# ---------------------------------------------------------------- 用户回复

async def _reject(plugin, repo, finance, amo, pending, now):
    _set_terminal(repo, pending, _state.REJECTED)
    _clear_pending(repo)
    _drop_open_events(repo, pending)
    try:
        nudge = getattr(finance, "nudge_mood", None)
        if callable(nudge):
            nudge({"委屈": 6, "开心": -5}, idem_key("shop:reject", int(now.timestamp())))
    except Exception as exc:
        isolated("shopping_engine.handle_reply.mood", exc)
    await _push(amo, "好吧，听你的，那不买了……")
    return {"ok": True, "bought": False}


def _expire_pending(repo, pending, state):
    """超时/沉默：标记 expired、清 pending、把这件衣服的 tryon 事件一起作废。"""
    pending["state"] = _state.advance(state, _state.EXPIRED)
    _save_pending(repo, pending)
    _drop_open_events(repo, pending)
    _clear_pending(repo)


def _set_terminal(repo, pending, state):
    pending["state"] = _state.advance(pending.get("state"), state)
    _save_pending(repo, pending)


async def handle_reply(plugin, text, now=None):
    """用户在 pending 期间回复：买/好看/转账 → 结算；太贵/不好看 → 放弃。

    只有 approved→purchased 全链路成功才说「买啦」；未 approved 的回复不会产生购买事实。
    """
    now = now or _dt.datetime.now()
    repo = getattr(plugin, "finance_repo", None)
    finance = getattr(plugin, "finance", None)
    amo = _amo(plugin)
    pending = _pending_order(repo) if repo is not None else {}
    if not pending or finance is None or amo is None:
        return None
    state = _state.normalize_state(pending.get("state"))
    if state == _state.PURCHASED:
        return {"ok": True, "bought": True, "duplicate": True}
    if not _state.is_open(state):
        return None
    body = str(text or "")
    buy = int(pending.get("price") or 0)
    transfer = re.search(r"转账\]?\s*([0-9]+(?:\.[0-9]+)?)", body)
    yes = bool(transfer) or any(w in body for w in ("买", "好看", "可以", "要", "好呀", "行"))
    no = any(w in body for w in ("太贵", "不好看", "不买", "算了", "别买", "不要"))
    key = _purchase_idem(pending)
    async with _lock_for(key):
        # 锁内重读：另一个回复/定时任务可能已经推进/结算过
        pending = _pending_order(repo)
        state = _state.normalize_state(pending.get("state")) if pending else ""
        if not pending or state == _state.PURCHASED:
            return {"ok": True, "bought": True, "duplicate": True}
        if not _state.is_open(state):
            return None
        if no:
            return await _reject(plugin, repo, finance, amo, pending, now)
        if not yes:
            return None
        cash = _cash_cents(finance, _life_state.read(plugin))
        if cash < buy:
            await _push(amo, "啊哦，我的钱不够了，老公你能转我一点吗？")
            return {"ok": False, "bought": False, "reason": "insufficient"}
        # approved：先把事务日志落盘，再动钱（崩溃也能靠 shop:purchases 收尾/退款）
        pending["state"] = _state.advance(state, _state.APPROVED)
        pending["approved_at"] = now.timestamp()
        _save_pending(repo, pending)
        record = _state.purchase_record(pending, key, now.timestamp())
        if not _save_purchases(repo, _purchases(repo) + [record]):
            pending["state"] = _state.advance(pending.get("state"), _state.TRYON)
            _save_pending(repo, pending)
            await _push_failure(amo, "event")
            return {"ok": False, "bought": False, "reason": "journal"}
        return await _settle_purchase(plugin, repo, finance, amo, pending, record, now)
