# -*- coding: utf-8 -*-
"""HTTP API 层：面板与接口处理（由 main 的 WatchPlugin 继承调用）。

保持 class 形式，是因为这些方法都依赖 self.finance_* / self.work_events；
放成 mixin 可以原样搬运，不改一行业务代码。
"""
from __future__ import annotations

import datetime as _dt
import json
import time

try:
    from ..guard import isolated
except ImportError:  # 离线测试 / 顶层加载
    from guard import isolated


class ApiMixin:
    async def _api_finance_demo(self, request=None):
        try:
            return self._finance_snapshot(demo=True)
        except Exception as exc:
            isolated("web.finance_demo", exc)
            return {"status": "error", "message": "demo failed"}

    async def _api_finance(self, request=None):
        try:
            demo = False
            try:
                raw = str(getattr(request, "url", "") or "")
                query = getattr(request, "query_params", None)
                if query:
                    raw += " " + str(dict(query))
                demo = "demo=1" in raw
            except Exception:
                pass
            return self._finance_snapshot(demo=demo)
        except Exception as exc:
            isolated("web.finance", exc)
            return {"status": "error", "message": "finance failed"}

    async def _read_json(self):
        try:
            from astrbot.api.web import request as web_request
            if web_request is not None:
                try:
                    data = await web_request.json(default={})
                except TypeError:      # 老版本没有 default 参数
                    data = await web_request.json()
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            isolated("web.body", exc)
        return {}

    async def _api_trade(self, request=None):
        try:
            body = await self._read_json()
            action = str(body.get("action") or "").lower()
            code = str(body.get("code") or "").strip()
            qty = body.get("qty") or 0
            price = body.get("price") or None
            idem = str(body.get("idem") or "")
            trading = self.trading
            if trading is None:
                return {"ok": False, "message": "交易模块未就绪"}
            if action == "buy":
                return await trading.buy(code, qty, price, idem)
            if action == "sell":
                return await trading.sell(code, qty, price, idem)
            if action == "subscribe":
                amount_cents = int(round(float(body.get("qty") or body.get("amount") or 0) * 100))
                return await trading.subscribe(code, amount_cents, price, idem)
            if action == "redeem":
                return await trading.redeem(code, qty, price, idem)
            return {"ok": False, "message": "未知操作"}
        except Exception as exc:
            isolated("web.trade", exc)
            return {"ok": False, "message": "交易失败：%s" % exc}

    async def _api_city(self, request=None):
        """GET 看当前城市；POST {city:"东莞"} 搬家（同步 amo 面板城市与天气）。"""
        try:
            try:
                from ..services.city import city_view, move_to
            except ImportError:
                from services.city import city_view, move_to
            body = await self._read_json()
            name = str(body.get("city") or "").strip()
            if not name:
                return {"ok": True, **city_view(self)}
            moved = await move_to(self, name)
            self.city_override = name
            if self.finance_repo is not None:
                try:
                    self.finance_repo.meta_set("city", name)
                except Exception as exc:
                    isolated("web.city.meta", exc)
            return {"ok": True, "moved": moved, **city_view(self)}
        except Exception as exc:
            isolated("web.city", exc)
            return {"ok": False, "message": "城市接口失败：%s" % exc}

    async def _api_work(self, request=None):
        """GET 今日职场状态；POST {kind: late|sick|project|overtime} 触发事件。"""
        try:
            if self.work_events is None:
                return {"ok": False, "message": "职场引擎未就绪"}
            body = await self._read_json()
            action = str(body.get("action") or "state").lower()
            if action in ("fire", "trigger"):
                raw = str(body.get("kind") or body.get("event") or "").strip()
                alias = {"迟到": "late", "早退": "late", "病假": "sick",
                         "全勤": "full_attendance", "全勤奖": "full_attendance",
                         "项目": "project", "奖金": "project", "加班": "overtime"}
                kind = alias.get(raw, raw.lower())
                return await self.work_events.fire(kind, day=str(body.get("day") or ""), source="api")
            return {"ok": True, "events": self.work_events.today_events(),
                    "prompt": self.work_events.prompt_line()}
        except Exception as exc:
            isolated("web.work", exc)
            return {"ok": False, "message": "职场接口失败：%s" % exc}

    async def _api_leave(self, request=None):
        """替她请假：{kind: 事假|病假, days: 1}，走完整链路。"""
        try:
            if self.finance_salary is None:
                return {"ok": False, "message": "工资模块未就绪"}
            body = await self._read_json()
            kind = str(body.get("kind") or "事假")
            days = float(body.get("days") or 1.0)
            return await self.finance_salary.apply_leave(kind=kind, days=days,
                                                         reason=str(body.get("reason") or ""),
                                                         day=str(body.get("day") or ""))
        except Exception as exc:
            isolated("web.leave", exc)
            return {"ok": False, "message": "请假失败：%s" % exc}

    async def _api_expenses(self, request=None):
        """本月日常开销：分类汇总 + 最近逐笔。"""
        try:
            if self.finance_expense is None:
                return {"ok": False, "message": "开销模块未就绪"}
            now = _dt.date.today()
            summary = self.finance_expense.month_summary(now)
            rows = self.finance_repo.expenses(period=now.strftime("%Y-%m")) if self.finance_repo else []
            return {"ok": True, "month": now.strftime("%Y-%m"),
                    "total_cents": summary["total_cents"],
                    "categories": summary["categories"],
                    "items": [{"day": row.get("day"), "category": row.get("category"),
                               "label": row.get("label"),
                               "amount_cents": int(row.get("amount_cents") or 0)}
                              for row in (rows or [])[:60]]}
        except Exception as exc:
            isolated("web.expenses", exc)
            return {"ok": False, "message": "开销接口失败：%s" % exc}

    async def _api_salary(self, request=None):
        """GET 看基本工资/明细/实发；POST {action:add|raise|settle}。"""
        try:
            if self.finance_salary is None:
                return {"ok": False, "message": "工资模块未就绪"}
            body = await self._read_json()
            action = str(body.get("action") or "state").lower()
            period = _dt.date.today().strftime("%Y-%m")
            if action == "set":
                self.finance_salary.set_base(int(round(float(body.get("base") or 0) * 100)))
            elif action == "add":
                kind = "bonus" if str(body.get("kind")) in ("bonus", "奖金") else "deduction"
                amount = int(round(float(body.get("amount") or 0) * 100))
                if kind == "deduction":
                    amount = -abs(amount)
                label = str(body.get("label") or ("奖金" if kind == "bonus" else "扣款"))
                self.finance_salary.add_item(period, kind, label, amount)
            elif action == "raise":
                pct = float(body.get("pct") or 0.1)
                return await self.finance_salary.raise_or_cut(pct)
            elif action == "settle":
                return await self.finance_salary.settle(_dt.date.today(), period)
            summary = self.finance_salary.summary(period)
            summary["thrifty_until"] = self.finance_salary.thrifty_until()
            summary["ok"] = True
            return summary
        except Exception as exc:
            isolated("web.salary", exc)
            return {"ok": False, "message": "工资接口失败：%s" % exc}

    async def _api_property(self, request=None):
        """GET 看房车状态，POST {action:"buy", kind:"house"|"car"} 下单。"""
        try:
            if self.finance_property is None:
                return {"ok": False, "message": "房产模块未就绪"}
            # Quart 兼容层不会把 request 传进来，直接读请求体；GET 没有 body 时就是看状态
            body = await self._read_json()
            action = str(body.get("action") or "state").lower()
            kind = "house" if str(body.get("kind") or "house") in ("house", "房", "买房") else "car"
            if action == "buy":
                return await self.finance_property.buy(kind)
            state = self.finance_property.state()
            state["afford"] = {"house": self.finance_property.afford("house"),
                               "car": self.finance_property.afford("car")}
            state["ok"] = True
            return state
        except Exception as exc:
            isolated("web.property", exc)
            return {"ok": False, "message": "房产接口失败：%s" % exc}

    async def _api_finance_help(self, request=None):
        try:
            state = self.finance_help.status() if self.finance_help else {}
            state["prompt"] = self.finance_help.prompt_text() if self.finance_help else ""
            return state
        except Exception as exc:
            isolated("web.help", exc)
            return {"low": False, "prompt": ""}

    async def _api_loan(self, request=None):
        try:
            body = await self._read_json()
            amount = int(round(float(body.get("amount") or 0) * 100))
            periods = int(body.get("periods") or 12)
            rate = float(body.get("rate") or 0.06)
            return await self.finance_help.take_loan(amount, periods, rate)
        except Exception as exc:
            isolated("web.loan", exc)
            return {"ok": False, "message": "借款失败：%s" % exc}

    async def _api_consume(self, request=None):
        try:
            body = await self._read_json()
            items = body.get("items") or []
            return {"items": await self.finance_consume.buy_items(items)}
        except Exception as exc:
            isolated("web.consume", exc)
            return {"items": [], "message": "消费失败：%s" % exc}

    async def _api_decision(self, request=None):
        try:
            body = await self._read_json()
            slot = str(body.get("slot") or "close")
            return await self.finance_decision.run_slot(slot)
        except Exception as exc:
            isolated("web.decision", exc)
            return {"ok": False, "message": "决策失败：%s" % exc}

    async def _api_debug(self, request=None):
        try:
            body = await self._read_json()
            action = str(body.get("action") or "").lower()
            if action == "salary" and self.finance is not None:
                return await self.finance.income(680000, "工资(调试)", "debug:salary:%d" % int(time.time()), salary=True)
            if action == "rent" and self.finance is not None:
                return await self.finance.expense(120000, "房租(调试)", "debug:rent:%d" % int(time.time()), necessary=True)
            if action == "spend" and self.finance_expense is not None:
                category = str(body.get("category") or "food")
                label = str(body.get("label") or "一笔开销")
                amount = int(round(float(body.get("amount") or 0) * 100))
                return await self.finance_expense.spend(category, label, amount)
            if action == "quotes" and self.finance_decision is not None:
                result = await self.finance_decision.run_slot("close")
                return {"ok": True, "result": result}
            return {"ok": False, "message": "未知调试动作"}
        except Exception as exc:
            isolated("web.debug", exc)
            return {"ok": False, "message": str(exc)}

    async def _api_diag(self, request=None):
        try:
            return await self.diag()
        except Exception as exc:
            isolated("web.diag", exc)
            return {"status": "error", "message": "diag failed"}

    # ---------------- 命令（入口，必须被隔离） ----------------
