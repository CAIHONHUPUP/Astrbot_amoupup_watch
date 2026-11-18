# -*- coding: utf-8 -*-
"""金额（分/元）唯一实现：换算 / 四舍五入 / 格式化 / 幂等键拼接。

为什么集中在这里（Phase 15）：
改造前同一件事散落在五个地方——utils.helpers 的 cents/round_cents/yuan、
services/work_engine 自己写的 yuan（少一次 round）、utils.mood 的千分位 yuan、
utils.facts 的 money、utils/humanize 里的 int(round(...))。每多一份实现，就多一次
「流水 ¥12.34、日记 12.35、卡片 ¥ 12.35」的口径漂移。

约定：
- 金额一律用「分」的 int 表示，不用浮点存钱；
- 元 → 分用 Decimal 十进制四舍五入（12.345 → 1235 分），不用 * 100；
- 所有对外文本都必须经过本模块的格式化函数；
- 幂等键（idem）文本也在这里拼，保证「同一笔账」在任何入口都得到同一个键。

本模块是**纯函数**：不碰数据库、不碰网络、不碰文件、不读配置。
旧名字全部保留为 re-export（helpers.cents / work_engine.yuan 等），调用方不必改。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

#: 1 元 = 100 分；金额最小单位就是「分」。
CENT = 100
#: 分是十进制两位，元 → 分必须先量化再乘 100，避免 12.345*100=1234.4999…
QUANT = Decimal("0.01")


# ---------------------------------------------------------------- 换算
def cents(value):
    """元 → 分（十进制四舍五入）：12.345 元 → 1235 分。"""
    amount = Decimal(str(value)).quantize(QUANT, rounding=ROUND_HALF_UP)
    return int(amount * CENT)


def round_cents(value):
    """分 → 分（只做整数取整，容忍 0.6 这类浮点残差）：1500.6 → 1501。"""
    return int(round(float(value)))


def to_yuan(value_cents):
    """分 → 元（float，用于除法计算，不用于展示）：1500 → 15.0。"""
    return round_cents(value_cents) / float(CENT)


def to_yuan_int(value_cents):
    """分 → 整数元（四舍五入，人话化/档位判断用）：27522 → 275。"""
    return int(round(int(value_cents) / float(CENT)))


def yuan_int(value_cents):
    """分 → 整数元（向下取整，写「花了 N 块」这种整元叙述用）：27522 → 275。"""
    return int(value_cents) // CENT


# ---------------------------------------------------------------- 格式化
def fmt_yuan(value_cents):
    """分 → 「¥12.34」（流水/卡片统一口径）。"""
    return "¥%.2f" % to_yuan(value_cents)


#: 兼容旧名字：utils.helpers.yuan / services.work_engine.yuan 都指向这一份实现。
yuan = fmt_yuan


def fmt_amount(value_cents):
    """分 → 「12.34」（不要货币符号的句子用）。"""
    return "%.2f" % to_yuan(value_cents)


def fmt_yuan_grouped(value_cents):
    """分 → 「¥1,234.56」（带千分位，心情/统计口径）。"""
    return "¥%s" % format(int(value_cents) / float(CENT), ",.2f")


def fmt_amount_grouped(value_cents):
    """分 → 「1,234.56」（带千分位、无货币符号，账本事实口径）。"""
    return format(int(value_cents or 0) / float(CENT), ",.2f")


# ---------------------------------------------------------------- 幂等键
def idem(*parts):
    """幂等键拼接：固定用「:」连接，逐段 str()。

    只做拼接，不做任何裁剪/小写/哈希——同一笔账在任何入口拼出来必须逐字节相同，
    否则钱包会当成两笔钱（重复扣）或把第二笔当重复丢掉（丢写）。
    """
    return ":".join(str(part) for part in parts)


def idem_ok(value):
    """幂等键是否可用（非空、无前后空白）。记账入口和账本审计共用。"""
    text = str(value or "")
    return bool(text.strip()) and text == text.strip()


def bill_idem(period, kind):
    """账单这类「按期 + 按项」的幂等键：bill:2026-11:water。"""
    return idem("bill", period, kind)


def salary_idem(period):
    """工资的幂等键：salary:2026-11（15 日发的钱每月只有一笔）。"""
    return idem("salary", period)


__all__ = [
    "CENT", "QUANT",
    "cents", "round_cents", "to_yuan", "to_yuan_int", "yuan_int", "yuan",
    "fmt_yuan", "fmt_amount", "fmt_yuan_grouped", "fmt_amount_grouped",
    "idem", "idem_ok", "bill_idem", "salary_idem",
]
