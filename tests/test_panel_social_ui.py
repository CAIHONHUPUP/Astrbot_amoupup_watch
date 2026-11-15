# -*- coding: utf-8 -*-
"""社交面板不许有「上帝之手」按钮：只能看她自然发生的动态。"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL = os.path.join(ROOT, "views", "守护", "index.html")


def _panel():
    with open(PANEL, encoding="utf-8") as fh:
        return fh.read()


def test_no_manual_invite_buttons():
    body = _panel()
    assert "抛个邀约" not in body
    assert "让她决定" not in body
    assert "socialTrigger" not in body
    assert "socialDecide" not in body


def test_no_frontend_social_trigger_calls():
    body = _panel()
    assert 'action: "invite"' not in body
    assert 'action: "decide"' not in body
