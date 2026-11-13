# -*- coding: utf-8 -*-
"""main.py 护栏：只允许装配/注册/分发；分层目录必须在；根目录不留 shim。"""
import importlib
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "main.py")


def _src():
    return io.open(MAIN, encoding="utf-8").read()


def test_main_stays_thin():
    assert len(_src().splitlines()) <= 520, "main.py 又变胖了，业务逻辑应该去 services/core"


def test_main_defines_the_plugin_class():
    # AstrBot 用 handler_module_path 判定 handler 归属，主类必须留在 main.py
    assert "class WatchPlugin" in _src()


def test_main_has_no_business_implementation():
    text = _src()
    for banned in ("def _api_", "def _snapshot_real", "SELECT ", "amortization(", "def _finance_provider"):
        assert banned not in text, "main.py 里出现了业务实现：%s" % banned


def test_layers_exist():
    for rel in ("models/db.py", "models/ledger.py", "models/memory.py",
                "services/trade_engine.py", "services/life_engine.py",
                "services/work_engine.py", "services/work_events.py",
                "services/property_engine.py", "services/city.py",
                "core/agent_loop.py", "core/amo_bridge.py",
                "utils/helpers.py", "ui/cards.py", "api/handlers.py"):
        assert os.path.isfile(os.path.join(ROOT, rel)), rel


def test_root_shims_are_gone():
    """分层完成后根目录不再留 shim，import 一律走真实模块。"""
    import services.trade_engine as real
    assert real.Trading is not None
    for gone in ("finance_trading", "finance_market", "finance_store", "store",
                 "finance_link", "finance_decision", "finance_life", "finance_salary"):
        try:
            importlib.import_module(gone)
        except ImportError:
            continue
        raise AssertionError("根目录还留着 shim：%s" % gone)
