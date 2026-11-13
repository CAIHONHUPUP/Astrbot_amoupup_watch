# 我能永远守护你 · Astrbot_amoupup_watch

> **我能永远守护你**（独立社会模拟插件，AstrBot v4）。她有自己的社交世界：谁找她、她跟谁熟、哪些事会传开。
> **与 amo（Astrbot_amoupup_forever）只做握手接入，不进入 amo 代码库。**

- 当前版本：**0.1.0（边界层已落地）**：guard / runtime / store / scheduler + 15 项单测；业务逻辑（world.py）还没写
- 跑测试：`pytest tests -q`（插件根目录放 PYTHONPATH）
- 设计文档：[docs/ISOLATION-DESIGN.md](docs/ISOLATION-DESIGN.md)
- 握手契约：[docs/HANDSHAKE.md](docs/HANDSHAKE.md)
- 作者：彩虹upup

## 为什么单独开仓库

amo 已经是 6.9.x、1300+ 测试、每天在线跑的主插件。社会模拟是一个**会自己长数据**的子系统，
把它塞进 amo 等于把「最会积累状态的部分」和「最不能出事的聊天主链」绑在一起。所以：

| | amo | 本插件 |
|---|---|---|
| 职责 | 对话、日程、记忆、生图 | 社会世界模拟、事件、关系 |
| 数据库 | amo_forever.db | **自己的** plugin_data/Astrbot_amoupup_watch/social.db |
| 定时器 | 自己的 | **自己的** |
| 崩了会怎样 | 她不能聊天 | amo 只多一个 None 判断，照常聊天 |

amo 侧只加一个约 25 行的桥接：发现插件 → 调 `describe()` → 记一条日志。没有别的。

## 五条硬边界（详见设计文档）

1. **异常隔离**：5 类入口全部在 `guard.py` 收口，日志统一 `[WATCH] isolated`，绝不冒泡进 amo 派发层。
2. **不阻塞事件循环**：自有 `ThreadPoolExecutor`（4 IO + 1 DB），**不用** `asyncio.to_thread`（那个池子 amo 也在用）。
3. **存储独立**：`plugin_data/Astrbot_amoupup_watch/`，路径不可配置 + 路径守卫，代码里不出现 amo 任何文件名。
4. **定时器自管**：注册表 + 代际 tag + 幂等 terminate（3 秒硬截止）。
5. **资源有上限**：事件/关系/日志/轮询全部有上限，且每条上限都写明「触顶怎么办」。

## 本地目录

    D:\dafeiyu\work\watch\          ← 开发副本（就是仓库根）
    D:\dafeiyu\astrbot\data\plugins\Astrbot_amoupup_watch\   ← 部署位置（后续同步）

## 安装（等 v0.2 有功能之后）

1. 把整个目录复制到 `astrbot/data/plugins/Astrbot_amoupup_watch/`
2. 重启 core（或重载插件）
3. 日志出现 `[WATCH] loaded 0.1.0 api=1.0` 即加载成功
4. amo 侧会自己握手，日志出现 `[amo] watch handshake ok`

## 开发顺序（不许跳）

1. `guard.py`（异常收口）→ 2. `runtime.py`（线程池 + 看门狗）→ 3. `store.py`（库 + 路径守卫）
→ 4. `scheduler.py`（定时器）→ 5. 跑通验收 A/C/D → 6. 才写 `world.py` 的社会逻辑

先写功能再补边界，等于把五条硬边界变成事后追认。

## 明确不做

- 不注册 LLM tool（工具调用是第三个入口，边界稳了再说）
- 不订阅 amo 的同步 EventBus（会拖慢 amo）
- 不读 / 不写 amo 的任何文件
- 不上架插件市场
