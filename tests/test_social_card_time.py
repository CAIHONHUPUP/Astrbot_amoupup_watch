# -*- coding: utf-8 -*-
"""社交卡片：按预约日重新锚定「明晚」，时间显示预约时段（2026-11-18 误导事故）。"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.decisions_ext import _social_rows  # noqa: E402


class FakeRepo:
    def __init__(self, rows):
        self.rows = rows

    def events(self, limit=30):
        return list(self.rows)


def _row(day, text, start="19:00"):
    return {"kind": "social:accept", "day": day,
            "created_at": 1794895820.0,  # 2026-11-17 11:30
            "payload": {"npc": "沈幼微", "text": text, "amount_cents": 10000,
                        "kind": "other", "place": "楼下小馆", "start": start,
                        "end": "19:30"}}


def test_card_uses_appointment_time():
    day = dt.date.today().isoformat()
    rows = _social_rows(FakeRepo([_row(day, "表姐明晚约我楼下小馆")]), day)
    assert rows and rows[0]["time"] == "19:00"


def test_relative_word_is_reanchored_to_today():
    day = dt.date.today().isoformat()
    rows = _social_rows(FakeRepo([_row(day, "表姐明晚约我楼下小馆")]), day)
    assert "今晚" in rows[0]["detail"] and "明晚" not in rows[0]["detail"]
