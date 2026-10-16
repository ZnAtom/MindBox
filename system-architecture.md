# 智能储物柜：供 AI 读取的架构源文件

版本：0.1；日期：2026-10-16；状态：设计快照，未实现、未完成整机验证。
配套文件：system-assessment.md 为可行性证据；system-architecture.json 为固定节点及连接清单。

本文件保留硬件架构快照，软件实际状态统一见[开发入口](code/README.md)。当前采集目标已细化为K230上传完整音视频；本快照中的关键照片连接不表示完整视频上传已经实现。

2026-10-16 最新范围决定：用户明确暂不做重量变化。本文件下方的称重连线、稳定重量状态和差重约定仅为历史架构，不用于当前实现或验收；当前操作通过视频/照片及开合状态关联。数据库实际设计见[数据与调用约定](code/doc/data-contract.md)。

## 读取规则

1. 以文字、节点 ID 和边定义为准，不依赖图片 OCR。箭头标签指定数据内容和方向。
2. implemented=false 表示没有实现证据，不代表已经确认设备不支持。
3. 两个称重传感器测整个柜体，不是两个抽屉独立称重；门磁只提供门状态。
4. ESP 是语音终端，不运行通用 ASR/LLM/TTS；这些服务在 SERVER 或其调用的云服务内。
5. SERVER 可以远离柜体，但完整业务需要 SERVER 在线；BROWSER 不是服务器进程。
6. K230 与 ESP 不直接互传图像；SERVER 关联两者数据。
7. 小智 voice_session_id 与柜体 operation_id 是两个不同概念，不能自动视为相同。
8. 两个完整桥传感器加两个 HX711 是条件性接线，实物型号与数量仍待确认。
9. 连接是设计要求，不是现有小智、K230 固件默认全部支持的声明。
10. 此次没有授权或执行修复；所有 unresolved 项保持待用户决定。

## A. 完整数据框图源码

ROUTER 是传输基础设施，为避免把路由器误认为业务处理节点，下图协议箭头直接连接应用端点；JSON 记录所有无线链路经 ROUTER。

```mermaid
flowchart TB
    USER["USER 用户"]
    subgraph CABINET["柜体"]
        MIC["MIC 麦克风"]
        AMP["AMP 音频功放"]
        SPK["SPK 扬声器"]
        D1["D1 抽屉1门磁"]
        D2["D2 抽屉2门磁"]
        D3["D3 抽屉3门磁"]
        L["L 左称重传感器"]
        R["R 右称重传感器"]
        H1["H1 HX711左，条件性"]
        H2["H2 HX711右，条件性"]
        ESP["ESP ESP32-S3小智：本地唤醒、音频编解码、采样"]
        CAM["CAM 配套摄像头"]
        K["K K230：采集、缓存、压缩"]
    end
    subgraph SERVER["SERVER 独立服务器部署边界"]
        GW["GW 设备接入与鉴权"]
        ASR["ASR 音频转文字"]
        INTENT["INTENT 意图与参数解析"]
        SESSION["SESSION 操作会话、抽屉归属、稳定重量差"]
        SELECT["SELECT 关键帧和上下文整理"]
        APP["APP 校验、追问、统一执行服务"]
        DB["DB 主数据库"]
        FILES["FILES 照片存储"]
        SCHED["SCHED 持久化提醒调度"]
        TTS["TTS 语音合成及Opus输出"]
        WEB["WEB 网页服务"]
        MAIL["MAIL 邮件执行器"]
    end
    AI["AI 云端多模态与复杂语言理解"]
    SYNC["SYNC 可选云端副本，不是第二个独立主库"]
    EMAIL["EMAIL 邮件服务"]
    BROWSER["BROWSER 手机或电脑浏览器"]
    USER -->|"语音"| MIC -->|"音频输入"| ESP
    ESP -->|"播放数据"| AMP --> SPK -->|"声音"| USER
    D1 -->|"开关电平"| ESP
    D2 -->|"开关电平"| ESP
    D3 -->|"开关电平"| ESP
    L -->|"桥式模拟信号"| H1 -->|"数字采样"| ESP
    R -->|"桥式模拟信号"| H2 -->|"数字采样"| ESP
    CAM -->|"图像"| K
    ESP -->|"F01 Opus上行及语音状态，WSS"| GW
    GW -->|"F02 Opus下行及播放控制，WSS"| ESP
    ESP -->|"F03 门磁、重量、事件；传感器扩展未实现"| GW
    GW -->|"F04 提醒、确认、采样命令；空闲通道未验证"| ESP
    GW -->|"F05 操作编号与采集请求；现有Socket格式待查"| K
    K -->|"F06 照片、拍摄时间、帧编号、请求关联"| GW
    GW -->|"F07 接收音频"| ASR -->|"F08 文字与置信信息"| INTENT
    GW -->|"F09 传感器和图像元数据"| SESSION
    GW -->|"F10 照片写入"| FILES
    SESSION -->|"F11 操作上下文"| SELECT
    FILES -->|"F12 照片读取"| SELECT
    DB -->|"F13 相关库存"| SELECT
    SELECT -->|"F14 HTTPS：关键照片、重量差、上下文"| AI
    INTENT -->|"F15 必要时解析复杂指令"| AI
    AI -->|"F16 结构化候选结果，不直接写库"| APP
    INTENT -->|"F17 语音请求"| APP
    APP -->|"F18 校验后的事务写入"| DB
    DB -->|"F19 查询及版本信息"| APP
    APP -->|"F20 确认或追问文本"| TTS
    TTS -->|"F21 Opus与播放状态"| GW
    DB -->|"F22 已持久化提醒"| SCHED
    SCHED -->|"F23 到点播报文本"| TTS
    SCHED -->|"F24 网页提醒"| WEB
    SCHED -->|"F25 邮件任务"| MAIL -->|"F26 邮件服务接口"| EMAIL
    BROWSER -->|"F27 HTTPS：查询、修改请求"| WEB
    WEB -->|"F28 HTTPS/WebSocket：页面、状态、提醒"| BROWSER
    WEB -->|"F29 用户请求"| APP
    APP -->|"F30 查询及操作结果"| WEB
    DB -->|"F31 可选后台同步，冲突规则待定义"| SYNC
```

## B. 完整硬件框图源码

箭头中的 5 V/GND、3.3 V/GND 均表示供电正线和对应回路，不是只接正极。所有柜体非隔离电路共地；服务器和柜体通过 Wi-Fi 通信，无需另拉地线。

```mermaid
flowchart TB
    BAT["BAT 标称12 V电池：类型、容量、满充电压待定"]
    CHARGER["CHARGER 电池匹配充电器：连接方式按电池规格，待定"]
    PROTECT["PROTECT 匹配的电池保护"]
    FUSE["FUSE 靠近电池的保险丝：额定值待定"]
    SWITCH["SWITCH 总开关"]
    BUCK["BUCK 稳压5 V降压：此前5 A建议尚未验收"]
    BUS["BUS 5 V与GND配电端子"]
    ESP["ESP 小智板：指定5 V输入，具体板型待定"]
    REG["REG 小智板3.3 V输出：余量待核对"]
    K["K K230指定5 V输入"]
    CAM["CAM K230配套相机"]
    LIGHT["LIGHT 可选5 V补光"]
    MIC["MIC 小智配套麦克风电路"]
    AMP["AMP 小智配套功放：实际供电来源待板图确认"]
    SPK["SPK 匹配阻抗和功率的扬声器"]
    D1["D1 无源门磁1：GPIO_D1与GND"]
    D2["D2 无源门磁2：GPIO_D2与GND"]
    D3["D3 无源门磁3：GPIO_D3与GND"]
    L["L 左完整桥传感器：条件假设"]
    R["R 右完整桥传感器：条件假设"]
    H1["H1 HX711左：3.3 V兼容待确认"]
    H2["H2 HX711右：3.3 V兼容待确认"]
    ROUTER["ROUTER 2.4 GHz路由器"]
    SERVER["SERVER 服务器主机，与柜体分开放置"]
    SERVER_PSU["SERVER_PSU 主机自己的电源"]
    ROUTER_PSU["ROUTER_PSU 路由器自己的电源"]
    CLIENT["CLIENT 手机或电脑"]
    INTERNET["INTERNET 互联网到云AI；服务器不在局域网时也依赖此链路"]
    CHARGER -->|"P00 匹配的充电接口，未定型"| BAT
    BAT -->|"P01 电池电压，含回路"| PROTECT
    PROTECT -->|"P02 保护输出"| FUSE -->|"P03"| SWITCH
    SWITCH -->|"P04 实际电池电压"| BUCK
    BUCK -->|"P05 5 V/GND"| BUS
    BUS -->|"P06 独立5 V/GND支路"| ESP
    BUS -->|"P07 独立5 V/GND支路"| K
    BUS -->|"P08 可选5 V/GND支路"| LIGHT
    ESP -->|"P09 板载稳压"| REG
    REG -->|"P10 3.3 V/GND，条件性"| H1
    REG -->|"P11 3.3 V/GND，条件性"| H2
    ESP -->|"P12 板上供电路径待核对，不直接接裸扬声器"| AMP
    ESP -->|"P13 板上麦克风供电待核对"| MIC
    MIC -->|"H01 I2S/PDM/Codec，按板卡"| ESP
    ESP -->|"H02 I2S/Codec，按板卡"| AMP
    AMP -->|"H03 扬声器输出线对，按功放；不假设负端接地"| SPK
    D1 -->|"H04 GPIO_D1与GND；输入上拉"| ESP
    D2 -->|"H05 GPIO_D2与GND；输入上拉"| ESP
    D3 -->|"H06 GPIO_D3与GND；输入上拉"| ESP
    H1 -->|"H07 E+/E-桥激励，实际电压待定"| L
    L -->|"H08 A+/A-差分信号"| H1
    H2 -->|"H09 E+/E-桥激励，实际电压待定"| R
    R -->|"H10 A+/A-差分信号"| H2
    H1 -->|"H11 DOUT到GPIO_H1_DOUT"| ESP
    ESP -->|"H12 GPIO_H1_SCK到PD_SCK"| H1
    H2 -->|"H13 DOUT到GPIO_H2_DOUT"| ESP
    ESP -->|"H14 GPIO_H2_SCK到PD_SCK"| H2
    CAM -->|"H15 配套CSI排线；电源/控制按接口"| K
    ESP -.->|"N01 Wi-Fi双向"| ROUTER
    K -.->|"N02 Wi-Fi双向"| ROUTER
    ROUTER <-->|"N03 局域网部署：网线或Wi-Fi"| SERVER
    ROUTER <-->|"N04 上网"| INTERNET
    CLIENT <-->|"N05 浏览器经网络访问"| SERVER
    SERVER_PSU -->|"P14 独立供电"| SERVER
    ROUTER_PSU -->|"P15 独立供电"| ROUTER
```

不提供实际 GPIO 编号；不能把 GPIO_D1 等符号当成板上丝印。实际型号确定前，上图不能直接作为施工接线表。三线半桥、单 HX711 或分离 VCC/VDD 模块均须另行确认，当前不修改方案。

## C. 操作处理时序源码

```mermaid
sequenceDiagram
    participant ESP as ESP
    participant K as K
    participant GW as GW
    participant SESSION as SESSION
    participant AI as AI
    participant APP as APP
    participant DB as DB
    Note over K: 采集与短时缓存是待验证要求
    ESP->>GW: 门打开事件，device_id/boot_id/seq/operation_id
    GW->>SESSION: 建立操作，保存操作前稳定重量
    GW->>K: 采集请求，operation_id/request_id
    K->>GW: 操作中照片，frame_id/captured_at/operation_id
    ESP->>GW: 所有门关闭及稳定重量
    GW->>SESSION: 计算delta=after-before，关联证据
    SESSION->>AI: 通过关键帧整理服务调用：照片、delta、相关库存
    AI->>APP: 候选物品、动作、日期来源、不确定项
    alt 信息不足
        APP->>GW: 通过TTS服务追问，关联operation_id
        GW->>ESP: 播放追问，等待语音答复
        Note over APP,DB: 未确认前保留pending，不提交确定库存变化
    else 信息足够或用户已确认
        APP->>DB: 按幂等键执行事务，写库存和审计记录
        DB->>APP: 已提交，返回记录版本
        APP->>GW: 通过TTS服务确认
        GW->>ESP: 播报已记录
    end
```

## D. 机器读取的数据约定

以下为建议业务约定，不是小智原生协议；传感器扩展如何承载尚未决定。operation_id 可由设备本地产生并经服务器验证；断网采集需保留本地事件顺序。voice_session_id 只标识一次语音连接／会话，可关联零个或多个 operation_id。

```json
{
  "schema_version": "proposed-0.1",
  "type": "cabinet.sensor_event",
  "device_id": "cabinet-01",
  "boot_id": "example-boot-uuid",
  "seq": 104,
  "event_id": "example-event-uuid",
  "operation_id": "example-operation-uuid",
  "voice_session_id": null,
  "device_monotonic_ms": 83000,
  "captured_at_utc": null,
  "clock_quality": "unsynchronized",
  "drawer_states": {"1": "closed", "2": "closed", "3": "closed"},
  "weight": {
    "unit": "g",
    "scope": "whole_cabinet",
    "left_g": 8100.0,
    "right_g": 7900.0,
    "total_g": 16000.0,
    "stable": true,
    "calibration_id": "example-calibration-v1"
  }
}
```

上例重量只是消息样例，不是实际设备读数。left_g/right_g 仅适用于两个完整桥独立采集假设。未同步设备时不能靠不同设备的 monotonic_ms 直接对齐照片，必须使用操作编号、请求关联及服务器接收时间，并记录时间不确定性。

```text
operation_state:
  IDLE -> CAPTURING -> WAITING_STABLE -> ANALYZING
  ANALYZING -> PENDING_CONFIRMATION | READY_TO_COMMIT | FAILED
  PENDING_CONFIRMATION -> READY_TO_COMMIT | CANCELLED
  READY_TO_COMMIT -> COMMITTED

invariants:
  - delta_g = closed_stable_after_g - closed_stable_before_g
  - positive_delta_is_evidence_for_addition_not_proof_of_item_identity
  - zero_delta_does_not_prove_no_item_change
  - weight_scope_always_whole_cabinet
  - AI_output_is_candidate_and_never_direct_database_write
  - repeated_event_id_does_not_create_repeated_inventory_changes
  - one_operation_may_contain_multiple_drawers_or_items_and_need_confirmation
  - unsynchronized_timestamps_are_not_equal_to_accurate_wall_clock_time
  - expiry_date_source_must_be_one_of_user_label_ai_estimate_unknown
  - reminder_sent_is_not_equal_to_reminder_played
  - server_offline_does_not_imply_full_offline_voice_or_reminders
```

## E. 当前未解决事项

- U01：小智具体开发板、PSRAM、固件版本、麦克风／功放、电源路径和空闲引脚未知。
- U02：传感器型号／线数、HX711 数量及模块电路未知。
- U03：电池、降压模块和电源线规格未知。
- U04：空闲主动提醒通道生命周期未定义，语音会话通道不一定常驻。
- U05：传感器主动事件上报、断线缓存、幂等及恢复尚无实现证据。
- U06：K230 现有 Socket 协议、缓存可用性、远程 TLS 支持未知。
- U07：服务器、ASR/TTS/模型供应商、地址及实际服务健康未知。
- U08：整柜机械支撑、有效称重分辨率、视野和实际识别成功率未测。
- U09：邮件服务配置与浏览器离线通知能力未定义。
- U10：所有目标延迟、吞吐和功耗预算均待实测，不得升级为实测结论。
