# -*- coding: utf-8 -*-
"""Phase 15 共享测试桩：FakeRepo / FakeClock / FakeAmo / FakeFinance / FakePlugin / 临时库。

改造前 62 个测试文件各写一份 Fake* 类，同名不同义：有的 expense 返回 ok=True 但不扣钱，
有的按 idem 去重、有的不去重，于是「重复扣款」这种 bug 在不同文件里表现不一样。
这里给一份**契约明确**的最小实现：

- FakeRepo：meta 字典 + settlements 幂等表（settled / mark_settled 与真实仓储同签名）；
- FakeClock：可拨动的时钟，方便「跨 0 点」「发薪日」这类用例；
- FakeAmo：能陪面最小桩（wallet / state / db / push_to_owner），够 FinanceLink 用；
- FakeFinance：**像钱包一样按 idem 去重**的 FinanceLink 桩，并把每一笔写进 ledger 行，
  这样 ledger_audit 可以直接拿它验证「重复 idem / 精度 / 余额缺口」；
- FakePlugin：把上面几个拼成插件对象，模块用关键字参数挂；
- temp_store / temp_repo：tmp_path 下的真 SQLite（跑完自动随 tmp_path 清理，不碰 plugin_data）。

只依赖标准库与插件自身模块；导入期不改全局状态。
"""
from __future__ import annotations

import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
for _p in (ROOT, TESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Phase 18：temp_dir / temp_store / LogCapture / PII 断言统一从 support 复用，本地只留特化。
from support import *  # noqa: E402,F401,F403
from support import SUPPORT_VERSION  # noqa: E402,F401

#: 默认冻结时刻：2026-11-16 15:00（工作日下午，银行/基金都开着）
DEFAULT_NOW = dt.datetime(2026, 11, 16, 15, 0, 0)


class FakeClock:
    """可拨动的时钟：call() 或 now_ts() 拿当前值，advance() 往前走。"""

    def __init__(self, now=None):
        self.now = now or DEFAULT_NOW

    def __call__(self):
        return self.now

    def now_ts(self):
        return float(self.now.timestamp())

    def day(self):
        return self.now.date()

    def set(self, now):
        self.now = now
        return self.now

    def advance(self, seconds=0, minutes=0, hours=0, days=0):
        self.now = self.now + dt.timedelta(seconds=seconds, minutes=minutes,
                                           hours=hours, days=days)
        return self.now


class FakeRepo:
    """meta / settlements 字典桩：和 models.ledger.FinanceRepo 同名同参。"""

    def __init__(self, meta=None, settlements=None):
        self.data = dict(meta or {})
        self.meta = self.data            # 旧测试有的摸 .meta、有的摸 .data：同一个 dict
        self.settlements = dict(settlements or {})
        self.writes = []

    # ---- meta ----
    def meta_get(self, key, default=None):
        return self.data.get(key, default)

    def meta_set(self, key, value):
        self.writes.append((key, value))
        self.data[key] = value

    # ---- settlements（按期 + 按项的幂等表）----
    def settled(self, period, kind):
        return (str(period), str(kind)) in self.settlements

    def mark_settled(self, period, kind, payload="{}"):
        self.settlements[(str(period), str(kind))] = str(payload)
        return True


class FakeWallet:
    """钱包桩：按 idem 去重，重复写入返回 False（和 amo 语义一致）。"""

    def __init__(self, cash=500000, withdrawable=None):
        self.cash = int(cash)
        self.withdrawable = int(cash if withdrawable is None else withdrawable)
        self.idems = set()
        self.rows = []

    def balance(self, account="cash"):
        return self.withdrawable if str(account) == "withdrawable" else self.cash

    def _write(self, account, delta, reason, idem):
        key = str(idem or "")
        if not key or key in self.idems:
            return False
        self.idems.add(key)
        self.rows.append({"idem": key, "account": str(account), "delta_cents": int(delta),
                          "reason": str(reason or ""), "ts": float(DEFAULT_NOW.timestamp())})
        if str(account) == "withdrawable":
            self.withdrawable += int(delta)
        else:
            self.cash += int(delta)
        return True

    def credit(self, amount, reason="", idem="", account="cash", **kwargs):
        return self._write(account, int(amount), reason, idem)

    def debit(self, amount, reason="", idem="", account="cash", **kwargs):
        if int(amount) <= 0 or self.balance(account) < int(amount):
            return False
        return self._write(account, -int(amount), reason, idem)

    def ledger(self, limit=200):
        return list(reversed(self.rows))[:int(limit)]


class FakeAmo:
    """能陪面最小桩：钱包 + 心情 + 推送记录。"""

    def __init__(self, cash=500000):
        self.wallet = FakeWallet(cash)
        self.pushed = []
        self.closet = []

    async def push_to_owner(self, text, *args, **kwargs):
        self.pushed.append(str(text))
        return True

    def add_closet_item(self, item):
        self.closet.append(dict(item or {}))
        return {"ok": True, "item": item}


class FakeFinance:
    """FinanceLink 桩：按 idem 去重 + 完整记录（ledger 可直接被 ledger_audit 读）。"""

    def __init__(self, cash=500000, amo=None, ok=True):
        self.cash = int(cash)
        self.ok = bool(ok)
        self.amo = amo if amo is not None else FakeAmo(cash)
        self.expenses = []
        self.incomes = []
        self.transfers = []
        self.memories = []
        self.diaries = []
        self.moods = []
        self.schedules = []
        self.events = []
        self.idems = set()
        self.ledger_rows = []
        self.clock = FakeClock()
        if self.cash:      # 和生产一致：初始存款也写一行流水，账本合计才对得上余额
            self.ledger_rows.append({
                "idem": "initial", "account": "cash", "delta_cents": int(self.cash),
                "reason": "初始存款", "ts": float(self.clock.now_ts()),
            })
            self.idems.add("initial")

    # ---- 只读 ----
    def _amo(self):
        return self.amo

    def balance_cents(self):
        return self.cash

    def withdrawable_cents(self):
        return self.cash

    def ledger(self, limit=200):
        return list(reversed(self.ledger_rows))[:int(limit)]

    def mood(self):
        return {"score": 60.0, "word": "平静", "dominant": "平静"}

    # ---- 写（唯一入口）----
    def _move(self, delta, reason, idem, account="cash"):
        key = str(idem or "")
        if not key:
            return {"ok": False, "reason": "missing_idem"}
        if key in self.idems:
            return {"ok": False, "reason": "duplicate"}
        if int(delta) < 0 and self.cash < abs(int(delta)):
            return {"ok": False, "reason": "余额不足"}
        self.idems.add(key)
        self.cash += int(delta)
        self.ledger_rows.append({
            "idem": key, "account": str(account), "delta_cents": int(delta),
            "reason": str(reason or ""), "ts": float(self.clock.now_ts()),
        })
        return {"ok": True}

    async def expense(self, amount, reason="", idem="", necessary=False, quiet=False, **kwargs):
        out = self._move(-abs(int(amount)), reason, idem)
        if out.get("ok"):
            self.expenses.append((int(amount), str(reason), str(idem)))
        return out

    async def income(self, amount, reason="", idem="", salary=False, **kwargs):
        out = self._move(abs(int(amount)), reason, idem)
        if out.get("ok"):
            self.incomes.append((int(amount), str(reason), str(idem)))
        return out

    async def transfer_in(self, amount, reason="用户转账", idem="", **kwargs):
        out = self._move(abs(int(amount)), reason, idem)
        if out.get("ok"):
            self.transfers.append((int(amount), str(reason), str(idem)))
        return out

    # ---- 副作用桩（只记录）----
    def nudge_mood(self, deltas, idem="", **kwargs):
        self.moods.append((dict(deltas or {}), str(idem)))
        return True

    def small_diary(self, text):
        self.diaries.append(str(text))
        return True

    def long_diary(self, text):
        self.diaries.append(str(text))
        return True

    async def permanent_memory(self, text, idem=""):
        self.memories.append(str(text))
        return True

    async def long_memory(self, text, idem=""):
        self.memories.append(str(text))
        return True

    async def remember_event(self, kind, amount_cents, text, idem=""):
        self.events.append((str(kind), int(amount_cents or 0), str(text), str(idem)))
        return True

    def add_schedule(self, day, activity, **kwargs):
        self.schedules.append((str(day), str(activity)))
        return True

    async def low_balance(self, need_cents=0, idem=""):
        self.events.append(("low_balance", int(need_cents or 0), "", str(idem)))
        return True


class FakePlugin:
    """插件桩：finance / finance_repo 一定在，其它模块按关键字挂上去。"""

    def __init__(self, cash=500000, config=None, **modules):
        self.finance = modules.pop("finance", None) or FakeFinance(cash)
        self.finance_repo = modules.pop("finance_repo", None) or FakeRepo()
        self.config = dict(config or {})
        for name, value in modules.items():
            setattr(self, name, value)

    def cfg_str(self, key, default=""):
        return str(self.config.get(key, default))

    def cfg_bool(self, key, default=False):
        return bool(self.config.get(key, default))

    def cfg_int(self, key, default=0):
        return int(self.config.get(key, default))


def temp_store(tmp_path, name="d"):
    """tmp_path 下的真 SQLite：(store, repo)。跑完随 tmp_path 一起删，不碰 plugin_data。"""
    from models.db import Store
    from models.ledger import FinanceRepo

    store = Store(root=tmp_path / name, min_free_mb=0)
    store.init()
    return store, FinanceRepo(store)


__all__ = ["ROOT", "DEFAULT_NOW", "FakeClock", "FakeRepo", "FakeWallet",
           "FakeAmo", "FakeFinance", "FakePlugin", "temp_store", "SUPPORT_VERSION",
           "temp_dir", "LogCapture", "assert_no_pii"]
