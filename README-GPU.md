# GPU（NVIDIA CUDA）加速变体 / GPU variant

主 `Dockerfile` 已参数化 GPU 构建，本目录提供开关说明。

## 构建 / Build

```bash
# CUDA 12.1（覆盖 GTX 16xx ~ RTX 40 系）
docker build --build-arg BUILD_CUDA=1 --build-arg CUDA_CHANNEL=cu121 -t aisubtitle/app:gpu .

# RTX 50 系（Blackwell）
docker build --build-arg BUILD_CUDA=1 --build-arg CUDA_CHANNEL=cu128 -t aisubtitle/app:gpu .
```

## Compose 启用 / Compose

在自部署 compose 的 `ai-subtitle` 服务里追加：

```yaml
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

应用保持 `USE_GPU=auto`：探测到 CUDA 即用 GPU 转写，否则自动回退 CPU，无需二选一。

## 说明 / Notes

- 加速范围：**ASR 转写**（faster-whisper/CTranslate2，仅 NVIDIA CUDA 后端；Intel/AMD 核显无法用于转写）。烧录与预览转码的硬加速走 VAAPI（Intel 核显），与 GPU 变体互不影响、可同时存在。
- GPU 镜像约 8-10GB；显存需求 large-v3 约 5-6GB VRAM。
- 商店版默认 CPU 镜像（兼容全部机型）；有 NVIDIA 设备的用户建议用本变体自部署。
- 前置条件：宿主机 NVIDIA 驱动 + nvidia-container-toolkit。
