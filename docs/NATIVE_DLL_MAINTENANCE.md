# xajh_bridge DLL 维护与使用规范

> 适用：`native/xajh_bridge/`（桥接 DLL 与注入器）。
> 2026-08-30 规范化：去除版本号副本产物，全链统一 `xajh_bridge.dll` 单一命名。
> 违反本规范的部署方式（手动拷贝、版本号命名、运行态设断点）均已被实战证明会出事故。

## 一、产物与命名（铁律）

| 项 | 规定 |
| --- | --- |
| 唯一产物名 | **`xajh_bridge.dll`**（无版本号后缀） |
| 禁止 | 生成/拷贝/加载 `xajh_bridge_<日期序号>.dll` 之类的版本号副本 |
| 更新检测 | 内容比对（run.bat 的 `fc /b`、stage 的 sha256），**与文件名无关** |
| 构建暂存 | `build/native/`——可再生的暂存区，build_gui 每次先清空重建，勿手动维护 |

全链（构建产物 → run.bat 同步 → native/bin → 注入 → dist 打包）只认这一个名字。

## 二、构建 ID 规范（单源）

- **唯一源**：`app/core/bridge_protocol.py` 的 `BRIDGE_BUILD_ID`（当前 2026083001）。
- 构建时 `build_x86.bat` 从它解析并通过 `/DBRIDGE_BUILD_ID=<值>` 传给 C++ 编译器；
  `bridge_protocol.h` 用 `#ifndef` 尊重外部定义（不得在头文件里硬编码覆盖）。
- **DLL 实质变更（影响行为）必须 bump**（格式 YYYYMMDDNN）——pong 上报、
  日志排障、二进制核对都依赖它区分新旧。
- 仅改注释/空白可不变更。

## 三、构建流程

```
1. 改 native/xajh_bridge/dllmain.cpp
2. 实质变更 → bump bridge_protocol.py 的 BRIDGE_BUILD_ID
3. cmd /c native\xajh_bridge\build_x86.bat
   （产物落 build/native/，构建暂存区；编译错误会 exit /b 1）
4. 校验：新 ID 字面量已编译进 DLL（pefile dword 计数法，见文档尾）
```

完整打包（同时重建全部 native 产物）：

```
tools\build_gui.bat prod [版本号]    # dev = 内部测试（无 UPX 壳）
```

`build_gui.bat` 会自动：清空重建 `build/native/` → `check_native_bin.py` 清单校验 →
拷贝到 `native/bin/`（源码运行目录）→ 打进 dist。**禁止手动拷贝 DLL 进 dist。**

## 四、部署与游戏侧生效（换桥必须换游戏）

DLL 注入后固定映射在游戏进程里，**无法热替换**。生效顺序：

```
1. 关闭所有游戏窗（旧桥随进程释放）
2. 重启助手（启动时自动把 build/native → native/bin 按内容同步）
3. 再开游戏窗（attach 时注入新桥）
4. 日志验证：pong build=<新 ID> —— 看到才算新 DLL 上场
```

**顺序铁律：先助手后游戏。** 游戏先开、助手后动，注入的是旧桥
（旧桥已在游戏里时，stale 检测/武装拒绝都会触发，表现为"bridge unavailable or stale"）。

## 五、使用红线（客户端崩溃教训）

1. **游戏逻辑函数严禁远程线程调用**——UI 线程桥接命令或封包（CD1740 任意线程安全）。
   反例：promote（0x73E300）远线程调用 → 十丶三窗 ACCESS_VIOLATION 崩溃（2026-08-29）。
2. **软件断点严禁在目标运行时设置**——INT3 补丁写到执行中代码 → 崩溃（2026-08-30）。
   断点只能在暂停态操作；运行态下的 `RunCmd("bp ...")` 同样禁止。
3. **汇编复刻类 hook**：原函数逐指令复刻时，**每跳返回值的传播必须逐条核对**
   （参数传错 → ACCESS_VIOLATION，2026-08-30）。复刻后先在调试器确认行为再上生产。

## 六、诊断手段

| 手段 | 用法 |
| --- | --- |
| pong 构建 ID | 日志 `pong build=<ID>`，与 `bridge_protocol.py` 对比即知新旧 |
| 二进制核对 | pefile 在镜像里按 dword 搜目标 ID（见 `.issues` 各实验脚本） |
| 状态机监控 | `.issues/_monitor_<PID>.py`（纯 RPM 只读，变化才记录） |
| 链路检查 | `tools/check_hang_chain.py`（35 环静态接线 + 协议一致性） |
| 产物校验 | `tools/check_native_bin.py <dir>`（清单/空文件/架构） |
| 崩溃定位 | `runtime/crashdumps/` + `logs/xajh_helper_error_<日期>.log` |

## 七、相关文档

- 逆向与根因全档：`.issues/DUNGEON_FIGHT_CHAIN_20260829.md`
- 跟随打怪修复交付：`docs/FOLLOW_FIGHT_FIX_20260830.md`
- 维护交接总纲：`docs/AI_HANDOFF.md`
- 功能说明：`docs/FEATURES.md`

@-author 规范整理 by ak
