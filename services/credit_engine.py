# -*- coding: utf-8 -*-
"""资金紧张：低于总资产 5% 时的二选一（借款 / 求助）+ 等额本息还款。

硬规则：
- 一次资金紧张只能选一条路（bank 或 user），选完本轮关闭；
- 借款用真实年化 3.0%–18%，等额本息，3/6/12/24 期；
- 每期还款日自动扣款，余额不足转逾期并预警，**不自动再借新贷**；
- 总资产 = 现金 + 持仓市值 − 未还本金。
"""
from __future__ import annotations

import datetime as _dt
import time

try:
    from ..guard import isolated
except ImportError:  # 按顶层包/单文件加载时
    from guard import isolated

LOW_RATIO = 0.05
LOAN_RATE_RANGE = (0.03, 0.18)
ALLOWED_PERIODS = (3, 6, 12, 24)


def amortization(principal_cents, periods, annual_rate):
    """等额本息：返回 (每期还款, 总利息, [{seq, principal, interest, due_day}])。"""
    principal = int(principal_cents)
    periods = max(1, int(periods))
    monthly_rate = float(annual_rate) / 12.0
    if monthly_rate <= 0:
        payment = int(round(principal / periods))
    else:
        factor = (1 + monthly_rate) ** periods
        payment = int(round(principal * monthly_rate * factor / (factor - 1)))
    balance = principal
    rows = []
    today = _dt.date.today()
    for seq in range(1, periods + 1):
        interest = int(round(balance * monthly_rate))
        principal_part = payment - interest
        if seq == periods:
            principal_part = balance
            payment_last = principal_part + interest
        else:
            payment_last = payment
        balance -= principal_part
        year = today.year + (today.month - 1 + seq) // 12
        month = (today.month - 1 + seq) % 12 + 1
        day = min(today.day, 28)
        rows.append({
            "seq": seq, "principal": principal_part, "interest": interest,
            "payment": payment_last, "due_day": "%04d-%02d-%02d" % (year, month, day),
        })
    total_interest = sum(r["interest"] for r in rows)
    return (rows[0]["payment"] if rows else payment), total_interest, rows


class FinanceHelp:
    def __init__(self, plugin, repo, finance):
        self.plugin = plugin
        self.repo = repo
        self.finance = finance

    # ---------- 状态 ----------
    def total_assets_cents(self):
        cash = self.finance.balance_cents() or 0
        market_value = 0
        try:
            for row in self.repo.positions():
                market_value += int(row.get("last_cents") or 0) * float(row.get("qty") or 0)
        except Exception:
            market_value = 0
        debt = 0
        try:
            for loan in self.repo.loans():
                debt += int(loan.get("balance_cents") or 0)
        except Exception:
            debt = 0
        try:
            for item in self.repo.assets():
                market_value += int(item.get("value_cents") or 0)   # 房产/车辆计入总资产
        except Exception:
            pass
        return int(cash + market_value - debt)

    def status(self):
        cash = self.finance.balance_cents() or 0
        total = self.total_assets_cents()
        ratio = (cash / total) if total > 0 else 0.0
        choice = str(self.repo.meta_get("lowfund_choice") or "")
        return {"cash_cents": cash, "total_cents": total, "ratio": ratio,
                "low": total > 0 and ratio < LOW_RATIO, "choice": choice,
                "ratio_pct": round(ratio * 100, 2)}

    def prompt_text(self):
        state = self.status()
        if not state["low"]:
            return ""
        if state["choice"]:
            return ""
        return ("她现在可用余额只剩总资产的 %.1f%%，只能选一种方式：向银行借款"
                "（要还本付息，可选 3/6/12/24 期），或找你要钱。选哪个？" % state["ratio_pct"])

    def _episode_open(self):
        return not str(self.repo.meta_get("lowfund_choice") or "")

    def _close_episode(self, choice):
        self.repo.meta_set("lowfund_choice", str(choice))

    def maybe_reset(self):
        if self.status()["ratio"] >= LOW_RATIO and self.repo.meta_get("lowfund_choice"):
            self.repo.meta_set("lowfund_choice", "")

    # ---------- A. 借款 ----------
    async def take_loan(self, amount_cents=50000, periods=12, annual_rate=0.06, idem=""):
        if not self._episode_open():
            return {"ok": False, "message": "本轮资金紧张已经选过方式了"}
        amount = int(amount_cents)
        periods = int(periods)
        if amount <= 0:
            return {"ok": False, "message": "借款金额必须大于 0"}
        if periods not in ALLOWED_PERIODS:
            return {"ok": False, "message": "期数只能是 3 / 6 / 12 / 24"}
        rate = max(LOAN_RATE_RANGE[0], min(LOAN_RATE_RANGE[1], float(annual_rate)))
        payment, total_interest, rows = amortization(amount, periods, rate)
        idem = str(idem or ("loan:%s" % time.strftime("%Y%m%d%H%M%S")))
        loan_id = self.repo.add_loan(idem, "银行小额贷", amount, amount, rate, periods, payment)
        if not loan_id:
            return {"ok": False, "message": "这笔借款已经记过了"}
        for row in rows:
            self.repo.add_installment(loan_id, row["seq"], row["due_day"], row["principal"], row["interest"])
        await self.finance.borrow(amount, "银行小额贷", idem + ":wallet", months=periods)
        self._close_episode("loan")
        text = "借了 %.2f 元，年利率 %.2f%%，%d 期，每期约 %.2f 元，总利息 %.2f 元，首期还款日 %s。" % (
            amount / 100.0, rate * 100, periods, payment / 100.0, total_interest / 100.0,
            rows[0]["due_day"])
        await self.finance.remember_event("借款", amount, text, idem)
        return {"ok": True, "message": text, "monthly_cents": payment,
                "total_interest_cents": total_interest, "due_day": rows[0]["due_day"]}

    # ---------- B. 用户转账 ----------
    async def user_transfer(self, amount_cents, idem=""):
        if not self._episode_open():
            return {"ok": False, "message": "本轮资金紧张已经选过方式了"}
        idem = str(idem or ("transfer:%s" % time.strftime("%Y%m%d%H%M%S")))
        result = await self.finance.income(int(amount_cents), "用户转账", idem, salary=False)
        if not result.get("ok"):
            return {"ok": False, "message": "转账入账失败"}
        try:
            self.finance.mark_withdrawable(int(amount_cents), idem + ":w")
        except Exception:
            pass
        if self._episode_open():
            self._close_episode("user")
        return {"ok": True, "message": "收到转账 %.2f 元" % (int(amount_cents) / 100.0)}

    # ---------- 每期还款 ----------
    async def repay_tick(self, now=None):
        now = now or _dt.datetime.now()
        today = now.date().isoformat()
        item = self.repo.due_installment(today)
        if not item:
            return "none"
        payment = int(item["principal_cents"]) + int(item["interest_cents"])
        cash = self.finance.balance_cents() or 0
        if cash < payment:
            self.repo.mark_installment_overdue(item["id"])
            text = "%s 应还 %.2f 元，余额不够，已逾期。" % (today, payment / 100.0)
            await self.finance.remember_event("逾期", payment, text, "overdue:%s" % today)
            return "overdue"
        idem = "repay:%s:%s" % (item["loan_id"], item["seq"])
        result = await self.finance.expense(payment, "还贷", idem, necessary=True)
        if not result.get("ok"):
            return "fail"
        self.repo.pay_installment(item["id"], time.time())
        loan = None
        for row in self.repo.loans():
            if int(row["id"]) == int(item["loan_id"]):
                loan = row
                break
        if loan is not None:
            left = max(0, int(loan["balance_cents"]) - int(item["principal_cents"]))
            self.repo.update_loan(loan["id"], left, "done" if left <= 0 else "active")
        text = "%s 还贷 %.2f 元（本金 %.2f + 利息 %.2f），剩余本金见账本。" % (
            today, payment / 100.0, int(item["principal_cents"]) / 100.0, int(item["interest_cents"]) / 100.0)
        await self.finance.remember_event("还款", payment, text, idem)
        return "paid"
