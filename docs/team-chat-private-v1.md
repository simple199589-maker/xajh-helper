# 私聊收发与组队前离队控制面 — 正式功能成果

日期：2026-08-29  
状态：已转正式（发送链路实机验证通过；私聊专用 ring DLL 已编译待随重启部署）  
范围：私聊 c2s 封包结构、Python 侧收发、组队前跨设备预检查/离队协议（PLEAVE/PLEFT）、生产集成点。  
前置：依赖 `xajh_team_tap.dll` v5（send_mgr 精确定位 + mailbox 发送）与 `xajh_chat_tap.dll`（接收 tap），见 `docs/team-chat-send-mgr-v5.md`。

## 1. 结论

私聊（传音）收发全链路打通，并在此基础上实现"组队前先离队"的跨设备控制面：

```text
主控 ──[主P]PLEAVE@<id>──▶ 游戏服务器 ──▶ 副控（在队则离队）
主控 ◀──[副G]PLEFT@<id> OK=1── 游戏服务器 ◀── 副控回执
```

跨设备（群控不通）+ 未组队（队内控不通）场景下，私聊是唯一控制面 —— 这解开了
"自动整队需要先通信，通信又依赖已组队"的自举矛盾。

实机验证（2026-08-29，三开在线）：

```text
PRIV-OK1: 十丶三(6456) → 初一(35952)   收到 ch=9 "…十丶三&^u 对你说：PRIV-OK1"
PRIV-OK2: 初一(35952) → 十丶三(6456)   收到 ch=9 "…初一&^u 对你说：PRIV-OK2"
E2E:      PLEAVE@0829185101 → 初一(mock离队) → PLEFT@0829185101 OK=1 → 主控确认
```

## 2. 私聊 c2s 封包结构（0x60）

实机抓包定案（`tools/watch_private_capture.py` 高频轮询 team_tap v5 的 dump 槽位，
双样本逐字节回归）：命令字节 `0x60`，与队伍频道 `0x4F` 不同，这是此前假设失败的原因。

```text
offset  size  内容
0x00    1     0x60（私聊命令）
0x01    1     total = 全包长 - 2（与队伍封包同约定）
0x02..0x06    00*5
0x07    1     0x01
0x08..0x0B    00*4
0x0C..0x0F    sender_rid 大端 u32
0x10..0x13    00*4
0x14..0x17    target_rid 大端 u32（私聊目标，核心字段）
0x18…   1+n   sender_name_bytes(u8) + 名字 UTF-16LE
…       1+m   target_name_bytes(u8) + 名字 UTF-16LE
…       2     00 00
…       1+k   text_bytes(u8) + 文本 UTF-16LE
tail    2     00 00

总长公式：len = 24 + (1+2n) + (1+2m) + 2 + (1+2k) + 2
```

金样本（`tests/test_private_chat.py` 内置逐字节回归）：

```text
ZCAP1: 十丶三(0x012B4001)→苦寒未曾来(0x0009E001) "ZCAP1"  57B
ZCAP2: 十丶三(0x012B4001)→初一(0x012BA001)       "ZCAP2"  51B
```

要点：私聊封包没有队伍封包的身份指纹字段（offset 6..9 是固定零/标志位），
发送必须跳过 `_team_mailbox_send` 的 ident 改写（`patch_ident=False`）。

## 3. 发送链路

`app/core/team_chat.py`：

- `build_private_chat_c2s(text, sender_rid, sender_name, target_rid, target_name)` — 组包。
- `send_private_message(pid, target_rid, target_name, text)` — 解析发送方身份
  （rid 复用队内控解析链 + SessionStore；名字走 GetHostPlayer + GetObjectName，
  解析成功后进程级缓存），经 team_tap v5 mailbox 发送，`patch_ident=False`。
- 发送方身份未就绪时拒绝发送（场景门拒绝 CRT 时表现为 `sender name unresolved`，
  正式流程在角色绑定后调用 `cache_role_identity` / `cache_role_name` 预置，零 CRT）。
- **发送限频**：`_private_send_gate` 每发送者滑窗 `PRIVATE_SEND_WINDOW_MAX=3` 条 /
  `PRIVATE_SEND_WINDOW_S=10s`（对齐组队邀请"3 个一批+冷却"的服务端限频量级）。
  窗口满时**阻塞等待**放行（控制面消息不能丢），等待会打日志。队伍最大 6 人
  （除自己最多 5 个目标）→ 前 3 条秒发、后 2 条自动等窗口滑过，整段 PLEAVE
  最长约 10s，随后再等 `PRIVATE_LEAVE_SETTLE_S=5s` 离队缓冲。

## 4. 接收链路

游戏把收到的私聊经 `AddChatMessage(channel=9)` 上屏，chat_tap 全频道捕获：

- 显示格式：接收 `^u&#<噪声>名字&^u 对你说：正文`；本机回显 `你对 … 说：正文`。
  名字带渲染噪声，控制面按花名册匹配（`match_known_sender`），不解析噪声。
- **v3 DLL（`xajh_chat_tap.dll`，2026-08-29 编译）**：新增私聊专用 ring
  （`private_write_seq` + `private_events[512]`，共享内存 303,640 → 580,124 字节）。
  主 ring 容量 50 且战斗 ch=12 高频刷屏，专用 ring 保证控制消息不被挤掉。
  编译产物：`build/native/chat_tap/xajh_chat_tap.dll`
  （SHA256 前缀 `76CCA7A658709505`，115,712B）。旧版驻留时按 v5 同样规则
  **需重启游戏进程后加载**；运行产物由打包流程统一分发。
- **v2 兼容（已实现）**：Python 读取端 `ChatTapReader.open()` 先按 v3 尺寸映射，
  失败自动回退 v2 尺寸（`legacy=True`），主 ring 过滤 ch=9 使用；v2 布局下
  主/队伍 ring 与 v3 同位，解析不受影响。未重启的存量进程不需要任何操作。

## 5. 组队前离队协议（PLEAVE / PLEFT）

`app/core/private_team_link.py`：

```text
[主P]PLEAVE@<msg_id>            主控 → 副控：在队则离队
[副G]PLEFT@<msg_id> OK=<1|0>    副控 → 主控：回执（仅测试/诊断路径使用）
```

**生产策略（2026-08-29 定）：主控只管发，不等回执。**

- 主控 `notify_slaves_leave(pid, targets)`：对名单逐一发送 PLEAVE 后返回；
  调用方（`TeamFormService.form()`）固定等待 `PRIVATE_LEAVE_SETTLE_S`（5s）
  作为离队缓冲。没收到、没离队都不阻断（后续邀请会兜底暴露未离队成员）。
- 副控 `handle_pleave_commands(..., reply=False)`：轮询 → 校验来源在花名册 →
  本地离队（`team_ops.leave_team`）。生产不回 PLEFT（主控不等回执，免私聊噪声）；
  `reply=True` 回执路径保留给 `tools/test_private_e2e.py` 验证与诊断
  （`request_slaves_leave`）。
- msg_id 去重，过期噪声丢弃。
- 接收游标 `PrivateChatWatch`：优先 v3 私聊专用 ring，回退 v2 主 ring 过滤。

## 6. 生产集成点

- **主控**：`TeamFormService.form()` 在"全员离队"步骤前插入
  `_private_precheck_leave()`（`team_ops.py`）—— 对整队名单逐个私聊 PLEAVE
  （只管发），随后固定等待 5s 离队缓冲；之后照旧走群控发布 + 队长离队 + 批量邀请。
- **副控**：任务页 `_team_chat_tick()`（1.5s 周期）在 role=slave 时调用
  `_private_link_tick()`（`app/ui/pages/_impl.py`）—— 不依赖队内控开关，
  只要群控角色为副控即监听私聊命令并执行离队（不回执）。
- 单元测试：`tests/test_private_chat.py`、`tests/test_private_team_link.py`
  （金样本回归、协议解析、只管发/不回执路径、回执去重、身份拒绝路径）。
- 端到端脚本：`tools/test_private_e2e.py`（阶段 0 环境自检 / 1 双向收发 /
  2 协议往返 mock 离队 / `--real-leave` 真离队演练）。

## 7. 边界与注意

- 私聊文本对服务端可见（同现有队内控文本同等暴露面），协议串沿用
  `[主P]/[副G]` 前缀风格，注意不要在正文里写敏感信息。
- 发送依赖 team_tap v5 的 `send_mgr`；`ret=1` 只代表游戏内 wrapper 受理，
  服务端是否投递以接收方 ch=9 为准。
- 离线/旧版副控不回执 → 主控 6 秒超时后继续，不阻塞整队。
- 私聊接收在 v2 DLL 下依赖主 ring（容量 50）+ 1.5s 轮询，战斗刷屏极端情况下
  有被覆盖的理论风险；重启部署 v3 后由专用 ring（512 条）消除。
