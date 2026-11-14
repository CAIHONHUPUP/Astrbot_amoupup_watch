# -*- coding: utf-8 -*-
"""test_cross_plugin.py：Mock 陪伴/记忆/amo，验证三条跨插件链路。"""
import asyncio, datetime as dt
from models.db import Store
from models.ledger import FinanceRepo
from services.medical_engine import MedicalEngine
from services.expense_engine import FinanceExpense
from services.credit_engine import FinanceHelp
from core.amo_bridge import FinanceLink
import tempfile, shutil


class Wallet:
    def __init__(self, c=0): self.c = c
    def balance(self, account="cash"): return self.c
    def debit(self, n, r, i):
        if n > self.c: return False
        self.c -= n; return True
    def credit(self, n, r, i): self.c += n; return True


class Amo:
    def __init__(self, c):
        self.wallet = Wallet(c)
        self.memories = []
        self.state = type("S", (), {
            "add_schedule_item": lambda *a, **k: {"changed": True},
            "mood_score": lambda *a, **k: 60,
            "mood_words": lambda *a, **k: {},
            "dominant_mood": lambda *a, **k: "平静",
            "nudge": lambda *a, **k: True,
            "nudge_once": lambda *a, **k: True,
        })()
        class DB:
            def save_diary(self, *a, **k): return True
            def kv_set(self, *a, **k): return True
            def kv_get(self, *a, **k): return None
        self.db = DB()
    def finance_remember(self, content, kind="finance", tier=2, idem_key=""):
        self.memories.append(content); return True
    def finance_recall(self, query, limit=5): return []


class Plugin:
    config = {}

def make(cash=1000000):
    tmp = tempfile.mkdtemp(prefix="cp-")
    store = Store(tmp); store.init()
    repo = FinanceRepo(store)
    p = Plugin(); p.finance_repo = repo; p.context = None
    amo = Amo(cash)
    p.finance = FinanceLink(p)
    p.finance._amo = lambda: amo
    return tmp, store, repo, p, amo

def test_user_sick_full_chain():
    tmp, store, repo, p, amo = make(2000000)
    p.medical = MedicalEngine(p, repo, p.finance)
    recorded = []
    async def record_memory(text, idem):
        recorded.append(text); return True
    p.finance.permanent_memory = record_memory
    p.finance.small_diary = lambda t: (recorded.append(t), True)[1]
    p.finance.long_diary = lambda t: (recorded.append(t), True)[1]
    out = asyncio.run(p.medical.handle_sick_event("heavy", "重症肺炎", 3, "sick_cp_1"))
    assert out["ok"], out
    assert out["self_pay_cents"] == out["total_cents"] - out["insurance_cents"]
    assert out["self_pay_cents"] <= out["total_cents"] * 0.3 + 1
    assert p.medical.is_sick() is True
    assert p.medical.in_bed_rest() is True
    assert any(("自费" in m or "看病" in m) for m in recorded), recorded
    print("PASSUSER_SICK 自付=%s 报销=%s 卧床=%s 记忆=%d" % (
        out["self_pay_cents"], out["insurance_cents"], p.medical.in_bed_rest(), len(recorded)))
    store.close(); shutil.rmtree(tmp, ignore_errors=True)

def test_rent_bankruptcy_help():
    tmp, store, repo, p, amo = make(30000)
    repo.add_asset("house", "测试房产", 100000000)   # 总资产 100 万，现金只剩 300
    help_eng = FinanceHelp(p, repo, p.finance)
    state = help_eng.status()
    text = help_eng.prompt_text()
    print("PASSLOW_FUND low=%s cash=%s 求助文案=%s" % (state.get("low"), state.get("cash"), bool(text)))
    assert state["low"] is True and ("借款" in text or "要钱" in text)
    store.close(); shutil.rmtree(tmp, ignore_errors=True)

def test_high_spend_blocked():
    tmp, store, repo, p, amo = make(5000)
    exp = FinanceExpense(p, repo, p.finance)
    out = asyncio.run(exp.spend("food", "闺蜜约饭", 20000, day="2026-11-14", idem="cp:dinner"))
    print("PASSEXPENSE_BLOCKED ok=%s reason=%s cash=%s" % (out.get("ok"), out.get("reason"), amo.wallet.balance()))
    assert out["ok"] is False and amo.wallet.balance() == 5000
    store.close(); shutil.rmtree(tmp, ignore_errors=True)
