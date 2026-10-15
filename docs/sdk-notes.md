# SDK 适配说明

本文记录对官方 Python SDK 的核对结果和本项目的适配方式，供真实硬件联调时对照。事实来源：官方仓库固定 commit 的源码，以及纳入 `powermonitor._vendor.pymonsoon` 的对应模块。

## 来源与版本

- 官方仓库：https://github.com/msoon/PyMonsoon
- 本项目使用的版本：commit `1afab10eac4468bd54f0f2256a9ed870eb5346c4`。HVPM 所需的五个模块已私有内置，具体范围、机械性修改和许可证见 [第三方说明](../THIRD_PARTY_NOTICES.md)。
- PyPI `monsoon` 0.1.88 发布较早，不直接使用；固定 commit 的 setup.py 版本号同样标为 0.1.88，因此 0.1.88 这个标签不能作为版本来源依据，只认 commit。
- 安装 `powermonitor-hvpm` 时不会安装顶层 `Monsoon` 包，也不会访问上游 Git 仓库。应用继续对所调用的 SDK 私有实现做源码哈希检查，测试同时核对内置 revision 和命名空间。
- SDK 通过 pyusb（usb.core）经 libusb-1.0 访问设备，USB VID 0x2AB9 / PID 0x0001；HVPM 与 LVPM 共用该 ID。

## 连接与型号校验

- `HVPM.Monsoon().setup_usb()` 只建立连接，不保证连到的就是 HVPM：`enumerateDevices()` 会同时列出 HVPM/LVPM；连错型号或无设备时底层只警告、可能正常返回。
- 适配层在连接后执行 `fillStatusPacket()`，核对 `statusPacket.hardwareModel` 为 HVPM（已在 `backends/monsoon.py` 实现该检查），并读取固件版本、序列号、限流和 DAC 校准值。
- `setVout(v)` 直接改变设备输出，SDK 没有独立 enable 接口，也没有可靠的输出设定回读。必须区分"命令下发成功"与"实测输出电压"。
- 关断按官方示例使用 `setVout(0)`。`closeDevice()` 会对设备 reset，但 reset、命令成功都不是物理断电保证。

## 为什么不能直接用 periodicCollectSamples

固定版本的 `periodicCollectSamples()` 每次进入先执行一次 `self.monsoon.BulkRead()`，作用是把当前 USB 缓冲里的一个包读出来丢弃。若在连续采集中反复调用它，会周期性丢一个 USB 包（官方注释也承认该接口通常多收 3 至 5 个样本而非精确返回）。

本项目的做法（已在 `backends/monsoon.py` 实现，并带内部符号的版本检查）：

1. 只启动一次 `periodicStartSampling()`，不反复启停；
2. 直接调用固定版本的内部 `_SampleEngine__sampleLoop(0, one_packet_buffer, 1)` 逐完整包处理；
3. 定期 `getSamples()` 取走校准后的样本，保留官方的校准流程、量程自动切换和物理量转换，不重写协议和校准公式；
4. 停止使用 `periodicStopSampling()`；不使用带递归重连的阻塞 `startSampling()`；
5. 不启用 SDK 自带的 CSV 输出：它与本应用共用同一个样本缓冲，会"吃掉"本应用的数据（SDK 源码中开启 CSV 后该轮返回为空，即 no CSV engine drain）。

## 时间戳、单位与 5 kHz 重建轴

- SDK 时间戳来自 `time.time() - startTime`（主机时钟，浮点秒），在逐包处理时同一 USB 包内所有样本被赋同一个值，且包含开机校准等待时间。
- 因此本项目同时记录：`host_elapsed_s`（SDK 原值）与 `time_s`（按样本序号除以标称 5000 点/秒重建，每样本支撑区间 0.2 ms）。
- 重建轴仅用于对齐、选区与积分，不声称这是硬件精确采样时间。实际速率以硬件验收实测为准，若实测与 5 kHz 偏差影响统计，需在元数据中记录。
- SDK 主通道电流单位为毫安，适配层统一换算为安培（A），电压本来就是伏特（V），功率由同一原始样本电流乘电压得到，不单独从 SDK 取功率。
- `BulkRead()` 底层读超时 1000 ms；worker 线程以此为粒度检查取消标志，并对启动校准和长期无数据设置超时。

## 线程模型

所有 SDK 调用集中在一个 worker 线程串行执行，Qt 主线程不做阻塞 USB 操作。取消靠线程安全标志在读取间隙生效，不依赖阻塞中无法响应的 Qt 槽。默认每约 250 个样本向录制器交付一批，每包仍检查错误与丢样状态；关闭官方逐包控制台输出以免占用采集带宽。启动校准上限 15 秒，无有效测量上限 5 秒；实际 USB 阻塞以 SDK 1000 ms 读取超时为下限。

首次看到非零丢样计数或后续计数变化，均保留原值并标记时间轴不完整，不猜测精确丢失数量。异常前已解析的待交付样本尽量保留，录制标记不完整；停止命令失败时保留引擎状态供显式重试，不声称输出已关断。

## 许可证记录

- 固定 commit 自带 LICENSE.txt，文本为标准 MIT 授权，版权归 Monsoon Solutions, Inc.（2017）；setup.py 中 `license='MIT'`。
- 但同一个 setup.py 的 trove classifier 写的是 `License :: OSI Approved :: GNU General Public License (GPL)`，与 LICENSE.txt 及 license 字段不一致。
- 本项目只记录该不一致事实，不作法律结论；对外分发前由责任人自行确认适用条款。

## 待实测项

- 固件版本与 DAC 校准状态对输出精度的影响（SDK 源码注释提到部分设备出厂 DAC 值不准，建议 Rev 32 固件并执行校准）。
- 实际采样率、跨批样本连续性、错误状态位与丢样计数语义。
- 长采集、拔 USB、关断失败、close 失败时的真实行为。详见 [硬件验收清单](hardware-validation.md)。
