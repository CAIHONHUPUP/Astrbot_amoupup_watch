# -*- coding: utf-8 -*-
"""决定页扩展 + 消费对账（2026-11-17，只读）。

约定：
- 不新增任何写路径。数据全部来自现有可读字段：
    宵夜   amo kv late_snack_decision:<day>:<hh>（AmoBridge.late_snack_decisions）
    看病   amo kv sick_decision:<day>
    约会   amo kv last_date_event / date_prep
    购物   amo kv last_purchase
    社交   守护自己的 events 审计表（social:invite/accept/reject/expire）
- 金额单位一律「分」，前端再换元。
- 对账口径与 amo 面板「本时段消费」一致：只算 wallet 支出（delta_cents < 0），按
  (day, hour) 聚成块；决定金额和同小时支出对不上就标 pending（前端「待对账」），
  绝不静默吞掉。
"""
from __future__ import annotations
from typing import Any

import datetime as _dt

try:
    from ..guard import isolated
except ImportError:  # 按顶层包加载时
    from guard import isolated

#: 决定类型 -> 中文标签（决定页分组用）
KIND_LABELS = {
    "late_snack": "宵夜",
    "date": "约会",
    "social": "社交",
    "shopping": "购物",
    "sick": "看病",
}

#: 决定页展示顺序（宵夜在最前，与旧页一致）
KIND_ORDER = ("late_snack", "date", "social", "shopping", "sick")

STATUS_OK = "ok"
STATUS_PENDING = "pending"
STATUS_NA = "na"

#: 社交事件里只认这几种（与 social_engine.SOCIAL_KINDS 的子集对齐）
SOCIAL_KINDS = ("social:invite", "social:accept", "social:reject", "social:expire")


def _as_int(value, default=0):
    """宽松取整：面板/老数据里的字符串数字也算数。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _local(ts):
    """epoch 秒 -> 本地 datetime；非法返回 None。"""
    try:
        return _dt.datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _hour_bucket(ts):
    """epoch 秒 -> (YYYY-MM-DD, 小时)；非法返回 None。"""
    when = _local(ts)
    if when is None:
        return None
    return when.strftime("%Y-%m-%d"), when.hour


def _clock(ts):
    """epoch 秒 -> HH:MM；非法返回空串。"""
    when = _local(ts)
    return when.strftime("%H:%M") if when is not None else ""


def kv_value(bridge, key, default=None):
    """只读 kv_get；未就绪/失败/None 一律回落到 default。"""
    if bridge is None:
        return default
    try:
        result = bridge.call("kv_get", str(key), default)
    except Exception:
        return default
    if not getattr(result, "ok", False):
        return default
    return default if result.value is None else result.value


def ledger_rows(bridge, limit=300):
    """只读 wallet 流水；拿不到就是空列表（不抛、不写）。"""
    if bridge is None:
        return []
    try:
        result = bridge.call("ledger", max(1, _as_int(limit, 300)))
    except Exception:
        return []
    rows = result.value if getattr(result, "ok", False) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def ledger_blocks(rows):
    """流水 -> 按 (day, hour) 聚合的支出块；口径同 amo「本时段消费」（只算支出）。

    入账（工资/退款/转账）不是消费，一律跳过；ts 解析不了的行也不猜。
    """
    blocks: dict[str, Any] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        cents = _as_int(row.get("delta_cents"))
        if cents >= 0:
            continue
        bucket = _hour_bucket(row.get("ts"))
        if bucket is None:
            continue
        item = blocks.setdefault(bucket, {"day": bucket[0], "hour": bucket[1],
                                          "amount_cents": 0, "count": 0})
        item["amount_cents"] += abs(cents)
        item["count"] += 1
    return sorted(blocks.values(), key=lambda b: (b["day"], b["hour"]), reverse=True)


def blocks_for_day(blocks, day):
    """只留某一天的块（决定页默认只看今天）。"""
    wanted = str(day or "")
    return [dict(b) for b in (blocks or []) if str(b.get("day") or "") == wanted]


def reconcile(items, blocks, day=""):
    """宵夜决定 vs 同小时块支出：对上 ok，对不上 pending（前端标「待对账」）。

    返回 (rows, summary)；rows 与 items 下标一一对应，前端按下标贴标签。
    """
    index = {(str(b.get("day") or ""), _as_int(b.get("hour"))): b for b in (blocks or [])}
    checked = 0
    pending = 0
    decision_total = 0
    rows = []
    for pos, item in enumerate(items or []):
        item = item if isinstance(item, dict) else {}
        cents = _as_int(item.get("amount_cents"))
        day_text = str(item.get("day") or day or "")
        hour = _as_int(item.get("hour"))
        spent = _as_int((index.get((day_text, hour)) or {}).get("amount_cents"))
        if cents <= 0:
            status = STATUS_NA            # 没花钱，不参与对账
        else:
            checked += 1
            decision_total += cents
            status = STATUS_OK if spent == cents else STATUS_PENDING
            if status == STATUS_PENDING:
                pending += 1
        rows.append({"index": pos, "day": day_text, "hour": hour,
                     "decision_cents": cents, "ledger_cents": spent, "status": status})
    ledger_total = sum(_as_int(b.get("amount_cents")) for b in blocks_for_day(blocks, day))
    summary = {
        "status": STATUS_PENDING if pending else STATUS_OK,
        "checked": checked,
        "pending": pending,
        "decision_total_cents": decision_total,
        "ledger_total_cents": ledger_total,
        "items": rows,
    }
    return rows, summary


def _date_rows(bridge, day):
    """约会：last_date_event（刚约完）+ date_prep（约会前的准备）。"""
    rows = []
    event = kv_value(bridge, "last_date_event", {})
    if isinstance(event, dict) and event:
        rows.append({
            "kind": "date", "label": KIND_LABELS["date"],
            "title": str(event.get("activity") or "约会"),
            "detail": "刚和老公共度",
            "day": str(event.get("day") or day or ""),
            "time": _clock(event.get("ended_ts")),
            "amount_cents": 0,
            "source": "kv:last_date_event",
        })
    prep = kv_value(bridge, "date_prep", {})
    if isinstance(prep, dict) and prep.get("activity"):
        rows.append({
            "kind": "date", "label": KIND_LABELS["date"],
            "title": str(prep.get("activity") or "约会"),
            "detail": "约会前的准备",
            "day": str(day or ""),
            "time": _clock(prep.get("start_ts")),
            "amount_cents": 0,
            "source": "kv:date_prep",
        })
    return rows


def _shopping_rows(bridge, day):
    """购物：last_purchase（她刚买的贵东西）。金额字段是分。"""
    purchase = kv_value(bridge, "last_purchase", {})
    if not isinstance(purchase, dict) or not purchase:
        return []
    bucket = _hour_bucket(purchase.get("ts"))
    return [{
        "kind": "shopping", "label": KIND_LABELS["shopping"],
        "title": str(purchase.get("name") or "东西"),
        "detail": "最近买的东西",
        "day": (bucket[0] if bucket else str(day or "")),
        "time": _clock(purchase.get("ts")),
        "amount_cents": _as_int(purchase.get("price")),
        "source": "kv:last_purchase",
    }]


def _sick_rows(bridge, day):
    """看病：sick_decision:<day>（带病上班 / 请病假）。"""
    decision = kv_value(bridge, "sick_decision:" + str(day or ""), {})
    if not isinstance(decision, dict) or not decision:
        return []
    action = str(decision.get("action") or "")
    title = "带病上班" if "work" in action else ("请病假" if "leave" in action else "身体不舒服")
    return [{
        "kind": "sick", "label": KIND_LABELS["sick"],
        "title": title,
        "detail": str(decision.get("reason") or ""),
        "day": str(day or ""),
        "time": "",
        "amount_cents": _as_int(decision.get("amount_cents") or decision.get("charged")),
        "source": "kv:sick_decision",
    }]


def _social_rows(repo, day):
    """社交：守护自己 events 审计表里的 social:* 事件（只读，不 trim、不写）。"""
    if repo is None:
        return []
    try:
        rows = list(repo.events(30) or [])
    except Exception:
        return []
    out = []
    wanted = str(day or "")
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or "")
        if kind not in SOCIAL_KINDS:
            continue
        row_day = str(row.get("day") or "")
        if wanted and row_day and row_day != wanted:
            continue
        payload = row.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        who = str(payload.get("npc") or "")
        verb = {"social:invite": "收到邀约", "social:accept": "答应了邀约",
                "social:reject": "拒绝了邀约", "social:expire": "邀约过期没回"}.get(kind, "社交事件")
        detail = (who + " " if who else "") + str(payload.get("text") or "").strip()
        # 卡片按「预约那天」重新锚定相对词（明晚/明天），别让今天的卡片还说「明晚」
        if kind == "social:accept" and row_day:
            try:
                target = _dt.date.fromisoformat(row_day)
                delta = (target - _dt.date.today()).days
                label = {0: "今天", 1: "明天", 2: "后天"}.get(delta) or row_day
                evening = {0: "今晚", 1: "明晚", 2: "后晚"}.get(delta) or (label + "晚上")
                for word in ("明晚", "今晚", "后晚"):
                    detail = detail.replace(word, evening)
                for word in ("明早", "今早", "明天", "今天", "后天"):
                    if word in detail:
                        detail = detail.replace(word, label + ("早上" if word.endswith("早") else ""))
            except Exception as exc:
                isolated("finance.social.card_retime", exc)
        out.append({
            "kind": "social", "label": KIND_LABELS["social"],
            "title": verb,
            "detail": detail,
            "day": row_day or wanted,
            "time": str(payload.get("start") or "") or _clock(row.get("created_at")),
            "amount_cents": _as_int(payload.get("amount_cents")),
            "source": "events:" + kind,
        })
    return out[:20]


def collect_extra(bridge, repo, day):
    """宵夜以外的自主决定（约会/社交/购物/看病）；任何一步失败只少一行，不抛。"""
    rows = []
    for builder in (
        lambda: _date_rows(bridge, day),
        lambda: _social_rows(repo, day),
        lambda: _shopping_rows(bridge, day),
        lambda: _sick_rows(bridge, day),
    ):
        try:
            rows.extend(builder())
        except Exception:      # cleanup: 单个来源失败不影响整页
            continue
    rows.sort(key=lambda r: (str(r.get("day") or ""), str(r.get("time") or "")), reverse=True)
    return rows


def extras_total_cents(rows):
    """其他决定里能对账的金额合计（分）。"""
    return sum(max(0, _as_int((r or {}).get("amount_cents"))) for r in rows or []
               if isinstance(r, dict))
