# 金融管家 v1 实施计划（先设计，后编码）

> 2026-11-13 · 依据：amo 6.9.x / 守护 0.1.0 / 能记 3.0.x
> 初始状态：存款 **23,000 元**、无房、无车、空仓；时间用真实系统时间，另加调试开关。

## 一、接口映射表

| 能力 | 接口 | 参数 | 返回 | 状态 |
|---|---|---|---|---|
| 读可用余额 | `amo_wallet.py::Wallet.balance(account="cash")` | account | 分（int） | ✅ |
| 入账 | `Wallet.credit(amount_cents, reason, idem, ref="")` | 分、原因、幂等键 | bool | ✅ |
| 扣款 | `Wallet.debit(amount_cents, reason, idem, ref="")` | 同上 | bool（余额不足 False） | ✅ |
| 读情绪分 | `amo_human_state.py::HumanState.mood_score()` | — | 0–100 | ✅ |
| 读情绪词 | `HumanState.mood_words()` | — | 自然语言 | ✅ |
| 写情绪（幂等） | `HumanState.nudge_once(idem, **deltas)` | 十维键 | bool | ✅ |
| 日程追加 | `HumanState.add_schedule_item(day, item, source, reason)` | day、item | `{changed}` | ✅ |
| 小日记 | `AmoDB.save_diary(day, hour, text)` | day、`"09"`、正文 | None | ✅ |
| 长日记 | `DiaryApiMixin.write_diary_text(day, text)` | day、正文 | `{code,data}` | ✅ |
| 记忆写 | `HumanCorePlugin.finance_remember(content, kind, tier, idem_key)` | 正文、分类、等级、幂等 | bool | ✅ |
| 记忆读 | `HumanCorePlugin.finance_recall(query, limit)` | 查询、条数 | `list[dict]` | ✅ |
| 持仓/成交/贷款 | 守护自己的库：`positions / trades / loans / loan_installments / assets / settlements / candidates / meta` | — | — | ✅（表已建） |
| 决策 | `finance_decision.py::FinanceDecision.run_slot(slot)` | morning/noon/close | `{decision, executed}` | ✅ |
| 交易 | `finance_trading.py::Trading.buy/sell/subscribe/redeem` | code、qty、price、idem | `{ok, message}` | ✅ |
| 借款 | `finance_help.py::FinanceHelp.take_loan(amount, periods, rate)` | 分、3/6/12/24、年化 | `{ok, message}` | ✅ |
| 单品消费 | `finance_consume.py::FinanceConsume.buy_item(item, qty)` | 商品名、数量 | `{ok, amount_cents}` | ✅ |
| 三餐 | `finance_daily.py::FinanceDaily.tick(now)` | 时间 | 已吃列表 | ✅ |
| 结算 | `finance_life.py::FinanceLife.tick(now)` | 时间 | 各项状态 | ✅ |
| 插件发现 | `context.get_registered_star("Astrbot_amoupup_forever")` | — | star_cls | ✅ |
| 面板数据 | `GET /api/plug/Astrbot_amoupup_watch/finance` | — | 快照 JSON | ✅ |
| 面板操作 | `/finance/help|loan|consume|decision`、`/trade` | JSON body | JSON | ✅ |

缺失/待补：可用余额与可取余额分账（T+1 可取、基金 T+7 到账）、候选池接入决策、股息红利税说明。

## 二、指令与按钮清单

### 聊天指令

| 指令 | 行为 |
|---|---|
| `/理财` | 二级菜单：1 总资产 2 持仓 3 贷款 4 日程 0 关闭 |
| `/理财 总资产` | 总资产 = 余额 + 持仓市值 − 未还本金；附情绪词 |
| `/理财 持仓` | 每只：名称/代码/数量/成本/现价/浮动盈亏% |
| `/理财 贷款` | 每笔：类型/本金/年利率/期数/每期/剩余本金/下次还款日 |
| `/理财 日程` | 当日财务日程（工资/还款/大额交易） |
| `/理财 关闭` | 输出一段持仓简报后退出菜单 |
| `/watch`、`/watch finance` | 运行状态 / 金融摘要 |
| `[转账]XXX元` | 入账，写记忆+日程+小日记 |
| `建议买XXXXXX`（待接） | 只写入推荐池（7 天过期，上限 10），不买入 |

### 面板按钮

| 区域 | 控件 |
|---|---|
| 顶部 | 真实/演示切换；「交易」按钮 |
| 资产四联 | 总资产、可用、持仓市值、今日盈亏 |
| 持仓卡（折叠） | 股票/基金切换；每行「卖」；「持仓简报」→ 再点「收起」 |
| 交易面板 | 买入/卖出/申购/赎回/贷款 五个动作；底部「关闭」→ 收起并输出持仓简报 |
| 银行（折叠） | 欠款、月供、年利率、可贷额度；「贷款」 |
| 买房买车（折叠） | 全款、30% 首付、10/20/30 年月供、车贷 |
| 生活账单（折叠） | 工资、房租、水电燃气宽带、本月支出 |
| 心情（折叠） | 情绪条 + 最近财务事件（只出词，不出数） |
| 行情（折叠） | 今日 3 次查询进度、数据源 |
| 最近决策（折叠） | 时间 + 理由 + 是否执行 |
| 最近流水（折叠） | 每笔钱 |
| 低资金横幅 | 余额 < 总资产 5% 时显示二选一提示 |

## 三、模拟时钟测试清单

| # | 场景 | 操作 | 预期 |
|---|---|---|---|
| 1 | 初始状态 | 首次启动 | 存款 23,000；无房无车；空仓；重启后一致 |
| 2 | 工资 | 时钟 2026-11-15 09:00 tick | +6,800 一次；再 tick 不重复；记忆/小日记/日程各一条 |
| 3 | 账单 | 11-01 00:10 tick | 房租 + 水/电/燃气/宽带逐项；第二次不重复 |
| 4 | 欠费 | 余额不足时 11-01 | 不标记已结算；写欠费记忆；触发 5% 二选一提示 |
| 5 | 买入 | 09:35 买 600519 ×100 @1680 | 扣款=金额+佣金(≥5)+过户费；持仓可用 0；成本加权 |
| 6 | T+1 | 同日卖出 | 拒绝「T+1」；次日 refresh 后可卖 |
| 7 | 涨跌停 | 一字涨停报价买入 | 拒绝「一字涨停，买不进」；跌停卖出同理 |
| 8 | 停牌 | 报价 price=0 | 买卖均「停牌，无法成交」 |
| 9 | 行情失败 | fetch 返回空 | 不成交、不改现价、不写盈亏记忆 |
| 10 | 加权成本 | 100@10 + 100@12，卖 100@13 | 成本≈11；实现盈亏=(13−11)×100−费用 |
| 11 | 分红送转 | 每股 0.5 元 + 10 送 10 | 现金入账；股数×2；成本÷2 |
| 12 | 5% 二选一 | 余额 <5% | 只提示一次；选贷款则转账关闭；选转账则贷款关闭 |
| 13 | 分期还款 | 借款 6,000 / 6 期 | 每期本金+利息；到期扣款；不足转逾期 |
| 14 | 逐笔消费 | 早餐 豆浆+肠粉 | 两条记录（1.5 + 6），无「餐饮 50」 |
| 15 | 重启不丢 | 买入+借款后重启 | 持仓、成交、贷款、期数全部还在；定时任务不重复 |
| 16 | 情绪 | 连续亏损 | 0–100 截断；对外只出词（哭唧唧→崩溃） |
| 17 | 调薪 | 1 月/7 月 | 按公式 ±5%–15%，半年只调一次 |

## 四、需要你确认的参数

| 参数 | 我的默认 | 需你确认 |
|---|---|---|
| 初始存款 | 23,000 元 | ✅ 已给 |
| 有无房车 | 无房无车 | ✅ 已给 |
| 初始持仓 | 空仓 | ✅ 已给 |
| 固定锚点池 | 沪深 300 抽样 24 只（写进配置） | 是否用我给的名单 |
| 用户推荐池 | 7 天过期、上限 10 只 | 过期天数/上限 |
| 情绪上下限 | 0–100，向 50 回落 | 回落速度（默认 45 分钟半衰） |
| 调薪公式 | 见下 | 是否认可 |
| 闲置现金计息 | 不计息 | 是否要活期 0.2% |
| 基金赎回款 | 暂记 T+7 到账 | 是否本期就做 |

调薪公式（写死）：

    A = (期末总资产 − 期初总资产) / 期初总资产
    B = 半年收入 / 半年支出
    score = 0.6 × clamp(A, −0.3, 0.3)/0.3 + 0.4 × clamp((B − 1), −1, 1)
    pct  = 5% + 10% × (score + 1)/2          # 落在 5%–15%
    新工资 = clamp(旧工资 × (1 + pct), 3000, 20000)
