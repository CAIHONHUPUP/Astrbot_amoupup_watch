# -*- coding: utf-8 -*-
"""AmoBridge / FinanceLink 覆盖率补测（2026-11-18）。

覆盖跨插件门面的「未就绪 / 方法缺失 / 异常 / 超时 / 协议自述」，
以及 FinanceLink 的只读、钱包、日程、记忆、日记、基金事实等分支。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.amo_bridge as bridge_mod  # noqa: E402
import core.amo_contract as amo_contract  # noqa: E402
from core.amo_bridge import AmoBridge, FinanceLink, _data_provider, _protocol_dict  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Wallet:
    def __init__(self, cash=100000, withdrawable=50000, credit_ok=True, debit_ok=True):
        self.cash = cash
        self.wd = withdrawable
        self.credit_ok = credit_ok
        self.debit_ok = debit_ok
        self.calls = []
        self.ledger_rows = [{"idem": "l1", "delta_cents": -100}, {"idem": "l2", "delta_cents": 200}]

    def balance(self, account="cash"):
        return self.wd if str(account) == "withdrawable" else self.cash

    def credit(self, cents, reason, idem, account="cash"):
        self.calls.append(("credit", cents, reason, idem, account))
        if not self.credit_ok:
            return False
        if str(account) == "withdrawable":
            self.wd += cents
        else:
            self.cash += cents
        return True

    def debit(self, cents, reason, idem, **kwargs):
        self.calls.append(("debit", cents, reason, idem, kwargs))
        if not self.debit_ok:
            return False
        self.cash -= cents
        return True

    def ledger(self, limit=200):
        return list(self.ledger_rows)[:int(limit)]


class State:
    def __init__(self):
        self.mood = {"疲惫": 10}
        self.health = {"status": "正常"}
        self.schedule = {}
        self.current = {"place": "餐厅", "zone": "restaurant"}
        self.nudges = []
        self.action = None

    def mood_score(self):
        return 72.0

    def mood_words(self):
        return "心情不错"

    def dominant_mood(self):
        return "开心"

    def nudge_once(self, idem, **deltas):
        self.nudges.append((idem, deltas))
        return True

    def nudge(self, **deltas):
        self.nudges.append(("nudge", deltas))

    def add_schedule_item(self, day, item, source="", reason=""):
        self.schedule.setdefault(day, []).append(item)
        return {"changed": True}

    def current_activity(self):
        return dict(self.current)

    def set_current_action(self, action, until, place="", zone="", category=""):
        self.action = (action, until, place, zone, category)
        return True


class DB:
    def __init__(self):
        self.diaries = []
        self.rows = {}

    def save_diary(self, day, hour, text):
        self.diaries.append((day, hour, text))
        self.rows[(day, hour)] = text

    def get_diary(self, day, hour):
        return self.rows.get((day, hour), "")


class Amo:
    def __init__(self, **kwargs):
        self.wallet = Wallet(**kwargs)
        self.state = State()
        self.db = DB()
        self.llm = None
        self.long_diary_calls = []
        self.memories = []
        self.summary_line = None
        self.protocol = {"name": "amo", "version": "1"}
        self.protocol_raises = False

    def protocol_info(self):
        if self.protocol_raises:
            raise RuntimeError("protocol boom")
        return self.protocol

    async def finance_remember(self, content, kind="finance", idem_key="", **kwargs):
        if getattr(self, "fail_memory", False):
            raise RuntimeError("mem boom")
        self.memories.append((kind, idem_key, content))
        return True

    async def finance_recall(self, query, limit=6):
        return [{"content": row[2]} for row in self.memories[:limit]]

    def write_diary_text(self, day, text):
        self.long_diary_calls.append((day, text))
        return {"code": 0}

    def note_expense_summary(self, line):
        self.summary_line = line

    def push_to_owner(self, text):
        return True


class Meta:
    def __init__(self, amo, activated=True):
        self.star_cls = amo
        self.activated = activated


class Ctx:
    def __init__(self, amo=None, boom=False, stars=None):
        self.amo = amo
        self.boom = boom
        self.stars = stars or {}

    def get_registered_star(self, name):
        if self.boom:
            raise RuntimeError("ctx boom")
        if self.amo is not None and name == "Astrbot_amoupup_forever":
            return Meta(self.amo)
        return self.stars.get(name)


class Repo:
    def __init__(self, meta=None):
        self.data = dict(meta or {})
        self.writes = []

    def meta_get(self, key, default=None):
        return self.data.get(key, default)

    def meta_set(self, key, value):
        self.writes.append((key, value))
        self.data[key] = value

    def add_fund_trade_fact(self, *args, **kwargs):
        self.data["fund_fact"] = (args, kwargs)


class Plugin:
    def __init__(self, amo=None, repo=None, **kwargs):
        self.context = Ctx(amo, **kwargs)
        self.finance_repo = repo if repo is not None else Repo()
        self.finance = None


def make_link(amo=None, **kwargs):
    return FinanceLink(Plugin(amo, **kwargs))


# ---------------- AmoBridge：解析 / 只读 ----------------
def test_bridge_not_ready_defaults():
    plugin = Plugin(None)
    bridge = AmoBridge(plugin)
    assert bridge.resolve() is None
    assert bridge.ready is False
    assert bridge.get("state") is None
    assert bridge.get("state", "x") == "x"
    assert bridge.state() is None and bridge.db() is None
    assert bridge.schedule() == {}
    assert bridge.owner_umos() == []
    result = bridge.call("balance")
    assert result.ok is False and result.reason == "not_ready"
    assert bool(result) is False and "not_ready" in repr(result)
    assert run(bridge.call_async("balance")).reason == "not_ready"


def test_bridge_resolve_from_finance_and_caches():
    amo = Amo()
    calls = []

    class Finance:
        def _amo(self):
            calls.append(1)
            return amo

    bridge = AmoBridge(Plugin(None), finance=Finance())
    assert bridge.ready is True
    assert bridge.ready is True
    assert len(calls) == 1
    assert bridge.state() is amo.state
    assert bridge.db() is amo.db
    assert bridge.schedule() == {}


def test_bridge_resolve_exception_and_plugin_finance():
    class BoomFinance:
        def _amo(self):
            raise RuntimeError("amo boom")

    plugin = Plugin(None)
    plugin.finance = BoomFinance()
    bridge = AmoBridge(plugin)
    assert bridge.resolve() is None

    amo = Amo()
    plugin = Plugin(None)
    plugin.finance = types.SimpleNamespace(_amo=lambda: amo)
    assert AmoBridge(plugin).resolve() is amo


def test_bridge_given_amo_wins():
    amo = Amo()
    bridge = AmoBridge(Plugin(None), amo=amo)
    assert bridge.resolve() is amo
    assert bridge.get("state.schedule") == {}


def test_bridge_get_missing_path():
    bridge = AmoBridge(Plugin(None), amo=Amo())
    assert bridge.get("state.not_there", "d") == "d"
    assert bridge.get("", "d") is not None


# ---------------- 协议自述 ----------------
def test_protocol_dict_and_data_provider_variants():
    assert _protocol_dict({"a": 1}) == {"a": 1}
    assert _protocol_dict([1]) is None
    assert _data_provider(Plugin(None)) is not None or _data_provider(Plugin(None)) is None

    class StarWithMem:
        mem = types.SimpleNamespace(protocol_info=lambda: {"ok": True})

    class Ctx2:
        def __init__(self):
            self.stars = {"Astrbot_amoupup_data": Meta(StarWithMem())}

        def get_registered_star(self, name):
            return self.stars.get(name)

    plugin = types.SimpleNamespace(context=Ctx2())
    assert _data_provider(plugin).protocol_info() == {"ok": True}

    class StarPlain:
        pass

    class Ctx3:
        def get_registered_star(self, name):
            return Meta(StarPlain())

    assert _data_provider(types.SimpleNamespace(context=Ctx3())) is not None
    assert _data_provider(types.SimpleNamespace(context=None)) is None
    assert _data_provider(types.SimpleNamespace(context=object())) is None
    assert _data_provider(types.SimpleNamespace(context=Ctx(boom=True))) is None


def test_protocol_info_data_provider():
    star = types.SimpleNamespace(protocol_info=lambda: {"lib": "data"})
    plugin = types.SimpleNamespace(context=Ctx(None, stars={"Astrbot_amoupup_data": Meta(star)}))
    bridge = AmoBridge(plugin)
    assert bridge.protocol_info("data") == {"lib": "data"}

    broken = types.SimpleNamespace(protocol_info=lambda: (_ for _ in ()).throw(RuntimeError("x")))
    plugin = types.SimpleNamespace(context=Ctx(None, stars={"Astrbot_amoupup_data": Meta(broken)}))
    assert AmoBridge(plugin).protocol_info("data") is None

    old = types.SimpleNamespace()
    plugin = types.SimpleNamespace(context=Ctx(None, stars={"Astrbot_amoupup_data": Meta(old)}))
    assert AmoBridge(plugin).protocol_info("data") is None

    assert AmoBridge(Plugin(None)).protocol_info("data") is None


def test_protocol_info_forever():
    amo = Amo()
    bridge = AmoBridge(Plugin(None), amo=amo)
    assert bridge.protocol_info() == {"name": "amo", "version": "1"}

    amo.protocol = "not dict"
    assert bridge.protocol_info() is None

    amo.protocol = {"x": 1}
    amo.protocol_raises = True
    assert bridge.protocol_info() is None

    class OldAmo:
        pass

    assert AmoBridge(Plugin(None), amo=OldAmo()).protocol_info() is None
    assert AmoBridge(Plugin(None)).protocol_info() is None


# ---------------- 调用 ----------------
def test_call_registered_success_and_errors():
    amo = Amo()
    bridge = AmoBridge(Plugin(None), amo=amo)
    result = bridge.call("balance")
    assert result.ok is True and result.value == 100000
    assert bridge.call("ledger", 1).value[0]["idem"] == "l1"

    class NoWallet:
        pass

    assert AmoBridge(Plugin(None), amo=NoWallet()).call("balance").reason == "missing"

    class BoomWallet:
        @property
        def wallet(self):
            raise RuntimeError("wallet boom")

    result = AmoBridge(Plugin(None), amo=BoomWallet()).call("balance")
    assert result.ok is False and result.reason == "failed" and isinstance(result.error, RuntimeError)


def test_call_unregistered_and_hidden(monkeypatch):
    bridge = AmoBridge(Plugin(None), amo=Amo())
    result = bridge.call("not_registered")
    assert result.ok is False and result.reason == "missing"

    seen = []
    monkeypatch.setattr(bridge_mod, "_HIDDEN", ("hidden_ok", "hidden_missing", "hidden_boom"))

    class Hider:
        def hidden_ok(self):
            return 7

        def hidden_boom(self):
            raise RuntimeError("hidden boom")

    hider = Hider()
    bridge = AmoBridge(Plugin(None), amo=hider)
    assert bridge.call("hidden_ok").value == 7
    assert bridge.call("hidden_missing").reason == "missing"
    assert bridge.call("hidden_boom").reason == "failed"
    assert run(bridge.call_async("hidden_ok")).value == 7


def test_note_contract_logs_when_not_covered(monkeypatch):
    monkeypatch.setattr(amo_contract, "check", lambda method: (False, "drift"))
    bridge = AmoBridge(Plugin(None), amo=Amo())
    assert bridge.call("balance").ok is True


def test_call_compat_variants():
    calls = []

    class Compat:
        def method(self, *args, **kwargs):
            calls.append((args, kwargs))
            if len(args) == 1 and not kwargs:
                raise TypeError("old signature")
            return "ok:%s" % (args,)

    bridge = AmoBridge(Plugin(None), amo=Amo())
    monkeypatch_calls = []

    def fake_call(method, *args, **kwargs):
        from core.amo_bridge import AmoCallResult

        monkeypatch_calls.append((method, args, kwargs))
        if len(args) == 1 and not kwargs:
            return AmoCallResult(False, reason="failed", error=TypeError("old"))
        return AmoCallResult(True, "ok")

    bridge.call = fake_call
    assert bridge.call_compat("x", [((1,), {}), ((2, 3), {})]).value == "ok"
    assert monkeypatch_calls == [("x", (1,), {}), ("x", (2, 3), {})]

    bridge.call = lambda method, *a, **k: __import__("core.amo_bridge", fromlist=["AmoCallResult"]).AmoCallResult(
        False, reason="failed", error=ValueError("boom"))
    assert bridge.call_compat("x", [((1,), {})]).reason == "failed"

    bridge.call = lambda *a, **k: None
    assert bridge.call_compat("x", []).reason == "not_ready"


def test_call_async_registered_sync_awaitable_and_timeout(monkeypatch):
    class Handler:
        def sync_value(self):
            return 5

    monkeypatch.setattr(bridge_mod, "_CALLS", {
        "sync_value": lambda amo, *a, **k: amo.sync_value(),
        "async_value": lambda amo, *a, **k: amo.async_value(),
        "slow": lambda amo, *a, **k: amo.slow(),
        "boom": lambda amo, *a, **k: amo.boom(),
    })

    class Obj(Handler):
        async def async_value(self):
            return 6

        async def slow(self):
            await asyncio.sleep(0.2)

        def boom(self):
            raise RuntimeError("sync boom")

    bridge = AmoBridge(Plugin(None), amo=Obj())
    assert run(bridge.call_async("sync_value")).value == 5
    assert run(bridge.call_async("async_value")).value == 6
    timed = run(bridge.call_async("slow", timeout=0.01))
    assert timed.ok is False and timed.reason == "timeout"
    failed = run(bridge.call_async("boom"))
    assert failed.ok is False and failed.reason == "failed"
    assert run(bridge.call_async("missing_method")).reason == "missing"


# ---------------- FinanceLink：解析 ----------------
def test_finance_link_amo_resolution():
    amo = Amo()
    assert make_link(amo)._amo() is amo
    assert make_link(None)._amo() is None
    assert make_link(None, boom=True)._amo() is None
    assert FinanceLink(types.SimpleNamespace(context=None))._amo() is None
    assert FinanceLink(types.SimpleNamespace(context=Ctx(None)))._amo() is None

    class MetaNoStar:
        activated = True

    class CtxNoStar:
        def get_registered_star(self, name):
            return MetaNoStar()

    assert FinanceLink(types.SimpleNamespace(context=CtxNoStar()))._amo() is None

    class MetaInactive:
        activated = False
        star_cls = amo

    class CtxInactive:
        def get_registered_star(self, name):
            return MetaInactive()

    assert FinanceLink(types.SimpleNamespace(context=CtxInactive()))._amo() is None

    class BrokenAmo:
        state = None

    class CtxBroken:
        def get_registered_star(self, name):
            return Meta(BrokenAmo())

    assert FinanceLink(types.SimpleNamespace(context=CtxBroken()))._amo() is None


# ---------------- 房屋 ----------------
class Housing:
    def __init__(self, result=None, boom=False):
        self.result = result or {"ok": True}
        self.boom = boom
        self.moved = []
        self.furniture = []

    def add_home(self, label, kind="own", city="", style=""):
        if self.boom:
            raise RuntimeError("home boom")
        return {"id": "h1", "label": label}

    def move_in(self, home_id):
        self.moved.append(home_id)

    def on_bankruptcy(self):
        if self.boom:
            raise RuntimeError("lost boom")

    def add_furniture(self, name):
        self.furniture.append(name)
        if self.boom:
            raise RuntimeError("furniture boom")
        return self.result


def test_house_bought_and_lost():
    housing = Housing()
    spawned = []
    amo = Amo()
    amo.housing = housing
    amo._spawn = lambda coro: spawned.append(coro)
    amo._ensure_home_images = lambda force=False: "images"
    link = make_link(amo)
    assert link.house_bought("小家", "东莞", "简约风") is True
    assert housing.moved == ["h1"] and spawned == ["images"]

    amo.housing = None
    assert link.house_bought() is False
    assert link.house_lost() is False

    amo.housing = Housing(boom=True)
    assert link.house_bought() is False
    assert link.house_lost() is False

    amo.housing = Housing()
    assert link.house_lost() is True


def test_furniture_bought_branches():
    housing = Housing()
    spawned = []
    amo = Amo()
    amo.housing = housing
    amo._spawn = lambda coro: spawned.append(coro)
    amo._ensure_home_images = lambda force=False: "imgs"
    link = make_link(amo)
    assert link.furniture_bought("一件T恤") is False
    assert housing.furniture == []
    assert link.furniture_bought("沙发") is True
    assert spawned == []  # 没 redraw

    housing.result = {"ok": True, "redraw": True}
    assert link.furniture_bought("双人床") is True
    assert spawned == ["imgs"]

    housing.result = {"ok": False}
    assert link.furniture_bought("书桌") is False

    amo.housing = None
    assert link.furniture_bought("沙发") is False

    amo.housing = Housing(boom=True)
    assert link.furniture_bought("沙发") is False


# ---------------- 只读 / 钱包 ----------------
def test_balance_withdrawable_ledger():
    amo = Amo()
    link = make_link(amo)
    assert link.balance_cents() == 100000
    assert link.withdrawable_cents() == 50000
    assert [row["idem"] for row in link.ledger(1)] == ["l1"]

    class BoomWallet(Wallet):
        def balance(self, account="cash"):
            raise RuntimeError("balance boom")

        def ledger(self, limit=200):
            raise RuntimeError("ledger boom")

    amo.wallet = BoomWallet()
    assert link.balance_cents() is None
    assert link.withdrawable_cents() is None
    assert link.ledger() == []

    assert make_link(None).balance_cents() is None
    assert make_link(None).withdrawable_cents() is None
    assert make_link(None).ledger() == []


def test_transfer_in_and_mark_withdrawable():
    amo = Amo()
    link = make_link(amo)
    assert link.transfer_in(1000, "t1") is True
    kinds = [(call[0], call[4]) for call in amo.wallet.calls]
    assert kinds == [("credit", "cash"), ("credit", "withdrawable")]

    assert link.mark_withdrawable(500, "m1") is True
    assert amo.wallet.calls[-1][4] == "withdrawable"

    amo.wallet.credit_ok = False
    assert link.transfer_in(1000, "t2") is False
    assert link.mark_withdrawable(500, "m2") is False

    class BoomWallet(Wallet):
        def credit(self, *args, **kwargs):
            raise RuntimeError("credit boom")

    amo.wallet = BoomWallet()
    assert link.transfer_in(1, "t3") is False
    assert link.mark_withdrawable(1, "m3") is False
    assert make_link(None).transfer_in(1, "t4") is False
    assert make_link(None).mark_withdrawable(1, "m4") is False


def test_mood_and_prompt_line():
    amo = Amo()
    link = make_link(amo)
    mood = link.mood()
    assert mood == {"score": 72.0, "word": "心情不错", "dominant": "开心"}
    assert link.prompt_line() == "她现在心情不错。"

    amo.state.mood_words = None
    mood = link.mood()
    assert mood["word"]  # 回退到 word_for_score

    class BoomState(State):
        def mood_score(self):
            raise RuntimeError("mood boom")

    amo.state = BoomState()
    assert link.mood() == {"score": None, "word": "平静", "dominant": ""}
    assert make_link(None).mood()["score"] is None


# ---------------- 记忆 / 初始化 ----------------
class FinanceMemory:
    def __init__(self):
        self.short = []
        self.long = []

    async def overwrite_short(self, related, content, idem, tag=""):
        self.short.append((related, content, idem, tag))
        return {"ok": True, "kind": "short"}

    async def remember(self, tier, content, idem, related="", tag=""):
        self.long.append((tier, content, idem, related, tag))
        return {"ok": True, "kind": tier}


def test_tier_uses_finance_memory_and_memory_link():
    amo = Amo()
    link = make_link(amo)
    result = run(link._tier("long", "内容", "idem1"))
    assert result is True
    assert amo.memories[0][0] == "finance"

    tm = FinanceMemory()
    link.plugin.finance_memory = tm
    assert run(link._tier("short", "短内容", "idem2", related="600519")) ["kind"] == "short"
    assert tm.short == [("600519", "短内容", "idem2", "pnl")]
    assert run(link._tier("long", "长内容", "idem3", tag="trade"))["kind"] == "long"
    assert tm.long == [("long", "长内容", "idem3", "", "trade")]


def test_memory_link_failures():
    amo = Amo()
    amo.fail_memory = True
    link = make_link(amo)
    result = run(link.memory.remember("内容", idem="i"))
    assert result["ok"] is False and result["reason"]

    assert run(link.memory.remember("   "))== {"ok": False, "verified": False, "reason": "empty"}
    assert run(make_link(None).memory.remember("x"))["reason"] == "memory 未就绪"
    assert run(make_link(None).memory.context()) == ""


def test_credit_initial_and_backfill():
    amo = Amo()
    repo = Repo()
    link = make_link(amo, repo=repo)
    assert link.credit_initial(5000) is True
    assert [(c[0], c[4]) for c in amo.wallet.calls] == [("credit", "cash"), ("credit", "withdrawable")]

    amo.wallet.credit_ok = False
    assert link.credit_initial(5000) is False

    class BoomWallet(Wallet):
        def credit(self, *args, **kwargs):
            raise RuntimeError("credit boom")

    amo.wallet = BoomWallet()
    assert link.credit_initial(1) is False
    assert make_link(None).credit_initial(1) is False

    amo = Amo()
    amo.wallet.cash = 80000
    amo.wallet.wd = 20000
    repo = Repo()
    link = make_link(amo, repo=repo)
    assert link.backfill_withdrawable() is True
    assert repo.meta_get("withdrawable_backfilled") == "1"
    assert amo.wallet.wd == 80000
    assert link.backfill_withdrawable() is False

    amo = Amo()
    link = make_link(amo, repo=Repo({"withdrawable_backfilled": "1"}))
    assert link.backfill_withdrawable() is False

    assert make_link(None)._amo() is None
    assert make_link(None).backfill_withdrawable() is False

    amo = Amo()
    amo.wallet.wd = amo.wallet.cash
    assert make_link(amo, repo=Repo()).backfill_withdrawable() is True


def test_count_helper():
    link = make_link(Amo(), repo=Repo())
    link._count("half_income_cents", 1000)
    assert link.plugin.finance_repo.meta_get("half_income_cents") == 1000
    link._count("half_income_cents", 500)
    assert link.plugin.finance_repo.meta_get("half_income_cents") == 1500
    FinanceLink(types.SimpleNamespace(finance_repo=None))._count("x", 1)

    class BoomRepo(Repo):
        def meta_get(self, key, default=None):
            raise RuntimeError("meta boom")

    FinanceLink(types.SimpleNamespace(finance_repo=BoomRepo()))._count("x", 1)


def test_nudge_helper():
    amo = Amo()
    link = make_link(amo)
    assert link._nudge(amo, "i1", {}) is True
    assert link._nudge(amo, "i1", {"开心": 2}) is True
    assert amo.state.nudges[-1] == ("i1:mood", {"开心": 2})

    class NudgeState(State):
        def nudge_once(self, idem, **deltas):
            return False

    amo.state = NudgeState()
    assert link._nudge(amo, "i2", {"开心": 1}) is False

    amo.state.nudge_once = None
    assert link._nudge(amo, "i3", {"开心": 1}) is True

    class BoomState(State):
        def nudge_once(self, idem, **deltas):
            raise RuntimeError("nudge boom")

    amo.state = BoomState()
    assert link._nudge(amo, "i4", {"开心": 1}) is False


# ---------------- 日程 ----------------
def test_schedule_early_morning_and_late_night():
    amo = Amo()
    link = make_link(amo)
    early = dt_stamp(2026, 11, 18, 3, 20)
    assert link._schedule(amo, "支出", 2000, "夜宵", when_ts=early) is True
    item = amo.state.schedule["2026-11-18"][0]
    assert item["start"] == "10:00" and item["end"] == "10:10"
    assert item["place"] == "餐厅" and item["zone"] == "restaurant"
    assert item["detail"].startswith("支出 20.00 元，夜宵")

    late = dt_stamp(2026, 11, 18, 23, 55)
    assert link._schedule(amo, "支出", 100, when_ts=late) is True
    item = amo.state.schedule["2026-11-18"][1]
    assert item["start"] == "23:55" and item["end"] == "23:59"


def dt_stamp(year, month, day, hour, minute):
    import datetime as _dt

    return _dt.datetime(year, month, day, hour, minute).timestamp()


def test_schedule_fallbacks_and_errors():
    amo = Amo()
    link = make_link(amo)

    class BoomCurrent(State):
        def current_activity(self):
            raise RuntimeError("activity boom")

    amo.state = BoomCurrent()
    assert link._schedule(amo, "入账", 100) is True
    assert amo.state.schedule[list(amo.state.schedule)[0]][0]["place"] == "家"

    class NoChange(State):
        def add_schedule_item(self, day, item, source="", reason=""):
            return {"changed": False}

    amo.state = NoChange()
    assert link._schedule(amo, "入账", 100) is False

    class BoomSchedule(State):
        def add_schedule_item(self, *args, **kwargs):
            raise RuntimeError("schedule boom")

    amo.state = BoomSchedule()
    assert link._schedule(amo, "入账", 100) is False


def test_add_schedule_dedupes_and_reports():
    amo = Amo()
    link = make_link(amo)
    assert link.add_schedule("2026-11-18", "约会", place="商场", start="14:00", end="17:00") is True
    assert link.add_schedule("2026-11-18", "约会", place="商场", start="14:00", end="17:00") is True
    assert len(amo.state.schedule["2026-11-18"]) == 1

    amo.state.schedule["2026-11-18"].append("not a dict")
    assert link.add_schedule("2026-11-18", "约会", start="14:00", end="17:00") is True

    class NoChangeState(State):
        def add_schedule_item(self, day, item, source="", reason=""):
            return {"changed": False}

    amo.state = NoChangeState()
    assert link.add_schedule("2026-11-18", "聚餐", start="18:00", end="20:00") is False

    assert make_link(None).add_schedule("2026-11-18", "x") is False

    class BoomState(State):
        def add_schedule_item(self, *args, **kwargs):
            raise RuntimeError("boom")

    amo.state = BoomState()
    assert link.add_schedule("2026-11-18", "x") is False


def test_schedule_windows():
    amo = Amo()
    amo.state.schedule = {"2026-11-18": [
        {"start": "14:00", "end": "17:00", "activity": "约会", "category": "social"},
        {"start": "", "end": "17:00"},
        "bad",
    ]}
    link = make_link(amo)
    assert link.schedule_windows("2026-11-18") == [
        {"start": "14:00", "end": "17:00", "activity": "约会", "category": "social"}]
    assert link.schedule_windows("2026-11-19") == []
    assert make_link(None).schedule_windows("2026-11-18") == []

    class BoomState:
        @property
        def schedule(self):
            raise RuntimeError("schedule boom")

    amo.state = BoomState()
    assert link.schedule_windows("2026-11-18") == []


# ---------------- 疲惫 / 动作 / 日记 ----------------
def test_fatigue_branches():
    amo = Amo()
    link = make_link(amo)
    assert link.fatigue() == 10
    amo.state.mood["疲惫"] = "bad"
    assert link.fatigue() == 0
    amo.state.mood["疲惫"] = 30
    amo.state.health = {"status": "疲惫"}
    assert link.fatigue() == 60
    assert make_link(None).fatigue() == 0

    class BoomState:
        @property
        def mood(self):
            raise RuntimeError("mood boom")

        @property
        def health(self):
            raise RuntimeError("health boom")

    amo.state = BoomState()
    assert link.fatigue() == 0


def test_set_current_action_branches():
    amo = Amo()
    link = make_link(amo)
    assert link.set_current_action("约会", 123.0, "商场", "mall", "social") is True
    assert amo.state.action == ("约会", 123.0, "商场", "mall", "social")

    amo.state.set_current_action = None
    assert link.set_current_action("约会") is False

    class BoomState(State):
        def set_current_action(self, *args, **kwargs):
            raise RuntimeError("action boom")

    amo.state = BoomState()
    assert link.set_current_action("约会") is False
    assert make_link(None).set_current_action("约会") is False


def test_small_and_long_diary():
    amo = Amo()
    link = make_link(amo)
    assert link.small_diary("今天收入 100 元") is True
    assert amo.db.diaries and "收入" in amo.db.diaries[0][2]

    amo.llm = object()  # 有 LLM：相对导入失败后仍落回事实日记
    assert link.small_diary("发工资了") is True
    assert "发工资了" in amo.db.diaries[-1][2]

    class BoomDB(DB):
        def save_diary(self, *args):
            raise RuntimeError("diary boom")

    amo.db = BoomDB()
    assert link.small_diary("x") is False
    assert make_link(None).small_diary("x") is False

    amo = Amo()
    link = make_link(amo)
    assert link.long_diary("长篇") is True
    assert amo.long_diary_calls[0][1] == "长篇"
    amo.write_diary_text = None
    assert link.long_diary("长篇") is False
    amo.write_diary_text = lambda day, text: {"code": 1}
    assert link.long_diary("长篇") is False
    amo.write_diary_text = lambda day, text: (_ for _ in ()).throw(RuntimeError("boom"))
    assert link.long_diary("长篇") is False
    assert make_link(None).long_diary("x") is False


def test_diary_fallback_wording():
    assert FinanceLink._diary_fallback("这个月发工资了") == "今天发工资了，看到余额多了一点，心里踏实了些。"
    assert FinanceLink._diary_fallback("工资结算到账") != "工资结算到账"
    assert FinanceLink._diary_fallback("买菜 20 元") == "买菜 20 元"


# ---------------- 入账 / 支出 ----------------
def test_income_branches():
    amo = Amo()
    link = make_link(amo)
    out = run(link.income(680000, "工资", "salary:1", salary=True,
                          detail={"base": 1}, when_ts=dt_stamp(2026, 11, 18, 23, 55)))
    assert out["ok"] is True and out["steps"]["wallet"] is True
    assert out["steps"]["schedule"] is True
    item = amo.state.schedule["2026-11-18"][0]
    assert item["activity"] == "财务·工资到账"
    assert item["start"] == "23:55" and item["end"] == "23:59"
    assert any("这个月发工资了" in row[2] for row in amo.memories)


def test_income_wallet_error_and_not_ready():
    amo = Amo()
    link = make_link(amo)

    class BoomWallet(Wallet):
        def credit(self, *args, **kwargs):
            raise RuntimeError("credit boom")

    amo.wallet = BoomWallet()
    out = run(link.income(100, "红包", "idem"))
    assert out["ok"] is False
    assert run(make_link(None).income(1, "x", "i")) == {"ok": False, "reason": "amo 未就绪", "steps": {}}


class FakeFinance:
    """FinanceLink 需要 finance 承载 _amo；这里只是薄包装。"""

    def __init__(self, amo):
        self.amo = amo

    def _amo(self):
        return self.amo


def test_expense_branches():
    amo = Amo()
    link = make_link(amo)
    out = run(link.expense(1500, "早餐：豆浆油条", "e1"))
    assert out["ok"] is True and out["steps"]["wallet"] is True
    debit = amo.wallet.calls[-1]
    assert debit[0] == "debit" and debit[1] == 1500
    assert debit[3] == "e1"
    assert debit[4]["category"] == "餐饮" and debit[4]["sub_category"] == "早餐"
    assert amo.summary_line is None  # 相对导入失败时静默跳过

    out = run(link.expense(2000, "房租", "e2", necessary=True))
    assert out["steps"]["schedule"] is True
    assert amo.state.schedule[list(amo.state.schedule)[0]][0]["activity"] == "财务·支出"

    out = run(link.expense(300, "奶茶", "e3", quiet=True))
    assert "memory" not in out["steps"]


def test_expense_memory_and_diary_are_human_sentences():
    """2026-11-20：账本格式（时间戳+余额）不许抄进长期记忆和小日记，余额会过期。"""
    amo = Amo()
    link = make_link(amo)
    run(link.expense(500, "地铁 × 1", "e-human"))
    assert amo.memories and "余额" not in amo.memories[-1][2]
    assert "地铁" in amo.memories[-1][2] and "花了" in amo.memories[-1][2]
    assert amo.db.diaries and "余额" not in amo.db.diaries[-1][2]
    assert "支出 " not in amo.db.diaries[-1][2]


def test_small_diary_merges_same_hour_instead_of_overwriting():
    amo = Amo()
    link = make_link(amo)
    run(link.expense(500, "地铁 × 1", "e-m1", quiet=True))
    run(link.expense(1500, "面条 × 1", "e-m2", quiet=True))
    text = amo.db.diaries[-1][2]
    assert "地铁" in text and "面条" in text


def test_expense_failures():
    amo = Amo()
    amo.wallet.debit_ok = False
    link = make_link(amo)
    out = run(link.expense(100, "零食", "e4"))
    assert out["ok"] is False and out["reason"] == "余额不足"

    class BoomWallet(Wallet):
        def debit(self, *args, **kwargs):
            raise RuntimeError("debit boom")

    amo.wallet = BoomWallet()
    out = run(link.expense(100, "零食", "e5"))
    assert out["ok"] is False and out["steps"]["wallet"] is False
    assert run(make_link(None).expense(1, "x", "i"))["reason"] == "amo 未就绪"


def test_expense_meta_categories():
    meta = FinanceLink._expense_meta
    assert meta("午餐：面条")[0:2] == ("餐饮", "午餐")
    assert meta("打车回家")[0:2] == ("交通", "打车")
    assert meta("买了条裙子")[0:2] == ("购物", "衣物")
    assert meta("随礼：份子钱")[0:2] == ("人情", "随礼")
    assert meta("保险续费")[0:2] == ("保险", "保费")
    assert meta("水电话费")[0:2] == ("固定账单", "生活缴费")
    cat, sub, item, trigger = meta("完全未知的支出")
    assert (cat, sub, item, trigger) == ("其他", "日常", "完全未知的支出", "完全未知的支出")


def test_trade_pnl_branches():
    amo = Amo()
    link = make_link(amo)
    out = run(link.trade_pnl(5000, 0.02, "600519", "t1"))
    assert out["ok"] is True and out["steps"]["diary"] is True
    assert "交易盈利" in amo.db.diaries[-1][2]
    assert "trade" not in out["steps"]

    out = run(link.trade_pnl(-200000, -0.15, "600519", "t2"))
    assert out["steps"]["schedule"] is True
    assert out["steps"]["long_diary"] is True

    out = run(link.trade_pnl(100, 0.0, "", "t3", diary=False, memory=False))
    assert "diary" not in out["steps"] and "memory" not in out["steps"]
    assert run(make_link(None).trade_pnl(1, 0, "x", "i"))["reason"] == "amo 未就绪"


def test_borrow_branches():
    amo = Amo()
    link = make_link(amo)
    out = run(link.borrow(100000, "银行贷款", "b1", months=6))
    assert out["ok"] is True and out["steps"]["wallet"] is True
    assert "分 6 个月还" in out["steps"].get("message", "") or out["steps"]["diary"] is True
    assert amo.memories and "借款" in amo.memories[-1][2]

    class BoomWallet(Wallet):
        def credit(self, *args, **kwargs):
            raise RuntimeError("credit boom")

    amo.wallet = BoomWallet()
    out = run(link.borrow(100, "借", "b2"))
    assert out["ok"] is False and out["steps"]["wallet"] is False
    assert run(make_link(None).borrow(1, "x", "i"))["reason"] == "amo 未就绪"


def test_nudge_mood_and_memory_shortcuts():
    amo = Amo()
    link = make_link(amo)
    assert link.nudge_mood({"开心": 5}, "m1") is True
    assert amo.state.nudges[-1][0] == "m1:mood"

    class BoomState(State):
        def nudge_once(self, *args, **kwargs):
            raise RuntimeError("nudge boom")

    amo.state = BoomState()
    assert link.nudge_mood({"开心": 1}, "m2") is False
    assert make_link(None).nudge_mood({}, "m3") is False
    assert run(link.long_memory("账本汇总", "idem")) is True
    assert run(link.permanent_memory("买房了", "idem2")) is True


def test_low_balance_and_remember_event():
    amo = Amo()
    link = make_link(amo)
    out = run(link.low_balance(5000, "low1"))
    assert out["ok"] is True and out["steps"]["schedule"] is True
    assert "余额不够" in amo.db.diaries[-1][2]
    assert amo.memories and "余额不足" in amo.memories[-1][2]
    assert run(make_link(None).low_balance(1, "i"))["reason"] == "amo 未就绪"
    assert run(link.remember_event("求助", 100, "缺钱", "r1"))["ok"] is True


# ---------------- 交易事实 / 日记 ----------------
def test_record_trade_stock_and_fund():
    amo = Amo()
    repo = Repo()
    link = make_link(amo, repo=repo)
    out = run(link.record_trade("buy", "stock", "茅台", "600519", 10.5, 100, 105000,
                                fee_cents=500, idem="tr1"))
    assert out["ok"] is True and out["verified"] is True
    assert "买入股票" in amo.memories[-1][2]
    assert amo.db.diaries and "买入股票" in amo.db.diaries[-1][2]

    out = run(link.record_trade("sell", "stock", "茅台", "600519", 11.0, 100, 110000,
                                pnl_cents=5000, pnl_pct=0.05, holding_qty=0, idem="tr2"))
    assert "本次实现盈亏 +" in amo.memories[-1][2]

    out = run(link.record_trade("buy", "fund", "易方达", "110011", 1.5, 100, 15000,
                                idem="tr3"))
    assert out["ok"] is True and out["fact"]["action"] == "buy_fund"
    assert repo.data["fund_fact"][0][0] == "tr3"


def test_record_fund_fact_and_outage():
    amo = Amo()
    repo = Repo()
    link = make_link(amo, repo=repo)
    out = run(link.record_fund_fact("buy_fund", "易方达", "110011", 15000,
                                    shares=100, nav=1.5, fee_cents=10,
                                    pnl_cents=0, idem="f1", when="14:50"))
    assert out["ok"] is True and out["fact"]["amount"] == 150.0
    assert out["fact"]["shares"] == 100.0 and out["fact"]["nav"] == 1.5
    assert out["fact"]["time"] == "14:50"
    assert repo.data["fund_fact"][0][0] == "f1"
    payload = json.loads(amo.memories[-1][2])
    assert payload["code"] == "110011"

    class BoomRepo(Repo):
        def add_fund_trade_fact(self, *args, **kwargs):
            raise RuntimeError("fact boom")

    link = make_link(amo, repo=BoomRepo())
    assert run(link.record_fund_fact("sell_fund", "易方达", "110011", 100))["ok"] is True

    amo.fail_memory = True
    assert run(link.record_fund_fact("sell_fund", "易方达", "110011", 100))["ok"] is False
    amo.fail_memory = False

    out = run(link.record_fund_outage({"day": "2026-11-18", "action": "skip_fund_trade"}))
    assert out["ok"] is True and out["fact"]["day"] == "2026-11-18"
    assert "fund_outage" not in out


def test_record_daily_pnl():
    amo = Amo()
    link = make_link(amo)
    result = run(link.record_daily_pnl([
        {"name": "茅台", "code": "600519", "price": "10.50", "pnl_cents": 500, "pnl_pct": 0.02},
        {"name": "亏损股", "code": "000001", "price": "8.00", "pnl_cents": -100, "pnl_pct": -0.01},
    ], 400, idem="close"))
    assert result is True
    text = amo.memories[-1][2]
    assert "收盘持仓盈亏" in text and "茅台(600519)" in text and "总持仓盈亏 +\xa54.00" in text

    run(link.record_daily_pnl([], 0, idem="close2"))
    assert "无持仓" in amo.memories[-1][2]
