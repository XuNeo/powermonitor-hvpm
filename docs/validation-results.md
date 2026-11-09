# Linux 无硬件验证记录

日期：2026-10-12。环境：Linux x86_64、Python 3.10.12、PySide6 6.8.3、pyqtgraph 0.14.0、numpy 2.2.6。官方 SDK 固定 commit `1afab10eac4468bd54f0f2256a9ed870eb5346c4`，本地依赖无冲突。

## 已通过

- 初版 `python tools/validate.py`：164 项测试通过，ruff 和应用源码 Pyright 零错误/零警告。后续单电流图优化后的最终结果见下方 2026-10-13 小节。
- 真实官方 SampleEngine + HVPM 包转换 + 本应用适配器，无 USB 延迟：最终约 47,318 样本/秒，高于标称 5,000 样本/秒。这不是实机吞吐或精度验证。
- 10 分钟实时模拟 controller/写盘/20 Hz 显示快照：3,000,250 点，连续无序号缺口，完整录制，停止 20 ms；近期显示缓存最多 50,000 点，进程 RSS 约 34–46 MiB。期间代码后续仍有故障路径加强，最终控制器另跑一分钟回归通过。
- 加速生成 1 小时录制：18,000,000 点、792,000,000 字节；完整校验打开约 0.18 秒，整段积分约 0.65 秒，三图全览约 1.05 秒、缩放到一秒约 0.115 秒。回放 RSS 包含文件映射的常驻页，不能把它描述成只有近期缓存大小。
- 原生 Qt/X11 实际窗口：确认使能模拟器、采集、数值选窗、十字游标、浅/深色、STOP 不断电、显式 OFF、5000 行选区 CSV、回放及数据表；关闭修复后四轮连续通过，并再次以全部最终修复跑通完整流程，GUI STOP 约 0.15–0.21 秒。
- 原生 X11 的 Qt 回归共 29 项通过；关闭专项在 10 个独立进程中通过。截图首次生成返回 0，覆盖已有路径返回 2 且保留原图。
- 关闭修复后原生 Qt 窗口完整运行十分钟：3,003,750 点、录制完整、选区 CSV 5000 行、OFF 确认、正常退出，零错误；运行期 RSS 约 145 MiB。该轮 GUI STOP 含自动回放绘图耗时 6.28 秒，未达交互目标。随后 profiler 确认瓶颈为抗锯齿宽线的 QPainter 路径，改为按视口保峰降采样、非抗锯齿分段线；相同十分钟波形全览从约 13 秒降至 0.15 秒，缩放 0.036 秒，最终完整 GUI 流程 STOP 69 ms。最后这一绘图修复未再重跑十分钟实时录制，已复测同一长录制与全部回归。
- Linux 真实 SDK 的无设备扫描返回 0 台，正常完成；未打开任何设备或改变供电。
- 构建 `powermonitor_desktop-0.1.0-py3-none-any.whl` 成功，并直接从 wheel 路径运行帮助与界面截图，排除仅 editable 安装可运行的问题。
- 颜色校验：当前波形只使用已验证的浅色 current #2a78d6 / 深色 current #3987e5；电压/功率不再作为波形色，仍通过数值统计和表格读取。

## 2026-10-13 单电流图与交互优化

- UI 改为单 Current 图；普通滚轮只缩放 X 时间轴，Shift+滚轮只缩放 Y 电流轴，左拖矩形只放大 X 并作为统计选区；右键逐级恢复之前的 X/Y 缩放。图上缩放保持 Follow latest，实时窗口保留缩放宽度继续右锚最新数据。
- 用户手动改变 X 或在实时采集中应用数值选区会暂停 Follow latest；采集和写盘继续，历史 snapshot/标尺冻结。重新勾选 Follow 后立即拉取最新 10 秒。
- Follow 暂停后完全跳过 recent snapshot、plot、Raw table 和 live stats；Follow 模式按 sample_count/尾 index 去重。Raw table 仅可见时低频刷新并保留页码；recent snapshot 拼接结果缓存且返回独立副本。
- Statistics 合并为一个后台 executor，运行中只保留最新 pending 请求；GUI 线程用 20 ms QTimer 收结果，消除了 QThread wrapper 的 Shiboken 生命周期竞态。相关压力测试跨独立进程连续通过。
- 300 万点 current-only 全览约 0.074 秒，缩放一秒约 0.006 秒；对比旧三图约 0.15 秒 / 0.036 秒。Linux offscreen 10 秒 live benchmark 约 5 万点、1 条曲线、RSS 约 118 MiB。
- 最终完整自动化为 201 项测试，ruff、Pyright 通过，uv locked sync 成功；新增覆盖 live zoom 保持 Follow、显式 follow window width、X-only 框选、右键 X/Y 历史回退、统计合并、snapshot 缓存和 acquisition 回归。
- 最终 simulator GUI smoke 跑通：手动 X 缩放后 Follow 保持开启，窗口保留缩放宽度并继续右锚最新数据；手动取消 Follow 后 sample_count 继续增长但视窗冻结，重新勾选立即跳最新；随后完成数值选区、统计、STOP、OFF、5000 行 CSV 和正常关闭。macOS 最终 smoke STOP 约 86 ms。
- 由于 macOS Retina/Cocoa 轴绘制明显慢于 Linux，最终 live 波形展示节流为 2 Hz，采样和写盘仍为 5 kHz，滚轮/框选事件即时处理。mbp 原生 10 秒 benchmark：p50 约 2.16 ms；重绘帧 p95 约 83 ms；UI 工作占比约 15.5%；RSS 约 253 MiB。该数值仍未达到原计划的 16 ms 活跃刷新目标，但通过降低帧率显著减少持续卡顿；最终交互体验需由车间主任在实体 Mac 桌面确认。
- mbp 使用 `/opt/homebrew/bin/uv` locked sync 后，ruff、Pyright 和 192 项测试全部通过；原生 Cocoa simulator smoke 完成 Follow 暂停/恢复、统计、STOP、OFF、5000 行 CSV 和关闭，STOP 约 36 ms。

## 2026-10-13 示波器式波形显示（mean 趋势 + min/max 垂直包络）

本节记录显示层重做的设计与最终验证结论。

- 背景：旧显示把每个抽取桶的 min/max 极值按时间顺序连成连续折线，形成原始信号中不存在的锯齿；同时主曲线开启 pyqtgraph 0.14 的 `peak` 降采样，其官方语义就是穿过 min/max 的 saw wave，预抽取后再开存在二次降采样风险。参考 Tektronix 的 Sample/Peak Detect/High Resolution/Envelope/Average 语义、Keysight InfiniiVision 的 Normal/Peak/High Resolution 说明，以及 pyqtgraph `PlotDataItem` 的 `peak` 与 `connect="pairs"` 文档。
- 改法：一个 Current Plot 拆成 `current`（主线）与 `current_envelope`（垂直包络）两个 item。可见记录数不超过约两倍逻辑像素列数时为 raw 模式，主线逐点连接全部原始点，包络为空；更密时为 aggregate 模式，主线画每像素列的 mean，包络用同色淡细线以共享 X 的 `[min,max]` 点对画真实垂直范围，列间不连接。两 item 均 `ds=1, auto=False`、`clipToView=False`，移除 `method="peak"`。
- 数据边界：5 kHz 原始采集、`.pmcap`、统计、CSV、Raw samples、Cursor 全部继续读原始 records，不受显示聚合影响。GAP_BEFORE、超容差时间跳变、NaN/Inf、INVALID/DEVICE_ERROR 切断连续段，mean/min/max 不跨段、不跨缺口，首尾部分列不补零，空列不画。
- 交互保持：普通滚轮只缩放 X、Shift+滚轮只缩放 Y、左拖矩形只放大 X 并作为统计选区、Follow latest 缩放时保持并继续右锚最新数据、右键逐级恢复 X/Y、macOS live 展示 2 Hz 节流，均不回归。
- Benchmark 工具适配：`tools/benchmark_plot.py` 与 `tools/benchmark_live_ui.py` 对每个 curve 输出 curve 数、display mode（raw/aggregate）、raw visible count、current line 点数、envelope 点数与 envelope pairs；优先读 widget 的 `display_summary()`，无该辅助信息时从实际 curve 数据推断并在 `raw_visible_count_source` 标明来源，不假定 curve 数量固定。`benchmark_plot.py` 额外给出全览、1 秒缩放和窄到 raw 模式的缩放三段计时，并把事件泵到延迟重绘真正完成后再读数。
- 正确性验证：常量无伪振荡；斜坡 mean 单调且包络点对等于桶内真实 min/max；正弦 mean/包络与暴力参考一致；单样本正负尖峰由 aggregate envelope 精确保留并在 raw 模式逐点出现；固定种子白噪声 mean/min/max 与参考桶统计一致；GAP_BEFORE、时间跳变、NaN/Inf、INVALID/DEVICE_ERROR 不被跨越；raw 模式逐点保留 gap 后有效 sample；绘图前后原始 bytes 和统计结果不变。
- Linux 最终 ruff、Pyright、211 项测试通过。300 万点录制全览约 0.130 秒，1 秒缩放约 0.006 秒；aggregate 最终约 1020 个 mean 点和 2040 个 envelope 端点，输出规模与逻辑像素列一致。Linux 10 秒 live：约 50,750 raw visible，aggregate 为 854 mean 点 + 854 对 envelope，UI 工作占比约 4.25%，RSS 约 119 MiB。
- mbp arm64 locked uv、ruff、Pyright、211 项测试通过；Cocoa simulator 全流程完成 Follow、X-only 框选、右键历史、统计、STOP、OFF、5000 行 CSV 和关闭。10 秒 live aggregate：约 48,000 raw visible，833 mean 点 + 833 对 envelope，UI 工作占比约 16.0%，RSS 约 251 MiB。非登录 SSH 图形会话仍偶发 TSM/modal 警告，但流程与结果完整；真实桌面视觉和手感由车间主任继续验收。

## 2026-10-13 长时间实时历史与 STOP 迁移修正

- mbp 真实录制核对确认数据未丢：260.8244 秒录制含 1,304,122 点，另有 562.9 秒和 18,914.9 秒完整录制，metadata count 与 samples.bin 文件记录数一致。
- 根因是旧 live UI 只用 controller 最近 50,000 点缓存作为 plot bounds，5 kHz 下天然只有约 10 秒；STOP 后完整文件虽被打开，但视图被重置为全览且 live zoom history 在 offline 语义下失配。
- 新增 GrowingCaptureReader：采集中按 samples.bin 文件大小读取所有已 flush 的完整 44-byte records，忽略 partial 尾部，文件增长时只增量校验并换用新的 read-only memmap；旧 snapshot 继续供统计线程稳定读取。recent 50k 缓存保留为文件尚未建立时的短暂 fallback，不再限制 live history。
- Live UI 每 2 Hz refresh 完整 memmap；Follow 开启时右锚当前末端，Follow 关闭时仅扩展 bounds、不移动视图。Raw table 和早期历史统计均可访问完整 live 文件。
- STOP 从 GrowingCaptureReader 无缝切到最终 CaptureReader：保留 X/Y、selection 与右键 zoom history，不再强制全览；新 RUN 和用户手动打开其他 replay 才清历史。
- 新增 26 项 reader/长历史/transition 回归，总测试数 237。百万记录增长 memmap、旧 snapshot、partial bytes、乱序保旧、200 秒 full bounds、早期统计、STOP view preservation 均通过；12 秒 GUI transition smoke STOP 约 55 ms。

## 2026-10-14 实时 Y 缩放保持修正

- 根因：GrowingCaptureReader 每次 2 Hz refresh 在 Follow 开启时以 `preserve_view=False` 更新 plot，反复把 `_y_range_initialized` 清零，随后应用自己的 autoY 再次 `setYRange()`，覆盖 Shift+wheel 手动范围；recent fallback 也有同类默认参数问题。
- 修复：同一 live 数据源的 growing memmap 和 recent fallback 更新均使用 `preserve_view=True, preserve_history=True`。Follow 只右锚/平移 X，绝不改 Y；新 RUN、手动打开其他 replay、demo 才允许初始 autoY。
- 新增 GrowingReader 多次 remap、recent fallback、STOP live→final 三段 Y 保持回归；GUI smoke 在 Shift Y 后等待后续 live refresh 并校验范围不变。最终 ruff、Pyright 和 239 项测试通过，Linux GUI smoke 完成。

## 2026-10-15 公开发行准备

- distribution 定名为 `powermonitor-hvpm` 0.1.0，安装命令为 `pip install powermonitor-hvpm`，入口命令保持 `powermonitor`。
- 固定 revision 的 PyMonsoon HVPM 所需模块已放入私有 `powermonitor._vendor.pymonsoon` 命名空间；发行 metadata 不再包含 Git direct URL，也不发布顶层 `Monsoon` 包。
- 最终 Linux 验证为 ruff、Pyright 和 241 项测试通过；`twine check --strict`、wheel/sdist 内容检查通过。
- 全新 venv 仅从 wheel 安装后，`pip check`、`powermonitor --help`、私有 SDK import 与 offscreen GUI 截图均通过；截图包含完整 simulator 波形界面。
- GitHub Actions 已配置 Linux/macOS CI、同一 wheel artifact 的干净安装 smoke，以及 GitHub Release 到 PyPI Trusted Publishing 的 OIDC 发布流程。
- 以上仍是无硬件验证；真实 HVPM 物理供电、USB 和采样误差验收状态不变。

## 问题与限制

- 曾两次在关闭窗口时出现 exit 139（其中一次在十分钟采集后）。录制和选区 CSV 均完整，原生 core 定位于 QWidget 关闭回调的 Shiboken 异常处理；确定性复现了已删除 QThread 的 Python 引用残留导致 `isRunning()` 抛异常。已改为先从容器移除再删除 Qt 对象，并防御历史失效引用；慢统计运行时关闭请求会等待用户重试，不销毁运行线程。针对性回归、修复后原生完整交互及十分钟 Qt 长测均已通过。
- 本机缺 `libxcb-cursor.so.0`。为原生 X11 测试，只把发行版 `libxcb-cursor0` 解压到 `.venv/xcb-runtime.RO3ZXc`，使用该路径的 `LD_LIBRARY_PATH`；没有 sudo、安装系统包或修改 udev。
- 未连接真实 HVPM；物理输出、关断、DAC 校准、固件兼容、测量误差、USB 丢包和实际速率均待硬件验收。
- macOS arm64 的 uv 安装、全部测试、Cocoa 窗口和 simulator 端到端流程已验证；USB/HVPM 仍未实测。Cocoa 通过非登录 SSH 图形会话运行 smoke 时出现 TSM UI server 警告，但流程和结果完整，需登录桌面手工确认滚轮手感。

## 本地证据

产物在 `artifacts/`，不纳入 Git：

- `validation-final.log`、`environment.txt`
- `ten-minute-v2.benchmark.json`、`controller-final-minute.benchmark.json`
- `one-hour.benchmark.json`、`plot-benchmark.json`、`sdk-final-benchmark.json`
- `gui-render-final/`：最终版本浅/深色窗口、统计、表格截图及端到端结果
- `gui-ten-minute/`：关闭修复前长测的完整录制、CSV 与崩溃日志
- `gui-ten-minute-fixed/`、`gui-ten-minute-fixed.log`：关闭修复后的原生 Qt 长测
- `delivery-wheel/`：包含最后绘图修复的 wheel；`wheel-demo.png` 为此前同一包装方式的运行截图

复测命令见 README 和 `tools/benchmark.py`、`tools/benchmark_sdk.py`、`tools/benchmark_plot.py`、`tools/gui_smoke.py`。所有 benchmark 使用合成数据，不访问真实 USB。

## 2026-11-05 命令行工作流

- 先将终端 `live` 迁移提交 `7acf4c2` cherry-pick 到主仓库；旧 worktree 已删除。
- 新增 `scan/status/output on/output off/capture/info/stats/export`，保留默认 GUI 入口；CLI 不加载 Qt。
- 供电命令明确确认、必填电压、开启前配置 OFF 读回、写后配置读回，不自动重试或退出反向关断。
  配置读回不代表测量电压或实际物理输出，输出状态未知时保持未知。
- CLI 与 GUI 使用相同 hwlock 协作资源；测试覆盖锁冲突、超时、进程死亡释放、工作线程使用与清理顺序。
- ruff、pyright、373 项常规测试及 2 项性能测试通过；GUI offscreen smoke 保存 24,000 样本，STOP 约 87 ms。
- wheel/sdist 构建、distribution 检查、twine 检查通过；wheel 在仅安装 numpy/pyusb、无 Qt 的虚拟环境中完成全部 CLI 模拟工作流。
- 实机 HVPM 36612 / firmware 32 的扫描、状态读回、3 秒录制和离线统计通过；保存 14,804 个样本。
  前后配置电压均为 0 V，实际采样电压约 4.74 V，设备未知丢样告警保留；未发送电源写命令。
- 供电 on/off、非零配置读回和退出保持尚未实机验证，必须先确认 DUT 保护条件和安全测试电压。
  未实现高级限流/USB passthrough 写控制、交互会话、测试编排、设备 reset、校准或常驻服务。
