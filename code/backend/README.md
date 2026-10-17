# 运行离线视频处理

流程、效果和下一步统一从[开发入口](../README.md)阅读。当前包含离线视频流水线和SQLite数据库服务层/管理命令；没有运行中的HTTP库存服务。首版暂不做重量变化。

## 环境

Python 3.13；基础依赖在`requirements.lock.txt`，第一批另用`requirements-batch.txt`安装日期和调度依赖；ASR使用独立环境及`requirements-asr.txt`。本地`.env`由Git和Docker忽略；新环境参考`.env.example`。实测学校别名为`deepseek-pro`，底层版本未披露；不宣称已调用官方4.1 flash。单并发、有限重试，TLS配置沿用环境设置。

```powershell
cd [local path hidden]
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-batch.txt
```

## 无模型调用的检查

```powershell
# 读容器、编码、宽高、PTS时间基、音轨采样率与声道
.\.venv\Scripts\python.exe -m app.cli inspect-media ..\..\data

# 从真实结果读取工具调用与持久化状态，不执行数据库写入
.\.venv\Scripts\python.exe -m app.cli inspect-run var/runs/improved --output var/review/call-results.json

# 解码、扫描选帧和提取音轨
.\.venv\Scripts\python.exe -m app.cli prepare ..\..\data --output var/runs/prepare --max-frames 10

.\.venv\Scripts\python.exe -m pytest -q
```

`inspect-run`未传`--database`时返回`database.status=not_connected`，不推断库存为空；传数据库路径才读取真实导入状态和提交回执。历史运行JSON中的`not_implemented`保留原样。

## 数据库

使用Python自带sqlite3，无新增依赖。默认数据库为`var/mind-box.sqlite3`，截图按内容哈希归档到`var/mind-box-assets/`，两者均不提交Git。迁移SQL在`migrations/`，启动初始化会检查已应用版本的校验值。

```powershell
# 初始化：默认初始库存未知；只有实际确认空柜时才在首次建柜加 --known-empty
.\.venv\Scripts\python.exe -m app.cli db init
.\.venv\Scripts\python.exe -m app.cli db status

# 导入已有识别结果，只存证据、候选和pending，不自动变更库存，不调用模型
.\.venv\Scripts\python.exe -m app.cli db import-run var/runs/state-actions-v8/v8 --base-revision 0
.\.venv\Scripts\python.exe -m app.cli db inventory --include-removed
.\.venv\Scripts\python.exe -m app.cli db receipts
.\.venv\Scripts\python.exe -m app.cli inspect-run var/runs/state-actions-v8 --database var/mind-box.sqlite3

# 按数据约定准备明确提交计划JSON，再执行受控事务
.\.venv\Scripts\python.exe -m app.cli db commit plan.json

# 一致性备份数据库及已归档截图，目标目录必须尚不存在
.\.venv\Scripts\python.exe -m app.cli db backup var/backups/session-01
```

其他命令：`db item <id>`、`db history <id>`、`db operation <id>`、`db resolve <run_id> --reason ... --reviewed-by ...`、`db create-item request.json`、`db correct-item request.json`、`db create-reminder request.json`、`db reminders`、`db cancel-reminder <id>`、`db trigger-reminders`。`--database`和`--cabinet`放在`db`之后、子命令之前。数据库关系图、请求样例和功能边界见[数据约定](../doc/data-contract.md)。

新识别运行可在`batch`或`actions`命令末尾加`--database var/mind-box.sqlite3 --cabinet cabinet-01`，读取真实库存快照并在处理结束后保存候选；不会自动提交库存。输出目录仍应保持不可变，修改结果需新建运行目录或显式新attempt。离线素材不保证来自同一连续柜体，导入时应核对柜体与基线版本。

第一批已增加APScheduler周期扫描入口，SQLite保存真实期限和任务，重启后重新扫描；不是第二套队列。网页发送器和语音播放器尚未实现，`trigger-reminders`及扫描只产生待投递任务，不意味着收到或播放。

## 浏览器语音查询

语音查询服务接收浏览器录音，在电脑本地用 `faster-whisper` 转写，只把转写文字发送到配置的 OpenAI 兼容远端模型。模型通过 `search_inventory` 工具读取 SQLite 中已经确认的库存，回答由浏览器的 `speechSynthesis` 在电脑端播报；远端模型不能直接写数据库。

先初始化真实库存库并准备本地 ASR 模型：

```bash
cd code/backend
python -m app.cli db --database var/mind-box.sqlite3 init
# 如需建立明确的空柜基线，首次初始化时使用 init --known-empty
python -m pip install -r requirements-voice.txt
# 只需联网下载一次本地 ASR 权重；运行语音服务时不会再下载
python - <<'PY'
from faster_whisper.utils import download_model
print(download_model('small', cache_dir='var/models', local_files_only=False))
PY
```

配置 `.env` 中的 `MIND_BOX_API_URL`、`MIND_BOX_API_KEY` 和 `MIND_BOX_MODEL` 后启动服务：

```bash
python -m uvicorn app.voice_api:app --host 127.0.0.1 --port 8780
```

模型权重必须已经位于 `MIND_BOX_ASR_MODELS`（默认 `var/models`），服务只使用本地权重，不在请求时下载。WebUI 的 Vite 开发服务器将 `/api/voice` 转发到 `127.0.0.1:8780`；可用 `MIND_BOX_VOICE_URL` 修改地址。没有 API 密钥或没有初始化数据库时，接口返回明确错误，不会生成猜测答案。

备份恢复：将备份目录中的数据库和同名`*-assets`目录放在同一父目录下，继续使用该数据库路径；可直接对备份路径运行`db --database <path> status`检查。原始视频仍为外部引用，重要录像另行备份。数据库备份使用SQLite backup API，兼容WAL，不直接复制正在写入的主文件。

## 模型识别与报告

```powershell
.\.venv\Scripts\python.exe -m app.cli probe
.\.venv\Scripts\python.exe -m app.cli batch ..\..\data --output var/runs/new-run --max-frames 10 --resume
.\.venv\Scripts\python.exe -m app.cli audit var/runs/new-run
.\.venv\Scripts\python.exe -m app.cli report var/runs/new-run
```

`batch`接受单个视频或目录，默认不读子目录及人工答案。更换算法/模型/提示词使用新运行目录；`--resume`复用成功请求缓存及输入/配置一致的完成结果。`report`仅重建HTML，不调用模型。

输出：`report.html/summary.json/audit.json/api-attempts.jsonl`；每个视频有`media.json/frames/annotated/crops/audio.wav/detection.json/verification.json/evidence.json/tool-trace.json/result.json`。不保证每个失败阶段都有完整产物。原视频只读，结果与模型缓存属于私人素材，不提交Git。

既有两轮结果在`var/runs/baseline`和`improved`，额外v8主图排序实验在`crop-rank-check`；HTML可直接打开，无需开发服务器。新代码增加容器信息和局部crop排序，不重写历史实验。

## 抽屉动作回归

`batch`已接入新的抽屉阶段、前后快照和分段状态流程。单独检查动作时，可复用冻结的认物结果，但仍从原视频重新取时序帧：

```powershell
.\.venv\Scripts\python.exe -m app.cli actions ..\..\data --source-run var/runs/improved --output var/runs/new-actions --videos v7 v8 --resume
.\.venv\Scripts\python.exe -m app.cli evaluate-actions var/runs/new-actions
.\.venv\Scripts\python.exe -m app.cli inspect-run var/runs/new-actions --output var/review/action-calls.json
```

省略`--videos`会检查全部视频。输出目录必须与来源目录分开。`evaluate-actions`单独读取`datasets/action-regression.json`，该文件是根据人工答案与冻结crop标注的评测资料，识别和动作流程都不读取它。更换认物结果后需重新标注candidate ID，不可套用旧映射。

新增产物为`action-media.json/action-frames/storyboards/appearance-matches.json/matched-frames/scene-plan.json/coverage-windows.json/snapshots/snapshot-states.json/window-states.json/rejected-state-claims.json/action-tool-trace.json/operation-analysis.json`。SIFT绿色框只是正向几何提示，背景可能误配，不能用匹配缺失判断取出。`consistent`只表示当前视觉观察可对账，实际是否正确仍由独立评测判定。`return`表示同一实例取出后放回，事件仍全部`committed=false`。

动作证据默认8fps并保留真实末帧；预览最多20帧，拼成每页4帧并标真实时间。每约1.25秒覆盖一个短区间，并给前后各约250ms上下文，截止到合拢前快照。未知、遮挡、终态冲突保留pending；有先前已验证状态时可显式跨短时遮挡关联，时间窗可能变宽，不冒充精确抓取时刻。

## Docker

历史曾验证Docker Linux引擎，但基础镜像下载超时。本轮2026-10-17找不到Docker命令或预期安装路径，未构建/运行容器；下面是可执行配置，不是容器成功记录。真实样本在`.venv`和独立ASR环境完成。具备Docker的机器上：

```powershell
docker compose config --quiet
docker compose build
docker compose run --rm processor python -m app.cli batch /data --output /app/var/runs/docker-test --max-frames 10 --resume
docker compose run --rm processor python -m app.cli inspect-run /app/var/runs/docker-test
```

视频只读挂载，输出写`var`；Key仅运行时注入。新增`Dockerfile.asr`和Compose的`asr` profile，语音权重单独挂载。联网商品搜索默认关闭，仍不是反向搜图；独立OCR和分割未部署。

## 第一批离线入口

首次配置独立ASR环境；不改主环境的PyAV版本：

```powershell
.\.venv\Scripts\python.exe -m venv var/environments/asr
.\var\environments\asr\Scripts\python.exe -m pip install -r requirements-asr.txt
.\var\environments\asr\Scripts\python.exe -m app.asr var/runs/voice-intake-20261016-01/v10/audio.wav --output var/asr/download-check.json --models var/models --model small
```

官方Hugging Face下载本轮超时，曾显式使用`HF_ENDPOINT=https://hf-mirror.com`下载相同仓库；本地权重校验值记录在`var/runs/batch-a-asr/v10-final.json`。依赖必须保持`av==16.1.0`：本轮ASR环境的av19不兼容faster-whisper传入的`metadata_errors`。主环境继续使用原av18.1.0。

新视频统一入口，目录必须新建；`--asr-python`可选，传入后先视觉，再独立转写和槽位处理。动作pending不阻止语音建议保存；只有确认实物关联后才写实物字段。不是实时流式接口。

```powershell
.\.venv\Scripts\python.exe -m app.batch_a ../../data/v10.mp4 --root var/experiments/new-v10-cold --asr-python var/environments/asr/Scripts/python.exe
.\.venv\Scripts\python.exe -m app.batch_tools var/experiments/new-v10-cold inspect
.\.venv\Scripts\python.exe -m app.task_worker var/experiments/new-v10-cold --seconds 10
```

也可分步执行`batch_tools ... speech --video-hash ... --audio ... --asr ...`；`--removal-policy-reason`显式启用过期取出产品策略，来源标为configured_policy，不冒充语音。`enrich --run ... --candidate ... --item ...`只补既有关联实物。商品命令为`memory-stage / memory-verify / memory-search / memory-reuse / snapshot`，详细参数可看各命令帮助。`memory-reuse`需要当前已保存的品牌+款式或真正解码证据，类名相似仅返回candidate。

`task-complete --task ... --version ... --expiry-cutoff <带时区绝对时刻> --due-at <带时区绝对时刻>`用于用户明确补齐的一次提醒；条件规则不传due-at。必须先有已关联实物的确定年份日期；不默认提前一天或九点。生产时间、有效期只能经来源受限的事实事务或真实人工纠正写入。

实际结果：`var/review/batch-a-audit.json`为合并审计；`var/experiments/batch-a-v9-cold`和`batch-a-v10-cold`为自动结果；`batch-a-assisted-v10-r2/acceptance-report.json`为已明确标注的复核演示，不能当作自动成功。其中comparisons分别保存cold/warm/holdout/disabled库及真实短倒计时；生产库存没有迁移或提交。

## 视频直传能力最小测试

独立入口先生成两段顺序相反的3秒H.264 MP4，要求模型返回每阶段的颜色和形状，逐项比对；HTTP200及模型自称可看视频不算通过。只向已配置学校接口发送合成素材，不写数据库；输出目录必须新建，原始回答与失败日志保留。

```powershell
.\.venv\Scripts\python.exe -m app.video_probe --output var/experiments/video-chat-new
.\.venv\Scripts\python.exe -m app.video_probe --output var/experiments/video-response-new --protocol responses_input_video
.\.venv\Scripts\python.exe -m app.video_probe --output var/experiments/image-control-new --image-controls var/experiments/video-chat-new
```

`chat_video_url`和`responses_input_video`是待验证的格式假设，未取得学校视频协议文档；不能推断其模型或路由支持所有其他视频上传方式。2026-10-17实测4次MP4均未通过，三张图片对照通过，因此未发送v7/v9/v10等真实录像作视频直传对比。完整结果与下一步见[统一失败经验](../doc/evaluation-and-lessons.md#5-历史边界与统一失败经验)。当前`success` API日志仅表示取得回答，必须另看`report.json`的`native_video_status`和各trial的`passed`。

## P0动作回归

复用已冻结的物品crop/候选，对动作阶段独立回归；原视频、旧结果和正式库只读。每个视频有独立SQLite/assets/manifest，`ans.md`只用于事后评测，标签不会输入模型。所有新运行目录必须不存在；中断后使用新目录和显式精确缓存来源，不覆盖旧实验。

```powershell
.\.venv\Scripts\python.exe -m app.action_regression --root var/experiments/replay-new --mode replay --variant recorded
.\.venv\Scripts\python.exe -m app.action_regression --root var/experiments/fresh-new --mode fresh --videos v9 v10 v7 v8
.\.venv\Scripts\python.exe -m app.action_regression --root var/experiments/cache-new --mode fresh --cache-from var/experiments/fresh-new
.\.venv\Scripts\python.exe -m app.action_regression --root var/experiments/cache-new --mode finalize
.\.venv\Scripts\python.exe -m scripts.action_regression_audit var/experiments/fresh-new var/experiments/cache-new --output var/review/action-audit.json
```

`recorded`保留相反复核，`original`仅重放各窗口首轮回答，两者都不是新模型观察。`fresh`重新从原视频解码并调用动作观察，物品识别仍冻结；`--cache-from`仅按完整请求哈希命中，未命中才联网，cache_seed来源及哈希写manifest。`finalize`只对已提交的同item补名称，重复执行不再新增事务；pending不补成已入库。审计脚本只读数据库，汇总真实回执、调用/校验失败、输入哈希和当前测试报告。

已保存目录：`p0-replay-recorded-20261017-01`、`p0-replay-original-20261017-01`、`p0-fresh-20261017-01`、`p0-final-20261017-01`；合并审计`var/review/p0-audit.json`。实际通过/失败、调用量与当前数据库结果统一见[评测](../doc/evaluation-and-lessons.md)。

## 联合检查与v7溯源

以下命令不读取密钥、不调用模型/ASR。v7仅在内存中重放/替换冻结观察，输出诊断；speech使用SQLite backup和归档资源创建新副本，接入保存的真实ASR/槽位，重复执行相同处理以检查幂等，保留源库哈希。

```powershell
.\.venv\Scripts\python.exe -m scripts.workflow_review v7 --output var/review/v7-module-review.json
.\.venv\Scripts\python.exe -m scripts.workflow_review speech --root var/experiments/joint-review-new --removal-policy-reason '用户配置的到期后取出提示策略，不是音频原话'
```

speech目标目录必须不存在，副本沿用冻结源manifest/scope以便重现；不会回写源库，也不会新增库存。`--removal-policy-reason`仅用于v10，省略则只生成真实音频要求的任务。当前v9关联pending、v10已关联但任务缺日期/时间，均如实报告。架构统一见[处理流程](../doc/processing-flow.md)，结果与修改建议见[评测](../doc/evaluation-and-lessons.md)。
