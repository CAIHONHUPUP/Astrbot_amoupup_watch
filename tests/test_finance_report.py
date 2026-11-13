# -*- coding: utf-8 -*-
"""面板生活账单读数：结算记录 + 钱包流水 → 面板字段。"""
import datetime as dt

from ui.report import life_block, market_block

TODAY = dt.date(2026, 11, 13)


def _settle(day, kind, payload):
    return {"day": day, "kind": kind, "payload": str(payload)}


def _flow(reason, cents, ts, account="cash"):
    stamp = dt.datetime(2026, 11, 13, 18, 0).timestamp() if ts is None else ts
    return {"reason": reason, "delta_cents": cents, "ts": stamp, "account": account}


def test_life_block_sums_this_month_settlements():
    rows = [
        _settle("2026-11", "rent", 185303),
        _settle("2026-11", "water", 1929),
        _settle("2026-11", "electric", 10917),
        _settle("2026-11", "gas", 3888),
        _settle("2026-11", "broadband", 6359),
        _settle("2026-10", "rent", 99999),          # 上个月不算
        _settle("2026-11", "rent", "has_house"),    # 非数字 payload 忽略
    ]
    life = life_block(rows, [], 680000, TODAY)
    assert life["rent"] == 1853.03
    assert life["utilities"] == 230.93
    assert life["salary"] == 6800
    assert life["month"] == "2026-11"


def test_life_block_month_expense_excludes_bills_and_income():
    rows = [_settle("2026-11", "rent", 185303)]
    flows = [
        _flow("面条 × 1", -1500, None),
        _flow("豆浆 × 1", -150, None),
        _flow("包子 × 1", -200, None),
        _flow("房租", -185303, None),      # 已算进 rent
        _flow("水费", -1929, None),        # 已算进 utilities
        _flow("工资", 680000, None),       # 收入
        _flow("买入 600519", -100000, None),  # 交易，另有卡片
        _flow("早餐", -300, dt.datetime(2026, 10, 31, 9, 0).timestamp()),  # 上个月
        _flow("可取余额初始化", 2090104, None, account="withdrawable"),
    ]
    life = life_block(rows, flows, 680000, TODAY)
    assert life["month_expense"] == 18.5
    assert life["rent"] == 1853.03


def test_life_block_next_salary_date():
    assert life_block([], [], 680000, dt.date(2026, 11, 13))["next_salary"] == "2026-11-15"
    assert life_block([], [], 680000, dt.date(2026, 11, 20))["next_salary"] == "2026-12-15"
    assert life_block([], [], 680000, dt.date(2026, 12, 31))["next_salary"] == "2027-01-15"


def test_market_block_counts_today_slots():
    assert market_block(["morning", "noon", "close"])["checks"] == 3
    assert market_block(["morning"])["checks"] == 1
    assert market_block([])["limit"] == 3
