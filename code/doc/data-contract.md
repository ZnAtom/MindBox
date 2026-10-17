# 数据与调用约定

更新：2026-10-17。本文件只维护现行数据库、字段/状态和写入边界，以及日程扩展所需契约。处理架构见[流程](processing-flow.md)，真实结果统一见[评测](evaluation-and-lessons.md)。原SQL迁移及失败实验保留，历史阶段报告不再复制到此处。

## 1. 实物、商品与证据身份

| 标识 | 作用与边界 |
| --- | --- |
| capture_id / 视频SHA256 | 一次媒体来源；视频/音轨/PTS共享原点，不等于可信绝对拍摄时间 |
| operation_id / run_id | 一次物理操作与其分析attempt；重分析不是新操作，已提交操作不能再add |
| candidate_id | 仅在run中有效，不能跨视频当item_id |
| observation_id / frame_id | 数据库证据ID与本地产物引用；帧ID可能含PTS，时间读media_time_ms |
| item_id | 一件实物及其进出历史；同袋取出再放回复用，同SKU另一袋新建 |
| product_id | 可复用商品/物品类型档案；不能代替实物身份 |
| track_id | 新因果入口的会话内轨迹ID，由模型关联、程序台账持久化；不是独立运动跟踪器，不能跨会话当item_id |
| utterance_id | 音轨最终语句、版本、原话及时间窗；当前按同视频/音轨建立稳定ID |
| task_command / reminder / delivery ID | 用户意图草稿、一次发生、某渠道投递，三者不同 |

**大类足够入库。** `identity=supported`表示当前画面与该实物candidate可关联，不表示已认出品牌/款式。`action_status=confirmed`与`product_match_status=category_only`可以同时存在。品牌、容量缺失不产生库存pending；动作、件数/实体混淆仍须待确认。

## 2. 当前数据库

SQLite启用WAL、外键及10秒busy timeout；标准库sqlite3，无ORM。001/002包含20张业务表，003新增10张表，004新增automatic_sessions；另有schema_migrations保存迁移版本和SHA256。新HTTP服务启动时执行截至004的迁移，已执行迁移不能原地修改。

### 自动记录会话

| 字段/关联 | 含义 |
| --- | --- |
| `automatic_sessions.id` | 开柜会话稳定ID；后续作为Store的capture_id |
| `cabinet_id` | 外键关联真实柜子，当前cabinet-01 |
| `state` | capturing、queued、processing、committed、pending、failed、dismissed |
| `root` | 服务端本机存档目录，不暴露给网页；session.json和原帧是恢复所需数据 |
| `created_at / updated_at` | 状态时间，UTC |
| `detail_json` | 切片进度、失败原因或实际事务回执；不保存API密钥 |
| `session.json.frames` | JPEG SHA256、帧ID、主机单调到达毫秒、UTC到达毫秒；relative media_time_ms来自单调到达时间 |
| `session.json.doors` | 实际视觉编号、known状态及同一主机时间轴；不采用模型估计层数 |
| `input_sha256` | 在线JPEG会话为frames/doors/epoch规范化清单的SHA256，不冒充原始MP4哈希 |
| `inventory-context.json` | 分析起始库存版本、item_id与会话obj-ID绑定、历史外观参考 |
| `auto-<固定哈希>` | 会话 + obj-ID + 事件ID生成operation_id，保证崩溃后重放不重复提交 |

一个会话可产生多条逐动作operation，但库存仍只存既有items/inventory_commits中。自动记录状态表不创建第二份库存。按程序时间轴得到的capture_times由采集适配器提供，不允许模型输出或HTTP表单直接指定为可信时间。离线回放的测试者指定层数与媒体PTS不等于实际视觉编号或绝对拍摄时刻。

本机API：GET `/api/inventory`、`/api/inventory/evidence/{id}`、`/api/recording/state`、`/api/recording/sessions/{id}`及其帧；POST `/api/inventory/items`补录，PATCH `/api/inventory/items/{id}`校正，POST `/api/recording/sessions/{id}/retry|dismiss`处理记录。写库存必须携带库存revision、已有物版本和幂等key；来自其他网站的Origin不能修改。服务只监听本机，不构成远程登录/鉴权方案。

```mermaid
flowchart LR
    C["cabinets / locations / devices"] --> M["media_captures / media_segments"]
    C --> E["原始events"]
    M --> O["operations / processing_runs"]
    O --> V["observations / candidates / pending_candidates"]
    V --> G["应用校验与inventory_commits"]
    G --> I["items / item_observations / storage_visits"]
    G --> H["inventory_changes / verified_events"]
    I --> P["product_profiles / examples / claims / item_product_links"]
    P --> K["knowledge_snapshots / experiment_context"]
    M --> S["speech_utterances：原话、时间、关联与日期候选"]
    S --> T["task_commands / item_event_rules / rule_firings"]
    I --> T
    T --> R["reminders / deliveries / jobs"]
    G --> R
```

| 表组 | 关键语义/约束 |
| --- | --- |
| cabinets / locations / devices / events | 柜体revision与inventory_known；位置归属；设备/事件身份去重，同键异内容冲突；无重量事件 |
| media_captures / media_segments | 源哈希、共同时间轴与采集元数据；当前完整文件作为单段，不宣称已接在线分片 |
| operations / processing_runs | 柜体操作序号、当前attempt、基线revision、原始JSON及工具审计；换目录重跑同视频默认复用操作 |
| observations / candidates / pending_candidates | 证据哈希/PTS/归档引用；候选唯一(run_id,candidate_id)；未解决项及审核理由 |
| items / item_observations / storage_visits | 稳定实物、版本及证据关联；每件最多一条未关闭进出记录 |
| inventory_commits / inventory_changes / verified_events | 真实事务回执、净变化审计、映射真实item的过程历史；一个operation只提交一次 |
| jobs | 事务队列、业务唯一键、租约令牌、重试和期限；旧租约不能完成新worker的任务 |
| reminders / deliveries | 一次提醒及渠道queued/received/played等状态；网页不能确认语音播放 |
| product_profiles / product_examples / product_claims | candidate/verified/rejected、版本、多视角、逐字段来源及撤回；不是未经核实模型回答的累积 |
| item_product_links / knowledge_snapshots / experiment_context | 商品版本与实物映射、内容哈希快照、真实隔离scope/mode/manifest |
| speech_utterances | 原音轨归档/哈希、最终原文、分段/偏移、版本、候选事实、item_link与缺失项 |
| task_commands | 当前只支持before_expiry/after_expiry_remove；唯一(utterance_id,kind)，状态needs_input/scheduled/active/canceled |
| item_event_rules / rule_firings | 同item已确认取出的条件及版本；唯一(rule_id,rule_version,event_id)触发去重 |

证据归档为`<数据库stem>-assets/<sha256>`，DB存相对路径；原视频仍为外部引用。`Database.backup`使用SQLite backup API并复制归档资源，不能只复制一个可能含未落盘WAL的文件。副本验收需完整性/外键/哈希/历史数量检查。

## 3. 物品字段与时间

| 字段 | 来源与未知处理 |
| --- | --- |
| name / name_kind / category / description | 粗类别或外观名称均可；name_kind为specific/attribute_description/unknown，视觉初始名称为attribute_description；未知袋子不猜食品 |
| brand / variant / product_id | 可空；品牌、口味、型号属于补全，不参与粗存门槛 |
| quantity / unit | 有数量依据才填，混框不算一件；null不否定已有一条实物记录 |
| presence_status | present/unseen/removed/uncertain；未见不自动removed |
| identity_status | candidate/confirmed/group_uncertain，描述实物身份，不是SKU确定程度 |
| action_status / product_match_status | confirmed/pending；unknown/category_only/candidate/verified，两条状态轴独立 |
| location_id / storage_visits | 同柜体位置；取出放回沿用item，新增一次访问而非新实物 |
| first_seen_at / last_seen_at / stored_at | 可信采集锚点+媒体证据；无锚点保持null，不用处理时间或文件修改时间补 |
| production_on / expires_on / shelf_life_raw | 明确用户/包装证据；候选月日与正式日期分开，缺年不写假日期 |
| opened_at / review_at / notes | 开封有证据，检查建议不等于有效期；备注不能绕过正式日期校验 |
| created_at / updated_at / version | 系统处理与版本，不冒充采集/放入时间 |

当前没有独立net_content/barcode/aliases字段或解码闭环。无需200ml、品牌或口味即可通过基本认物验收。name_kind保存在现有item值JSON中，本轮不新增或原地改SQL迁移。

`field_meta={source,certainty,evidence_refs,extractor,supporting_sources}`；source可为vision/ocr/barcode/user/system/derived/unknown/speech/rag/web，certainty为recorded/estimated/unknown。枚举有web不表示允许网页直接回写。recorded是有来源，不保证用户/文字绝对无误。

实际标签日期需要可追溯ocr/barcode/user/speech或derived来源；推导须保存输入引用。一年/一个月采用日历运算，不换成365/30天；有效期日期与到期日何时失效的边界策略分开。离线“今年”只用可信采集时钟或显式synthetic_test时钟，不用当前系统年。任一输入疑点只应影响对应字段，逐字段疑点隔离为待修改项。

## 4. 动作证据与写库

状态为inside/outside/absent/occluded/unknown。outside必须在箱外可见；absent须箱内清楚且无目标，不能由反光/关门/无SIFT框推断。before所有引用必须早于after，且仅引用实际提供帧。at_rest表示抽屉承托/已松手，手靠近不等于抓取。

旧入口`window-states.json`保存普通观察、initial_observation、review_observation、review_decision、supplied_frame_ids/input_kind。P0-A已按媒体时间约束before/after修正方向；同帧相反放稳/在柜判断仍保留冲突，不能整窗覆盖其他物品。

`operation_analysis`包含event_candidates、net_changes、pending、observation_gaps、baseline_observations、snapshot_checkpoints、placement_completions、cancelled_placements、unobserved_prefixes。跨窗事件保存evidence_window_ids和state_evidence，不把箱外正证据改写成清楚缺失。supported_endpoints/consistent均不是提交回执，也不是正确率。

旧入口`CoarseIntake.assess`当前policy为single-insertion-evidence-v2，路线stable_state_comparison/direct_crossing。单新增、可辨物体、同实例、放稳、时间、可见性和无未决冲突成立即可；身份细节缺失不拒绝。仍不支持整段混合多事件自动准入；新因果入口把动作拆成独立operation提交。

新入口`app.causal`契约：

- 父session_id复用现有media_captures.id；每个子operation_id来自session、track、动作类型及证据时间窗的哈希，已提交operation直接返回原回执。没有额外capture_session表。
- `causal/session.json`保存watermark_ms、步骤、轨迹台账、真实事务回执、最终inventory和results；轨迹pending与数据库pending_candidates分开，DB的pending_count不能代替会话未决数量。
- TrackPoint位置为inside/outside/frame_edge/out_of_frame/occluded/unknown，带frame_id、归一化bbox、at_rest和逐点drawer_phase。不可见点bbox=null；只用NEW已到达帧更新，旧帧仅作上下文。out_of_frame要求看见视频边缘离开过程；关屉遮挡保留原库存。
- 首帧原有实物可在隔离演示库调用register_visible_baseline，回执registration_kind=visible_initial_baseline、filmed_inventory_action=false；它创建初始实物记录和访问，不编造本视频的add事件，不把未知整柜变为空柜/已盘清。
- `results`按已提交回执列item_id、track_id、kind、name/name_kind/category/description、location_id、stored_at、media_window_ms、support、evidence_refs、quality、operation_id/commit_id/revision。名称为当前库存投影，事件窗口仍为当时证据；support不是概率，probability=null。历史录像缺可信绝对时钟，stored_at保持null，媒体毫秒仍可追溯。
- visual_quality为诊断，含sharpness、曝光风险、原始尺寸和可见比例估计；严重模糊不单独拒绝。usable或可辨认partial允许，错框/混框/低实物支持度pending，取放证据另验。
- 解码完成仍有未决时status=completed_with_pending；完成读取不等于业务验收成功。中途DB异常为paused_error，同配置resume可重取确定性回执。新配置/提示/输入变更需新实验目录。

| 调用 | 边界 |
| --- | --- |
| Store.import_run | 归档观察/候选/待处理，不改变库存；相同输入重传复用 |
| Store.get_inventory_snapshot | 同读事务读取真实items/revision；未知基线单列，不把空DB当已知空柜 |
| Store.register_visible_baseline | 仅接受标注为初始观察的完成run，验证实物与定位，明确返回非视频放入的初始登记回执；当前供因果隔离实验使用 |
| Store.commit_inventory | 应用显式计划，验证当前attempt、证据归属、item版本、柜体revision及幂等；短事务内写库存/历史/回执/jobs |
| Enrichment.apply | 名称/类别/描述/品牌/款式/商品关联白名单；不改数量、presence、stored_at，不再add；人工新纠正和陈旧响应冲突 |
| SpeechService.apply_facts | 仅已关联语句的日期/期限事实；无关item引用、未知年、冲突均不能覆盖正式日期 |
| Store.correct_item | 受信人工纠正，保存来源和版本审计；不是模型写库工具 |

库存提交计划含operation_id、run_id、base_revision、idempotency_key、reviewed_by与decisions。add创建item；remove/return/update/keep必须带真实item_id和expected_version；return复用已移除身份。一次净决策可同时记录四步历史，不能把v7四个动作都当数量变化。

回执字段为status/commit_id/cabinet_id/operation_id/run_id/revision/added_item_ids/removed_item_ids/updated_item_ids/kept_item_ids/evidence_refs。同键同内容返回原回执，同键异内容冲突；模型请求在事务外。读取当前状态与导入时回执分开，原JSON的committed=false不覆盖后来真实DB提交。

## 5. 语音、商品记忆与联合回执

`SpeechService.ingest`验证视频scope、音轨哈希、最终文本、准确引用的槽位与时间段；原话/候选归一化并存。完全相同稿重传复用；文本/时窗改稿当前拒绝并要求重核验，不静默覆盖事实或任务。中间稿无副作用。

关联目前为同视频新增item、名称归一化或有限类别兼容、事件时间接近及仍在柜内。v9“可乐/罐装汽水”已通过饮品类别兼容规则关联；仍保留候选分数、唯一候选和冲突拒绝，不按时间单独强绑。语句先pending不阻塞实物入库。关联成立后的日期只补该item，不继承到其他同款物品。

统一会话回执为计划中的读视图，须返回各阶段status、run/item/utterance/task/reminder/delivery ID、缺失项与来源。不得把inventory=committed、task=needs_input合并成整段失败；同样不得以任务已保存宣称已设置或已播放。联合检查目前由`scripts.workflow_review`生成，尚无对应HTTP服务。

商品RAG只存可复用名称、类别、品牌/款式等类型信息和合格样例；生产/到期日、实际剩余量、数量、位置、stored_at、语音任务不可进入可复用档案。只读verified检索返回最多3候选，词法score不是概率；只有鸡胸肉大类不能继承某SKU。当前限制性自动复用要求已观察到品牌+款式或可靠标识，不妨碍粗存。

隔离模式cold/warm/holdout/disabled由服务固定，每个实验独立SQLite/assets/index/缓存/manifest。冷库禁止回退生产；warm显式种子；holdout排除同商品及同图别名泄漏。manifest记录视频/提示词/代码/商品快照哈希、时钟、预算及显式缓存来源。精确缓存回归不是新模型识别试验。

后台补全键包含item、证据哈希、提取器版本；重读item和柜体版本，人工纠正优先，取出后只能补历史属性，不恢复在柜。新增清晰视角才重开识别；商品撤回后未来召回失效，已有item不静默抹字段。在线拟用capture父会话+子operation，结束仅对账，避免两次add。

## 6. 日程数据与后续迁移

现有任务状态：needs_input为草稿，scheduled为已建一次提醒，active为事件规则生效，canceled为取消。`reminders.pending/triggered/missed`是调度状态；`deliveries.queued/received/played`是投递状态，均不代表用户done。

before_expiry需要同item明确expires_on、显式到期边界和未来due_at，due_at必须早于到期边界。after_expiry_remove不接受due_at，需要同item已提交remove、可信事件时钟、规则生效之后/到期之后的事件。去重键(rule_id,version,event_id)。历史录像无可信时钟不触发新警告；取出不自动完成其他提醒。

语音意图source=speech；用户配置的过期取出策略source=configured_policy，不能伪装音轨听到。日期修改使旧活动规则失效并取消未领取投递；已发出的网络副作用不能保证撤回。关联或日期晚到时，needs_input草稿会刷新同一记录的缺失项并递增版本；用户补答后的真实Web/设备闭环仍待接入。

### 周期任务扩展（尚未实施）

| 结构 | 设计与复用 |
| --- | --- |
| task_commands | 003已有特定到期语音命令；扩展时迁移/兼容，不再次创建同名通用表冒充全新能力 |
| task_drafts | 多轮槽位、每槽位来源消息/锚点、缺失项、version、续接期限；多个草稿先选中，不广播补答 |
| user_tasks | 标题、可空item、身份/柜体范围、渠道、active/paused/canceled/completed、version；通用提醒不强制item |
| task_schedules | task、once/daily/weekly、本地时间/IANA时区、规则版本、next_due_at、enabled/exhausted、宽限策略；早晚可有两条 |
| task_exceptions / task_events | 今天完成/跳过的唯一发生键，任务本次/系列修改审计；到点前done也能抑制今天发生 |
| reminders（扩展） | 复用一次发生，不另建task_occurrences；唯一(schedule_id,version,scheduled_for)，用户动作与调度状态分列 |
| deliveries（迁移） | 增加round/target_id/规则版本/期限；唯一(reminder_id,round,channel,target_id)，稳定非空target |
| jobs（复用） | schedule.changed/reminder.deliver等类型，同一租约队列；识别与投递分kind保障执行资源 |

修改系列只影响未来并递增版本，保留历史。snooze保留原scheduled_for，另存next_notify_at和新round；done/skip取消该次未来轮次，系列继续。暂停恢复只算未来；离线恢复只按配置补最近仍有效一次，历史记missed，不逐条播报。现有一次提醒默认宽限600秒，不能在迁移时静默更改。

相对时间锚定说出该表达的消息首次accepted_at；重传不重新计时，补答时已过期则澄清。每天当地21点由CronTrigger计算，非固定24小时；next_due_at由库计算，模型不提供任意cron。初期Asia/Shanghai，夏令时未验。

一个独立APScheduler 3.x进程，MemoryJobStore为唤醒投影；业务期限持久化SQLite。启动/周期对账恢复期限与snooze；回调携带规则ID/版本/计划时刻，短事务校验后生成一次reminder/deliveries/jobs，再推进下次时间。模型/网络不阻塞回调。部署需限制第二调度进程。

升级使用下一个可用迁移号，001–003不改；在完整备份副本回填旧ID/幂等键/状态。deliveries旧唯一键不支持多轮多目标，须重建回填，已triggered/played/canceled不能重播。验证外键/完整性/历史数量、旧请求与ACK重放后再迁正式库。

网络投递至少一次，携带稳定delivery_id，客户端持久去重。received/voice played/用户done分开；网页关闭后通知可查询不等于已通知到人。设备主动下发、身份权限、设备回执去重和周期任务HTTP接口均待实施；不能因为FastAPI在依赖中就声称服务已运行。

## 7. 长流程实验分析账本

`model_comparison.long_video`将结果写入指定实验目录的`analysis.sqlite3`，不运行正式库迁移，不调用Store.commit_inventory。它是模型/时序算法的审计中间层，不能拿其objects行数当作真实库存数量。

当前自动记录服务已经有会话级采集、追加帧落盘和会话结束后的异步 worker；下面是录制期间增量窗口仍需补充的目标契约：`capture_sessions(capture_id, device_id, epoch, started_at, status, source_hash, last_capture_seq, last_media_time_ms, audio_clock)`保存采集会话；`capture_frames(capture_id, epoch, seq, device_pts, received_monotonic_ms, file, sha256, quality)`保存追加证据；`analysis_windows(window_id, capture_id, core_start_ms, core_end_ms, context_start_ms, watermark_ms, state_before_hash, request_hash, status, attempt_count)`保存只允许使用已到达帧的窗口；`analysis_updates(window_id, object_id, kind, status, event_id, evidence_refs, reason)`保存候选/确认/pending更新。窗口和事件均要求稳定ID；重复窗口响应只能更新同一记录，不能新增库存。

在线状态不应与生产库存状态混用：`observed`表示模型看到，`candidate`表示规则尚未闭合，`confirmed`表示证据规则通过，`pending`表示等待后续帧或人工补证据，`rejected`表示明确冲突。只有`confirmed`允许调用Store事务；候选可以通过SSE/WebSocket显示。`watermark_ms`是后端已经收到并持久化的最高媒体时间，任何在线模型请求不得引用更晚的帧。捕获断线时关闭当前窗口并标记`incomplete`，恢复后新建epoch，不把两个epoch的帧直接拼接。

| 表 | 内容与约束 |
| --- | --- |
| session | 视频SHA-256、代码/提示词哈希、处理状态；completed只表示片段处理结束 |
| objects | 全局obj-ID、名称/类别/外观、元数据历史、最后观察、动作台账位置；含柜外背景物品 |
| observations | `(object_id,frame_id)`唯一；原始媒体时间、归一化bbox、inside/outside/视频边缘/遮挡、松手/停稳、抽屉阶段 |
| proposals | 按顺序保存全部动作提案及accepted/pending原因；旧pending后被后续补齐仍保留原记录 |
| accepted_events | 符合当前程序证据规则的add/remove/return/move、实物ID、时间窗与证据引用；不等于独立人工验证 |

observed_existing建立可见的已有状态，不伪造放入；move只改变位置，不增加件数。物品名称可后续细化，obj-ID不随商品名称变化。视频没有可信拍摄日期时stored_at=null，仅保留媒体毫秒。恢复从不可变片段JSON重建同一分析账本，重叠提案不新增事件；新代码/提示词使用新实验目录。`database-receipt.json`记录实际行数，`long_video_audit`只读检查完整性、解决已补齐的旧提案并生成报告，不自动把分析结果导入生产库。
