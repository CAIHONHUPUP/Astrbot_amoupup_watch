# -*- coding: utf-8 -*-
import asyncio, datetime as dt, tempfile, shutil, types
import services.trade_engine as te
from models.db import Store
from models.ledger import FinanceRepo
from services.trade_engine import Trading

class W:
    def __init__(self): self.c = 1000000
    def balance(self, account="cash"): return self.c
    def debit(self, n, r, i): self.c -= n; return True
    def credit(self, n, r, i): self.c += n; return True

class Link:
    def __init__(self): self.w = W()
    def _amo(self): return types.SimpleNamespace(wallet=self.w)
    def balance_cents(self): return self.w.c
    def mood(self): return {"word": "平静"}
    async def record_trade(self, *a, **k): return True
    async def trade_pnl(self, *a, **k): return {"ok": True}

class Runtime:
    async def run_io(self, fn, *a, **k):
        return {"code": "600519", "name": "贵州茅台", "price": 16.0, "prev_close": 15.8,
                "open": 15.9, "high": 16.3, "low": 15.6, "volume": 1, "turnover": 1, "suspended": False}
    async def run_db(self, fn, *a, **k): return fn(*a, **k)

class P:
    config = {}

class FixedDT(dt.datetime):
    @classmethod
    def now(cls, tz=None): return dt.datetime(2026, 11, 16, 10, 0)

def test_buy_uses_explicit_price():
    te._dt = types.SimpleNamespace(datetime=FixedDT, date=dt.date, timedelta=dt.timedelta, time=dt.time)
    tmp = tempfile.mkdtemp(prefix="trade-")
    store = Store(tmp); store.init()
    t = Trading(P(), Runtime(), FinanceRepo(store), Link())
    out = asyncio.run(t.buy("600519", 100, 16.2, "t:buy"))
    store.close(); shutil.rmtree(tmp, ignore_errors=True)
    assert out["ok"] and "16.2" in out["message"], out
