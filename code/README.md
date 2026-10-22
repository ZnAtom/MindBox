# Mind Box 软件

本目录包含 Mind Box 的后端、K230 设备桥接、网页和辅助工具。产品介绍与路线图见[项目主页](../README.md)。

## 组件

| 目录 | 用途 |
| --- | --- |
| [backend](backend/README.md) | 图像分析、SQLite 库存、语音、提醒与个人 Agent API |
| [device/k230](device/k230/README.md) | K230 摄像头、抽屉状态检测和电脑录音桥接 |
| [webui](webui/README.md) | 库存管理、3D 柜体、摄像头与语音界面 |
| [modeling](modeling/README.md) | 可选的柜体模型生成工具 |
| [datasets](datasets/README.md) | 识别回归数据与标注模板 |
| `scripts/` | Windows 与 macOS/Linux 本地服务启动脚本 |

## 启动

先按[后端说明](backend/README.md)准备 Python 环境、模型配置与语音权重，在 `code/webui` 安装 npm 依赖，并按[设备说明](device/k230/README.md)连接开发板。

Windows，在仓库根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\code\scripts\mindbox.ps1 start
```

默认本地服务：

| 服务 | 地址 |
| --- | --- |
| 网页 | http://127.0.0.1:5173 |
| 摄像头桥接 | http://127.0.0.1:8765 |
| 后端 | http://127.0.0.1:8787 |
| 只读 Agent API | http://127.0.0.1:8788 |

脚本支持 `start`、`restart`、`stop`、`status`；`reset` 等同于重启服务，不清空数据。macOS/Linux 可在准备好对应虚拟环境后使用 `./code/scripts/mindbox.sh start`；其停止和重启命令会终止指定端口的监听进程，使用前需确认端口归属。

## 检查

在 `code/backend` 运行 `python -m pytest -q`；在 `code/webui` 运行 `npm run build`。
