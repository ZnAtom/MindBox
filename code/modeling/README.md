# 柜体建模工具

从视频提取参考图，通过 Hyper3D Rodin 生成 GLB，支持任务续查、下载与材质统一。此工具是可选组件，不影响 Mind Box 主服务运行。

## 准备

安装本目录 `requirements.txt`，在进程环境中配置 `RODIN_API_KEY`。模型生成与拆分会调用付费服务。

从仓库根目录查看命令帮助：

```sh
python code/modeling/workflow.py --help
```

## 使用

按 `cabinet-v1.json` 的格式准备自己的配置和视频。示例配置引用的本地视频不随仓库发布，需要自行提供或修改路径。

```sh
python code/modeling/workflow.py inspect path/to/videos
python code/modeling/workflow.py prepare path/to/cabinet.json
python code/modeling/workflow.py submit code/modeling/var/inputs/<id>/prepared.json
python code/modeling/workflow.py finish code/modeling/var/tasks/<id>/task.json
```

上传前检查参考图和裁剪范围。配置及图片变化时使用新的 ID。`finish` 用于续查已有任务，不重新提交生成请求。

生成输出保存在被忽略的 `var/`。模型的实际尺寸、部件结构与抽屉运动需要人工核查。网页模型预览入口为 `/models.html`，素材来源见[模型说明](../webui/public/models/SOURCES.md)。
