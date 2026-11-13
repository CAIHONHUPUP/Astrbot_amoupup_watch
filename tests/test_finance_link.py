# -*- coding: utf-8 -*-
import asyncio

from core.amo_bridge import FinanceLink
from core.memory_link import MemoryLink


class Meta:
    activated = True

    def __init__(self, amo):
        self.star_cls = amo


class Ctx:
    def __init__(self, amo):
        self.amo = amo

    def get_registered_star(self, name):
        return Meta(self.amo) if name == "Astrbot_amoupup_forever" else None


class Plugin:
    def __init__(self, amo):
        self.context = Ctx(amo)
        self.store = None


class FakeWallet:
    def __init__(self, balance=100000, credit_ok=True, debit_ok=True):
        self.balance_cents = balance
        self.credit_ok = credit_ok
        self.debit_ok = debit_ok
        self.calls = []

    def balance(self):
        return self.balance_cents

    def credit(self, cents, reason, idem, ref=""):
        self.calls.append(("credit", cents, idem))
        if self.credit_ok:
            self.balance_cents += cents
        return self.credit_ok

    def debit(self, cents, reason, idem, ref=""):
        self.calls.append(("debit", cents, idem))
        if self.debit_ok:
            self.balance_cents -= cents
        return self.debit_ok


class FakeState:
    def __init__(self, score=60):
        self.score = score
        self.nudges = []
        self.schedule = []
        self.fail_nudge = False

    def mood_score(self):
        return self.score

    def dominant_mood(self):
        return "开心"

    def mood_words(self):
        return "很开心、心情不错" if self.score >= 70 else "平静、没什么波动"

    def nudge_once(self, idem, **deltas):
        if self.fail_nudge:
            raise RuntimeError("mood boom")
        self.nudges.append((idem, deltas))
        return True

    def add_schedule_item(self, day, item, source="finance", reason=""):
        self.schedule.append(item)
        return {"changed": True}


class FakeDB:
    def __init__(self):
        self.diaries = []

    def save_diary(self, day, hour, text):
        self.diaries.append((day, hour, text))


class FakeAmo:
    def __init__(self, **kw):
        self.wallet = FakeWallet(**kw)
        self.state = FakeState()
        self.db = FakeDB()
        self.long_diaries = []
        self.memories = []
        self.fail_memory = False

    def write_diary_text(self, day, text):
        self.long_diaries.append((day, text))
        return {"code": 0, "data": {"date": day}}

    async def finance_remember(self, content, kind="finance", tier=2, idem_key=""):
        if self.fail_memory:
            raise RuntimeError("mem boom")
        self.memories.append({"kind": kind, "idem": idem_key, "content": content})
        return True

    async def finance_recall(self, query, limit=6):
        return [{"content": m["content"]} for m in self.memories[:limit]]


def make(**kw):
    amo = FakeAmo(**kw)
    return amo, FinanceLink(Plugin(amo))


def run(coro):
    return asyncio.run(coro)


def test_income_writes_wallet_mood_schedule_diary_memory():
    amo, link = make()
    res = run(link.income(680000, "工资", "salary:2026-11", salary=True))
    assert res["ok"] is True
    assert amo.wallet.calls[0][0] == "credit"
    assert amo.state.nudges and amo.state.nudges[0][0].startswith("salary:2026-11")
    item = amo.state.schedule[0]
    assert "情绪由" in item["event"] and not any(ch.isdigit() for ch in item["event"])
    assert amo.db.diaries and "到账" in amo.db.diaries[0][2]
    assert amo.memories and "发工资" in amo.memories[0]["content"]
    assert amo.memories[0]["idem"] == "salary:2026-11:mem"
    assert amo.long_diaries


def test_expense_insufficient_does_not_break():
    amo, link = make(debit_ok=False)
    res = run(link.expense(500000, "房租", "rent:2026-11", necessary=True))
    assert res["ok"] is False and res["reason"] == "余额不足"
    assert amo.state.schedule == [] and amo.db.diaries == [] and amo.memories == []


def test_trade_loss_writes_long_diary_and_memory():
    amo, link = make()
    res = run(link.trade_pnl(-200000, -0.12, "600519", "trade:1"))
    assert res["ok"] is True
    assert amo.state.nudges[0][1].get("郁闷", 0) > 0
    assert amo.long_diaries and "亏了" in amo.long_diaries[0][1]
    assert amo.memories and "实现亏损" in amo.memories[0]["content"]


def test_mood_failure_does_not_block_wallet():
    amo, link = make()
    amo.state.fail_nudge = True
    res = run(link.income(100000, "用户转账", "tx:1"))
    assert res["ok"] is True
    assert res["steps"]["mood"] is False


def test_memory_failure_does_not_break_trade():
    amo, link = make()
    amo.fail_memory = True
    res = run(link.income(100000, "用户转账", "tx:2"))
    assert res["ok"] is True
    assert res["steps"]["memory"] is False


def test_record_trade_text_has_fields_and_no_mood_numbers():
    amo, link = make()
    res = run(link.record_trade(
        "buy", "stock", "贵州茅台", "600519", 1680.0, 100,
        16800000, fee_cents=517, holding_qty=100, idem="buy:1"))
    assert res["ok"] is True and res["verified"] is True
    text = amo.memories[0]["content"]
    for token in ("买入", "贵州茅台", "600519", "100", "现价" if False else "成交价", "费用"):
        assert token in text
    assert "心情+" not in text and "心情-" not in text
    assert amo.memories[0]["kind"] == "finance"   # 记忆类型统一为 finance（交易靠 idem 前缀区分）


def test_record_daily_pnl_writes_holdings_only():
    amo, link = make()
    run(link.record_daily_pnl(
        [{"name": "贵州茅台", "code": "600519", "price": 1650.0, "pnl_cents": 500000, "pnl_pct": 0.0313}],
        500000, idem="pnl:2026-11-13"))
    text = amo.memories[0]["content"]
    assert "收盘持仓盈亏" in text and "贵州茅台" in text and "总持仓盈亏" in text


def test_memory_context_reads_back():
    amo, link = make()
    amo.memories.append({"kind": "finance", "idem": "x", "content": "持有贵州茅台 100 股"})
    text = run(link.memory_context())
    assert "持有贵州茅台" in text


def test_prompt_line_has_no_numbers():
    _, link = make()
    line = link.prompt_line()
    assert line.startswith("她现在") and not any(ch.isdigit() for ch in line)
