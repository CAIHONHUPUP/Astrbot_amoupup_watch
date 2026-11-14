# -*- coding: utf-8 -*-
"""/守护面板 综合卡：财务/生活/状态/社交一眼看全。"""
from __future__ import annotations

try:
    from ..guard import isolated
    from ..utils.helpers import yuan
except ImportError:
    from guard import isolated
    from utils.helpers import yuan


def dashboard_text(plugin):
    try:
        finance = getattr(plugin, "finance", None)
        repo = getattr(plugin, "finance_repo", None)
        cash = int(finance.balance_cents() or 0) if finance else 0
        mood = (finance.mood() or {}).get("word", "—") if finance else "—"
        positions = repo.positions() if repo else []
        market = sum(int(p.get("qty") or 0) * int(p.get("last_price_cents") or 0) for p in positions)
        loans = sum(int(r.get("balance_cents") or 0) for r in (repo.loans() if repo else [])) if hasattr(repo, "loans") else 0
        month = __import__("datetime").date.today().strftime("%Y-%m")
        expense = sum(int(r.get("amount_cents") or 0) for r in (repo.expenses(month) if hasattr(repo, "expenses") else []))
        city = (getattr(plugin, "config", {}) or {}).get("city") or ""
        medical = getattr(plugin, "medical", None)
        sick = bool(medical.is_sick()) if medical is not None else False
        social = getattr(plugin, "social", None)
        last = ""
        if social is not None:
            rows = social.repo.npcs()[:3]
            last = "；".join("%s：%s" % (r["name"], r.get("last_event") or "最近没动静") for r in rows)
        total = cash + market
        lines = [
            "### 她的综合状态",
            "- **财务**：可用 %s ｜ 持仓 %s ｜ 待还贷款 %s ｜ 总资产 %s"
            % (yuan(cash), yuan(market), yuan(loans), yuan(total)),
            "- **生活**：城市 %s ｜ 本月支出 %s ｜ 房 %s ｜ 车 %s"
            % (city or "未设置", yuan(expense),
               "有" if (repo.assets_of("house") if hasattr(repo, "assets_of") else []) else "无",
               "有" if (repo.assets_of("car") if hasattr(repo, "assets_of") else []) else "无"),
            "- **状态**：情绪 %s ｜ 生病 %s ｜ 负债 %s"
            % (mood, "是" if sick else "否", "是" if loans else "否"),
        ]
        if last:
            lines.append("- **社交**：" + last)
        return "\n".join(lines)
    except Exception as exc:
        isolated("dashboard.text", exc)
        return "面板生成失败：%s" % exc
