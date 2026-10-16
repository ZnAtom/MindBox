# 后端接口约定 v0.1

日期：2026-10-16。状态：拟实施接口，不是已运行服务，也不是现有小智原生协议。后续以实现生成的 OpenAPI 和版本化消息 schema 为准。

## 1. 通用约定

- HTTP 前缀 `/api/v1`。设备和管理端使用不同 bearer token；身份及柜体归属从鉴权结果读取。
- JSON 默认 UTF-8，时间带时区，未知值 `null`。列表使用 cursor/limit，默认 50、最大 200。
- 修改类管理接口携带 `Idempotency-Key`。相同 key 不同 body 返回 409；库存修改另带 `expected_version`。
- 设备事件、帧和回执使用消息自身唯一身份去重，不要求额外幂等头。
- 异步工作返回 202 和资源 ID，接收不等于完成。错误示例：`{"error":{"code":"inventory_version_conflict","message":"库存已变化，请重新获取","retryable":true},"request_id":"..."}`。
- 400 表示无效请求/消息，401/403 为鉴权/权限，404 为资源不存在，409 为幂等或版本冲突，413 为上传过大，422 为字段结构不符，429 为限流，503 为暂不可用。重试建议通过 `Retry-After` 返回。

## 2. 接口清单

| 方法与路径 | 调用方 | 行为 |
| --- | --- | --- |
| `GET /health/live` | 运维 | 进程存活；不暴露密钥 |
| `GET /health/ready` | 运维 | 存储可写等核心条件；详细依赖状态需管理权限 |
| `POST /api/v1/devices/heartbeat` | 设备 | 固件版本、能力和状态；由服务端更新 last_seen |
| `POST /api/v1/device-events` | 传感器设备 | 持久化单个事件及后续任务 |
| `POST /api/v1/observations` | 相机设备 | multipart 上传一张照片和 metadata |
| `POST /api/v1/capture-requests/{id}/complete` | 相机设备 | 采集结束，提交本次帧身份清单 |
| `GET /api/v1/operations/{id}` | 管理端/所属设备 | 操作状态、收帧情况、证据质量和失败原因 |
| `GET /api/v1/items` | 管理端 | 按位置、类别、状态检索库存；带柜体 revision |
| `GET /api/v1/items/{id}` | 管理端 | 物品、字段来源、证据和 version |
| `PATCH /api/v1/items/{id}` | 管理端 | 主动纠正已存在物品，经过统一库存服务 |
| `GET /api/v1/observations/{id}/image` | 管理端/授权服务 | 授权读取照片，禁止任意本地路径 |
| `POST /api/v1/commands` | 管理端/语音桥 | 文本查询、修改或提醒请求，返回异步指令资源 |
| `GET /api/v1/commands/{id}` | 管理端/语音桥 | 读取处理结果、回复文本或待澄清内容 |
| `POST /api/v1/reminders` | 管理端 | 直接建立明确时间的一次性提醒 |
| `GET /api/v1/reminders` | 管理端 | 查看提醒及各渠道投递状态 |
| `POST /api/v1/reminders/{id}/cancel` | 管理端 | 取消提醒，返回是否已触发/播放 |
| `POST /api/v1/deliveries/{id}/receipts` | 设备/管理端 | 上报接收、播放完成或播放失败 |
| `GET /api/v1/devices` | 管理端 | 在线状态、能力、固件及最后事件时间 |
| `POST /api/v1/realtime-ticket` | 管理端 | 签发短期、一次性浏览器连接 ticket |
| `WS /api/v1/stream` | 浏览器 | 状态变化通知；认证时消费 ticket |

设备 HTTP 接入是可先实现的统一业务入口；小智/K230 的实际 Socket、WebSocket 或网关可以调用同一应用服务。不要让硬件为满足本提案被迫全部更换原有协议。

## 3. 门磁与重量事件

`POST /api/v1/device-events` 示例（ID 使用完整 UUID；以下固定值仅供协议示例）：

```json
{
  "schema_version": "1.0",
  "event_id": "11111111-1111-4111-8111-111111111111",
  "device_id": "esp-01",
  "boot_id": "22222222-2222-4222-8222-222222222222",
  "seq": 104,
  "operation_id": "33333333-3333-4333-8333-333333333333",
  "kind": "door.opened",
  "captured_at": null,
  "monotonic_ms": 83000,
  "clock_quality": "unsynchronized",
  "payload": {
    "drawer_states": {"1": "open", "2": "closed", "3": "closed"}
  }
}
```

首版 kind：`door.opened`、`door.closed`、`weight.stable`。服务器依据完整 drawer_states 判断是否所有门关闭。`weight.stable` 的 payload：

```json
{
  "drawer_states": {"1": "closed", "2": "closed", "3": "closed"},
  "weight": {
    "scope": "whole_cabinet",
    "unit": "g",
    "total": 16000.0,
    "left": 8100.0,
    "right": 7900.0,
    "stable": true,
    "sample_window_ms": 1000,
    "calibration_id": "cal-v1"
  }
}
```

数值为消息样例，不是实测。left/right 可空，scope 只能 whole_cabinet；total 必须有限数值，范围由设备配置检验。声称 stable 不代表通过验证：服务器还检查门状态、标定、采样窗口及读数时效。稳定窗口和噪声阈值待实测。

空闲稳定读数允许 `operation_id=null`，用于下次操作的前重量基线。其他操作内事件应携带操作编号；多门期间设备应复用当前 operation_id。设备不支持本地产生编号时由适配网关生成并维护关联，不用时间戳硬凑。

首次接收返回 202：

```json
{
  "event_id": "11111111-1111-4111-8111-111111111111",
  "accepted": true,
  "duplicate": false,
  "operation_id": "33333333-3333-4333-8333-333333333333",
  "operation_state": "capturing"
}
```

完全相同事件重传返回 200 和 `duplicate=true`；相同 event_id 或设备启动序号对应不同内容返回 409。序号有缺口则记录待补数据，不能把接收顺序当成完整动作顺序。

## 4. 采集请求和图片

逻辑采集请求如下，由适配器转为真实设备协议：

```json
{
  "type": "capture.request",
  "request_id": "44444444-4444-4444-8444-444444444444",
  "operation_id": "33333333-3333-4333-8333-333333333333",
  "mode": "operation_keyframes",
  "max_frames": 6
}
```

`max_frames=6` 为初始预算，需与实际 K230 能力、画面质量和模型成本一起调整。超出请求预算的帧不会静默覆盖已存证据。

上传采用 `multipart/form-data`：`file` 为 JPEG，`metadata` 为 JSON 字符串，包含：

```json
{
  "schema_version": "1.0",
  "device_id": "camera-01",
  "boot_id": "55555555-5555-4555-8555-555555555555",
  "frame_id": "frame-000128",
  "request_id": "44444444-4444-4444-8444-444444444444",
  "operation_id": "33333333-3333-4333-8333-333333333333",
  "captured_at": null,
  "monotonic_ms": 92000,
  "clock_quality": "unsynchronized",
  "sha256": "<实际图片内容的 SHA-256>"
}
```

初始限制：每张最大 5 MiB、解码后最多 1600 万像素，只接受能正确解码的 JPEG；均为可配置软件限制。校验实际文件格式、字节数、像素数和哈希，服务端生成存储路径，以临时文件写入后原子移动。相同帧身份不同哈希返回 409，不能覆盖原照片。

完成接口携带 `frame_ids` 和完成原因，服务端核对实际收到的帧，缺帧继续等待到 deadline；先到 complete、后到图片也可正确收尾。设备不支持 complete 时使用配置的超时收帧并标注质量降级。

## 5. 模型候选输出

内部结构示例，不是允许模型直接执行的数据库命令：

```json
{
  "schema_version": "1.0",
  "operation_id": "33333333-3333-4333-8333-333333333333",
  "actions": [
    {
      "kind": "add",
      "existing_item_id": null,
      "name": "苹果",
      "category": "fruit",
      "quantity": 1,
      "unit": "piece",
      "location_id": "drawer-1",
      "evidence_refs": ["observation-id"],
      "expires_on": null,
      "expiry_source": "unknown",
      "review_suggestion": null
    }
  ],
  "uncertainties": [],
  "needs_review": false
}
```

kind 支持 add/remove/update/no_change，remove/update 必须引用当前柜体的真实 item。引用证据须存在于本次输入。`needs_review=false` 也不能跳过程序校验；模型自报置信度不作为实际可靠率。所谓新增必须有可见物品及进出证据，移除必须有取出依据，单纯没拍到不通过。过期日期为 AI 估计时不得写 expires_on，只能生成 review_suggestion。

## 6. 查询、修改和提醒

文本指令：

```json
{
  "text": "三分钟后提醒我吃苹果",
  "source": "web",
  "voice_session_id": null
}
```

首次请求保存 accepted_at，返回 202 和 command_id；处理结果包含 `status, reply_text, facts, reminder_id, needs_clarification`。facts 使用真实 item/reminder 引用。纯查询不修改库存；需要澄清的修改不会先提交。ASR 文字通过语音桥提交同一路径，source 从授权角色校验。

直接创建提醒示例：

```json
{
  "item_id": null,
  "target_text": "吃苹果",
  "message": "记得吃苹果",
  "due_at": "2026-10-16T10:03:00Z",
  "channels": ["web", "voice"]
}
```

该时刻只为格式示例。直接时间接口要求未来时刻；用户口语已过期或不明确的时间应澄清。要求 voice 但设备能力未知时可以建立提醒，响应同时返回该渠道 waiting_device/unsupported 状态；不能承诺已能播放。

物品纠正示例：`{"expected_version":3,"patch":{"name":"青苹果","expires_on":null},"reason":"用户纠正"}`。只允许白名单业务字段；用户不能写系统时间、审计或柜体 revision。清空日期与省略字段含义不同。

## 7. 状态通知与播放回执

浏览器通知：

```json
{
  "type": "inventory.changed",
  "notification_id": "66666666-6666-4666-8666-666666666666",
  "cabinet_id": "cabinet-01",
  "inventory_revision": 12,
  "operation_id": "33333333-3333-4333-8333-333333333333",
  "occurred_at": "2026-10-16T10:00:00Z"
}
```

其他类型：operation.updated、reminder.triggered、delivery.updated、device.status。WebSocket 通知可以重复或漏过，不承担历史事件总线职责；页面先连接后拉取完整快照，对比 revision、忽略旧通知，重连再次拉取。reminder.triggered 可携带 delivery_id，页面显示后用接收回执确认。

设备播放命令逻辑上携带 delivery_id、message/audio_ref、expires_at。`audio_ref` 需鉴权或短期签名，格式与 Opus 参数由语音适配器协商。接收端回执：

```json
{
  "receipt_kind": "played",
  "device_time": null,
  "detail": null
}
```

receipt_kind 为 received/played/failed；只有授权目标设备可写音频回执，浏览器只能确认网页渠道 received。重复回执幂等保存；乱序回执不能使 played 倒退为 sent。played 表示终端报告播放完成，真实扬声器效果仍需实物验证。

## 8. 小智接入边界

小智音频 WebSocket 地址、hello 消息、编解码和打断逻辑待固件版本核对后补充独立协议文档。本文件的 device-events、capture 和 delivery 是 Mind Box 业务协议；不能直接将其发送给现有小智端并假设生效。先验证文本和网页提醒，再验证真实语音查询，最后验证空闲 30 分钟后的主动提醒。
