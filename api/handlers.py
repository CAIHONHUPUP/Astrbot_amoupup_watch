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

    async def _api_ledger(self, request=None):
        """GET 明细流水：每一笔带 大类/小类/物品/原因/时间。"""
        try:
            finance = getattr(self, "finance", None)
            amo = finance._amo() if finance is not None and hasattr(finance, "_amo") else None
            if amo is None:
                return {"ok": False, "message": "钱包未就绪"}
            return {"ok": True, "items": amo.wallet.ledger(80)}
        except Exception as exc:
            isolated("web.ledger", exc)
            return {"ok": False, "message": "流水读取失败"}

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

    async def _api_bank(self, request=None):
        """GET 活期/定期明细；POST {action:deposit|withdraw, amount_cents, term}。"""
        try:
            body = await self._read_json()
            bank = getattr(self, "bank", None)
            if bank is None:
                try:
                    from ..services.bank_engine import build_bank
                except ImportError:
                    from services.bank_engine import build_bank
                bank = build_bank(self)
                self.bank = bank
            if not body:
                return {"ok": True, "current_cents": bank.current_cents(),
                        "fixed": bank.active_fixed_rows(), "fixed_cents": bank.fixed_cents(),
                        "last_matured": bank.last_matured(),
                        "total_cents": bank.total_cents()}
            action = str(body.get("action") or "deposit")
            amount = int(body.get("amount_cents") or 0)
            term = int(body.get("term") or 0)
            if action == "settle_matured":
                return {"ok": True, "matured": await bank.process_matured_fixed_deposits()}
            if action == "deposit":
                return await bank.deposit(amount, term)
            return await bank.withdraw(amount, None if not term else 0)
        except Exception as exc:
            isolated("web.bank", exc)
            return {"ok": False, "message": "银行接口异常"}

    async def _api_social(self, request=None):
        """GET 核心关系/应收；POST {action:"event", name:"随礼"} 触发一次社交事件。"""
        try:
            social = getattr(self, "social", None)
            if social is None:
                try:
                    from ..services.social_engine import build_social
                except ImportError:
                    from services.social_engine import build_social
                social = build_social(self)
                self.social = social
            social.ensure_seed()
            if not await self._read_json():
                rows = social.repo.npcs()
                try:
                    try:
                        from ..services.gift_engine import birthday_info
                    except ImportError:
                        from services.gift_engine import birthday_info
                    for row in rows:
                        info = birthday_info(self, str((row or {}).get("name") or ""))
                        row["birthday"] = info.get("birthday")
                        row["birthday_text"] = info.get("text")
                except Exception:
                    pass
                recv = social.repo.receivables()
                return {"ok": True, "npcs": rows, "receivables": recv,
                        "line": social.relation_line()}
            body = await self._read_json()
            name = str(body.get("name") or "随礼")
            table = {"随礼": social.gift_money, "红包": social.red_packet,
                     "聚餐": social.dinner, "借钱": social.borrow_request, "批评": social.criticism}
            fn = table.get(name)
            if fn is None:
                return {"ok": False, "message": "未知事件"}
            return {"ok": True, "result": await fn()}
        except Exception as exc:
            isolated("web.social", exc)
            return {"ok": False, "message": "社交接口异常"}

    async def _api_insurance(self, request=None):
        """GET 保单列表；POST {kind:"critical|medical"} 购买（年费扣款，幂等）。"""
        try:
            body = await self._read_json()
            medical = getattr(self, "medical", None)
            if medical is None:
                return {"ok": False, "message": "医疗模块未就绪"}
            try:
                from ..services.insurance_engine import PRODUCTS, build_insurance
            except ImportError:
                from services.insurance_engine import PRODUCTS, build_insurance
            ins = getattr(self, "insurance", None) or build_insurance(self)
            self.insurance = ins
            if body.get("kind"):
                return await ins.buy(body["kind"])
            st = ins.status()
            repo = getattr(self, "finance_repo", None)
            def owned(kind):
                try:
                    return bool(repo.assets_of(kind)) if repo else False
                except Exception:
                    return False
            has_car, has_house = owned("car"), owned("house")
            def line(key):
                spec = PRODUCTS[key]; row = st.get(key) or {}
                if row.get("expired") or row.get("lapsed"):
                    status = "已失效（断缴）"
                elif row.get("grace"):
                    status = "宽限期（至 %s）" % row.get("grace_until")
                elif row.get("active"):
                    status = "生效中" + (("（下次缴费：%s 年）" % (int(row["year"]) + 1)) if row.get("year") else "")
                else:
                    status = "未购买"
                if spec.get("kind") == "car" and not has_car: status = "无车不适用"
                if spec.get("kind") == "house" and not has_house: status = "无房不适用"
                return {"name": spec["name"], "status": status}
            groups = [{"title": "人身保险", "items": [line(k) for k in ("e生保", "金钟罩", "重疾")]},
                      {"title": "财产保险", "items": [line(k) for k in ("交强险", "商车险", "家财险")]}]
            return {"ok": True, "groups": groups, "hint": "发送 /督促她买保险 <险种> 可以为她提供保障",
                    "policies": st, "has_policy": bool(st),
                    "catalog": [{"kind": k, "name": v["name"],
                                 "premium_cents": v["premium"][0]} for k, v in PRODUCTS.items()]}
            return {"ok": True, "policies": medical.policies(),
                    "catalog": [{"kind": "critical", "name": "重疾险", "premium_cents": medical.PREMIUM["critical"]},
                                {"kind": "medical", "name": "医疗险", "premium_cents": medical.PREMIUM["medical"]}],
                    "has_policy": medical.has_policy("critical")}
        except Exception as exc:
            isolated("web.insurance", exc)
            return {"ok": False, "message": "保险接口异常"}

    async def _api_fortune(self, request=None):
        try:
            fortune = getattr(self, "fortune", None)
            if fortune is None:
                return {"ok": False, "message": "突发引擎未就绪"}
            return await fortune.monthly_roll()
        except Exception as exc:
            isolated("web.fortune", exc)
            return {"ok": False, "message": "突发事件接口异常"}

    async def _api_lending(self, request=None):
        try:
            body = await self._read_json()
            lending = getattr(self, "lending", None)
            if lending is None:
                return {"ok": False, "message": "人情债模块未就绪"}
            if body.get("friend"):
                return await lending.lend(body["friend"], int(body.get("amount_cents") or 0),
                                          int(body.get("months") or 6))
            return await lending.repay_tick()
        except Exception as exc:
            isolated("web.lending", exc)
            return {"ok": False, "message": "人情债接口异常"}

    async def _api_medical(self, request=None):
        """GET 看病历/生病状态；POST {level,name,days,event_id} 模拟一场病（走同一入口）。"""
        try:
            body = await self._read_json()
            medical = getattr(self, "medical", None)
            if medical is None:
                return {"ok": False, "message": "医疗模块未就绪"}
            if not body:
                hunger = {}
                try:
                    finance = getattr(self, "finance", None)
                    amo = finance._amo() if finance is not None and hasattr(finance, "_amo") else None
                    if amo is not None and hasattr(amo, "hunger_state"):
                        hunger = amo.hunger_state()
                except Exception:
                    hunger = {}
                return {"ok": True, "is_sick": medical.is_sick(), "last_checkup": self.finance_repo.meta_get("checkup:last", "") if self.finance_repo else "", "last_hp": self.finance_repo.meta_get("checkup:last_hp", "") if self.finance_repo else "",
                        "in_bed_rest": medical.in_bed_rest(),
                        "hunger": hunger,
                        "sick_until": self.finance_repo.meta_get("is_sick_until", "") if self.finance_repo else ""}
            if str(body.get("action") or "") == "checkup":
                return await medical.checkup()
            result = await medical.handle_sick_event(
                body.get("level", "light"), body.get("name", "感冒"),
                body.get("days", 1), body.get("event_id", ""))
            return {"ok": bool(result.get("ok")), **result}
        except Exception as exc:
            isolated("web.medical", exc)
            return {"ok": False, "message": "系统扣款异常，但身体重要，请先休息。"}

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
