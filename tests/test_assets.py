# -*- coding: utf-8 -*-
from services.assets import total_assets, breakdown

class Bank:
    def current_cents(self): return 100
    def fixed_cents(self): return 200

class P:
    bank = Bank()

def test_total_includes_deposits():
    assert total_assets(P(), cash_cents=1000, market_cents=500,
                        fixed_cents=20000, debt_cents=3000) == 1000 + 500 + 300 + 20000 - 3000

def test_breakdown_shape():
    out = breakdown(P(), cash_cents=1000, market_cents=0, fixed_cents=0, debt_cents=0)
    assert out["bank"] == 300 and out["total"] == 1300
