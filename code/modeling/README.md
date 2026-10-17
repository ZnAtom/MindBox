# 柜体 AI 建模工作流

用途：从柜体视频提取参考照片，调用影眸 Rodin 生成简化 GLB，下载后统一成无纹理的中性材质。可以串行处理多个柜体，保留任务记录供中断后继续。

本轮真实调用已经成功，生成任务实际消耗 0.5 credits。第一版为 9,532 个三角面、约 342 KB、一个材质、没有纹理。预览入口为 WebUI 的 `models.html`。这是真实 AI 输出，不是原有手工柜体的替代截图；尺寸和抽屉结构还需要核验。

Bang 文件上传拆分也已完成，实际另消耗 0.5 credits，输出 8 个部件、10,000 个三角面。逐件检查后，部件结构不满足抽屉独立运动要求，当前只作为部件试验预览，详见 `RESULTS.md`。

## 流程

```text
原视频
  → 按时间抽帧、读取手机旋转信息并校正方向
  → 生成带时间戳的联系表
  → 人工选定同状态的互补视角、柜体范围和必要的背景遮罩
  → 形成每个柜体的 JSON 配置
  → 裁剪、保留比例、统一留白
  → Rodin：GLB、Raw 三角面、目标 10,000 面、material=None
  → 保存任务编号，查询全部子任务直到完成
  → 下载模型，保留原始文件
  → 统一不透明灰色材质，统计实际面数和文件大小
  → 人工检查外形；需要时单独调用 Bang 拆分
  → 在浏览器检查结构，合格后再绑定抽屉动画
```

自动化部分是抽帧、裁剪、提交、查询、下载和材质处理。**当前不会自动保证选帧正确，也不会自动保证分出三只完整抽屉。** 新柜体需要先核选视角和边界；`cabinet-v1.json` 里的时间点和遮罩只适用于这次视频。

## 运行

以下命令从仓库根目录运行。当前可直接复用 `code/backend/.venv/Scripts/python.exe`，新环境依赖见本目录 `requirements.txt`。

密钥只通过进程环境变量 `RODIN_API_KEY` 传入；不要放进 JSON、命令参数、代码、截图或版本库。可以在 PowerShell 以隐藏输入方式设置：

```powershell
$env:RODIN_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host '临时密钥' -AsSecureString)).Password
```

先查看素材，以下步骤不调用远程接口：

```powershell
code/backend/.venv/Scripts/python.exe code/modeling/workflow.py inspect data/3d --interval 0.65
code/backend/.venv/Scripts/python.exe code/modeling/workflow.py prepare code/modeling/cabinet-v1.json
```

查看 `code/modeling/var/review/` 中的联系表，再检查 `var/inputs/cabinet-v1/` 中实际待上传的照片。配置中的 `crop` 和 `mask` 坐标以旋转校正后、长边最多 1600 像素的帧为准，不是原始编码方向。

验证认证并提交一份模型：

```powershell
code/backend/.venv/Scripts/python.exe code/modeling/workflow.py balance
code/backend/.venv/Scripts/python.exe code/modeling/workflow.py submit code/modeling/var/inputs/cabinet-v1/prepared.json
code/backend/.venv/Scripts/python.exe code/modeling/workflow.py finish code/modeling/var/tasks/cabinet-v1/task.json
```

批量生成时传入多份已经核对的配置。每份必须使用不同 `id`；`batch` 会先准备图片，再串行提交、等待、下载和统一材质：

```powershell
code/backend/.venv/Scripts/python.exe code/modeling/workflow.py batch code/modeling/cabinet-v1.json path/to/another-cabinet.json
```

同一份配置和照片会复用已有任务。配置或照片改变后要使用新的 `id`，不会悄悄覆盖原任务。提交超时会留下 `unconfirmed` 记录并停止，不能据此判断没有扣费，也不会自动重新提交。已经明确拒绝的请求记录为 `rejected`。`finish` 只续查、下载和处理，不创建新的生成任务。

## 拆分与显示

拆分是另一次付费调用。首轮按资产编号调用 Bang 返回 `ASSET_NOT_AVAILABLE`，改用下载的模型文件加参考图片后成功受理：

```powershell
code/backend/.venv/Scripts/python.exe code/modeling/workflow.py submit code/modeling/var/inputs/cabinet-v1/prepared.json --split-model code/modeling/var/tasks/cabinet-v1/downloads/base.glb
code/backend/.venv/Scripts/python.exe code/modeling/workflow.py finish code/modeling/var/tasks/cabinet-v1-split-upload/task.json
```

上传模式在使用拆分指令时仍需附参考图，即使不生成材质。指令要求固定框架与上、中、下三只完整抽屉，共四组；输出是否满足必须检查，不能用网格数量直接判断。目标面数是所有部件合计，不是每个部件各一万面。

统一材质的文件输出到 `var/results/<任务>/`。如需发布到预览目录，可使用：

```powershell
code/backend/.venv/Scripts/python.exe code/modeling/workflow.py style path/to/source.glb code/webui/public/models/my-cabinet.glb
```

再在 `code/webui/public/models/catalog.json` 添加该模型的路径和说明。预览支持旋转、缩放、网格查看、双主题、下载与参考照片对照。模型停止操作后不持续渲染。现有储物界面的手工模型和取放动画继续保留。

任务响应和任务订阅凭证在被忽略的 `var/` 中保存；原始 API 密钥不保存。模型下载使用独立、不带认证头的请求，不向签名存储地址转发 API 密钥。生成 POST 不自动重试，状态查询有等待上限。余额查询不需要输出账户历史。

## 测试与依据

```powershell
Set-Location code/modeling
../backend/.venv/Scripts/python.exe -m unittest test_workflow -v
```

测试覆盖重复计费防护、提交结果不确定时停止、HTTP 成功但业务失败、不完整任务拒绝下载、文件上传拆分、输入数量限制，以及统一材质时保留原始几何。

- [Rodin Gen-2.5](https://docs.hyper3d.ai/en/api-specification/rodin-gen2-5)
- [Bang](https://docs.hyper3d.ai/en/api-specification/bang)
- [状态查询](https://docs.hyper3d.ai/en/api-specification/check-status)
- [结果下载](https://docs.hyper3d.ai/en/api-specification/download-results)
- [余额查询](https://docs.hyper3d.ai/en/api-specification/check-balance)
