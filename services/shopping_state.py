# -*- coding: utf-8 -*-
"""购物状态机（2026-11-22 task-24）：纯函数，不碰库/网络/文件。

背景（work/audit/media_facts.md §3.6 / §5.7）：
- 11-21 13:40 她说"买了米白色针织开衫，花了 649 元"，但 wallet_ledger 没有这笔、
  wardrobe 里也没有这件；同一件衣服在 11-22 又变回"985 买不买"。
- watch 的 shop:events 在 tick 时就排了一条 +180min 的 home 事件，到点必然说"买了"，
  跟"用户到底同不同意买"完全解耦。
- 颜色+单品名直接 "%s%s" 拼出"米白色米白色"。

所以购物只允许一条状态链：
    interest（看中/试穿前）→ tryon（在试）→ approved（他同意买、开始结算）
        → purchased（账本扣款 + 衣橱入库 + 事件入队全部成功）
任何"买了/花了"的人话只能在 purchased 之后由"已入账的购买记录"驱动；
未 purchased 的事件一律不许说买了。

本模块只放可单测的纯规则：
- 状态常量 / 合法迁移 / normalize_state；
- 「能不能说买了」的闸门 claim_allowed()；
- 单品显示名去重 item_label()、人话价格 money_words()、余额人话 cash_phrase()；
- pending / 事件记录的结构构造（不含 IO）。

IO（repo.meta、wallet、wardrobe、push）全部留在 shopping_engine.py。
"""
from __future__ import annotations

#: 状态：看中 -> 试穿 -> 他同意（开始结算） -> 已买
INTEREST = "interest"
TRYON = "tryon"
APPROVED = "approved"
PURCHASED = "purchased"
#: 终态：自己/他放弃、超时放回衣架、结算失败
REJECTED = "rejected"
EXPIRED = "expired"
FAILED = "failed"

STATES = (INTEREST, TRYON, APPROVED, PURCHASED, REJECTED, EXPIRED, FAILED)
#: 还能继续推进（未终结）的状态
OPEN_STATES = (INTEREST, TRYON, APPROVED)

#: 允许的迁移：只能往前走；approved 失败可以回 tryon 重来，但绝不跳级。
_TRANSITIONS = {
    INTEREST: (TRYON, APPROVED, REJECTED, EXPIRED),
    TRYON: (APPROVED, REJECTED, EXPIRED),
    APPROVED: (PURCHASED, TRYON, FAILED),
    PURCHASED: (),
    REJECTED: (),
    EXPIRED: (),
    FAILED: (),
}

#: 说"买了/花了"的唯一状态：purchased。
PURCHASE_CLAIM_STATE = PURCHASED
#: 允许发"在商场试穿"的状态（interest/tryon；approved 后已经在结算，不再问好不好看）
TRYON_PUSH_STATES = (INTEREST, TRYON)
#: 购买记录（shop:purchases）里必须完成的步骤；顺序即执行顺序。
#: 事件先排队、衣橱最后入库：任何一步失败都只需退款+撤事件，不用回滚衣橱。
PURCHASE_STEPS = ("debited", "event", "wardrobe")


def normalize_state(value) -> str:
    """坏数据/旧数据一律当 interest：旧 pending 没有 state 字段。"""
    text = str(value or "").strip()
    return text if text in STATES else INTEREST


def can_transition(src, dst) -> bool:
    return str(dst or "") in _TRANSITIONS.get(normalize_state(src), ())


def advance(src, dst) -> str:
    """合法就返回新状态，不合法保持原状态（调用方据此判断有没有推进）。"""
    state = normalize_state(src)
    return dst if can_transition(state, dst) else state


def is_open(state) -> bool:
    return normalize_state(state) in OPEN_STATES


def is_purchased(state) -> bool:
    return normalize_state(state) == PURCHASED


def tryon_push_allowed(state) -> bool:
    """能不能发"在商场试…"：只有 interest/tryon。"""
    return normalize_state(state) in TRYON_PUSH_STATES


def purchase_complete(record) -> bool:
    """购买记录三步都完成且状态 purchased，才算真的买到了。"""
    entry = record if isinstance(record, dict) else {}
    if normalize_state(entry.get("state")) != PURCHASED:
        return False
    steps = entry.get("steps") if isinstance(entry.get("steps"), dict) else {}
    return all(bool(steps.get(step)) for step in PURCHASE_STEPS)


def claim_allowed(kind, state, purchase_record=None) -> bool:
    """人话事实的闸门：tryon 只认 tryon/interest；home（说买了）只认完整购买记录。

    purchase_record 是 shop:purchases 里的记录（没有就是 None）——光有事件里的
    state=purchased 不算，必须账本+衣橱+事件三步都对上，防"没买说买了"。
    """
    kind = str(kind or "").strip()
    if kind == "tryon":
        return tryon_push_allowed(state)
    if kind == "home":
        return is_purchased(state) and purchase_complete(purchase_record)
    return False


# ---------------------------------------------------------------- 人话
def item_label(name, color="") -> str:
    """单品显示名：名字里已经带颜色就不重复拼（修"米白色米白色"）。

    例：("米白色针织开衫", "米白色") -> "米白色针织开衫"
        ("针织开衫", "米白色")       -> "米白色针织开衫"
    两边都空时退回"衣服"，保证文案不出现空槽。
    """
    body = str(name or "").strip()
    tint = str(color or "").strip()
    if not body:
        return tint or "衣服"
    if not tint or tint in body:
        return body
    return tint + body


def money_words(price_cents) -> str:
    """把价格（分）说成人话，不报 985/4072 这种精确数字。"""
    try:
        value = int(price_cents or 0)
    except (TypeError, ValueError):
        value = 0
    if value >= 500000:
        return "好几千了"
    if value >= 200000:
        return "快两千了"
    if value >= 80000:
        return "快一千了"
    if value >= 50000:
        return "小几百了"
    if value >= 10000:
        return "一百来块"
    return "几十块"


def cash_phrase(cash_cents, price_cents) -> str:
    """余额人话：够/不太够，绝不把"卡里还剩 4072"写进聊天 prompt。"""
    try:
        cash = int(cash_cents or 0)
        price = int(price_cents or 0)
    except (TypeError, ValueError):
        cash, price = 0, 0
    return "卡里余额还够" if cash >= price else "卡里余额不太够"


# ---------------------------------------------------------------- 记录结构
def pending_record(name, color, category, time_slot, price, cash, now, due,
                   scene=None) -> dict:
    """tick 选到单品后的 pending（state=interest；真正在试再进 tryon）。"""
    scene = dict(scene or {})
    return {
        "name": str(name or ""),
        "color": str(color or ""),
        "category": str(category or "top"),
        "time_slot": str(time_slot or "day"),
        "price": int(price or 0),
        "cash": int(cash or 0),
        "due": float(due or 0),
        "created": float(now or 0),
        "state": INTEREST,
        "scene_id": str(scene.get("scene_id") or ""),
        "place": str(scene.get("place") or ""),
        "snapshot": str(scene.get("snapshot") or ""),
    }


def tryon_event(item, due, cash, scene=None) -> dict:
    """到点提醒"试穿问好不好看"；只允许 pending 在 interest/tryon 时发。"""
    item = dict(item or {})
    scene = dict(scene or {})
    return {
        "due": float(due or 0),
        "kind": "tryon",
        "state": TRYON,
        "name": item.get("name", ""),
        "color": item.get("color", ""),
        "price": int(item.get("price") or 0),
        "cash": int(cash or 0),
        "place": str(scene.get("place") or ""),
        "event_id": str(scene.get("event_id") or ""),
        "snapshot": str(scene.get("snapshot") or ""),
    }


def home_event(pending, due, cash, purchase_id, scene=None) -> dict:
    """买到之后才允许排"回家炫耀"；必须带 purchase_id 供 flush 对账。"""
    pending = dict(pending or {})
    scene = dict(scene or {})
    return {
        "due": float(due or 0),
        "kind": "home",
        "state": PURCHASED,
        "purchase_id": str(purchase_id or ""),
        "name": pending.get("name", ""),
        "color": pending.get("color", ""),
        "price": int(pending.get("price") or 0),
        "cash": int(cash or 0),
        "place": str(scene.get("place") or ""),
        "event_id": str(scene.get("event_id") or ""),
        "snapshot": str(scene.get("snapshot") or ""),
    }


def purchase_record(pending, idem, now) -> dict:
    """approved 时落的事务日志：三步都记 True 才算 completed。"""
    pending = dict(pending or {})
    return {
        "id": str(idem or ""),
        "state": APPROVED,
        "label": item_label(pending.get("name"), pending.get("color")),
        "name": str(pending.get("name") or ""),
        "color": str(pending.get("color") or ""),
        "price": int(pending.get("price") or 0),
        "created": float(now or 0),
        "steps": {step: False for step in PURCHASE_STEPS},
        "reason": "",
    }
