# -*- coding: utf-8 -*-
"""LLM 工具包装层：只做参数校验/异常捕获/转发 services，不写业务逻辑。"""
from __future__ import annotations

try:
    from astrbot.api.event import filter
except Exception:
    filter = None


def _optional_hook(name):
    finder = getattr(filter, name, None) if filter is not None else None
    if callable(finder):
        return finder

    def _decorator(*args, **kwargs):
        def _apply(func):
            return func
        return _apply
    return _decorator


class ToolWrappersMixin:
        @_optional_hook("llm_tool")("buy_insurance")

        async def buy_insurance(self, event, insurance_type: str = ""):

            """主动购买商业保险：e生保/医无忧/金钟罩/重疾险/交强险/商业车险/家财险。"""

            try:

                from .core.llm_tools import buy_insurance_tool

            except ImportError:

                from core.llm_tools import buy_insurance_tool

            return await buy_insurance_tool(self, insurance_type)



        @_optional_hook("llm_tool")("buy_gift")

        async def buy_gift(self, event, target_name: str = "", gift_name: str = "", price: float = 0.0):

            """给家人/朋友买生日礼物，会自动扣款并写记忆。"""

            try:

                from .core.llm_tools import buy_gift_tool

            except ImportError:

                from core.llm_tools import buy_gift_tool

            return await buy_gift_tool(self, target_name, gift_name, price)



        @_optional_hook("llm_tool")("deposit_money")

        async def deposit_money(self, event, amount: float = 0.0, account_type: str = "current", term_months: int = 0):

            """把闲钱存成活期/定期（定期期限 3/6/12/36 个月），由她自己决定。"""

            try:

                from .core.llm_tools import deposit_money_tool

            except ImportError:

                from core.llm_tools import deposit_money_tool

            return await deposit_money_tool(self, amount, account_type, term_months)



        @_optional_hook("llm_tool")("make_date")
        async def make_date_tool(self, event, title: str = "", when: str = "", deposit: float = 0):

            """约好一件事：写进日程并进入期待状态。when 例：2026-11-15 18:00 / 明天18:00。"""

            try:

                from .services.date_engine import make_date

            except ImportError:

                from services.date_engine import make_date

            out = await make_date(self, title, when, int(float(deposit or 0) * 100))

            return out.get("message") or "好呀"



        @_optional_hook("llm_tool")("cancel_date")
        async def cancel_date_tool(self, event, title: str = "", reason: str = ""):

            """用户临时去不了/放鸽子：触发情绪、定金、安慰消费、冷淡和记忆。"""

            try:

                from .services.date_engine import cancel_date

            except ImportError:

                from services.date_engine import cancel_date

            out = await cancel_date(self, title, reason)

            return out.get("message") or "最近没有约好的事呀。"



        @_optional_hook("llm_tool")("attend_date")
        async def attend_date_tool(self, event, title: str = "", mode: str = ""):

            """赴约结算：置装、买单模式（老公请客/她请客/AA）、约会中状态。"""

            try:

                from .services.date_engine import attend_date

            except ImportError:

                from services.date_engine import attend_date

            out = await attend_date(self, title, mode)

            return out.get("message") or "我出门啦"



        @_optional_hook("llm_tool")("propose_date")
        async def propose_date_tool(self, event, activity: str = "", time: str = ""):

            """老公提议吃火锅/逛街/看电影时，答应并写进日程。"""

            try:

                from .core.llm_tools import propose_date_tool

            except ImportError:

                from core.llm_tools import propose_date_tool

            return await propose_date_tool(self, activity, time)
