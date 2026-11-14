# -*- coding: utf-8 -*-
import asyncio, datetime as dt
from services.bank_engine import BankEngine

class Repo:
    def __init__(self): self.meta, self.tasks = {}, {}
    def meta_get(self, k, d=None): return self.meta.get(k, d)
    def meta_set(self, k, v): self.meta[k] = v
    def task_done(self, k): return k in self.tasks
    def mark_task(self, k, n=""): self.tasks[k] = 1
class Fin:
    def __init__(self, c=1000000): self.c, self.diaries, self.moods = c, [], []
    def balance_cents(self): return self.c
    async def expense(self, n, r, i, necessary=False):
        if n > self.c: return {"ok": False}
        self.c -= n; return {"ok": True}
    async def income(self, n, r, i, **k): self.c += n; return {"ok": True}
    def nudge_mood(self, d, i): self.moods.append(d)
    def small_diary(self, t): self.diaries.append(t)
    def long_diary(self, t): self.diaries.append(t)
class P: config = {}

def test_deposit_withdraw_and_penalty():
    repo, fin = Repo(), Fin()
    b = BankEngine(P(), repo, fin)
    assert asyncio.run(b.deposit(500000, 12, dt.datetime(2026, 1, 1)))["ok"]
    assert fin.c == 500000 and b.fixed_cents() == 500000
    out = asyncio.run(b.withdraw(500000, 0, dt.datetime(2026, 2, 1)))
    assert out["ok"] and out["penalty"] is True
    assert fin.c > 500000  # 本金 + 活期微息

def test_matured_fixed_pays_principal_and_interest_once():
    repo, fin = Repo(), Fin()
    b = BankEngine(P(), repo, fin)
    asyncio.run(b.deposit(1000000, 12, dt.datetime(2025, 11, 16)))
    # 未到期：不动
    assert asyncio.run(b.process_matured_fixed_deposits(dt.datetime(2026, 5, 1))) == []
    assert b.current_cents() == 0 and b.fixed_cents() == 1000000
    # 到期：本金 + 12 个月利息（1.7%）→ 活期
    paid = asyncio.run(b.process_matured_fixed_deposits(dt.datetime(2026, 11, 16)))
    assert len(paid) == 1 and paid[0]["interest"] == 17000
    assert b.current_cents() == 1017000 and b.fixed_cents() == 0
    # 幂等：再跑一次不重复入账
    assert asyncio.run(b.process_matured_fixed_deposits(dt.datetime(2026, 11, 17))) == []
    assert b.current_cents() == 1017000
    assert any("定期" in d for d in fin.diaries)

def test_daily_tick_guards_before_0005_and_by_date():
    repo, fin = Repo(), Fin()
    b = BankEngine(P(), repo, fin)
    asyncio.run(b.deposit(500000, 3, dt.datetime(2026, 1, 1)))
    assert asyncio.run(b.daily_tick(dt.datetime(2026, 4, 1, 0, 1)))["matured"] == []
    out = asyncio.run(b.daily_tick(dt.datetime(2026, 4, 1, 0, 6)))
    assert len(out["matured"]) == 1 and b.current_cents() == 500000 + 500000 * 13 // 1000 // 4
    again = asyncio.run(b.daily_tick(dt.datetime(2026, 4, 1, 9, 0)))
    assert again["matured"] == [] and b.current_cents() == 500000 + 500000 * 13 // 1000 // 4

def test_double_scan_never_pays_twice():
    """Bug1：并发/重启重复巡检也只入账一次。"""
    repo, fin = Repo(), Fin()
    b = BankEngine(P(), repo, fin)
    asyncio.run(b.deposit(1000000, 12, dt.datetime(2025, 11, 16)))
    now = dt.datetime(2026, 11, 17, 0, 6)

    async def run():
        return await asyncio.gather(b.process_matured_fixed_deposits(now),
                                    b.process_matured_fixed_deposits(now))
    first, second = asyncio.run(run())
    assert len(first) + len(second) == 1
    assert b.current_cents() == 1017000  # 只加一次

def test_interest_is_integer_cents_no_float_drift():
    """Bug2：利息按整数分算（基点×月数//120000），不经过 float 乘法。"""
    from services.bank_engine import _interest_cents
    # 3333.33 元 × 1.7% × 12 月 = 56.66666 元 → 5666 分（截断，不四舍五入）
    assert _interest_cents(333333, 0.017, 12) == 5666
    assert isinstance(_interest_cents(333333, 0.023, 36), int)
    # 1 年期整存：整数版与手算完全一致
    assert _interest_cents(1000000, 0.017, 12) == 17000

def test_missed_days_are_caught_up_after_restart():
    """Bug3：漏跑几天，重启后把积压的到期单一次结清。"""
    repo, fin = Repo(), Fin()
    b = BankEngine(P(), repo, fin)
    asyncio.run(b.deposit(500000, 3, dt.datetime(2026, 1, 1)))
    # 模拟宕机：4 月 1 日到期，直到 4 月 10 日才重启
    paid = asyncio.run(b.process_matured_fixed_deposits(dt.datetime(2026, 4, 10, 9, 0)))
    assert len(paid) == 1 and b.fixed_cents() == 0

def test_maturity_uses_local_midnight_and_legacy_rows():
    """Bug4：到期时间统一转成本地 epoch 秒；老数据只有 end 日期也能结。"""
    from services.bank_engine import _midnight_ts, _row_mature_ts
    import datetime as dt2
    day = dt2.date(2026, 11, 16)
    assert _row_mature_ts({"mature_at": 1234567890}) == 1234567890
    assert _row_mature_ts({"end": "2026-11-16"}) == _midnight_ts(day)
    assert _row_mature_ts({}) is None
    # 本地零点前一秒未到期、零点整到期
    assert _row_mature_ts({"end": "2026-11-16"}) <= _midnight_ts(day)

def test_emergency_uses_current_first():
    repo, fin = Repo(), Fin(300000)
    b = BankEngine(P(), repo, fin)
    asyncio.run(b.deposit(200000, 0))
    out = asyncio.run(b.emergency_liquidate(150000))
    assert out["ok"] and out["from"] == "current"
