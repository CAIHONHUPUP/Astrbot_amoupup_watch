# 握手契约（amo ↔ watch）

> 版本：api_version **1.0** · 更新 2026-11-13
> 原则：**amo 只做接入握手，社会逻辑一行都不许进 amo。**

## 1. 发现（amo 侧，纯懒加载）

    # amo_social_bridge.py（amo 侧唯一新增文件，约 25 行）
    _INST = None
    _CHECKED = 0.0

    def get_watch(plugin, force=False):
        """没装/停用/加载失败一律返回 None，绝不抛异常。"""
        global _INST, _CHECKED
        now = time.monotonic()
        if not force and now - _CHECKED < 60:
            return _INST
        _CHECKED = now
        _INST = None
        try:
            ctx = getattr(plugin, "context", None)
            meta = ctx.get_registered_star("Astrbot_amoupup_watch") if ctx else None
            if meta is None or getattr(meta, "activated", True) is False:
                return None
            inst = getattr(meta, "star_cls", None)
            if inst is not None and callable(getattr(inst, "describe", None)):
                _INST = inst
        except Exception:
            _INST = None
        return _INST

    async def handshake(plugin):
        """启动后调一次，日志留痕；失败不影响 amo 任何功能。"""
        inst = get_watch(plugin, force=True)
        if inst is None:
            logger.info("[amo] watch 未安装/未启用，社会模拟降级为关闭")
            return None
        try:
            info = await asyncio.wait_for(asyncio.to_thread(inst.describe), 0.2)
        except Exception as exc:
            logger.warning("[amo] watch 握手失败: %r", exc)
            return None
        if str(info.get("api_version", "")).split(".")[0] != "1":
            logger.warning("[amo] watch api 版本不匹配: %s", info.get("api_version"))
            return None
        logger.info("[amo] watch handshake ok version=%s", info.get("version"))
        return info

## 2. 端口（v0.1 只开第一个）

| 端口 | 签名 | 线程 | 超时 | v0.1 |
|---|---|---|---|---|
| `describe()` | 同步，返回 dict | 任意（纯内存） | 0.2s | **开** |
| `snapshot(umo, budget)` | 异步，返回 dict|None | 事件循环 | 0.3s | 关（v0.2） |
| `notify(kind, payload)` | 同步入队，立即返回 | 事件循环 | 不计 | 关（v0.2） |

`describe()` 返回：

    {
      "plugin": "Astrbot_amoupup_watch",
      "version": "0.1.0",
      "api_version": "1.0",
      "features": [],          // 能力开关，amo 按这个决定用不用，不猜
      "status": "ready",       // ready | stopped | degraded
      "started_at": 1763000000.0
    }

## 3. 版本规则

- 主版本（1.x）**只加不改**；删字段必须升主版本。
- amo 只认主版本号；不匹配 = 当作没装，降级不报错。
- `features` 是能力协商的唯一入口：新功能先进 features，amo 看到才调用。

## 4. 时序

    AstrBot 启动
      ├─ watch.initialize()            # 只注册，不做重活
      └─ amo.on_astrbot_loaded()
           └─ handshake()              # 一次，日志留痕
                └─ describe()          # 0.2s 超时
   运行期（v0.2 起）
      amo 收到消息 → snapshot()（0.3s 超时，失败就不注入）→ 正常回复
                   → notify()（不等待）
   卸载
      amo 什么都不用做；watch.terminate() 自清

## 5. 失败语义（amo 侧必须遵守）

| 情况 | amo 行为 |
|---|---|
| 没装 / 停用 / 加载失败 | `get_watch()` 返回 None，零日志噪音、零重试 |
| 握手超时 / 抛异常 | 记一条 warning，当次不接入，60 秒后再试 |
| api_version 不匹配 | 当没装，等对方升级 |
| snapshot 超时 | 用 ≤10 分钟缓存；没有缓存就不注入社会块 |
| notify 队列满 | 直接丢，不重试、不阻塞 |

**验收标准**：把 `Astrbot_amoupup_watch` 目录整个删掉，amo 启动日志里最多一条 info，聊天完全正常。

## 6. amo 侧改动清单（就这些）

1. 新增 `amo_social_bridge.py`（约 25 行，上面第 1 节）。
2. `main.py` 的 `initialize()` 末尾加一行 `self._spawn(handshake(self))`（已有的 `_spawn`）。
3. 不新增配置项（要关就停用插件）。

不改 amo 的既有默认值、不动它的库、不 import watch 的任何模块。
