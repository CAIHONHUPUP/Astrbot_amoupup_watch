# -*- coding: utf-8 -*-
"""理财 LLM 工具与理财菜单逻辑。"""
from __future__ import annotations

try:
    from ..guard import isolated
except ImportError:
    from guard import isolated


async def finance_reply(plugin, action="status", day="", event_kind=""):
    """理财/持仓/请假/职场事件的文本回复（LLM 工具入口）。"""
    try:
        act = str(action or "status").lower()
        if act == "positions":
            rows = plugin.finance_repo.positions() if plugin.finance_repo else []
            if not rows:
                return "她现在空仓。"
            return "\n".join("%s(%s) %s 股，现价 %.2f，浮动 %+.2f%%" % (
                r.get("name") or r.get("code"), r.get("code"), r.get("qty"),
                int(r.get("last_cents") or 0) / 100.0,
                ((int(r.get("last_cents") or 0) - int(r.get("cost_cents") or 0))
                 / max(1, int(r.get("cost_cents") or 0)) * 100.0)) for r in rows)
        if act in ("work", "职场"):
            if plugin.work_events is None:
                return "职场模块未就绪。"
            return plugin.work_events.prompt_line() or "今天职场没什么事。"
        if act in ("leave", "请假"):
            if plugin.finance_salary is None:
                return "工资模块未就绪。"
            result = await plugin.finance_salary.apply_leave(day=day or "")
            return ("好呀，正好有点累……不过请%s要扣 ¥%.2f，有点肉疼 QAQ。\n%s"
                    % (result["label"], abs(result["amount_cents"]) / 100.0, result["card"]))
        if act == "decide":
            if plugin.finance_decision is None:
                return "决策模块未就绪。"
            result = await plugin.finance_decision.run_slot("close")
            d = (result or {}).get("decision") or {}
            return "她决定：%s（%s）" % (d.get("reason") or "先不动", d.get("action") or "hold")
        mood = plugin.finance.mood()
        cents = plugin.finance.balance_cents() or 0
        return "她现在%s，可用余额 ¥%.2f。" % (mood.get("word") or "平静", cents / 100.0)
    except Exception as exc:
        isolated("finance.tool", exc)
        return "金融信息暂时读不到。"

