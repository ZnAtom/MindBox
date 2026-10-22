# 图像分析与评估工具

本包包含分段视频分析、Responses 客户端与模型评估工具。`app.automatic` 依赖其中的 `long_video`、`long_video_audit` 和 `responses_client` 模块。

在 `code/backend` 查看可用参数：

```sh
python -m model_comparison.run --help
python -m model_comparison.evaluate --help
```

评估所需原始视频和运行输出不随仓库发布。联网分析需要配置模型服务，会产生 API 调用；评估结果应与对应数据、模型和配置一同解释。
