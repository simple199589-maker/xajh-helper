# 队内控 send_mgr 精确定位 — 正式功能成果

日期：2026-08-28  
状态：已转正式  
范围：队内控发送管理器（`send_mgr`）精确定位、进程内 mailbox 发送、生产集成与实机验证。  
不包含：私聊发送。私聊接收已有原始能力，但发送封包仍未转正式。

## 1. 结论

队内控发送不再依赖“先手动 PING 多次、等聊天 hook 撞到 send_mgr”的预热流程。

`xajh_team_tap.dll` v5 注入后会主动扫描本进程内存，按发送管理器对象的稳定虚表和字段不变式精确定位 `send_mgr`；定位成功后写入共享内存，Python 侧可直接组包并投递到进程内 mailbox，由游戏进程内 sender 线程调用原生 wrapper 发送。

实机三开验证结果：

```text
[16332] PASS v=5 status=1 mgr=0x37FBB638 source=exact exact=FOUND count=1
[22476] PASS v=5 status=1 mgr=0x342682A8 source=exact exact=FOUND count=1
[42148] PASS v=5 status=1 mgr=0x3247E620 source=exact exact=FOUND count=1
summary: PASS
```

完整诊断快见 `.issues/deliver/send_mgr_exact_20260828_diag.log`。

## 2. 客户端固定特征

目标客户端为固定 x86 构建：

- ImageBase：`0x00400000`
- TimeDateStamp：`0x56736608`
- SizeOfImage：`0x038A5000`

关键发送 wrapper：

```text
0x00D089B0
thiscall(this=send_mgr, octets*) -> bool
```

`send_mgr` 精确签名：

```text
[obj + 0x00] == live(0x012C7C00)   vtable
[obj + 0x7C] == obj
[obj + 0x9C] == obj
[obj + 0xCC] == live(0x01D61A88)   crypto cfg A
[obj + 0xD0] == live(0x01D61B9C)   crypto cfg B
[obj + 0x24] == 0x00008000         buffer cap
```

DLL 内保存的是 RVA，运行时按实际模块基址换算：

```text
SEND_MGR_VTABLE_RVA = 0x00EC7C00
CRYPTO_CFG_A_RVA    = 0x01961A88
CRYPTO_CFG_B_RVA    = 0x01961B9C
```

扫描时要求对象 8 字节对齐，并读取完整 `0xE0` 对象边界，避免把代码段、立即数或短匹配误判为对象。只有唯一命中 `count == 1` 时才写入正式 `send_mgr`。

## 3. 生产运行链路

1. UI 走队内控/群控初始化路径。
2. `app/core/team_chat.py::ensure_team_tap()` 注入 `xajh_team_tap.dll`。
3. DLL 创建 `Local\XajhTeamTap_<pid>` 共享内存，并启动精确扫描线程。
4. 精确扫描唯一命中后写入：
   - `send_mgr`
   - `send_mgr_seen`
   - `exact_send_mgr`
   - `exact_send_mgr_seen`
   - `exact_scan_status = EXACT_SCAN_FOUND`
   - `exact_scan_count = 1`
5. Python 侧读取共享内存并校验 v5、active、唯一命中。
6. Python 构造聊天 c2s 明文，写入 mailbox。
7. 游戏进程内 sender 线程以正确 thiscall 栈帧调用 `0x00D089B0` 发送。
8. 接收继续由 `xajh_chat_tap.dll` 的队伍专用 ring 读取。

## 4. 共享内存 v5 关键布局

协议版本：`TEAM_TAP_VERSION = 5`  
结构总长：`1748`

| 字段 | 偏移 | 含义 |
| --- | ---: | --- |
| magic | 0 | `TMTP` |
| version | 4 | `5` |
| struct_size | 8 | `1748` |
| status | 12 | `1 = ACTIVE` |
| send_mgr | 20 | 正式发送管理器 |
| send_mgr_seen | 24 | 正式发送管理器有效标记 |
| hits | 160 | wrapper 调用 ring，共 64 条 |
| send_req | 928 | mailbox 请求，单槽，payload 最大 512 |
| dump_len | 1456 | 最近聊天 Octets dump 长度 |
| dump | 1460 | 最近聊天 Octets dump |
| send_ident | 1716 | 聊天封包身份指纹 3 字节 |
| exact_status | 1732 | 精确扫描状态 |
| exact_count | 1736 | 精确命中数量 |
| exact_send_mgr | 1740 | 精确扫描对象 |
| exact_seen | 1744 | 精确扫描结果有效标记 |

精确扫描状态：

| 值 | 含义 |
| --- | --- |
| 0 | idle |
| 1 | running |
| 2 | found |
| 3 | multiple |
| 4 | not_found |

## 5. 防退化与兼容

- 原有 wrapper hook 保留，但不再作为发现前提。
- hook 命中会继续写入 hits ring，可用于核对调用来源和实际聊天流量。
- v5 的发送就绪依据是精确扫描唯一命中；`hook_hits` 可以是 0。
- Python 侧要求 `version >= 5`；发现旧版驻留时明确报“需重启游戏加载 v5”。
- DLL 不能在已经驻留旧版本的同进程中热替换；旧 DLL 仍持有共享内存和 hook，必须重启游戏进程。
- 如果 `send_mgr` 发送失败，Python 会清掉正式槽；DLL 精确扫描线程会重新校验对象。

## 6. 构建与产物

生产目录：

```text
native/bin/xajh_team_tap.dll
native/bin/xajh_inject.exe
```

当前生产 team_tap 产物：

```text
native/bin/xajh_team_tap.dll
size   = 116224
sha256 = 3E1F9E6DC0C2B1461E3F25555F14928A72FE25E9FC7A8214B6AAA5D206977321
```

实机验证使用的独立 v5 发布件：

```text
dist/team_tap_exact_v5/xajh_team_tap.dll
size   = 116224
sha256 = 6E0D761E50A7AEDBAD8FFE77B155519438D8A461166D68F6BDE37B4FB3FD7822
```

说明：两个 DLL 来自同一 v5 源码的不同构建批次，二进制哈希不同；协议、偏移和扫描规则一致。发布包以 `native/bin` 当前生产产物为准。

构建：

```powershell
native\xajh_team_tap\build_x86.bat
native\build_all.bat
```

产物检查：

```powershell
python tools\check_native_bin.py native\bin
```

## 7. 诊断

生产/源码目录：

```powershell
python .\diag_team_send_mgr_exact.py --wait 0
```

独立注入测试：

```powershell
python .\diag_team_send_mgr_exact.py --inject --wait 0
python .\diag_team_send_mgr_exact.py 16332 22476 42148 --inject --wait 0
```

判定标准：

```text
v=5
status=1
source=exact
exact=FOUND
count=1
send_mgr == exact_send_mgr
summary: PASS
```

`hook_hits` 不要求大于 0；精确扫描成功时即使没有手动聊天流量也应 `PASS`。

## 8. 验证记录

2026-08-28 实机三开：

```text
=== team_tap exact send_mgr test ===
[16332] PASS v=5 status=1 mgr=0x37FBB638 source=exact exact=FOUND count=1 hook_hits=64
[22476] PASS v=5 status=1 mgr=0x342682A8 source=exact exact=FOUND count=1 hook_hits=64
[42148] PASS v=5 status=1 mgr=0x3247E620 source=exact exact=FOUND count=1 hook_hits=64
summary: PASS
```

其他检查：

```text
python tools\check_native_bin.py native\bin
native bin OK: 8 files present
```

## 9. 边界

- 该成果只固化“精确找到 send_mgr 并发送队伍聊天”。
- 客户端更新、PE 时间戳、镜像大小、vtable RVA或对象布局变化时，必须重新逆向并更新签名。
- `exact_count != 1` 时不允许选择候选对象发送，避免误投递或崩溃。
- 私聊发送仍未定案；不能把本成果扩展为“私聊控制通道已可用”。
