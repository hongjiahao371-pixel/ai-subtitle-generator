# AI 字幕生成器 UPK 发布说明（商店提交用）

> 提交开发者门户时，把对应语言文本粘贴到 ReleaseNote（上限 5000 字符）。

## zh-CN（v1.0.0 build 1）

AI 字幕生成器 v1.0.0 首发。

本地优先的 AI 字幕工具：基于 Whisper 本地语音识别为 NAS 中的视频生成时间轴精准的字幕，素材与结果全部保存在您自己的设备上。

主要功能：
- 字幕生成：Whisper 离线识别，默认 small 模型；可在任务中选择 tiny/base/medium/large-v3-turbo/large-v3，按需自动下载
- 中英双语字幕烧录：检测到 Intel 核显时自动启用 VAAPI 硬加速（复用系统自带驱动，确保与整机内核匹配），无核显或驱动不可用时自动回退 CPU 软编
- 字幕提取：提取视频内嵌文本字幕轨，或以 OCR 方式识别画面中的硬字幕
- 格式互转：SRT / ASS / VTT / JSON / TXT 双向转换
- 字幕编辑：时间轴调整、样式编辑与实时预览
- 翻译：内置离线翻译模型（中英等语言对开箱即用）；随包附带本地大模型翻译引擎（Hy-MT2-7B 翻译 + Qwen3-4B 润色，CPU 运行、空闲自动休眠以节省内存）；也可在设置中改配任意 OpenAI 兼容云端接口

使用说明：
- 安装时请选择存放视频的媒体目录（只读挂载，用于在应用内直接选取视频；应用不会修改该目录内容）
- 首次启动会按需从公共镜像源下载所选的 Whisper 模型；语音识别、翻译、润色默认全部在本机完成
- 应用通过 http://NAS_IP:9000 访问，仅监听局域网

隐私说明：本应用不提供云服务；素材、模型与生成结果均保存在本机。仅当用户主动配置云端 LLM 接口时，字幕文本才会发送到用户指定的服务地址。

## en-US (v1.0.0 build 1)

AI Subtitle Generator v1.0.0, first release.

Local-first AI subtitle tool for your NAS: generates accurate, timestamped subtitles with on-device Whisper speech recognition. All media, models, and results stay on your own device.

Key features:
- Subtitle generation: offline Whisper recognition (small by default; tiny/base/medium/large-v3-turbo/large-v3 selectable per task, downloaded on demand)
- Bilingual subtitle burning: automatic Intel iGPU VAAPI acceleration when available (uses the system-provided graphics driver for best kernel compatibility), with automatic CPU fallback
- Subtitle extraction: embedded text tracks, or OCR recognition of on-screen subtitles
- Format conversion: SRT / ASS / VTT / JSON / TXT
- Subtitle editing: timing adjustments, styling, live preview
- Translation: bundled offline translation models (works out of the box for common language pairs); a local LLM engine is included (Hy-MT2-7B for translation, Qwen3-4B for refinement — CPU mode, sleeps when idle); any OpenAI-compatible cloud endpoint can be configured instead

Notes:
- Choose your video media folder during installation (mounted read-only for browsing; the app never modifies it)
- On first use the selected Whisper model is downloaded from a public mirror; recognition, translation, and refinement all run locally by default
- The app is served at http://NAS_IP:9000 and only listens on the LAN

Privacy: no cloud service is provided. Media, models, and results remain on your device. Subtitle text is only sent off-device if you explicitly configure a cloud LLM endpoint.

## 提交备忘（不上传）

- 应用 ID：com.jvsheng.aisubtitle（上架后不可改）
- 版本：1.0.0，build 1 → 完整版本号 1.0.0.1
- 架构：amd64（nasync 产品线）
- 商店截图：按平台要求单独上传（PC 1854×1236 ≥2 张 / 移动 1125×2436 ≥2 张）
- 包内不含隐私政策外链（本应用无官网/仓库），如审核要求补充可挂 GitHub Gist 后在门户资料里填
- 权限说明：compose 声明 /dev/dri（Intel 核显硬加速，可选，无核显设备自动 CPU 路线）；安装参数 MEDIA_DIR 为用户选择的只读媒体目录
