# AI 字幕生成器 隐私政策 / Privacy Policy

**生效及更新日期 / Effective and last updated: 2026-10-25**

## 0. 运营者信息 / Operator

- 运营者：个人开发者 jvsheng（GitHub: [hongjiahao371-pixel](https://github.com/hongjiahao371-pixel)），无企业主体；真实身份信息已按绿联云开发者平台要求完成实名认证，并由平台在应用详情页按规定披露。
- 个人信息保护联系人：jvsheng，可通过 GitHub [Issues](https://github.com/hongjiahao371-pixel/ai-subtitle-generator/issues) 联系。

- Operator: individual developer jvsheng (GitHub: [hongjiahao371-pixel](https://github.com/hongjiahao371-pixel)); no corporate entity. Identity has been verified by the UGREEN developer platform and is disclosed on the app detail page as required. Contact via GitHub [Issues](https://github.com/hongjiahao371-pixel/ai-subtitle-generator/issues).

## 1. 核心原则：本地优先 / Core Principle: Local First

本应用是运行在用户自己 NAS 上的**本地离线工具**。语音识别（Whisper）、翻译（内置离线模型与本地大模型）、字幕烧录全部在本机完成，**默认不向开发者或任何第三方发送任何数据**。本应用不集成广告、分析与遥测 SDK，不收集账号、手机号、位置、通讯录或支付信息。

This app is a **local, offline tool** running on your own NAS. Speech recognition (Whisper), translation (bundled offline models and local LLMs), and subtitle burning all run on your device. **By default nothing is sent to the developer or any third party.** The app contains no advertising, analytics, or telemetry SDKs, and does not collect accounts, phone numbers, location, contacts, or payment information.

## 2. 处理的信息 / Information Processed

| 信息类型 | 场景 | 用途 | 存放 | 外发 |
| --- | --- | --- | --- | --- |
| 视频/音频文件与文件名 | 用户选择处理 | 语音识别、字幕烧录、预览 | 本机挂载目录（媒体目录只读） | 否 |
| 生成的字幕与任务记录 | 识别/翻译/编辑 | 结果保存、历史恢复 | 本机应用数据目录 | 否 |
| AI 模型文件 | 首次使用按需下载 | 本地推理 | 本机模型目录 | 下载自公共镜像源（hf-mirror/HuggingFace），仅下载不上传 |
| 云端 LLM 配置（base_url/api_key/模型名） | **用户主动**在设置中填写 | 使用外部 AI 翻译引擎 | 本机配置文件 | 仅在用户启用该引擎时 |
| 可选访问令牌（AUTH_TOKEN） | 用户自行设置 | API 访问验证 | 本机环境变量 | 否 |

| Type | Scenario | Purpose | Storage | Leaves device |
| --- | --- | --- | --- | --- |
| Video/audio files & names | User-selected tasks | ASR, burning, preview | Local mounts (media folder read-only) | No |
| Generated subtitles & task history | Recognition/translation/editing | Results, history | Local app data | No |
| AI model files | On-demand download | Local inference | Local model dir | Download-only from public mirrors |
| Cloud LLM config | **Only if the user configures it** | External AI translation | Local config file | Only when the user enables that engine |
| Optional access token (AUTH_TOKEN) | User-configured | API auth | Local env | No |

## 3. 唯一的数据外发场景 / The Only Off-Device Scenario

应用内置的 AI 翻译默认使用**随包附带的本地大模型**（CPU 运行），不出网。若用户主动在设置中配置了云端 OpenAI 兼容接口，则**待翻译的字幕文本**会发送到**用户自己填写的服务地址**。开发者不提供、不接触、不存储该数据。用户可随时清空配置恢复全本地模式。

The bundled local LLM (CPU) keeps translation on-device by default. If — and only if — the user configures a cloud OpenAI-compatible endpoint, subtitle text being translated is sent to **the endpoint the user entered**. The developer does not provide, see, or store that data. Clearing the configuration returns the app to fully local mode.

## 4. 设备与目录权限 / Device and Directory Permissions

- 媒体目录：安装时由用户选择，**只读挂载**，应用不修改其中任何文件。
- 核显设备（/dev/dri）：仅用于视频烧录与预览转码的硬件加速，不用于任何数据传输。
- 以上权限均不涉及个人敏感信息采集。

- Media folder: chosen by the user at install, mounted **read-only**; the app never modifies files in it.
- GPU device (/dev/dri): used only for hardware-accelerated encoding; never used for data transmission.

## 5. 保存与删除 / Retention and Deletion

全部数据（上传文件、字幕、模型、配置、历史）保存在本机应用目录，卸载应用或手动删除即彻底清除。撤回授权可通过卸载应用实现。

All data (uploads, subtitles, models, config, history) stays in local app directories. Uninstalling the app or deleting the directories removes it completely. Withdraw consent by uninstalling.

## 6. 未成年人 / Minors

本应用不面向未成年人提供服务。

This app is not directed at minors.

## 7. 政策更新 / Changes

重大变更将通过应用更新说明发布。Material changes will be announced in release notes.
