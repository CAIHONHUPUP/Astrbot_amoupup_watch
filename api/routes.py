# -*- coding: utf-8 -*-
"""Web 面板路由表：main 只负责遍历注册。"""

BASE = "/Astrbot_amoupup_watch"

ROUTES = [
    ("/diag", "_api_diag", ["GET"], "守护面板：运行状态"),
    ("/finance", "_api_finance", ["GET"], "守护面板：金融"),
    ("/ledger", "_api_ledger", ["GET"], "守护面板：明细流水"),
    ("/finance_demo", "_api_finance_demo", ["GET"], "守护面板：金融演示数据"),
    ("/finance/help", "_api_finance_help", ["GET"], "守护面板：资金紧张"),
    ("/finance/loan", "_api_loan", ["POST"], "守护面板：借款"),
    ("/finance/consume", "_api_consume", ["POST"], "守护面板：消费"),
    ("/finance/decision", "_api_decision", ["POST"], "守护面板：决策"),
    ("/finance/debug", "_api_debug", ["POST"], "守护面板：调试触发"),
    ("/finance/property", "_api_property", ["GET", "POST"], "守护面板：房产与车辆"),
    ("/finance/salary", "_api_salary", ["GET", "POST"], "守护面板：工资明细"),
    ("/finance/leave", "_api_leave", ["POST"], "守护面板：请假"),
    ("/finance/work", "_api_work", ["GET", "POST"], "守护面板：职场事件"),
    ("/finance/medical", "_api_medical", ["GET", "POST"], "守护面板：医疗"),
    ("/housing", "_api_housing", ["GET"], "守护面板：房屋（数据来自能陪）"),
    ("/finance/insurance", "_api_insurance", ["GET", "POST"], "守护面板：保险"),
    ("/finance/social", "_api_social", ["GET", "POST"], "守护面板：社交"),
    ("/finance/bank", "_api_bank", ["GET", "POST"], "守护面板：银行"),
    ("/finance/fortune", "_api_fortune", ["POST"], "守护面板：突发事件"),
    ("/finance/lending", "_api_lending", ["GET", "POST"], "守护面板：人情债"),
    ("/finance/city", "_api_city", ["GET", "POST"], "守护面板：城市/搬家"),
    ("/finance/expenses", "_api_expenses", ["GET"], "守护面板：日常开销"),
]
