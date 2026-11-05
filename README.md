# powermonitor-hvpm

`powermonitor-hvpm` 是适用于 macOS 和 Linux 的 Monsoon HVPM 功耗监测桌面工具，提供电源输出控制、实时电流波形、窗口统计、原始数据录制、离线回放和 CSV 导出。

## 功能

- 设置和控制 HVPM 主通道电源输出
- 以 5 kHz 标称采样率采集电流与电压
- 实时显示电流波形
- 计算选定时间窗口的平均电流、平均功率、电荷和能量
- 保存完整原始数据并离线回放
- 导出完整数据或选定区间为 CSV
- 提供模拟器，无需连接硬件即可体验界面和数据分析
- 支持浅色与深色主题

## 系统要求

- macOS 12 或更高版本，或现代 Linux 桌面系统
- Python 3.10 至 3.13
- 连接 HVPM 时需要系统提供 libusb-1.0

应用已经包含所需的 PyMonsoon SDK，无需单独安装 `Monsoon` Python 包。

## 安装

### 使用 uv

推荐作为独立工具安装，不污染其他 Python 项目：

```sh
uv tool install powermonitor-hvpm
powermonitor
```

升级：

```sh
uv tool upgrade powermonitor-hvpm
```

卸载：

```sh
uv tool uninstall powermonitor-hvpm
```

也可以不安装，直接临时运行：

```sh
uvx --from powermonitor-hvpm powermonitor
```

若安装后终端找不到 `powermonitor`，执行一次：

```sh
uv tool update-shell
```

然后重新打开终端。

### 使用 pip

```sh
python -m pip install powermonitor-hvpm
powermonitor
```

## USB 配置

### macOS

使用 Homebrew 安装 libusb：

```sh
brew install libusb
```

连接 HVPM 后直接启动 `powermonitor`，再在界面中扫描设备。macOS 插入 USB 设备时通常不会弹出提示。

### Linux

使用发行版包管理器安装 libusb。例如 Ubuntu/Debian：

```sh
sudo apt install libusb-1.0-0
```

普通用户还需要设备访问权限。HVPM 的 USB VID/PID 为 `2ab9:0001`，可创建 udev 规则：

```text
SUBSYSTEM=="usb", ATTRS{idVendor}=="2ab9", ATTRS{idProduct}=="0001", MODE="0660", GROUP="plugdev"
```

保存为 `/etc/udev/rules.d/70-monsoon-hvpm.rules`，确保当前用户属于 `plugdev` 组，然后重载规则并重新插拔设备：

```sh
sudo udevadm control --reload-rules
sudo udevadm trigger
```

## 使用方法

启动应用：

```sh
powermonitor
```

打开已有录制：

```sh
powermonitor --open /path/to/capture.pmcap
```

启动后端到端操作流程：

1. 选择 **Monsoon HVPM** 后端。
2. 点击 **Scan**，选择设备并点击 **Connect**。
3. 先点击 **OFF**，确认输出处于已知关断状态。
4. 设置目标电压并点击 **Apply target**。
5. 点击 **Enable output**，确认电压后开启输出。
6. 点击 **RUN** 开始采集和录制。
7. 点击 **STOP** 停止采集；STOP 不会自动关闭电源输出。
8. 测量结束后点击 **OFF** 关闭输出。

### 断开连接与退出

应用在断开 HVPM 或退出时只释放主机侧 USB 资源，不调用 SDK 的 USB reset。
停止采集仍会发送停止采样命令；由应用开启的输出仍会在断开或退出时尝试关闭。
这不是“退出后保持应用开启的电源输出”功能。

直接使用 Python 后端时，`MonsoonBackend()` 保留 SDK 默认关闭行为；传入
`reset_on_close=False` 才会使用不 reset USB 的清理路径。该路径的重复连接与
bulk endpoint 释放行为仍需在目标 HVPM、操作系统和 libusb 组合上实机验证。

### 实时终端电流

无需打开 GUI，也不创建录制文件：

```sh
powermonitor live
powermonitor live --serial SERIAL --interval 0.2 --seconds 30
powermonitor live --plain
```

- `I` 为本刷新窗口中最新的有效电流；`avg` 和 `peak` 为自上次刷新以来的有效样本均值和峰值，单位为 mA。
- 交互终端显示带电流轴、时间标签的滚动 ASCII 波形，自动缩放；波形点是刷新时的 `I`，不是完整 5 kHz 原始波形。
- `--plain` 仅显示数值与告警；重定向时输出逐行文本，不使用终端控制序列。默认刷新间隔为 0.2 秒。
- `--seconds` 限制采样时长（含校准、不含连接与锁等待）；省略时持续运行，按 Ctrl+C 退出。
- 无效样本、设备错误和非有限电流不计入统计；丢样等质量告警保留显示。当前窗口没有有效样本时明确提示，不重复旧电流。

此模式只采样：**不设置电压、不切换输出、不 reset USB、不保存 `.pmcap` 或 CSV**。
退出先关闭采样流、发送 STOP，STOP 成功后才释放主机 USB 资源；STOP 失败时返回失败，不继续释放。
它与 GUI 的供电控制行为不同，不会关闭由其他工具开启的输出。

真实设备模式要求系统 libusb、USB 访问权限及 `hwlock` 命令；缺少依赖时失败，不自动安装或回退。
`hwlock` 可用 `uv tool install hwlock` 单独安装。命令自动持有 `monsoon-hvpm` 协作锁，
锁等待上限为 2 秒；并行使用其他 HVPM 工具时也应使用同一个资源名。

无硬件验证：

```sh
powermonitor live --backend simulator --seconds 2
python -m powermonitor live --backend simulator --seconds 2 --plain
```

模拟器保持输出关闭，因此显示零电流是预期结果。终端模式不加载 Qt，也不访问录制存储模块。

### 模拟模式

默认启动时使用 Simulator 后端。模拟器不会访问 USB，也不会控制任何物理电源输出，可用于熟悉界面、波形操作、统计和导出功能。

## 波形操作

- **滚轮**：缩放时间轴
- **Shift + 滚轮**：缩放电流轴
- **左键拖框**：放大所选时间范围，并将其作为统计选区
- **右键**：逐级恢复之前的缩放范围
- **Follow latest**：保持当前时间窗口宽度并跟随最新数据
- 取消 **Follow latest**：冻结当前视图，采集和录制继续进行

放大时显示原始采样点；数据密集时显示平均趋势和峰值包络。统计、CSV 和原始数据表始终使用完整原始样本，不受屏幕显示方式影响。

## 统计与导出

可以通过以下方式选择统计区间：

- 在波形上拖框
- 在 **Selection window** 中输入起止时间

**Statistics** 页面提供平均电流、平均功率、电荷、能量、有效时长和数据覆盖率。可导出：

- 完整录制 CSV
- 当前选区 CSV

内部单位为秒、安培、伏特和瓦特。

## 录制文件

录制使用 `.pmcap` 目录格式：

```text
capture.pmcap/
├── metadata.json
├── samples.bin
└── events.jsonl
```

每次采集都会创建新的录制目录，不会覆盖已有数据。停止采集后，应用会自动打开刚完成的录制，并保留当前视图和缩放历史。

原始录制约占用 792 MB/小时；导出的 CSV 通常更大。

## 供电安全

- 扫描、连接、打开录制和开始采集都不会自动开启输出。
- 设置目标电压不会立即改变输出，必须再次确认并启用。
- 软件允许的输出范围为 0.80 V 至 13.50 V；启用前请确认被测设备可承受该电压。
- **RUN/STOP 只控制采集，不控制供电。**
- 关断或通信失败时，应用不会假定设备已经断电；操作硬件前请确认实际输出状态。
- 不建议以 root 用户运行应用。

## 从源码运行

```sh
git clone https://github.com/XuNeo/powermonitor-hvpm.git
cd powermonitor-hvpm
uv sync --extra test
uv run python -m powermonitor
```

运行测试：

```sh
uv run python tools/validate.py
```

## 许可证

项目采用 [MIT License](LICENSE)。内置 PyMonsoon SDK 的来源和许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
