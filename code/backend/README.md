# Mind Box 视频处理实验

已经实现视频→选帧→AI定位→原图裁剪→局部复核→RAG类别规范→动作候选→JSON/HTML。此处是离线实验流水线，还没有接入K230上传服务或正式库存数据库。

## 环境

推荐Python 3.13。Docker Desktop已安装且Linux引擎已启动；本机2026-10-16尝试Docker Hub、Google镜像缓存均在基础镜像下载阶段超时，因此**容器构建和容器内执行尚未验证**。真实视频使用隔离的`.venv`运行。

```powershell
cd [local path hidden]
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

在本地`.env`配置模型入口和密钥。`.env.example`可供新环境参考；当前`.env`已设置临时Key且被Git和Docker忽略。不要把它提交、截图或写进日志。

学校接口实际可用别名是`deepseek-pro`，`deepseek-flash`对当前Key返回400“未绑定模型”。响应也只披露`deepseek-pro`，无法证明底层具体版本。TLS默认开启，单并发，最多4次尝试；连接错误、超时、408/429/5xx有限退避重试，400/401/403直接记录失败。不要用盲目重试掩盖模型绑定错误。

## 运行

```powershell
# 文本、真实图片语义、JSON、原生工具调用的独立能力探测
.\.venv\Scripts\python.exe -m app.cli probe

# 不调用AI：解码、检查质量/变化、选帧、提取音轨
.\.venv\Scripts\python.exe -m app.cli prepare ..\..\data --output var/runs/prepare --max-frames 10

# 调用AI：文件或整个目录；只遍历视频扩展名，不读取人工答案文档
.\.venv\Scripts\python.exe -m app.cli batch ..\..\data --output var/runs/new-run --max-frames 10 --resume

# 调用统计、失败记录、结果哈希和截图总览
.\.venv\Scripts\python.exe -m app.cli audit var/runs/new-run

# 仅从已有JSON重新生成静态报告，不调用AI
.\.venv\Scripts\python.exe -m app.cli report var/runs/new-run

.\.venv\Scripts\python.exe -m pytest -q
```

输出`var/runs/<run>/report.html`可直接在浏览器查看。每段视频包含原分辨率帧、标框图、截图、音轨、媒体时间、原始候选、复核和最终物品字段。`api-attempts.jsonl`记录每次真实调用/重试/结构修复/缓存命中；`tool-trace.json`记录工具参数与返回。缓存包括模型响应，不含请求图片或鉴权头，但包含物品识别文本，应按私人数据保管。

复用同一输出目录时，成功请求可命中缓存，已完成视频在输入哈希和配置一致时可跳过。更换模型、提示词或选帧参数建议使用新目录，保留对比。接口出错会保留该视频失败状态并继续下一视频；程序异常退出可用`--resume`继续。

## Docker

网络恢复或为Docker配置可用网络后：

```powershell
docker compose config --quiet
docker compose build
docker compose run --rm processor python -m app.cli probe --output /app/var/probe
docker compose run --rm processor python -m app.cli batch /data --output /app/var/runs/docker-test --max-frames 10 --resume
docker compose run --rm processor python -m app.cli audit /app/var/runs/docker-test
```

镜像包含固定依赖和代码；`.env`只在运行时注入，视频挂载只读。没有GPU依赖。官方Google缓存可用时可显式指定`docker compose build --build-arg PYTHON_IMAGE=mirror.gcr.io/library/python:3.13-slim`；本机该入口也超时，不能声称已构建成功。

## 当前边界

- 检测不依赖固定YOLO类别，也没有用这8段视频训练；当前由多模态模型给框，几何合法不保证框紧贴目标。
- 最新代码按crop区域清晰度/曝光和贴边情况挑主图；v8有单独补测记录，完整两轮保留原先视角顺序。
- RAG首版是本地类别定义和中文字符二元组检索；不是商品数据库，也不是向量检索。人工答案`data/ans.md`不参与识别上下文。
- `search_catalog/get_evidence/search_product_images`为受限只读工具；模型无库存写入或任意文件/网络权限。
- 图片搜索适配器需额外`SERPAPI_API_KEY`及`MIND_BOX_WEB_SEARCH=true`，默认关闭；它是文字图片搜索，不是真正反向搜图。本次没有执行联网商品搜索。
- 已提取音轨但未转写，未部署SAM、独立OCR或ASR。包装文字来自视觉模型，仍可能误读。品牌文字校验只能减少无依据补全，不能证明文字真实。
- 名称、描述、大类、品牌/款式候选有证据引用；置信等级不是校准概率。重复候选只提示关联，不自动合并数量。
- 动作仅输出带帧证据的媒体时间窗；无可信拍摄墙钟，绝对放入时间保持null，不用处理时间代替。

详细设计见[流程设计](../doc/rag-tools-and-video-mvp.md)，当前待办见[TODO](../doc/video-pipeline-todo.md)，实测结果见[实测报告](../doc/video-mvp-experiment-report.md)。
