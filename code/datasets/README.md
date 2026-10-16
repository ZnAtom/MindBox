# Mind Box 音视频实验数据集

用途：接收真实录制素材，验证选帧、物品截图、名称/描述/时间字段和库存动作。当前只有规范和空模板，没有测试数据或处理结果。处理设计见 [完整方案](../../proj1/project-planning/technical/video-processing-and-item-recognition-plan.md)。

## 1. 现在怎么录

第一批先录 8--12 条短视频，每条约 20--60 秒，保留原始声画、原始分辨率和帧率。手机或电脑录制也可以先测算法，但最终要补 K230 实拍以检查视角、压缩和运动模糊，不能用手机成绩代替 K230 成绩。

尽量固定相机在未来柜体的接近视角，动作前后各留 2--3 秒可见稳定画面。不要为了算法刻意每件停顿很久；先有正常速度样本，再单独录极快/遮挡样本。录制时无需每次开口；自然语音样本和静默样本分开测试。

| 案例 | 实际动作 | 要测什么 |
| --- | --- | --- |
| 01 | 空柜放一件零食，静默 | 基础选帧、定位和名称，无语音也能工作 |
| 02 | 放一瓶饮料并说名称/数量 | 声画关联，不能凭声音伪造框 |
| 03 | 片头已有物品，再拿出 | 放入时间应未知；正确关联取出 |
| 04 | 连续放2--4件不同物品 | 分时出现、多件计数和每件最佳图 |
| 05 | 同款两包，取其中一包 | 防合并、防重复计数、身份不确定 |
| 06 | 拿出再放回 | 不把 A→B→A 过程去重掉，不重复新增 |
| 07 | 只移动抽屉或翻动物品 | 不产生无依据增删 |
| 08 | 快速动作、手遮挡、小物品 | 漏帧和证据不足 |
| 09 | 反光/透明/柔软包装 | 分割、OCR 和裁剪完整性 |
| 10 | 包装正面/背面/日期面分时露出 | 保留互补截图，避免编造日期 |
| 11 | 取放但声音说错/旁人说话 | 语音冲突不能直接改库存 |
| 12 | 1--3分钟连续多个操作，中间长静止 | 分段、去重和积压；之后再补更长稳定性视频 |

可以先交前3--5条，不必等整个数据集齐全。未知形状的普通日用品务必有一部分，不限食品。正式长录和网络断开/补传在设备接入后另测。

## 2. 推荐目录

```text
code/datasets/
  README.md
  templates/
    manifest.template.json
    ground-truth.template.json
    ground-truth.example.json
  batch-001/
    manifest.json
    raw/
      case-001.mp4
      case-002.mp4
    ground_truth/
      case-001.json
      case-002.json
```

`raw` 不覆盖、不加字幕、不通过聊天软件二次压缩；大视频可直接保存在此电脑指定目录，manifest 使用相对素材路径。原始素材和处理产物默认不提交 Git。

## 3. 最小需要提供什么

必须有原视频；最好每条附一句真实过程，例如“原来有两包同款，拿出一包，最后剩一包”。如果知道录制起始日期，附带时区填写，否则留空。不要凭文件复制时间或今天日期补录像时间。

manifest 从 templates 复制后填写：

- `capture_id`：每次录制唯一；
- `video_path`：原视频；独立音频才填写 audio_path，嵌入视频时为 null；
- `started_at_utc`：可信录制起始 UTC 时刻，未知 null；`clock_quality` 标明可信程度；
- `audio_offset_ms`：独立音频相对采集共同原点的偏移，未知 null，不能默认为0；
- `initial_inventory`：事先知道的物品实例，含人工实例 ID；未知 null，空柜为 []，二者不同；
- `location_id`：已知抽屉/区域；
- `split`：unassigned/calibration/test；首轮按完整录制划分，不按相邻帧随机拆。

在未知绝对时间但声画同文件时，仍可评价视频内相对时间。独立声轨偏移未知时不评价毫秒级声画同步，先记录限制。不得把 ground_truth 的名称、动作、日期输入识别器；生产本来能知道的初始库存可以单独按受控输入提供。

## 4. 人工真值怎么标

使用 `ground-truth.template.json`，所有时间均为采集共同原点下毫秒；原视频嵌入声轨可采用视频时间原点。模糊动作填区间，不用虚假精确时间。可以先只写文本过程，之后在看帧时补时间窗和框。

`templates/ground-truth.example.json` 是填法示例，时间与物品均为虚构，不可计入评测。真实注释完成后 `annotation_status` 使用 `annotated`；未标注和示例一律不计分。

- `object_instance_id` 是人工实例身份；两包同款一定不同，不等于模型生成 item_id。
- `action` 使用 add/remove/move/take_and_return/no_change/uncertain。
- `initially_present=true` 表示片头已在柜中，不应要求算法恢复未知放入时间。
- `event_window_ms` 是真实动作发生的尽量窄区间；`visibility_intervals_ms` 是能看到/辨认物品的区间，两者不混用。
- `fields` 只填已知事实；unknown 留 null；没有印日期和日期拍不清是两类不同情况。
- 选少量代表帧标原图 bbox `xyxy`，必要时再标 mask；不必一开始标所有帧。
- 无法靠画面判断的事实标 `observable=false`，单列不可观察项，不能强求算法猜中。

## 5. 每次处理应返回什么

下方是完整流水线的目标产物。当前MVP的真实产物位于`code/backend/var/runs/baseline`和`improved`，已包含帧、标框、crop、音轨、media/result/evidence JSON、HTML及审计；SAM mask、独立OCR和转写尚未生成。运行方式见[后端说明](../backend/README.md)。

```text
processing-runs/<run_id>/
  input-manifest.json          实际输入、哈希与时钟信息
  run-config.json              固定参数、模型名、入口、版本和提示词版本
  timeline.json                操作/子动作及证据时间
  frame-scores.jsonl           每个候选帧质量、是否保留和理由
  frames/                     保留的原分辨率帧
  annotated/                  带物品框、编号和时间的全景
  crops/                      每件主图与互补图
  masks/                      启用分割时的结果
  transcript.json             带原媒体时间的原始转写
  ocr.json                    原文、文字框和对应 crop
  candidates.json             字段和动作候选、未知/冲突
  item-records.json            程序校验后的模拟物品记录
  report.html                 可视化核对：原帧、截图、字段来源和错误
  metrics.json                覆盖率、漏检、误删、耗时和调用用量
```

每个 crop 必须反查到原视频、帧 PTS、原图 bbox、所属操作和实际模型版本。没有模型或接口失败时保存中间产物并标明缺失阶段，不用模拟成功结果替代。

## 6. 首轮对照顺序

1. 先只跑媒体检查、选帧和音频转写，确认关键动作未被过滤。
2. 固定候选帧，跑 Grounding DINO 及按需 SAM/OCR，检查每件物品截图。
3. 同样图片/OCR/时间证据交给官方 DeepSeek 与 GPT 中转分别抽字段；学校 DeepSeek 入口单独记录。
4. 人工核对品牌、数量、动作和时间。输出 unknown 合理与否也要计分，不能只统计非空字段。
5. 冻结校准参数后跑留出的完整录制测试集，再决定主模型是否更换。

验证数据用于评测，不用于训练；本轮无须用户先标出一套可训练类别。先得到真实失败样例，再决定是否需要增加切片、分割或跟踪复杂度。

## 7. 本轮实际执行

用户素材位于根目录`data/v1.mp4`至`v8.mp4`，约40秒；嵌入音轨已提取为16kHz单声道WAV，保留首PTS及共同原点，无ASR转写。首轮直接比较学校VLM给框与crop复核，未先部署Grounding DINO/SAM。

`data/ans.md`在首轮全部输出完成后才阅读；没有进入提示词或本地词库。第二轮用于修正开发样本上的错误，不能视为新的独立测试集。两轮结果和局限见[实测报告](../doc/video-mvp-experiment-report.md)。
