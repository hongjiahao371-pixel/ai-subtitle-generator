# AI 字幕生成器 / AI Subtitle Generator

本地优先的 NAS 字幕工具：Whisper 离线语音识别 + 本地大模型翻译 + 核显硬编烧录，素材与结果全部留在你自己的设备上。

Local-first subtitle tool for your NAS: offline Whisper recognition + local LLM translation + iGPU-accelerated burning. Your media and results never leave your device.

## 功能 / Features

- **字幕生成**：faster-whisper 离线识别（tiny ~ large-v3 按任务选择，缺的自动从镜像源下载），词级时间轴
- **双语烧录**：中英字幕烧入视频，Intel 核显自动 VAAPI 硬加速（复用系统驱动，整机内核零冲突），无核显自动回退 CPU；字号/颜色/描边/边距可调
- **翻译**：内置离线模型（en-zh/zh-en）开箱即用；随包附带本地大模型引擎（Hy-MT2-7B 翻译 + Qwen3-4B 润色，llama.cpp CPU 推理，空闲自动休眠）；也可配置任意 OpenAI 兼容云端接口
- **字幕提取**：内嵌文本轨一键抽取；内嵌图像轨（PGS/DVD）OCR 识别（RapidOCR + PP-OCRv5）
- **编辑与质检**：双语对照编辑器带视频实时预览、时间轴调整；质检报告（阅读速度/时长/重叠检查，Netflix/BBC 等模板）
- **格式互转**：SRT / ASS / VTT / JSON / TXT
- **NAS 媒体库**：安装时授权媒体目录（只读），应用内直接浏览选取 NAS 视频批量处理

## 部署 / Deploy

商店版：绿联 UGOS Pro 应用中心安装（应用 ID `com.jvsheng.aisubtitle`），安装时选择视频媒体目录即可。

自部署（Docker Compose，三容器：应用 + 本地翻译 LLM + 润色 LLM）：

```bash
# 1. 构建/获取镜像后，参照 upk/aisubtitle/rootfs_common/docker-compose.yaml 编排
# 2. 媒体目录只读挂载到 /nas/videos，数据目录持久化 ./data ./models 等
# 3. 访问 http://NAS_IP:9000
```

> 说明：本地大模型 GGUF（Hy-MT2-7B / Qwen3-4B，约 7.1GB）随商店包附带；OCR 强化模型（PP-OCRv5 rec，84MB）可从 PaddleOCR 官方发布获取放入 `ocr-models/`，缺失时自动降级内置模型。

## 隐私 / Privacy

纯本地处理，零遥测零上报。唯一外发场景：用户主动配置云端 LLM 接口时，待翻译字幕文本发往用户指定地址。详见 [PRIVACY.md](PRIVACY.md)。

Everything runs locally; no telemetry. The only off-device case is a user-configured cloud LLM endpoint. See [PRIVACY.md](PRIVACY.md).

## 开源致谢 / Open Source

[faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT) · [Flask](https://flask.palletsprojects.com/) (BSD) · [RapidOCR](https://github.com/RapidAI/RapidOCR) (Apache-2.0) · [llama.cpp](https://github.com/ggml-org/llama.cpp) (MIT) · [Helsinki-NLP opus-mt](https://huggingface.co/Helsinki-NLP) (CC-BY 4.0 模型) · [Qwen3](https://github.com/QwenLM/Qwen3) (Apache-2.0) · [Noto Sans CJK](https://github.com/notofonts/noto-cjk) (OFL) · ffmpeg (LGPL/GPL)

## 许可 / License

[MIT](LICENSE) · 用户协议 [TERMS.md](TERMS.md)
