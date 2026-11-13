# -*- coding: utf-8 -*-
"""面板读数：把「已经发生的账」翻译成面板字段（纯函数，方便单测）。

以前 `life` 块是写死的 0（房租 0 / 水电 0 / 本月支出 0），钱实际已经扣了，
面板却看不到。这里统一从两处聚合：

- settlements 表：房租 / 水 / 电 / 燃气 / 宽带 的当月结算记录；
- amo.wallet.ledger：本月真实流水（饮食、交通、人情等）。

净值仍然以 amo 钱包为准，本模块只做「解释」，不碰余额。
"""
from __future__ import annotations

import datetime as _dt

#: 算进「水电燃气」的结算类型
UTILITY_KINDS = ("water", "electric", "gas", "broadband")

#: 不算「本月支出」的流水原因（房租水电走 settled，工资是收入，交易/借贷另有卡片）
_EXCLUDE_PREFIXES = (
    "房租", "水费", "电费", "燃气费", "宽带", "物业", "取暖",
    "工资", "奖金", "红包",
    "买入", "卖出", "申购", "赎回", "基金",
    "贷款", "还款", "利息", "分期",
    "转账", "初始存款", "可取余额", "卖出资金", "用户转账",
)


def _cents(value) -> int:
    """payload 里可能是 '185303' 也可能是 'has_house'，解析不了就当 0。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _month_of(ts) -> str:
    try:
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m")
    except Exception:
        return ""


def _next_salary_date(today: _dt.date, salary_day: int) -> str:
    day = max(1, min(28, int(salary_day or 15)))
    if today.day < day:
        return today.replace(day=day).isoformat()
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return _dt.date(year, month, day).isoformat()


def life_block(settlements, ledger, salary_cents, today, rent_day=1, salary_day=15) -> dict:
    """生活账单：当月已结算的房租/水电 + 本月其他支出。金额单位是元（面板口径）。"""
    today = today or _dt.date.today()
    month = today.strftime("%Y-%m")
    rent = 0
    utilities = 0
    for row in settlements or []:
        if str(row.get("day") or "") != month:
            continue
        kind = str(row.get("kind") or "")
        amount = _cents(row.get("payload"))
        if kind == "rent":
            rent += amount
        elif kind in UTILITY_KINDS:
            utilities += amount
    expense = 0
    for row in ledger or []:
        delta = int(row.get("delta_cents") or 0)
        if delta >= 0:
            continue
        if str(row.get("account") or "cash") != "cash":
            continue
        if _month_of(row.get("ts")) != month:
            continue
        reason = str(row.get("reason") or "")
        if any(reason.startswith(prefix) for prefix in _EXCLUDE_PREFIXES):
            continue
        expense += -delta
    return {
        "salary": int(salary_cents or 0) / 100.0,
        "salary_day": int(salary_day or 15),
        "next_salary": _next_salary_date(today, salary_day),
        "rent": rent / 100.0,
        "rent_day": int(rent_day or 1),
        "utilities": utilities / 100.0,
        "month_expense": expense / 100.0,
        "month": month,
    }


def market_block(slots, limit=3, source="腾讯 qt.gtimg.cn", last="") -> dict:
    """行情/决策时点：今天已经跑过几个（9:30 / 11:30 / 14:50）。"""
    done = [str(s) for s in (slots or []) if str(s)]
    return {"checks": len(done), "limit": int(limit or 3), "last": last or "",
            "source": source or "", "slots": done}
