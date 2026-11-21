# -*- coding: utf-8 -*-
"""到点扣款 + 面板别把未来的约会写成「已经吃完」。

2026-11-20 线上：09:56 答应柳如烟 18:30 的火锅，当场扣了 200，
「最近关系动态」还把事后的回忆（note）直接贴出来 —— 像已经吃完。
"""
import asyncio
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services.social_engine import SocialEngine, panel_events  # noqa: E402
from tests.social_fakes import FakeFinance, FakePlugin, FakeRepo  # noqa: E402

DAY = "2026-11-21"
START = dt.datetime(2026, 11, 21, 14, 0, 0).timestamp()
IDEM = "social_event:柳如烟:2026-11-21:14:00"


def _invite(**over):
    inv = {"token": "soc-charge", "kind": "meal", "topic": "下班吃火锅", "npc": "柳如烟",
           "relation": "闺蜜", "circle": "friend", "place": "楼下小馆",
           "day": DAY, "start": "14:00", "end": "17:00", "zone": "restaurant",
           "amount_cents": 20000, "ask": "晚上一起吃火锅？"}
    inv.update(over)
    return inv


def _engine(cash=500000):
    repo, fin = FakeRepo(), FakeFinance(cash=cash)
    return SocialEngine(FakePlugin(), repo, fin), repo, fin


def test_accept_does_not_charge_until_event_starts():
    eng, repo, fin = _engine()
    eng.set_invite(_invite())
    res = asyncio.run(eng.accept_invite(now=dt.datetime(2026, 11, 21, 10, 0), note="好呀，下班见"))
    assert res["ok"] and res["steps"]["wallet"] == "pending"
    assert fin.expenses == [] and fin.cash == 500000
    assert [c["idem"] for c in eng.pending_charges()] == [IDEM]


def test_settle_before_start_keeps_money():
    eng, repo, fin = _engine()
    eng.set_invite(_invite())
    asyncio.run(eng.accept_invite(now=dt.datetime(2026, 11, 21, 10, 0), note="好呀"))
    out = asyncio.run(eng.settle_charges(now=START - 60))
    assert out["paid"] == 0 and out["pending"] == 1
    assert fin.expenses == [] and len(eng.pending_charges()) == 1


def test_settle_at_start_charges_once_with_event_idem():
    eng, repo, fin = _engine()
    eng.set_invite(_invite())
    asyncio.run(eng.accept_invite(now=dt.datetime(2026, 11, 21, 10, 0), note="好呀"))
    out = asyncio.run(eng.settle_charges(now=START))
    assert out["paid"] == 1 and out["pending"] == 0
    assert fin.cash == 500000 - 20000
    assert len(fin.expenses) == 1 and fin.expenses[0]["idem"] == IDEM
    # 再扫一遍不会重复扣
    assert asyncio.run(eng.settle_charges(now=START + 600))["paid"] == 0
    assert len(fin.expenses) == 1


def test_settle_skips_when_plan_was_cancelled():
    eng, repo, fin = _engine()
    eng.set_invite(_invite())
    asyncio.run(eng.accept_invite(now=dt.datetime(2026, 11, 21, 10, 0), note="好呀"))
    fin.windows[DAY] = [{"start": "14:00", "end": "17:00", "activity": "在家躺着"}]
    fin.schedules = []                   # 她改口不去了，原来的约会块也没了
    out = asyncio.run(eng.settle_charges(now=START))
    assert out["dropped"] == 1 and fin.expenses == [] and fin.cash == 500000


def test_accept_freezes_the_money():
    """第七条：答应时把钱冻住——余额不变，但别的花销动不了。"""
    eng, repo, fin = _engine()
    eng.set_invite(_invite())
    asyncio.run(eng.accept_invite(now=dt.datetime(2026, 11, 21, 10, 0), note="好呀"))
    assert fin.cash == 500000 and fin.expenses == []
    assert fin.held == 20000
    assert fin.holds.get("hold:social_event:柳如烟:2026-11-21:14:00") == 20000
    assert fin.available_cents() == 480000
    charge = eng.pending_charges()[0]
    assert str(charge.get("hold_idem") or "").startswith("hold:")


def test_settle_consumes_and_releases_the_hold():
    eng, repo, fin = _engine()
    eng.set_invite(_invite())
    asyncio.run(eng.accept_invite(now=dt.datetime(2026, 11, 21, 10, 0), note="好呀"))
    asyncio.run(eng.settle_charges(now=START))
    assert fin.held == 0 and fin.holds == {}
    assert fin.cash == 480000
    assert fin.expenses and fin.expenses[0]["amount_cents"] == 20000


def test_cancel_releases_the_hold():
    eng, repo, fin = _engine()
    eng.set_invite(_invite())
    asyncio.run(eng.accept_invite(now=dt.datetime(2026, 11, 21, 10, 0), note="好呀"))
    fin.schedules = []
    fin.windows[DAY] = [{"start": "14:00", "end": "17:00", "activity": "在家躺着"}]
    asyncio.run(eng.settle_charges(now=START))
    assert fin.held == 0 and fin.holds == {}
    assert fin.expenses == []


def test_accept_rejects_when_money_already_held():
    eng, repo, fin = _engine()
    fin.held = 499000          # 只剩 1000 可花
    eng.set_invite(_invite())
    res = asyncio.run(eng.accept_invite(now=dt.datetime(2026, 11, 21, 10, 0), note="好呀"))
    assert res["ok"] is False and res["reason"] == "insufficient"
    assert fin.schedules == [] and eng.pending_charges() == []


def test_short_money_pays_what_it_can_and_owes_the_rest():
    eng, repo, fin = _engine()
    eng.set_invite(_invite())
    asyncio.run(eng.accept_invite(now=dt.datetime(2026, 11, 21, 10, 0), note="好呀"))
    fin.cash = 1000                      # 到点时钱已经花掉了
    out = asyncio.run(eng.settle_charges(now=START))
    assert out["paid"] == 1 and out["pending"] == 0
    assert fin.cash == 0
    assert len(fin.expenses) == 1 and fin.expenses[0]["amount_cents"] == 1000
    assert repo.npc["柳如烟"]["money_owed"] == 19000
    assert fin.diaries and "欠" in fin.diaries[-1]
    evt = [r for r in repo.log if r["kind"] == "social:accept"][-1]
    assert evt["payload"]["occurred"] is True
    assert evt["payload"]["unpaid_cents"] == 19000


def test_short_money_with_empty_wallet_records_debt_only():
    eng, repo, fin = _engine()
    eng.set_invite(_invite())
    asyncio.run(eng.accept_invite(now=dt.datetime(2026, 11, 21, 10, 0), note="好呀"))
    fin.cash = 0
    out = asyncio.run(eng.settle_charges(now=START))
    assert out["paid"] == 0 and out["dropped"] == 1 and fin.expenses == []
    assert repo.npc["柳如烟"]["money_owed"] == 20000
    assert eng.pending_charges() == []


def test_borrow_is_still_paid_on_the_spot():
    eng, repo, fin = _engine()
    eng.set_invite(_invite(kind="borrow", topic="借钱周转", amount_cents=30000))
    res = asyncio.run(eng.accept_invite(now=dt.datetime(2026, 11, 21, 10, 0), note="先拿着用"))
    assert res["ok"] and res["steps"]["wallet"] is True
    assert fin.cash == 500000 - 30000 and eng.pending_charges() == []


def test_panel_shows_plan_not_recap_for_future_event():
    """线上那条：events 只有 day 列、payload 里没有 day，就被当成「已经吃完」。"""
    row = {"id": 49, "kind": "social:accept", "day": "2099-01-01",
           "created_at": 1.0,
           "payload": {"text": "行，下班六点半楼下小馆见，我先去泡杯咖啡。",
                       "npc": "柳如烟", "status": "accepted",
                       "start": "18:30", "end": "20:00",
                       "note": "今天累归累，还是跟如烟吃了火锅，听她吐槽冷战。"}}
    text = panel_events([row])[0]["display"]
    assert text == row["payload"]["text"]
    assert "吃了" not in text


def test_deferred_accept_writes_future_tense_schedule_detail():
    """未来的约：日程里只能写「说好要去」，不能先挂上她事后的回忆。"""
    eng, repo, fin = _engine()
    eng.set_invite(_invite())
    asyncio.run(eng.accept_invite(now=dt.datetime(2026, 11, 21, 10, 0), note="今天累归累，还是跟如烟吃了火锅"))
    detail = str(fin.schedules[0]["detail"])
    assert "吃了" not in detail and detail.startswith("答应了柳如烟")


def test_record_stores_day_in_payload():
    eng, repo, fin = _engine()
    eng.set_invite(_invite())
    asyncio.run(eng.accept_invite(now=dt.datetime(2026, 11, 21, 10, 0), note="好呀"))
    evt = [r for r in repo.log if r["kind"] == "social:accept"][-1]
    assert evt["payload"]["day"] == DAY
