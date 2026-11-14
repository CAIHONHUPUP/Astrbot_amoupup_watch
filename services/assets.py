# -*- coding: utf-8 -*-
"""总资产核算（唯一口径）：
总资产 = 可用现金 + 股票市值 + 活期 + 定期 + 固定资产 − 总负债
"""
from __future__ import annotations


def _call(obj, *names, default=0):
    for name in names:
        fn = getattr(obj, name, None)
        if fn is None:
            continue
        try:
            value = fn() if callable(fn) else fn
            if value is not None:
                return int(value)
        except Exception:
            continue
    return default


def bank_cents(plugin):
    """活期 + 定期（没有银行引擎时按 0）。"""
    bank = getattr(plugin, "bank", None)
    if bank is None:
        return 0
    try:
        return int(bank.current_cents()) + int(bank.fixed_cents())
    except Exception:
        return 0


def total_assets(plugin, cash_cents=None, market_cents=None,
                 fixed_cents=None, debt_cents=None):
    repo = getattr(plugin, "finance_repo", None)
    if cash_cents is None:
        cash_cents = _call(getattr(plugin, "finance", None), "balance_cents") if hasattr(plugin, "finance") else _call(repo, "cash_cents")
    if market_cents is None:
        market_cents = _call(repo, "market_value_cents", "position_value_cents")
    if fixed_cents is None:
        fixed_cents = _call(repo, "assets_value_cents")
    if debt_cents is None:
        debt_cents = _call(repo, "debt_cents", "loan_principal_cents")
    return (int(cash_cents or 0) + int(market_cents or 0) + bank_cents(plugin)
            + int(fixed_cents or 0) - int(debt_cents or 0))


def breakdown(plugin, **kw):
    return {"cash": int(kw.get("cash_cents") or 0), "market": int(kw.get("market_cents") or 0),
            "bank": bank_cents(plugin), "fixed": int(kw.get("fixed_cents") or 0),
            "debt": int(kw.get("debt_cents") or 0),
            "total": total_assets(plugin, **kw)}
