# -*- coding: utf-8 -*-
"""收盘 run_slot 端到端：股票 50% 单笔裁剪 + 500 元基金首次申购 + 医院费按天去重。

2026-11-18 线上事故参数复盘：现金 6604.26 元、中国银行 7.36 元、LLM 下 500 股，
以前直接拒单；现在按 50% 上限裁成 400 股成交，并在同一收盘槽补一笔 500 元基金。
医院费走 charge_schedule_visit：同一天只扣一次挂号+交通（15 元）。
"""
import asyncio
import datetime as dt
import json

import pytest

import core.agent_loop as agent_loop
import services.trade_engine as trade_engine
from core.agent_loop import FinanceDecision
from models.db import Store
from models.ledger import FinanceRepo
from services.medical_engine import MedicalEngine
from services.trade_engine import Trading

NOW = dt.datetime(2026, 11, 18, 14, 50)
DAY = dt.date(2026, 11, 18)
QUOTE = {"code": "601988", "name": "中国银行", "price": 7.36, "prev_close": 7.30}
BUY = {"action": "buy", "code": "601988", "qty": 500, "reason": "空仓试一笔"}


class FakeWallet:
    def __init__(self, cents=660426):
        self.cents = cents
        self.seen = []

    def balance(self):
        return self.cents

    def debit(self, cents, reason, idem, ref=""):
        if self.cents < cents:
            return False
        self.cents -= cents
        self.seen.append(("debit", cents, idem))
        return True

    def credit(self, cents, reason, idem, ref=""):
        self.cents += cents
        self.seen.append(("credit", cents, idem))
        return True


class FakeAmo:
    def __init__(self, wallet):
        self.wallet = wallet


class FakeMemory:
    def __init__(self):
        self.written = []

    async def remember(self, *args, **kwargs):
        self.written.append((args, kwargs))
        return True


class FakeFinance:
    def __init__(self, wallet):
        self.wallet = wallet
        self.amo = FakeAmo(wallet)
        self.memory = FakeMemory()
        self.facts = []

    def balance_cents(self):
        return self.wallet.cents

    def mood(self):
        return {"word": "平静", "score": 60}

    def _amo(self):
        return self.amo

    async def record_trade(self, *args, **kwargs):
        return {"ok": True}

    async def record_fund_fact(self, *args, **kwargs):
        self.facts.append((args, kwargs))
        return {"ok": True}


class FakeRuntime:
    async def run_io(self, fn, *args, timeout=None, **kwargs):
        return fn(*args, **kwargs)


class FakeFundMarket:
    def __init__(self, plugin, runtime, repo):
        pass

    async def refresh(self, now):
        return None

    def latest(self, day):
        return {"110020": {"code": "110020", "name": "易方达沪深300ETF联接A",
                           "estimate": 1.65, "nav": 1.65, "tradeable": True}}

    def facts(self, day, cash, quotes):
        return "基金：110020 估值 1.65"


def _decision(tmp_path, monkeypatch, cash=660426):
    store = Store(root=tmp_path / "d", min_free_mb=0)
    store.init()
    repo = FinanceRepo(store)
    wallet = FakeWallet(cash)
    finance = FakeFinance(wallet)
    trading = Trading(None, FakeRuntime(), repo, finance)
    dec = FinanceDecision(None, FakeRuntime(), repo, finance, trading)
    monkeypatch.setattr(agent_loop, "FundMarket", FakeFundMarket)
    monkeypatch.setattr(trade_engine, "fetch_quotes", lambda codes: {"601988": dict(QUOTE)})
    monkeypatch.setattr(trade_engine, "in_session", lambda now: True)

    async def fake_refresh(self, codes):
        return {"601988": dict(QUOTE)}

    async def fake_news(self, positions, now, day):
        return "", None

    async def fake_decision(self, now, cash, positions, quotes, mood, news_text,
                            llm_decide, fund_text=""):
        return dict(BUY)

    monkeypatch.setattr(FinanceDecision, "_refresh", fake_refresh)
    monkeypatch.setattr(FinanceDecision, "_slot_news", fake_news)
    monkeypatch.setattr(FinanceDecision, "_slot_decision", fake_decision)
    return dec, repo, finance, wallet, store


def test_close_slot_clips_stock_to_50pct_and_buys_fund(tmp_path, monkeypatch):
    dec, repo, finance, wallet, store = _decision(tmp_path, monkeypatch)
    try:
        result = asyncio.run(dec.run_slot("close", NOW))
        executed = result["executed"]
        assert executed["ok"] is True, executed
        # 500 股请求 → 现金 6604.26 的 50% 只够 400 股
        pos = repo.position("stock", "601988")
        assert pos["qty"] == 400
        assert "400 股" in executed["message"]
        payload = json.loads(repo.meta_get("last_decision"))
        assert payload["qty"] == 500          # 决策本身保留 LLM 的原始请求
        assert payload["executed"] is True
        # 同一收盘槽补一笔 500 元基金首次申购
        assert payload["fund_entry"]["code"] == "110020"
        assert payload["fund_entry"]["amount_cents"] == 50000
        assert payload["fund_entry"]["executed"] is True
        fund = repo.position("fund", "110020")
        assert fund and fund["qty"] > 0
        # 股票成交 294400 分 + 费用 503 分，再扣基金 50000 分
        assert wallet.cents == 660426 - 294400 - 503 - 50000
    finally:
        store.close()


def test_close_slot_skips_fund_when_position_exists(tmp_path, monkeypatch):
    dec, repo, finance, wallet, store = _decision(tmp_path, monkeypatch)
    try:
        repo.upsert_position("fund", "110020", "旧基金", 100.0, 165, 165, 100.0, "2026-11-18")
        result = asyncio.run(dec.run_slot("close", NOW))
        assert result["executed"]["ok"] is True
        payload = json.loads(repo.meta_get("last_decision"))
        assert "fund_entry" not in payload
    finally:
        store.close()


class FakeMetaRepo:
    def __init__(self):
        self.meta = {}

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = str(value)


class FakeScheduleDB:
    def __init__(self, rows):
        self.rows = rows

    def get_schedule(self, day):
        return [dict(row) for row in self.rows.get(day, [])]


class FakeMedAmo:
    def __init__(self, rows):
        self.state = type("State", (), {})()
        self.state.db = FakeScheduleDB(rows)


class FakeMedFinance:
    def __init__(self, rows):
        self.amo = FakeMedAmo(rows)
        self.expenses = []
        self.rows = []

    def _amo(self):
        return self.amo

    def ledger(self, limit=500):
        return list(self.rows)

    async def expense(self, cents, label, idem, necessary=False):
        self.expenses.append((cents, label, idem))
        self.rows.append({"idem": idem, "amount_cents": -cents})
        return {"ok": True, "amount_cents": cents}


def test_hospital_fee_charged_once_per_day():
    rows = {"2026-11-18": [{"activity": "去医院看病", "place": "市医院"}],
            "2026-11-19": [{"activity": "去医院复查", "place": "医院"}]}
    repo = FakeMetaRepo()
    finance = FakeMedFinance(rows)
    engine = MedicalEngine(None, repo, finance)
    first = asyncio.run(engine.charge_schedule_visit(DAY))
    assert first == {"ok": True, "amount_cents": 1500}
    assert len(finance.expenses) == 1
    # 同一天再跑：meta 已记，直接跳过
    assert asyncio.run(engine.charge_schedule_visit(DAY)) is None
    assert len(finance.expenses) == 1
    # 第二天日程又有医院：新的一天扣一次
    second = asyncio.run(engine.charge_schedule_visit(dt.date(2026, 11, 19)))
    assert second == {"ok": True, "amount_cents": 1500}
    assert [row[2] for row in finance.expenses] == ["hospital:2026-11-18", "hospital:2026-11-19"]


def test_hospital_fee_skips_when_ledger_already_has_idem():
    rows = {"2026-11-20": [{"activity": "挂号", "place": "医院"}]}
    repo = FakeMetaRepo()
    finance = FakeMedFinance(rows)
    finance.rows.append({"idem": "hospital:2026-11-20", "amount_cents": -1500})
    engine = MedicalEngine(None, repo, finance)
    assert asyncio.run(engine.charge_schedule_visit(dt.date(2026, 11, 20))) is None
    assert finance.expenses == []
