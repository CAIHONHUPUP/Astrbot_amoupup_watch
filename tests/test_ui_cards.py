# -*- coding: utf-8 -*-
"""面板数据组装：真实快照 / 兜底 / 文案分支。"""
import asyncio
import datetime as _dt
import json
import time
from types import SimpleNamespace

import pytest

import services.city as city_mod
from ui.cards import CardMixin

NOW = time.time()


def _today():
    return _dt.date.today()


class FakeFinance:
    def __init__(self, balance=100000, ledger=None, mood_word="平静"):
        self.balance = balance
        self.ledger_rows = ledger if ledger is not None else []
        self.mood_word = mood_word

    def balance_cents(self):
        return self.balance

    def withdrawable_cents(self):
        return 50000

    def mood(self):
        return {"score": 60, "dominant": "平静", "word": self.mood_word}

    def ledger(self, limit=50):
        return list(self.ledger_rows)[:limit]

    def _amo(self):
        day = _today().isoformat()
        return SimpleNamespace(state=SimpleNamespace(schedule={day: [
            {"category": "finance", "start": "09:00", "end": "09:10",
             "activity": "工资到账", "detail": "¥7,000"},
            {"category": "work", "start": "09:30", "end": "10:00",
             "activity": "干活", "detail": ""},
        ]}))


class FakeRepo:
    def __init__(self):
        self._positions = [{"kind": "stock", "code": "600519", "name": "茅台",
                            "qty": 100, "cost_cents": 160000, "last_cents": 165000}]
        self._trades = [
            {"side": "buy", "name": "茅台", "code": "600519", "amount_cents": 16000000,
             "fee_cents": 4500, "ts": NOW, "note": "100 股"},
            {"side": "sell", "name": "茅台", "code": "600519", "amount_cents": 16500000,
             "fee_cents": 4500, "ts": NOW},
            {"side": "weird", "name": "基金", "code": "005827", "amount_cents": 100},
        ]
        self._loans = [{"kind": "房贷", "balance_cents": 45200000, "monthly_cents": 238000,
                        "rate": 0.031, "principal_cents": 50000000, "months": 360}]
        self._quotes = [{"code": "600519", "name": "茅台", "price_cents": 165000, "prev_cents": 160000}]
        self._settlements = [{"day": _today().strftime("%Y-%m"), "kind": "rent", "payload": "300000"}]
        self.meta = {
            "last_decision": json.dumps({"slot": "close", "action": "hold"}),
            "fund_quotes": json.dumps({"005827": {"name": "易方达", "nav": 2.18, "change_pct": 1.2}}),
            "decision_done:" + _today().isoformat() + ":stock:600519": "1",
        }

    def positions(self):
        return list(self._positions)

    def trades(self, limit=20):
        return list(self._trades)[:limit]

    def loans(self):
        return list(self._loans)

    def market_quotes(self, day):
        return list(self._quotes)

    def settlements(self, month):
        return list(self._settlements)

    def settled(self, period, kind):
        return True

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = value

    def meta_keys(self, prefix):
        return [k for k in self.meta if k.startswith(prefix)]


class FakeRuntime:
    async def run_io(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def lag_stats(self):
        return "p99=0.1s"


class FakePlugin(CardMixin):
    def __init__(self):
        self.finance = FakeFinance(ledger=[{"delta_cents": -1500, "account": "cash",
                                            "ts": NOW, "reason": "面条"}])
        self.finance_repo = FakeRepo()
        self.finance_life = SimpleNamespace(salary_cents=lambda: 680000)
        self.finance_property = SimpleNamespace(state=lambda: {
            "has_house": True, "house_value": 1200000.0, "car_value": 120000.0,
            "house_price": 1500000, "down_pct": 30, "mortgage": 3580})
        self.finance_salary = SimpleNamespace(
            summary=lambda period: {"base_cents": 680000, "bonus_cents": 50000,
                                    "deduction_cents": 20000, "net_cents": 710000,
                                    "items": [{"label": "全勤", "amount_cents": 50000}]},
            thrifty_until=lambda: "2026-11-20")
        self.finance_expense = SimpleNamespace(month_summary=lambda: {
            "total_cents": 287000,
            "categories": [{"key": "food", "label": "餐饮", "amount_cents": 150000}]})
        self.finance_help = SimpleNamespace(status=lambda: {"used": 1},
                                           prompt_text=lambda: "要帮忙吗")
        self.work_events = SimpleNamespace(today_events=lambda: [{"text": "加班"}])
        self.context = None
        self.config = {"allow_test_commands": True}
        self.runtime = FakeRuntime()
        self.bank = SimpleNamespace(current_cents=lambda: 100000, fixed_cents=lambda: 200000)

    async def diag(self):
        return {"display_name": "能守护", "version": "1.0.8", "generation": "g1",
                "status": "ready", "tasks": {"count": 1, "names": ["finance_tick"]},
                "lag": {"p99": 0.1, "max": 0.2, "hits": 0},
                "store": {"events": 4, "people": 0, "edges": 0, "settlements": 6,
                          "db_mb": 0.22, "free_mb": 100, "readonly": False}}


@pytest.fixture(autouse=True)
def _stub_city(monkeypatch):
    monkeypatch.setattr(city_mod, "city_view", lambda plugin: {"city": "二线", "tier": "二线",
                                                               "factor": 1.0})


def test_snapshot_real_assembles_view():
    view = FakePlugin()._snapshot_real()
    assert view["mode"] == "real"
    assert view["positions"][0]["code"] == "600519"
    assert view["trades"][0]["kind"] == "买入"
    assert view["market"]["stocks"] and view["market"]["funds"]
    assert view["flow"] and view["flow"][0]["kind"] == "支出"
    assert view["bank"]["items"]
    assert view["life"]["has_house"] is True
    assert view["life"]["salary_net"] == 7100.0
    assert view["life"]["expense_categories"]
    assert view["wallet"]["withdrawable"] == 500.0
    assert view["plan"]["house"]["price"] == 1500000


def test_snapshot_real_without_repo():
    p = FakePlugin()
    p.finance_repo = None
    p.finance_property = None
    p.finance_salary = None
    p.finance_expense = None
    view = p._snapshot_real()
    assert view["positions"] == [] and view["trades"] == []
    assert view["market"]["stocks"] == [] and view["market"]["funds"] == []


def test_snapshot_life_falls_back_on_error():
    class BoomRepo(FakeRepo):
        def settlements(self, month):
            raise RuntimeError("db down")

    p = FakePlugin()
    p.finance_repo = BoomRepo()
    life, market = p._snapshot_life()
    assert life["salary"] == 0
    assert market["checks"] == 0


def test_snapshot_property_and_salary_and_expense_errors():
    class Boom:
        def state(self):
            raise RuntimeError("property")

        def summary(self, period):
            raise RuntimeError("salary")

        def month_summary(self):
            raise RuntimeError("expense")

    p = FakePlugin()
    boom = Boom()
    p.finance_property = boom
    p.finance_salary = boom
    p.finance_expense = boom
    view = p._snapshot_real()
    assert view["plan"]["house"]["price"] == 0
    assert "salary_net" not in view["life"]


def test_snapshot_holdings_handles_repo_errors():
    class BoomRepo(FakeRepo):
        def positions(self):
            raise RuntimeError("positions")

        def trades(self, limit=20):
            raise RuntimeError("trades")

    p = FakePlugin()
    p.finance_repo = BoomRepo()
    positions, market_value, total_pnl, trades = p._snapshot_holdings()
    assert positions == [] and trades == []
    assert market_value == 0 and total_pnl == 0


def test_snapshot_loans_handles_errors():
    class BoomRepo(FakeRepo):
        def loans(self):
            raise RuntimeError("loans")

    p = FakePlugin()
    p.finance_repo = BoomRepo()

    class BoomHelp:
        def status(self):
            raise RuntimeError("help")

    p.finance_help = BoomHelp()
    loans, debt, monthly, rate, mood, decision, help_state = p._snapshot_loans()
    assert loans == [] and debt == 0 and monthly == 0 and rate == 0.0
    assert decision == {"slot": "close", "action": "hold"}
    assert help_state == {}


def test_finance_snapshot_adds_bank():
    p = FakePlugin()
    view = p._finance_snapshot()
    assert view["bank"]["current_cents"] == 100000
    assert view["bank"]["fixed_cents"] == 200000
    assert view["wallet"]["total"] > 100000


def test_finance_provider_variants():
    provider = SimpleNamespace(snapshot=lambda: {"mode": "provider"})
    p = FakePlugin()
    p.context = SimpleNamespace(get_registered_star=lambda name: SimpleNamespace(
        activated=True, star_cls=provider))
    assert p._finance_snapshot_raw() == {"mode": "provider"}

    p.context = SimpleNamespace(get_registered_star=lambda name: None)
    assert p._finance_snapshot_raw()["mode"] == "real"

    p.context = SimpleNamespace(get_registered_star=lambda name: SimpleNamespace(activated=False))
    assert p._finance_snapshot_raw()["mode"] == "real"

    def boom(name):
        raise RuntimeError("context broken")

    p.context = SimpleNamespace(get_registered_star=boom)
    assert p._finance_snapshot_raw()["mode"] == "real"


def test_provider_snapshot_exception_falls_back():
    class BadProvider:
        def snapshot(self):
            raise RuntimeError("provider down")

    p = FakePlugin()
    p.context = SimpleNamespace(get_registered_star=lambda name: SimpleNamespace(
        activated=True, star_cls=BadProvider()))
    assert p._finance_snapshot_raw()["mode"] == "real"


def test_demo_and_empty_snapshot():
    p = FakePlugin()
    demo = p._finance_snapshot_raw(demo=True)
    assert demo["mode"] == "demo"
    assert len(demo["positions"]) == 4
    p.finance = None
    p.finance_repo = None
    empty = p._finance_snapshot_raw()
    assert empty["mode"] == "real"
    assert empty["positions"] == []


def test_fmt_finance_text_and_error():
    p = FakePlugin()
    text = p._fmt_finance()
    assert "可用余额" in text and "持有" in text
    p.finance_repo = SimpleNamespace(positions=lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert "空仓" in p._fmt_finance()


def test_format_money_branches():
    p = FakePlugin()
    assert "理财菜单" in p._format_money("")
    assert "总资产" in p._format_money("1")
    assert "茅台" in p._format_money("持仓")
    assert "房贷" in p._format_money("贷款")
    assert "工资到账" in p._format_money("日程")
    assert "持仓简报" in p._format_money("0")
    assert "没听懂" in p._format_money("随便")

    p2 = FakePlugin()
    p2.finance_repo = FakeRepo()
    p2.finance_repo._positions = []
    p2.finance_repo._loans = []
    assert "空仓" in p2._format_money("持仓")
    assert "没有贷款" in p2._format_money("贷款")
    assert "当前空仓" in p2._format_money("关闭")

    p3 = FakePlugin()
    p3.finance._amo = lambda: (_ for _ in ()).throw(RuntimeError("amo"))
    assert "今天没有财务日程" in p3._format_money("日程")


def test_handle_watch_dispatch():
    p = FakePlugin()
    assert "1.0.8" in asyncio.run(p._handle_watch(SimpleNamespace(message_str="/watch")))
    assert "可用余额" in asyncio.run(p._handle_watch(SimpleNamespace(message_str="watch finance")))
    with pytest.raises(RuntimeError):
        asyncio.run(p._handle_watch(SimpleNamespace(message_str="watch test raise")))

    p.config = {"allow_test_commands": False}
    text = asyncio.run(p._handle_watch(SimpleNamespace(message_str="watch test busy 0.1")))
    assert "allow_test_commands" in text

    p.config = {"allow_test_commands": True}
    good = asyncio.run(p._handle_watch(SimpleNamespace(message_str="watch test busy 0.1 good")))
    assert good.startswith("busy 0.1s")
    bad = asyncio.run(p._handle_watch(SimpleNamespace(message_str="watch test busy 0.1 bad")))
    assert "mode=bad" in bad
    assert "未知测试" in asyncio.run(p._handle_watch(SimpleNamespace(message_str="watch test nope")))


def test_fmt_diag_with_store():
    p = FakePlugin()
    text = p._fmt_diag({"display_name": "能守护", "version": "1.0.8", "status": "ready",
                        "generation": "g", "tasks": {"count": 1, "names": ["a"]},
                        "lag": {"p99": 0.1, "max": 0.2, "hits": 0},
                        "store": {"events": 4, "people": 0, "edges": 0, "settlements": 6,
                                  "db_mb": 0.22, "free_mb": 100, "readonly": False}})
    assert "库 events=4" in text
