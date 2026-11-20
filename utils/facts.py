# -*- coding: utf-8 -*-
"""账本事实（2026-11-15）：只拼客观数据，不带任何情绪词。

流水记忆/日程明细一律用这里的格式：
    2026-11-15 10:00 发工资：基本 6800.00 元，奖金 825.28 元，扣款 412.64 元，实发 7212.64 元，余额 25918.14 元
    2026-11-15 09:18 支出 20.00 元（餐饮/一碗热汤面），余额 25816.19 元
情绪由 LLM 在聊天/日记里按真实心情自己说，代码层不再写死。
"""
from __future__ import annotations

import time

try:  # Phase 15：账本事实的金额格式只有一个实现
    from .money import fmt_amount_grouped as money
except ImportError:  # 顶层模块模式
    from money import fmt_amount_grouped as money


def stamp(when_ts=None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(when_ts) if when_ts else time.localtime())


def ledger_fact(kind, amount_cents, balance_cents, item="", when_ts=None) -> str:
    head = "%s %s %s 元" % (stamp(when_ts), kind, money(amount_cents))
    detail = ("（" + str(item).strip() + "）") if str(item or "").strip() else ""
    return "%s%s，余额 %s 元" % (head, detail, money(balance_cents))


def spend_line(amount_cents, item="") -> str:
    """一句人话的消费（进小日记/长期记忆用）。

    不写时间戳、不写余额：余额会过期，账本明细在 wallet_ledger 里，不往记忆里抄。
    """
    item = str(item or "").strip()
    head = (item + "，") if item else ""
    return "%s花了 %s 元。" % (head, money(amount_cents))


def salary_fact(base_cents, bonus_cents, deduction_cents, net_cents, balance_cents,
                when_ts=None) -> str:
    parts = ["基本 " + money(base_cents) + " 元"]
    if int(bonus_cents or 0):
        parts.append("奖金 " + money(bonus_cents) + " 元")
    if int(deduction_cents or 0):
        parts.append("扣款 " + money(deduction_cents) + " 元")
    parts.append("实发 " + money(net_cents) + " 元")
    parts.append("余额 " + money(balance_cents) + " 元")
    return "%s 发工资：%s" % (stamp(when_ts), "，".join(parts))


def salary_zero_fact(base_cents, net_cents, when_ts=None) -> str:
    return "%s 工资结算：基本 %s 元，扣款后实发 %s 元，本月无入账" % (
        stamp(when_ts), money(base_cents), money(net_cents))
