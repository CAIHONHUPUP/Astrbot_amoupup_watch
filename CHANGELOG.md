# CHANGELOG

## 1.0.1（2026-11-15 修复：/watch 诊断、定时任务连锁跳过、滞纳金账实不符）

- **P1 /watch 永远失败**：`ui/cards.py` 用了未定义的 `DISPLAY_NAME`，
  而且 `data.get("display_name", DISPLAY_NAME)` 的默认值会**先求值**，所以不管传什么进去都抛 NameError；
  `watch_cmd` 兜底后统一回「命令出错已被隔离」，`/watch` 与 `/watch diag` 永远看不到状态。
  改成字面量兜底（不 import main，避免循环导入）。
- **P1 一个定时任务失败，后面全部静默跳过**：`_finance_tick` 里 `finance_life.tick()` /
  `date_tick` / `shopping` / `gift` / 决策 / 日结 / 银行 / 职场 / 社交都是裸 `await`，
  行情接口超时（8s×2 重试）就会让账单、工资、职场、社交、破产检查当天全不执行；
  新增 `_guard_step(name, awaitable)`，每一步单独隔离并记 `finance.<step>`。
  顺手修：`social.scheduled()` 每轮跑两遍（249/291 两处）、`fortune/lending/medical` 被错误缩进在
  `if work_events` 里（职场模块一出错就不体检）。
- **P1 滞纳金「没扣到钱也写已扣」**：`expense` 余额不足时返回 `ok=False` 而不是抛异常，
  `bill_flow.overdue_tick` 先写 `row["penalty"]` 再扣款且不检查返回值 → 下一轮直接 `continue`，
  账上没少钱、记忆和日记却说被扣了，永远补不回来。现在只有扣款成功才落账；最终违约金同理。
- **P2**：`repo.finish_pending` 改走 `runtime.run_db`（事件循环里不再同步写 sqlite）；
  清死代码（`agent_loop` 未用变量、`api/handlers` 未用 import）；新增 `.coveragerc`（底线 55%）。
- 测试 **309 项全过**（新增 /watch、_guard_step、滞纳金成功/失败 4 项回归），覆盖率 **58%**。

## 1.0.0 正式版（2026-11-14）

首个 1.0 正式版：约会/赴约、逛街购物、夜宵、迟到罚款、礼物与生日、明细账单与 /账单、
LLM 工具包装层抽离、面板支出明细。
