# 生产发布检查单

## 1. 仓库检查

- `git status` 中没有密钥、日志、抓包、运行时配置或构建产物。
- 不存在 `*.bak`、临时补丁、编号探针或未引用的研究输出。
- `README.md`、`docs/USER_MANUAL.md` 与当前界面一致。

## 2. 自动化检查

```powershell
python -m compileall -q app common packet memory tests tools
python -m unittest tests.test_foundation_services tests.test_config_and_facades tests.test_upx_pack -q
```

以上是当前稳定生产门禁。历史全量套件仍有 7 个待校准模块，清单见 `docs/AI_HANDOFF.md`；发布记录必须单独注明，不能写成全量测试通过。

必须覆盖以下登录权限断言：

- 正式卡密成功后有 token，允许自动宝箱和云控。
- `admin` 不请求服务端卡密接口。
- `admin` 每次登录有效期约为 16 小时。
- `admin.cloud_control_allowed == False`。
- `admin.auto_loot_allowed == False`。
- `admin` 功能窗不存在 `loot` 页面和导航按钮。
- `admin` 功能窗默认选中 `settings`。

## 3. 手工登录验收

### Windows 权限

1. 普通权限启动，确认提示提权或退出。
2. 管理员权限启动，确认进入登录页。

### `admin` 测试卡

1. 手工输入 `admin` 登录。
2. 托盘提示显示本地测试授权和到期时间。
3. 连接一个游戏窗口。
4. 确认默认打开“快捷设置”。
5. 确认左侧没有“自动宝箱”。
6. 确认挂机、自动副本、妖楼、任务、杂货、输入和日志页面可打开。
7. 确认不会建立云控连接。

### 正式卡密

1. 使用有效正式卡密登录。
2. 确认“自动宝箱”可见并可进入。
3. 确认登录 token 已注入功能窗但未写入配置文件。
4. 使用失效或解绑异常卡密，确认功能被停止并返回登录页。

## 4. 挂机验收

1. 在快捷设置保存模式、半径、拾取、维修和活力阈值。
2. 重启助手并重新连接同一角色，确认配置恢复。
3. 开启挂机后刷新状态，确认模式和半径与设置一致。
4. 关闭拾取，确认待处理掷骰走放弃；开启拾取时走需求。
5. 无技能挂机必须能通过“关闭挂机”停止。
6. 丸子与有凤互斥，有凤与无技能互斥。
7. 主控同步开关时，副控详细参数保持各自配置。

## 5. 构建验收

```powershell
cmd /c tools\build_gui.bat prod
```

- 若默认 `dist\xajh_helper` 被运行中的游戏/助手锁定（游戏加载了 `dist\...\native\bin` 下的桥接 DLL，删除/改名报 `WinError 5`），改用全新目录：
  `cmd /c tools\build_gui.bat prod v1.0.7` → 输出 `dist\xajh_helper-v1.0.7\`，不触碰被锁定目录。
- 运行 `dist\xajh_helper\xajh_helper.exe`（或换目录后对应 `dist\xajh_helper-v1.0.7\xajh_helper.exe`），不要运行 `build\` 中间产物。
- 确认 `_internal\app\data\build_profile.json` 的渠道为 `prod`。
- 确认生产 profile 不含验证码 key 和明文 license secret。
- 确认 DLL、注入器、伤害读取器和全部 `app/data` 资源存在。
- 确认 `build/generated/upx_pack_report.json` 显示 `upx_test=ok`，且只处理顶层启动器。
- 确认构建日志包含 `frozen runtime: OK`。
- 确认构建输出包含 `UPX_PACK_OK`、`frozen runtime: OK` 和 `launcher_upx=1`。
- 确认 `build/generated/upx_pack_report.json` 中 `upx_test` 为 `ok`，且作用范围仅为顶层启动器。
- 确认分发目录中的桥接 DLL、注入器和伤害读取器与 `native/bin` 对应文件 SHA-256 一致。
- 整包启动、登录、连接、隐藏到托盘和退出各执行一次。
