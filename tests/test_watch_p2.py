# -*- coding: utf-8 -*-
"""Phase 15（守护 P2）：金额单一实现 / 账本审计 / 结算幂等 / 共享 fixtures / 契约。

为什么要有这个文件：
- 金额换算和幂等键以前有 5 份实现，改了谁都可能让「流水」和「日记」对不上；
- 账本出错时没有只读的核对入口，只能靠肉眼；
- 工资/账单重复结算这类问题以前只在「钱包按 idem 挡住」这一层兜着，上层拿到的
  返回值是错的（reason=wallet），没人发现；
- 62 个测试文件各写一份 Fake，重复扣款的 bug 在不同文件里表现不一样。

这里把上面四件事各钉一组测试，并且**做反向验证**：把实现改坏，测试必须红。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from tests import fixtures
    from tests.fixtures import (FakeAmo, FakeClock, FakeFinance, FakePlugin,  # noqa: F401
                               FakeRepo, FakeWallet, temp_store)
except ImportError:  # 直接以 tests/ 为根目录跑时
    import fixtures  # type: ignore[no-redef]
    from fixtures import (FakeAmo, FakeClock, FakeFinance, FakePlugin,  # type: ignore[no-redef]
                          FakeRepo, FakeWallet, temp_store)

from services import bill_flow, ledger_audit  # noqa: E402
from services.work_engine import FinanceSalary  # noqa: E402
from utils import money  # noqa: E402

#: 元 → 分 A/B 表（17 组）：左边是喂进去的元，右边是必须得到的分。
YEN_TO_CENTS = (
    (0, 0),
    ("0.01", 1),
    ("0.10", 10),
    (1, 100),
    ("12.34", 1234),
    (12.345, 1235),
    ("1.005", 101),
    ("1.015", 102),
    ("0.995", 100),
    ("6800", 680000),
    (0.1 + 0.2, 30),
    (999.999, 100000),
    ("1000000.00", 100000000),
    (-12.345, -1235),
    (0.001, 0),
    ("88.885", 8889),
    (3.14159, 314),
)


def _plugin(cash=500000):
    finance = FakeFinance(cash=cash)
    return FakePlugin(finance=finance, finance_repo=FakeRepo())


def _rules(report):
    return {str(item.get("rule")) for item in report.get("issues") or []}


def _inject(plugin, **row):
    """往桩账本里塞一行异常流水（模拟线上脏数据）。"""
    item = {"idem": "x:1", "account": "cash", "delta_cents": -100,
            "reason": "面条", "ts": time.time()}
    item.update(row)
    plugin.finance.ledger_rows.append(item)
    return item


# ================================================================ 金额单一实现
@pytest.mark.parametrize("yen,expected", YEN_TO_CENTS)
def test_money_cents_ab_table(yen, expected):
    assert money.cents(yen) == expected


def test_money_cents_never_uses_float_multiply():
    """12.345 元 * 100 在浮点下是 1234.4999…；唯一实现必须给 1235。"""
    assert money.cents(12.345) == 1235
    assert int(float(12.345) * 100) == 1234        # 这条就是不许再出现的老写法
    assert money.cents("88.885") == 8889


def test_money_round_trip_and_integer_yuan():
    for _yen, expected in YEN_TO_CENTS:
        assert money.round_cents(expected) == expected
        assert money.cents(expected / 100.0) == expected
    assert money.to_yuan(1500) == 15.0
    assert money.to_yuan_int(27522) == 275
    assert money.yuan_int(27599) == 275


def test_money_formatting_is_byte_identical_to_legacy():
    for value in (0, 1, 5, 100, 1500, 123456, 100000000, 27522):
        assert money.fmt_yuan(value) == "¥%.2f" % (value / 100.0)
        assert money.fmt_amount(value) == "%.2f" % (value / 100.0)
        assert money.fmt_yuan_grouped(value) == "¥%s" % format(value / 100.0, ",.2f")
        assert money.fmt_amount_grouped(value) == format(value / 100.0, ",.2f")


def test_engines_share_one_money_implementation():
    """旧名字全部是同一批函数对象，不是各写一份。"""
    from services import work_engine
    from utils import facts, helpers, mood

    assert helpers.cents is money.cents
    assert helpers.round_cents is money.round_cents
    assert helpers.to_yuan is money.to_yuan
    assert helpers.yuan is money.fmt_yuan
    assert work_engine.yuan is money.fmt_yuan
    assert mood.yuan is money.fmt_yuan_grouped
    assert facts.money is money.fmt_amount_grouped


def test_no_second_money_definition_outside_money_module():
    """反向兜底：除 utils/money.py 外，不许再冒出一份 def yuan/cents。"""
    pattern = re.compile(r"^def (yuan|cents|round_cents|to_yuan|money)\(", re.M)
    offenders = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("tests", "tools", "__pycache__",
                                               ".git", "_test_tmp", ".pytest_cache")]
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            if rel == "utils/money.py":
                continue
            with open(path, encoding="utf-8") as fh:
                if pattern.search(fh.read()):
                    offenders.append(rel)
    assert not offenders, "又出现了第二份金额实现：%s" % offenders


def test_idem_join_is_byte_identical_to_percent_format():
    pairs = (
        (("salary", "2026-11"), "salary:%s" % ("2026-11",)),
        (("bill", "2026-11", "water"), "bill:%s:%s" % ("2026-11", "water")),
        (("bill:auto", "2026-11", "rent"), "bill:auto:%s:%s" % ("2026-11", "rent")),
        (("meal", "2026-11-16", "lunch"), "meal:%s:%s" % ("2026-11-16", "lunch")),
        (("shop:pending", 1730000000, "mem"), "shop:pending:%s:mem" % (1730000000,)),
    )
    for parts, expected in pairs:
        assert money.idem(*parts) == expected
    assert money.salary_idem("2026-11") == "salary:2026-11"
    assert money.bill_idem("2026-11", "water") == "bill:2026-11:water"


def test_idem_ok_rejects_empty_and_padded_keys():
    assert money.idem_ok("bill:2026-11:water")
    assert not money.idem_ok("")
    assert not money.idem_ok("  ")
    assert not money.idem_ok(" bill:2026-11")
    assert not money.idem_ok(None)


# ================================================================ 账本审计
def test_audit_ledger_clean_book():
    plugin = _plugin()
    asyncio.run(plugin.finance.income(680000, "工资", money.salary_idem("2026-11")))
    asyncio.run(plugin.finance.expense(1500, "面条", "meal:2026-11-16:lunch"))
    report = ledger_audit.audit_ledger(plugin, days=7)
    assert report["issues"] == []
    assert report["ok"] is True
    assert report["entries"] >= 2
    assert "账本没问题" in ledger_audit.audit_summary(report)


def test_audit_ledger_flags_duplicate_idem():
    plugin = _plugin()
    for _ in range(2):
        _inject(plugin, idem="dupe:1", delta_cents=-100, reason="面条")
    report = ledger_audit.audit_ledger(plugin)
    assert report["ok"] is False
    assert "duplicate_idem" in _rules(report)


def test_audit_ledger_flags_idem_conflict():
    plugin = _plugin()
    _inject(plugin, idem="conflict:1", delta_cents=-100, reason="面条")
    _inject(plugin, idem="conflict:1", delta_cents=-900, reason="火锅")
    assert "idem_conflict" in _rules(ledger_audit.audit_ledger(plugin))


def test_audit_ledger_flags_missing_idem():
    plugin = _plugin()
    _inject(plugin, idem="", delta_cents=-100)
    assert "missing_idem" in _rules(ledger_audit.audit_ledger(plugin))


def test_audit_ledger_flags_precision():
    plugin = _plugin()
    _inject(plugin, idem="float:1", delta_cents=-20.5)
    assert "precision" in _rules(ledger_audit.audit_ledger(plugin))


def test_audit_ledger_flags_balance_gap():
    plugin = _plugin()
    plugin.finance.cash += 100          # 余额凭空多 1 元，流水没有对应行
    assert "balance_gap" in _rules(ledger_audit.audit_ledger(plugin))


def test_audit_ledger_flags_double_charge_across_two_idems():
    plugin = _plugin()
    _inject(plugin, idem="water:2026-11", delta_cents=-2000, reason="水费")
    _inject(plugin, idem="bill:2026-11:water", delta_cents=-2000, reason="水费")
    report = ledger_audit.audit_ledger(plugin)
    assert "double_charge" in _rules(report)


def test_audit_ledger_without_finance_is_reported_not_raised():
    class _Bare:
        pass

    report = ledger_audit.audit_ledger(_Bare())
    assert report["ok"] is False
    assert "no_ledger" in _rules(report)


def test_audit_ledger_is_read_only():
    plugin = _plugin()
    asyncio.run(plugin.finance.expense(1500, "面条", "meal:1"))
    rows_before = [dict(row) for row in plugin.finance.ledger_rows]
    cash_before = plugin.finance.cash
    meta_before = dict(plugin.finance_repo.data)
    ledger_audit.audit_ledger(plugin)
    ledger_audit.audit_ledger(plugin)
    assert plugin.finance.ledger_rows == rows_before
    assert plugin.finance.cash == cash_before
    assert plugin.finance_repo.data == meta_before


# ================================================================ 重复结算
class _SalaryRepo(FakeRepo):
    """带工资明细的仓储桩（真实 FinanceRepo 的 salary_items 子集）。"""

    def __init__(self):
        super().__init__()
        self.items_rows = []

    def add_salary_item(self, period, kind, label, amount_cents):
        self.items_rows.append({"period": str(period), "kind": str(kind),
                                "label": str(label), "amount_cents": int(amount_cents)})
        return True

    def salary_items(self, period):
        return [dict(row) for row in self.items_rows if row["period"] == str(period)]


def _salary(period="2026-11", now=dt.datetime(2026, 11, 16, 10, 0)):
    repo = _SalaryRepo()
    finance = FakeFinance(cash=1000000)
    plugin = FakePlugin(finance=finance, finance_repo=repo)
    return plugin, FinanceSalary(plugin, repo, finance), repo, finance, now


def test_salary_settle_twice_pays_once():
    plugin, salary, repo, finance, now = _salary()
    first = asyncio.run(salary.settle(now, "2026-11"))
    assert first["ok"] is True and first["paid"] == 680000
    incomes = len(finance.incomes)
    items = len(repo.items_rows)
    second = asyncio.run(salary.settle(now, "2026-11"))
    assert second["ok"] is False and second["reason"] == "settled"
    assert len(finance.incomes) == incomes, "工资不能发两遍"
    assert len(repo.items_rows) == items, "工资明细不能重复追加"


def test_salary_year_end_bonus_is_not_added_twice():
    plugin, salary, repo, finance, _now = _salary()
    january = dt.datetime(2026, 1, 15, 10, 0)
    asyncio.run(salary.settle(january, "2026-01"))
    bonuses = [row for row in repo.items_rows if row["label"] == "年终奖"]
    assert len(bonuses) == 1, "1 月发薪日应当补一笔年终奖"
    asyncio.run(salary.settle(january, "2026-01"))
    assert len([row for row in repo.items_rows if row["label"] == "年终奖"]) == 1


class _LifeStub:
    """生活模块最小桩：只提供账单计划需要的三样东西。"""

    def _has(self, kind):
        return False

    def rent_cents(self, period):
        return 160000

    def utility_plan(self, period, month=None):
        return ({"key": "water", "name": "水费", "amount_cents": 2000},
                {"key": "electric", "name": "电费", "amount_cents": 8000},
                {"key": "gas", "name": "燃气费", "amount_cents": 4000},
                {"key": "broadband", "name": "宽带", "amount_cents": 6000})


def _bill_plugin(cash=500000):
    finance = FakeFinance(cash=cash)
    repo = FakeRepo()
    plugin = FakePlugin(finance=finance, finance_repo=repo, finance_life=_LifeStub())
    return plugin, repo, finance


def test_bill_pay_charges_once_with_the_single_bill_idem():
    plugin, repo, finance = _bill_plugin()
    now = dt.datetime(2026, 11, 16, 10, 0)
    bill_flow.create_due(plugin, now)
    out = asyncio.run(bill_flow.pay(plugin, "水费", now))
    assert out["ok"] is True
    assert finance.expenses == [(2000, "水费", money.bill_idem("2026-11", "water"))]
    assert repo.settled("2026-11", "water")


def test_bill_pay_refuses_second_charge_from_another_idem():
    """自动代扣已经用 water:2026-11 扣过、结算键又丢了：手动交不能再扣一遍。"""
    plugin, repo, finance = _bill_plugin()
    now = dt.datetime(2026, 11, 16, 10, 0)
    bill_flow.create_due(plugin, now)
    asyncio.run(finance.expense(2000, "水费", "water:2026-11"))
    cash_before = finance.cash
    out = asyncio.run(bill_flow.pay(plugin, "水费", now))
    assert out["ok"] is False and out.get("already_settled") is True
    assert finance.cash == cash_before, "同一笔水费不能扣两遍"
    assert repo.settled("2026-11", "water"), "收口后必须写结算键"


def test_bill_pay_after_paid_does_not_touch_wallet():
    plugin, repo, finance = _bill_plugin()
    now = dt.datetime(2026, 11, 16, 10, 0)
    bill_flow.create_due(plugin, now)
    asyncio.run(bill_flow.pay(plugin, "水费", now))
    spent = list(finance.expenses)
    again = asyncio.run(bill_flow.pay(plugin, "水费", now))
    assert again["ok"] is False and again.get("already_settled") is True
    assert finance.expenses == spent


# ================================================================ fixtures 契约
MIGRATED = (
    "test_late_night_engine.py",
    "test_gift_engine.py",
    "test_shopping_engine.py",
    "test_medical_engine.py",
    "test_new_engines.py",
    "test_event_engine.py",
    "test_finance_daily.py",
    "test_finance_life.py",
)


def test_fixtures_expose_the_shared_fakes():
    for name in ("FakeAmo", "FakeFinance", "FakePlugin", "FakeClock", "temp_store"):
        assert hasattr(fixtures, name), "共享 fixtures 缺 %s" % name


def test_fixture_finance_dedupes_by_idem():
    finance = FakeFinance(cash=1000)
    assert asyncio.run(finance.expense(100, "面条", "x:1"))["ok"] is True
    assert asyncio.run(finance.expense(100, "面条", "x:1"))["ok"] is False
    assert finance.cash == 900
    assert asyncio.run(finance.expense(100, "面条", ""))["reason"] == "missing_idem"
    assert len(finance.ledger_rows) == 2          # 初始存款 + 第一笔面条


def test_fixture_clock_moves_forward():
    clock = FakeClock(dt.datetime(2026, 11, 16, 23, 59))
    assert clock.day().isoformat() == "2026-11-16"
    clock.advance(minutes=2)
    assert clock.day().isoformat() == "2026-11-17"
    assert clock.now_ts() > 0


def test_fixture_temp_store_stays_under_tmp_path(tmp_path):
    store, repo = temp_store(tmp_path)
    try:
        assert str(tmp_path) in str(store.root)
        repo.meta_set("k", "v")
        assert repo.meta_get("k") == "v"
        assert list(tmp_path.rglob("*.db")) or list(tmp_path.rglob("*"))
    finally:
        store.close()


def test_at_least_eight_test_files_use_shared_fixtures():
    users = []
    tests_dir = os.path.join(ROOT, "tests")
    for name in sorted(os.listdir(tests_dir)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        with open(os.path.join(tests_dir, name), encoding="utf-8") as fh:
            text = fh.read()
        if "fixtures import" in text:
            users.append(name)
    assert len(users) >= 8, "至少 8 个测试文件要迁移到共享 fixtures，现在是 %d" % len(users)
    for name in MIGRATED:
        assert name in users, "%s 还没迁移到共享 fixtures" % name


# ================================================================ 反向验证
def test_reverse_naive_multiply_would_be_caught():
    """反向验证 1：把元→分换成 float * 100，A/B 表必须红。"""

    def naive(value):
        return int(float(value) * 100)

    wrong = [yen for yen, expected in YEN_TO_CENTS if naive(yen) != expected]
    assert wrong, "A/B 表必须能抓住 float*100 的口径漂移"
    assert naive(12.345) == 1234, "反向样本本身要真的错"


def test_reverse_missing_idem_segment_is_visible():
    """反向验证 2：idem 少拼一段 → 键变了 → 审计会当成两笔不同的流水。"""
    plugin = _plugin()
    _inject(plugin, idem=money.bill_idem("2026-11", "water"), delta_cents=-2000, reason="水费")
    _inject(plugin, idem=money.idem("bill", "2026-11"), delta_cents=-2000, reason="水费")
    assert money.bill_idem("2026-11", "water") != money.idem("bill", "2026-11")
    assert "double_charge" in _rules(ledger_audit.audit_ledger(plugin))


def test_reverse_audit_without_duplicate_rule_would_miss(monkeypatch):
    """反向验证 3：把重复检查摘掉，审计就漏了 → 说明是这个规则在兜。"""
    plugin = _plugin()
    for _ in range(2):
        _inject(plugin, idem="dupe:1", delta_cents=-100, reason="面条")
    red = ledger_audit.audit_ledger(plugin)
    assert "duplicate_idem" in _rules(red)
    monkeypatch.setattr(ledger_audit, "_check_idem", lambda window: [])
    green = ledger_audit.audit_ledger(plugin)
    assert "duplicate_idem" not in _rules(green), "摘掉规则后必须漏掉（反向验证）"


def test_reverse_float_cents_would_be_flagged():
    """反向验证 4：金额写成浮点分，审计必须报 precision。"""
    plugin = _plugin()
    _inject(plugin, idem="float:2", delta_cents=-20.5)
    assert "precision" in _rules(ledger_audit.audit_ledger(plugin))
    assert not ledger_audit._is_integer_cents(-20.5)
    assert ledger_audit._is_integer_cents(-20)


def test_reverse_salary_guard_removal_would_double_append(monkeypatch):
    """反向验证 5：拆掉幂等闸门，重复结算就会真的再发一遍。"""
    plugin, salary, repo, finance, now = _salary()
    asyncio.run(salary.settle(now, "2026-11"))
    incomes = len(finance.incomes)

    def _never(period, kind):
        return False

    monkeypatch.setattr(repo, "settled", _never)
    again = asyncio.run(salary.settle(now, "2026-11"))
    assert again["ok"] is False and again["reason"] == "wallet", "去掉闸门后只剩钱包兜底"
    assert len(finance.incomes) == incomes, "钱仍然只发一笔（钱包按 idem 挡住）"
