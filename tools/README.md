# 工具目录

`tools/` 只保留正式构建、数据生成和可复现协议分析工具；GUI 运行时不依赖该目录中的 CLI 脚本。

## 正式构建

- `build_gui.bat`：生成 dev/prod onedir 包。
- `_write_build_profile.py`：生成渠道 profile 和打包密钥模块。
- `_free_dist_lock.py`：构建前清理被占用的旧 `dist`。
- `_pack_upx.py`：使用固定版本与哈希的 UPX 压缩并校验生产启动器。
- `_verify_packaged_bridge.py`：校验打包后的桥接组件。

## 数据生成

- `_build_scene_map.py`
- `_build_item_names.py`
- `_build_instance_names.py`
- `_build_instance_levels.py`
- `build_entity_names_clean.py`
- `build_map_names.py`
- `build_map_names_from_common.py`
- `extract_entity_*.py`
- `extract_map_*.py`
- `find_*map*.py`

生成结果进入 `app/data/`。修改生成逻辑时同时提交数据差异和对应测试。

## 协议分析

- `auto_analyze_packets.py`：`packet.auto_analyze` 的 CLI 入口。
- `pck_inspect.py`：检查资源包结构。
- `read_daobiao.py`：读取数据表并输出结构化结果。

更详细的封包分析说明见 `docs/PACKET_AUTO_ANALYZE.md`。

## 提交规则

- 稳定回归放入 `tests/`。
- 不提交编号试探、一次性探针、临时输出或源文件备份。
- 硬编码本机路径、账号、角色名或 PID 的脚本应删除，不能作为长期结论附件。
- 新工具只有在可重复、有明确输入输出、并已经产生可维护成果时才进入主分支。
- 固定地址必须受 `app/data/client_builds.json` 的精确客户端画像约束。
