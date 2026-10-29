# 源码与开发说明

本目录保存粉红试用版运行源码、前端源文件、隔离测试、打包工具和第三方许可。测试夹具均为合成中文数据，不使用真实账号、设备标识或录音原文。

## 目录

- app/：本机 HTTP 服务、本人设备配置与鉴权、下载、CTC 转写、分人、导出与可选校对。
- app/web/：React 界面，含粉红按钮、查找筛选及播放器。
- verification/：身份隔离、HTTP Range、首配重试、校对和时间轴的隔离回归。
- packaging/：Windows PyInstaller 打包、依赖白名单、许可与来源校验。

## 在 Windows 开发

使用 Python 3.12 x64、Node.js 和 pnpm。以下命令在本目录执行。

```cmd
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
pnpm --dir app/web install --frozen-lockfile
pnpm --dir app/web build
.venv\Scripts\python.exe -m unittest discover -s verification -p test_*.py
.venv\Scripts\python.exe -m unittest discover -s packaging -p test_release_audit.py
```

模型与原生音频工具不存入 Git。请从本项目[发布页](https://github.com/huilan-666/huilantest/releases/tag/a1-recorder-v0.3.0-pink)下载完整程序包，解压后把其中 models/ 和 runtime/ 两个目录复制到本源码目录，保留相同目录结构。资源哈希受 packaging/release-inputs.json 校验，不能随意替换模型。

以独立本地数据目录运行，避免混用日常资料：

```cmd
.venv\Scripts\python.exe app\main.py --data-dir local-data --resources .
```

首次配置使用本人账号和卡，程序只监听 127.0.0.1。运行生成的资料不应加入公开仓库。

## 重建 Windows 包

```cmd
.venv\Scripts\python.exe packaging\build_release.py
.venv\Scripts\python.exe packaging\build_release.py --build
```

构建脚本仅收集白名单程序、模型和许可，个人 private/、recordings/、transcripts/、logs/ 不进入包。构建产物在 packaging/builds/，分享完整 ZIP。首次运行或转写测试需另外实测，隔离测试的替身不代表真实硬件或模型准确性。

## 开源来源与许可

沿用 [Shawn-TKD/dingtalk-a1-pc-tools](https://github.com/Shawn-TKD/dingtalk-a1-pc-tools) 的 A1 客户端和控制台，以及相关协议研究。原作者许可和归属在 app/vendor/ 与 app/web/ 中保留。各运行库、模型和音频工具适用各自许可，见 packaging/LICENSE_SCOPE.md、THIRD_PARTY_NOTICES.md、licenses/。未包含 MuMu、钉钉安装包或模拟器资料。
