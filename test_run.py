# -*- coding: utf-8 -*-
"""test_run.py：Mock amo/记忆，跑 工资→房租→买入→T+1卖出→生病→总资产。"""
import asyncio, datetime as dt, tempfile, shutil, types
from models.db import Store
from models.ledger import FinanceRepo
from services.work_engine import FinanceSalary
from services.life_engine import FinanceLife
from services.trade_engine import Trading
from services.medical_engine import MedicalEngine
import services.trade_engine as te


class Wallet:
    def __init__(self, c=0): self.c = c
    def balance_cents(self): return self.c
    def debit(self, n, r, i):
        if n > self.c: return False
        self.c -= n; return True
    def credit(self, n, r, i): self.c += n; return True
    def balance(self, account="cash"): return self.c


class Link:
    def __init__(self, c=0):
        self.w = Wallet(c); self.diaries = []; self.memories = []; self.moods = []
    def balance_cents(self): return self.w.balance_cents()
    def _amo(self):
        return types.SimpleNamespace(wallet=self.w, state=None)
    def withdrawable_cents(self): return self.w.balance_cents()
    def mood(self): return {"word": "平静"}
    async def income(self, c, r, i, salary=False): self.w.credit(c, r, i); return {"ok": True}
    async def expense(self, c, r, i, necessary=False, quiet=False): return {"ok": self.w.debit(c, r, i)}
    async def trade_pnl(self, *a, **k): return {"ok": True}
    async def record_trade(self, *a, **k): return True
    async def record_daily_pnl(self, *a, **k): return True
    async def borrow(self, *a, **k): return {"ok": True}
    def nudge_mood(self, d, i): self.moods.append(d)
    def small_diary(self, t): self.diaries.append(t)
    def long_diary(self, t): self.diaries.append(t)
    async def permanent_memory(self, t, i): self.memories.append(t)
    async def long_memory(self, t, i): self.memories.append(t)
    def add_schedule(self, *a, **k): pass
    async def low_balance(self, *a, **k): return {"ok": True}
    def ledger(self, limit=200): return []


QUOTE = {"code": "600519", "name": "贵州茅台", "price": 16.0,
         "prev_close": 15.8, "open": 15.9, "high": 16.2, "low": 15.7,
         "volume": 100000, "turnover": 1.6e9, "suspended": False}


class Runtime:
    async def run_io(self, *a, **k):
        return dict(QUOTE)
    async def run_db(self, fn, *a, **k):
        return fn(*a, **k)


class P:
    config = {}
    work_events = None


def freeze(when):
    class FixedDT(dt.datetime):
        @classmethod
        def now(cls, tz=None): return when
    class Shim:
        datetime = FixedDT; date = dt.date; timedelta = dt.timedelta; time = dt.time
    te._dt = Shim


async def main():
    tmp = tempfile.mkdtemp(prefix="run-"); store = Store(tmp); store.init()
    repo = FinanceRepo(store); link = Link(500000); plugin = P()
    plugin.finance_repo = repo
    sal, life = FinanceSalary(plugin, repo, link), FinanceLife(plugin, repo, link)
    trading, med = Trading(plugin, Runtime(), repo, link), MedicalEngine(plugin, repo, link)

    print("== 1. 发工资 6800 + 全勤 300")
    sal.add_item("2026-11", "bonus", "全勤奖", 30000)
    r = await sal.settle(dt.datetime(2026, 11, 15, 10, 0), "2026-11")
    net = r.get("summary", {}).get("net_cents")
    print("  实发:", net / 100, " 余额:", link.balance_cents() / 100,
          " 等于7100?", net == 710000)

    print("== 2. 扣房租（2026-12-01 二线）")
    await life.tick(dt.datetime(2026, 12, 1, 9, 0))
    print("  房租:", life.rent_cents("2026-12") / 100, " 余额:", link.balance_cents() / 100)

    print("== 3. 2026-11-16 10:00 买入 600519×100 @¥16")
    freeze(dt.datetime(2026, 11, 16, 10, 0))
    b = await trading.buy("600519", 100, 16.0, "run:buy")
    print("  ", b, " 余额:", link.balance_cents() / 100)
    print("  持仓:", [(p["code"], p["qty"], p["cost_cents"] / 100) for p in repo.positions()])
    store.write("UPDATE positions SET available=100 WHERE code='600519'")  # 模拟隔夜 T+1 解锁

    print("== 4. 2026-11-17 10:00 T+1 卖出 @¥16.5")
    freeze(dt.datetime(2026, 11, 17, 10, 0))
    s = await trading.sell("600519", 100, 16.5, "run:sell")
    print("  ", s, " 余额:", link.balance_cents() / 100, " 持仓:", repo.positions())

    print("== 5. 生病（轻症，报70%）")
    m = await med.handle_sick_event("light", "感冒", 1, "run:sick")
    print("  总费:", m["total_cents"] / 100, " 报销:", m["insurance_cents"] / 100,
          " 自付:", m["self_pay_cents"] / 100, " 交通:", m["hospital"]["fare"] / 100)
    print("  报销=70%?", m["insurance_cents"] == int(round(m["total_cents"] * 0.7)))
    print("  日记:", len(link.diaries), " 记忆:", len(link.memories))

    cash = link.balance_cents()
    print("== 6. 总资产：现金", cash / 100, " + 持仓 0 =", cash / 100)
    store.close(); shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
