# -*- coding: utf-8 -*-
"""定时任务 / LLM 工具包装 / 工具逻辑覆盖率补测（2026-11-18）。

只测分支与转发：断言返回文案、调用参数、钱包/日程副作用。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.jobs as jobs  # noqa: E402
import core.llm_tools as llm_tools  # noqa: E402
import core.tool_wrappers as tool_wrappers  # noqa: E402
import services.bank_engine as bank_engine  # noqa: E402
import services.bankruptcy_engine as bankruptcy_engine  # noqa: E402
import services.bill_flow as bill_flow  # noqa: E402
import services.date_engine as date_engine  # noqa: E402
import services.fund_market as fund_market  # noqa: E402
import services.social_engine as social_engine  # noqa: E402
import services.shopping_engine as shopping_engine  # noqa: E402
import services.gift_engine as gift_engine  # noqa: E402
import services.late_night_engine as late_night_engine  # noqa: E402
import services.nightly_memory as nightly_memory  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ====================== jobs ======================
class Repo:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.finished = []

    async def due_pending(self, today):
        return list(self.rows)

    async def finish_pending(self, idem):
        self.finished.append(idem)


class Wallet:
    def __init__(self):
        self.calls = []

    def credit(self, amount, reason, idem, account="cash"):
        self.calls.append((amount, reason, idem, account))
        return True


class Amo:
    def __init__(self):
        self.wallet = Wallet()
        self.pushed = []
        self.overtime = 0
        self.summary = None

    async def push_to_owner(self, text):
        self.pushed.append(text)

    def on_forced_overtime(self):
        self.overtime += 1

    def note_expense_summary(self, text):
        self.summary = text


class Runtime:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def run_db(self, fn, *args, timeout=0):
        self.calls.append((fn, args))
        if self.fail:
            raise RuntimeError("db boom")
        return await fn(*args) if asyncio.iscoroutinefunction(fn) else fn(*args)


class Store:
    def __init__(self):
        self.touched = 0

    def touch(self):
        self.touched += 1


class JobsPlugin:
    def __init__(self):
        self.finance_repo = Repo()
        self.trading = None
        self.runtime = Runtime()
        self.finance = type("F", (), {"_amo": lambda self: Amo()})()
        self.steps = []

    async def _guard_step(self, name, coro):
        self.steps.append(name)
        return await coro


def test_process_pending_credits_withdrawable_and_cash():
    plugin = JobsPlugin()
    amo = Amo()
    plugin.finance = type("F", (), {"_amo": lambda self: amo})()
    plugin.finance_repo = Repo([
        {"amount_cents": 1000, "kind": "withdrawable", "idem": "p1"},
        {"amount_cents": 2000, "kind": "cash_withdrawable", "idem": "p2"},
    ])
    run(jobs.process_pending(plugin))
    assert amo.wallet.calls == [
        (1000, "卖出资金可取", "p1:w", "withdrawable"),
        (2000, "fund_redeem_cash", "p2", "cash"),
        (2000, "fund_redeem_withdrawable", "p2:w", "withdrawable"),
    ]
    assert plugin.finance_repo.finished == ["p1", "p2"]


def test_process_pending_refresh_and_failures():
    plugin = JobsPlugin()

    class Trading:
        async def refresh_available(self):
            raise RuntimeError("refresh boom")

    plugin.trading = Trading()
    plugin.finance = type("F", (), {"_amo": lambda self: Amo()})()
    run(jobs.process_pending(plugin))  # 不抛

    plugin = JobsPlugin()
    plugin.finance = type("F", (), {"_amo": lambda self: None})()
    run(jobs.process_pending(plugin))  # amo 未就绪

    plugin = JobsPlugin()
    plugin.finance_repo = None
    run(jobs.process_pending(plugin))  # repo 未就绪

    plugin = JobsPlugin()
    amo = Amo()

    class BoomRepo(Repo):
        async def due_pending(self, today):
            raise RuntimeError("query boom")

    plugin.finance_repo = BoomRepo()
    plugin.finance = type("F", (), {"_amo": lambda self: amo})()
    run(jobs.process_pending(plugin))

    plugin = JobsPlugin()
    plugin.finance_repo = Repo([{"amount_cents": "bad", "kind": "withdrawable", "idem": "x"}])
    plugin.finance = type("F", (), {"_amo": lambda self: amo})()
    run(jobs.process_pending(plugin))
    assert plugin.finance_repo.finished == []


class FakeSocial:
    def __init__(self, out=None, boom=False):
        self.out = out or {"push": ["我出门啦", "", None]}
        self.boom = boom
        self.calls = []

    async def scheduled(self, now=None):
        self.calls.append(now)
        if self.boom:
            raise RuntimeError("social boom")
        return self.out


def build_tick_plugin(monkeypatch, **overrides):
    plugin = JobsPlugin()
    amo = Amo()
    social = FakeSocial()
    events = []

    monkeypatch.setattr(bill_flow, "create_due", lambda p: events.append("create_due"))
    async def overdue(p):
        events.append("overdue")
    monkeypatch.setattr(bill_flow, "overdue_tick", overdue)
    monkeypatch.setattr(bill_flow, "prompt_line", lambda p: "账单提醒")
    monkeypatch.setattr(bill_flow, "survival_line", lambda p: "生存提醒")
    async def date_tick(p):
        events.append("date")
    monkeypatch.setattr(date_engine, "tick", date_tick)
    async def nightly(p):
        events.append("nightly")
    monkeypatch.setattr(nightly_memory, "nightly_tick", nightly)
    async def late_tick(p):
        events.append("late")
    monkeypatch.setattr(late_night_engine, "tick", late_tick)
    async def shop_tick(p):
        events.append("shopping")
    async def shop_flush(p):
        events.append("shop_flush")
    monkeypatch.setattr(shopping_engine, "tick", shop_tick)
    monkeypatch.setattr(shopping_engine, "flush", shop_flush)
    async def gift_tick(p):
        events.append("gift")
    monkeypatch.setattr(gift_engine, "tick", gift_tick)
    monkeypatch.setattr(social_engine, "build_social", lambda p: social)
    async def fund_tick(p):
        events.append("fund")
    monkeypatch.setattr(fund_market, "fund_market_tick", fund_tick)
    monkeypatch.setattr(bankruptcy_engine, "build_bankruptcy",
                        lambda p: types.SimpleNamespace(check=lambda now: _async_value("bankruptcy")))
    plugin.finance = type("F", (), {"_amo": lambda self: amo,
                                    "backfill_withdrawable": lambda self: None})()
    plugin.finance_life = types.SimpleNamespace(tick=lambda: _async_value("life"))
    plugin.finance_help = None
    plugin.finance_decision = types.SimpleNamespace(tick=lambda: _async_value("decision"))
    plugin.finance_daily = types.SimpleNamespace(tick=lambda: _async_value("daily"))
    plugin.bank = types.SimpleNamespace(daily_tick=lambda: _async_value("bank"))
    plugin.work_events = None
    plugin.fortune = types.SimpleNamespace(monthly_roll=lambda now: _async_value("fortune"))
    plugin.lending = types.SimpleNamespace(repay_tick=lambda now: _async_value("lending"))
    plugin.medical = types.SimpleNamespace(
        insurance_tick=lambda now: _async_value("medical"),
        charge_schedule_visit=lambda: _async_value("hospital"))
    plugin._process_pending = lambda: _async_value("pending")
    for key, value in overrides.items():
        setattr(plugin, key, value)
    return plugin, amo, social, events


async def _async_value(name, box=None):
    if box is not None:
        box.append(name)
    return name


def test_finance_tick_full_path(monkeypatch):
    plugin, amo, social, events = build_tick_plugin(monkeypatch)
    run(jobs.finance_tick(plugin))
    assert "create_due" in events and "overdue" in events
    assert "nightly" in events and "late" in events and "shop_flush" in events
    assert "gift" in events and "fund" in events
    assert amo.summary == "账单提醒；生存提醒"
    assert amo.pushed == ["我出门啦"]
    assert plugin.steps == ["life", "date", "late_night", "shopping", "shop_flush", "gift",
                            "pending", "fund_market", "decision", "daily", "bank_daily",
                            "fortune", "lending", "medical", "hospital_visit", "bankruptcy"]


def test_finance_tick_social_error_and_no_push(monkeypatch):
    plugin, amo, social, events = build_tick_plugin(monkeypatch)
    social.boom = True
    run(jobs.finance_tick(plugin))
    assert amo.pushed == []


def test_finance_tick_nightly_and_bill_errors(monkeypatch):
    plugin, amo, social, events = build_tick_plugin(monkeypatch)
    async def boom_nightly(p):
        raise RuntimeError("nightly boom")
    monkeypatch.setattr(nightly_memory, "nightly_tick", boom_nightly)
    monkeypatch.setattr(bill_flow, "create_due", lambda p: (_ for _ in ()).throw(RuntimeError("bill boom")))
    run(jobs.finance_tick(plugin))


def test_finance_tick_builds_bank_and_work_events(monkeypatch):
    plugin, amo, social, events = build_tick_plugin(monkeypatch)

    calls = []

    class WorkEvents:
        async def daily_random(self, now):
            calls.append("random")

        async def daily_recap(self, now):
            calls.append("recap")

        async def sync_amo_sick(self, now):
            calls.append("sick")

        def today_events(self):
            return ["加班到九点"]

    plugin.work_events = WorkEvents()
    run(jobs.finance_tick(plugin))
    assert calls == ["random", "recap", "sick"]
    assert amo.overtime == 1


def test_finance_tick_work_events_errors_are_isolated(monkeypatch):
    plugin, amo, social, events = build_tick_plugin(monkeypatch)

    class WorkEvents:
        async def daily_random(self, now):
            return None

        async def daily_recap(self, now):
            return None

        async def sync_amo_sick(self, now):
            return None

        def today_events(self):
            raise RuntimeError("events boom")

    plugin.work_events = WorkEvents()
    run(jobs.finance_tick(plugin))


def test_finance_tick_outer_exception(monkeypatch):
    plugin, amo, social, events = build_tick_plugin(monkeypatch)

    def boom():
        raise RuntimeError("backfill boom")

    plugin.finance = type("F", (), {"backfill_withdrawable": boom,
                                    "_amo": lambda self: amo})()
    run(jobs.finance_tick(plugin))  # 不抛


def test_finance_tick_help_repay(monkeypatch):
    plugin, amo, social, events = build_tick_plugin(monkeypatch)
    done = []
    plugin.finance_help = types.SimpleNamespace(
        maybe_reset=lambda: done.append("reset"),
        repay_tick=lambda: _async_value("repay", done))
    run(jobs.finance_tick(plugin))
    assert "reset" in done and "repay" in done


def test_heartbeat():
    plugin = JobsPlugin()
    store = Store()
    plugin.store = store
    run(jobs.heartbeat(plugin))
    assert store.touched == 1

    plugin.store = None
    run(jobs.heartbeat(plugin))

    plugin.store = Store()
    plugin.runtime = Runtime(fail=True)
    run(jobs.heartbeat(plugin))


# ====================== tool_wrappers ======================
class Host(tool_wrappers.ToolWrappersMixin):
    pass


def test_optional_hook_without_filter(monkeypatch):
    monkeypatch.setattr(tool_wrappers, "filter", None)

    def deco(fn):
        return fn

    assert tool_wrappers._optional_hook("llm_tool")()(deco) is deco


def test_tool_wrappers_forward_llm_tools(monkeypatch):
    calls = []

    async def make(name):
        async def fn(*args, **kwargs):
            calls.append((name, args, kwargs))
            return name
        return fn

    names = ["buy_insurance_tool", "buy_gift_tool", "deposit_money_tool",
             "confirm_arrival_tool", "rent_out_property_tool", "sell_property_tool",
             "propose_date_tool", "accept_social_event_tool", "reject_social_event_tool",
             "buy_stock_tool", "sell_stock_tool", "buy_fund_tool", "sell_fund_tool"]
    host = Host()
    for name in names:
        monkeypatch.setattr(llm_tools, name, (lambda n: (lambda *a, **k: _recorded(n, a, k, calls)))(name))

    assert run(host.buy_insurance(insurance_type="重疾"))[0] == "buy_insurance_tool"
    assert run(host.buy_gift(target_name="小明", gift_name="花", price=12.5))[0] == "buy_gift_tool"
    assert run(host.deposit_money(amount=100, account_type="fixed", term_months=3))[0] == "deposit_money_tool"
    assert run(host.confirm_arrival(activity_name="吃饭"))[0] == "confirm_arrival_tool"
    assert run(host.rent_out_property(property_name="小屋", rent=1000))[0] == "rent_out_property_tool"
    assert run(host.sell_property(property_name="小屋"))[0] == "sell_property_tool"
    assert run(host.propose_date_tool(activity="火锅", time="18:00"))[0] == "propose_date_tool"
    assert run(host.accept_social_event(npc="小明", place="商场", amount=10.0, note="备注",
                                        start="14:00", end="17:00", about_user="他"))[0] == "accept_social_event_tool"
    assert run(host.reject_social_event(reason="累", note="下次"))[0] == "reject_social_event_tool"
    assert run(host.buy_stock(code="600519", qty=100, price=10.0))[0] == "buy_stock_tool"
    assert run(host.sell_stock(code="600519", qty=100, price=10.0))[0] == "sell_stock_tool"
    assert run(host.buy_fund(code="110011", amount=500.0, nav=1.5))[0] == "buy_fund_tool"
    assert run(host.sell_fund(code="110011", shares=100.0, nav=1.5))[0] == "sell_fund_tool"
    assert [c[0] for c in calls] == names


async def _recorded(name, args, kwargs, sink):
    sink.append((name, args, kwargs))
    return (name, args, kwargs)


def test_tool_wrappers_date_tools(monkeypatch):
    calls = []

    async def make_date(plugin, title, when, cents):
        calls.append(("make", title, when, cents))
        return {"message": ""}

    async def cancel_date(plugin, title, reason):
        calls.append(("cancel", title, reason))
        return {}

    async def attend_date(plugin, title, mode):
        calls.append(("attend", title, mode))
        return {"message": "出发"}

    monkeypatch.setattr(date_engine, "make_date", make_date)
    monkeypatch.setattr(date_engine, "cancel_date", cancel_date)
    monkeypatch.setattr(date_engine, "attend_date", attend_date)
    host = Host()
    assert run(host.make_date_tool(title="看电影", when="明天18:00", deposit=50)) == "好呀"
    assert calls[-1] == ("make", "看电影", "明天18:00", 5000)
    assert run(host.cancel_date_tool(title="看电影", reason="加班")) == "最近没有约好的事呀。"
    assert calls[-1] == ("cancel", "看电影", "加班")
    assert run(host.attend_date_tool(title="看电影", mode="AA")) == "出发"
    assert calls[-1] == ("attend", "看电影", "AA")


def test_tool_wrappers_fund_actions(monkeypatch):
    import core.fund_actions as fund_actions

    calls = []

    async def send(plugin, text):
        calls.append(("send", text))
        return {"ok": True, "message": "ok"}

    async def skip(plugin, reason):
        calls.append(("skip", reason))
        return {"ok": True}

    async def realloc(plugin, target, cents, term, note):
        calls.append(("realloc", target, cents, term, note))
        return {"ok": True, "message": "已挪"}

    monkeypatch.setattr(fund_actions, "send_message_to_user_action", send)
    monkeypatch.setattr(fund_actions, "skip_fund_trade_action", skip)
    monkeypatch.setattr(fund_actions, "reallocate_funds_action", realloc)
    host = Host()
    assert run(host.send_message_to_user(text="在呢")) == "（系统）已发送。"
    assert calls[-1] == ("send", "在呢")
    assert run(host.skip_fund_trade(reason="不想买")) == "（系统）已跳过基金交易。"
    assert calls[-1] == ("skip", "不想买")
    assert run(host.reallocate_funds(target="deposit_fixed", amount=12.34, term_months=3,
                                     note="先存")) == "（系统）已执行：已挪"
    assert calls[-1] == ("realloc", "deposit_fixed", 1234, 3, "先存")


def test_tool_wrappers_fund_actions_failure(monkeypatch):
    import core.fund_actions as fund_actions

    async def send(plugin, text):
        return {"ok": False, "message": "没网"}

    async def realloc(plugin, target, cents, term, note):
        return {"ok": False, "message": "钱不够"}

    monkeypatch.setattr(fund_actions, "send_message_to_user_action", send)
    monkeypatch.setattr(fund_actions, "reallocate_funds_action", realloc)
    host = Host()
    assert run(host.send_message_to_user(text="在呢")) == "（系统）没发出去：没网"
    assert run(host.reallocate_funds(target="shopping", amount=1)) == "（系统）没执行：钱不够"


def test_modify_date_tool(monkeypatch):
    async def propose(plugin, activity, when):
        return "改到 %s 了" % when

    monkeypatch.setattr(llm_tools, "propose_date_tool", propose)
    assert run(tool_wrappers.modify_date_tool(object(), activity="火锅", time="19:00")) == "改到 19:00 了"


# ====================== llm_tools ======================
class Ins:
    def __init__(self, status=None, buy_ok=True, reason="", boom=False):
        self.status_data = status or {}
        self.buy_ok = buy_ok
        self.reason = reason
        self.boom = boom
        self.poor = []
        self.bought = []

    def status(self):
        return self.status_data

    async def poor_diary(self, key):
        self.poor.append(key)

    async def buy(self, key):
        self.bought.append(key)
        if self.boom:
            raise RuntimeError("buy boom")
        return {"ok": self.buy_ok, "reason": self.reason}


class Finance:
    def __init__(self, cash=1000000, expense=None):
        self.cash = cash
        self.expense_result = expense or {"ok": True}
        self.calls = []
        self.diaries = []
        self.memories = []

    def balance_cents(self):
        return self.cash

    async def expense(self, cents, reason, idem, necessary=False):
        self.calls.append((cents, reason, idem, necessary))
        return self.expense_result

    def small_diary(self, text):
        self.diaries.append(text)

    async def permanent_memory(self, text, idem):
        self.memories.append((text, idem))
        return True


def test_buy_insurance_unknown_and_vehicle_guards():
    plugin = types.SimpleNamespace()
    assert "支持的险种" in run(llm_tools.buy_insurance_tool(plugin, "不存在"))
    assert run(llm_tools.buy_insurance_tool(plugin, "交强险")) == "她现在没有车，不用买车险。"
    assert run(llm_tools.buy_insurance_tool(plugin, "家财险")) == "她现在没有房，不用买家财险。"


def test_buy_insurance_property_state():
    ins = Ins()
    plugin = types.SimpleNamespace(
        finance_property=types.SimpleNamespace(state=lambda: {"has_car": True, "has_house": True}),
        insurance=ins, finance=Finance(cash=1000000))
    assert run(llm_tools.buy_insurance_tool(plugin, "交强险")) == "买好了：交强险"
    assert ins.bought == ["交强险"]

    class BoomProperty:
        def state(self):
            raise RuntimeError("state boom")

    plugin = types.SimpleNamespace(finance_property=BoomProperty(), insurance=Ins(),
                                   finance=Finance(cash=1000000))
    assert run(llm_tools.buy_insurance_tool(plugin, "e生保")) == "买好了：平安e生保"


def test_buy_insurance_grace_poor_and_failure():
    plugin = types.SimpleNamespace(
        insurance=Ins(status={"e生保": {"grace": True}}), finance=Finance(cash=1000000))
    assert "宽限期" in run(llm_tools.buy_insurance_tool(plugin, "e生保"))

    ins = Ins()
    plugin = types.SimpleNamespace(insurance=ins, finance=Finance(cash=1))
    message = run(llm_tools.buy_insurance_tool(plugin, "e生保"))
    assert "买不起" in message and ins.poor == ["e生保"]

    plugin = types.SimpleNamespace(insurance=Ins(buy_ok=False, reason="no_money"),
                                   finance=Finance(cash=1000000))
    assert run(llm_tools.buy_insurance_tool(plugin, "e生保")) == "没买成（no_money）"


class Bank:
    def __init__(self, ok=True, reason=""):
        self.ok = ok
        self.reason = reason
        self.calls = []

    async def deposit(self, cents, term):
        self.calls.append((cents, term))
        return {"ok": self.ok, "reason": self.reason}

    def current_cents(self):
        return 900000

    def fixed_cents(self):
        return 100000


def test_deposit_money_validation():
    plugin = types.SimpleNamespace(finance=Finance())
    assert "金额格式不对" in run(llm_tools.deposit_money_tool(plugin, "x"))
    assert "大于 0" in run(llm_tools.deposit_money_tool(plugin, 0))
    assert "account_type 只能是" in run(llm_tools.deposit_money_tool(plugin, 100, "weird"))
    assert "3 / 6 / 12 / 36" in run(llm_tools.deposit_money_tool(plugin, 100, "fixed", 5))
    assert "存不了" in run(llm_tools.deposit_money_tool(
        types.SimpleNamespace(finance=Finance(cash=1000)), 100))


def test_deposit_money_success_and_failure(monkeypatch):
    bank = Bank()
    plugin = types.SimpleNamespace(finance=Finance(), bank=bank)
    text = run(llm_tools.deposit_money_tool(plugin, 500, "current", 12))
    assert "存好了" in text and "活期" in text
    assert bank.calls == [(50000, 0)]

    bank.ok = False
    bank.reason = "no_money"
    assert "存款没成功（no_money）" in run(llm_tools.deposit_money_tool(plugin, 100, "fixed", 3))

    built = Bank()

    class P:
        pass

    plugin2 = P()
    plugin2.finance = Finance()
    monkeypatch.setattr(bank_engine, "build_bank", lambda p: built)
    assert "存好了" in run(llm_tools.deposit_money_tool(plugin2, 100))
    assert plugin2.bank is built


def test_deposit_money_bad_term_type_and_cash_error(monkeypatch):
    bank = Bank()
    plugin = types.SimpleNamespace(finance=types.SimpleNamespace(
        balance_cents=lambda: (_ for _ in ()).throw(RuntimeError("cash boom"))), bank=bank)
    assert "3 / 6 / 12 / 36" in run(llm_tools.deposit_money_tool(plugin, 100, "fixed", "bad"))
    assert "存不了" in run(llm_tools.deposit_money_tool(plugin, 100, "current", "bad"))


def test_buy_gift_validation_and_success():
    assert run(llm_tools.buy_gift_tool(types.SimpleNamespace(), "小明", "")) == "要买什么礼物呀？"
    assert run(llm_tools.buy_gift_tool(types.SimpleNamespace(), "小明", "花", "x")) == "价格不太对。"
    assert run(llm_tools.buy_gift_tool(types.SimpleNamespace(), "小明", "花", 0)) == "礼物价格要大于 0。"
    assert run(llm_tools.buy_gift_tool(types.SimpleNamespace(finance=None), "小明", "花", 1)) == "钱包未就绪。"

    finance = Finance(cash=1000)
    assert "买不起" in run(llm_tools.buy_gift_tool(
        types.SimpleNamespace(finance=finance), "小明", "花", 100))

    finance = Finance(cash=100000)
    text = run(llm_tools.buy_gift_tool(types.SimpleNamespace(finance=finance), "小明", "花", 12.5))
    assert text == "买好啦：小明 的花，花了 12 元。"
    assert finance.calls[0][0] == 1250
    assert finance.diaries and "小明" in finance.diaries[0]
    assert finance.memories and finance.memories[0][1] == "gift:小明:1250:mem"


def test_buy_gift_expense_failures():
    finance = Finance(cash=100000, expense={"ok": False})
    assert run(llm_tools.buy_gift_tool(types.SimpleNamespace(finance=finance), "小明", "花", 1)) == \
        "礼物没买成，钱包好像不够。"

    class BoomFinance(Finance):
        async def expense(self, *args, **kwargs):
            raise RuntimeError("expense boom")

    assert run(llm_tools.buy_gift_tool(types.SimpleNamespace(finance=BoomFinance()), "小明", "花", 1)) == \
        "礼物没买成，钱包好像不够。"

    class BoomMemory(Finance):
        def small_diary(self, text):
            raise RuntimeError("diary boom")

    finance = BoomMemory(cash=100000)
    assert "买好啦" in run(llm_tools.buy_gift_tool(
        types.SimpleNamespace(finance=finance), "小明", "花", 1))


class Housing:
    def __init__(self, homes=None, current=None, rent=(True, "租出去了"), sell=(True, "卖掉了")):
        self._homes = homes or []
        self._current = current or {"id": "h1"}
        self.rent_result = rent
        self.sell_result = sell
        self.rented = []
        self.sold = []

    def homes(self):
        return list(self._homes)

    def current(self):
        return self._current

    def rent_out(self, home_id, cents):
        self.rented.append((home_id, cents))
        return self.rent_result

    def sell_home(self, home_id):
        self.sold.append(home_id)
        return self.sell_result


def housing_plugin(housing):
    amo = types.SimpleNamespace(housing=housing)
    finance = types.SimpleNamespace(_amo=lambda: amo)
    return types.SimpleNamespace(finance=finance)


def test_housing_helpers_and_pick_home():
    assert llm_tools._housing_of(types.SimpleNamespace(finance=None)) is None
    assert llm_tools._housing_of(types.SimpleNamespace(
        finance=types.SimpleNamespace(_amo=lambda: None))) is None

    homes = [{"id": "h1", "name": "小家", "kind": "rent"},
             {"id": "h2", "name": "大屋", "kind": "own", "status": "idle"}]
    housing = Housing(homes, current={"id": "h1"})
    assert llm_tools._pick_home(housing, "h2")["id"] == "h2"
    assert llm_tools._pick_home(housing, "大屋")["id"] == "h2"
    assert llm_tools._pick_home(housing, "不存在") is None
    assert llm_tools._pick_home(housing)["id"] == "h2"
    assert llm_tools._pick_home(housing, only_idle=True)["id"] == "h2"
    housing._current = {"id": "h2"}
    assert llm_tools._pick_home(housing, only_idle=True) is None
    housing._homes = [{"id": "h3", "kind": "own", "status": "rented_out"}]
    assert llm_tools._pick_home(housing, only_idle=True) is None
    callable_homes = types.SimpleNamespace(homes=lambda: [{"id": "h4", "kind": "own"}],
                                           current=lambda: {"id": "h1"})
    assert llm_tools._pick_home(callable_homes)["id"] == "h4"
    housing._homes = []
    assert llm_tools._pick_home(housing) is None


def test_rent_out_property_tool():
    assert run(llm_tools.rent_out_property_tool(types.SimpleNamespace(finance=None))) == "房屋模块未就绪。"
    housing = Housing([{"id": "h2", "kind": "own", "rent_cents": 200000}])
    plugin = housing_plugin(housing)
    assert run(llm_tools.rent_out_property_tool(plugin, "h2")) == "租出去了"
    assert housing.rented == [("h2", 200000)]

    assert run(llm_tools.rent_out_property_tool(plugin, "h2", "bad")) == "租出去了"
    assert housing.rented[-1] == ("h2", 200000)

    housing.rent_result = (False, "租客不要")
    assert run(llm_tools.rent_out_property_tool(plugin, "h2")) == "没租出去：租客不要"

    housing._homes = [{"id": "h1", "kind": "rent"}]
    assert run(llm_tools.rent_out_property_tool(plugin)) == "没有可出租的闲置自有房产。"


def test_sell_property_tool():
    assert run(llm_tools.sell_property_tool(types.SimpleNamespace(finance=None))) == "房屋模块未就绪。"
    housing = Housing([{"id": "h2", "kind": "own"}])
    plugin = housing_plugin(housing)
    assert run(llm_tools.sell_property_tool(plugin, "h2")) == "卖掉了"
    assert housing.sold == ["h2"]

    housing.sell_result = (False, "没人买")
    assert run(llm_tools.sell_property_tool(plugin, "h2")) == "没卖成：没人买"

    housing._homes = []
    assert "没有可出售" in run(llm_tools.sell_property_tool(plugin))


def test_propose_date_tool(monkeypatch):
    calls = []

    async def make_date(plugin, activity, when):
        calls.append((activity, when))
        return {"ok": True, "message": "好呀"}

    monkeypatch.setattr(date_engine, "make_date", make_date)
    plugin = types.SimpleNamespace()
    assert run(llm_tools.propose_date_tool(plugin, "", "中午")) == "好呀"
    assert calls[-1] == ("吃火锅", "12:00")

    async def not_ok(plugin, activity, when):
        return {"ok": False, "message": ""}

    monkeypatch.setattr(date_engine, "make_date", not_ok)
    assert "已经约好了" in run(llm_tools.propose_date_tool(plugin, "看电影", ""))

    async def no_message(plugin, activity, when):
        return {"ok": True}

    monkeypatch.setattr(date_engine, "make_date", no_message)
    assert "说定啦" in run(llm_tools.propose_date_tool(plugin))


def test_confirm_arrival_tool(monkeypatch):
    monkeypatch.setattr(date_engine, "active_date", lambda plugin: {"title": "看电影"})
    assert "已经在约会中" in run(llm_tools.confirm_arrival_tool(types.SimpleNamespace()))

    monkeypatch.setattr(date_engine, "active_date", lambda plugin: None)
    monkeypatch.setattr(date_engine, "find_pending", lambda plugin, name: None)
    assert run(llm_tools.confirm_arrival_tool(types.SimpleNamespace())) == "现在没有在等你赴约呀。"

    monkeypatch.setattr(date_engine, "find_pending", lambda plugin, name: {"title": "看电影"})

    async def attend(plugin, title):
        return {"ok": False, "message": ""}

    monkeypatch.setattr(date_engine, "attend_date", attend)
    assert "约会没开始成" in run(llm_tools.confirm_arrival_tool(types.SimpleNamespace()))

    async def attend_ok(plugin, title):
        return {"ok": True}

    monkeypatch.setattr(date_engine, "attend_date", attend_ok)
    assert "约会已经开始" in run(llm_tools.confirm_arrival_tool(types.SimpleNamespace()))


class Social:
    def __init__(self, accept=None, reject=None):
        self.accept_result = accept or {"ok": True, "note": "去了", "cost": 1250}
        self.reject_result = reject or {"ok": True, "reason": "太累"}
        self.calls = []

    async def accept_invite(self, **kwargs):
        self.calls.append(("accept", kwargs))
        return self.accept_result

    async def reject_invite(self, **kwargs):
        self.calls.append(("reject", kwargs))
        return self.reject_result


def test_accept_social_event_tool():
    assert "没有待回的邀约" in run(llm_tools.accept_social_event_tool(types.SimpleNamespace(social=None)))
    social = Social()
    text = run(llm_tools.accept_social_event_tool(
        types.SimpleNamespace(social=social), "小明", "商场", "12.5", "备注", "14:00", "17:00", "他"))
    assert "已按她的决定结算" in text and "12.50" in text
    assert social.calls[0][1]["amount_cents"] == 1250

    social = Social(accept={"ok": False, "reason": "没钱"})
    assert "没去成：没钱" in run(llm_tools.accept_social_event_tool(
        types.SimpleNamespace(social=social), "小明", "商场", "x"))


def test_reject_social_event_tool():
    assert run(llm_tools.reject_social_event_tool(types.SimpleNamespace(social=None))) == \
        "（系统）现在没有待回的邀约。"
    social = Social()
    text = run(llm_tools.reject_social_event_tool(types.SimpleNamespace(social=social), "累", "下次"))
    assert "已经婉拒了" in text and "太累" in text

    social = Social(reject={"ok": False, "message": "没法拒绝"})
    assert "没拒绝成：没法拒绝" in run(llm_tools.reject_social_event_tool(
        types.SimpleNamespace(social=social)))


class Trading:
    def __init__(self, ok=True, boom=False):
        self.ok = ok
        self.boom = boom
        self.calls = []

    async def _do(self, action, *args, **kwargs):
        self.calls.append((action, args, kwargs))
        if self.boom:
            raise RuntimeError("trade boom")
        return {"ok": self.ok, "message": "done"}

    async def buy(self, code, qty, price, idem):
        return await self._do("buy", code, qty, price, idem)

    async def sell(self, code, qty, price, idem):
        return await self._do("sell", code, qty, price, idem)

    async def subscribe(self, code, amount, nav, idem):
        return await self._do("subscribe", code, amount, nav, idem)

    async def redeem(self, code, shares, nav, idem):
        return await self._do("redeem", code, shares, nav, idem)


def test_trade_tools_success_paths():
    trading = Trading()
    plugin = types.SimpleNamespace(trading=trading)
    assert "交易成功" in run(llm_tools.buy_stock_tool(plugin, None, "600519", 100, 10.0))
    assert trading.calls[-1][0] == "buy"
    assert "交易成功" in run(llm_tools.sell_stock_tool(plugin, None, "600519", 100, 10.0))
    assert trading.calls[-1][0] == "sell"
    assert "交易成功" in run(llm_tools.buy_fund_tool(plugin, None, "110011", 500.0, 1.5))
    assert trading.calls[-1][1][1] == 50000
    assert "交易成功" in run(llm_tools.sell_fund_tool(plugin, None, "110011", 100.0, 1.5))
    assert trading.calls[-1][0] == "redeem"


def test_trade_tools_guards_and_errors():
    assert "交易模块未就绪" in run(llm_tools.buy_stock_tool(types.SimpleNamespace(trading=None), None, "600519"))
    plugin = types.SimpleNamespace(trading=Trading())
    assert "缺少股票/基金代码" in run(llm_tools.buy_stock_tool(plugin, None, "  "))
    assert "未知交易动作" in run(llm_tools._trade_tool(plugin, None, "unknown", "600519"))
    assert "交易失败" in run(llm_tools.buy_stock_tool(types.SimpleNamespace(trading=Trading(boom=True)),
                                                     None, "600519"))
    assert "交易被拒绝" in run(llm_tools.buy_stock_tool(types.SimpleNamespace(trading=Trading(ok=False)),
                                                        None, "600519"))
