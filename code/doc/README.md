# Mind Box 后端文档

日期：2026-10-16，Asia/Shanghai。

状态：后端设计提案 + 已实现的离线视频实验。8段视频已完成两轮学校DeepSeek实测；设备上传、ASR和正式库存事务尚未实现，具体效果与限制见实测报告。

| 文档 | 内容 |
| --- | --- |
| [后端设计](backend-design.md) | 目标、技术选型、模块、业务流程、部署与故障处理 |
| [数据模型](data-model.md) | 实体、字段、唯一约束、事务和状态机 |
| [接口约定](api-contracts.md) | 设备上报、图片、库存、查询、提醒和状态推送 |
| [实施计划](implementation-plan.md) | 开发顺序、各阶段交付、验证方式和待补信息 |
| [完整音视频处理方案](../../proj1/project-planning/technical/video-processing-and-item-recognition-plan.md) | 本轮新要求：K230 完整音视频传电脑、选帧、开放定位与模型分工 |
| [物品字段与时间规则](../../proj1/project-planning/technical/item-information-contract.md) | 自动名称/描述、放入时间、来源和库存校验 |
| [视频实验数据集](../datasets/README.md) | 录制场景、目录、标注模板与拟交付产物 |
| [视频处理TODO](video-pipeline-todo.md) | 逐项实施状态与环境阻碍 |
| [RAG、工具与提示词](rag-tools-and-video-mvp.md) | 当前流程、置信与搜索边界 |
| [真实视频实测报告](video-mvp-experiment-report.md) | 两轮真实结果、失败记录与后续优化 |

## 与已有资料的关系

最新输入要求（2026-10-16 本轮）：K230 采集完整视频和音频交给电脑处理。上表新增的音视频方案优先描述这部分；原后端文档中的照片接口和小智音频仍作为已有设计快照，完整音视频接口与时间轴增量尚未实现。模型选定是首轮实验基线，未视为已实测结论。

- 产品范围遵循 [产品形态](../../proj1/project-planning/product-design/product-concept.md)：正常收纳即可，不强制逐件登记。
- 设备分工遵循 [2026-10-16 系统架构](../../system-architecture.md)：K230 采图，小智承担语音和计划中的传感器采样，服务器关联数据。
- 验证边界遵循 [可运行性评估](../../system-assessment.md)：整柜称重、空闲播报和端到端效果均待实测。
- 相比 10 月 13 日候选方案，本设计提出具体的软件实现基线，保留硬件与供应商适配点；不将它自动记为既有已确认决定。

## 本次交付边界

`code/backend/app/`已实现可运行的离线视频CLI、受限工具和结果报告。完整业务服务按实施计划后续推进，不能把实验候选直接视为正式库存。
