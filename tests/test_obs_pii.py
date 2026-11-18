# -*- coding: utf-8 -*-
"""Phase 17：可观测性 / PII 脱敏 / 错误码登记（守护侧）。

守护插件日志里本来就没有直接打 uid（grep 口径 0 处），所以这边重点在：

1. 同一份脱敏 / 结构化日志实现（三边同名同行为）；
2. describe() / diag_sync() 只新增 health 字段（plugin/version/uptime/db/jobs/counts），
   原字段逐字保留，握手契约不破；
3. 错误码登记表里 0/1/403 原样保留。
"""
from __future__ import annotations

import os
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import errors  # noqa: E402  (core/errors.py，与另两个插件同名同表)
from core.observability import (  # noqa: E402
    FULL_MASK, fingerprint, health_snapshot, log_event, mask_id, mask_open_id,
    mask_secret, mask_token, mask_value, scrub,
)

UID = "123456789"
OPEN_ID = "ABCDEF1234567890XYZ"
TOKEN = "sk-live-abcdefghijklmnop"


class _Recorder:
    def __init__(self):
        self.lines = []

    def __getattr__(self, _name):
        def _emit(*args, **_kwargs):
            self.lines.append(" ".join(str(arg) for arg in args))
        return _emit


class _FakeJobs:
    def report(self):
        return [{"name": "tick", "running": False}]


def test_mask_id_matrix():
    assert mask_id(UID) == "12****89"
    assert mask_id("123456") == FULL_MASK
    assert mask_id("") == ""
    assert mask_id(None) == ""


def test_mask_open_id_token_secret():
    assert mask_open_id(OPEN_ID) == "ABCD****0XYZ"
    assert mask_token(TOKEN) == "****mnop"
    assert mask_token("short") == FULL_MASK
    assert mask_secret("k-1234567890") == FULL_MASK


def test_mask_is_irreversible():
    for raw in (UID, OPEN_ID, TOKEN, "13800138000", "1234"):
        masked = mask_value("uid", raw)
        assert masked != raw
        assert raw not in masked


def test_fingerprint_is_stable_not_reversible():
    first = fingerprint(UID)
    assert first == fingerprint(UID)
    assert len(first) == 8
    assert UID not in first
    assert first != fingerprint("987654321")


def test_log_event_masks_sensitive_fields(monkeypatch):
    import core.observability as mod

    recorder = _Recorder()
    monkeypatch.setattr(mod, "logger", recorder)
    line = log_event("info", "port.describe", uid=UID, open_id=OPEN_ID, token=TOKEN)
    assert line.startswith("[port.describe]"), line
    for raw in (UID, OPEN_ID, TOKEN):
        assert raw not in line
    assert recorder.lines == [line]


def test_log_event_scrubs_free_text(monkeypatch):
    import core.observability as mod

    recorder = _Recorder()
    monkeypatch.setattr(mod, "logger", recorder)
    line = log_event("warning", "chat.raw", detail="对方 QQ 13800138000 说了一句")
    assert "13800138000" not in line
    assert "chat.raw" in line


def test_scrub_keeps_short_numbers():
    assert scrub("code=403 ok") == "code=403 ok"


def test_health_snapshot_shape():
    snap = health_snapshot("Astrbot_amoupup_watch", version="1.1.0",
                           db={"schema_version": 1, "ok": True}, jobs=_FakeJobs(),
                           counts={"npc": 4}, started_at=1000.0, now=1064.0)
    assert snap["plugin"] == "Astrbot_amoupup_watch"
    assert snap["version"] == "1.1.0"
    assert snap["uptime_s"] == 64
    assert snap["db"] == {"schema_version": 1, "ok": True}
    assert snap["jobs"][0]["name"] == "tick"
    assert snap["counts"] == {"npc": 4}
    assert snap["error_code"] == ""


def test_health_snapshot_degrades_without_raising():
    class Broken:
        def schema_status(self):
            raise RuntimeError("db gone")

    snap = health_snapshot("demo", db=Broken(), jobs=object())
    assert snap["db"] == {"schema_version": None, "ok": False}
    assert snap["jobs"] == []
    assert snap["error_code"] == "health.degraded"


def test_error_registry_numbers_unchanged():
    assert errors.CODE_BY_NUMBER == {0: "ok", 1: "fail", 403: "forbidden"}
    assert errors.code_number(errors.CODE_OK) == 0
    assert errors.code_number(errors.CODE_FAIL) == 1
    assert errors.code_number(errors.CODE_FORBIDDEN) == 403


def test_describe_code():
    assert "业务失败" in errors.describe_code(1)
    assert "守卫拒绝" in errors.describe_code("forbidden")
    assert "降级" in errors.describe_code(errors.CODE_HEALTH_DEGRADED)
    assert "未知" in errors.describe_code("no-such-code")


def test_obs_health_uses_store_and_scheduler():
    import main

    fake = types.SimpleNamespace(store=object(), scheduler=_FakeJobs(),
                                 _started_at=time.time() - 10)
    snap = main.WatchPlugin.obs_health(fake)
    assert snap["plugin"] == main.PLUGIN_NAME
    assert snap["version"] == main.PLUGIN_VERSION
    assert snap["db"] == {"schema_version": 1, "ok": True}
    assert snap["jobs"][0]["name"] == "tick"
    assert 5 <= snap["uptime_s"] <= 60


def test_describe_and_diag_add_health_only():
    import main

    plugin = main.WatchPlugin()
    described = plugin.describe()
    for old in ("plugin", "display_name", "version", "api_version", "features",
                "status", "started_at", "generation", "tasks", "jobs"):
        assert old in described, old
    assert described["health"]["plugin"] == main.PLUGIN_NAME

    diagnosed = plugin.diag_sync()
    for old in ("generation", "tasks", "lag", "degraded", "jobs"):
        assert old in diagnosed, old
    assert diagnosed["health"]["version"] == main.PLUGIN_VERSION


def test_no_observability_routes_added():
    """Phase 17 只加字段，不加路由：不许出现 /health /obs 这类新端点。"""
    from api.routes import ROUTES

    paths = [row[0] for row in ROUTES]
    assert len(paths) == len(set(paths))
    assert not [path for path in paths if "health" in path or "obs" in path], paths
