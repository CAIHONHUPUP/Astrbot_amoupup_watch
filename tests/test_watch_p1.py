# -*- coding: utf-8 -*-
"""Phase 14 守护 P1：bridge 调用层 / date 收尾一致性 / social 邀约生命周期。

只测纯规则与降级路径，不碰线上库、不写 plugin_data。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import amo_contract  # noqa: E402
from core.amo_bridge import AmoBridge  # noqa: E402
from services import date_engine as de  # noqa: E402
from services import date_rules as dr  # noqa: E402
from services import social_rules as sr  # noqa: E402
from services.social_engine import SocialEngine  # noqa: E402
from tests.social_fakes import FakeFinance as SFakeFinance  # noqa: E402
from tests.social_fakes import FakePlugin as SFakePlugin  # noqa: E402
from tests.social_fakes import FakeRepo as SFakeRepo  # noqa: E402

SAT = dt.datetime(2026, 11, 21, 10, 0)


# ---------------------------------------------------------------- 假 amo / 假插件
class FakeDB:
    def __init__(self):
        self.kv = {}
        self.writes = []

    def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    def kv_set(self, key, value):
        self.kv[key] = value
        self.writes.append(key)

    def save_diary(self, *a, **k):
        return True


class FakeState:
    def __init__(self):
        self.schedule = {}
        self.added = []
        self.commits = []
        self.order = []

    def current_activity(self):
        return {"place": "楼下小馆", "category": "social"}

    def add_schedule_item(self, day, item, source="", reason=""):
        self.added.append(dict(item))
        self.order.append("add_schedule_item")

    def commit_schedule_change(self, day, items, old_items=None, source="", reason=""):
        self.commits.append((day, [dict(x) for x in items], source, reason))
        self.schedule[day] = [dict(x) for x in items]
        self.order.append("commit_schedule_change")


class FakeAmo:
    def __init__(self):
        self.db = FakeDB()
        self.state = FakeState()
        self.calls = []
        self.finished = None
        self.ending = None
        self.action = None
        self.mood = None
        self.fact = None
        self.last_event = None
        self.hunger = []

    def finish_date_session(self, title="", reason="", now=None):
        self.calls.append("finish_date_session")
        self.finished = (title, reason)
        self.state.order.append("finish_date_session")

    def cancel_date_session(self, reason="", now=None):
        self.calls.append("cancel_date_session")

    def date_session(self, now=None):
        return {"phase": "none"}

    def date_ending(self, now=None):
        return {"sent": False}

    def clear_date_ending(self):
        self.calls.append("clear_date_ending")

    def clear_date_pause(self):
        self.calls.append("clear_date_pause")

    def set_date_pause(self, minutes=15):
        self.calls.append("set_date_pause")

    def is_dating(self, now=None):
        return False

    def set_date_mood(self, *a, **k):
        self.mood = a

    def set_last_date_event(self, *a, **k):
        self.last_event = a

    def set_date_prep(self, *a, **k):
        return True

    def set_date_incident(self, *a, **k):
        return True

    def set_date_fact(self, *a, **k):
        self.fact = a

    def set_date_ending(self, fact, reason=""):
        self.ending = (fact, reason)

    def set_current_action(self, *a, **k):
        self.action = a

    def set_date_now(self, *a, **k):
        self.calls.append("set_date_now")

    def set_date_home(self, *a, **k):
        self.calls.append("set_date_home")

    def add_hunger(self, *a, **k):
        self.hunger.append(a)


class FakeRepo:
    def __init__(self):
        self.data = {}

    def meta_get(self, key, default=None):
        return self.data.get(key, default)

    def meta_set(self, key, value):
        self.data[key] = value


class FakeFinance:
    def __init__(self, amo):
        self.amo = amo
        self.diaries = []
        self.memories = []
        self.moods = []

    def _amo(self):
        return self.amo

    def balance_cents(self):
        return 500000

    def fatigue(self):
        return 20

    def mood(self):
        return {"word": "开心"}

    def nudge_mood(self, *a, **k):
        self.moods.append(a)

    def small_diary(self, text):
        self.diaries.append(text)

    async def permanent_memory(self, text, idem):
        self.memories.append(text)

    async def expense(self, *a, **k):
        return {"ok": True}


_UNSET = object()


def _date_plugin(amo=_UNSET):
    class Plugin:
        pass

    p = Plugin()
    p.finance_repo = FakeRepo()
    p.finance = FakeFinance(FakeAmo() if amo is _UNSET else amo)
    return p


def _dating(p, now=SAT):
    de._set_active(p, {"title": "逛街", "status": "dating",
                       "start": now.timestamp() - 3600,
                       "end": now.timestamp() + 3600})


# ---------------------------------------------------------------- bridge / 契约
def test_amo_bridge_not_ready_is_degraded():
    p = _date_plugin(amo=None)
    result = AmoBridge(p).call("set_date_mood", "x", 0)
    assert result.ok is False and result.reason == "not_ready"
    assert AmoBridge(p).ready is False


def test_amo_bridge_missing_method_is_degraded():
    class Bare:
        pass

    p = _date_plugin(amo=Bare())
    result = AmoBridge(p).call("set_date_mood", "x", 0)
    assert result.ok is False and result.reason == "missing"


def test_amo_bridge_exception_is_isolated():
    p = _date_plugin()

    def boom(*a, **k):
        raise RuntimeError("amo 炸了")

    p.finance.amo.set_date_mood = boom
    result = AmoBridge(p).call("set_date_mood", "x", 0)
    assert result.ok is False and result.reason == "failed"
    assert isinstance(result.error, RuntimeError)


def test_amo_bridge_contract_version_is_readonly():
    before = {}
    for name in amo_contract.PROVIDER_FILES:
        path = amo_contract.snapshot_path(name)
        with open(path, "rb") as fh:
            before[name] = hashlib.sha1(fh.read()).hexdigest()
    summary = amo_contract.describe()
    after = {}
    for name in amo_contract.PROVIDER_FILES:
        path = amo_contract.snapshot_path(name)
        with open(path, "rb") as fh:
            after[name] = hashlib.sha1(fh.read()).hexdigest()
    assert before == after, "契约快照只读：读取/校验不得改写文件"
    assert summary["count"]["forever"] > 0
    assert len(summary["version"]["forever"]) == 12


def test_watch_calls_only_snapshot_methods():
    surface = amo_contract.methods("forever")
    drift = set(amo_contract.KNOWN_DRIFT)
    path = os.path.join(ROOT, "core", "amo_bridge.py")
    text = open(path, encoding="utf-8").read()
    called = set()
    for match in re.finditer(r"\bamo(?:\.[A-Za-z_]\w*)*\.([A-Za-z_]\w*)\s*\(", text):
        called.add(match.group(1))
    assert called, "调用层应当有真实的 amo.xxx 调用点"
    unknown = called - set(surface) - drift
    assert not unknown, "调用了快照外、也非已知漂移的方法：%s" % sorted(unknown)


def test_business_code_never_touches_amo_directly():
    for rel in ("services/date_engine.py", "services/social_engine.py"):
        path = os.path.join(ROOT, rel)
        text = open(path, encoding="utf-8").read()
        hits = re.findall(r"\b(amo|ama)\s*\.\s*[A-Za-z_]\w*\s*\(", text)
        assert not hits, "%s 还直摸 amo：%s" % (rel, hits[:5])


def test_known_drift_is_cleared_and_covered():
    """Phase 15：两个真实漂移已经补进快照，KNOWN_DRIFT 必须为空。"""
    surface = amo_contract.methods("forever")
    assert set(amo_contract.KNOWN_DRIFT) == set()
    for name in ("set_date_ending", "set_last_date_event"):
        assert name in surface, "%s 必须已经收录进快照" % name


# ---------------------------------------------------------------- date
def test_finish_date_duplicate_is_idempotent():
    p = _date_plugin()
    _dating(p)
    with mock.patch.object(de, "_set_active", lambda *a, **k: None):
        first = asyncio.run(de.finish_date(p, SAT, reason="user_done"))
        second = asyncio.run(de.finish_date(p, SAT, reason="user_done"))
    assert first.get("duplicate") is not True
    assert second.get("duplicate") is True
    assert len(p.finance.memories) == 1, "重复收尾不能多写一份共同记忆"


def test_finish_truncates_ongoing_and_drops_future_blocks():
    p = _date_plugin()
    _dating(p)
    p.finance.amo.state.schedule["2026-11-21"] = [
        {"start": "19:00", "end": "23:00", "activity": "和老公逛街", "category": "social"},
        {"start": "22:00", "end": "23:30", "activity": "和老公电影", "category": "social"},
    ]
    now = dt.datetime(2026, 11, 21, 20, 0)
    asyncio.run(de.finish_date(p, now, reason="user_done"))
    rows = p.finance.amo.state.schedule["2026-11-21"]
    assert rows[0]["end"] == "20:00"
    assert all("电影" not in str(r.get("activity")) for r in rows), "约会结束不该再挂未来的和老公块"


def test_finish_truncates_before_date_home():
    p = _date_plugin()
    _dating(p)
    p.finance.amo.state.schedule["2026-11-21"] = [
        {"start": "19:00", "end": "23:00", "activity": "和老公逛街", "category": "social"}]
    asyncio.run(de.finish_date(p, dt.datetime(2026, 11, 21, 20, 0), reason="user_done"))
    order = p.finance.amo.state.order
    assert order.index("commit_schedule_change") < order.index("finish_date_session")
    assert order.index("finish_date_session") < order.index("add_schedule_item")


def test_finish_clears_ghost_state():
    p = _date_plugin()
    _dating(p)
    amo = p.finance.amo
    for key in ("date_pause", "date_preview", "date_meal", "date_timer"):
        amo.db.kv[key] = {"x": 1}
    asyncio.run(de.finish_date(p, SAT, reason="user_done"))
    for key in ("date_pause", "date_preview", "date_meal", "date_timer"):
        assert amo.db.kv[key] == {}
    assert "clear_date_ending" in amo.calls


def test_pause_and_resume_use_session_api():
    p = _date_plugin()
    _dating(p)
    out = asyncio.run(de.handle_date_text(p, "老婆我去个洗手间", SAT))
    assert out.get("action") == "pause"
    assert "set_date_pause" in p.finance.amo.calls
    asyncio.run(de.handle_date_text(p, "我回来了", SAT))
    assert "clear_date_pause" in p.finance.amo.calls


def test_interrupt_cancels_session():
    p = _date_plugin()
    _dating(p)
    out = asyncio.run(de.handle_date_text(p, "老板叫我回去加班", SAT))
    assert out.get("action") == "interrupt"
    assert "cancel_date_session" in p.finance.amo.calls
    assert p.finance.amo.action == ("", 0)


def test_date_rules_window_boundaries_are_pure():
    now = dt.datetime(2026, 11, 14, 18, 30)
    assert dr.parse_when("18:40", now) == dt.datetime(2026, 11, 14, 18, 40)
    assert dr.parse_when("18:20", now) == dt.datetime(2026, 11, 14, 19, 0)
    assert dr.parse_when("17:00", now) == dt.datetime(2026, 11, 15, 17, 0)
    assert dr.human_when(dt.datetime(2026, 11, 15, 18, 30), now) == "明天晚上六点半左右"
    rows = [{"start": "18:00", "end": "21:00", "activity": "和老公火锅", "category": "social"}]
    patched, changed = dr.truncate_finished_blocks(rows, dt.datetime(2026, 11, 14, 19, 0))
    assert changed and patched[0]["end"] == "19:00"


def test_pay_mode_is_pure_and_unchanged():
    assert dr.pay_mode("b", 0, "火锅", 35000) == (35000, "her", True)
    assert dr.pay_mode("aa", 0, "火锅", 35000) == (17500, "aa", False)
    assert dr.pay_mode("", 30000, "火锅", 35000) == (5000, "host", False)
    assert dr.pay_mode("", 0, "火锅", 35000) == (35000, "her", False)


# ---------------------------------------------------------------- social
def _social_engine(cash=500000, amo=None):
    repo, fin = SFakeRepo(), SFakeFinance(cash=cash, amo=amo)
    plugin = SFakePlugin()
    return SocialEngine(plugin, repo, fin), repo, fin


def _invite(token="soc-a", **over):
    inv = {"token": token, "kind": "shopping", "topic": "逛街", "npc": "柳如烟",
           "relation": "闺蜜", "circle": "friend", "place": "商场",
           "day": "2026-11-21", "start": "14:00", "end": "17:00", "zone": "mall",
           "amount_cents": 27522, "ask": "明天下午有空不"}
    inv.update(over)
    return inv


def test_expired_invite_is_cleared_and_unblocks_can_invite():
    eng, repo, fin = _social_engine()
    # 过期时间要相对 SAT 算：用 time.time() 会在真实时间过了 10:00 后假红
    eng.set_invite(_invite(expires=SAT.timestamp() - 10))
    ok, reason = eng.can_invite(SAT)
    assert ok is True and reason == ""
    assert eng.pending_invite() is None, "过期邀约必须清掉，否则冷却闸永远卡 pending"


def test_accept_expired_invite_is_rejected():
    eng, repo, fin = _social_engine()
    eng.set_invite(_invite(expires=SAT.timestamp() - 10))
    res = asyncio.run(eng.accept_invite(note="好呀"))
    assert res["ok"] is False and res["reason"] == "no_invite"
    assert fin.cash == 500000 and fin.expenses == []


def test_create_invite_lock_prevents_duplicate_compose():
    eng, repo, fin = _social_engine()
    eng._inviting = True
    out = asyncio.run(eng.create_invite(now=SAT, kind="meal", npc="妈妈"))
    assert out["ok"] is False and out["reason"] == "busy_compose"
    assert eng.pending_invite() is None


def test_duplicate_invite_same_event_is_not_replaced():
    eng, repo, fin = _social_engine()
    eng.set_invite(_invite(token="old"))
    live = eng._pending_for_decision(SAT)
    assert live and sr.same_invite(live, _invite(token="new"))
    assert not sr.same_invite(live, _invite(token="new", start="19:00"))


def test_collision_reschedule_keeps_one_idem():
    eng, repo, fin = _social_engine()
    fin.windows = {"2026-11-21": [{"start": "14:00", "end": "17:00", "activity": "上班"}]}
    eng.set_invite(_invite())
    # 用固定的周六上午做「答应」时刻：不写 now 会拿真实时间，过了改期后的 14:00
    # 就会走「到点当场扣钱」，这个用例在下午跑必红（时间炸弹）。
    res = asyncio.run(eng.accept_invite(note="好呀", now=SAT))
    assert res["ok"] is True
    assert res["schedule"]["day"] == "2026-11-22"
    # 答应时不扣钱；到改期后的 14:00 才扣，幂等键仍按原始邀约日子算
    assert fin.expenses == [] and eng.pending_charges()
    assert str(eng.pending_charges()[0]["idem"]).startswith(
        "social_event:柳如烟:2026-11-21:14:00")
    asyncio.run(eng.settle_charges(now=dt.datetime(2026, 11, 22, 14, 0, 0).timestamp()))
    assert fin.expenses and fin.expenses[0]["idem"].startswith(
        "social_event:柳如烟:2026-11-21:14:00")


def test_expire_marks_invite_resolved():
    eng, repo, fin = _social_engine()
    eng.set_invite(_invite(token="soc-exp", expires=SAT.timestamp() - 1))
    out = eng.expire_invite(SAT)
    assert out["reason"] == "expired"
    assert eng.is_resolved("soc-exp") and eng.pending_invite() is None


def test_busy_state_falls_back_to_watch_active_date():
    repo, fin = SFakeRepo(), SFakeFinance(amo=None)
    plugin = SFakePlugin()
    plugin.finance_repo = repo
    repo.meta["date:active"] = json.dumps({"status": "dating", "title": "逛街"})
    eng = SocialEngine(plugin, repo, fin)
    assert eng.busy_state(SAT) == "dating"


def test_invite_lifecycle_pure_rules():
    inv = _invite(expires=SAT.timestamp() - 1)
    assert sr.invite_status(inv, SAT) == sr.EXPIRED
    assert sr.live_invite(inv, SAT) is None
    assert sr.invite_status(_invite(), SAT) == sr.PENDING
    assert sr.is_terminal(sr.ACCEPTED) and not sr.is_terminal(sr.PENDING)
    assert sr.invite_idem("柳如烟", "2026-11-21", "14:00") == \
        "social_event:柳如烟:2026-11-21:14:00"


def test_cooldown_decision_boundaries():
    assert sr.can_invite_decision(pending=True, hour=10) == (False, "pending")
    assert sr.can_invite_decision(pending=False, hour=8) == (False, "quiet_hours")
    assert sr.can_invite_decision(pending=False, hour=10, count=2) == (False, "daily_cap")
    now_ts = 1_000_000.0
    gap_ok = sr.can_invite_decision(pending=False, hour=10, last_ts=now_ts - 2.5 * 3600,
                                    now_ts=now_ts)
    gap_blocked = sr.can_invite_decision(pending=False, hour=10,
                                         last_ts=now_ts - 2.5 * 3600 + 1, now_ts=now_ts)
    assert gap_ok == (True, "") and gap_blocked == (False, "gap")
    assert sr.can_invite_decision(pending=False, hour=10, npc_done=True) == (False, "npc_today")
