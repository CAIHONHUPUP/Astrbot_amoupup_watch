# -*- coding: utf-8 -*-
"""守护 Web API 层覆盖率补测（2026-11-18）。

只调用 ApiMixin 的 handler 方法：用最小的宿主桩（只提供 handler 真正会读的属性），
断言每个分支的返回 code / 关键字段 / 账本副作用。
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

from api import handlers as handlers_mod  # noqa: E402
from api.handlers import ApiMixin, _ledger_date_time, _ledger_ts  # noqa: E402


class Host(ApiMixin):
    """最小宿主：handler 用到什么属性就传什么，缺的保持不存在（走降级分支）。"""

    def __init__(self, body=None, **attrs):
        self._body = {} if body is None else dict(body)
        for key, value in attrs.items():
            setattr(self, key, value)

    async def _read_json(self):
        return dict(self._body)


def run(coro):
    return asyncio.run(coro)


class FakeRepo:
    def __init__(self, meta=None):
        self.data = dict(meta or {})
        self.writes = []

    def meta_get(self, key, default=None):
        return self.data.get(key, default)

    def meta_set(self, key, value):
        self.writes.append((key, value))
        self.data[key] = value

    def assets_of(self, kind):
        return self.data.get("assets:" + str(kind)) or []

    def expenses(self, period=""):
        return list(self.data.get("expenses") or [])


class Req:
    def __init__(self, url="", query=None):
        self.url = url
        self.query_params = dict(query or {})


def _install_web(mod, monkeypatch):
    import astrbot.api as api

    mod.__path__ = []
    setattr(api, "web", mod)
    if not hasattr(api, "__path__"):
        api.__path__ = []
    monkeypatch.setitem(sys.modules, "astrbot.api.web", mod)


def _drop_web(monkeypatch):
    import astrbot.api as api

    monkeypatch.delitem(sys.modules, "astrbot.api.web", raising=False)
    monkeypatch.delattr(api, "web", raising=False)


# ---------------- 基础工具 ----------------
def test_ledger_ts_and_date_time_guards():
    assert _ledger_ts(None) == 0.0
    assert _ledger_ts("bad") == 0.0
    assert _ledger_ts(float("inf")) == 0.0
    assert _ledger_ts("12.5") == 12.5
    assert _ledger_date_time(0) == ("", "")
    assert _ledger_date_time(1e20) == ("", "")


def test_read_json_success_and_non_dict(monkeypatch):
    class ReqOk:
        async def json(self, default=None):
            assert default == {}
            return {"city": "东莞"}

    mod = types.ModuleType("astrbot.api.web")
    mod.request = ReqOk()
    _install_web(mod, monkeypatch)
    assert run(ApiMixin._read_json(Host())) == {"city": "东莞"}

    class ReqList:
        async def json(self, default=None):
            return ["not", "dict"]

    mod.request = ReqList()
    assert run(ApiMixin._read_json(Host())) == {}


def test_read_json_old_request_without_default(monkeypatch):
    class ReqOld:
        async def json(self):
            return {"a": 1}

    mod = types.ModuleType("astrbot.api.web")
    mod.request = ReqOld()
    _install_web(mod, monkeypatch)
    assert run(ApiMixin._read_json(Host())) == {"a": 1}


def test_read_json_import_error_returns_empty(monkeypatch):
    _drop_web(monkeypatch)
    assert run(ApiMixin._read_json(Host())) == {}


# ---------------- 金融总览 ----------------
class SnapshotHost(Host):
    def _finance_snapshot(self, demo=False):
        if getattr(self, "boom", False):
            raise RuntimeError("snapshot boom")
        return {"status": "ok", "demo": bool(demo)}


def test_finance_demo_ok_and_error():
    assert run(SnapshotHost()._api_finance_demo()) == {"status": "ok", "demo": True}
    boom = SnapshotHost()
    boom.boom = True
    assert run(boom._api_finance_demo()) == {"status": "error", "message": "demo failed"}


def test_finance_reads_demo_flag_from_request():
    req = Req(url="/finance?demo=1", query={"demo": "1"})
    assert run(SnapshotHost()._api_finance(req))["demo"] is True
    assert run(SnapshotHost()._api_finance(Req()))["demo"] is False
    boom = SnapshotHost()
    boom.boom = True
    assert run(boom._api_finance(Req()))["status"] == "error"


# ---------------- 基金 ----------------
class FundRepo(FakeRepo):
    def fund_quotes(self, day):
        if self.data.get("boom"):
            raise RuntimeError("quotes boom")
        return [
            {"code": "110011", "name": "易方达", "dwjz_e4": 12345, "gsz_e4": 12400,
             "gszzl_bp": 55, "source": "eastmoney", "tradeable": 1,
             "gztime": "2026-11-18 14:50", "ts": 1.0},
            {"code": "000001", "name": "华夏"},
        ]


def test_funds_list_and_refreshed_and_errors():
    repo = FundRepo()
    repo.data["fund_refresh:2026-11-18"] = "2026-11-18 14:50"
    data = run(Host(finance_repo=repo)._api_funds())
    assert data["ok"] is True and data["day"] == dt.date.today().isoformat()
    first = data["items"][0]
    assert first["nav"] == pytest.approx(1.2345)
    assert first["estimate"] == pytest.approx(1.24)
    assert first["change_pct"] == pytest.approx(0.55)
    assert first["tradeable"] is True
    assert data["refreshed"] == "2026-11-18 14:50"
    assert data["items"][1]["nav"] == 0.0 and data["items"][1]["tradeable"] is False

    assert run(Host()._api_funds()) == {"ok": False, "message": "账本还没准备好"}
    bad = FundRepo()
    bad.data["boom"] = True
    assert run(Host(finance_repo=bad)._api_funds())["message"] == "基金行情暂时读不到，稍后再看"


class FundMarket:
    def __init__(self, result=None, boom=False):
        self.result = result or {"ok": True, "refreshed": 3}
        self.boom = boom
        self.calls = []

    async def refresh(self, force=False):
        self.calls.append(force)
        if self.boom:
            raise RuntimeError("refresh boom")
        return self.result


def test_funds_refresh_uses_cached_market_and_forces():
    market = FundMarket()
    host = Host(finance_repo=FakeRepo(), runtime=object(), fund_market=market)
    assert run(host._api_funds_refresh()) == {"ok": True, "refreshed": 3}
    assert market.calls == [True]
    assert host.fund_market is market

    assert run(Host()._api_funds_refresh()) == {"ok": False, "message": "账本还没准备好"}
    bad = Host(finance_repo=FakeRepo(), runtime=object(), fund_market=FundMarket(boom=True))
    assert run(bad._api_funds_refresh()) == {"ok": False, "message": "刷新失败，晚点再试"}


# ---------------- 银行 ----------------
class FakeBank:
    def __init__(self):
        self.calls = []

    def current_cents(self):
        return 700

    def active_fixed_rows(self):
        return [{"amount_cents": 100}]

    def fixed_cents(self):
        return 100

    def last_matured(self):
        return {"amount_cents": 50}

    def total_cents(self):
        return 800

    async def deposit(self, amount, term):
        self.calls.append(("deposit", amount, term))
        return {"ok": True, "amount_cents": amount, "term": term}

    async def withdraw(self, amount, term):
        self.calls.append(("withdraw", amount, term))
        return {"ok": True, "amount_cents": amount}

    async def process_matured_fixed_deposits(self):
        self.calls.append(("settle",))
        return [{"amount_cents": 100}]


def test_bank_get_deposit_withdraw_settle():
    bank = FakeBank()
    host = Host(bank=bank)
    data = run(host._api_bank())
    assert data == {"ok": True, "current_cents": 700, "fixed": [{"amount_cents": 100}],
                    "fixed_cents": 100, "last_matured": {"amount_cents": 50}, "total_cents": 800}

    assert run(Host(body={"action": "deposit", "amount_cents": 5000, "term": 12},
                    bank=bank)._api_bank())["term"] == 12
    assert bank.calls[-1] == ("deposit", 5000, 12)

    assert run(Host(body={"action": "withdraw", "amount_cents": 300}, bank=bank)._api_bank())["ok"] is True
    assert bank.calls[-1] == ("withdraw", 300, None)

    assert run(Host(body={"action": "withdraw", "amount_cents": 300, "term": 6},
                    bank=bank)._api_bank())["ok"] is True
    assert bank.calls[-1] == ("withdraw", 300, 0)

    assert run(Host(body={"action": "settle_matured"}, bank=bank)._api_bank()) == {
        "ok": True, "matured": [{"amount_cents": 100}]}


def test_bank_exception_returns_error():
    class BoomBank(FakeBank):
        async def deposit(self, amount, term):
            raise RuntimeError("bank boom")

    data = run(Host(body={"action": "deposit", "amount_cents": 1}, bank=BoomBank())._api_bank())
    assert data == {"ok": False, "message": "银行接口异常"}


# ---------------- 社交 ----------------
class SocialRepo:
    def npcs(self):
        return [{"name": "小明"}]

    def receivables(self):
        return [{"name": "小红", "amount_cents": 100}]


class FakeSocial:
    def __init__(self, boom=False):
        self.boom = boom
        self.repo = SocialRepo()
        self.invites = []
        self.cancelled = 0
        self.hits = []

    def ensure_seed(self):
        if self.boom:
            raise RuntimeError("seed boom")

    def relation_line(self):
        return "关系不错"

    def events(self, limit):
        return []

    def pending_invite(self):
        return {"npc": "小明"}

    async def create_invite(self, kind="", npc="", amount_cents=0):
        self.invites.append((kind, npc, amount_cents))
        return {"ok": True, "invite": {"npc": npc}}

    def clear_invite(self):
        self.cancelled += 1

    async def scheduled(self):
        return {"decision": "accept", "invite": {"npc": "小明"}}

    async def gift_money(self):
        self.hits.append("随礼")
        return {"ok": True, "event": "随礼"}

    async def red_packet(self):
        self.hits.append("红包")
        return {"ok": True}

    async def dinner(self):
        self.hits.append("聚餐")
        return {"ok": True}

    async def borrow_request(self):
        self.hits.append("借钱")
        return {"ok": True}

    async def criticism(self):
        self.hits.append("批评")
        return {"ok": True}


def test_social_get_lists_npcs_with_birthday(monkeypatch):
    from services import gift_engine

    monkeypatch.setattr(gift_engine, "birthday_info",
                        lambda plugin, name: {"birthday": "05-20", "text": "生日：5月20日"})
    data = run(Host(social=FakeSocial())._api_social())
    assert data["ok"] is True
    assert data["npcs"][0]["birthday"] == "05-20"
    assert data["npcs"][0]["birthday_text"] == "生日：5月20日"
    assert data["receivables"][0]["name"] == "小红"
    assert data["line"] == "关系不错"
    assert data["pending"] == {"npc": "小明"}


def test_social_post_actions():
    social = FakeSocial()
    host = Host(social=social)
    invite = run(Host(body={"action": "invite", "kind": "dinner", "npc": "小明",
                            "amount": "12.5"}, social=social)._api_social())
    assert invite["ok"] is True
    assert social.invites == [("dinner", "小明", 1250)]

    bad_amount = run(Host(body={"action": "invite", "npc": "小明", "amount": "x"},
                          social=social)._api_social())
    assert bad_amount["ok"] is True
    assert social.invites[-1] == ("", "小明", 0)

    cancel = run(Host(body={"action": "cancel"}, social=social)._api_social())
    assert cancel["ok"] is True and social.cancelled == 1

    decide = run(Host(body={"action": "decide"}, social=social)._api_social())
    assert decide["ok"] is True and decide["result"]["decision"] == "accept"

    gift = run(Host(body={"name": "随礼"}, social=social)._api_social())
    assert gift["ok"] is True and social.hits == ["随礼"]

    unknown = run(Host(body={"name": "不存在"}, social=social)._api_social())
    assert unknown == {"ok": False, "message": "未知事件"}


def test_social_exception_returns_error():
    assert run(Host(social=FakeSocial(boom=True))._api_social()) == {
        "ok": False, "message": "社交接口异常"}


# ---------------- 保险 ----------------
class FakeIns:
    def __init__(self):
        self.bought = []

    def status(self):
        return {
            "e生保": {"active": True, "year": 2026},
            "金钟罩": {},
            "重疾": {"expired": True},
            "交强险": {"grace": True, "grace_until": "2026-12-01"},
            "商车险": {},
            "家财险": {"lapsed": True},
        }

    async def buy(self, kind):
        self.bought.append(kind)
        return {"ok": True, "kind": kind}


def test_insurance_groups_and_buy():
    repo = FakeRepo()
    host = Host(medical=object(), insurance=FakeIns(), finance_repo=repo)
    data = run(host._api_insurance())
    assert data["ok"] is True
    human = [i["status"] for i in data["groups"][0]["items"]]
    assert human == ["生效中（下次缴费：2027 年）", "未购买", "已失效（断缴）"]
    assert [i["name"] for i in data["groups"][0]["items"]] == ["平安e生保", "平安金钟罩", "平安一年期重疾险"]
    property_items = [i["status"] for i in data["groups"][1]["items"]]
    assert property_items == ["无车不适用", "无车不适用", "无房不适用"]
    assert [c["kind"] for c in data["catalog"]]
    assert data["has_policy"] is True

    repo.data["assets:car"] = [{"id": 1}]
    repo.data["assets:house"] = [{"id": 1}]
    owned = run(Host(medical=object(), insurance=FakeIns(), finance_repo=repo)._api_insurance())
    assert [i["status"] for i in owned["groups"][1]["items"]][0] == "宽限期（至 2026-12-01）"

    bought = run(Host(body={"kind": "critical"}, medical=object(), insurance=FakeIns())._api_insurance())
    assert bought["ok"] is True

    assert run(Host(medical=None)._api_insurance()) == {"ok": False, "message": "医疗模块未就绪"}


def test_insurance_exception_returns_error():
    class BoomIns(FakeIns):
        def status(self):
            raise RuntimeError("ins boom")

    data = run(Host(medical=object(), insurance=BoomIns())._api_insurance())
    assert data == {"ok": False, "message": "保险接口异常"}


# ---------------- 突发 / 人情债 ----------------
def test_fortune_branches():
    class Fortune:
        async def monthly_roll(self):
            return {"ok": True, "word": "吉"}

    assert run(Host(fortune=Fortune())._api_fortune()) == {"ok": True, "word": "吉"}
    assert run(Host(fortune=None)._api_fortune()) == {"ok": False, "message": "突发引擎未就绪"}

    class BoomFortune:
        async def monthly_roll(self):
            raise RuntimeError("fortune boom")

    assert run(Host(fortune=BoomFortune())._api_fortune())["message"] == "突发事件接口异常"


def test_lending_lend_and_repay():
    class Lending:
        def __init__(self):
            self.calls = []

        async def lend(self, friend, cents, months):
            self.calls.append(("lend", friend, cents, months))
            return {"ok": True}

        async def repay_tick(self):
            self.calls.append(("repay",))
            return {"ok": True, "paid": 1}

    lending = Lending()
    assert run(Host(body={"friend": "小李", "amount_cents": 5000, "months": 3},
                    lending=lending)._api_lending())["ok"] is True
    assert lending.calls[-1] == ("lend", "小李", 5000, 3)
    assert run(Host(body={}, lending=lending)._api_lending())["paid"] == 1
    assert run(Host(lending=None)._api_lending()) == {"ok": False, "message": "人情债模块未就绪"}

    class BoomLending(Lending):
        async def repay_tick(self):
            raise RuntimeError("lending boom")

    assert run(Host(body={}, lending=BoomLending())._api_lending())["message"] == "人情债接口异常"


# ---------------- 房屋 ----------------
def _write_png(path):
    from PIL import Image

    Image.new("RGBA", (8, 8), (255, 0, 0, 128)).save(path, "PNG")
    return path


def test_house_image_data_paths(tmp_path):
    host = Host()
    assert host._house_image_data("") == ""
    assert host._house_image_data(str(tmp_path / "missing.png")) == ""

    big = tmp_path / "big.png"
    with open(big, "wb") as fh:
        fh.write(b"\0" * (8 * 1024 * 1024 + 1))
    assert host._house_image_data(str(big)) == ""

    png = _write_png(tmp_path / "room.png")
    data = host._house_image_data(str(png))
    assert data.startswith("data:image/jpeg;base64,") and len(data) > 100

    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    raw = host._house_image_data(str(broken))
    assert raw.startswith("data:image/png;base64,")


class FakeHousing:
    def __init__(self, images=None):
        self.images = images or {}
        self.moved = []

    def current(self):
        return {"id": 1, "name": "出租屋", "kind": "rent", "status": "living",
                "style": "简约风", "colors": "米白", "furniture": ["沙发"],
                "rent_cents": 120000, "images": self.images}

    def homes(self):
        return [self.current(), {"id": 2, "name": "", "images": {}}]

    def card_text(self, kind="总览"):
        return "%s卡片" % kind

    def move_in(self, home_id):
        self.moved.append(home_id)


def test_housing_assembles_homes_and_images(tmp_path):
    png = _write_png(tmp_path / "lr.png")
    housing = FakeHousing({"living": str(png), "bed": str(tmp_path / "missing.png")})
    amo = types.SimpleNamespace(housing=housing)
    finance = types.SimpleNamespace(_amo=lambda: amo)
    data = run(Host(finance=finance)._api_housing())
    assert data["ok"] is True
    assert data["overview"] == "总览卡片" and data["own"] == "房产卡片"
    assert len(data["homes"]) == 2
    assert data["homes"][0]["is_current"] is True
    assert list(data["homes"][0]["images"]) == ["living"]
    assert data["current"]["images"] == data["homes"][0]["images"]
    assert data["homes"][1]["name"] == "家" and data["homes"][1]["is_current"] is False

    assert run(Host(finance=None)._api_housing()) == {"ok": False, "message": "能陪房屋模块未就绪", "homes": []}


def test_housing_exception_returns_message():
    class BoomHousing(FakeHousing):
        def homes(self):
            raise RuntimeError("homes boom")

    amo = types.SimpleNamespace(housing=BoomHousing())
    finance = types.SimpleNamespace(_amo=lambda: amo)
    data = run(Host(finance=finance)._api_housing())
    assert data["ok"] is False and data["message"].startswith("房屋读取失败")
    assert data["homes"] == []


# ---------------- 医疗 ----------------
class FakeMedical:
    def __init__(self):
        self.calls = []

    def health_snapshot(self):
        return {"sick": True, "status": "感冒", "until": "2026-11-20"}

    def is_sick(self):
        return False

    def in_bed_rest(self):
        return True

    async def checkup(self):
        self.calls.append("checkup")
        return {"ok": True, "hp": 90}

    async def handle_sick_event(self, level, name, days, event_id):
        self.calls.append((level, name, days, event_id))
        return {"ok": True, "level": level}


def test_medical_get_and_actions():
    repo = FakeRepo({"checkup:last": "2026-11-10", "checkup:last_hp": 80})
    hunger = {"hunger": 40}
    amo = types.SimpleNamespace(hunger_state=lambda: hunger)
    finance = types.SimpleNamespace(_amo=lambda: amo)
    medical = FakeMedical()
    data = run(Host(medical=medical, finance=finance, finance_repo=repo)._api_medical())
    assert data["ok"] is True and data["is_sick"] is True and data["in_bed_rest"] is True
    assert data["last_checkup"] == "2026-11-10" and data["last_hp"] == 80
    assert data["hunger"] == hunger and data["sick_until"] == "2026-11-20"

    assert run(Host(body={"action": "checkup"}, medical=medical)._api_medical()) == {"ok": True, "hp": 90}
    sick = run(Host(body={"level": "heavy", "name": "流感", "days": 2, "event_id": "e1"},
                    medical=medical)._api_medical())
    assert sick["ok"] is True and sick["level"] == "heavy"
    assert medical.calls[-1] == ("heavy", "流感", 2, "e1")

    assert run(Host(medical=None)._api_medical()) == {"ok": False, "message": "医疗模块未就绪"}


def test_medical_exception_returns_friendly_message():
    class BoomMedical(FakeMedical):
        def health_snapshot(self):
            raise RuntimeError("medical boom")

    data = run(Host(medical=BoomMedical(), finance_repo=FakeRepo())._api_medical())
    assert data["ok"] is False and "身体重要" in data["message"]


# ---------------- 城市 ----------------
def test_city_get_and_move():
    finance = types.SimpleNamespace(_amo=lambda: None)
    repo = FakeRepo()
    host = Host(body={}, finance=finance, finance_repo=repo)
    data = run(host._api_city())
    assert data["ok"] is True and data["city"] == "" and data["tier"] == "未设置"

    mover = Host(body={"city": "东莞"}, finance=finance, finance_repo=repo)
    moved = run(mover._api_city())
    assert moved["ok"] is True and moved["moved"]["reason"] == "amo_missing"
    assert moved["city"] == "东莞"
    assert ("city", "东莞") in repo.writes
    assert mover.city_override == "东莞"


def test_city_exception_returns_error():
    class BoomCityRepo(FakeRepo):
        def meta_set(self, key, value):
            raise RuntimeError("meta boom")

    finance = types.SimpleNamespace(_amo=lambda: None)
    host = Host(body={"city": "东莞"}, finance=finance, finance_repo=BoomCityRepo())
    host.city_override = ""
    data = run(host._api_city())
    assert data["ok"] is True and data["moved"]["ok"] is True


# ---------------- 职场 / 请假 ----------------
class FakeWorkEvents:
    def __init__(self):
        self.fired = []

    def today_events(self):
        return [{"kind": "late"}]

    def prompt_line(self):
        return "今天有点困"

    async def fire(self, kind, day="", source=""):
        self.fired.append((kind, day, source))
        return {"ok": True, "kind": kind}


def test_work_state_and_fire_aliases():
    work = FakeWorkEvents()
    state = run(Host(work_events=work)._api_work())
    assert state["ok"] is True and state["events"] == [{"kind": "late"}]
    assert state["prompt"] == "今天有点困"

    assert run(Host(body={"action": "fire", "kind": "迟到", "day": "2026-11-18"},
                    work_events=work)._api_work())["kind"] == "late"
    assert work.fired[-1] == ("late", "2026-11-18", "api")

    assert run(Host(body={"action": "trigger", "event": "加班"}, work_events=work)._api_work())["kind"] == "overtime"
    assert run(Host(body={"action": "fire", "kind": "Unknown"}, work_events=work)._api_work())["kind"] == "unknown"
    assert run(Host(work_events=None)._api_work()) == {"ok": False, "message": "职场引擎未就绪"}


def test_work_exception_returns_message():
    class BoomWork(FakeWorkEvents):
        async def fire(self, kind, day="", source=""):
            raise RuntimeError("work boom")

    data = run(Host(body={"action": "fire", "kind": "late"}, work_events=BoomWork())._api_work())
    assert data["ok"] is False and data["message"].startswith("职场接口失败")


class FakeSalary:
    def __init__(self):
        self.calls = []

    async def apply_leave(self, kind="", days=1.0, reason="", day=""):
        self.calls.append(("leave", kind, days, reason, day))
        return {"ok": True, "kind": kind, "days": days}

    def set_base(self, cents):
        self.calls.append(("base", cents))

    def add_item(self, period, kind, label, amount):
        self.calls.append(("item", period, kind, label, amount))

    def summary(self, period):
        return {"period": period, "base_cents": 500000}

    def thrifty_until(self):
        return "2026-12-01"

    async def raise_or_cut(self, pct):
        self.calls.append(("raise", pct))
        return {"ok": True, "pct": pct}

    async def settle(self, when, period):
        self.calls.append(("settle", str(when), period))
        return {"ok": True, "period": period}


def test_leave_handler():
    salary = FakeSalary()
    data = run(Host(body={"kind": "病假", "days": "2", "reason": "发烧", "day": "2026-11-18"},
                    finance_salary=salary)._api_leave())
    assert data["ok"] is True and data["days"] == 2.0
    assert salary.calls[-1] == ("leave", "病假", 2.0, "发烧", "2026-11-18")
    assert run(Host(finance_salary=None)._api_leave()) == {"ok": False, "message": "工资模块未就绪"}


def test_leave_exception_returns_message():
    class BoomSalary(FakeSalary):
        async def apply_leave(self, kind="", days=1.0, reason="", day=""):
            raise RuntimeError("leave boom")

    data = run(Host(body={}, finance_salary=BoomSalary())._api_leave())
    assert data["ok"] is False and data["message"].startswith("请假失败")


# ---------------- 开销 ----------------
class FakeExpense:
    def month_summary(self, now):
        return {"total_cents": 1234, "categories": [{"name": "餐饮", "cents": 1234}]}

    async def spend(self, category, label, cents):
        return {"ok": True, "category": category, "label": label, "amount_cents": cents}


def test_expenses_summary_and_items():
    repo = FakeRepo({"expenses": [
        {"day": "2026-11-17", "category": "餐饮", "label": "午饭", "amount_cents": 1800},
        {"day": "2026-11-18", "category": "交通", "label": "地铁", "amount_cents": 300},
    ]})
    data = run(Host(finance_expense=FakeExpense(), finance_repo=repo)._api_expenses())
    assert data["ok"] is True
    assert data["month"] == dt.date.today().strftime("%Y-%m")
    assert data["total_cents"] == 1234
    assert data["items"][0] == {"day": "2026-11-17", "category": "餐饮",
                                "label": "午饭", "amount_cents": 1800}
    assert run(Host(finance_expense=None)._api_expenses()) == {"ok": False, "message": "开销模块未就绪"}


def test_expenses_exception_returns_message():
    class BoomExpense(FakeExpense):
        def month_summary(self, now):
            raise RuntimeError("expense boom")

    data = run(Host(finance_expense=BoomExpense(), finance_repo=FakeRepo())._api_expenses())
    assert data["ok"] is False and data["message"].startswith("开销接口失败")


# ---------------- 工资 ----------------
def test_salary_actions():
    salary = FakeSalary()
    period = dt.date.today()
    if period.day > 15:
        period = period.replace(day=1) + dt.timedelta(days=32)
    expect = period.strftime("%Y-%m")

    state = run(Host(body={}, finance_salary=salary)._api_salary())
    assert state["ok"] is True and state["period"] == expect
    assert state["thrifty_until"] == "2026-12-01"

    run(Host(body={"action": "set", "base": "5000"}, finance_salary=salary)._api_salary())
    assert salary.calls[-1] == ("base", 500000)

    run(Host(body={"action": "add", "kind": "奖金", "amount": "800", "label": "项目"},
             finance_salary=salary)._api_salary())
    assert salary.calls[-1] == ("item", expect, "bonus", "项目", 80000)

    run(Host(body={"action": "add", "kind": "扣款", "amount": "300"},
             finance_salary=salary)._api_salary())
    assert salary.calls[-1] == ("item", expect, "deduction", "扣款", -30000)

    assert run(Host(body={"action": "raise", "pct": "0.2"},
                    finance_salary=salary)._api_salary()) == {"ok": True, "pct": 0.2}
    settled = run(Host(body={"action": "settle"}, finance_salary=salary)._api_salary())
    assert settled["ok"] is True and settled["period"] == expect
    assert salary.calls[-1][0] == "settle"

    assert run(Host(finance_salary=None)._api_salary()) == {"ok": False, "message": "工资模块未就绪"}


def test_salary_exception_returns_message():
    class BoomSalary(FakeSalary):
        def summary(self, period):
            raise RuntimeError("salary boom")

    data = run(Host(body={}, finance_salary=BoomSalary())._api_salary())
    assert data["ok"] is False and data["message"].startswith("工资接口失败")


# ---------------- 房车 ----------------
class FakeProperty:
    def __init__(self):
        self.bought = []

    def state(self):
        return {"house": {"owns": False}, "car": {"owns": True}}

    def afford(self, kind):
        return kind == "car"

    async def buy(self, kind):
        self.bought.append(kind)
        return {"ok": True, "kind": kind}


def test_property_state_and_buy():
    prop = FakeProperty()
    state = run(Host(body={}, finance_property=prop)._api_property())
    assert state["ok"] is True
    assert state["afford"] == {"house": False, "car": True}
    assert state["house"] == {"owns": False}

    assert run(Host(body={"action": "buy", "kind": "car"},
                    finance_property=prop)._api_property())["kind"] == "car"
    assert prop.bought == ["car"]
    assert run(Host(body={"action": "buy", "kind": "房"},
                    finance_property=prop)._api_property())["kind"] == "house"
    assert run(Host(finance_property=None)._api_property()) == {"ok": False, "message": "房产模块未就绪"}


def test_property_exception_returns_message():
    class BoomProperty(FakeProperty):
        def state(self):
            raise RuntimeError("property boom")

    data = run(Host(body={}, finance_property=BoomProperty())._api_property())
    assert data["ok"] is False and data["message"].startswith("房产接口失败")


# ---------------- 资金紧张 / 借款 / 消费 / 决策 / 调试 ----------------
class FakeHelp:
    def __init__(self):
        self.loans = []

    def status(self):
        return {"low": True, "need_cents": 5000}

    def prompt_text(self):
        return "钱不够了"

    async def take_loan(self, amount, periods, rate):
        self.loans.append((amount, periods, rate))
        return {"ok": True, "amount_cents": amount}


class FakeDecision:
    def __init__(self):
        self.slots = []
        self.outage = 0

    async def run_slot(self, slot):
        self.slots.append(slot)
        return {"ok": True, "slot": slot}

    async def simulate_fund_outage(self):
        self.outage += 1
        return {"skipped": "fund_outage"}


def test_finance_help_and_loan():
    help_ = FakeHelp()
    data = run(Host(finance_help=help_)._api_finance_help())
    assert data["low"] is True and data["prompt"] == "钱不够了"
    assert run(Host(finance_help=None)._api_finance_help()) == {"prompt": ""}

    got = run(Host(body={"amount": "100", "periods": 6, "rate": 0.05},
                   finance_help=help_)._api_loan())
    assert got["ok"] is True and help_.loans[-1] == (10000, 6, 0.05)

    class BoomHelp(FakeHelp):
        async def take_loan(self, amount, periods, rate):
            raise RuntimeError("loan boom")

    bad = run(Host(body={"amount": "100"}, finance_help=BoomHelp())._api_loan())
    assert bad["ok"] is False and bad["message"].startswith("借款失败")


def test_consume_handler():
    class Consume:
        async def buy_items(self, items):
            return [{"item": i.get("item"), "ok": True} for i in items]

    data = run(Host(body={"items": [{"item": "肠粉", "qty": 1}]},
                    finance_consume=Consume())._api_consume())
    assert data["items"] == [{"item": "肠粉", "ok": True}]

    class BoomConsume:
        async def buy_items(self, items):
            raise RuntimeError("consume boom")

    bad = run(Host(body={"items": []}, finance_consume=BoomConsume())._api_consume())
    assert bad["items"] == [] and bad["message"].startswith("消费失败")


def test_decision_handler_and_exception():
    decision = FakeDecision()
    assert run(Host(body={"slot": "noon"}, finance_decision=decision)._api_decision())["slot"] == "noon"
    assert decision.slots == ["noon"]

    class BoomDecision(FakeDecision):
        async def run_slot(self, slot):
            raise RuntimeError("decision boom")

    bad = run(Host(body={}, finance_decision=BoomDecision())._api_decision())
    assert bad["ok"] is False and bad["message"].startswith("决策失败")


def test_debug_actions():
    class Finance:
        def __init__(self):
            self.calls = []

        async def income(self, cents, reason, idem, salary=False):
            self.calls.append(("income", cents, salary))
            return {"ok": True}

        async def expense(self, cents, reason, idem, necessary=False):
            self.calls.append(("expense", cents, necessary))
            return {"ok": True}

    finance = Finance()
    host = Host(body={"action": "salary"}, finance=finance)
    assert run(host._api_debug())["ok"] is True
    assert finance.calls[-1][0] == "income" and finance.calls[-1][2] is True

    host = Host(body={"action": "rent"}, finance=finance)
    run(host._api_debug())
    assert finance.calls[-1] == ("expense", 120000, True)

    host = Host(body={"action": "spend", "category": "food", "label": "包子", "amount": "12.5"},
                finance_expense=FakeExpense())
    spend = run(host._api_debug())
    assert spend == {"ok": True, "category": "food", "label": "包子", "amount_cents": 1250}

    decision = FakeDecision()
    host = Host(body={"action": "quotes"}, finance_decision=decision)
    assert run(host._api_debug())["result"]["slot"] == "close"

    host = Host(body={"action": "fund_outage"}, finance_decision=decision)
    assert run(host._api_debug())["result"]["skipped"] == "fund_outage"

    assert run(Host(body={"action": "unknown"})._api_debug()) == {
        "ok": False, "message": "未知调试动作"}


def test_debug_exception_returns_message():
    class BoomFinance:
        async def income(self, *args, **kwargs):
            raise RuntimeError("debug boom")

    data = run(Host(body={"action": "salary"}, finance=BoomFinance())._api_debug())
    assert data["ok"] is False and data["message"] == "debug boom"


def test_diag_handler():
    class Diag:
        async def diag(self):
            return {"status": "ok", "plugins": 2}

    assert run(Host(diag=Diag().diag)._api_diag())["plugins"] == 2

    class BoomDiag:
        async def diag(self):
            raise RuntimeError("diag boom")

    assert run(Host(diag=BoomDiag().diag)._api_diag()) == {"status": "error", "message": "diag failed"}