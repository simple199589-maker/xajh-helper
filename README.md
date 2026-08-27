# XAJH Helper

> **用途声明**
>
> 本程序仅供本地学习、研究与交流使用，切勿用于或纳入任何生产用途。

Windows 桌面助手，面向《笑傲江湖 OL》多角色日常操作。仓库已进入生产收敛阶段：正式 GUI、桥接组件、回归测试和可复用研究成果保留；一次性探针、临时输出和历史备份不再进入主分支。

## 正式功能

| 功能 | 说明 |
| --- | --- |
| 登录与连接 | 卡密登录、一键连接多开、前台按 `Delete` 连接或唤起功能窗 |
| 快捷设置 | 按角色保存挂机、主副控、云控和定时任务设置；功能窗默认打开此页 |
| 挂机 | 普通/副本模式、范围、拾取、无技能、丸子、有凤来仪、维修、补活力 |
| 自动宝箱 | 扫描、单次开箱、循环开箱、临时与固定黑名单 |
| 九层妖楼 | 进本、验证码识别与窗口尺寸校验 |
| 自动副本 | 活跃、副本、切糕流程 |
| 自动任务 | 任务刷新、寻路、接交、定时队列、组队和地图飞行 |
| 杂货使用 | 背包使用、自动使用、自动出售和已固化的业务脚本 |
| 鼠标/键盘 | 后台按键、鼠标与组合输入 |
| 系统日志 | 当前角色的关键操作、运行状态和失败原因 |

产品能力与边界见 [功能说明](docs/FEATURES.md)，具体操作见 [用户手册](docs/USER_MANUAL.md)。

## 登录权限

程序启动必须具有 Windows 管理员权限，这是连接游戏进程的系统权限，与登录卡密不是一回事。

| 登录类型 | 有效期 | 自动宝箱 | 云控 | 其它正式功能 |
| --- | --- | --- | --- | --- |
| 服务端正式卡密 | 服务端返回 | 可用 | 可用 | 可用 |
| 本地测试卡 `admin` | 每次登录 16 小时 | 不可用，页面不创建 | 不可用 | 可用 |

`admin` 可在源码、测试包和生产包中手工输入。生产包不主动预填该测试卡。

## 运行

生产交付运行：

```text
dist\xajh_helper\xajh_helper.exe
```

必须分发整个 `dist\xajh_helper\`，不能只复制 EXE，也不要运行 `build\` 中间产物。

源码运行：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

## 测试与构建

```powershell
# 当前生产门禁
python -m compileall -q app common packet memory tests tools
python -m unittest tests.test_foundation_services tests.test_config_and_facades tests.test_upx_pack -q

# 历史全量套件审计（当前仍有待校准用例，见发布检查单）
python -m unittest discover -s tests -v

# 生产包
cmd /c tools\build_gui.bat prod

# 内部测试包
cmd /c tools\build_gui.bat dev

# 换目录打包（默认 dist\xajh_helper 被运行中的游戏/助手锁定，无法替换 DLL 时）
cmd /c tools\build_gui.bat prod v1.0.7
```

生产构建会对顶层 `xajh_helper.exe` 应用固定版本 UPX 普通壳，并执行 UPX 完整性测试和冻结运行时冒烟检查。`_internal`、桥接 DLL、注入器和辅助进程保持原样，避免影响生产加载。开发包默认不加壳；可用 `XAJH_ENABLE_UPX=0|1` 覆盖默认值，或用 `XAJH_UPX_EXE` 指向本机已验证的 UPX。

打包默认输出到 `dist\xajh_helper\`。若该目录被运行中的游戏/助手锁定（游戏会加载 `dist\...\native\bin` 下的桥接 DLL，无法删除/改名），可传第二个版本号参数把输出切到全新目录：`build_gui.bat prod v1.0.7` 会打包到 `dist\xajh_helper-v1.0.7\`，不触碰被锁定的旧目录。整个流程（free-lock、PyInstaller、桥接校验、UPX、冒烟）都跟随该目录。

UPX 固定为 `5.2.0`，下载包按官方 SHA-256 验证。当前 PyInstaller Windows bootloader 启用了 CFG，UPX 处理顶层启动器时需要 `--force`，因此该启动器的 CFG 会被移除；原生组件不受影响。加壳报告写入 `build/generated/upx_pack_report.json`。

生产验收见 [生产发布检查单](docs/PRODUCTION_CHECKLIST.md)，工程结构见 [维护交接](docs/AI_HANDOFF.md)。

## 目录

```text
app/       正式 GUI 与业务实现
common/    路径、环境等公共能力
native/    x86 桥接 DLL、注入器和读数工具
packet/    已沉淀的封包分析能力
tests/     自动化回归测试
tools/     正式构建、数据生成和协议分析工具
docs/      用户、发布、协议与研究结论文档
```

`build/`、`dist/`、`logs/`、`runtime/`、`captures/` 和 `.issues/` 均为本机生成内容，不提交仓库。
