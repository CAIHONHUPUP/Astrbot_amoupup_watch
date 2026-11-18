# -*- coding: utf-8 -*-
"""账本审计（只读）：核对「流水 vs 余额」「重复幂等键」「金额精度」。

Phase 15 背景：一次开销/一笔工资现在会经过 engines → FinanceLink → amo.wallet 好几层，
任何一层把金额、幂等键或期数写错，表现出来都只是「钱好像不对」，很难定位。这里给一个
**只读**的核对入口，规则全部是「同一笔钱只能记一次」「分必须是整数」这类硬约束：

- 不扣钱、不写库、不修数据（发现问题只返回问题列表，绝不自动补齐）；
- 面板、QA 工具、测试都能调，默认看最近 7 天；
- 只读 amo.wallet.ledger() 和 balance_cents()，不新增跨插件接口。

规则：
1. missing_idem      流水没有幂等键（无法去重，重试必重复扣）；
2. duplicate_idem    同一个 idem 入账两次（内容还相同）；
3. idem_conflict     同一个 idem 记了两笔不同的钱（后一笔会被钱包丢掉 → 丢写）；
4. precision         金额不是整数「分」，或不是 int（浮点漂移）；
5. balance_gap       流水合计 ≠ 当前余额（只在这一页就是全量历史时才算，避免误报）；
6. double_charge     同一期同一项同额、幂等键却不同（两个记账入口各拼一套键 → 重复扣）。
"""
from __future__ import annotations
from typing import Any

import datetime as _dt
import time

try:
    from ..guard import isolated
except ImportError:  # 离线测试 / 顶层加载
    from guard import isolated

try:
    from ..utils.money import idem_ok
except ImportError:  # 顶层加载
    from utils.money import idem_ok

#: 默认审计窗口（天）
DEFAULT_DAYS = 7
#: 一次最多读多少条流水；读满就说明「这可能不是全量历史」，余额对账跳过
LEDGER_LIMIT = 2000
#: 每月只会发生一次的固定支出/收入：同名同额却出现两笔不同幂等键 = 重复扣款嫌疑
RECURRING_WORDS = frozenset((
    "房租", "房贷", "水费", "电费", "燃气费", "宽带", "物业费", "养车",
    "工资", "滞纳金", "违约金",
))


def _issue(rule, detail, row=None):
    item = {"rule": str(rule), "detail": str(detail)}
    if isinstance(row, dict):
        for key in ("idem", "reason", "delta_cents", "ts", "account"):
            if key in row:
                item[key] = row[key]
    return item


def _period_of(row):
    try:
        ts = float(row.get("ts") or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m")


def _is_integer_cents(value):
    """金额必须是「分」的整数：bool / float / 字符串都算精度问题。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return int(value) == value


def _scope(rows, days):
    """窗口内的流水（按 ts 倒序读回来，这里只筛不排序）。"""
    try:
        start = time.time() - max(1, int(days)) * 86400.0
    except (TypeError, ValueError):
        start = time.time() - DEFAULT_DAYS * 86400.0
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            if float(row.get("ts") or 0) >= start:
                out.append(row)
        except (TypeError, ValueError):
            out.append(row)
    return out


def _check_idem(window):
    """规则 1-3：幂等键缺失 / 重复 / 冲突。"""
    issues = []
    seen: dict[Any, Any] = {}
    for row in window:
        key = str(row.get("idem") or "")
        if not idem_ok(key):
            issues.append(_issue("missing_idem", "流水没有可用的幂等键", row))
            continue
        mark = (str(row.get("reason") or ""),
                int(row.get("delta_cents") or 0),
                str(row.get("account") or "cash"))
        previous = seen.get(key)
        if previous is None:
            seen[key] = mark
            continue
        if previous == mark:
            issues.append(_issue("duplicate_idem", "同一幂等键入账两次", row))
        else:
            issues.append(_issue("idem_conflict", "同一幂等键对应两笔不同的钱（后一笔会丢写）", row))
    return issues


def _check_precision(window):
    """规则 4：金额必须是整数分。"""
    issues = []
    for row in window:
        value = row.get("delta_cents")
        if _is_integer_cents(value):
            continue
        issues.append(_issue("precision", "金额不是整数分：%r" % (value,), row))
    return issues


def _check_balance(rows, window, finance, complete):
    """规则 5：现金流水合计 vs 当前余额。只在拿到全量历史时才算。"""
    if not complete:
        return []
    total = 0
    for row in rows:
        if not _is_integer_cents(row.get("delta_cents")):
            return []          # 有精度问题就交给规则 4，别再叠一个假缺口
        if str(row.get("account") or "cash") != "cash":
            continue
        total += int(row.get("delta_cents") or 0)
    reader = getattr(finance, "balance_cents", None)
    if not callable(reader):
        return []
    try:
        actual = reader()
    except Exception as exc:
        isolated("ledger_audit.balance", exc)
        return []
    if actual is None:
        return []
    gap = int(actual) - int(total)
    if gap == 0:
        return []
    return [_issue("balance_gap", "流水合计 %d 分，余额 %d 分，差 %d 分" % (total, int(actual), gap))]


def _check_double_charge(window):
    """规则 6：同一期同一项同额、幂等键不同 → 两个入口各扣了一遍。"""
    buckets: dict[Any, Any] = {}
    for row in window:
        reason = str(row.get("reason") or "")
        if reason not in RECURRING_WORDS:
            continue
        if not _is_integer_cents(row.get("delta_cents")):
            continue
        period = _period_of(row)
        if not period:
            continue
        mark = (period, reason, abs(int(row.get("delta_cents") or 0)))
        buckets.setdefault(mark, set()).add(str(row.get("idem") or ""))
    issues = []
    for (period, reason, amount), keys in buckets.items():
        if len(keys) <= 1:
            continue
        issues.append(_issue("double_charge",
                             "%s %s %d 分出现了 %d 个不同幂等键：%s"
                             % (period, reason, amount, len(keys),
                                "、".join(sorted(keys)[:4]))))
    return issues


def audit_ledger(plugin, days=DEFAULT_DAYS):
    """只读核对账本，返回问题列表；**不修数据、不动余额、不写文件**。

    plugin 只需要有一个 finance（FinanceLink）；没有账本/读失败时返回一条
    no_ledger/ledger_error，ok=False，绝不抛异常打断主链。
    """
    try:
        span = max(1, int(days if days is not None else DEFAULT_DAYS))
    except (TypeError, ValueError):
        span = DEFAULT_DAYS
    finance = getattr(plugin, "finance", None)
    reader = getattr(finance, "ledger", None)
    if not callable(reader):
        return {"ok": False, "days": span, "entries": 0, "issues": [
            _issue("no_ledger", "账本未就绪：读不到 finance.ledger")]}
    try:
        rows = [row for row in (reader(LEDGER_LIMIT) or []) if isinstance(row, dict)]
    except Exception as exc:
        isolated("ledger_audit.read", exc)
        return {"ok": False, "days": span, "entries": 0, "issues": [
            _issue("ledger_error", "读流水失败：%r" % (exc,))]}
    complete = len(rows) < LEDGER_LIMIT
    window = _scope(rows, span)
    issues = []
    issues.extend(_check_idem(window))
    issues.extend(_check_precision(window))
    issues.extend(_check_balance(rows, window, finance, complete))
    issues.extend(_check_double_charge(window))
    return {"ok": not issues, "days": span, "entries": len(window),
            "total_entries": len(rows), "complete": complete, "issues": issues}


def audit_summary(report):
    """一行摘要（日志/面板用），不参与判定。"""
    data = report if isinstance(report, dict) else {}
    if data.get("ok"):
        return "账本没问题：最近 %s 天 %d 笔流水" % (data.get("days"), int(data.get("entries") or 0))
    return "账本有 %d 个问题：%s" % (
        len(data.get("issues") or []),
        "、".join(sorted({str(item.get("rule")) for item in data.get("issues") or []})),
    )


__all__ = ["DEFAULT_DAYS", "LEDGER_LIMIT", "RECURRING_WORDS", "audit_ledger", "audit_summary"]
