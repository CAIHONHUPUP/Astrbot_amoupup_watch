# -*- coding: utf-8 -*-
"""午间挂单：11:35 只复盘，买卖排到 14:50 执行；面板标签同步。"""
import asyncio
import datetime as dt
import json
import os

from core.agent_loop import FinanceDecision

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL = os.path.join(ROOT, "views", "守护", "index.html")
DAY = "2026-11-18"


class FakeMemory:
    async def remember(self, *args, **kwargs):
        return True


class FakeFinance:
    def __init__(self):
        self.memory = FakeMemory()

    def mood(self):
        return {"word": "平静", "score": 60}

    def _amo(self):
        return None


class FakeRepo:
    def __init__(self):
        self.meta = {}

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_set(self, key, value):
        self.meta[key] = str(value)

    def positions(self):
        return []


class FakeTrading:
    def __init__(self, ok=True, message="买入 601818 100 股，成交价 3.60，费用 5.00"):
        self.ok = ok
        self.message = message
        self.calls = []

    async def buy(self, code, qty, price=None, idem=""):
        self.calls.append(("buy", str(code), int(qty)))
        return {"ok": self.ok, "message": self.message}

    async def sell(self, code, qty, price=None, idem=""):
        self.calls.append(("sell", str(code), int(qty)))
        return {"ok": self.ok, "message": self.message}

    async def subscribe(self, code, amount_cents, nav=None, idem="", name=""):
        self.calls.append(("subscribe", str(code), int(amount_cents)))
        return {"ok": self.ok, "message": "申购成功"}

    async def redeem(self, code, shares, nav=None, idem=""):
        self.calls.append(("redeem", str(code), float(shares)))
        return {"ok": self.ok, "message": "赎回成功"}


def _dec(repo, trading):
    return FinanceDecision(None, None, repo, FakeFinance(), trading)


def _buy():
    return {"action": "buy", "code": "601818", "qty": 100,
            "reason": "心情好且空仓已久，光大银行低价银行股小仓位试一笔"}


def _hold():
    return {"action": "hold", "code": "", "reason": "空仓观望，等更好的机会"}


def _noon(dec, when=None):
    now = when or dt.datetime(2026, 11, 18, 11, 35)
    return asyncio.run(dec._slot_settle("noon", _buy(), now))


def _close(dec, decision, when=None):
    now = when or dt.datetime(2026, 11, 18, 14, 50)
    return asyncio.run(dec._slot_settle("close", decision, now))


def _pending(repo):
    raw = repo.meta_get("pending_order") or ""
    return json.loads(raw) if raw else None


def _last(repo):
    return json.loads(repo.meta_get("last_decision"))


def test_noon_buy_is_queued_but_not_executed():
    repo, trading = FakeRepo(), FakeTrading()
    out = _noon(_dec(repo, trading))
    assert out["ok"] is False and out["message"] == "只复盘，不成交"
    assert trading.calls == []
    plan = _pending(repo)
    assert plan["day"] == DAY and plan["slot"] == "noon"
    assert plan["action"] == "buy" and plan["code"] == "601818" and plan["qty"] == 100
    assert plan["queued_at"] == "11:35"
    last = _last(repo)
    assert last["pending"] is True and last["executed"] is False
    assert last["action"] == "buy"


def test_noon_hold_clears_old_pending():
    repo, trading = FakeRepo(), FakeTrading()
    _noon(_dec(repo, trading))
    assert _pending(repo)["code"] == "601818"
    now = dt.datetime(2026, 11, 18, 11, 40)
    out = asyncio.run(_dec(repo, trading)._slot_settle("noon", _hold(), now))
    assert out["ok"] is False
    assert repo.meta_get("pending_order") == ""


def test_close_replays_pending_buy_even_after_restart():
    repo, trading = FakeRepo(), FakeTrading()
    _noon(_dec(repo, trading))
    # 模拟进程重启：新的 FinanceDecision 实例读同一个库
    out = _close(_dec(repo, trading), _hold())
    assert out["ok"] is True
    assert trading.calls == [("buy", "601818", 100)]
    last = _last(repo)
    assert last["action"] == "buy" and last["executed"] is True
    assert last["from_pending"] is True and last["slot"] == "close"
    assert repo.meta_get("pending_order") == ""


def test_close_own_trade_supersedes_pending():
    repo, trading = FakeRepo(), FakeTrading()
    _noon(_dec(repo, trading))
    sell = {"action": "sell", "code": "600519", "qty": 100, "reason": "落袋"}
    out = _close(_dec(repo, trading), sell)
    assert out["ok"] is True
    assert trading.calls == [("sell", "600519", 100)]
    assert repo.meta_get("pending_order") == ""
    assert _last(repo).get("from_pending") is not True


def test_expired_pending_is_not_replayed():
    repo, trading = FakeRepo(), FakeTrading()
    stale = {"day": "2026-11-17", "slot": "noon", "action": "buy",
             "code": "601818", "qty": 100, "reason": "昨天的计划"}
    repo.meta_set("pending_order", json.dumps(stale, ensure_ascii=False))
    out = _close(_dec(repo, trading), _hold())
    assert out["ok"] is False
    assert trading.calls == []
    assert repo.meta_get("pending_order") == ""


def test_failed_pending_is_recorded_with_reason():
    repo, trading = FakeRepo(), FakeTrading(ok=False, message="余额不足，无法支付交易费用")
    _noon(_dec(repo, trading))
    _close(_dec(repo, trading), _hold())
    last = _last(repo)
    assert last["action"] == "buy" and last["executed"] is False
    assert "余额不足" in last["message"]
    assert repo.meta_get("pending_order") == ""


def test_fund_pending_replays_at_close():
    repo, trading = FakeRepo(), FakeTrading()
    dec = _dec(repo, trading)
    fund = {"action": "buy_fund", "code": "005827", "amount_cents": 50000, "reason": "加点"}
    asyncio.run(dec._slot_settle("noon", fund, dt.datetime(2026, 11, 18, 11, 35)))
    dec._fund_quotes = {"005827": {"code": "005827", "name": "易方达",
                                   "estimate": 2.1, "tradeable": True}}
    out = _close(dec, _hold())
    assert out["ok"] is True
    assert trading.calls == [("subscribe", "005827", 50000)]
    assert repo.meta_get("pending_order") == ""


def test_hold_streak_resets_when_pending_trade_fills():
    repo, trading = FakeRepo(), FakeTrading()
    dec = _dec(repo, trading)
    repo.meta_set("hold_streak", "2")
    dec._update_hold_streak(_hold(), [], {"ok": False})
    assert repo.meta_get("hold_streak") == "3"
    dec._update_hold_streak(_hold(), [], {"ok": True})
    assert repo.meta_get("hold_streak") == "0"
    dec._update_hold_streak(_buy(), [], {"ok": True})
    assert repo.meta_get("hold_streak") == "0"


def test_panel_decision_labels():
    with open(PANEL, encoding="utf-8") as fh:
        body = fh.read()
    assert "计划（14:50 待执行）" in body
    assert "已执行（午间计划）" in body
    assert "· 未成交" in body
    assert "decision.message" in body
