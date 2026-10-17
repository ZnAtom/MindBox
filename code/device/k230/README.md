# K230 USB 摄像头与门状态桥接

本目录是 `feat/k230-live-camera-cache` 分支的设备接入源码。原 `/Users/cao/K230/31-Mind-Box/` 为开发板示例工作目录；本功能后续以这里的版本为准。

## 当前链路

```text
GC2093 持续采集 1280x720 RGB565
  -> Display.VIRT(to_ide=True)
  -> CanMV VS Code 0.9.10 的 USB JPEG 预览帧
  -> pc_door_bridge.py 的内存滚动缓存
  -> HTTP MJPEG -> WebUI 第一台柜体的摄像头区域

IO21 KEY -> MINDBOX_DOOR JSON -> 同一 USB 连接 -> SSE -> 三层柜门动画
```

摄像头在柜门关闭时也持续工作，电脑也持续接收。第一台柜体任意抽屉打开时，概览面板显示画面；全部关闭后移除网页视频连接，但不停止采集和缓存。第二台柜体仍是界面预览，不映射真实摄像头。

这版使用现有 CanMV 扩展的本地共享接口，不修改扩展或固件。它依赖 VS Code 保持运行，暂不是脱离 IDE 的 USB 摄像头驱动。画面帧不通过 SD 卡文件传输。

## 运行

电脑端仅需 Python 3.9+ 标准库，WebUI 依照 `../../webui/README.md` 安装依赖。

1. USB 连接 K230，保留一个启用 CanMV 的 VS Code 窗口，执行 `CanMV: Connect Board`。
2. 停止旧的录制/按键脚本，运行本目录的 `k230_key_door.py`。摄像头资源不能被两个脚本同时占用。
3. 在项目根目录启动桥接：

```sh
python3 -u code/device/k230/pc_door_bridge.py
```

4. 在 `code/webui` 运行 `npm run dev`，打开 http://127.0.0.1:5173/。
5. KEY 按 1/2/3 次分别切换上/中/下层。每次按住约 0.1~0.2 秒，同组间隔小于 500 毫秒；最后松开约 500 毫秒后生效。摄像头快照会占用主循环时间，不保证识别极短的轻触。

板端仅设定 30fps 采集目标，实际 USB 帧率由预览通道决定，应查看 `/api/camera/state` 的 `received_fps`。脚本退出时释放 Sensor 和 Display。手动运行脚本不改板端 `boot.py` 或 `main.py`，重启后需要重新运行。

若电脑桥接未连接而 IDE 已连接，检查是否有多个 CanMV 窗口。0.9.10 的共享接口可能属于已关闭的窗口；在保留窗口执行 `Developer: Restart Extension Host`，再连接开发板。仅重新连接串口不能重建已丢失的共享接口。

## 缓存与接口

HTTP 只监听 `127.0.0.1:8765`；Vite 将 `/api/doors` 和 `/api/camera` 转发到它。改端口时，同时设置 WebUI 的 `MIND_BOX_BRIDGE_URL`。

| GET 路径 | 返回 |
| --- | --- |
| `/api/doors/state` | 门状态和连接状态 |
| `/api/doors/events` | SSE 完整门状态，重连后自动同步 |
| `/api/camera/state` | 在线状态、实际帧率、缓存帧数/字节数/时长、最新帧编号 |
| `/api/camera/mjpeg` | 浏览器实时画面，只发最新帧，慢客户端不会积压 USB 帧 |
| `/api/camera/latest.jpg` | 当前最新 JPEG，断流后返回 404 |
| `/api/camera/cache` | 当前窗口内各帧编号、到达时间、大小和流 `epoch` |
| `/api/camera/frames/<generation>.jpg` | 按电脑帧编号取 JPEG，淘汰或重连后返回 404 |

缓存按电脑单调时钟的**收到帧时间**保留最近 10 秒，同时上限 64 MiB、1200 帧；达到任意上限都淘汰最旧帧。因此启动不足 10 秒、断流或高码率下，实际覆盖可能不足 10 秒，应检查 `buffered_seconds`。`received_unix_ms` 便于和其他电脑事件关联，不是相机曝光时间。`frame_id` 来自扩展，`generation` 是电脑服务内递增编号；重连/板端重新启动会清空缓存并更换 `epoch`。

后端可以先取缓存索引，再立即读取需要的帧。读取期间旧帧仍会正常淘汰，404 应按过期处理。这版缓存是内存窗口，**尚不归档整段开门会话、不生成 MP4，也不包含音频**。后续会话录制需要在开门时固定前 10 秒，并追加开门期间和关门后帧；不能在关门后才回读整个滚动窗口。

7 秒没有画面或门心跳即显示离线，不以最后一帧冒充实时画面。预览流只有图像，没有声音、设备采样时间戳或音画同步保证。本轮不调用识别模型，不写正式库存；正式处理接口仍以 `../../doc/data-contract.md` 为准。

## 验证

2026-10-17 真机验证：01Studio CanMV K230、GC2093、CanMV v1.8、VS Code CanMV 0.9.10。USB 收到的 JPEG 为 1280x720，约 28.7~29.3fps；热身后约 290 帧覆盖 9.96 秒，当前场景缓存约 34~36 MB。KEY 单击开/关第一层已验证，关门后接收继续。桌面 1440x1000 和手机 390x844 均显示真实、持续变化的画面，无横向溢出；第二台预览柜体不会显示真实相机。以上是当前场景实测，不是长时间稳定性或音画同步结论。

```sh
python3 -m unittest discover -s code/device/k230 -p 'test_*.py' -v
```

测试覆盖多击/消抖、门状态去重/重启、SSE 恢复、十秒淘汰、字节/帧数上限、JPEG 读取过期、MJPEG 更新/断流，以及 USB 图像和脚本输出交错。`pc_door_bridge.py --simulate --port 8766` 只模拟门状态，不伪造相机画面；正式桥接不开放模拟开门写接口。
