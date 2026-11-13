# -*- coding: utf-8 -*-
"""数据层：models 包 + 记忆索引 + 兼容 shim。"""
import time

from models import FinanceRepo, MemoryIndex, Store
from models.db import SCHEMA_VERSION
from models.memory import MemoryIndex as MemoryIndexDirect


def make(tmp_path):
    store = Store(root=tmp_path / "d", min_free_mb=0)
    store.init()
    return store, FinanceRepo(store)


def test_models_package_exports_and_schema(tmp_path):
    store, repo = make(tmp_path)
    try:
        assert SCHEMA_VERSION >= 1
        assert isinstance(repo.memory, MemoryIndexDirect)
        # 期望的表都在（分层后 schema 仍由 models.db 建）
        for table in ("meta", "expenses", "salary_items", "loans", "assets", "memory_index"):
            rows = store.query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            assert rows, table
    finally:
        store.close()


def test_shims_still_import_the_same_classes():
    from models.ledger import FinanceRepo as ShimRepo
    from models.db import Store as ShimStore

    assert ShimStore is Store
    assert ShimRepo is FinanceRepo


def test_memory_index_crud_and_ttl(tmp_path):
    store, repo = make(tmp_path)
    try:
        idx = repo.memory
        assert idx.exists("a") is False
        assert idx.put("a", "long", "今天买了面条", ttl_days=90)
        assert idx.exists("a") is True
        assert idx.put("a", "long", "重复写", ttl_days=90) is False   # 幂等
        assert idx.count("long") == 1
        assert idx.count() == 1
        idx.archive("a")
        assert idx.count("long") == 0
        assert idx.put("b", "news", "茅台新闻", ttl_days=3, now=time.time() - 4 * 86400)
        assert [row["idem"] for row in idx.expired()] == ["b"]
        assert idx.delete("a") is True
        assert idx.exists("a") is False
    finally:
        store.close()


def test_ledger_records_expense_and_salary(tmp_path):
    store, repo = make(tmp_path)
    try:
        assert repo.add_expense("e1", "2026-11-13", "food", "面条 × 1", 1500) is True
        assert repo.add_expense("e1", "2026-11-13", "food", "面条 × 1", 1500) is False  # idem 幂等
        totals = repo.expense_totals("2026-11")
        assert totals[0]["category"] == "food" and totals[0]["total"] == 1500
        repo.add_salary_item("2026-11", "bonus", "全勤奖", 10000)
        assert repo.salary_items("2026-11")[0]["label"] == "全勤奖"
    finally:
        store.close()
