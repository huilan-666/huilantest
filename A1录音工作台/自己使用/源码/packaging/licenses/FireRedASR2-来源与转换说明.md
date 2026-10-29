# FireRedASR2 CTC INT8 · 权重许可与转换来源

状态：本次入包资源，用于本地转写。转写结果为可校对稿，用户可选回听校对，不声称逐字无错。本文件保留所用模型的权重许可依据和转换来源，实际运行验证由主任务另行记录。

## 权重许可依据

- 权重发布者：FireRedTeam。
- 原始权重仓：https://huggingface.co/FireRedTeam/FireRedASR2-AED
- 固定提交：2304afed56eacfee6256dee5937ed22ffa0b64ec。
- 固定模型卡：https://huggingface.co/FireRedTeam/FireRedASR2-AED/raw/2304afed56eacfee6256dee5937ed22ffa0b64ec/README.md
- 该模型卡第 5 行明确声明 `license: apache-2.0`。此声明来自直接承载 `model.pth.tar` 的官方权重仓，不是从代码仓许可证推定模型权重许可。随附的 FireRedASR2-AED-model-card.md 保留原始模型卡全文。
- 本次读取的权重仓没有独立 LICENSE 文件。随附的 FireRedASR2-Apache-2.0.txt 保留官方源码仓中的标准 Apache-2.0 全文；权重适用该许可的依据仍是上述固定模型卡。
- 许可全文固定来源：https://raw.githubusercontent.com/FireRedTeam/FireRedASR2S/4e7d9aaf4482a47cec1724807026b9b151926eb5/LICENSE
- 不对训练数据另行作许可判断，也不将训练数据的许可当作权重许可。

## 转换工件与修改说明

- 转换工件发布者：k2-fsa/sherpa-onnx。
- 官方说明：https://k2-fsa.github.io/sherpa/onnx/FireRedAsr/pretrained.html
- 文件名：sherpa-onnx-fire-red-asr2-ctc-zh_en-int8-2026-02-25.tar.bz2。
- 官方资产 ID：362637452。
- 元数据：https://api.github.com/repos/k2-fsa/sherpa-onnx/releases/assets/362637452
- 下载：https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-fire-red-asr2-ctc-zh_en-int8-2026-02-25.tar.bz2
- 压缩包大小：520516278 字节。
- 官方 SHA256：1da8b737ecc5e29f36759a4460c754863e7c919a4ba325aea187331fbfc83274。
- 按官方说明，发布包内的 `model.int8.onnx` 从 FireRedASR2-AED 仅提取 CTC 支路，排除 attention decoder，转换为 ONNX 并进行 INT8 量化。因此它是转换后的派生工件，并非原始 PyTorch 权重文件。
- 本许可资料整理步骤未下载、执行或修改模型。解包后模型文件的实际哈希、精度、时间戳、耗时和内存验收，由主任务另行记录，不能以此许可记录代替运行验证。

## 分发时保留

保留模型完整名称、FireRedTeam 与 k2-fsa/sherpa-onnx 的来源声明、固定模型卡、Apache-2.0 全文，以及本文件中的 CTC 提取、格式转换和 INT8 修改说明。最终工件如另带版权或 NOTICE 声明，应一并传递其适用内容。
