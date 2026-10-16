# 后端数据模型与事务

版本：0.1；日期：2026-10-16。本文描述计划模型，数据库和迁移尚未创建。

## 1. 通用规则

- 主键采用服务端或设备生成的 UUID 字符串；设备提交的 UUID 需要格式和归属验证。
- 系统时刻统一 UTC，API 使用带时区的 RFC 3339；界面按 Asia/Shanghai 显示。设备时间不可信时保留 `null` 和时间质量。
- 包装“年月日”采用 DATE 字段，不擅自转换为当天零点的精确到期时刻。用户提醒采用明确 UTC 时刻，模糊时间由可配置规则或追问解决。
- 重量用整数毫克保存，接口示例允许克数，入库统一换算；这只避免浮点累积，不代表毫克级测量精度。计数、日期、重量未知用 `null`，与 0 不同。
- JSON 字段用于原始数据、字段来源及供应商响应；检索和业务约束使用正常列。所有枚举和引用在应用层校验，核心约束同时写入数据库。

## 2. 主要表

### 柜体、区域与设备

| 表 | 关键字段与约束 |
| --- | --- |
| `cabinets` | `id, name, inventory_revision, created_at`；库存提交时递增 revision |
| `locations` | `id, cabinet_id, label, drawer_no`；同柜体 drawer_no 唯一；可配置“位置未知” |
| `devices` | `id, cabinet_id, role, credential_hash, capabilities, firmware_version, last_seen_at, last_boot_id`；role 为 sensor/camera/voice 或组合 |

capabilities 记录如传感器、缓存、采集结束标记、空闲控制、播放回执支持状态；未知不写成支持。密钥不明文入库，初始化与轮换走管理配置或脚本。

### 原始事件、操作与照片

| 表 | 关键字段与约束 |
| --- | --- |
| `events` | `id, device_id, boot_id, seq, operation_id, kind, device_time, monotonic_ms, clock_quality, received_at, payload, payload_hash, processing_status`；唯一 `(device_id, boot_id, seq)`，id 也唯一 |
| `operations` | `id, cabinet_id, operation_no, state, opened_at, closed_at, location_ids, before_weight_mg, after_weight_mg, calibration_id, weight_quality, capture_deadline_at, frozen_at, base_inventory_revision, current_attempt, committed_at, uncertainty_reasons`；唯一 `(cabinet_id, operation_no)` |
| `capture_requests` | `id, operation_id, camera_device_id, state, requested_at, completed_at, expected_frame_count, received_frame_count, deadline_at` |
| `observations` | `id, camera_device_id, boot_id, frame_id, operation_id, request_id, captured_at, received_at, clock_quality, visibility_status, image_key, sha256, byte_size, width, height, is_late`；唯一 `(camera_device_id, boot_id, frame_id)` |
| `analysis_attempts` | `id, operation_id, attempt_no, base_inventory_revision, input_refs, provider, model, prompt_version, status, candidate_json, raw_response_ref, error_code, started_at, finished_at`；唯一 `(operation_id, attempt_no)` |

事件重复身份且 payload_hash 相同返回原结果；同一身份不同内容返回冲突并记日志，绝不覆盖。`operation_id` 必须归属设备所在柜体；照片还须核对 request 的相机权限与操作归属。

`captured_at` 未同步时可空，`received_at` 永远由服务器填写。稳定重量来自事件，需保留原始事件引用和标定版本，不只保存差值。`before_weight_mg/after_weight_mg` 任一未知时差值未知。

### 库存与证据

| 表 | 关键字段与约束 |
| --- | --- |
| `items` | `id, cabinet_id, location_id, name, category, quantity, unit, identity_status, presence_status, stored_at, first_seen_at, last_seen_at, production_on, shelf_life_days, expires_on, opened_at, review_at, review_reason, latest_observation_id, notes, field_meta, version, created_at, updated_at` |
| `item_observations` | `item_id, observation_id, relation, evidence_region, certainty`；联合主键；一次照片可关联多个物品 |
| `inventory_changes` | `id, cabinet_id, operation_id?, analysis_attempt_id?, command_request_id?, item_id, action, before_json, after_json, reason, evidence_refs, revision, change_index, created_at`；唯一 `(cabinet_id, revision, change_index)` |
| `inventory_commits` | `id, cabinet_id, operation_id?, command_request_id?, revision, created_at`；operation_id 唯一且 command_request_id 唯一，防止一次操作/命令重复提交 |

一行 items 优先对应一件/一包装，不自动合并同名物品。确实只能观察到一组时 `identity_status=group_uncertain`，数量可空，不伪造个体追踪。`quantity` 非负且有 unit；件数须为整数。presence_status：`present/unseen/removed/uncertain`，未见不能自动置 removed。

field_meta 示例：

```json
{
  "name": {"source": "vision", "certainty": "recorded", "evidence_refs": ["observation-id"]},
  "stored_at": {"source": "system", "certainty": "estimated", "evidence_refs": ["event-id"]},
  "expires_on": {"source": null, "certainty": "unknown", "evidence_refs": []}
}
```

`recorded` 表示有记录依据，不保证识别绝对准确。值有冲突时保留原证据和待核实信息；模型推断不得覆盖用户/包装提供的明确日期。`shelf_life_days` 仅适合明确天数，包装若按月/年描述则保留原文并使用日历规则，不按固定 30/365 天擅自换算。

### 指令、提醒与投递

| 表 | 关键字段与约束 |
| --- | --- |
| `command_requests` | `id, principal_id, idempotency_key, body_hash, source, text, voice_session_id?, accepted_at, status, parsed_intent, response_json`；唯一 `(principal_id, idempotency_key)` |
| `reminders` | `id, cabinet_id, item_id?, target_text, origin, message, due_at, channels, status, created_at, triggered_at, canceled_at, command_request_id?`；一次性任务，status 为 pending/triggered/canceled/missed |
| `deliveries` | `id, reminder_id?, notification_kind, source_id, channel, target_id, payload, state, attempts, next_attempt_at, expires_at, sent_at, received_at, played_at, last_error`；唯一业务投递键 `(notification_kind, source_id, channel, target_id)`，source_id 为提醒/提交等业务来源 |
| `delivery_receipts` | `id, delivery_id, device_id, receipt_kind, received_at, payload`；唯一 `(delivery_id, device_id, receipt_kind)` |

无法唯一定位苹果时可以建立通用提醒 `item_id=null, target_text="吃苹果"`，不创建虚构物品。提醒触发不代表食物吃掉；语音播放完成也不等于用户执行。取消与到期触发通过条件更新竞争：取消先提交则不产生新投递；已触发时尽力取消尚未发出的渠道，已播放无法撤回，返回实际结果。

delivery state：`queued/waiting_device/sending/sent/received/played/failed/expired/canceled`。网页渠道终态可以是 received，只有音频渠道使用 played；同一个提醒可有不同渠道状态。

### 任务与幂等请求

| 表 | 关键字段与约束 |
| --- | --- |
| `jobs` | `id, kind, business_key, payload, state, attempts, max_attempts, next_run_at, lease_owner, lease_expires_at, last_error, created_at, finished_at`；business_key 唯一 |
| `idempotency_requests` | `principal_id, route_scope, key, body_hash, state, resource_id, response_status, response_json, created_at`；联合主键；用于直接库存修改和提醒建立等同步写请求 |

语音/文本指令自身用 command_requests 去重；其他写接口用 idempotency_requests。相同 key 与相同请求返回原结果；不同 body 返回 409；正在处理返回任务资源与 202。业务命令在保留期内持续去重，不能很快清理 key 后使设备离线重传重复执行。

## 3. 原子提交边界

库存自动提交步骤：

1. 在事务外完成识别与候选校验，得到引用、修改集和 base_revision。
2. 开启短写事务，确认操作尚未提交、attempt 是当前版本。
3. 通过 `UPDATE cabinets ... WHERE inventory_revision = base_revision` 比较并递增版本；影响行数为 0 则回滚，重新分析/待核实。
4. 在同事务内写 items、item_observations、inventory_changes、inventory_commits，更新 operation.state，并插入通知 jobs/deliveries。
5. 提交后响应，worker 再执行网络推送。任一步失败整个事务回滚。

手工纠正和语音修改也走同一库存服务，要求物品 version 并递增柜体 revision，防止识别旧结果覆盖用户刚改的数据。已提交操作不原地重放，纠正应生成有原记录引用的新变更。

事件接收同事务保存原始事件及必要的处理 job；确认“已接收”之前二者都必须提交。提醒触发与建立渠道投递同事务提交，避免提醒已触发但任务丢失。

## 4. 操作状态

```text
capturing -> waiting_stable -> ready -> analyzing
analyzing -> committed | pending | retry_wait | failed
retry_wait -> analyzing
capturing / waiting_stable -> pending_evidence
pending_evidence -> ready | pending | canceled
pending -> ready（补证据或明确纠正，创建新 attempt）| canceled
```

`pending` 表示业务不确定，`failed` 表示处理失败；二者不混用。进入 pending 后可以继续后续操作，保留未解决上下文。`committed` 为本次操作的写入终态；迟到结果只能归档，纠正用新变更。

## 5. 索引与迁移

优先索引：`events(device_id, received_at)`、`operations(cabinet_id, state, operation_no)`、`observations(operation_id, received_at)`、`items(cabinet_id, presence_status, location_id)`、`reminders(status, due_at)`、`jobs(state, next_run_at)`、`deliveries(state, next_attempt_at)`。所有外键及唯一约束在迁移中建立。

数据库初始迁移与模型一同提交；配置和模拟数据不混入正式迁移。自动迁移的破坏性变更需要检查，备份恢复应在联调阶段实测。
