# -*- coding: utf-8 -*-
"""端到端模拟：发工资 → 买菜 → 买入股票 → T+1 卖出 → 交房租。

全程用临时 SQLite + 假钱包，不碰线上数据、不联网。
运行：python test_simulation.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import shutil
import tempfile

from models.db import Store
from models.ledger import FinanceRepo
from services.work_engine import FinanceSalary
from services.life_engine import FinanceLife
from services.trade_engine import Trading
from services.expense_engine import FinanceExpense


class FakeWallet:
    def __init__(self, cents=0):
        self.balance = cents

    def balance_cents(self):
        return self.balance

    def debit(self, cents, reason, idem):
        if cents > self.balance:
            return False
        self.balance -= int(cents)
        return True

    def credit(self, cents, reason, idem):
        self.balance += int(cents)
        return True


class FakeLink:
    """假 FinanceLink：钱包 + 日记 + 记忆 + 日程，全部内存记录。"""

    def __init__(self, balance=0):
        self.wallet = FakeWallet(balance)
        self.diaries = []
        self.memories = []
        self.schedules = []
        self.moods = []

    def balance_cents(self):
        return self.wallet.balance_cents()

    def withdrawable_cents(self):
        return self.wallet.balance_cents()

    def mood(self):
        return {"word": "平静", "score": 60}

    async def income(self, cents, reason, idem, salary=False):
        ok = self.wallet.credit(cents, reason, idem)
        return {"ok": ok, "reason": reason, "amount_cents": int(cents)}

    async def expense(self, cents, reason, idem, necessary=False, quiet=False):
        ok = self.wallet.debit(cents, reason, idem)
        self.diaries.append("支出 %s：%s" % (reason, cents / 100.0))
        return {"ok": ok, "reason": "" if ok else "余额不足", "amount_cents": int(cents)}

    async def trade_pnl(self, pnl_cents, ratio, symbol, idem):
        self.wallet.credit(pnl_cents, "交易盈亏 " + symbol, idem) if pnl_cents > 0 else self.wallet.debit(-pnl_cents, "交易盈亏 " + symbol, idem)
        return {"ok": True}

    async def record_trade(self, *a, **k):
        return True

    async def record_daily_pnl(self, *a, **k):
        return True

    async def borrow(self, cents, reason, idem, months=12):
        self.wallet.credit(cents, reason, idem)
        return {"ok": True}

    def nudge_mood(self, deltas, idem):
        self.moods.append(deltas)
        return True

    def small_diary(self, text):
        self.diaries.append(str(text))
        return True

    def long_diary(self, text):
        self.diaries.append(str(text))
        return True

    async def permanent_memory(self, text, idem):
        self.memories.append(str(text))
        return True

    async def long_memory(self, text, idem):
        self.memories.append(str(text))
        return True

    def add_schedule(self, day, activity, detail="", place="家", category="leave"):
        self.schedules.append({"day": day, "activity": activity, "detail": detail})
        return True

    async def low_balance(self, need_cents, idem):
        return {"ok": True}

    def ledger(self, limit=200):
        return []


class FakeRuntime:
    async def run_io(self, fn, *a, **k):
        return []


class FakePlugin:
    def __init__(self, link):
        self.config = {}
        self.finance = link
        self.finance_repo = None
        self.work_events = None
        self.finance_salary = None
        self.finance_life = None
        self.finance_property = None
        self.finance_help = None
        self.finance_daily = None
        self.city_override = "东莞"


def line(title=""):
    print("\n" + "=" * 62)
    if title:
        print(title)
        print("=" * 62)


def show(link, tag):
    print("  [余额] ¥%.2f   %s" % (link.wallet.balance_cents() / 100.0, tag))


async def main():
    tmp = tempfile.mkdtemp(prefix="watch-sim-")
    store = Store(tmp)
    repo = FinanceRepo(store)
    link = FakeLink(balance=200000)  # 启动 2000 元
    plugin = FakePlugin(link)
    salary = FinanceSalary(plugin, repo, link)
    life = FinanceLife(plugin, repo, link)
    trading = Trading(plugin, FakeRuntime(), repo, link)
    expense = FinanceExpense(plugin, repo, link)

    line("0. 初始")
    show(link, "启动资金")

    line("1. 发工资（2026-11-15，月薪 6800 + 全勤 300 + 加班 625.28）")
    period = "2026-11"
    salary.add_item(period, "bonus", "全勤奖", 30000)
    salary.add_item(period, "bonus", "周末加班", 62528)
    result = await salary.settle(dt.datetime(2026, 11, 15, 10, 0), period)
    print("  settle ->", {k: result.get(k) for k in ("ok", "net_cents", "paid_cents") if k in result})
    show(link, "发薪后")

    line("2. 买菜（2026-11-15 18:10，¥8.50）")
    bought = await expense.spend("food", "青菜", 850, day="2026-11-15", idem="sim:veg")
    print("  spend ->", bought.get("ok"), bought.get("reason") or "成功")
    show(link, "买菜后")

    line("3. 买入股票（2026-11-16 10:05，600519 × 100 股 @ ¥16.00）")
    buy = await trading.buy("600519", 100, 1600.0, "sim:buy")
    print("  buy ->", buy)
    show(link, "买入后")
    pos = repo.positions()
    print("  持仓 ->", [(p.get("code"), p.get("qty"), p.get("cost_cents")) for p in pos])

    line("4. T+1 卖出（2026-11-17 10:05，600519 × 100 @ ¥16.50）")
    sell = await trading.sell("600519", 100, 1650.0, "sim:sell")
    print("  sell ->", sell)
    show(link, "卖出后")
    print("  持仓 ->", repo.positions())

    line("5. 交房租（2026-12-01 09:00，东莞二线）")
    tick = await life.tick(dt.datetime(2026, 12, 1, 9, 0))
    print("  life.tick ->", tick if not isinstance(tick, dict) else {k: tick[k] for k in list(tick)[:6]})
    show(link, "交房租后")

    line("6. 汇总")
    print("  日记条数:", len(link.diaries))
    print("  记忆条数:", len(link.memories))
    print("  日程条数:", len(link.schedules))
    print("  最终余额: ¥%.2f" % (link.wallet.balance_cents() / 100.0))
    store.close() if hasattr(store, "close") else None
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
