# 云控客户端协议（game-get）

> 服务端按本协议做房间转发；客户端按此实现（含签名与多开隔离）。

## 模型

- **主控**：开启云控 + 填写「主控名称」→ 只发不收业务指令
- **副控**：填写同一「主控名称」→ 只收并执行
- **鉴权**：`Authorization: Bearer <login-token>`；token 由 `/api/license/verify` 登录成功后返回
- **签名**：每次请求用登录 token 作 HMAC 密钥；**无 token 则云控不上线、不发任何 HTTP**
- **通道**：`master_name`（主控名称）；须与 API Key 绑定隔离，避免同名串房
- **事件**：服务端对 `event.action` **不白名单**，同房透传；客户端负责解析执行

房间逻辑建议：

```text
room = hash(login_token.subject) + ":" + normalize(master_name)
```

## 同机多开隔离（客户端行为）

同一台电脑可多开多个功能窗，各自独立角色与云控开关：

| 本窗 | 本机 TaskSyncHub | 云控 |
|------|------------------|------|
| 主控（开云） | 正常 fan-out 给本机「未开云的副控」 | 正常 publish |
| 副控（开云，配置可上线） | **不接收**本机事件（unsubscribe + origin 过滤） | 只收/执行 `origin=cloud` |
| 副控（未开云） | **只收**本机事件 | 不参与云 |
| 无控 | 不收不发 | 不参与云 |

说明：

- 「开云可上线」= 启用 + 主控名称 + URL + 登录 token + 角色 master/slave
- 隔离按本窗配置判定，不依赖当前 online
- 主控始终可同时打本机未开云副控 + 云端

## Base URL

默认复用打码服务根地址（设置页 URL / `XAJH_CAPTCHA_BASE_URL`）。

路径前缀：`/api/cloud-control`

## 接口

鉴权头（**全部接口必填签名**）：

```http
Authorization: Bearer <login-token>
Content-Type: application/json
X-Timestamp: <unix-seconds>
X-Nonce: <random-8-128-ascii>
X-Signature: <hmac-sha256-hex>
```

签名密钥为本次登录返回的 `login-token`：

```text
string_to_sign = METHOD + "\n" + PATH + "\n" + timestamp + "\n" + nonce + "\n" + sha256_hex(raw_body)
signature = HMAC_SHA256(login-token, string_to_sign)  # lowercase hex
```

- POST：`PATH` = `/api/cloud-control/join|leave|heartbeat|publish`，`raw_body` = 请求 JSON 原文字节  
- GET poll：`PATH` = `/api/cloud-control/poll?<query>`（与实际 URL query 完全一致），`raw_body` 为空  
- 客户端复用 `build_token_signature_headers`，token 仅保存在内存会话中
- 客户端无 token：**不启动 worker**，状态「登录令牌不可用，请重新登录」

### POST `/api/cloud-control/join`

```json
{
  "v": 1,
  "master_name": "我的主号",
  "role": "master|slave",
  "client_id": "pid-12345",
  "pid": 12345
}
```

响应：

```json
{ "ok": true, "session_id": "...", "cursor": "0" }
```

### POST `/api/cloud-control/leave`

```json
{
  "v": 1,
  "master_name": "我的主号",
  "role": "master|slave",
  "client_id": "pid-12345",
  "session_id": "...",
  "pid": 12345
}
```

### POST `/api/cloud-control/heartbeat`

主控保活（约 20s）。体同 leave 字段。

### POST `/api/cloud-control/publish`（仅主控）

```json
{
  "v": 1,
  "type": "task_sync",
  "master_name": "我的主号",
  "client_id": "pid-12345",
  "session_id": "...",
  "msg_id": "12345-accept-10021-1710000000000",
  "event": {
    "action": "accept|complete|path|claim_activity|map_fly|…",
    "task_id": 10021,
    "source_pid": 12345,
    "ts": 1710000000.0,
    "can_finish": null,
    "name": "任务名",
    "origin": "cloud",
    "portal_kind": "",
    "points": null,
    "msg_id": "12345-accept-10021-1710000000000"
  }
}
```

服务端：把 `event` 推给 **同一 room 下 role=slave** 的连接；不要回推给发布者。

响应：

```json
{ "ok": true, "delivered": 3 }
```

### GET `/api/cloud-control/poll`（副控长轮询）

Query：

- `master_name`
- `client_id`
- `session_id`（可选）
- `cursor`（可选）
- `wait_s`（1–60，客户端默认 25）

响应：

```json
{
  "ok": true,
  "cursor": "1",
  "events": [
    {
      "msg_id": "...",
      "event": { "...": "同 publish.event" }
    }
  ]
}
```

无消息时：阻塞到 `wait_s` 后返回空 `events`（或 200 + 空列表）。

## 事件动作

> **服务端不对 `action` 维护业务白名单**：校验鉴权/房间后 **透传广播** `event` 给同房 slave。  
> 具体动作语义由客户端约定并执行；未知 action 副控侧忽略即可。

| action | 含义 | 字段约定 |
|--------|------|----------|
| `accept` | 接任务 | `task_id` 必需 |
| `complete` | 交任务 | `task_id` 必需；可带 `can_finish` |
| `path` | 寻路 | `task_id` 必需；可带 `portal_kind` |
| `claim_activity` | 领活跃宝箱 | `task_id` 可为 0；可带 `points` |
| `map_fly` | 地图飞行（福州/师门/死亡点） | `name` = `fuzhou` / `shimen` / `death`；`task_id` = 槽位提示（slot+1，客户端用） |

客户端本机 hub 与云控共用同一 `VALID_ACTIONS` 集合；扩展新动作时：主控 publish + 副控执行分支 +（可选）本机 hub 即可，**无需改服务端 action 列表**。

## 安全建议

1. 必须校验 `Authorization: Bearer <login-token>`，房间按 token subject 隔离
2. 必须校验 HMAC 签名（密钥为 login token）
3. 仅 `role=master` 的 session 可 `publish`  
4. 副控不可 publish（客户端也不发）  
5. `msg_id` 去重（建议服务端按 room 保留 30–60s）  
6. 限流：每 key 发布频率、每连接 poll 并发

## 客户端设置对应

| UI | settings key |
|----|----------------|
| 启用云控 | `cloud_control_enabled` |
| 主控名称 | `cloud_control_master_name` |
| 登录令牌 | `login_token`（来自内存中的 AuthService session，不落盘） |
| URL | `captcha_base_url` |
| 角色 | `task_control_role` = master/slave/none |

## 服务端实现位置

已实现于业务识别服务仓库：

- 模块：`cnn_for_captcha-main/cloud_control.py`
- 挂载：`sameobject_api.py` → `/api/cloud-control/*`
- 鉴权：Bearer login token + token HMAC
- 测试：`python -m unittest test_cloud_control -v`

部署后随 `sameobject_api` 同端口对外（如 `https://xiaoao.joini.cloud`）。
