# Third-Party Notices

本文件记录本分发中包含的第三方源代码及其改动事实。除特别说明的内置（vendored）代码外，其余第三方组件（numpy、PySide6、pyqtgraph、pyusb 等）均由包管理器在安装时从 PyPI 获取，不随本项目源码分发，其条款以各自主页或分发包为准。本文件只陈述来源与改动事实，不构成任何法律结论。

## 内置的 Monsoon PyMonsoon SDK

本项目内置（vendor）了官方 Python SDK 的一部分，用于与 Monsoon HVPM 通信。

- 上游仓库：https://github.com/msoon/PyMonsoon
- 上游版本：commit `1afab10eac4468bd54f0f2256a9ed870eb5346c4`
- 上游版本号标识：该 commit 的 `setup.py` 报告版本 `0.1.88`（与 PyPI 上较早发布的 `monsoon` 0.1.88 同名，但内容不能仅凭版本号判定一致，本项目只以上述 commit 为准）。
- 内置模块（五个）：`HVPM.py`、`sampleEngine.py`、`Operations.py`、`pmapi.py`、`calibrationData.py`。这五个模块覆盖 HVPM 主通道设备访问、协议常量、USB 传输、校准数据与采样引擎；上游的 LVPM、命令行入口与固件刷新等模块未内置。
- 该 SDK 经 pyusb（`usb.core` / `usb.util`）通过系统 libusb-1.0 访问设备，USB VID 0x2AB9 / PID 0x0001。运行时仍需系统提供 libusb，Linux 上通常还需配置 udev 权限，详见 README。

### 相对上游所做的改动

在内置过程中，对上述五个模块做了以下机械性改动；设备协议、校准公式、量程切换和物理量换算逻辑保持上游原样：

1. 改为包内相对导入：上游模块中的 `from Monsoon import ...` / `from Monsoon.calibrationData import ...` 改为指向本项目内置包的相对导入。因此本分发不再依赖、也不安装任何外部顶层 `Monsoon` 命名空间包；内置 SDK 位于本项目自己的包内。
2. 移除未使用的 scipy 导入：`sampleEngine.py` 上游第 17、18 行为 `import scipy as sp` 与 `import scipy.stats`，但该模块源码中没有任何对 `sp` 或 `scipy` 的实际调用。内置版本删除了这两行导入，因此本项目运行依赖中不再需要 scipy。
3. 修正一处 Python 字符串比较：`platform.system() is "Windows"` 改为语义等价且正确的 `==`，避免解释器的 `SyntaxWarning`。
4. 在上游模块首部增加仅用于本项目工具链的 `ruff` / `pyright` 文件级说明；这些说明不改变运行时行为。
5. 清理行尾空白并统一文本文件末尾换行，不改变运行时行为。
6. 除上述机械性改动外，未改动采样、校准与协议处理代码。

### 上游许可证（事实记录）

- 该 commit 自带 `LICENSE.txt`，文本为标准 MIT 授权，版权署名 Monsoon Solutions, Inc. (2017)；其 `setup.py` 中 `license='MIT'`。
- 同一 `setup.py` 的 trove classifier 写的是 `License :: OSI Approved :: GNU General Public License (GPL)`，与 `LICENSE.txt` 文本及 `license` 字段不一致。本项目仅如实记录这一不一致，不就适用条款作结论；对外分发前由责任人自行向上游确认。

上游 MIT 授权文本（按上游 `LICENSE.txt` 标准 MIT 文本转录）：

```
MIT License

Copyright (c) 2017 Monsoon Solutions, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
