# -*- coding: utf-8 -*-
"""逻辑层：交易、行情、生活（三餐/交通/水电/开销）、工资、房车、信贷、职场事件。

依赖方向：services → models/utils，绝不允许反着 import 上层。
"""
from __future__ import annotations

from .candidates import CandidatePools  # noqa: F401
from .consume_engine import FinanceConsume  # noqa: F401
from .credit_engine import FinanceHelp  # noqa: F401
from .daily_engine import FinanceDaily  # noqa: F401
from .expense_engine import FinanceExpense  # noqa: F401
from .life_engine import FinanceLife  # noqa: F401
from .market_api import fetch_quotes, parse_quotes  # noqa: F401
from .property_engine import FinanceProperty  # noqa: F401
from .trade_engine import Trading  # noqa: F401
from .work_engine import FinanceSalary  # noqa: F401
from .work_events import WorkEventEngine  # noqa: F401

__all__ = [
    "Trading", "fetch_quotes", "parse_quotes", "CandidatePools",
    "FinanceConsume", "FinanceDaily", "FinanceExpense", "FinanceLife",
    "FinanceProperty", "FinanceSalary", "FinanceHelp", "WorkEventEngine",
]
