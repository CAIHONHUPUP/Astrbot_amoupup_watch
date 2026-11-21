# -*- coding: utf-8 -*-
"""邀约标题和地点必须一致（2026-11-21 线上：标题「妈妈中午回家吃饭」地点却是楼下小馆）。"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.social_rules import align_topic_place  # noqa: E402


def test_home_topic_is_rewritten_when_place_is_restaurant():
    assert align_topic_place("妈妈中午回家吃饭", "楼下小馆") == "妈妈中午一起吃饭"


def test_home_topic_kept_when_place_is_home():
    assert align_topic_place("妈妈中午回家吃饭", "家") == "妈妈中午回家吃饭"


def test_home_topic_kept_for_parents_home():
    # 2026-11-21 用户口径：可以写父母家，回家吃饭就该配父母家
    assert align_topic_place("妈妈中午回家吃饭", "父母家") == "妈妈中午回家吃饭"


def test_public_place_removed_when_place_is_home():
    text = align_topic_place("去楼下小馆吃饭", "家")
    assert "小馆" not in text


def test_unrelated_topic_unchanged():
    assert align_topic_place("周末去商场逛街", "商场") == "周末去商场逛街"


def test_empty_topic_safe():
    assert align_topic_place("", "楼下小馆") == ""
