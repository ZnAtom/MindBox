# K230 摄像头桥接

将 K230 摄像头画面、抽屉状态与电脑麦克风连接到 Mind Box。电脑通过 CanMV VS Code 扩展的共享接口接收 JPEG；使用时需要保持 VS Code 和开发板连接。

## 硬件与软件

- CanMV K230、GC2093 摄像头和 USB 连接。
- 已安装 CanMV 扩展的 VS Code。
- Python 环境及本目录的 `requirements-vision.txt`、`requirements-audio.txt`；中文唤醒另需 `requirements-wake.txt`。
- 完整服务的环境准备见[后端说明](../../backend/README.md)。

## 连接设备

1. 在 VS Code 执行 `CanMV: Connect Board`。
2. 在开发板上运行本目录的 `k230_key_door.py`。
3. 从仓库根目录使用[统一启动脚本](../../README.md)，或手动启动桥接：

```sh
python code/device/k230/pc_door_bridge.py --door-source vision --record-audio
```

桥接默认监听 `http://127.0.0.1:8765`。查看 `/api/camera/state` 的 `connected` 与帧率确认连接。Windows 使用命名管道，macOS/Linux 使用本地 socket；无需另外打开 USB 串口。

## 抽屉状态

在网页 `/vision.html` 打印 ArUco 标记，编号 1、2、3 对应上、中、下层抽屉。摄像头向下俯拍，标记应在抽屉打开时可见、关闭时隐藏，每次只打开一层。

唯一标记持续约 0.3 秒确认打开；连续约 2 秒无抽屉标记确认关闭。长时间遮挡也可能触发关闭判断，需要确保安装位置、照明和视野稳定。断流显示离线，不使用旧画面确认关门。

不加 `--door-source vision` 时使用板端 KEY 状态：连续按 1、2、3 次分别切换对应抽屉。

## 录音与唤醒

`--record-audio` 使用电脑系统麦克风，音频从开门确认开始，关门确认后结束。它没有开门前音频预录，USB 摄像头本身不提供声音。

在仓库根目录下载中文唤醒模型：

```sh
python code/device/k230/download_wake_models.py
```

手动启动桥接时加 `--wake-word '你好小盒'` 可启用唤醒；统一启动器默认启用。使用 `MIND_BOX_WAKE_ENABLED=0` 可关闭启动器的唤醒功能。语音回复由网页播放。

## 常用接口

| 路径 | 用途 |
| --- | --- |
| `/api/camera/state` | 连接、帧率和缓存状态 |
| `/api/camera/mjpeg` | 实时画面 |
| `/api/doors/state` | 抽屉状态 |
| `/api/doors/events` | 状态事件流 |
| `/api/vision/state` | 标记与确认状态 |
| `/api/audio/state` | 录音状态 |

如果 IDE 有画面但桥接未连接，关闭多余 CanMV 窗口，在保留窗口重启 Extension Host 后重新连接开发板。

## 测试

从仓库根目录运行：

```sh
python -m unittest discover -s code/device/k230 -p 'test_*.py'
```
