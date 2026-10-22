# Mind Box 后端

Python 后端负责摄像头会话归档、异步图像识别、SQLite 库存、语音查询、提醒和带认证的只读 Agent API。

## 环境准备

使用 Python 3.13。以下命令从本目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-batch.txt -r requirements-voice.txt -r ..\device\k230\requirements-vision.txt -r ..\device\k230\requirements-wake.txt
Copy-Item .env.example .env
```

macOS/Linux 使用 `.venv/bin/python` 替代 `.venv\Scripts\python.exe`。Bash 启动器还需要在 `code/device/k230/.venv` 安装设备目录的视觉、音频与唤醒依赖。

编辑本地 `.env`，填写所用服务实际支持的 URL、模型名和密钥：

| 配置 | 用途 |
| --- | --- |
| `MIND_BOX_API_URL / KEY / MODEL` | 语音转写后的语义理解，使用 Chat Completions 接口 |
| `MIND_BOX_AUTO_API_URL / KEY / MODEL` | 自动图像分析，使用 Responses 接口 |
| `MIND_BOX_BRIDGE_URL` | 摄像头桥接地址，默认 `http://127.0.0.1:8765` |
| `MIND_BOX_DATABASE` | SQLite 路径，默认 `var/mind-box.sqlite3` |
| `MIND_BOX_ASR_MODEL / MODELS` | 本地 Whisper 模型名与缓存目录 |

示例配置中的服务地址可替换；图像和语义配置分别生效。密钥留在后端，勿提交 `.env`。

首次下载语音转写权重：

```powershell
.\.venv\Scripts\python.exe -c "from faster_whisper.utils import download_model; print(download_model('small', cache_dir='var/models'))"
```

如需中文唤醒，在仓库根目录运行：

```powershell
.\code\backend\.venv\Scripts\python.exe code/device/k230/download_wake_models.py
```

## 运行

完整设备链路使用[统一启动入口](../README.md)。单独启动后端与 Agent API 时，在本目录分别运行：

```sh
python -m app.server --port 8787
python -m app.agent_server --port 8788
```

摄像头桥接默认运行在 8765。后端在抽屉关闭后异步分析，识别耗时可能超过操作时长；网页关闭后，采集与后端任务仍可继续运行。

## 语音与提醒

支持文字或电脑麦克风输入、库存查询、日期信息补充、一次性和每日提醒。转写在本地执行，语义理解使用已配置的远端模型。浏览器负责语音回复与提醒播报，播放时须保持网页打开并允许声音。

## 个人 Agent API

在网页“设置 → 个人 Agent”中创建连接，为指定柜体授予查询权限。Key 仅显示一次，可轮换或撤销。接口定义为 `http://127.0.0.1:8788/openapi.json`。

该接口提供经认证的只读库存查询；第三方 Agent 的接入需要自行验证。

## 数据与备份

运行数据位于被 Git 忽略的 `var/`：

- `mind-box.sqlite3`：库存与任务数据库。
- `mind-box-assets/`：已提交的证据图片。
- `mind-box-sessions/`：原始会话帧、时间轴和录音。
- `models/`：本地语音模型。
- `log/`：启动日志。

备份时同时保留数据库、证据和会话目录。数据库迁移位于 `migrations/`，由程序初始化处理。

## 测试

```sh
python -m pytest -q
```
