# 队伍缩略面板快捷动作：指定跟随 & 选中同步（能力封装）

> 正式能力文档。调研 2026-08-25，x32dbg + LyScript 断点实测命中确认，封装已实测验证（游戏内生效）。
> 2026-08-26 修复：右键队员跟随/选中同步必须用 `Win_QuickTeamMember` 命令对象，且需写 `+0x170/+0x174` 目标字段。
> 目标：笑傲江湖 OL（xajh.exe，x86，主模块基址 0x400000）。
> 封装位置：`app/core/team_ops.py`

## 1. 两个能力

| 能力 | UI 入口 | 处理函数 | 是否发包 | 封装 API |
| --- | --- | --- | --- | --- |
| 指定跟随 | 右键队员头像 → `Btn_Follow` | `0x009A0120` | 否（纯本地） | `quick_team_follow` |
| 选中同步 | 右键队员头像 → `Btn_TargetSame` | `0x009A0390` | 否（纯本地） | `quick_team_target_sync` |

均为**纯客户端本地逻辑**，不发网络封包。已实测验证。

## 2. 关键机制（修复核心，勿改动）

- 右键普通队员头像使用 `Win_QuickTeamMember`，右键队长头像使用 `Win_QuickTeamLeader`；两者 vtable 相同（`0x128AB34`）但对象不同。封装按目标类型优先选择对应对象，并保留另一个对象作为回退。
- `0x009A0120` / `0x009A0390` 内部通过 `0xE8CD20` 读取 **`Win_QuickTeamMember + 0x170/+0x174`** 作为「目标 id」。
- `plg::SetTarget` **不更新** `+0x170/+0x174`（它只更新另一个选中系统）。真实右键时游戏会把目标写入这两个字段。
- **因此调用前必须手动写**：`[cmd + 0x170] = 目标id低32位`，`[cmd + 0x174] = 目标id高32位`。

封装已内置此逻辑（`_quick_cmd_this` 优先返回 `Win_QuickTeamMember`；调用前写 `+0x170/+0x174`）。

## 3. 调用方式与入参

> **入参：`target_id` = 目标角色的用户 id（64 位，十进制或十六进制）。**
> 用户 id 可通过 `list_party_members(session)` / `read_cecteam_members(session)` 读取（`obj_id` 字段）。

### 3.1 指定跟随 — `quick_team_follow(session, *, target_id, log)`

```python
from app.core.team_ops import quick_team_follow

r = quick_team_follow(session, target_id=647169)   # 十进制 id
r = quick_team_follow(session, target_id=0x9E001)  # 十六进制等价

# target_id=0：跟随 command 对象当前记录的目标（上次右键/跟随写入的 +0x170）
r = quick_team_follow(session)
```

- 流程：`plg::SetTarget(target_id)`（确保选中）→ 写 `[cmd+0x170]=target_id` → 调 `0x009A0120`
- 效果：自己开始跟随目标队员（本地跟随目标切换，队友无感知，不发包）

### 3.2 选中同步 — `quick_team_target_sync(session, *, target_id, log)`

```python
from app.core.team_ops import quick_team_target_sync

r = quick_team_target_sync(session, target_id=647169)
r = quick_team_target_sync(session)   # 用 command 对象当前目标
```

- 流程：`SetTarget(target_id)` → 写 `[cmd+0x170]=target_id` → 调 `0x009A0390`
- 效果：自己的选中目标 = 目标队员当前选中的目标（纯本地）
- 注意：目标队员**当前必须已选中某物**，否则提示「无法选中对方选中的目标」

## 4. 返回值 `TeamOpResult`

```python
@dataclass
class TeamOpResult:
    ok: bool          # 远程调用执行成功（未抛异常）
    action: str       # "quick_follow" / "quick_target_sync"
    message: str      # 人类可读结果
    error: str | None # 失败原因
    detail: dict      # this / target_id / ret / path / client_local=True
```

> `detail.ret` 是命令对象收尾调用的返回值，**不代表动作成败**；效果以游戏表现为准。

## 5. UI 控件与命令对象定位（断点验证）

### 命令对象（this）
- 右键普通队员跟随/选中同步：**`Win_QuickTeamMember`**（vtable `0x0128AB34`）
- 右键队长操作：**`Win_QuickTeamLeader`**（同 vtable）
- 若对应菜单对象尚未创建，封装允许使用另一个对象回退。
- 获取：`get_game_ui_dlg(session, "Win_QuickTeamMember")`

### 右键菜单命令表（0x011DCC40 区域，0x86E630 注册）

| 控件名 | 动作函数 | 封装 |
| --- | --- | --- |
| `Btn_Follow` (0x128A864) | 0x009A0120 | `quick_team_follow` |
| `Btn_TargetSame` (0x128AAE8) | 0x009A0390 | `quick_team_target_sync` |
| `Btn_AddFriend` (0x128AAF8) | 0x9A0290 | — |
| `Btn_Trade` (0x128AB08) | 0x9A0240 | — |
| `Btn_Disband` (0x1289FCC) | 0x9A04A0 | — |
| `Btn_Leave` (0x1289FD8) | 0x9A0520 | — |
| `Btn_TeamSetting` (0x128AAD8) | 0x9A0180 | — |

### 关键函数调用链（断点实测）

- 真实点击跟随：`0x009A0120`(this=Win_QuickTeamMember) → `0xE8CD20` 读 `+0x170/+0x174` → `0x8CEE30`(→0x4AE3B0) → `0x49C940`(执行) → `0x60DAF0`(真正跟随)
- `0xE8CD20`：`mov edi,[esi+0x170]; mov esi,[esi+0x174]; eax=edi; edx=esi; ret`
- `0x49C940`：`mov ecx,[esi+0x94]; test ecx,ecx; jz skip; call 0x60DAF0`（[obj+0x94] 非 0 才执行跟随）

## 6. 重要区分：指定跟随 vs 组队跟随

| | 指定跟随（本封装） | 组队跟随 |
| --- | --- | --- |
| 入口 | 右键队员头像 → 跟随 | `Win_TeamMain` 顶部「组队跟随」 |
| 处理函数 | `0x009A0120` | `0x9A0680`/`0x9A06A0` → `0xCEC8A0` |
| 是否发包 | 否 | 是（c2s `0x1286`） |
| 封装 | `quick_team_follow` | `set_team_follow`（已有） |

⚠️ 两个「跟随」是不同的功能，不要混用。

## 7. 已知限制

- 需游戏已附加（`session` 来自 `GameAttachSession` / `open_attach_session`）。
- 需已组队且有目标队员（用户 id 来自队伍成员读取）。
- 选中同步需目标队员当前选中了某物。
- 命令对象方法返回值不代表成败，效果以游戏表现为准。

## 8. 队伍集合（进行中，未完全验证）

`quick_team_gather`（`Btn_Gather` → 核心 `0x00885300`）：
- 命令对象 this = `[get_game_ui_dlg("Win_TeamMain") + 0x34]`
- 核心 `0x885300` 当前被注入 `xajh_chat_tap.dll` hook（`0x04181820`），参数依赖命令对象内部状态
- 远程调用已可执行，但实测未观察到弹提示，**待进一步验证**（可能需 UI 线程执行或模拟真实点击）。


