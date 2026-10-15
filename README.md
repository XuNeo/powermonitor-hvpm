# powermonitor-hvpm

Monsoon HVPM 功耗监测桌面工具：Python 3 + PySide6，本地离线运行。支持无硬件的确定性模拟模式，以及通过内置（vendored）的官方 PyMonsoon SDK 连接真实 HVPM，完成供电控制、实时电流波形、时间窗口统计、原始录制保存、离线回放和 CSV 导出。电压和功率仍保留在原始数据、统计和 CSV 中，不再单独绘制波形。

通信所需的 PyMonsoon SDK 已按固定上游 commit 私有内置进本项目包内，安装本分发即可使用，无需再从 GitHub 安装任何 `Monsoon` 包，运行环境中也不存在外部顶层 `Monsoon` 命名空间。内置范围、相对上游的机械性改动和许可证事实见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

首版仅覆盖 HVPM 主通道。不包含 LVPM、USB/Aux 通道控制、触发采集、PT5 兼容、固件升级和远程控制。

## 状态

- Linux 模拟模式与自动化已验证：供电控制、5 kHz 录制、单电流波形、窗口统计、CSV 和回放；具体结果见 [验证记录](docs/validation-results.md)。
- macOS 模拟模式（simulator）的软件验证已通过；真实硬件的 USB 联调在 macOS 上仍未实测，见下文平台说明。
- 真实 HVPM：适配按固定 SDK 版本实现，实物验收尚未进行，见 [硬件验收清单](docs/hardware-validation.md)。

不要把模拟结果当作实机结果。

## 环境要求

- Linux（参考环境 Ubuntu 22.04，glibc 2.35）或 macOS。
- Python 3.10 至 3.13。
- 连接真实硬件需要系统提供 libusb-1.0 运行库及（Linux 上）相应 udev 权限；模拟模式不需要任何 USB 库。SDK 经 pyusb 访问设备，pyusb 已作为正式依赖随包安装，但它只是 libusb 的 Python 绑定，libusb 共享库本身由操作系统提供，不通过 pip 安装。
- 依赖建议预留 1.5 GB；原始录制每小时约 792 MB，CSV 通常更大。
- Linux 桌面 Qt 需要 X11/Wayland 运行库；本机 X11 缺少 `libxcb-cursor.so.0`，已通过项目虚拟环境内解压的发行版 `libxcb-cursor0` 包验证，不修改系统。常规桌面安装可由系统包管理器提供该库。
- PySide6 6.8.3 的 macOS wheel 要求 macOS 12 或更高，x86_64 与 ARM64 均有 wheel；Linux wheel 为 manylinux_2_28，要求 glibc 不低于 2.28。

## 安装

### 用户主路径（PyPI）

```sh
pip install powermonitor-hvpm
```

安装后直接运行命令：

```sh
powermonitor --demo
```

分发包名为 `powermonitor-hvpm`，安装提供的命令仍为 `powermonitor`。上述命令安装的是发布到 PyPI 的正式依赖（numpy、PySide6、pyqtgraph、pyusb），不含外部 `Monsoon` 包，也不需要访问 github.com。

### 开发者 / 从源码运行

推荐使用 uv，在项目根目录执行：

```sh
uv sync --extra test
```

然后所有命令都通过 `uv run` 执行，例如：

```sh
uv run python -m powermonitor --demo
uv run python tools/validate.py
```

此时模拟模式功能完整；内置 SDK 的协议、生命周期和版本固定测试无需实物即可运行，只有真实 USB/供电验收需要 HVPM。发布打包工具放在独立的 `release` dependency group（build、twine），仅在构建分发包时需要：

```sh
uv run --group release python -m build
uv run --group release twine check dist/*
```

如不用 uv，也可以手动 venv：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -e '.[test]'
```

不要安装 PyPI 上的 `monsoon` 包：本项目不依赖它，内置 SDK 位于本项目包内，额外安装同名外部包没有作用，还可能造成命名混淆。

### Linux USB 权限（仅真实硬件）

内置 SDK 通过 pyusb 访问 USB VID 0x2AB9 / PID 0x0001。系统通常已自带 libusb-1.0；若发行版未提供，用发行版自己的包管理器安装，不使用 sudo pip。

默认 udev 规则下非 root 用户通常无法直接打开设备。最小权限做法是新增一条只匹配该 VID/PID 的 udev 规则，把设备节点授权给一个专用用户组（如 `plugdev`，前提是当前用户已在该组），例如：

```text
SUBSYSTEM=="usb", ATTRS{idVendor}=="2ab9", ATTRS{idProduct}=="0001", MODE="0660", GROUP="plugdev"
```

安装或重载规则属于系统级权限变更，接入实物时确认后执行；不要长期以 root 运行应用。规则生效后重新插拔设备。macOS 不使用 udev，通常需安装系统 libusb；该平台的真实 USB 联调仍待验证。

## 启动

安装后启动桌面窗口（命令入口）：

```sh
powermonitor --demo                 # 模拟模式，无硬件，默认入口
powermonitor --open 录制目录         # 离线打开已有 .pmcap 录制
```

从源码运行时把 `powermonitor` 换成 `python -m powermonitor`：

```sh
uv run python -m powermonitor --demo
uv run python -m powermonitor --open 录制目录
```

本工作区也可用 `tools/run-local.sh --demo`，它优先使用 uv；没有 uv 时才回退到已有 `.venv`。模拟模式不会访问 USB，也不会给任何物理端口上电。真实 HVPM 必须在界面中手动扫描并连接。

使用流程：Scan → Connect →（真实设备先 OFF）→ Apply target → 确认 Enable → RUN。波形页只显示电流：普通滚轮缩放时间轴，Shift+滚轮缩放电流轴，左键拖框只放大 X 时间轴并把该范围作为统计选区。图上缩放不会取消 Follow latest，实时窗口会保留缩放宽度并继续锚定最新数据；右键逐级恢复之前的 X/Y 缩放。只有手动取消 Follow 或在采集中输入固定数值选区时，视图才冻结；重新勾选后立即跳回最新数据。实时采样/录制保持 5 kHz，波形展示在 macOS 上按 2 Hz 刷新以避免 Retina/Cocoa 持续重绘卡顿。采集中的完整历史直接从正在增长的 `.pmcap/samples.bin` 只读映射，recent 10 秒缓存不再限制 X 轴；STOP 后无缝切换到最终回放并保留当前 X/Y、选区和右键缩放历史。

电流波形按示波器方式自适应渲染，且只作用于屏幕显示，不改动采集、录制、统计或导出的任何数据：

- 放大到可见样本不超过约两个绘图区逻辑像素列一个样本时进入 raw 模式：主线逐点连接全部原始有效样本，只在缺口和无效值处断开，不平滑、不抽点。
- 样本更密时进入 aggregate 模式：每个逻辑像素列内，主线画该列有效样本的 mean 趋势点，叠加一条同色淡细线画的垂直包络，从该列真实 min 到真实 max；包络按点对绘制，列与列之间不连接。单样本尖峰由包络保留，mean 只表达该时间列的真实平均，不替代尖峰。
- 同一列内若被 GAP_BEFORE、超过 0.2 ms 容差的时间跳变、NaN/Inf 或 INVALID/DEVICE_ERROR 标记分开，会拆成独立聚合组，趋势线和包络都不跨缺口；LOSS_UNKNOWN 不隐藏当前值。首尾部分列只按实际落入样本计算，空列不画。
- 主线和包络都显式关闭 pyqtgraph 的 peak 二次降采样和视口裁剪；缩放或平移后按新的原始视口重新聚合，而不是放大旧包络。因此缩小视图是“平均趋势 + 真实范围”，放大后自动看到全部原始点，不再把每个抽取桶的 min/max 极值按时间连成原始信号中不存在的锯齿折线。图下常显平均电流/功率；完整统计与原始表格分别在 Statistics / Raw samples。STOP 只停采集，OFF 单独关断；录制路径始终显示，结束后自动回放，未主动删除录制。

无硬件验证用统一入口（自动设置 offscreen Qt 平台，顺序执行 ruff、应用源码 Pyright、pytest；依赖一致性另由 `uv pip check` 验证）：

```sh
uv run python tools/validate.py
```

该入口包含离屏 Qt 交互测试；原生 X11 窗口的端到端验证命令如下（需 Xvfb 及 Qt X11 依赖）：

```sh
QT_QPA_PLATFORM=xcb xvfb-run -a -s '-screen 0 1440x1100x24' \
  uv run python tools/gui_smoke.py artifacts/gui-run
```

目标目录必须不存在。脚本仅操作模拟器，真实点击供电确认、采样与导出控件，保存浅/深色波形、统计和数据表截图；添加 `--seconds 600` 可执行十分钟窗口耐久测试。

一个用于复现实验环境的参考冻结版本见 `requirements-linux.lock.txt`（Linux x86_64 / Python 3.10，仅供参考，不参与包构建；macOS 需另行验证并锁定自己的版本）。

## 供电安全模型（使用前必读）

- 连接、重连、打开文件、开始采集都不会自动上电。
- 真实设备连接后输出状态为未知：必须先显式关断一次，应用才允许重新使能输出。
- 设置电压只是暂存目标值，不改变设备输出；使能需要在界面确认目标电压后才下发。
- 允许软件范围 0.80 至 13.50 V，0.01 V 步进，越界值直接拒绝。软件允许不等于待测板可承受：接入 DUT 前先确认待测板安全电压。
- RUN/STOP 只控制采集，不停电；关断按钮独立且始终可达。采集中按关断会先停止采集、退出采样模式，再下发 0 V，且不自动恢复采集。
- 关断或通信失败时输出状态按未知处理，不假定已断电。正常退出只关闭本次应用使能的输出，不替其他来源的输出负责。
- 详细硬件接线与验收步骤见 [硬件验收清单](docs/hardware-validation.md)。

## 数据与时间轴说明

- 内部单位统一为秒、安培、伏特；SDK 上报电流为毫安，适配层换算为安培。
- SDK 时间戳是主机收到 USB 包的秒级时间，同一包内所有样本相同。应用同时保存该原值（host_elapsed_s）和按样本序号、标称 5 kHz 重建的时间轴（time_s，相邻样本 0.2 ms）。重建时间不是硬件采样时间，界面与导出均按此标注。
- 录制格式为目录形式的 `.pmcap`：metadata.json、samples.bin、events.jsonl，不使用 pickle。异常退出的录制带不完整标记，回放时提示尾部截断。
- 选区为左闭右开区间，统计只基于原始样本，缺口不补零、不跨缺口积分；时间轴存在无法定位的丢样时结果标记为不完整。
- 屏幕波形的 raw/aggregate 切换只用于显示层。Cursor、键盘游标、Statistics、CSV 导出和 Raw samples 表格始终读取原始 records，从不读取 mean 主线或 min/max 包络。

## 文档

- [第三方许可与内置 SDK 说明](THIRD_PARTY_NOTICES.md)
- [SDK 适配说明](docs/sdk-notes.md)
- [硬件验收清单](docs/hardware-validation.md)
- [实现接口契约](docs/implementation-contract.md)
