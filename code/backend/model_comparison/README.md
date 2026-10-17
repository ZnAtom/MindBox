# 独立模型对比测试

本目录只用于用户授权的模型实验，正常应用不会导入这里的代码。配置不修改 Codex 设置、项目 `.env`、模型默认值或依赖锁。临时 Key 从进程环境 `MIND_BOX_COMPARE_KEY` 读取，不写文件。

原始结果位于被 Git 忽略的 `../var/experiments/model-compare-20261017/`：

本轮结论和逐视频评分见[实测报告](../var/experiments/model-compare-20261017/report.md)。10 段视频中，DeepSeek 原流程 / GPT 原流程 / GPT 密集帧的三项答案同时正确分别为 2/10、6/10、8/10；最终净变化正确分别为 5/10、8/10、9/10。正常项目未切换模型或流程。

| 组 | 内容 |
| --- | --- |
| `deepseek_pipeline` | 新请求运行当前完整视觉流程，10 张认物帧 + 原有 8fps 动作证据 |
| `gpt_pipeline` | 同一流程、提示词和规则，通过 Responses 适配器请求 gpt-6.1-sol |
| `gpt_dense` | 每秒 4 帧、整段覆盖并保留末帧，单轮直接观察实物、动作、净变化 |

三组使用 v1-v10 同一批开发视频；不提供人工答案或历史模型输出，不读取 RAG 记忆，不调用 ASR，不执行库存提交、联网商品搜索和提醒。v9/v10 的语音和日程不在本次视觉对比范围。品牌/规格不是基础通过门槛，实物对应、动作和净变化分别评分。

GPT 采用 `store=false`、图片 `detail=high`、`reasoning.effort=low`；Responses 输出预算为原流程 max_tokens 加 4000，容纳推理 token。这些是记录在 manifest 的配置差异，不能把跨协议实验称为完全相同的 token 条件。第三组同时改变流程与图片输入，所以用于比较方案整体，不能单独归因于帧密度。

网关列出并回报 `gpt-6.1-sol`；学校接口回报 `deepseek-pro`。模型名是网关声明，未独立验证实际底层。`store=false` 是请求参数，不构成对第三方服务留存策略的保证。本次官方文档页面返回 403，传输格式通过小图、工具调用和本地协议测试核验。

在 backend 目录使用现有 venv：

```powershell
.\.venv\Scripts\python.exe -m pytest model_comparison/test_transport.py -q
.\.venv\Scripts\python.exe -m model_comparison.run probe --root var/experiments/model-compare-20261017/probe
.\.venv\Scripts\python.exe -m model_comparison.run run --root var/experiments/model-compare-20261017 --model gpt-6.1-sol
.\.venv\Scripts\python.exe -m model_comparison.evaluate score --root var/experiments/model-compare-20261017
```

运行前在当前进程提供测试 Key；不要把 Key 放进上述命令文件或提交。正常 DeepSeek 凭据只从原项目配置读取。首次创建 manifest 时固定视频、正常代码/提示词/配置/正式库哈希；结束后 `isolation-check.json` 检查这些文件是否变化。原始结果不覆盖旧实验。

样本少且参与过开发，结果只能解释这批视频的表现；单次运行不能证明总体模型排名。对预测完成后再做独立标注，不能套用旧识别结果的 candidate_id。

评分依据当前 crop/密集帧人工写入隔离目录的 `review-mappings.json`，再由 `evaluate freeze` 固定全部结果 SHA-256 为 `manual-review.json`；`evaluate score` 仅重算评分，不请求模型。基础认物、存取过程、最终净变化分开；对“瓶装液体但未确认饮品”保守记基础分类未完成。人工答案没有穷尽抽屉内挪动，所以所有组一致排除 `move` 后评价存取动作，额外挪动记录另行保留。

本轮为缩短等待，在相同 GPT 原流程组内用 `run_tail` 提前处理 v10/v9/v8，日志分开写入 `api-tail`；主序列继续到这些视频时按原有 config_hash 复用完成结果，不重复调用。评分汇总 `api*` 下的所有日志。密集组也单独并行完成，其后主序列只读取已完成结果。
