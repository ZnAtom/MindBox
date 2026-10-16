# Mind Box 代码工作区

本目录用于保存项目代码，`doc/` 保存后端设计与实施计划。

已实现离线视频处理实验流水线，完成8段真实视频两轮测试。它输出物品截图、名称/描述/大类、证据与动作候选；K230实时上传、ASR和正式库存服务仍待实现。

## 入口

- [文档索引](doc/README.md)
- [服务器后端设计](doc/backend-design.md)
- [数据模型](doc/data-model.md)
- [接口约定](doc/api-contracts.md)
- [实施计划与验收](doc/implementation-plan.md)
- [后端代码目录](backend/README.md)
- [视频处理TODO](doc/video-pipeline-todo.md)
- [RAG、工具与提示词设计](doc/rag-tools-and-video-mvp.md)
- [真实视频实测报告](doc/video-mvp-experiment-report.md)

当前可运行部分使用Python 3.13、PyAV、OpenCV、Pillow、Pydantic和学校DeepSeek兼容接口，依赖已固定在`backend/requirements.lock.txt`。Docker引擎正常，项目镜像构建因外部镜像站超时受阻，实际样本在`.venv`中完成。FastAPI/SQLite库存服务仍按设计文档推进。

已有产品文档仍在 `../proj1/project-planning/`，最新系统架构与评估在仓库根目录。本目录没有改写原有“已确认技术方案”。
