# XAJH Helper 维护交接

本文记录当前生产结构和不可破坏的约束。正式行为以运行时、测试和当前代码为准；历史调研附件不纳入生产仓库。

## 文档边界

- `README.md`：项目入口、快速运行、构建和文档索引。
- `docs/FEATURES.md`：生产功能、权限矩阵和用户可见行为的范围来源。
- `docs/USER_MANUAL.md`：面向用户的操作步骤与常见问题。
- `docs/PRODUCTION_CHECKLIST.md`：发布前的构建与人工验收清单。
- 本文：实现边界、维护约束和验证基线。

功能发生变化时，应同步维护功能说明和用户手册；研究结论只有进入稳定界面并完成回归后，才能写入生产功能说明。

## 入口与交付

- 源码入口：`main.py -> app.ui.app_shell.main`
- 正式功能窗：`app/ui/session_window.py`
- 页面实现：`app/ui/pages/`
- 打包规格：`xajh_helper.spec`
- 构建入口：`tools/build_gui.bat`
- 交付目录：整个 `dist/xajh_helper/`（默认；`build_gui.bat prod <版本>` 可切到 `dist/xajh_helper-<版本>/`，用于默认目录被运行中的游戏锁定时）

生产包排除旧开发工作台的高风险实验入口，但源码和开发渠道仍可用于维护已沉淀的研究能力。生产构建默认只对顶层启动器应用固定版本 UPX，并在压缩后执行 `upx -t` 与冻结运行时 smoke test。

## 核心边界

| 边界 | 负责模块 |
| --- | --- |
| 登录、测试卡、会话权限 | `app/core/auth_mock.py` |
| 服务端卡密校验与签名 | `app/core/license_client.py` |
| 渠道配置 | `app/core/build_profile.py` |
| 游戏连接与注入门禁 | `app/core/inject_gate.py`、`app/core/xajh_bridge.py` |
| 每角色会话与页面 | `app/core/session_store.py`、`app/ui/session_window.py` |
| 挂机配置与维护 | `app/core/hang_settings.py` |
| 远程调用串行化 | `app/core/safe_dispatch.py`、`app/core/remote_runtime.py` |
| 任务与同步 | `app/core/task_api.py`、`app/core/task_sync.py` |
| 自动业务 | `app/core/loot/`、`activity_auto.py`、`yaolu_auto.py` |

## 权限规则

1. 程序启动先过 Windows 管理员权限门禁。
2. 正式卡密必须经 `/api/license/verify` 返回有效 token。
3. 本地测试卡固定为 `admin`，每次登录有效 16 小时。
4. `admin` 不允许自动宝箱，也不允许云控。
5. 功能窗必须根据 `AuthService.auto_loot_allowed` 决定是否创建自动宝箱页，不能只做视觉隐藏。
6. 功能窗默认页为 `settings`。

相关回归测试位于 `tests/test_foundation_services.py` 和 `tests/test_config_and_facades.py`。

## 挂机状态

- 落盘文件：`runtime/config/hang_prefs.json`
- 主键：角色数字 ID
- 默认：副本模式、半径 5、拾取开、维修 50%、活力 20%
- 群控契约：只同步开关，不同步详细配置
- 所有开始、停止、维护、维修、补活力和掷骰操作必须经过挂机互斥与 `SafeDispatch`
- 开始结果必须结合实时内存状态确认，不能只依赖远程调用返回值

## 配置与密钥

- `.env` 仅用于本机源码或构建，已被 Git 忽略。
- 生产 profile 不写验证码 key。
- `LICENSE_API_SECRET` 由 `tools/_write_build_profile.py` 生成到被忽略的 `_pack_secret.py`，不得写入 `build_profile.json`。
- 运行时 token 只保存在内存，不落盘。
- UPX 下载必须匹配 `tools/_pack_upx.py` 中固定的版本和 SHA-256；不得压缩 `_internal` 依赖。

## 研究成果保留规则

`packet/`、`memory/`、开发工作台、数据生成脚本、可复现的方法型工具和专题结论文档属于可维护成果。一次性 `_probe/_scan/_re/_live` 迭代、只处理历史 `.issues` 输出的报告脚本、临时输出、补丁脚本和源文件备份不再提交。

保留的下划线工具仅限正式构建、数据生成和被运行时或测试明确引用的诊断脚本。新增稳定验证应进入 `tests/`，不要继续在 `tools/` 堆叠编号脚本。

## 验证

```powershell
python -m compileall -q app common packet memory tests tools
python -m unittest tests.test_foundation_services tests.test_config_and_facades tests.test_upx_pack -q
cmd /c tools\build_gui.bat prod
```

当前历史全量套件仍有待校准模块：`test_badao_exchange`、`test_captcha_and_ui_geometry`、`test_input_and_runners`、`test_reticle_probe`、`test_skill_cast_probe`、`test_task_and_navigation`、`test_task_schedule`。其中妖楼 runner 的旧时序用例可能长时间不返回；不要把全量发现命令的中断误报为通过。

涉及真实游戏状态的 live 验证必须单独记录客户端版本、PID、输入和回读结果。发布验收使用 `docs/PRODUCTION_CHECKLIST.md`。
