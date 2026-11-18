# -*- coding: utf-8 -*-
"""自主交易决策（FinanceDecision）分支补测（2026-11-18）。

覆盖：候选/行情/新闻/决策输入、午间挂单、收盘执行、基金兜底、资金紧张、
收盘结算与 tick 时点；断言看具体 action/字段/账本写入，不用空断言。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.agent_loop as agent_loop  # noqa: E402
import core.fund_actions as fund_actions  # noqa: E402
import core.llm_decider as llm_decider  # noqa: E402
import services.bank_engine as bank_engine  # noqa: E402
import services.news_api as news_api  # noqa: E402
from core.agent_loop import FinanceDecision, PENDING_KEY  # noqa: E402

DAY = "2026-11-18"


def run(coro):
    return asyncio.run(coro)


def at(hour, minute=0):
    return dt.datetime(2026, 11, 18, hour, minute)


class FakeRepo:
    def __init__(self, positions=None, meta=None):
        self.data = dict(meta or {})
        self.rows = [dict(row) for row in (positions or [])]
        self.cached = []
        self.upserts = []

    def meta_get(self, key, default=None):
        return self.data.get(key, default)

    def meta_set(self, key, value):
        self.data[key] = value

    def positions(self, kind=None):
        if kind:
            return [row for row in self.rows if row.get("kind") == kind]
        return [dict(row) for row in self.rows]

    def cache_quote(self, day, code, last_cents, prev_cents, name):
        self.cached.append((day, code, last_cents, prev_cents, name))

    def upsert_position(self, kind, code, name, qty, cost, last, available):
        self.upserts.append((kind, code, last))
        for row in self.rows:
            if row.get("code") == code:
                row["last_cents"] = last


class FakeRuntime:
    def __init__(self, quotes=None, news=None, fail=False):
        self.quotes = {} if quotes is None else quotes
        self.news = [] if news is None else news
        self.fail = fail
        self.calls = []

    async def run_io(self, fn, *args, timeout=0):
        name = getattr(fn, "__name__", "?")
        self.calls.append((name, args))
        if self.fail:
            raise RuntimeError("io boom")
        if name == "fetch_quotes":
            return self.quotes
        return self.news


class FakeMemory:
    def __init__(self):
        self.lines = []
        self.context_args = None

    async def remember(self, line, kind="", idem=""):
        self.lines.append((kind, idem, line))
        return {"ok": True}

    def context_text(self, codes, limit=5):
        self.context_args = (list(codes), limit)
        return "记忆摘要"


class FakeAmoDB:
    def __init__(self):
        self.diaries = []

    def save_diary(self, day, hour, text):
        self.diaries.append((day, hour, text))


class FakeAmoState:
    def __init__(self):
        self.items = []

    def add_schedule_item(self, day, item, source="finance", reason=""):
        self.items.append((day, item, source, reason))
        return {"changed": True}


class FakeAmo:
    def __init__(self):
        self.db = FakeAmoDB()
        self.state = FakeAmoState()
        self.big_loss = 0

    def on_big_stock_loss(self):
        self.big_loss += 1


class FakeFinance:
    def __init__(self, cash=1000000, amo=None):
        self.cash = cash
        self.memory = FakeMemory()
        self.mood_data = {"score": 60.0, "word": "平静"}
        self.events = []
        self.pnl = []
        self.outages = []
        self.amo = amo

    def balance_cents(self):
        return self.cash

    def mood(self):
        return dict(self.mood_data)

    def _amo(self):
        return self.amo

    async def remember_event(self, *args):
        self.events.append(args)
        return True

    async def record_daily_pnl(self, items, total, idem=""):
        self.pnl.append((items, total, idem))
        return {"ok": True}

    async def record_fund_outage(self, fact, idem=""):
        self.outages.append((fact, idem))
        return {"ok": True}


class FakeTrading:
    def __init__(self):
        self.calls = []

    async def buy(self, code, qty, price, idem):
        self.calls.append(("buy", code, qty, idem))
        return {"ok": True, "message": "买入 %s" % code}

    async def sell(self, code, qty, price, idem):
        self.calls.append(("sell", code, qty, idem))
        return {"ok": True, "message": "卖出 %s" % code}

    async def subscribe(self, code, amount, nav, idem, name=""):
        self.calls.append(("subscribe", code, amount, nav, idem, name))
        return {"ok": True, "message": "申购"}

    async def redeem(self, code, shares, nav, idem):
        self.calls.append(("redeem", code, shares, nav, idem))
        return {"ok": True, "message": "赎回"}


class OldTrading(FakeTrading):
    async def subscribe(self, code, amount, nav, idem):
        self.calls.append(("subscribe_old", code, amount, nav, idem))
        return {"ok": True, "message": "旧申购"}


class FakeBank:
    def __init__(self, deposit_ok=True, liquid=None):
        self.deposit_ok = deposit_ok
        self.liquid = liquid or {"ok": False}
        self.calls = []

    async def deposit(self, amount, term):
        self.calls.append((amount, term))
        if not self.deposit_ok:
            return {"ok": False, "reason": "no_money"}
        return {"ok": True}

    async def emergency_liquidate(self, need):
        self.calls.append(("liquid", need))
        return self.liquid

    def prompt_line(self):
        return "银行利率 2%"


class FakePool:
    def __init__(self):
        self.repo = self
        self.observed = []
        self.remembered = []

    def daily_anchors(self, day):
        return ["600036"]

    def candidates(self):
        return [{"code": "000001"}, {"code": ""}, {"code": "600036"}]

    def remember_names(self, quotes):
        self.remembered.append(dict(quotes))

    def observe(self, quotes, day):
        self.observed.append((dict(quotes), day))
        return ["300750"]

    def affordable(self, quotes, cash):
        return [code for code in quotes if cash >= 100000]


def make_engine(**kwargs):
    plugin = kwargs.pop("plugin", None)
    repo = kwargs.pop("repo", None) or FakeRepo()
    finance = kwargs.pop("finance", None) or FakeFinance()
    runtime = kwargs.pop("runtime", None) or FakeRuntime()
    trading = kwargs.pop("trading", None) or FakeTrading()
    help_ = kwargs.pop("help_", None)
    pools = kwargs.pop("pools", None)
    return FinanceDecision(plugin or type("P", (), {})(), runtime, repo, finance,
                           trading, help_, pools)


# ---------------- 持仓 / 行情 ----------------
def test_positions_compute_pnl_and_fallbacks():
    repo = FakeRepo([
        {"kind": "stock", "code": "600519", "qty": 100, "cost_cents": 10000, "last_cents": 11000},
        {"kind": "stock", "code": "000001", "qty": 200, "cost_cents": 0, "last_cents": 500},
    ])
    rows = make_engine(repo=repo)._positions()
    assert rows[0]["pnl_pct"] == pytest.approx(10.0)
    assert rows[1]["pnl_pct"] == 0.0
    assert rows[0]["name"] == "600519"


def test_refresh_caches_quotes_and_updates_positions():
    repo = FakeRepo([{"kind": "stock", "code": "600519", "qty": 100, "cost_cents": 10000}])
    runtime = FakeRuntime(quotes={"600519": {"price": 10.5, "prev_close": 10.0, "name": "茅台"},
                                  "000001": {"price": None}})
    quotes = run(make_engine(repo=repo, runtime=runtime)._refresh(["600519", "000001"]))
    assert quotes["600519"]["price"] == 10.5
    assert repo.cached == [(DAY, "600519", 1050, 1000, "茅台")]
    assert repo.upserts == [("stock", "600519", 1050)]
    assert repo.rows[0]["last_cents"] == 1050


def test_refresh_cache_error_is_isolated():
    class BoomRepo(FakeRepo):
        def cache_quote(self, *args):
            raise RuntimeError("cache boom")

    repo = BoomRepo([{"kind": "stock", "code": "600519", "qty": 1, "cost_cents": 100}])
    runtime = FakeRuntime(quotes={"600519": {"price": 1.0}})
    assert run(make_engine(repo=repo, runtime=runtime)._refresh(["600519"]))["600519"]["price"] == 1.0
    assert repo.upserts == [("stock", "600519", 100)]


# ---------------- 候选 ----------------
def test_slot_candidates_with_and_without_pools():
    repo = FakeRepo([{"kind": "stock", "code": "600519", "qty": 1, "cost_cents": 100}])
    engine = make_engine(repo=repo, pools=FakePool())
    _, codes = engine._slot_candidates(at(9, 30))
    assert codes[0] == "600519"
    assert "600036" in codes and "000001" in codes

    engine = make_engine(repo=repo, pools=None)
    _, codes = engine._slot_candidates(at(9, 30))
    assert set(agent_loop.CANDIDATES).issubset(set(codes))


# ---------------- 行情失败兜底 ----------------
def test_slot_quotes_data_failed_day():
    repo = FakeRepo(meta={"news_fail:" + DAY: "quotes"})
    quotes, failure = run(make_engine(repo=repo)._slot_quotes(["600519"], at(9, 30), DAY))
    assert quotes is None
    assert failure["skipped"] == "data_failed"
    assert failure["executed"]["message"] == "数据失败，今天不交易"


def test_slot_quotes_failed_sets_news_fail():
    repo = FakeRepo()
    quotes, failure = run(make_engine(repo=repo, runtime=FakeRuntime(quotes={}))._slot_quotes(
        ["600519"], at(9, 30), DAY))
    assert quotes is None and failure["skipped"] == "quotes_failed"
    assert repo.meta_get("news_fail:" + DAY) == "quotes"


def test_slot_quotes_observation_extends_codes():
    pool = FakePool()
    quotes = {"600519": {"price": 10.0}}
    runtime = FakeRuntime(quotes=quotes)
    got, failure = run(make_engine(runtime=runtime, pools=pool)._slot_quotes(["600519"], at(9, 30), DAY))
    assert failure is None and got == quotes
    assert pool.remembered == [quotes]
    assert pool.observed[0][1] == DAY


# ---------------- 新闻 ----------------
def test_slot_news_quota_used_up():
    repo = FakeRepo(meta={"news_fetch:" + DAY: 3})
    news, failure = run(make_engine(repo=repo)._slot_news([], at(9, 30), DAY))
    assert news is None and failure["skipped"] == "news_quota"


def test_slot_news_empty_is_isolated_and_not_counted():
    repo = FakeRepo()
    runtime = FakeRuntime(news=[])
    news, failure = run(make_engine(repo=repo, runtime=runtime)._slot_news([], at(9, 30), DAY))
    assert news == "" and failure is None
    assert repo.meta_get("news_fetch:" + DAY) is None


def test_slot_news_merges_stock_news_and_remembers(monkeypatch):
    monkeypatch.setattr(news_api, "merge_stock_news",
                        lambda code, items, day: "持仓新闻 %s" % code)
    repo = FakeRepo()
    runtime = FakeRuntime(news=[{"title": "大盘上涨"}])
    plugin = type("P", (), {})()
    plugin.finance_memory = type("TM", (), {
        "written": [],
        "remember": None,
    })()
    written = []
    async def remember(kind, text, idem, related=""):
        written.append((kind, text, idem, related))
        return True
    plugin.finance_memory.remember = remember
    positions = [{"code": "600519", "qty": 1, "pnl_pct": 0}]
    news, failure = run(make_engine(plugin=plugin, repo=repo, runtime=runtime)._slot_news(
        positions, at(9, 30), DAY))
    assert failure is None
    assert "市场：大盘上涨" in news and "持仓新闻 600519" in news
    assert written == [("news", "持仓新闻 600519", "news:%s:600519" % DAY, "600519")]
    assert repo.meta_get("news_fetch:" + DAY) == 1


def test_slot_news_io_failure_is_isolated():
    runtime = FakeRuntime(fail=True)
    news, failure = run(make_engine(runtime=runtime)._slot_news([], at(9, 30), DAY))
    assert news == "" and failure is None


# ---------------- 决策输入 ----------------
class FakeInsurance:
    def __init__(self):
        self.hints = []

    def prompt_line(self):
        return "保险建议行"

    def decision_hint(self, health=80, news="", idle_cents=0, has_car=False, has_house=False):
        self.hints.append((health, news, idle_cents, has_car, has_house))
        return "【保险购买建议】买点保险"


def test_slot_decision_passes_allowed_and_memory():
    plugin = type("P", (), {})()
    plugin.finance_memory = FakeMemory()
    plugin.bank = FakeBank()
    plugin.insurance = FakeInsurance()
    captured = {}

    async def fake_llm(plugin_, cash, positions, quotes, word, allowed, news, memory, fund_text=""):
        captured.update(cash=cash, positions=positions, allowed=set(allowed),
                        word=word, news=news, memory=memory, fund_text=fund_text)
        return {"action": "hold", "code": "", "reason": "观望"}

    engine = make_engine(plugin=plugin, pools=FakePool())
    engine._fund_codes = {"110011"}
    positions = [{"code": "600519", "qty": 1, "pnl_pct": 0}]
    decision = run(engine._slot_decision(at(9, 30), 1000000, positions,
                                        {"600519": {"price": 1.0}}, {"word": "平静"},
                                        "新闻", fake_llm, fund_text="基金事实"))
    assert decision["action"] == "hold"
    assert captured["allowed"] == {"600519", "110011"}
    assert captured["fund_text"] == "基金事实"
    assert "记忆摘要" in captured["memory"] and "银行利率 2%" in captured["memory"]
    assert "保险建议行" in captured["memory"]


def test_slot_decision_without_plugin_skips_llm():
    engine = make_engine(plugin=None)
    engine.plugin = None
    assert run(engine._slot_decision(at(9, 30), 0, [], {}, {"word": "平静"}, "",
                                     lambda *a, **k: None)) is None


def test_slot_bank_line_without_bank_or_insurance_is_empty():
    plugin = type("P", (), {})()
    engine = make_engine(plugin=plugin)
    assert engine._slot_bank_line(at(9, 30)) == ""


def test_slot_bank_line_appends_insurance_line():
    class Ins:
        def prompt_line(self):
            return "保险建议行"

        def decision_hint(self, **kwargs):
            return ""

    plugin = type("P", (), {"bank": FakeBank(), "insurance": Ins()})()
    engine = make_engine(plugin=plugin)
    line = engine._slot_bank_line(at(9, 30))
    assert line.splitlines() == ["银行利率 2%", "保险建议行"]


def test_slot_insurance_hint_risk_and_dedupe():
    ins = FakeInsurance()
    repo = FakeRepo(meta={"news_text_today": "今天有人猝死", "new_asset_event": "car"})
    med = type("M", (), {"health_snapshot": lambda self: {"severity": 2}})()
    plugin = type("P", (), {"medical": med})()
    engine = make_engine(plugin=plugin, repo=repo)
    hint = engine._slot_insurance_hint(ins, at(9, 30))
    assert "【今日风险提示】" in hint and "新车" in hint and "猝死" in hint
    assert ins.hints[-1][0] == 60
    assert repo.meta_get("new_asset_event") == ""
    assert repo.meta_get("last_insurance_check_date") == DAY
    assert engine._slot_insurance_hint(ins, at(9, 30)) == ""


def test_slot_insurance_hint_without_extras():
    ins = FakeInsurance()
    engine = make_engine(repo=FakeRepo())
    hint = engine._slot_insurance_hint(ins, at(9, 30))
    assert hint == "【保险购买建议】买点保险"


# ---------------- 挂单 / 执行 ----------------
def test_fund_nudge_branches():
    engine = make_engine(repo=FakeRepo())
    hold = {"action": "hold", "code": "", "reason": "观望"}
    assert engine._fund_nudge(hold, "noon", 100000) == hold
    engine._fund_codes = {"110011"}
    engine._fund_quotes = {"110011": {"tradeable": True}}
    plan = engine._fund_nudge(hold, "close", 100000)
    assert plan == {"action": "buy_fund", "code": "110011", "amount_cents": 50000,
                    "reason": "基金池还没碰过，先小额试一笔"}
    assert engine._fund_nudge(hold, "close", 1000) == hold
    with_fund = FakeRepo([{"kind": "fund", "code": "110011", "qty": 10, "cost_cents": 100}])
    assert make_engine(repo=with_fund)._fund_nudge(hold, "close", 100000) == hold


def test_fund_entry_plan_and_record():
    engine = make_engine()
    engine._fund_codes = {"110011"}
    engine._fund_quotes = {"110011": {"tradeable": True}}
    assert engine._fund_entry_plan({"action": "hold"}, "noon") is None
    assert engine._fund_entry_plan({"action": "buy_fund"}, "close") is None
    plan = engine._fund_entry_plan({"action": "hold"}, "close")
    assert plan["action"] == "buy_fund"
    assert plan["amount_cents"] == 50000
    run(engine._record_fund_entry(plan, {"ok": True, "message": "ok"}, at(14, 50)))
    payload = json.loads(engine.repo.meta_get("last_decision"))
    assert payload["fund_entry"] == {"code": "110011", "amount_cents": 50000, "executed": True,
                                     "message": "ok"}


def test_fund_entry_plan_when_cash_missing():
    engine = make_engine()
    engine._fund_codes = {"110011"}
    engine._fund_quotes = {"110011": {"tradeable": True}}
    engine.finance.cash = 0
    assert engine._fund_entry_plan({"action": "hold"}, "close") is None


def test_idle_nudge_branches():
    hold = {"action": "hold", "code": "", "reason": "观望"}
    engine = make_engine()
    assert engine._idle_nudge(hold, [], {}, 100000, "noon") == hold
    positions = [{"code": "600519", "qty": 1, "pnl_pct": 0}]
    assert engine._idle_nudge(hold, positions, {}, 100000, "close") == hold
    engine.repo.data["hold_streak"] = 1
    assert engine._idle_nudge(hold, [], {}, 100000, "close") == hold
    engine.repo.data["hold_streak"] = 3
    assert engine._idle_nudge(hold, [], {}, 100000, "close") == hold  # pools None
    engine = make_engine(pools=FakePool())
    engine.repo.data["hold_streak"] = 3
    plan = engine._idle_nudge(hold, [], {"600519": {"price": 1.0}}, 1000000, "close")
    assert plan == {"action": "buy", "code": "600519", "qty": 100,
                    "reason": "空仓观望好几天了，先小仓位试一笔"}
    engine.repo.meta_get = lambda key, default=None: "bad"
    assert engine._idle_nudge(hold, [], {}, 100000, "close") == hold


def test_is_trade_matrix():
    engine = make_engine()
    assert engine._is_trade(None) is False
    assert engine._is_trade({"action": "hold"}) is False
    assert engine._is_trade({"action": "buy"}) is False
    assert engine._is_trade({"action": "buy", "code": "600519", "qty": 0}) is False
    assert engine._is_trade({"action": "buy", "code": "600519", "qty": 100}) is True
    assert engine._is_trade({"action": "sell", "code": "600519", "qty": "bad"}) is False
    assert engine._is_trade({"action": "buy_fund", "code": "110011", "amount_cents": 50000}) is True
    assert engine._is_trade({"action": "sell_fund", "code": "110011", "shares": 10}) is True
    assert engine._is_trade({"action": "sell_fund", "code": "110011", "shares": "x"}) is False


def test_queue_and_take_pending():
    engine = make_engine()
    trade = {"action": "buy", "code": "600519", "qty": "100", "amount_cents": 0,
             "shares": 0.0, "reason": "试试"}
    plan = engine._queue_pending(trade, at(11, 35))
    assert plan["day"] == DAY and plan["slot"] == "noon" and plan["qty"] == 100
    stored = json.loads(engine.repo.meta_get(PENDING_KEY))
    assert stored["action"] == "buy"

    taken = engine._take_pending(at(14, 50))
    assert taken["code"] == "600519"
    assert engine.repo.meta_get(PENDING_KEY) == ""
    assert engine._take_pending(at(14, 50)) is None

    assert engine._queue_pending({"action": "hold"}, at(11, 35)) is None
    assert engine.repo.meta_get(PENDING_KEY) == ""

    engine.repo.data[PENDING_KEY] = "{bad json"
    assert engine._take_pending(at(14, 50)) is None

    engine.repo.data[PENDING_KEY] = json.dumps({"action": "buy", "code": "600519",
                                                "qty": 1, "day": "2026-11-17"})
    assert engine._take_pending(at(14, 50)) is None
    assert engine.repo.meta_get(PENDING_KEY) == ""

    engine.repo.data[PENDING_KEY] = json.dumps({"action": "hold", "day": DAY})
    assert engine._take_pending(at(14, 50)) is None


def test_queue_and_take_pending_meta_errors():
    engine = make_engine()

    def boom(key, value=None):
        raise RuntimeError("meta boom")

    engine.repo.meta_set = boom
    assert engine._queue_pending({"action": "buy", "code": "600519", "qty": 1}, at(11, 35)) is None

    engine = make_engine()
    engine.repo.meta_get = boom
    assert engine._take_pending(at(14, 50)) is None

    engine = make_engine()
    engine.repo.meta_get = lambda key, default=None: "[]"
    engine.repo.meta_set = boom
    assert engine._take_pending(at(14, 50)) is None


def test_update_hold_streak():
    engine = make_engine()
    engine._update_hold_streak({"action": "hold"}, [], {"ok": False})
    assert engine.repo.meta_get("hold_streak") == "1"
    engine._update_hold_streak({"action": "hold"}, [], {"ok": False})
    assert engine.repo.meta_get("hold_streak") == "2"
    engine._update_hold_streak({"action": "buy"}, [], {"ok": True})
    assert engine.repo.meta_get("hold_streak") == "0"
    engine.repo.meta_get = lambda key, default=None: (_ for _ in ()).throw(RuntimeError("boom"))
    engine._update_hold_streak({"action": "hold"}, [], {"ok": False})  # 不抛


# ---------------- 执行 ----------------
def test_execute_deposit_and_failure():
    plugin = type("P", (), {"bank": FakeBank()})()
    engine = make_engine(plugin=plugin)
    out = run(engine._execute("close", {"action": "deposit", "amount_cents": 10000}, at(14, 50)))
    assert out["ok"] is True and "存了 100.00 元活期" in out["message"]
    assert plugin.bank.calls == [(10000, 0)]

    plugin.bank = FakeBank(deposit_ok=False)
    out = run(engine._execute("close", {"action": "deposit", "amount_cents": 100}, at(14, 50)))
    assert out["ok"] is False and "存款没成功（no_money）" in out["message"]


def test_execute_deposit_lazy_bank(monkeypatch):
    class BuiltBank(FakeBank):
        pass

    monkeypatch.setattr(bank_engine, "build_bank", lambda plugin: BuiltBank())
    plugin = type("P", (), {})()
    engine = make_engine(plugin=plugin)
    out = run(engine._execute("close", {"action": "deposit", "amount_cents": 500, "term_months": 3},
                              at(14, 50)))
    assert out["ok"] is True and "定期3个月" in out["message"]
    assert plugin.bank is not None


def test_execute_deposit_async_bank_factory():
    class Holder:
        def __init__(self):
            self.bank = None

    holder = Holder()

    async def make_bank(self):
        return FakeBank()

    plugin = type("P", (), {"_bank": make_bank})()
    engine = make_engine(plugin=plugin)
    out = run(engine._execute("close", {"action": "deposit", "amount_cents": 100}, at(14, 50)))
    assert out["ok"] is True


def test_execute_fund_branches():
    engine = make_engine()
    engine._fund_quotes = {"110011": {"tradeable": True, "estimate": 1.5, "name": "易方达"}}
    out = run(engine._execute("close", {"action": "buy_fund", "code": "110011",
                                        "amount_cents": 50000}, at(14, 50)))
    assert out["ok"] is True and engine.trading.calls[-1][0] == "subscribe"
    assert engine.trading.calls[-1][5] == "易方达"

    out = run(engine._execute("close", {"action": "sell_fund", "code": "110011",
                                        "shares": 10}, at(14, 50)))
    assert out["ok"] is True and engine.trading.calls[-1][0] == "redeem"

    engine._fund_quotes = {"110011": {"tradeable": False}}
    out = run(engine._execute("close", {"action": "buy_fund", "code": "110011",
                                        "amount_cents": 50000}, at(14, 50)))
    assert out == {"ok": False, "message": "今天这只基金没有可用估值，跳过交易"}
    out = run(engine._execute("close", {"action": "sell_fund", "code": "110011",
                                        "shares": 1}, at(14, 50)))
    assert out["message"] == "今天这只基金没有可用估值，跳过交易"


def test_execute_old_trading_subscribe_falls_back():
    engine = make_engine(trading=OldTrading())
    engine._fund_quotes = {"110011": {"tradeable": True, "estimate": 1.5, "name": "易方达"}}
    out = run(engine._execute("close", {"action": "buy_fund", "code": "110011",
                                        "amount_cents": 50000}, at(14, 50)))
    assert out["ok"] is True
    assert engine.trading.calls[-1][0] == "subscribe_old"


def test_execute_stock_and_default():
    engine = make_engine()
    out = run(engine._execute("morning", {"action": "buy", "code": "600519", "qty": 100}, at(9, 30)))
    assert out["ok"] is True and engine.trading.calls[-1] == ("buy", "600519", 100,
                                                              "decision:%s:morning:buy" % DAY)
    out = run(engine._execute("morning", {"action": "sell", "code": "600519", "qty": 100}, at(9, 30)))
    assert out["ok"] is True and engine.trading.calls[-1][0] == "sell"
    out = run(engine._execute("morning", {"action": "hold"}, at(9, 30)))
    assert out == {"ok": False, "message": "决定先不动"}


# ---------------- 记录 ----------------
def test_record_variants_write_meta_memory_and_diary():
    amo = FakeAmo()
    finance = FakeFinance(amo=amo)
    engine = make_engine(finance=finance)
    engine._news_used = True
    run(engine._record("close", {"action": "buy", "code": "600519", "qty": 100,
                                 "reason": "抄底"}, {"ok": True, "message": "成交"}, at(14, 50),
                       pending=True, from_pending=True))
    payload = json.loads(engine.repo.meta_get("last_decision"))
    assert payload["pending"] is True and payload["from_pending"] is True
    assert payload["qty"] == 100 and payload["message"] == "成交"
    assert finance.memory.lines[0][0] == "finance_decision"
    assert amo.db.diaries and "看了新闻" in amo.db.diaries[0][2]

    run(engine._record("close", {"action": "buy_fund", "code": "110011",
                                 "reason": "试水"}, {"ok": True}, at(14, 50)))
    line = json.loads(finance.memory.lines[-1][2])
    assert line["action"] == "buy_fund" and line["executed"] is True
    assert len(amo.db.diaries) == 1  # 基金不拼日记模板


def test_record_meta_error_and_no_amo():
    engine = make_engine()

    def boom(key, value=None):
        raise RuntimeError("boom")

    engine.repo.meta_set = boom
    run(engine._record("morning", {"action": "hold", "reason": "观望"}, {"ok": False}, at(9, 30)))
    assert len(engine.finance.memory.lines) == 1


def test_record_diary_exception_isolated():
    class BoomDB(FakeAmoDB):
        def save_diary(self, *args):
            raise RuntimeError("diary boom")

    amo = FakeAmo()
    amo.db = BoomDB()
    engine = make_engine(finance=FakeFinance(amo=amo))
    run(engine._record("morning", {"action": "hold", "reason": "观望"}, {"ok": False}, at(9, 30)))
    assert engine.finance.memory.lines


# ---------------- 收盘 / 结算 ----------------
def test_close_day_skips_done_and_records_pnl():
    repo = FakeRepo(meta={"close_done:" + DAY: "1"})
    finance = FakeFinance()
    engine = make_engine(repo=repo, finance=finance)
    assert run(engine.close_day(DAY)) == {"skipped": "done"}
    assert finance.pnl == []

    repo = FakeRepo([{"kind": "stock", "code": "600519", "name": "茅台", "qty": 100,
                      "cost_cents": 10000, "last_cents": 11000}])
    finance = FakeFinance(cash=100000)
    engine = make_engine(repo=repo, finance=finance,
                         runtime=FakeRuntime(quotes={"600519": {"price": 110.0}}))
    out = run(engine.close_day(DAY))
    assert out["ok"] is True and out["total_pnl"] == pytest.approx(1000.0)
    assert finance.pnl[0][1] == 100000
    assert repo.meta_get("close_done:" + DAY) == "1"
    assert repo.cached  # 收盘刷新了行情（run_io 返回 {} 时也会调用）


def test_close_day_big_loss_notifies():
    amo = FakeAmo()
    repo = FakeRepo([{"kind": "stock", "code": "600519", "name": "茅台", "qty": 1,
                      "cost_cents": 100000, "last_cents": 50000}])
    finance = FakeFinance(cash=10000, amo=amo)
    engine = make_engine(repo=repo, finance=finance)
    out = run(engine.close_day(DAY))
    assert out["total_pnl"] == pytest.approx(-500.0)
    assert amo.big_loss == 1


# ---------------- 资金紧张 ----------------
def test_low_fund_liquidates_without_alert():
    bank = FakeBank(liquid={"ok": True, "from": "fixed"})
    plugin = type("P", (), {"bank": bank})()
    engine = make_engine(plugin=plugin)
    engine.help = type("H", (), {"prompt_text": lambda self: "缺钱"})()
    run(engine._low_fund({"need_cents": 5000, "cash_cents": 1000}, at(14, 50)))
    assert engine.repo.meta_get("low_fund_alert") is None
    assert engine.finance.events == []


def test_low_fund_alert_and_schedule():
    amo = FakeAmo()
    engine = make_engine(finance=FakeFinance(amo=amo))
    engine.help = type("H", (), {"prompt_text": lambda self: "缺钱"})()
    run(engine._low_fund({"need_cents": 5000, "cash_cents": 1000}, at(14, 50)))
    assert engine.repo.meta_get("low_fund_alert") == DAY
    assert engine.finance.events[0][0] == "资金紧张"
    assert amo.state.items[0][1]["activity"] == "资金紧张"
    # 同一天不重复提醒
    run(engine._low_fund({"need_cents": 5000, "cash_cents": 1000}, at(15, 0)))
    assert len(engine.finance.events) == 1


def test_low_fund_bank_error_isolated():
    class BoomBank(FakeBank):
        async def emergency_liquidate(self, need):
            raise RuntimeError("bank boom")

    plugin = type("P", (), {"bank": BoomBank()})()
    engine = make_engine(plugin=plugin)
    engine.help = type("H", (), {"prompt_text": lambda self: "缺钱"})()
    run(engine._low_fund({"need_cents": 5000, "cash_cents": 1000}, at(14, 50)))
    assert engine.repo.meta_get("low_fund_alert") == DAY


# ---------------- tick ----------------
def test_tick_holiday_and_slots(monkeypatch):
    engine = make_engine()
    calls = []

    async def fake_run_slot(slot, now=None):
        calls.append(slot)
        return {"slot": slot}

    async def fake_close(day):
        calls.append("close_day")
        return {"ok": True}

    engine.run_slot = fake_run_slot
    engine.close_day = fake_close
    assert run(engine.tick(dt.datetime(2026, 11, 21, 10, 0))) == {"skipped": "holiday"}

    assert run(engine.tick(at(9, 0))) == {"done": []}
    assert calls == []

    out = run(engine.tick(at(11, 40)))
    assert [item["slot"] for item in out["done"]] == ["morning", "noon"]
    assert calls == ["morning", "noon"]

    # 同一时点第二次不重复决策
    assert run(engine.tick(at(11, 40))) == {"done": []}

    out = run(engine.tick(at(15, 10)))
    assert [item["slot"] for item in out["done"]] == ["close"]
    assert calls[-2:] == ["close", "close_day"]


# ---------------- run_slot ----------------
class FakeFundMarket:
    def __init__(self, plugin, runtime, repo, quotes=None):
        self.quotes = quotes if quotes is not None else {}
        self.refreshed = 0

    async def refresh(self, now=None):
        self.refreshed += 1

    def latest(self, day):
        return dict(self.quotes)

    def facts(self, day, cash, quotes):
        return "基金事实"


def test_run_slot_morning_executes_buy(monkeypatch):
    async def fake_llm(*args, **kwargs):
        return {"action": "buy", "code": "600519", "qty": 100, "reason": "抄底"}

    monkeypatch.setattr(llm_decider, "llm_decide", fake_llm)
    engine = make_engine(runtime=FakeRuntime(quotes={"600519": {"price": 10.0}}))
    out = run(engine.run_slot("morning", at(9, 30)))
    assert out["decision"]["action"] == "buy"
    assert out["executed"]["ok"] is True
    assert engine.trading.calls[-1][0] == "buy"
    assert engine.repo.meta_get("last_decision")


def test_run_slot_quotes_failure_short_circuits(monkeypatch):
    monkeypatch.setattr(llm_decider, "llm_decide", lambda *a, **k: None)
    engine = make_engine(runtime=FakeRuntime(quotes={}))
    out = run(engine.run_slot("morning", at(9, 30)))
    assert out["skipped"] == "quotes_failed"


def test_run_slot_news_failure_short_circuits(monkeypatch):
    async def fake_llm(*a, **k):
        return {"action": "hold", "code": "", "reason": "观望"}

    monkeypatch.setattr(llm_decider, "llm_decide", fake_llm)
    engine = make_engine(runtime=FakeRuntime(quotes={"600519": {"price": 10.0}}))
    engine.repo.data["news_fetch:" + DAY] = 3
    out = run(engine.run_slot("morning", at(9, 30)))
    assert out["skipped"] == "news_quota"


def test_run_slot_falls_back_to_decide_and_gamble_errors(monkeypatch):
    async def no_llm(*a, **k):
        return None

    monkeypatch.setattr(llm_decider, "llm_decide", no_llm)

    class BoomGamble:
        async def maybe_gamble(self, *a, **k):
            raise RuntimeError("gamble boom")

    plugin = type("P", (), {"gamble": BoomGamble()})()
    engine = make_engine(plugin=plugin, runtime=FakeRuntime(quotes={"600519": {"price": 10.0}}))
    out = run(engine.run_slot("morning", at(9, 30)))
    assert out["decision"]["action"] in ("buy", "sell", "hold")
    assert out["decision"]["reason"]


def test_run_slot_close_uses_fund_market(monkeypatch):
    class Market(FakeFundMarket):
        def __init__(self, *args):
            super().__init__(*args, quotes={"110011": {"tradeable": True, "name": "易方达"}})

    monkeypatch.setattr(agent_loop, "FundMarket", Market)
    engine = make_engine(runtime=FakeRuntime(quotes={"600519": {"price": 10.0}}))

    async def fake_decision(*args, **kwargs):
        engine._last_fund_text = kwargs.get("fund_text", "")
        return {"action": "hold", "code": "", "reason": "观望"}

    engine._slot_decision = fake_decision
    out = run(engine.run_slot("close", at(14, 50)))
    assert engine._fund_codes == {"110011"}
    assert engine._last_fund_text == "基金事实"
    assert out["decision"]["action"] == "buy_fund"


def test_run_slot_close_outage_when_no_tradeable(monkeypatch):
    class Market(FakeFundMarket):
        def __init__(self, *args):
            super().__init__(*args, quotes={"110011": {"tradeable": False}})

    monkeypatch.setattr(agent_loop, "FundMarket", Market)
    engine = make_engine()
    captured = {}

    async def fake_outage(now, day, cash, quotes, demo=False):
        captured.update(day=day, cash=cash, demo=demo)
        return {"skipped": "fund_outage"}

    engine._slot_outage = fake_outage
    out = run(engine.run_slot("close", at(14, 50)))
    assert out == {"skipped": "fund_outage"}
    assert captured == {"day": DAY, "cash": 1000000, "demo": False}


def test_run_slot_help_low_triggers_low_fund(monkeypatch):
    class Help:
        def status(self):
            return {"low": True}

        def prompt_text(self):
            return "缺钱"

    async def fake_decision(*args, **kwargs):
        return {"action": "hold", "code": "", "reason": "观望"}

    engine = make_engine(runtime=FakeRuntime(quotes={"600519": {"price": 10.0}}), help_=Help())
    engine._slot_decision = fake_decision
    called = []

    async def fake_low(state, now):
        called.append(state)

    engine._low_fund = fake_low
    out = run(engine.run_slot("morning", at(9, 30)))
    assert called == [{"low": True}]
    assert out["decision"]["action"] == "hold"


def test_run_slot_fund_market_exception_falls_back_to_outage(monkeypatch):
    class BoomMarket:
        def __init__(self, *args):
            raise RuntimeError("market boom")

    monkeypatch.setattr(agent_loop, "FundMarket", BoomMarket)
    engine = make_engine()
    captured = {}

    async def fake_outage(now, day, cash, quotes, demo=False):
        captured.update(cash=cash)
        return {"skipped": "fund_outage"}

    engine._slot_outage = fake_outage
    out = run(engine.run_slot("close", at(14, 50)))
    assert out == {"skipped": "fund_outage"} and captured["cash"] == 1000000


# ---------------- 估值全挂 ----------------
def test_slot_outage_llm_branches(monkeypatch):
    async def fake_outage(*args, **kwargs):
        return {"action": "send_message_to_user", "text": "网又崩了"}

    monkeypatch.setattr(llm_decider, "llm_decide_outage", fake_outage)

    async def send(plugin, text):
        return {"ok": True, "message": "已发：%s" % text}

    monkeypatch.setattr(fund_actions, "send_message_to_user_action", send)
    finance = FakeFinance()
    engine = make_engine(finance=finance)
    out = run(engine._slot_outage(at(14, 50), DAY, 5000,
                                  {"110011": {"name": "易方达", "nav": 1.5}}, demo=False))
    assert out["skipped"] == "fund_outage"
    assert out["executed"]["ok"] is True
    fact, idem = finance.outages[0]
    assert fact["reason"] == "fund_quote_unavailable" and fact["action"] == "send_message_to_user"
    assert fact["fund_count"] == 1 and "易方达" in fact["facts"]
    assert json.loads(engine.repo.meta_get("last_decision"))["action"] == "fund_outage"


def test_slot_outage_reallocate_demo_and_live(monkeypatch):
    async def fake_outage(*args, **kwargs):
        return {"action": "reallocate_funds", "target": "银行", "amount_cents": 5000,
                "term_months": 3, "note": "先放着"}

    monkeypatch.setattr(llm_decider, "llm_decide_outage", fake_outage)
    engine = make_engine()
    out = run(engine._slot_outage(at(14, 50), DAY, 5000, {}, demo=True))
    assert out["executed"] == {"ok": False, "action": "reallocate_funds",
                               "message": "demo_no_reallocate"}

    called = {}

    async def reallocate(plugin, target, amount, term, note):
        called.update(target=target, amount=amount, term=term, note=note)
        return {"ok": True}

    monkeypatch.setattr(fund_actions, "reallocate_funds_action", reallocate)
    out = run(engine._slot_outage(at(14, 50), DAY, 5000, {}, demo=False))
    assert called == {"target": "银行", "amount": 5000, "term": 3, "note": "先放着"}
    assert out["executed"]["ok"] is True


def test_slot_outage_skip_fund_trade_notifies(monkeypatch):
    async def fake_outage(*args, **kwargs):
        return {"action": "skip_fund_trade", "reason": "不看了", "text": "算了不看了"}

    monkeypatch.setattr(llm_decider, "llm_decide_outage", fake_outage)

    async def skip(plugin, reason):
        return {"ok": True, "message": "skipped"}

    async def send(plugin, text):
        return {"ok": True}

    monkeypatch.setattr(fund_actions, "skip_fund_trade_action", skip)
    monkeypatch.setattr(fund_actions, "send_message_to_user_action", send)
    finance = FakeFinance()
    engine = make_engine(finance=finance)
    out = run(engine._slot_outage(at(14, 50), DAY, 0, {}, demo=False))
    assert out["executed"]["notified"] is True and out["executed"]["text"] == "算了不看了"


def test_slot_outage_action_failure_and_decision_error(monkeypatch):
    async def fake_outage(*args, **kwargs):
        return {"action": "skip_fund_trade", "reason": "不看了", "text": "算了"}

    monkeypatch.setattr(llm_decider, "llm_decide_outage", fake_outage)

    async def boom(*args, **kwargs):
        raise RuntimeError("action boom")

    monkeypatch.setattr(fund_actions, "skip_fund_trade_action", boom)
    engine = make_engine()
    out = run(engine._slot_outage(at(14, 50), DAY, 0, {}, demo=False))
    assert out["executed"] == {"ok": False, "action": "skip_fund_trade",
                               "message": "action_failed"}

    async def llm_boom(*args, **kwargs):
        raise RuntimeError("llm boom")

    monkeypatch.setattr(llm_decider, "llm_decide_outage", llm_boom)
    out = run(engine._slot_outage(at(14, 50), DAY, 0, {}, demo=False))
    assert out["decision"] is None and out["executed"]["ok"] is False


def test_slot_outage_memory_and_record_error():
    class BoomMemory:
        def context_text(self, *args, **kwargs):
            raise RuntimeError("memory boom")

    finance = FakeFinance()

    async def record_boom(fact, idem=""):
        raise RuntimeError("record boom")

    finance.record_fund_outage = record_boom
    engine = make_engine(finance=finance)
    engine.plugin.finance_memory = BoomMemory()

    async def fake_outage(*args, **kwargs):
        return None

    old = llm_decider.llm_decide_outage
    llm_decider.llm_decide_outage = fake_outage
    try:
        out = run(engine._slot_outage(at(14, 50), DAY, 0, {}, demo=False))
    finally:
        llm_decider.llm_decide_outage = old
    assert out["decision"] is None
    assert engine.repo.meta_get("last_decision")


def test_simulate_fund_outage_uses_demo():
    engine = make_engine()
    captured = {}

    async def fake_outage(now, day, cash, quotes, demo=False):
        captured.update(day=day, cash=cash, demo=demo, quotes=quotes)
        return {"skipped": "fund_outage"}

    engine._slot_outage = fake_outage
    out = run(engine.simulate_fund_outage(at(14, 50)))
    assert out == {"skipped": "fund_outage"}
    assert captured["demo"] is True and captured["quotes"] == {}
    assert captured["day"] == DAY


# ---------------- 结算记录 ----------------
def test_slot_settle_noon_queues_and_records():
    engine = make_engine()
    trade = {"action": "buy", "code": "600519", "qty": 100, "reason": "抄底"}
    executed = run(engine._slot_settle("noon", trade, at(11, 35)))
    assert executed == {"ok": False, "message": "只复盘，不成交"}
    payload = json.loads(engine.repo.meta_get("last_decision"))
    assert payload["pending"] is True and payload["slot"] == "noon"
    assert json.loads(engine.repo.meta_get(PENDING_KEY))["code"] == "600519"


def test_slot_settle_close_uses_pending_plan():
    engine = make_engine()
    trade = {"action": "buy", "code": "600519", "qty": 100, "reason": "抄底"}
    engine._queue_pending(trade, at(11, 35))
    executed = run(engine._slot_settle("close", {"action": "hold", "reason": "观望"}, at(14, 50)))
    assert executed["ok"] is True
    payload = json.loads(engine.repo.meta_get("last_decision"))
    assert payload["from_pending"] is True and payload["action"] == "buy"


def test_slot_settle_close_adds_fund_entry():
    engine = make_engine()
    engine._fund_codes = {"110011"}
    engine._fund_quotes = {"110011": {"tradeable": True, "estimate": 1.2, "name": "易方达"}}
    executed = run(engine._slot_settle("close", {"action": "hold", "reason": "观望"}, at(14, 50)))
    assert executed["ok"] is False
    payload = json.loads(engine.repo.meta_get("last_decision"))
    assert payload["fund_entry"]["code"] == "110011"
    assert payload["fund_entry"]["executed"] is True