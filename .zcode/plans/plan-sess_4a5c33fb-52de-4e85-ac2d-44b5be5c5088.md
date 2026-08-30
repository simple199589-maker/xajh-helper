# 统一开/关挂机入口 refactor 方案

## 根因回顾
`_run_hang_sync`（队内控/群控/云控共用）自造了一套 worker：probe 到"already 开"直接 return，不复建丸子门控 → 10:01:50 off 停掉丸子后，之后 4 次 on 全部 skip，门控真空 10 分钟。本质是该路径与挂机设置页的开关流程分叉、各自漂移。

## 目标架构（一条逻辑，两个入口形态）
1. **core 层唯一管线** `app/core/hang_settings.py` 新增 `apply_hang_switch(session, cfg, desired_on, *, hwnd=0, temporary_mode=None, source=None, log=None) -> dict`：
   - `temporary_mode in (0,1)` 时 `replace(cfg, mode=...)` **仅内存生效，绝不落盘**；
   - `desired_on` 先 `apply_hang_prepare`（失败即返回）；
   - probe：已处于目标状态且模式一致 → **对账丸子**（on→`start_wanzi_packet_hang` 幂等 reuse/重启；off→`stop_wanzi_packet_hang(release=True)`）后返回 `skipped=True`；这是本次 bug 的修复点，烘焙进唯一实现；
   - 未达目标 → `start_hang` / `stop_hang`（透传 source）；
   - `read_hang_live` 复核，返回 `{ok, message, skipped, live, cfg(生效cfg)}`。
2. **挂机设置页公开方法** `SettingsPage.apply_hang_switch(desired_on, *, temporary_mode=None, persist=True, source_label="挂机设置", on_done=None)`（_impl.py 6489/6559 重构）：
   - `persist=True`（按钮路径）：`_hang_cfg_from_ui` + `_apply_hang_cfg_to_session` 落盘（现状行为不变）；
   - `persist=False`（远端/命令路径）：`get_hang_config(char_id=...)` 读已存配置，不落盘、不 `update_hang_guard_config`；
   - 沿用 `_hang_busy` 防重入（按钮与远端共用一把锁）、worker 线程、attach 生命周期（guard ok 时保留）、`_push("hang_done", ...)` 完成回调（hang_done 处理器增加可选 source_label，user_log 来源如实标注）；
   - 结束时回调 `on_done(ok, message)`（worker 线程调用，调用方用 `_push` 回 UI）。
   - `_on_hang_start` / `_on_hang_stop` 改为薄封装调用它，按钮行为不变。

## 迁移的入口（全部改走上述管线，删除各自 worker）
| 入口 | 位置 | 改法 |
|---|---|---|
| 队内控/群控/云控 挂机同步（本次 bug 路径） | `_impl.py:14534 _run_hang_sync` | 保留签名与 `_hang_sync_lock`，body 改为 `_sibling_page("settings").apply_hang_switch(..., persist=False, temporary_mode=..., source_label="副控内挂")`；设置页缺失时记错误日志并返回（不再留旧逻辑兜底） |
| 主控"内挂同步"按钮本号应用 | `_impl.py:14350 _on_team_hang_sync` | worker 内 probe→取反保留，本号开/关改调 page 能力（persist=False，temporary_mode=1/0）；`on_done` 里保留 publish `ACTION_HANG_SYNC`、`_team_hang_force_started`（从返回的生效 cfg 读 empty_skill）、状态文案 |
| 日常 routine 本号开/关挂机 | `task_schedule.py:1610 _start_routine_hang_local`、`:1782 _stop_routine_autoplay` | 删除自备 prepare+start/stop，改调 core `apply_hang_switch`（temporary_mode=0 强制普通模式的既有语义不变）；`ignore_dungeon_stuck` 的 `_arm_dungeon_target_guard` 后置步骤保留 |
| 登录编排自动挂机 | `login_orchestrator.py:431-439 _apply_role_settings` | prepare+start 改为一次 core `apply_hang_switch` 调用（cfg 已含角色盘上 mode，去掉直接 `cfg.mode` 变异） |

## 明确不动（及理由）
- `activity_auto.py` 切糕引擎 `_ensure_hang_on/_ensure_hang_off`（8314/8217）：引擎内部阶段控制（复活/解卡强制语义、guard 续命），已走 core start/stop 原语，不是用户可见开关入口，强行统一会改变 force 行为。
- 实验页强开/强关（main_window.py:5638/5676）：故意绕 gate 的调试工具。
- 手动丸子按钮（`start_wanzi_packet_manual`）：独立能力，非挂机开关。
- 直发包 1500/160002 仅存在于 hang_settings 内部（已核实无外部重复发包者）。

## 行为要点
- 远端/临时命令**永不落盘**覆盖角色配置；`temporary_mode` 只影响本次运行。
- "already 开" 快速路径保留但必对账丸子门控（修复本次 bug，同时避免主控高频 on/off/on 造成重启抖动）。
- 队内控 `挂done` 回执时机不变（就地回执，不等 worker 完成）。

## 验证
- 跑现有相关测试：`test_hang_settings.py`、`test_task_schedule.py`、`test_task_sync.py`、`test_cloud_sync.py`、`test_input_and_runners.py`、`test_task_and_navigation.py`。
- 在 `test_hang_settings.py` 增补用例：already-on 时 `apply_hang_switch` 会调用 `start_wanzi_packet_hang` 对账（复用现有 fake session 模式）；temporary_mode 不改写传入 cfg 原对象。
- 日志抽查点：远端 on/off 后应看到 `丸子控制门禁已启动/外功丸子挂机直发已启动` 或 skipped+对账消息，`source=` 体现真实调用方。