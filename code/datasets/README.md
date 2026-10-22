# 评估数据与标注

本目录提供回归用例和 JSON 标注模板。原始视频、识别输出与个人录音不随仓库发布。

- `action-regression.json`：固定样本的取放动作与净变化回归数据。
- `templates/ground-truth.template.json`：真实物品与动作标注模板。
- `templates/ground-truth.example.json`：虚构填法示例。
- `templates/manifest.template.json`：素材清单模板。

准备自己的数据时，保留原始视频时间戳、分辨率与音轨，并记录素材来源、权限和文件哈希。人工答案仅供评估，不能作为识别推理输入。模板和示例不代表已完成的识别测试。
