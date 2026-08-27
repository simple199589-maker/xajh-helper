# 计划任务组合日常

- `daily_badao_10021`：霸刀日常，任务 `10021`，到 scene `72` 读取 `101042` 上官霸刀。
- `daily_longaotian_10010`：龙傲天日常，任务 `10010`，动态复用任务 NPC/地宫传送流程。
- `daily_yucanghai_10011`：余沧海日常，任务 `10011`，scene `2030`，目标 TID `101013`。
  该任务按最多三只目标分轮执行：每击杀一只停止挂机并检查任务；未完成则重新扫描、寻路并挂机下一只。
- `daily_dongfang_10006`：东方不败日常，任务 `10006`，scene `2034`，目标 TID `100072`。

两项均为手动加入/移除的计划预设，不自动接取、不自动交付。未接任务记录 `task_skip_not_accepted` 后跳过；已接任务按任务寻路、resident 活体读取、世界坐标最近目标、现有挂机能力组和任务完成状态执行。完成后停止挂机，并使用默认飞行棋槽 `0` 返回福州城；主控通过 `ACTION_MAP_FLY` 同步，从控不重复广播。

执行过程会记录 `route_started`、`monster_selected`、`autoplay_started`、`task_completed`、`return_fly_started`、`routine_done` 和 `routine_blocked`。

## 地图就绪公共函数

所有涉及切图后的任务、resident、寻路和挂机业务，统一使用：

```python
from app.core.remote_runtime import wait_scene_ready

ready = wait_scene_ready(
    session,
    expected_scene=72,
    previous_pos=before_pos,
    require_position_change=True,
    timeout_s=30.0,
    log=log,
)
if not ready["ok"]:
    return "blocked"
```

`wait_scene_ready(session, *, expected_scene=0, timeout_s=30.0, previous_pos=None, require_position_change=False, log=None)` 的行为：

- 优先读取调度中心 `LiveSceneSnapshot.pos`；
- 独立入口脚本无法访问调度中心缓存时，回退到直接内存坐标；
- 校验目标 `scene_id`、有效坐标和 `read_scene_position()`；
- 发生换图时传入 `previous_pos` 并设置 `require_position_change=True`，防止把切图期间冻结的旧坐标当成新地图坐标；
- 已经在目标地图时不要求坐标变化；
- 返回 `ok`、`scene_id`、`pos`、`source`、`coord_changed` 和 `error`。

底层 `wait_pid_scene_stable()` 只用于 CRT/远程调用安全栅栏，不作为地图业务完成判定。
