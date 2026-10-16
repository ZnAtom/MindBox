# 运行离线视频处理

流程、效果和下一步统一从[开发入口](../README.md)阅读。当前包含离线视频流水线和SQLite数据库服务层/管理命令；没有运行中的HTTP库存服务。首版暂不做重量变化。

## 环境

Python 3.13；依赖固定在`requirements.lock.txt`。本地`.env`已保存学校临时Key，Git和Docker忽略该文件；新环境参考`.env.example`。学校当前可用别名为`deepseek-pro`，底层版本未披露。默认校验TLS、单并发、有限重试。

```powershell
cd [local path hidden]
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
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

提醒扫描、工作任务领取和回执记录已具备数据库接口；当前没有常驻调度循环、网页发送器或语音播放器。`trigger-reminders`只将到期提醒转为待投递任务，不意味着已播放。

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

Docker Linux引擎已验证；项目镜像因基础镜像下载超时尚未构建成功，真实样本在`.venv`完成。网络可用后：

```powershell
docker compose config --quiet
docker compose build
docker compose run --rm processor python -m app.cli batch /data --output /app/var/runs/docker-test --max-frames 10 --resume
docker compose run --rm processor python -m app.cli inspect-run /app/var/runs/docker-test
```

视频只读挂载，输出写`var`；Key仅运行时注入。联网商品搜索还需额外`SERPAPI_API_KEY`和`MIND_BOX_WEB_SEARCH=true`，默认不启用，不是真正反向搜图。ASR、独立OCR和分割未部署。
