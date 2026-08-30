# 副本挂机（跟随打怪）"不攻击"问题 — 根因定位与整链修复

> 日期：2026-08-29 → 2026-08-30
> 状态：**已修复，实机验证通过**（多副本、过图重入、全队无技能编队）。
> 逆向细节全档：`.issues/DUNGEON_FIGHT_CHAIN_20260829.md`

## 一、问题与根因

**现象**：副本模式挂机后角色站街不攻击；过图重入后复发；关挂重开状态粘滞。

**根因**（x32dbg 单步 + RPM 逐层实证）：副本挂机仅以封包开启时，状态机初始化
依赖"服务器响应 + seed follow"链。**队长身份**开启时跟随快照被持续刷成自己
（自引用），`StateAlert` 的四个出口条件（cond3~6）全关——状态机无路径到达
`StateAttack`，攻击子系统无人驱动 → 目标握着不掉血 → 永久死锁。队员身份开启
时快照指向真队长，链路正常（这就是手动"移交队长→以队员开挂→拿回"流程的成因）。

**关键状态机事实**（调试实证，详见逆向文档）：
- `instance_follow_fight` 状态族 vtable：Idle=0x12BEDBC / Alert=0x12BEE04 /
  FollowTarget=0x12BEEFC / Attack=0x12BF004；
- `StateAlert` 级联的出口之一 cond6 需要"快照目标 ≠ 当前选中目标"才重新跟随，
  而状态应用会把选中目标同步进快照 → 恒等 → 永不出口；
- `StateAttack` 态每帧驱动攻击组件（0xC53BC0，门：host+0x2299/0x229A 非零）。

## 二、修复链（按数据流顺序）

| 环节 | 改动 | 文件 |
| --- | --- | --- |
| 开挂初始化 | 封包前本地直调 StartAutoPlay（机器直入 StateAttack）+ 场景门等待 5s | `hang_settings.py` `_start_hang_unlocked` |
| 运行中补目标 | 放行看门狗：站街 ≥10s 无目标 → AOI 最近怪 ≤30m → F 通道（0x754280 thiscall）点名 | `dungeon_fight_kick.py` `maybe_kick` |
| 有目标不打 | 驱动攻击组件：Alert ∧ 握目标 ∧ 静止 ≥8s → 桥接命令驱动 0xC53BC0 ×4 | `follow_fight_watchdog.py` 分支2 |
| 过图重入 | scene rearm 异步重初始化（wait_scene_ready 30s 精等 → force → running 校验恢复，重试上限 5） | `hang_settings.py` `_tick` + `schedule_dungeon_rearm_reinit` 已被 tick 内联重试取代 |
| 停止失效 | 关闭后校验 running，仍运行则自动"重发开启→再关闭"×2 | `hang_settings.py` `stop_hang._finish` |
| 粘滞兜底 | Alert ≥10s 告警提示手动舞步（native 方案1 后应不再触发，保留兜底） | `follow_fight_watchdog.py` |

## 三、附带修复（同会话）

1. **scene-settle 门去抖**（`remote_runtime.note_pid_scene_snapshot`）：桥接并发读数
   的瞬时失败不再清零稳定窗（连续 3 次才算切换）——修复"扫不到怪"假象与 CRT 永久被拒。
2. **`_check_hang_state.py` RTTI 化**：旧 vtable 映射失效导致的"UNKNOWN"误报已修。
3. **`_arm_dungeon_target_guard` 短路**：守护 tick 不再每秒重入（mode=3 每次重置 trace）。
4. **构建 ID 单源化**：`bridge_protocol.py` 为唯一源，构建经 `/D` 传入 C++（header 尊重外部定义）；
   移除版本号副本产物——**全链统一 `xajh_bridge.dll` 单一命名**。

## 四、验证

- **静态**：`tools/check_hang_chain.py` — 35/35 环节通过（接线/协议/命名/编译）；
- **动态**：实机监控 50 分钟（`_monitor_19080.py`）——三副本（1524/1508/68 往返）
  全自动：进本 → Alert→Attack→SelectTarget→Killing 循环 → 清完回城 → 再进自动重复；
- 伴随修复的回归：132 例相关测试全过。

## 五、部署

- 源码运行：重启助手即生效（`run.bat` 启动时按内容比对同步 `build/native → native/bin`）；
- 生产：`tools\build_gui.bat prod`（DLL 与代码同源构建，一并打进 dist）；
- **换桥必须换游戏**：DLL 注入后固定在进程里，旧游戏进程重启后才会加载新桥。

## 六、红线（客户端崩溃教训）

1. 游戏逻辑函数（promote 0x73E300 等）**严禁远程线程调用**——UI 线程桥接命令或封包（CD1740）才安全；
2. 软件断点**严禁在目标运行时设置**——只能暂停态操作（x32dbg/LyScript 流程见
   `.issues/_x32_alert_deep.py` 模板）；
3. 汇编复刻类 hook：**每跳返回值的传播必须逐条核对**（ACCESS_VIOLATION 教训）。

## 七、日志前缀对照（两功能独立，勿混淆）

| 前缀 | 功能 | 归属 |
| --- | --- | --- |
| `副本放行：…` | 忽略怪放行（名单内怪提交/待机/恢复） | dungeon_fight_kick |
| `跟随看护：…` | 跟随机器卡监测（Alert 告警 / 驱动接战） | follow_fight_watchdog |
| `hang guard: scene rearm autoplay re-init` | 过图重初始化 | hang_settings 守护 tick |

@-author 整理 by ak（逆向与实机验证记录见 `.issues/DUNGEON_FIGHT_CHAIN_20260829.md`）
