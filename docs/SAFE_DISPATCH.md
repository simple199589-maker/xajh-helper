# SafeDispatch 全平台安全调度

> 目标：减少高频远程调用互锤导致的游戏客户端 CRT hang / `err=5` / 内存击穿。  
> 约束：**业务决定调用频次**；SafeDispatch 只做编排（排队 / 合并 / 缓存 / 门禁 / 周期托管）。

## 位置

- 调度核心：`app/core/safe_dispatch.py`
- 远程底层：`app/core/remote_runtime.py`（CRT / OpenProcess / VirtualAllocEx）
- 挂机长期守护：`app/core/hang_settings.py`（`start_hang_guard` / `stop_hang_guard`）

## 设计原则

1. **同 `game_pid` 串行，跨 pid 并行**  
   助手进程内按 pid 维护队列与 worker；多号互不阻塞。
2. **业务拥有 cadence**  
   tick / delay / interval 写在业务配置（如 `HANG_GUARD_TICK_S`、杂货 delay），不在调度器里“发明节流”。
3. **不降速 P0/P1 成功路径**  
   热路径可用 `bypass_queue` / `run_p0`：只做 gate + 执行，不加额外 sleep。
4. **进程本地缓存**  
   bag/shop/roll 等 TTL 缓存不跨助手实例共享；同 pid 的 CRT 仍靠命名互斥串行。
5. **hard_dead 一票否决**  
   `VirtualAllocEx/OpenProcess/CreateRemoteThread err=5` 等 → `mark_pid_hard_dead`；后续 CRT 拒绝，长循环应 `session_blocked` 后停手。

## 公共 API

```python
from app.core.safe_dispatch import (
    Priority, OpKind, get_dispatch, session_blocked, RemoteBlockedError,
)

d = get_dispatch()

# 提交（读/写/本地）
d.submit(pid, fn, priority=Priority.P1, kind=OpKind.CRT_WRITE, op="use_item")

# P0 热路径：gate + 内联，不入队加延迟
d.run_p0(pid, fn, op="open_first", kind=OpKind.CRT_WRITE)

# 只读缓存 + SingleFlight
items = d.read_cached(pid, "bag:2,3,4", producer, max_age=0.12, ttl_s=0.12,
                      priority=Priority.P2, kind=OpKind.RPM, op="list_packages")

# 写后失效
d.invalidate(pid, "bag", "bag_slot")

# 周期任务（interval 由业务传入）
d.schedule_periodic(pid, "hang_guard", 1.0, tick_fn, priority=Priority.P2, kind=OpKind.LOCAL)
d.cancel_periodic(pid, "hang_guard")

# 循环头探测
blocked, reason = session_blocked(pid)  # or session
```

### 优先级（默认语义）

| 级 | 典型 | 说明 |
|---|---|---|
| **P0** | 开第一格、进行中关键写、有 pending 的 roll 发包 | 不人为加间隔；仅受同 pid 串行 |
| **P1** | 杂货使用/出售、活跃任务写、写后 bag 校验 | 业务自带 delay；队列 FIFO |
| **P2** | bag/shop/scene 可缓存读、roll 空表轮询 | SingleFlight + TTL |
| **P3** | 修装/活力、低频 UI 刷新 | 到期才做 |
| **P4** | 导出 VA / 冷探测 | attach 生命周期级缓存 |

### OpKind

- `RPM` / `LOCAL`：默认可旁路队列（仍受 hard_dead 策略约束）
- `CRT_READ` / `CRT_WRITE` / `BRIDGE`：入优先级队列（除非 `bypass_queue=True`）

## 缓存策略

| 数据 | 建议 | 写后 |
|---|---|---|
| bag / 多包列表 | TTL ~80–150ms（`TTL_BAG_S`） | `invalidate_bag_cache` / `invalidate(pid, CACHE_BAG…)` |
| 单槽 verify | 写后实时 | max_age=0 |
| shop 槽位记忆 | per-pid 业务缓存 | 买失败 / 关店清 |
| roll 表 | 挂机 0.5s 缓存 | 放弃波次前 invalidate |
| export VA | 长 TTL | attach 生命周期 |

**必须实时**：一切 write 返回值、写后 verify、hard_dead 探测、roll 发包。

## 挂机长期守护

- 开挂成功：`start_hang_guard(session, cfg)` → `schedule_periodic(hang_guard_{pid})`
- 周期 **业务参数**（`hang_settings`）：
  - tick `HANG_GUARD_TICK_S = 1.0`
  - roll 空表 poll 缓存 `HANG_ROLL_POLL_CACHE_S = 0.5`（纯 RPM）
  - pending>0 且拾取关闭 → 走 `hang_maintain_once` 放弃波次（CRT）；发包成功后写 `entry+0x1C=1`，打通客户端 Roll UI/内存刷新，不等待服务器确认
  - 修装/活力检查间隔 1800s；每次开挂后由守护首轮补一次启动检测
- **空 pending 永不 CRT**
- 关挂 / 会话关闭：`stop_hang_guard` + `drop_pid`

## hard_dead 与恢复

1. `remote_runtime.note_remote_os_error` / `SafeDispatch.note_exception` 标记 hard_dead  
2. `ensure_pid_remote_callable` / `ensure_callable` 拒绝新 CRT  
3. 业务循环 `session_blocked` → hard_stop / break  
4. 重新注入或确认进程可用后：`clear_pid_remote_state(pid)`（并视情况 `drop_pid` 后再用）

会话窗口关闭时：`stop_hang_guard` + `get_dispatch().drop_pid(pid)`。

## 业务接入清单（当前）

| 模块 | 接入点 |
|---|---|
| `package_api` | bag `read_cached`；use/sell `invalidate`；shop 槽 per-pid |
| `grocery_auto` | 开第一格/使用/出售/开箱 hard_stop |
| `hang_settings` | 长期 guard + roll RPM 缓存 |
| `loot/_impl` SuperLootRunner | 循环头 gate；扫描 CRT ensure；hard fail note |
| `plg_objects` / `plg_interact` | CRT 前 ensure / 失败 note |
| `yaolu_auto` / `activity_auto` / `task_api` | 循环头 session_blocked |
| `task_schedule` | 队列项前 session_blocked |
| `badao_exchange` | 循环 hard_stop + note |
| `map_fly` / `team_ops` / `qshop_buy` | 入口 session_blocked 早退（飞行/离队/邀请/跟随/队伍标志/分配/商城购买） |
| `package_api.ensure_full_gold` | 卖满金守护内外循环 gate，hard_dead 停手 |
| `npc_service_mem` | AA3330/AA5590 CRT 前 gate；失败 note；传送选单 poll gate |
| `task_schedule` | 队列项前 session_blocked |
| `session_window` | shutdown drop_pid |

## 新业务接入约定

1. **禁止**新代码绕过 `remote_runtime` 裸 `CreateRemoteThread`（过渡期旧路径需逐步收口）。  
2. 长循环：**循环头** `session_blocked`；异常后 `get_dispatch().note_exception`。  
3. 可缓存读：优先 `read_cached`；写成功/失败精确 `invalidate`。  
4. 高频写：业务 delay 保留；调度侧不要再叠 sleep。  
5. 固定常量：优先 `IntEnum` / 模块级常量（Priority、PackageIndex、TID…）。

## 测试

- `tests/test_safe_dispatch.py`：优先级、SingleFlight、hard_dead、periodic、session_blocked  
- `tests/test_hang_settings.py`：hang guard 调度  
- 相关回归：package/grocery、loot_policy、badao、remote_runtime、object

## 明确非目标

- 不在 SafeDispatch 内按“功能名”写死业务节奏（霸刀/杂货/roll 的秒级策略归业务）。  
- 不跨助手进程共享业务缓存。  
- 不把系统聊天“扫信息”抬成高频 CRT。


## 状态预检 / 必新直取（state_dispatch）

业务约定：

- **可预检**：地图/坐标/背包/仓库/金钱/host_id/roll 等，进入重业务前 `prefetch_states(session)` 预热，多模块复用。
- **必须时效**：写后校验、交易确认、起飞前坐标/场景确认等，用 `require_fresh(session, kind)` 或 `get_state(..., fresh=True)`（`max_age=0`）主动拉取并回写缓存。
- **写后**：`invalidate_states(session, StateKind.BAG, StateKind.MONEY, ...)` 与业务 invalidate 对齐。

模块：`app/core/state_dispatch.py`

### 业务接入约定（非马上 vs 必新）
- SCENE/POS 生产者：**禁止** `recon_map_and_pos`（`scan_map_strings` 全进程扫内存会卡死）；hub miss 时走 `read_scene_position` 并回写 hub。
- **非马上需要**：打开飞行旗 / 扫旗 / 进页预检 → `prefetch_states` 或 `get_state(fresh=False)`。
- **必须时效**：起飞后位移校验 / 写后 bag 确认 / hard_dead 探测 → `fresh=True` 或 `require_fresh`，并 `invalidate_states`。
- `map_fly`：`open_transmit_flag` / `fly_official_slot` 起飞前 soft prefetch；校验 `_snapshot_scene_pos(fresh=True)`；位移成功 `_note_fly_state_change`。
- 杂货写后：`invalidate_bag_cache` 同步清 `StateKind.BAG/MONEY`。
- 组队纯 id 校验：优先 warm `HOST_ID`，禁止无谓 `GetObjectName`。
- 任务/妖楼 runner attach：`warmup_session`；跨图/开门后 `read_scene_state(fresh=True)` / portal `wait_scene` 必新。
- 拾取/活动：`open_attach_session` 默认 SCENE/POS 软预热；活动 `_loop` 再补 BAG/MONEY；`force_read` 走 fresh。




```python
from app.core.state_dispatch import StateKind, prefetch_states, warmup_session, get_state, require_fresh, invalidate_states

prefetch_states(session)  # 进页/开挂/飞前预热
bag = get_state(session, StateKind.BAG)                 # 允许短缓存
bag2 = require_fresh(session, StateKind.BAG)            # 交易后必新
invalidate_states(session, StateKind.BAG, StateKind.MONEY)
```

### 挂机守护
- 空 pending 无 CRT；死亡 CRT 仅 pending>0 且 3s 节流；Need/Pass 发包后补本地 decided；当前 burst≤24 / CD0.35s


## 宿主实时状态机（标题栏生产者）

主生产者：功能窗标题栏 `header_live`（`sample_host_live` → `LiveSceneHub`）。

### 一轮可拉（进状态机）
| 层 | 字段 | 节奏 |
|---|---|---|
| P0 | scene_id / label / pos / role / dead | 每 tick；dead CRT 3s 节流 |
| P1 | vitality / money | ~2.5–3s |
| P2 | equip_dura / host_id / in_team | ~8–15s |

### 不进连续 producer（按需）
- 整包 bag / warehouse / roll 列表 / 队伍名册解析 / 商店

消费：`get_live_scene` / `state_dispatch.get_state(SCENE|POS|DEAD|VITALITY|EQUIP_DURA|MONEY|…)`  
业务禁止各自扫图；`fresh=True` 仅关键路径。
