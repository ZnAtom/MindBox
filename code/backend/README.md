# 后端代码目录

后续服务器代码、依赖声明、数据库迁移、模拟设备和测试均放在本目录。当前只有目录说明。

计划结构如下；下列文件尚未创建，具体职责见 [后端设计](../doc/backend-design.md)。

```text
backend/
  pyproject.toml
  .env.example
  .gitignore
  app/
    main.py
    config.py
    api/                 # HTTP 接口与浏览器状态推送
    schemas/             # 输入输出及 AI 候选结果的结构校验
    models/              # 数据库模型
    repositories/        # 查询与事务持久化
    services/            # 操作会话、库存、查询、提醒
    workers/             # 数据库任务领取、重试、提醒扫描
    adapters/
      devices/           # K230、小智协议适配
      ai/                # 多模态识别与文本意图解析
      audio/             # ASR、TTS、音频格式适配
    storage/             # 照片读写
  migrations/
  scripts/               # 初始化、模拟设备、可重复演示
  tests/
  var/                   # 运行时数据库、照片、临时文件；不入库
```

启动方式、环境变量和实际实现状态随代码一同补充。此处不提供尚不能运行的启动命令。
