# -*- coding: utf-8 -*-
"""面板输出消毒：所有发往前端的结构先过这一层。

规则（2026-11-15 用户要求）：
- None / NaN / inf -> ""，避免前端出现 null / NaN；
- error / exception / traceback / api 这类底层字样 -> 换成友好文案；
- 调试词（系统误记 / 微型事件 / JIT回滚 / 原路退回）-> 人话（退款）；
- 只做展示层，不改数据库里的历史流水。
"""
from __future__ import annotations

import math
import re

FRIENDLY = "暂时读不到，稍后再看"

#: 数据里残留的调试词 -> 展示成什么
DEBUG_WORDS = (
    ("JIT:", ""),
    ("系统误记", "退款"),
    ("微型事件误触发", "小插曲"),
    ("微型事件", "小插曲"),
    ("JIT回滚", "退款"),
    ("原路退回", "退款"),
    ("JIT", ""),
    ("回滚", "退款"),
)

#: 出现这些词说明底层报错漏到了前端
ERROR_MARKS = ("traceback", "exception", "keyerror", "typeerror", "attributeerror",
               "timeouterror", "httperror", "http 5", "http 4", " 500", " 502",
               " 403", " 404", "api error", "error:")


#: 历史流水 reason 里残留的「大类/小类：」前缀（只影响展示，不改库）
LABEL_PREFIX_RE = re.compile(
    r"^(?:【[^】]{1,10}】\s*)?"
    r"(?:餐饮|日常|其他|交通|购物|人情|保险|固定账单|夜宵|早餐|午餐|晚餐|奶茶|打车|地铁|"
    r"衣服|礼物|随礼|房租|工资|收入|支出|退款|网购|存款|取款|搬家|约会类|医疗|学习|娱乐)"
    r"\s*[：:]\s*"
)


def strip_label_prefix(value) -> str:
    """去掉「餐饮：」「日常：」「其他：」这类前缀，只留具体物品/原因。"""
    text = str(value or "").strip()
    for _ in range(3):  # 少量嵌套前缀也能剥干净
        new_text = LABEL_PREFIX_RE.sub("", text)
        if new_text == text:
            break
        text = new_text.strip()
    return text


def sanitize_ledger_item(item):
    """流水行：先消毒，再把 reason/item_name 的前缀剥掉（历史数据不动，只影响展示）。"""
    data = sanitize_for_ui(item)
    if not isinstance(data, dict):
        return data
    for key in ("reason", "item_name", "sub_category"):
        if isinstance(data.get(key), str):
            data[key] = strip_label_prefix(data[key])
    return data


def _clean_text(value) -> str:
    text = str(value or "").strip()
    for old, new in DEBUG_WORDS:
        text = text.replace(old, new)
    text = " ".join(text.split())
    low = text.lower()
    if any(mark in low for mark in ERROR_MARKS):
        return FRIENDLY
    return text


def sanitize_for_ui(value, _depth: int = 0):
    """递归消毒；字典/列表结构保持不变，只清洗叶子值。"""
    if _depth > 8:
        return ""
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else ""
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, dict):
        return {str(key): sanitize_for_ui(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_for_ui(item, _depth + 1) for item in value]
    return _clean_text(value)
