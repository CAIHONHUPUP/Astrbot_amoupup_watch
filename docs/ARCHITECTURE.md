# 架构（2026-11-13 分层重构）

## 一句话

`main.py` 只做装配与分发；业务在 `services/`，决策与联动在 `core/`，数据在 `models/`，纯函数在 `utils/`，展现在 `ui/`，接口在 `api/`。

## 依赖方向（单向，禁止回指）

    main ──▶ api / ui ──▶ services / core ──▶ models ──▶ utils
                              └──▶ guard（所有层共用，只记日志）

- 上层可以调下层；**下层绝不 import 上层**。
- `guard.py` 是唯一允许被任意层 import 的横切模块（异常收口，`[WATCH] isolated`）。

## 目录与职责

| 目录 | 放什么 | 不许放什么 |
|---|---|---|
| `main.py` | 插件类、生命周期、指令/LLM 工具/hook 的薄包装、路由登记 | 任何计算、SQL、金额换算、prompt 拼接 |
| `api/` | Web 接口处理（ApiMixin） | 业务规则 |
| `ui/` | 面板字段组装、文本卡片；`views/守护/index.html` 前端 | 写库 |
| `services/` | 交易、行情、候选池、生活结算、三餐、消费、开销、工资、职场事件、房车、信贷、城市 | HTTP、amo 协议细节 |
| `core/` | 三次自主决策、LLM 决策、amo 联动桥、四层记忆、定时器、线程池 | 具体业务公式 |
| `models/` | 连接/迁移（db）、资产/贷款/流水（ledger）、记忆索引（memory） | 业务判断 |
| `utils/` | 城市系数、房价房租、金额取整、节假日、交易时段、稳定随机 | 状态 |

## 关键约定

1. **插件主类必须定义在 main.py**：AstrBot 用 `handler_module_path` 认领 handler，主类挪进子模块会让指令全部静默失效（amo 踩过）。所以方法进 mixin，类留在 main。
2. **余额唯一权威是 amo 的 wallet**：本项目不存余额副本，`models/ledger.py` 只记自己的资产/贷款/流水。
3. **金额一律「分」的整数**，取整走 `utils.helpers.cents`（十进制四舍五入，避免浮点）。
4. **按月幂等**：发薪/房租/房贷/物业费都写 `settlements(day, kind)`，重启重跑不会重复扣。
5. **随机必须稳定**：同一天/同一月用 `stable_int(seed, low, high)`，刷新不会变。
6. **失败不改状态**：扣款失败不标记已结算、不没收资产，下次 tick 重试。

## 加一个新功能的固定套路

1. 纯公式/常量 → `utils/helpers.py`（并补单测）；
2. 新表或新查询 → `models/ledger.py`（`CREATE TABLE IF NOT EXISTS` + 幂等迁移）；
3. 业务动作 → `services/xxx_engine.py` 一个类 + 一个 `async def`，只依赖 repo/finance；
4. 需要 amo 的能力（钱/心情/日程/日记/记忆）→ 走 `core/amo_bridge.py` 的公开方法，**不许直接碰 amo 的表**；
5. 对外接口 → `api/handlers.py` 加 `_api_xxx`，main 的路由表加一行；
6. 面板字段 → `ui/cards.py` 组装，前端 `views/守护/index.html` 条件渲染；
7. 测试 → `tests/test_xxx.py`，至少覆盖：正常值、边界、幂等、失败不改状态。

## 测试

    PYTHONPATH=. pytest tests -q      # 138 项

- `test_main_contract.py`：main.py ≤ 520 行、禁业务实现、分层目录必须在、根目录不许有 shim；
- `test_sim_clock.py`：发薪、买房换租、买车养车、余额求助、重启不丢；
- 其余按引擎分文件（trade/life/work/property/expense/city/leave/work_events…）。

## 分层迁移记录

1. models（db/ledger/memory）
2. utils（城市/金额/节假日）
3. services（12 个引擎）
4. core（决策/联动/记忆/调度）
5. ui + api（从 main 拆出 237+232 行）
6. main 瘦身 1000 → 486 行；根目录 20 个 shim 全部删除

备份在 `D:\dafeiyu\backup\main-split-*`、`shim-clean-*`。
