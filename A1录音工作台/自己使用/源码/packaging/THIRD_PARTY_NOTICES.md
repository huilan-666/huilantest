# 第三方来源与声明

A1 设备协议客户端及本地控制台基于 [Shawn-TKD/dingtalk-a1-pc-tools](https://github.com/Shawn-TKD/dingtalk-a1-pc-tools) 适配。作者的 MIT License 和 NOTICE.md 随包保存在 licenses/。协议研究来源按作者原 NOTICE 标注；没有把未授权的逆向研究文章内容改署为本工具代码。

本工具与钉钉、DingTalk、TALIX 及其商标所有者无隶属或背书关系。

本地转写使用 FireRedASR2-AED 的 CTC 支路 INT8 模型，官方权重模型卡声明 Apache 2.0；模型卡原文、Apache 2.0 全文及 CTC 提取/INT8 转换来源附入 licenses/。长录音由 MIT 许可的 Silero VAD 分段，词时间使用原始音频上的 CTC 帧位置。转写是可校对的自动稿，不保证逐字或人物标签全部正确。

本次程序包不包含 Buzz、Whisper 原生程序、Whisper Small 或 SenseVoice 权重；历史验证组件的许可证只作来源记录，实际资源以 BUILD-INFO.json 和 SHA256SUMS.json 为准。前端 React/ReactDOM/Scheduler 的许可分别保留。

说话人区分由 sherpa-onnx 运行库执行：运行库代码为 Apache 2.0；pyannote segmentation-3.0 模型的 MIT/CNRS 许可从原始模型压缩包原样提取；中文 ERes2Net 声纹模型按其原始 ModelScope 模型卡声明为 Apache 2.0，原模型卡和许可证分别附入 licenses/。运行库许可没有代替模型权重许可。

音频解码使用 Opus 官网列出的 Mozilla Windows opusdec 公开二进制，保留原包完整 LICENSE 和相关 Opus、Opusfile、Ogg、SpeexDSP 许可。原始压缩包的 opusinfo 没有包含在本工具中。下载来源、版本和本地计算的校验值见 licenses/audio-provenance.json。

Python 解释器以及 Bleak、WinRT、Cryptography、CFFI、NumPy、sherpa-onnx 等实际运行依赖的许可按构建环境收集，详细版本见 BUILD-INFO.json，文本见 licenses/python-packages/。构建时的 PyInstaller 使用其引导程序分发例外；不把 PyInstaller 的许可解释为本工具源码或全部依赖的统一许可。
