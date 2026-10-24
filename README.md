# Mind Box

**物品无感智能收纳。物品放取过程自动记录，想找物品时，询问即可定位。**

[English version](README_en.md) · [演示视频](docs/media/demo.mp4) · [项目路演 PPT](docs/presentation/MindBox.pptx) · [联系与交流](#contact)

## 项目设想

日常收纳有一个小小的矛盾：人的注意力有限，要管理的物品却越来越多。物品放进抽屉之后，我们可能很快忘记放了什么、放在哪里，也会忘记主动交代的提醒。Mind Box 希望让收纳过程自然地留下记录，把注意力留给更重要的事情。

使用者像往常一样取放物品。摄像头观察过程，软件识别物品与变化，并将记录和所在抽屉关联。之后可以查询物品位置、用语音交代信息或设定提醒。理想的体验是：善于收纳的人可以更有条理，不擅长收纳的人也不必先学会维护一份清单。

这个设想也来自我对万物互联生活的想象。AI 和多模态模型有机会处理没有预先定义的物品与问题，让物品记录成为个人 AI 理解现实生活的一部分。项目已实现带认证的只读查询 API；未来，像 Muse 这样的个人 Agent 可以在获得授权后读取物品、位置和取放记录。具体第三方 Agent 的接入仍有待验证。

> 让我们把繁杂的交给 AI，把情感、创造和生活留给人类。

## 当前原型

仓库包含一套软件原型和 K230 摄像头设备接入代码。电脑端桥接采集摄像头画面，以 ArUco 视觉标记判断抽屉状态；后端保存收纳过程，分段分析图像并将经过核对的取放结果写入 SQLite；网页展示物品、位置、证据画面和操作状态。语音入口可查询库存、补充信息和创建一次性或每日提醒。带认证的个人 Agent API 当前为本机只读查询接口。

当前触发链路依赖图像识别：视觉标记负责判断抽屉开合，并触发或结束一次收纳会话；图像模型负责分析物品和取放动作。这是正在迭代的原型，不代表通用识别已经解决，也不意味着识别必定实时完成。

Mind Box 仍处于原型阶段，完整硬件体验正在完善。识别结果不足或证据不完整时，系统会保留待核对状态。

## 演示与路演

<a id="frontend-demo"></a>

### 前端演示

![Mind Box 前端交互演示](docs/media/frontend-demo.gif)

- [演示视频](docs/media/demo.mp4)
- [项目路演 PPT](docs/presentation/MindBox.pptx)
- [安装与运行说明](code/README.md)

PPT 中展示的柜体界面和部分 3D 柜体画面属于项目原型素材；概念展示不等于下一代实体产品已经完成。

## 下一步开发

1. **优化识别算法。** 改进物品身份关联、取放动作判断、遮挡与异常状态处理，并使用留出样本持续评估结果。
2. **探索下一代工业设计。** 评估更适合日常使用的整体形态；一个候选方向是把摄像头放在抽屉内部，研究视野、照明、安装、供电和维护条件。它仍是产品设想，尚未定型。
3. **改进触发器。** 目前主要依靠图像识别来判断抽屉状态和启动收纳流程。下一步比较更稳定、响应及时的触发方式，并降低误触发和漏触发。
4. **继续优化网站前端。** 改进物品定位、识别过程反馈、记录核对和移动端使用体验。
5. **验证 Agent API 与真实设备闭环。** 完成实体柜体上的存取、断流恢复和语音联动测试，再逐步验证经授权的个人 Agent 接入。

## 代码结构

```text
.
├── README.md          # 中文项目介绍、开发路线与运行说明
├── README_en.md       # English project overview and development roadmap
├── LICENSE            # MIT License
├── code/
│   ├── backend/       # Python API、识别流程、SQLite、语音和 Agent API
│   ├── device/k230/   # K230 板端脚本与电脑端摄像头桥接
│   ├── scripts/       # 本地开发环境启动脚本
│   ├── webui/         # Vite 前端、库存界面与 3D 模型预览
│   │   └── src/       # 前端 JavaScript 和 CSS 源码
│   ├── modeling/      # 柜体 3D 模型工作流与结果
│   └── datasets/      # 回归数据与标注模板
└── docs/              # 路演文稿、演示视频与项目图片
    ├── media/
    └── presentation/
```

`data/` 是供本地实验使用的输入目录，原始视频受 `.gitignore` 规则保护，不应直接提交。

## 本地运行

完整依赖准备、硬件连接和故障排查请参阅[运行入口](code/README.md)、[后端说明](code/backend/README.md)和[K230 设备说明](code/device/k230/README.md)。

在 Windows 项目根目录启动已配置好的本地开发环境：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\code\scripts\mindbox.ps1 start
```

默认网页地址为 `http://127.0.0.1:5173/`。启动完整设备链路还需要配置 Python 和 Node.js 环境、模型 API 凭据，并让 VS Code CanMV 连接 K230 开发板。没有开发板时，可以单独运行网页查看界面。

在 `code/webui` 构建前端：

```sh
npm install
npm run build
```

在 `code/backend` 运行后端测试：

```sh
python -m pytest -q
```

<a id="contact"></a>

## 联系与交流

欢迎对这个想法感兴趣的朋友随时来交流、讨论产品方向或参与下一步探索。可以通过 [GitHub Issues](https://github.com/ZnAtom/MindBox/issues) 留言；也欢迎提交改进建议或分享相关经验。

本项目以 [MIT License](LICENSE) 开源。仓库中的第三方图片、模型和其他素材可能受各自来源许可约束，使用前请查看对应的来源说明。

## 致谢

感谢上海科技大学黑客松提供的创造与展示机会，感谢所有主办方，感谢上海联育孵化器提供比赛奖金。奖金将投入下一代产品研发。也感谢每一位观看演示、提出问题并帮助我们完善设想的朋友。

<a id="hackathon-photos"></a>

<table>
  <tr>
    <td width="50%" align="center"><img src="docs/media/hackathon-event.jpg" width="100%" alt="上海科技大学黑客松活动现场"></td>
    <td width="50%" align="center"><img src="docs/media/team-award.jpg" width="100%" alt="团队获得的二等奖及联育 DeepTech 奖"></td>
  </tr>
  <tr>
    <td align="center">黑客松活动现场</td>
    <td align="center">二等奖与联育 DeepTech 奖</td>
  </tr>
</table>
