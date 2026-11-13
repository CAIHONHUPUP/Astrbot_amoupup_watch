# -*- coding: utf-8 -*-
import asyncio
import json
import time

import pytest

import core.memory_tier as fmt
from core.memory_tier import TieredMemory
from services.news_api import merge_stock_news, parse_cls, parse_eastmoney
from models.ledger import FinanceRepo
from models.db import Store


class FakeMemory:
    def __init__(self):
        self.written = []

    async def remember(self, content, kind="finance", idem="", verify=True):
        self.written.append((kind, idem, content))
        return {"ok": True}


@pytest.fixture()
def env(tmp_path):
    store = Store(root=tmp_path / "d", min_free_mb=0)
    store.init()
    repo = FinanceRepo(store)
    mem = FakeMemory()
    tm = TieredMemory(None, repo, mem)
    yield tm, repo, mem, store
    store.close()


def test_parse_eastmoney_jsonp():
    payload = {"result": {"cmsArticleWebOld": [
        {"title": "<em>茅台</em>业绩超预期", "url": "http://x", "mediaName": "证券时报",
         "date": "2026-11-13", "content": "<p>营收增长</p>"},
        {"title": "第二条", "url": "http://y", "mediaName": "东财", "date": "", "content": ""},
    ]}}
    text = "jQuery(" + json.dumps(payload, ensure_ascii=False) + ")"
    rows = parse_eastmoney(text, 5)
    assert len(rows) == 2 and rows[0]["title"] == "茅台业绩超预期"
    assert rows[0]["snippet"] == "营收增长"


def test_parse_cls_list():
    payload = {"data": {"roll_data": [{"title": "快讯一", "brief": "摘要一", "ctime": 1}]}}
    rows = parse_cls(json.dumps(payload), 5)
    assert rows and rows[0]["title"] == "快讯一"


def test_merge_stock_news():
    items = [{"title": "业绩超预期"}, {"title": "机构上调评级"}, {"title": "北向资金流入"}]
    text = merge_stock_news("600519", items, "2026-11-13")
    assert "600519" in text and "3条" in text and "业绩超预期" in text


def test_tier_dedup_and_limits(env):
    tm, repo, mem, _ = env
    assert asyncio.run(tm.remember("long", "买入 601398 100股", "t1", related="601398")) is True
    assert asyncio.run(tm.remember("long", "买入 601398 100股", "t1", related="601398")) is False
    assert len(tm.retrieve(["601398"])) == 1
    assert len(tm.retrieve(["999999"])) == 0


def test_short_overwrite(env):
    tm, repo, mem, _ = env
    asyncio.run(tm.overwrite_short("601398", "浮盈 1%", "d1"))
    asyncio.run(tm.overwrite_short("601398", "浮盈 2%", "d2"))
    rows = tm.retrieve(["601398"])
    assert len(rows) == 1 and "2%" in rows[0]["content"]


def test_news_merge_one_per_day(env):
    tm, repo, mem, _ = env
    asyncio.run(tm.remember("news", "茅台 2条：业绩、评级", "news:2026-11-13:600519", related="600519"))
    asyncio.run(tm.remember("news", "茅台 3条：业绩、评级、资金", "news:2026-11-13:600519", related="600519"))
    assert len([r for r in tm.retrieve(["600519"]) if r["tier"] == "news"]) == 1


def test_expired_news_not_retrieved(env):
    tm, repo, _, _ = env
    repo.store.write(
        "INSERT INTO memory_index(idem, tier, content, related, created, expires_at, status) "
        "VALUES(?,?,?,?,?,?, 'active')",
        ("old", "news", "旧闻", "600519", time.time() - 10 * 86400, time.time() - 7 * 86400))
    assert not [r for r in tm.retrieve(["600519"]) if r["idem"] == "old"]


def test_compress_old_long(env, monkeypatch):
    tm, repo, _, _ = env
    monkeypatch.setattr(fmt, "LONG_COMPRESS_AT", 3)
    old = time.time() - 100 * 86400
    for i in range(6):
        repo.store.write(
            "INSERT INTO memory_index(idem, tier, content, related, created, expires_at, status) "
            "VALUES(?,?,?,?,?,0,'active')", ("old%d" % i, "long", "旧明细%d" % i, "601398", old))
    assert tm.compress_long() >= 1
    active = repo._rows("SELECT * FROM memory_index WHERE tier='long' AND status='active'")
    assert any(r["idem"].startswith("compress:") for r in active)


def test_30_day_simulation_bounded(env):
    tm, repo, _, _ = env
    for day in range(30):
        asyncio.run(tm.overwrite_short("601398", "第%d天浮盈" % day, "pnl:%d" % day))
        for n in range(2):
            asyncio.run(tm.remember("long", "第%d天支出%d" % (day, n), "exp:%d:%d" % (day, n)))
    rows = repo._rows("SELECT tier, COUNT(*) c FROM memory_index WHERE status='active' GROUP BY tier")
    counts = {r["tier"]: r["c"] for r in rows}
    assert counts.get("short") == 1       # 覆盖式，不按天累加
    assert counts.get("long") == 60       # 每天 2 笔，逐笔保留
