# Mind Box：物品信息、证据和时间字段合同

版本：0.1；日期：2026-10-16；状态：首轮实验设计，尚未创建数据库迁移。配套 [处理方案](video-processing-and-item-recognition-plan.md)。本合同补充已有 `code/doc/data-model.md`，不平行建立另一个库存主库。

## 1. 自动生成的记录分三层

1. `observation`：某帧、某区域看到了什么。可以命名未知，可以暂不对应库存；保留全景、截图、OCR 和时间。
2. `action_candidate`：本次可能发生了放入、取出、移动、拿出后放回或未变化，引用证据和旧物品候选。
3. `item`：程序校验后保存的物品记忆；有物品身份、在柜状态、位置和历史，不由模型直接写。

先保存观察、后决定动作。检测失败和“没有物品”不同；不确定身份和“新物品”也不同。

## 2. 物品字段

### 2.1 给用户显示的内容

| 字段 | 类型/示例 | 生成与约束 |
| --- | --- | --- |
| `name` | string，“红色包装零食袋” | 取最具体且有依据的名称，不认识品牌也可保存；不输出空泛的“物品1”冒充识别 |
| `description` | string/null | 1--2 句可观察外观、形态、用途候选；推测用途要明确是推测 |
| `category` | string，“饮料/食品/日用品/工具/其他/未知” | 便于查询；未知可回填，不限制检测类别空间 |
| `aliases` | string[] | 用户称呼、OCR 品名；避免把同义词当不同物品 |
| `brand` | string/null | 明确可读的商标或用户明确说明，不仅凭配色认品牌 |
| `product_model` | string/null | 具体型号/商品变体，未知留空；不承诺精确 SKU |
| `appearance` | object | `color/shape/package_type/distinguishing_marks`，作为找物与身份关联线索 |
| `net_content` | object/null | 例如 `{"value":330,"unit":"mL","raw_text":"330毫升"}`，来自包装；不是剩余量 |
| `barcode` | string/null | 解码与校验结果，记录码制；不作为单件实体身份 |
| `quantity` | integer/null | 单件包装默认可见计数 1；组观测看不清总数时 null，不自动合并同名包装 |
| `unit` | string/null | 例如 piece/package；有数量时必须有单位 |
| `open_state` | sealed/opened/unknown | 仅凭外观或语音明确判断，无法确认则 unknown |
| `location_id` | string/null | 抽屉及位置；多门/视角不明时 null |
| `thumbnail_ref` | evidence id/null | 可回查的主图，裁剪只代表可见部分 |
| `presence_status` | present/unseen/removed/uncertain | 沿用旧数据设计；unseen 不等于 removed |
| `identity_status` | candidate/confirmed/group_uncertain | confirmed 指有对应依据，不代表完全排除算法错误 |

`quantity` 是“库存这一行对应几件”，`visible_count` 是某张图可数到多少，`net_content` 是包装标称容量/净重，三者不同。整柜差重不能直接变成每件物品重量，也不能据此推断剩余容量或质量等级。

描述禁止无证据写“新鲜、未变质、适合食用、剩余80%”等断言。未设置物主身份识别，画外说话不自动确定 `owner`；首版不生成所有者字段。

### 2.2 系统和历史字段

| 字段 | 含义 | 写入者 |
| --- | --- | --- |
| `item_id` | 物品稳定 ID，UUID | 程序 |
| `cabinet_id` | 所属柜体 | 鉴权/采集上下文 |
| `version` | 防止旧结果覆盖新记录 | 库存事务 |
| `created_at/updated_at` | 数据库建立和修改时刻 | 程序，不能当作放入时间 |
| `first_seen_at/last_seen_at` | 证据中首次和最近一次看到 | 程序从有效拍摄时间算；无墙钟时为 null，另存媒体时间 |
| `stored_at` | 兼容旧设计的本次有效入库时刻 | 程序；可未知，不用上传/分析时间填充 |
| `removed_at` | 明确取出事件的时刻 | 程序；无法给单点则保留事件时间窗 |
| `production_on/expires_on` | 包装生产日期/确定有效期，DATE | 包装、明确用户陈述或经验证的日历推导 |
| `shelf_life_raw` | “保质期12个月”等原文及条件 | OCR/用户 |
| `review_at/review_reason` | 建议检查时间和原因 | 可由 AI 候选生成，不能当作有效期 |
| `latest_observation_id/field_meta` | 最新证据和字段来源 | 程序校验 |

`put_in_at` 作为外部显示名映射旧 `stored_at`，不再建立含义相同但可独立修改的第二个列。增加入柜访问记录 `storage_visits`：`id/item_id/operation_id/location_id/entered_window/exited_window/time_quality/evidence_refs`，支持同一包装拿走后再次放入；首轮可先保存为审计事件，实施时再定迁移。

视频开始前已经存在时：初始访问开始未知。整段明确看到了“拿起→放回”：记录 `take_and_return` 子动作，保持原 item，不重复新增；有明确离柜后另一次回柜时新增访问记录。无法确认是原物品还是另一件同款时，不强行复用 ID，也不自动建立确定的新数量。

## 3. 时间如何自动生成

模型描述“什么时刻的哪些证据显示了动作”，程序负责算时间。全流程区分：

- `media_time_ms`：相对这次采集共同原点的毫秒数，不是网络接收时间；
- `device_monotonic_ms`：设备本次启动中的时间；不同设备不能直接比较；
- `captured_at`：有可信时钟映射的 UTC 拍摄时间；
- `received_at`：电脑接收时间；
- `analyzed_at`：电脑/云模型分析时间。

PyAV 使用 `PTS × time_base` 定位帧。声明一个采集共同原点后统一映射，不要让音频、视频各自减去首个 PTS 而丢掉原始声画偏移；片段编号也不自动等于片内时间归零。重采样、VAD、重编码后的时间轴必须保留到原始 PTS 的映射。

若有同步锚点：

```text
captured_at_utc = anchor_utc + (media_time_ms - anchor_media_time_ms)
```

连续流时钟漂移要维护分段映射和误差范围。只有模型的视觉时序估计时，不能宣称精确到毫秒；即使锚点准确，动作边界仍存在视频帧间隔和遮挡误差。

典型输出规则：

| 场景 | 放入时间输出 |
| --- | --- |
| 片头已有物品 | `stored_at=null`，记录 first_seen；UI 显示“首次看见，放入时间未知” |
| 5.2秒尚未放入，8.4秒已在柜内 | 记录 `[5200,8400]ms` 的事件窗，依据对应帧确认是放入后再映射 UTC |
| 只有相对时间、没有可靠录像起始日期 | 绝对时间 null，UI 显示“视频约5.2--8.4秒放入” |
| 有明确放入瞬间且时钟可信 | 可写 `stored_at` 的代表时刻，并保留误差窗和 `estimated` 标记 |
| 用户明确说“昨天放的” | 存原话，按说话时刻和时区解析为日粒度区间；无法锚定则保持相对描述 |
| 用户只说“可乐”，未说明动作 | 可辅助命名，不能推出放入时间 |
| 上传/识别很晚 | 不用 received_at/analyzed_at 冒充放入时间 |

`first_seen_at` 也不能伪造：离线录像无可信起始日期时，它只能是媒体时间引用，不是处理当天。

## 4. 日期字段和冲突

保存 `date_candidates`：日期原文、OCR 原始框、解释为生产/到期/批号的候选、适用条件和证据。只读到 `20261016` 但看不到“生产日期/有效期”等说明时，不能直接判为到期日。

保质期按月/年时使用日历规则，不能固定按30/365天换算。仅当生产日期、期限、适用保存条件和规则均明确时才由程序生成派生日期，保留推导输入；存在歧义时只保留候选。开封保质期单独关联 `opened_at` 和条件。

优先保留明确用户纠正，但用户和包装互相矛盾时展示冲突和来源，不静默丢弃原始证据。AI 常识或常见保质期只进入 `review_at/review_reason`；不能覆盖已记录的包装日期。

## 5. 字段来源

每个非系统业务字段都保存独立的 `field_meta`：

```json
{
  "name": {
    "source": "vision",
    "certainty": "estimated",
    "evidence_refs": ["crop-001"],
    "extractor": "deepseek_official/deepseek-flash",
    "alternatives": [],
    "conflict": false
  },
  "stored_at": {
    "source": "system",
    "certainty": "unknown",
    "evidence_refs": []
  }
}
```

`source` 单选 `vision/ocr/barcode/user/system/derived/unknown`，多来源在 evidence_refs 及 supporting_sources 中列出；不使用不受控的 `vision+ocr+...` 字符串。沿用旧 certainty：`recorded/estimated/unknown`。recorded 表示有明确来源，不等于识别绝对正确。模型 confidence 可保存在原始响应，但不解释为校准后的可靠率。

已填字段无有效 evidence_ref 应拒绝或降级。系统字段来源应为程序，不允许模型通过描述伪造 `item_id`、UTC 时间、库版本和审核通过状态。包装和转写里的命令式文字只作数据，不能让模型照着改变处理规则或执行操作。

## 6. 动作候选

建议候选结构包含：

```text
candidate_id                    程序分配
kind                            add/remove/move/take_and_return/no_change/uncertain
existing_item_candidates[]      仅引用输入提供的真实旧 item_id
observation_refs[]              真实帧/crop/OCR/音频证据
event_window_ms                 已验证的媒体时间窗
proposed_fields                 名称、描述等字段候选
uncertainties[]                 遮挡、同款难分、时间未知等
```

模型最多建议待核实，不自行宣布 committed。视觉观察和语音冲突时保留两者，不能用“模型多数票”替代物理证据。多物品同次变化时可以生成多个候选，整柜重量只校验整次操作，不分摊到单件。

## 7. 自动提交门槛

- 新增：有可见物品证据及“从外进入→放置”或可比较前后变化的明确依据；仅初次看见可以创建观察，不直接断言此刻新增。
- 取出：有明确离柜动作和真实旧 item 关联；单纯遮挡/没看见不允许删除。
- 更新：字段有来源、item 归属正确；位置变化与取出区别处理。
- 同款难分：保存候选集合或 `group_uncertain`，不合并成确定实体。
- 数量/品牌/日期未知不必阻止有充分证据的新增；已知事实可自动保存，未知字段保持空，避免每次强制追问。
- 每次提交验证 `base_inventory_revision`、当前 attempt 和幂等键；重试或离线重新跑数据不重复写正式库存。默认实验仅导出 JSON/报告。

业务不确定映射已有 `operations.state=pending`，原因如 `ambiguous_identity`；缺视频为 `pending_evidence`；模型接口错误为 `failed/retry_wait`。`needs_review` 是 UI/候选标志，不另发明冲突的数据库终态。

## 8. 对已有数据模型的增量

实施时新增媒体会话/分片元数据，增加 crop/OCR/音频证据引用和时间窗。`items` 增补 description、brand、规格、appearance、open_state；名称/数量/位置等沿用旧列，外部 `put_in_at` 映射 `stored_at`。`analysis_attempts` 记录供应商入口、请求模型名和可取得的实际模型版本。

将处理阶段单独存 `processing_runs`：`run_id/capture_id/config_hash/prompt_version/model_versions/input_hash/output_refs/status`。同一输入可用不同模型重复实验，候选与正式库存事务严格分开。这里仅定义增量，尚未运行迁移。
