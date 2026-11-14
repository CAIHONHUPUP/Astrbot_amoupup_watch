# -*- coding: utf-8 -*-
"""展示层：把账本组装成面板字段（总资产、账单、工资、持仓、行情）。"""
from __future__ import annotations

import datetime as _dt
import json
import time

try:
    from ..ui.report import life_block, market_block
    from ..guard import isolated
except ImportError:  # 离线测试 / 顶层加载
    from ui.report import life_block, market_block
    from guard import isolated


class CardMixin:
    def _finance_provider(self):
        """金融插件（Astrbot_amoupup_finance）装了就把面板交给它，没装回退空态。"""
        try:
            context = getattr(self, "context", None)
            getter = getattr(context, "get_registered_star", None) if context else None
            meta = getter("Astrbot_amoupup_finance") if callable(getter) else None
            if meta is None or getattr(meta, "activated", True) is False:
                return None
            instance = getattr(meta, "star_cls", None)
            if instance is not None and callable(getattr(instance, "snapshot", None)):
                return instance
        except Exception as exc:
            isolated("finance.provider", exc)
        return None

    def _snapshot_real(self) -> dict:
        cash = self.finance.balance_cents() or 0
        rows = []
        if self.finance_repo is not None:
            try:
                rows = self.finance_repo.positions()
            except Exception as exc:
                isolated("finance.snapshot.positions", exc)
        positions = []
        market_value = 0
        total_pnl = 0
        for row in rows:
            qty = float(row.get("qty") or 0)
            cost = int(row.get("cost_cents") or 0)
            last = int(row.get("last_cents") or cost)
            pnl = int(round((last - cost) * qty))
            base = cost * qty
            total_pnl += pnl
            market_value += int(round(last * qty))
            positions.append({
                "kind": row.get("kind"), "code": row.get("code"),
                "name": row.get("name") or row.get("code"), "qty": qty,
                "cost": cost / 100.0, "price": last / 100.0,
                "pnl": pnl / 100.0, "pnl_pct": (pnl / base * 100.0) if base else 0.0,
            })
        trades = []
        if self.finance_repo is not None:
            try:
                for row in self.finance_repo.trades(20):
                    amount = int(row.get("amount_cents") or 0)
                    if str(row.get("side")) in ("buy", "subscribe"):
                        amount = -amount
                    trades.append({
                        "day": time.strftime("%m-%d %H:%M", time.localtime(float(row.get("ts") or 0))),
                        "kind": {"buy": "买入", "sell": "卖出", "subscribe": "申购", "redeem": "赎回"}.get(
                            str(row.get("side")), str(row.get("side"))),
                        "target": (row.get("name") or "") + " " + str(row.get("code") or ""),
                        "amount": amount / 100.0, "fee": int(row.get("fee_cents") or 0) / 100.0,
                        "note": row.get("note") or "",
                    })
            except Exception as exc:
                isolated("finance.snapshot.trades", exc)
        loans = []
        if self.finance_repo is not None:
            try:
                loans = self.finance_repo.loans()
            except Exception:
                loans = []
        debt = sum(int(x.get("balance_cents") or 0) for x in loans)
        monthly = sum(int(x.get("monthly_cents") or 0) for x in loans)
        rate = max([float(x.get("rate") or 0) for x in loans] or [0.0])
        mood = self.finance.mood()
        decision = {}
        try:
            if self.finance_repo is not None:
                raw = self.finance_repo.meta_get("last_decision")
                decision = json.loads(raw) if raw else {}
        except Exception:
            decision = {}
        help_state = {}
        try:
            if self.finance_help is not None:
                help_state = self.finance_help.status()
                help_state["prompt"] = self.finance_help.prompt_text()
        except Exception:
            help_state = {}
        # 生活账单 / 决策时点：从自己库里的结算记录 + amo 的钱包流水聚合，
        # 不再写死 0（以前面板显示房租 0，但钱其实早扣了）。
        try:
            today = _dt.date.today()
            rows = self.finance_repo.settlements(today.strftime("%Y-%m")) if self.finance_repo else []
            ledger = self.finance.ledger(200) if self.finance is not None else []
            salary_cents = self.finance_life.salary_cents() if self.finance_life is not None else 0
            life = life_block(rows, ledger, salary_cents, today)
            keys = self.finance_repo.meta_keys("decision_done:" + today.isoformat()) if self.finance_repo else []
            market = market_block([str(k).split(":", 3)[-1] for k in keys], 3,
                                  "腾讯 qt.gtimg.cn", "每天三次：9:30 / 11:30 / 14:50")
        except Exception as exc:
            isolated("finance.snapshot.life", exc)
            life = {"salary": 0, "salary_day": 15, "next_salary": "", "rent": 0,
                    "rent_day": 1, "utilities": 0, "month_expense": 0}
            market = {"checks": 0, "limit": 3, "last": "", "source": "腾讯 qt.gtimg.cn"}
        # 固定资产 / 房贷车贷：总资产 = 现金 + 持仓 + 房产 + 车 − 未还本金
        prop = {}
        assets_value = 0
        try:
            if self.finance_property is not None:
                prop = self.finance_property.state()
                assets_value = (int(round(float(prop.get("house_value") or 0) * 100))
                                + int(round(float(prop.get("car_value") or 0) * 100)))
                try:
                    from ..services.city import city_view
                except ImportError:
                    from services.city import city_view
                life.update(city_view(self))
                for key in ("has_house", "has_car", "mortgage", "car_loan", "property_fee",
                            "car_cost", "house_value", "car_value", "down_pct",
                            "house_price", "car_price", "mortgage_balance", "car_loan_balance"):
                    if key in prop:
                        life[key] = prop[key]
        except Exception as exc:
            isolated("finance.snapshot.property", exc)
        # 工资：基本 + 奖金 − 扣款 = 实发（持久化的 base_salary_cents 为准）
        try:
            if self.finance_salary is not None:
                today = _dt.date.today()
                period = today.strftime("%Y-%m")
                sal = self.finance_salary.summary(period)
                life["salary"] = sal["base_cents"] / 100.0
                life["salary_bonus"] = sal["bonus_cents"] / 100.0
                life["salary_deduction"] = sal["deduction_cents"] / 100.0
                life["salary_net"] = sal["net_cents"] / 100.0
                life["salary_items"] = [{"label": item["label"],
                                         "amount": item["amount_cents"] / 100.0}
                                        for item in sal["items"]]
                life["salary_paid"] = (bool(self.finance_repo.settled(period, "salary"))
                                       if self.finance_repo else False)
                if self.work_events is not None:
                    life["work_today"] = self.work_events.today_events()
                life["thrifty_until"] = self.finance_salary.thrifty_until()
        except Exception as exc:
            isolated("finance.snapshot.salary", exc)
        # 本月日常开销：按分类汇总（餐饮/交通/日用/人情/冲动）
        try:
            if self.finance_expense is not None:
                summ = self.finance_expense.month_summary()
                if summ.get("total_cents"):
                    life["month_expense"] = summ["total_cents"] / 100.0
                life["expense_categories"] = [
                    {"key": item["key"], "label": item["label"],
                     "amount": item["amount_cents"] / 100.0}
                    for item in summ.get("categories") or []]
        except Exception as exc:
            isolated("finance.snapshot.expense", exc)
        return {
            "mode": "real",
            "wallet": {"total": (cash + market_value + assets_value - debt) / 100.0,
                       "available": cash / 100.0,
                       "assets": assets_value / 100.0, "liabilities": debt / 100.0,
                       "withdrawable": (self.finance.withdrawable_cents() or 0) / 100.0,
                       "market_value": market_value / 100.0,
                       "today_pnl": total_pnl / 100.0,
                       "today_pct": (total_pnl / max(1, market_value - total_pnl) * 100.0)},
            "positions": positions,
            "trades": trades,
            "life": life,
            "mood": {"score": mood.get("score"), "dominant": mood.get("dominant"),
                     "last_event": ""},
            "market": market,
            "bank": {"debt": debt / 100.0, "monthly": monthly / 100.0, "rate": rate,
                     "limit": 0, "items": loans, "note": ""},
            "decision": decision,
            "help": help_state,
            "plan": {"deposit": cash / 100.0, "invest_cap": 50,
                     "house": {"price": prop.get("house_price") or 0,
                               "down_pct": prop.get("down_pct") or 30,
                               "years": prop.get("house_years") or [10, 20, 30],
                               "monthly": prop.get("mortgage") or 0,
                               "owned": bool(prop.get("has_house"))},
                     "car": {"price": prop.get("car_price") or 0, "years": 5,
                             "owned": bool(prop.get("has_car"))},
                     "rule": "她不会买买不起的；投资不超过存款 50%"},
        }

    def _finance_snapshot(self, demo: bool = False) -> dict:
        view = self._finance_snapshot_raw(demo)
        try:
            bank = getattr(self, "bank", None)
            if bank is None:
                try:
                    from ..services.bank_engine import build_bank
                except ImportError:
                    from services.bank_engine import build_bank
                bank = build_bank(self)
                self.bank = bank
            if isinstance(view, dict) and bank is not None:
                cur, fix = int(bank.current_cents()), int(bank.fixed_cents())
                view["bank"] = {"current_cents": cur, "fixed_cents": fix}
                if "total_cents" in view:
                    view["total_cents"] = int(view.get("total_cents") or 0) + cur + fix
        except Exception as exc:
            isolated("finance.bank.snapshot", exc)
        return view

    def _finance_snapshot_raw(self, demo: bool = False) -> dict:
        """金融概览：优先问金融插件，没有就回退空态（契约见 docs/INTEGRATION-MAP.md）。"""
        if not demo:
            provider = self._finance_provider()
            if provider is not None:
                try:
                    view = provider.snapshot()
                    if isinstance(view, dict):
                        return view
                except Exception as exc:
                    isolated("finance.snapshot", exc)
        if not demo:
            try:
                return self._snapshot_real()
            except Exception as exc:
                isolated("finance.snapshot", exc)
        if demo:
            return {
                "mode": "demo",
                "wallet": {"total": 128430.22, "available": 23180.22, "market_value": 105250.0,
                           "today_pnl": 3746.0, "today_pct": 3.69},
                "positions": [
                    {"kind": "stock", "code": "600519", "name": "贵州茅台", "qty": 100, "cost": 1600.0,
                     "price": 1650.0, "pnl": 5000.0, "pnl_pct": 3.13},
                    {"kind": "stock", "code": "300750", "name": "宁德时代", "qty": 500, "cost": 180.0,
                     "price": 176.0, "pnl": -2000.0, "pnl_pct": -2.22},
                    {"kind": "fund", "code": "005827", "name": "易方达蓝筹", "qty": 12345.67, "cost": 2.10,
                     "price": 2.18, "pnl": 987.65, "pnl_pct": 3.81},
                    {"kind": "fund", "code": "110020", "name": "沪深300ETF联接", "qty": 8000, "cost": 1.30,
                     "price": 1.27, "pnl": -240.0, "pnl_pct": -2.31},
                ],
                "trades": [
                    {"day": "11-13 10:32", "kind": "买入", "target": "600519 贵州茅台", "amount": -160000, "fee": 45.0, "note": "100 股"},
                    {"day": "11-12 15:00", "kind": "赎回", "target": "110020 沪深300ETF联接", "amount": 2540.0, "fee": 12.7, "note": "T+1 确认"},
                    {"day": "11-15 09:00", "kind": "工资", "target": "工资到账", "amount": 6800.0, "fee": 0, "note": "每月 15 日"},
                ],
                "life": {"salary": 6800, "salary_day": 15, "next_salary": "2026-12-15",
                         "rent": 0, "rent_day": 1, "utilities": 320, "month_expense": 2870.0},
                "mood": {"score": 68, "dominant": "开心", "last_event": "茅台止盈 +¥5,000"},
                "market": {"checks": 3, "limit": 3, "last": "9:30 / 11:30 / 14:50 各查一次", "source": "腾讯 qt.gtimg.cn"},
                "bank": {"debt": 452000.0, "monthly": 2380.0, "rate": 3.1, "limit": 120000.0,
                         "items": [{"kind": "房贷", "balance": 452000.0, "rate": 3.1, "years": 30}],
                         "note": "房贷剩余 452,000 · 3.1% · 月供 2,380"},
                "plan": {"deposit": 23180.22, "invest_cap": 50,
                         "house": {"price": 1200000, "down_pct": 30, "years": [10, 20, 30], "monthly": 3580},
                         "car": {"price": 120000, "years": 5},
                         "rule": "她不会买买不起的；投资不超过存款 50%；存款够全款才考虑全款"},
            }
        return {
            "mode": "real",
            "wallet": {"total": 0, "available": 0, "market_value": 0, "today_pnl": 0, "today_pct": 0},
            "positions": [], "trades": [],
            "life": {"salary": 6800, "salary_day": 15, "next_salary": "", "rent": 0,
                     "rent_day": 1, "utilities": 0, "month_expense": 0},
            "mood": {"score": None, "dominant": "", "last_event": ""},
            "market": {"checks": 0, "limit": 3, "last": "每天三次：9:30 / 11:30 / 14:50", "source": "腾讯 qt.gtimg.cn"},
            "bank": {"debt": 0, "monthly": 0, "rate": 0, "limit": 0, "items": [], "note": "没钱时可以先贷款，或找他转账"},
            "plan": {"deposit": 0, "invest_cap": 50,
                     "house": {"price": 0, "down_pct": 30, "years": [10, 20, 30], "monthly": 0},
                     "car": {"price": 0, "years": 5},
                     "rule": "她不会买买不起的；投资不超过存款 50%；存款够全款才考虑全款"},
        }


    def _fmt_diag(self, data: dict) -> str:
        store = data.get("store") or {}
        tasks = data.get("tasks") or {}
        lag = data.get("lag") or {}
        lines = [
            "[%s] %s gen=%s status=%s" % (data.get("display_name") or "我能永远守护你",
                                          data.get("version", "?"), data.get("generation", "?"),
                                          data.get("status", "?")),
            "任务 %s 个: %s" % (tasks.get("count", 0), ",".join(tasks.get("names", [])) or "-"),
            "loop lag p99=%ss max=%ss hits=%s" % (lag.get("p99", 0), lag.get("max", 0), lag.get("hits", 0)),
        ]
        if store:
            lines.append("库 events=%s people=%s edges=%s settle=%s %.2fMB free=%.0fMB readonly=%s" % (
                store.get("events", "-"), store.get("people", "-"), store.get("edges", "-"),
                store.get("settlements", "-"), store.get("db_mb", 0), store.get("free_mb", 0),
                store.get("readonly", "-")))
        return "\n".join(lines)

    async def _handle_watch(self, event) -> str:
        text = (getattr(event, "message_str", "") or "").strip()
        parts = [p for p in text.split() if p]
        if parts and parts[0].lstrip("/").lower() in ("watch", "守护"):
            parts = parts[1:]
        if parts and parts[0].lower() == "test":
            return await self._handle_test(parts[1:])
        if parts and parts[0].lower() in ("finance", "金融", "钱"):
            return self._fmt_finance()
        return self._fmt_diag(await self.diag())

    def _fmt_finance(self) -> str:
        """只给自然语言情绪，不出现心情数值。"""
        mood = self.finance.mood()
        cents = self.finance.balance_cents()
        money = "余额读取失败" if cents is None else "可用余额 ¥%s" % format(cents / 100.0, ",.2f")
        holdings = []
        try:
            if self.finance_repo is not None:
                for row in self.finance_repo.positions()[:3]:
                    holdings.append("%s %s 股" % (row.get("name") or row.get("code"), row.get("qty")))
        except Exception as exc:
            isolated("finance.text.positions", exc)
        tail = ("，持有 " + "、".join(holdings)) if holdings else "，现在空仓"
        return "她现在%s。%s%s。" % (mood.get("word") or "平静", money, tail)

    async def _handle_test(self, args) -> str:
        if not bool(self.config.get("allow_test_commands")):
            return "测试命令没开（allow_test_commands=false）"
        kind = args[0].lower() if args else ""
        if kind == "raise":
            raise RuntimeError("watch test raise（故意抛，验证隔离）")
        if kind == "busy":
            seconds = float(args[1]) if len(args) > 1 else 10.0
            seconds = max(0.1, min(seconds, 60.0))
            mode = (args[2].lower() if len(args) > 2 else "good")
            if mode == "bad":
                time.sleep(seconds)          # 反例：故意卡住事件循环
            else:
                await self.runtime.run_io(time.sleep, seconds)
            return "busy %.1fs mode=%s lag=%s" % (seconds, mode, self.runtime.lag_stats())
        return "未知测试：raise | busy <秒> [good|bad]"

    def _format_money(self, sub=""):
        sub = str(sub or "").strip()
        repo = self.finance_repo
        cash = self.finance.balance_cents() or 0
        rows = repo.positions() if repo else []
        market = sum(int(r.get("last_cents") or 0) * float(r.get("qty") or 0) for r in rows)
        loans = repo.loans() if repo else []
        debt = sum(int(x.get("balance_cents") or 0) for x in loans)
        word = self.finance.mood().get("word") or "平静"
        if sub in ("", "菜单"):
            return ("理财菜单：1 总资产 2 持仓 3 贷款 4 日程 0 关闭\n"
                    "也可以直接发 /理财 总资产、/理财 持仓、/理财 贷款、/理财 日程、/理财 关闭")
        if sub in ("总资产", "1"):
            return "总资产 ¥%.2f（余额 ¥%.2f + 持仓 ¥%.2f − 负债 ¥%.2f），她现在%s。" % (
                (cash + market - debt) / 100.0, cash / 100.0, market / 100.0, debt / 100.0, word)
        if sub in ("持仓", "2"):
            if not rows:
                return "她现在空仓，%s。" % word
            return "\n".join("%s(%s) %s股 成本%.2f 现价%.2f %+.1f%%" % (
                r.get("name") or r.get("code"), r.get("code"), r.get("qty"),
                int(r.get("cost_cents") or 0) / 100.0, int(r.get("last_cents") or 0) / 100.0,
                ((int(r.get("last_cents") or 0) - int(r.get("cost_cents") or 0))
                 / max(1, int(r.get("cost_cents") or 0)) * 100.0)) for r in rows)
        if sub in ("贷款", "3"):
            if not loans:
                return "她现在没有贷款。"
            return "\n".join("%s 本金%.2f 年利率%.2f%% %d期 每期%.2f 剩余%.2f" % (
                x.get("kind"), int(x.get("principal_cents") or 0) / 100.0, float(x.get("rate") or 0) * 100,
                int(x.get("months") or 0), int(x.get("monthly_cents") or 0) / 100.0,
                int(x.get("balance_cents") or 0) / 100.0) for x in loans)
        if sub in ("日程", "4"):
            try:
                day = time.strftime("%Y-%m-%d")
                amo = self.finance._amo()
                items = (amo.state.schedule.get(day) or []) if amo is not None else []
            except Exception:
                items = []
            lines = [x for x in items if str(x.get("category")) == "finance"]
            if not lines:
                return "今天没有财务日程。"
            return "\n".join("%s-%s %s %s" % (x.get("start"), x.get("end"), x.get("activity"), x.get("detail") or "") for x in lines)
        if sub in ("关闭", "0"):
            if not rows:
                return "持仓简报：当前空仓。"
            return "持仓简报：\n" + "\n".join("%s %s股 成本%.2f 现价%.2f %+.1f%%" % (
                r.get("name") or r.get("code"), r.get("qty"),
                int(r.get("cost_cents") or 0) / 100.0, int(r.get("last_cents") or 0) / 100.0,
                ((int(r.get("last_cents") or 0) - int(r.get("cost_cents") or 0))
                 / max(1, int(r.get("cost_cents") or 0)) * 100.0)) for r in rows)
        return "没听懂，发 /理财 看菜单。"

