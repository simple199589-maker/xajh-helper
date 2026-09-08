# TeShareCloud pxClient 通信协议研究

> 研究对象：天思 TeShare 云 ERP 桌面客户端 `D:\software\TeShareCloud\Client\pxClient.exe`（Delphi + RemObjects SDK，PID 19968 观测时实例）
> 研究方法：Npcap 被动抓包（只读，未注入/未修改任何流量），抓包于 2026-08-30
> 本目录自包含（脚本 + 文档 + 数据），可整体拷贝到其他项目继续用

## 一、总体结论

| 项 | 结论 |
|---|---|
| 是否 HTTP/AJAX | **不是**。pxClient 不走 HTTP、不查系统代理，Fiddler 抓不到 |
| 传输 | 与 `139.155.250.49` 的两条 TCP 长连接：**7798 = RPC 调用**，**7796 = 服务端推送** |
| 7798 协议 | RemObjects SDK BinMessage（魔数 `RO107`），二进制帧 + 明文方法名 |
| 7796 协议 | `[81 7E][2字节BE长度][明文JSON]` 的推送帧 |
| 加密 | **无加密**。字符串/方法名/GBK中文全明文；大数据包仅 zlib 压缩（可逆） |
| 登录配置 | `Client\Config\pxClient.xml`（Server=139.155.250.49, Port=7798, UserId=374233） |
| 客户端技术栈 | Delphi，`pxRODA.bpl`=RemObjects Data Abstract，Indy 网络库 |

## 二、7798 RPC 协议规格（已验证）

### 帧格式（TCP 流上）

```
数据消息:  [type:1][msg_id:4 LE][body_len:4 LE][body ...]
ACK 帧:   [0x02][msg_id:4 LE]                        ← 5字节无长度字段，客户端每处理完一条服务端消息回一个
ping/pong: [0x04 或 0x05][msg_id:4 LE]               ← SuperTCP 通道保活
```

- `type=0x01` 是通用数据消息（请求、响应都用它，靠方法名是否带 `Response` 后缀区分）
- `msg_id` 每连接从 0x41（'A'）开始递增；响应回带相同 id
- 所有数据消息 body 以 **5 字节魔数 `RO107`** 开头（解析时用它定位/校验对齐）
- body 长度不含 9 字节帧头

### 消息体结构

```
"RO107"                              魔数
[压缩标志:1]                          0x00=明文, 0x01=zlib
[固定前缀若干字节]                    观测为 00 00 00 00 00 00 00
[客户端会话GUID:16字节]               全程不变（0f484de3e47e9545ba3948b918b0ce95）
[接口名: u32LE长度前缀字符串]          如 pxSystemService
[方法名: u32LE长度前缀字符串]          如 GetServerTime / GetServerTimeResponse
[参数区（二进制）]
```

压缩标志=1 时，zlib 流（`78 9C`）从 GUID 之后开始，解压后才是接口名/方法名/数据。

### 参数区已验证的编码

| 类型 | 编码 |
|---|---|
| 字符串 | u32LE 长度前缀 + 内容（GBK/UTF-8 均观测到，中文明文） |
| TDateTime | 8 字节 double 小端（OLE 日期，纪元 1899-12-30）。`GetServerTimeResponse` 返回 `8feed2f11d97e640` → 45502.68 → 2026-08-30 22:2x ✓ |
| 整数 | 小端（int32/int64） |
| 表/对象 | Data Abstract 风格：字段元数据（`Alignment/taLeftJustify, DataType/datLargeInt/datDateTime, DisplayLabel, BlobType...`）+ 数据行；业务对象如 `pxQueryParam`、`GoodsBaseInfo` 按名字+字段序列化 |

## 三、7796 推送协议规格（已验证）

```
[81 7E][长度:2字节BE][明文JSON]      长度 = JSON 字节数
```

单据保存通知（type:1）：

```json
{"sender":"{A54206C5-B83F-45AB-A4DE-9097D7E67CE4}","type":1,"notify_type":"save",
 "profile":"贵阳七星关区新星商行","bill_id":883671,"bill_code":"XSCKD-2026-08-30-2249",
 "billtype_code":"SALE","billtype_name":"销售出库单"}
```

往来账户变动通知（type:2）：

```json
{"sender":"{...}","type":2,"profile":"...","owner":{"id":380364,"code":"","name":"王健",
 "maker_id":0,"receive":2775,"pay":0,"remain":2775,"class_id":440239}}
```

同一通知会重复推送两次；多帧可以粘在一个 TCP 段里（按长度头切分）。

## 四、RPC API 面

服务清单（从 `pxFWCore.bpl` 字符串提取，数字为出现次数，≈调用点数量）：

```
pxBillService(248)  pxBaseInfoService(266)  pxSystemService(188)  pxGoodsService(158)
pxRetailService(80) pxQueryService(71)      pxFinanceService(56)  pxLookupService(53)
pxManufactureService(58)  pxPromotionService(47)  pxSMSService(67)  pxMessageService(23)
pxBusinessService(19)  pxAliService(29)  pxFrameworkService(74)  pxFWService(2)
```

方法命名规律：`Get*/Save*/Del*/Check*/Audit*/ExecuteQuery/PrevBillId...`，代理生成 `Xxx`/`XxxResponse` 成对出现。

## 五、实测调用链（用户打开"采购入库单"的完整过程，8分钟抓包）

| # | 调用 | 参数摘要 | 响应 |
|---|---|---|---|
| 1 | `pxSystemService.GetServerTime` | 无 | TDateTime（心跳，约每分钟1次） |
| 2 | `pxSystemService.GetQueryParamObject` ×2 | — | pxQueryParam 模板 |
| 3 | `pxQueryService.ExecuteQuery` | `"BillSearch"`, pxQueryParam{A.BILLDATE: '2026-08-01'~'2026-08-31'} | 单据搜索结果（6KB, zlib） |
| 4 | `pxBillService.PrevBillId` | id, `"Purchase"` | 上一张采购单 id |
| 5 | `pxBillService.GetBillData` | id, 表清单 `BILL/BILLDETAIL/BillExtra/SEQUENCEDETAIL/BILLPAYMENT` | 五张表数据（5.8KB, zlib） |
| 6 | `pxBillService.GetBillGoodsInfo` | id | GoodsBaseInfo[]，商品名明文（长白萝卜、云南油麦菜…） |
| 7 | `pxBaseInfoService.GetData` | `"GOODSCLASS"`, TableRequestInfo | 商品分类表（zlib） |
| 8 | `pxLookupService.GetBaseGoodsByClassId` / `GetBaseGoodsByName` | 分类id / 名称 | 商品列表（zlib） |

## 六、工具链用法

环境要求：Windows + 已装 Npcap（本机 `C:\Windows\System32\Npcap` 有），Python 3 无第三方依赖。

```bash
# 1) 被动抓包 N 秒（自动处理入方向 802.1Q VLAN 帧），输出 <目录>/pxclient.pcap + streams/
python capture_pxclient.py 60 139.155.250.49 out1

# 2) 解码 RPC → 可读调用日志（方向/id/大小/是否zlib/服务.方法/参数摘要）
python decode_ro.py out1/pxclient.pcap > rpc_log.txt

# 3) Wireshark 存的 .pcapng 先转经典 pcap 再解
python pcapng2pcap.py xxx.pcapng xxx.pcap

# 4) 逐包明细（排错用）
python dump_pcap.py out1/pxclient.pcap
```

典型工作流：起 60 秒抓包 → 在客户端里做一个操作 → `decode_ro.py` 看调用了什么。

## 七、文件清单

| 文件 | 说明 |
|---|---|
| `capture_pxclient.py` | Npcap 抓包器（ctypes 直调 wpcap.dll，含 VLAN 剥离、TCP重组、流导出） |
| `decode_ro.py` | RO107 解码器：pcap→TCP重组→消息帧→zlib解压→参数字符串提取 |
| `pcapng2pcap.py` | pcapng→pcap 转换 |
| `dump_pcap.py` | 逐包 hex/文本 dump |
| `run1/` | 第一次抓包（75s，纯心跳）：pxclient.pcap + streams/ |
| `run2/` | 8分钟抓包（含完整业务操作）：pxclient.pcap、rpc_log.txt（**主要成果**）、dump.txt、capture_log.txt、streams/ |
| `ook.pcap` | 用户 Wireshark 抓包的转换版（原件 `captures/tcp/ook.pcapng`，内容为 GetBaseGoodsByName 一轮） |

## 八、已知边界 / 待深入

1. 消息体固定前缀（魔数与 GUID 之间的 7 字节）的逐字段含义未完全定死（压缩标志已确定，其余观测恒为 0）。
2. 参数区只做了字符串/日期/整数级还原；`pxQueryParam`、Data Abstract 表结构的完整字段级解析待写（编码格式已明确，工作量问题）。
3. 7796 推送的 JSON 中 `type` 字段的完整取值集合未枚举完（观测到 1=单据保存、2=账户变动）。
4. 未覆盖登录/认证包（抓包时连接已建立）；需要时重启客户端再抓即可。
5. 抓包需管理员权限（Npcap）；嗅探为混杂模式关闭的被动模式，不影响目标进程。
