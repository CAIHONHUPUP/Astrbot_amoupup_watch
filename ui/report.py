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

#: 本月消费口径（task-27）：唯一计算函数在 services/metrics.py（与能陪 metrics 同口径）。
#: 这里只做导入，不再本地按 reason 前缀排除——那是 473 vs 16016 两个面板不同源的根因。
try:
    from ..services.metrics import consumption_cents as _consumption_cents
except ImportError:
    from services.metrics import consumption_cents as _consumption_cents

#: 算进「水电燃气」的结算类型
UTILITY_KINDS = ("water", "electric", "gas", "broadband")


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


def life_block(settlements, ledger, salary_cents, today, rent_day=1, salary_day=15,
               consumption_cents=None) -> dict:
    """生活账单：当月已结算的房租/水电 + 本月消费。金额单位是元（面板口径）。

    consumption_cents：调用方已从能陪 MetricsAPI 取到本月消费（整数分）时直接传，
    保证「首页/生活卡」是同一个数；不传才用同一口径在本地重算。
    """
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
    if consumption_cents is None:
        expense = _consumption_cents(ledger or [], month)
    else:
        try:
            expense = int(consumption_cents)
        except (TypeError, ValueError):
            expense = _consumption_cents(ledger or [], month)
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
