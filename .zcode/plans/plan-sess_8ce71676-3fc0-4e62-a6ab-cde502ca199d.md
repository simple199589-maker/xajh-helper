# pxERP-MCP：天思云 ERP 协议直连 MCP 服务（新项目）

新建 `D:\work\python\pxerp-mcp\`（与 game-get 同级，不污染原仓库）。基于已逆向的 RemObjects RO107 协议直连 139.155.250.49:7798，不碰 UI、**不需要密码**：复用已登录客户端的会话 token。

## 认证方案（按你的要求调整）

不逆向登录/密码。会话 token = 消息头里那个 16 字节 ClientId GUID（`0f484de3-...`，已从现有抓包拿到）。阶段0 先做**会话复用实验**：最小 RO 客户端原型带此 token 新开 TCP 连接调 `GetServerTime`，验证服务端是否接受第二个连接共享会话：
- 接受 → MCP 配置只需 token（可自动从客户端流量或本地文件提取），开发绕过整个认证环节
- 拒绝/踢掉桌面客户端 → 记录实际行为后再定策略（如 MCP 独占会话）

## MCP 工具清单（最终交付）

**基础资料（只读）**：`get_partners` 往来单位、`get_warehouses` 仓库、`get_goods` 商品（按名称/分类）、`get_goods_classes` 商品分类、`get_operators` 经办人
**查询（只读）**：`query_bills` 单据查询（日期/类型/编号/往来单位过滤）
**录单（写，默认 dry_run）**：`create_purchase_bill` 采购入库单、`create_sales_bill` 销售出库单、`create_expense_bill` 一般费用单

## 项目结构

```
pxerp-mcp/
├── README.md              # 含 MCP 接入配置说明（通用 stdio，Trae/Claude 通用）
├── requirements.txt       # 仅 mcp SDK，其余纯标准库
├── config.example.json    # server/port/token（真实 config.json 加 .gitignore）
├── research/              # 迁入 game-get/tmp_capture 全部成果（抓包工具+文档+pcap）
├── pxerp/
│   ├── ro/framing.py      # 帧编解码：[type][id][len][body]、RO107、ACK、ping（格式已验证）
│   ├── ro/message.py      # 消息体构造/解析：header+GUID+接口+方法+参数区
│   ├── ro/types.py        # 参数编解码：str/int/double/bool/TDateTime/表
│   ├── ro/da.py           # Data Abstract 表编解码（字段元数据+数据行）
│   ├── client.py          # ROClient：connect(带token)/call/心跳/重连
│   ├── api/base_data.py   # 五个基础资料查询
│   ├── api/bill_query.py  # BillSearch 封装
│   ├── api/bills.py       # 三类单据：取号→构造主表+明细表→SaveBill/PostBill
│   └── server.py          # FastMCP stdio 服务
└── tests/                 # 回放测试：用实抓 pcap 做 golden 数据
```

## 实施阶段

**阶段0a：会话复用实验**（上面已述，先做，决定认证路线）
**阶段0b：补充抓包（需要你配合操作客户端）** —— 每场景一份 pcap + rpc_log 存 research/captures/：
1. **基础资料**：依次打开 往来单位/仓库/商品/商品分类/经办人 选择界面 → 抓 lookup 调用和返回表结构
2. **三类单据各录一张测试单并保存**：采购入库单、销售出库单、一般费用单 → 抓 NewBill/PrevBillId/GetBillData/SaveBill/PostBill 全链路（写接口的 golden 数据）
3. **单据查询**：换几个条件/翻页 → 补全 pxQueryParam 字段语义

**阶段1：RO107 协议核心**（framing/message/types/da）+ 回放测试（解码器 vs 库对拍）
**阶段2：会话与业务层**（client + api），每个函数对拍对应 golden 抓包
**阶段3：MCP 服务**（server.py + 配置 + 日志 + dry_run）

**阶段4：验证与安全机制**（写操作三级验证）：
1. **dry-run 字节对拍**：MCP 构造的 SaveBill 请求 vs 客户端实抓请求逐字节比对
2. 生产录测试单（真实保存）→ 核对
3. 保存后用 query_bills 回读核对金额/数量/往来单位
所有写工具默认 `dry_run=True`，需显式确认才真正发送。

## 风险与对策
- 会话 token 可能绑定 TCP 连接或单会话独占 → 阶段0a 实验先行，失败再定策略
- 协议细节偏差致坏单 → 字节对拍 + 回读核对 + 测试单先行；费用单 billtype 码（Purchase/SALE 已确认，费用单待抓包确认）
- token 失效/客户端退出 → MCP 检测到会话失效时报清晰错误，提示重新抓取/刷新 token