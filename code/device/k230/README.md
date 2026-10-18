# K230 USB 摄像头与门状态桥接

本目录是 `feat/k230-live-camera-cache` 分支的设备接入源码。原 `[local path hidden] 为开发板示例工作目录；本功能后续以这里的版本为准。

## 当前链路

```text
GC2093 持续采集 1280x720 RGB565
  -> Display.VIRT(to_ide=True)
  -> CanMV VS Code 0.9.10 的 USB JPEG 预览帧
  -> pc_door_bridge.py 的内存滚动缓存
  -> HTTP MJPEG -> WebUI 第一台柜体的摄像头区域

IO21 KEY -> MINDBOX_DOOR JSON -> 同一 USB 连接 -> SSE -> 三层柜门动画
                              -> 电脑麦克风录音 -> WAV + 开关门时间

可选视觉模式：俯拍画面 -> 电脑 ArUco 出现/消失检测 -> 同一门状态/动画/录音链路

自动记录：桥接HTTP缓存与视觉门状态 -> 独立后端持续落盘 -> 合柜后识别 -> Store库存 -> 网页
```

摄像头在柜门关闭时也持续工作，电脑也持续接收。第一台柜体任意抽屉打开时，概览面板显示画面；全部关闭后移除网页视频连接，但不停止采集和缓存。第二台柜体仍是界面预览，不映射真实摄像头。

自动记录使用`--door-source vision`，另启动[后端服务](../../backend/README.md#自动记录服务)。桥接继续保持轻量缓存，不在USB或视觉线程调用远端模型。完整时段约8fps原帧由后端归档，网页关闭不影响；本目录独立录音尚未自动关联到库存会话。

这版使用现有 CanMV 扩展的本地共享接口，不修改扩展或固件。它依赖 VS Code 保持运行，暂不是脱离 IDE 的 USB 摄像头驱动。画面帧不通过 SD 卡文件传输。

支持Windows、macOS和Linux：Windows复用扩展的命名管道，macOS/Linux使用本地UNIX socket。两种方式使用同一认证和JPEG/事件协议，不额外打开USB串口。

## 运行

电脑端需要 Python 3.9+。仅门状态/视频可用标准库；电脑麦克风录音需要安装 `requirements-audio.txt`。WebUI 依照 `../../webui/README.md` 安装依赖。

1. USB 连接 K230，保留一个启用 CanMV 的 VS Code 窗口，执行 `CanMV: Connect Board`。
2. 停止旧的录制/按键脚本，运行本目录的 `k230_key_door.py`。摄像头资源不能被两个脚本同时占用。
3. 在项目根目录首次安装录音依赖：

```sh
python3 -m venv code/device/k230/.venv
code/device/k230/.venv/bin/python -m pip install -r code/device/k230/requirements-audio.txt
```

随后每次启动桥接：

```sh
code/device/k230/.venv/bin/python -u code/device/k230/pc_door_bridge.py --record-audio
```

上面是macOS/Linux环境命令。Windows在项目根目录使用已安装视觉依赖的Python启动，首次只测画面和抽屉可以不录音：

```powershell
python -u code/device/k230/pc_door_bridge.py --door-source vision
```

Windows默认查找`%APPDATA%/Code/User/globalStorage/kendryte747.canmv-vscode`。自定义VS Code用户目录时用`--storage <扩展实际globalStorage目录>`。程序按照VS Code的路径规则匹配命名管道，包括盘符小写；不需要修改CanMV扩展。仅板端/IDE有画面不代表桥接已启动，应检查`http://127.0.0.1:8765/api/camera/state`中`connected=true`，再打开网页。

4. 在 `code/webui` 运行 `npm run dev`，打开 http://127.0.0.1:5173/。
5. KEY 按 1/2/3 次分别切换上/中/下层。每次按住约 0.1~0.2 秒，同组间隔小于 500 毫秒；最后松开约 500 毫秒后生效。摄像头快照会占用主循环时间，不保证识别极短的轻触。

板端仅设定 30fps 采集目标，实际 USB 帧率由预览通道决定，应查看 `/api/camera/state` 的 `received_fps`。脚本退出时释放 Sensor 和 Display。手动运行脚本不改板端 `boot.py` 或 `main.py`，重启后需要重新运行。

若电脑桥接未连接而 IDE 已连接，检查是否有多个 CanMV 窗口。0.9.10 的共享接口可能属于已关闭的窗口；在保留窗口执行 `Developer: Restart Extension Host`，再连接开发板。仅重新连接串口不能重建已丢失的共享接口。

## 电脑麦克风录音

K230 只发送门状态，声音来自电脑系统默认输入设备，可以是内置麦克风或外接麦克风。无需在浏览器申请麦克风权限，也不要求网页保持打开。macOS 首次录音会为启动 Python 的应用申请麦克风权限，请允许；首次等待授权会延迟录音开始，后续仍有设备启动延迟，不保证保留开门瞬间的第一个字。

- 任意一层打开时开始录音；多个抽屉打开时共用一段。
- 识别码连续消失约两秒、视觉确认最后一层关闭后立即停止并保存；确认前重新出现则继续原会话。
- 音频从开门事件开始，**没有开门前十秒的音频缓存**。视频仍持续采集并缓存最近十秒。
- 输出为 16kHz、单声道、16-bit PCM WAV，保存在 `code/device/k230/recordings/audio-<会话编号>/audio.wav`。同目录 `session.json` 保存输入设备、开关门时间、启动延迟、时长和完整性；每次使用新目录，不覆盖上次。
- 断线/心跳超时、板端重启或服务退出会结束录音，标记 `incomplete`。输入溢出也不会标记完整。单次最多录一小时，达到上限后须关闭所有抽屉再开启下一段。
- 麦克风无法打开时，网页显示“录音失败”，视频/门状态继续工作；排除权限或设备问题后，关闭所有抽屉并重新打开即可重试。

查看或选择输入设备，调整关门后的录音时间：

```sh
code/device/k230/.venv/bin/python code/device/k230/pc_door_bridge.py --list-audio-devices
code/device/k230/.venv/bin/python -u code/device/k230/pc_door_bridge.py --record-audio --audio-device 0 --audio-tail 0
```

设备编号以本机输出为准，也可传输入设备名称。`--audio-output <目录>` 更改保存目录。省略 `--record-audio` 可只启动原有的视频/门状态功能。录音文件已加入 Git 忽略；程序不会自动清理历史录音。采集使用 [sounddevice 的 RawInputStream](https://python-sounddevice.readthedocs.io/en/0.5.5/api/raw-streams.html)，与 USB 接收运行在不同线程。

## 用摄像头代替门磁

摄像头固定在柜体上方向下俯拍，抽屉识别码朝上：关闭时完全隐藏，打开后稳定可见，**每次只开一层**。基于 [OpenCV ArUco 检测](https://docs.opencv.org/4.x/d5/dae/tutorial_aruco_detection.html)，使用 `DICT_4X4_50`，编号 1/2/3 对应上/中/下层。不需要训练模型，不调用云端视觉服务。

安装依赖并启动（先停止占用 8765 端口的旧桥接）：

```sh
code/device/k230/.venv/bin/python -m pip install -r code/device/k230/requirements-vision.txt
code/device/k230/.venv/bin/python -u code/device/k230/pc_door_bridge.py --door-source vision --record-audio
```

板端继续运行 `k230_key_door.py` 持续传图。视觉模式下 KEY 的门状态仅用于跟踪板端重启，不再决定网页和录音状态；默认不加 `--door-source vision` 仍使用 KEY。`--simulate` 与视觉模式不能同时使用。

1. 打开 http://127.0.0.1:5173/vision.html （主界面设置中的“视觉识别”也能进入），点击“打印标记”。A4、原始比例 100%，关闭打印页眉页脚。三张标记的黑色方块设计为边长 50 mm，周围白边须保留，打印后用尺确认比例。原来的 1/2/3 标记可复用，参考码 0 不再使用。
2. **1 / 2 / 3** 分别朝上固定在上 / 中 / 下层抽屉上，选择不容易被手和物品遮挡的位置。确认关上后整张码被隐藏，拉开后露出完整黑边和白边。每个编号只出现一次；画面中的边长至少约 28 像素，实际建议更大并保证清晰、无反光。
3. 无需关闭位置标定。识别到唯一编号持续约 0.3 秒且至少 3 张检测帧后，网页打开对应层并开始电脑录音。识别到另一个唯一编号并确认后，按单抽屉约定切换开合层，不产生两层同时打开的状态。
4. 短暂丢码保持打开；连续 2 秒的新画面均未识别到抽屉码后，确认全部关闭并立即停止电脑录音。首次启动或重连也需观察，再确认开门或全部关闭。

`--vision-open-confirm`（默认 0.3）和 `--vision-close-delay`（默认 2）调整上述时间，必须满足 `0 < 开门确认 < 关门延时 <= 30` 秒。每秒最多分析约 10 张最新画面，工作线程不阻塞 USB 接收。旧几何阈值参数、标定 API 和标定文件读取均已移除，已有 `.vision-calibration.json` 不再生效。

- 同时看到多个编号时显示冲突，`known` 全部为 `false` 并保留上次姿态；已有录音继续，冲突不新建录音。恢复唯一编号或空画面后，重新计时确认。
- 同一张冻结画面不会累计确认；相邻检测画面间隔超过 0.5 秒时重置确认计时。超过 1.5 秒无新画面或 USB 中断时，门状态离线，录音按不完整会话收尾，不判成关门。
- **持续遮挡超过关门延时仍可能误判关闭**。这一规则依赖“码消失代表关闭”的安装条件，无法区分长期遮挡、相机挪开和真实关闭。需要调整贴码位置或增加关门延时，不能当成门磁同等精度的量测。开门确认和麦克风启动也会延迟录音起点，仍没有音频预录。

识别页通过 `/api/camera/mjpeg` 持续显示原始视频，与标记识别独立运行；每 200 毫秒读取识别状态并叠加角点，超过 500 毫秒的检测标注自动清除。页脚分别显示相机接收和识别帧率，识别速度不会限制视频刷新。

`/api/vision/state` 返回 `mode=marker_visibility`、每层 `open` / `known`、可见标记的 `corners`、`pending_drawer`、`pending_remaining_ms`、确认时间，以及 `camera_connected`、`camera_epoch`、`camera_fps`、`processing_fps`、`processing_ms`、`observation_age_ms`；后者是检测所用画面距电脑当前时间的毫秒数。`/api/vision/preview.jpg` 保留为带编号的单张预览兼容接口，识别页不轮询它。`/api/vision/markers/1.png` 至 `3.png` 是打印图片。视觉模式仅有一个权威门状态，板端 KEY 心跳不会覆盖它。

## 缓存与接口

HTTP 只监听 `127.0.0.1:8765`；Vite 将 `/api/doors`、`/api/camera`、`/api/audio` 和 `/api/vision` 转发到它。改端口时，同时设置 WebUI 的 `MIND_BOX_BRIDGE_URL`。

| GET 路径 | 返回 |
| --- | --- |
| `/api/doors/state` | 门状态和连接状态 |
| `/api/doors/events` | SSE 完整门状态，重连后自动同步 |
| `/api/camera/state` | 在线状态、实际帧率、缓存帧数/字节数/时长、最新帧编号 |
| `/api/camera/mjpeg` | 浏览器实时画面，只发最新帧，慢客户端不会积压 USB 帧 |
| `/api/camera/latest.jpg` | 当前最新 JPEG，断流后返回 404 |
| `/api/camera/cache` | 当前窗口内各帧编号、到达时间、大小和流 `epoch` |
| `/api/camera/frames/<generation>.jpg` | 按电脑帧编号取 JPEG，淘汰或重连后返回 404 |
| `/api/audio/state` | 电脑录音启用状态、当前会话、最近文件位置和错误 |

缓存按电脑单调时钟的**收到帧时间**保留最近 10 秒，同时上限 64 MiB、1200 帧；达到任意上限都淘汰最旧帧。因此启动不足 10 秒、断流或高码率下，实际覆盖可能不足 10 秒，应检查 `buffered_seconds`。`received_unix_ms` 便于和其他电脑事件关联，不是相机曝光时间。`frame_id` 来自扩展，`generation` 是电脑服务内递增编号；重连/板端重新启动会清空缓存并更换 `epoch`。

后端持续读取缓存，并在开门时归档**前2秒＋全过程＋关门后2秒**的约8fps JPEG与时间轴；桥接自身仍保留10秒滚动窗口，不生成MP4。电脑音频在开门时启动、视觉确认关门后立即结束。完整WAV由`/api/audio/sessions`列出，经`/api/audio/sessions/<id>/audio.wav`下载到同一视频会话；统一后端等待库存提交后转写、理解讲话和设置提醒。运行方式见[后端说明](../../backend/README.md#统一语音文字与提醒)。

7 秒没有画面或门心跳即显示离线，不以最后一帧冒充实时画面。USB 预览流只有图像，声音来自电脑麦克风。二者在同一服务中记录电脑单调时钟，但音频起点是打开输入流后的估计值，未做设备级同步校准，尚不生成带音轨的视频。本轮不调用识别模型，不写正式库存；正式处理接口仍以 `../../doc/data-contract.md` 为准。

## 验证

2026-10-17 真机验证：01Studio CanMV K230、GC2093、CanMV v1.8、VS Code CanMV 0.9.10。USB 收到的 JPEG 为 1280x720，约 28.7~29.3fps；热身后约 290 帧覆盖 9.96 秒，当前场景缓存约 34~36 MB。KEY 单击开/关第一层已验证，关门后接收继续。桌面 1440x1000 和手机 390x844 均显示真实、持续变化的画面，无横向溢出；第二台预览柜体不会显示真实相机。以上是当前场景实测，不是长时间稳定性或音画同步结论。

```sh
python3 -m unittest discover -s code/device/k230 -p 'test_*.py' -v
```

测试覆盖多击/消抖、门状态去重/重启、SSE 恢复、十秒淘汰、字节/帧数上限、JPEG 读取过期、MJPEG 更新/断流，以及 USB 图像和脚本输出交错。`pc_door_bridge.py --simulate --port 8766` 只模拟门状态，不伪造相机画面；正式桥接不开放模拟开门写接口。

电脑录音另有多层共享、关门确认、零尾音、断线/重启、权限失败、WAV 内容、录音上限和服务退出测试。2026-10-17 使用模拟门事件和真实 MacBook Pro Microphone 验证：16kHz 单声道 WAV 包含非零声音采样，音频溢出为零；尚未完成这轮音频与 K230 实体 KEY 的联合实测。

视觉测试使用合成 JPEG 和真实 OpenCV 标记检测，覆盖三个打印编号回读、可见开门/消失关门的确认、短暂遮挡、单层切换、多码冲突、冻结画面、检测中断、重连，以及门状态与录音的联动。无视觉依赖时跳过该组测试；完整验证请使用安装好上述依赖的 `.venv/bin/python`。这些测试不能替代贴好标记后的真实柜体验证。

2026-10-17 出现/消失版本：完整设备侧回归 62 项和前端构建通过。合成标记经真实 OpenCV 检测器验证了网页第一层开门、多个编号冲突保留姿态、切换第二层、消失延时关门和约 9.95 秒持续缓存；桌面 1440x1000、手机 390x844 无横向溢出，Three.js 截图像素非空，第一层实际模型位置从 0 到 1.3。打印页仅三张编号 1/2/3。更新服务时共享接口报告开发板未连接，尚未验证新安装角度下的实体柜体开合及真实麦克风联动。

随后用户确认当前识别可以正常使用。此确认覆盖现场识别可用性；尚无俯拍识别与真实麦克风的完整联合测试记录或长时间稳定性结果。

2026-10-17 Windows接入：补齐命名管道传输和VS Code路径散列规则，真实CanMV连接两秒收到60张JPEG；启动视觉桥接后约29fps、1280x720，十秒缓存、网页代理和后端采集均在线，实际抽屉状态变化已触发会话落盘。既有62项设备测试及新增4项Windows原生管道测试通过，新增覆盖认证、大帧分片与事件交错、超时取消后恢复、断线EOF和路径规则。本机自动识别密钥尚未配置，已采集会话保留为失败，不能据此宣称实柜模型入库验收通过；本轮未测试麦克风。
