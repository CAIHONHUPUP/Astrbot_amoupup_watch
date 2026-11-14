# -*- coding: utf-8 -*-
"""防回归：代码里不允许出现「她自主离开城市」的念头。"""
import glob
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANNED = ("逃离", "回老家", "出走", "搬离", "换个城市生活")


def test_no_escape_city_logic_in_code():
    for path in glob.glob(os.path.join(ROOT, "**", "*.py"), recursive=True):
        if "__pycache__" in path or os.sep + "tests" + os.sep in path:
            continue
        text = io.open(path, encoding="utf-8").read()
        for word in BANNED:
            assert word not in text, "%s 里出现了「%s」" % (os.path.basename(path), word)


def test_relocate_only_called_from_command():
    main = io.open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    calls = [line for line in main.splitlines() if "relocate(" in line and not line.strip().startswith(("from ", "def "))]
    assert len(calls) == 1, calls
