# -*- coding: utf-8 -*-
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from utils.humanize import human_money, humanize_numbers, has_cents  # noqa: E402


def test_human_money():
    assert human_money(27522) == "两百多"
    assert human_money(35707) == "三百多"
    assert human_money(8000) == "八十块"
    assert human_money(8500) == "八十多块"
    assert human_money(20000) == "两百"
    assert human_money(1500) == "十几块"
    assert human_money(500) == "几块"
    assert human_money(123456) == "一千多"


def test_humanize_numbers():
    assert "275.22" not in humanize_numbers("和柳如烟逛街花了 ¥275.22")
    assert "两百多" in humanize_numbers("和柳如烟逛街花了 ¥275.22")
    assert "三百" in humanize_numbers("随礼 357.07 元")
    out = humanize_numbers("明天 14:00 去逛街，11-16 见")
    assert "14:00" in out and "11-16" in out


def test_has_cents():
    assert has_cents("花了 ¥275.22")
    assert not has_cents("预算两百多")
