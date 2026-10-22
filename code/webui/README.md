# Mind Box 网页

基于 Vite、Three.js 与 Lucide，提供物品查询、3D 柜体、摄像头预览、记录核对、语音和提醒界面。

## 运行

需要 Node.js 20.19+ 或 22.12+。在本目录执行：

```sh
npm install
npm run dev
```

默认地址为 http://127.0.0.1:5173/。完整服务的准备与启动见[软件入口](../README.md)。

## 服务连接

Vite 将库存、记录、语音、提醒和连接管理请求转发至后端，将摄像头、抽屉、音频和视觉状态请求转发至设备桥接。

| 环境变量 | 默认值 |
| --- | --- |
| `MIND_BOX_BACKEND_URL` | `http://127.0.0.1:8787` |
| `MIND_BOX_BRIDGE_URL` | `http://127.0.0.1:8765` |
| `MIND_BOX_VOICE_URL` | 与后端相同 |

模型密钥由后端管理，不放入前端配置。

## 页面

- `/`：库存、柜体、操作记录、语音与提醒。
- `/vision.html`：摄像头标记检测与打印。
- `/models.html`：柜体模型预览。

第一台柜体连接真实后端库存与设备状态；其他预览设备使用浏览器本地数据。库存识别在合柜后异步完成。麦克风输入需要浏览器授权，语音与提醒播报需要页面保持打开并允许声音。

## 构建

```sh
npm run build
npm run preview
```

构建输出在 `dist/`。部署时仍需为 `/api/` 配置相应后端代理。

素材来源见 [物品图片](public/items/SOURCES.md)与[柜体模型](public/models/SOURCES.md)。
