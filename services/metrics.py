# -*- coding: utf-8 -*-
"""统一消费口径（MetricsAPI 消费端，task-27）。

work/audit/cross_plugin.md P0-3：守护首页“本月支出”读 watch.expenses（¥473，
只含日常小额），生活卡读 amo wallet ledger 却按 reason 前缀排除（¥16,016，
把定期存款 10,000 / 基金 500 也算成支出）——同一个指标两个数。

规则收敛成一份（与 forever/services/metrics.py 同一口径，
forever/tests/test_metrics_contract.py 用同一批流水做跨仓数值比对）：

- 本月消费：cash 负流水里排除
  * 资产类：存款/投资/基金/理财/转账/退款/账户调整（幂等前缀 save:/deposit:/
    invest:/transfer:/refund:/withdrawable:/bank:dep:）；
  * 账单类：房租/水费/电费/燃气费/宽带/物业/取暖（面板另有账单卡）；
  * 退款/冲正（幂等前缀 + “退款/退回/冲正/撤销/退还”）。
- 首选读取能陪 metrics_snapshot()（唯一计算函数）；对方是旧版时降级到
  本地同一口径重算，并把 source/degraded 一起返回，面板能看出是否走到新契约。
"""
from __future__ import annotations

import datetime as _dt

CONTRACT_NAME = "amo-metrics"
CONTRACT_VERSION = 1
SOURCE_CONTRACT = "amo.metrics"
SOURCE_LOCAL = "local_ledger"

ASSET_CATEGORIES = frozenset(("存款", "投资", "转账", "退款", "理财", "基金", "股票",
                              "账户调整"))
BILL_CATEGORIES = frozenset(("房租", "水电", "物业", "取暖", "宽带"))
BILL_REASON_PREFIXES = ("房租", "水费", "电费", "燃气费", "宽带", "物业", "取暖")
ASSET_REASON_PREFIXES = ("存款", "投资", "转账", "退款", "理财", "基金", "股票",
                         "买入", "卖出", "申购", "赎回", "贷款", "还款", "利息",
                         "分期", "工资", "奖金", "红包", "初始存款", "可取余额",
                         "卖出资金", "用户转账")
ASSET_IDEM_PREFIXES = ("save:", "deposit:", "invest:", "transfer:", "refund:",
                       "cleanup:refund:", "withdrawable:", "bank:dep:")
REFUND_WORDS = ("退款", "退回", "冲正", "撤销", "退还")

KIND_CONSUMPTION = "consumption"
KIND_BILL = "bill"
KIND_ASSET = "asset"
KIND_REFUND = "refund"
KIND_INCOME = "income"


def month_of(now=None):
    if isinstance(now, _dt.datetime):
        return now.strftime("%Y-%m")
    try:
        return _dt.datetime.fromtimestamp(float(now)).strftime("%Y-%m")
    except (TypeError, ValueError):
        return _dt.date.today().strftime("%Y-%m")


def _text(row, key):
    return str((row or {}).get(key) or "")


def _delta(row):
    try:
        return int((row or {}).get("delta_cents") or 0)
    except (TypeError, ValueError):
        return 0


def _month_of_ts(ts):
    try:
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m")
    except (TypeError, ValueError, OSError):
        return ""


def classify_row(row):
    """一行 ledger -> income/refund/bill/asset/consumption（与能陪同口径）。"""
    delta = _delta(row)
    reason = _text(row, "reason")
    category = _text(row, "category")
    idem = _text(row, "idem")
    if delta > 0:
        return KIND_REFUND if any(word in reason for word in REFUND_WORDS) else KIND_INCOME
    if idem.startswith("refund:") or idem.startswith("cleanup:refund:"):
        return KIND_REFUND
    if any(word in reason for word in REFUND_WORDS):
        return KIND_REFUND
    if category in ASSET_CATEGORIES:
        return KIND_ASSET
    if category in BILL_CATEGORIES:
        return KIND_BILL
    if idem.startswith(ASSET_IDEM_PREFIXES):
        return KIND_ASSET
    if any(reason.startswith(prefix) for prefix in BILL_REASON_PREFIXES):
        return KIND_BILL
    if any(reason.startswith(prefix) for prefix in ASSET_REASON_PREFIXES):
        return KIND_ASSET
    return KIND_CONSUMPTION


def consumption_cents(ledger, month="", now=None):
    """本月消费（整数分）：唯一计算函数（纯函数，面板/测试共用）。"""
    target = str(month or "").strip() or month_of(now)
    total = 0
    for row in ledger or []:
        if not isinstance(row, dict):
            continue
        if _text(row, "account") not in ("cash", ""):
            continue
        ts = _month_of_ts(row.get("ts"))
        if ts and target and ts != target:
            continue
        if classify_row(row) == KIND_CONSUMPTION:
            total += abs(_delta(row))
    return total


def _amo(plugin):
    finance = getattr(plugin, "finance", None)
    getter = getattr(finance, "_amo", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    return None


def _ledger(plugin, limit=500):
    finance = getattr(plugin, "finance", None)
    getter = getattr(finance, "ledger", None)
    if callable(getter):
        try:
            rows = getter(int(limit))
            if isinstance(rows, (list, tuple)):
                return [row for row in rows if isinstance(row, dict)]
        except Exception:  # cleanup: 首选账本读不到时降级到 amo 钱包账本
            pass
    amo = _amo(plugin)
    wallet = getattr(amo, "wallet", None) if amo is not None else None
    getter = getattr(wallet, "ledger", None)
    if callable(getter):
        try:
            return [row for row in (getter(int(limit)) or []) if isinstance(row, dict)]
        except Exception:
            return []
    return []


def month_consumption(plugin, month="", now=None):
    """本月消费：首选能陪 metrics_snapshot()（单一计算函数），旧版降级本地重算。

    返回 {cents, source, month, degraded}；任何失败都不抛（面板/推送不能因它炸）。
    """
    target = str(month or "").strip() or month_of(now)
    out = {"cents": 0, "source": SOURCE_LOCAL, "month": target, "degraded": ""}
    amo = _amo(plugin)
    getter = getattr(amo, "metrics_snapshot", None) if amo is not None else None
    if callable(getter):
        try:
            data = getter()
        except Exception as exc:
            out["degraded"] = "%s: %s" % (type(exc).__name__, exc)
            data = None
        if isinstance(data, dict) and str(data.get("contract") or "") == CONTRACT_NAME:
            wallet = data.get("wallet") or {}
            wallet_month = str(wallet.get("month") or "")
            if not wallet_month or wallet_month == target:
                out["cents"] = int(wallet.get("month_consumption_cents") or 0)
                out["source"] = SOURCE_CONTRACT
                out["degraded"] = ""
                return out
            out["degraded"] = "contract month=%s != %s" % (wallet_month, target)
        elif data is not None:
            out["degraded"] = "contract mismatch"
    ledger = _ledger(plugin)
    out["cents"] = consumption_cents(ledger, target, now)
    return out


def contract_descriptor():
    return {
        "contract": CONTRACT_NAME,
        "version": CONTRACT_VERSION,
        "role": "consumer",
        "preferred_source": SOURCE_CONTRACT,
        "fallback_source": SOURCE_LOCAL,
        "rules": {
            "asset_categories": sorted(ASSET_CATEGORIES),
            "bill_categories": sorted(BILL_CATEGORIES),
            "refund_words": list(REFUND_WORDS),
        },
    }
