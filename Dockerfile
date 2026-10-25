FROM python:3.11-slim

WORKDIR /app

ARG DEBIAN_FRONTEND=noninteractive

# 参数化镜像源（默认国内镜像加速，国际构建可传 APT_MIRROR=deb.debian.org 覆盖）
ARG APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

# === GPU 构建开关（0=CPU 默认，1=CUDA 加速）===
# 用法：BUILD_CUDA=1 docker compose build
#   - BUILD_CUDA=0：安装 CPU 版 torch（whl/cpu），镜像小（~原大小）
#   - BUILD_CUDA=1：安装 CUDA 版 torch + NVIDIA 运行时库，镜像大 ~4GB
#   - 无 GPU 也能构建 GPU 镜像（只是装运行时库，不需要显卡在场）
ARG BUILD_CUDA=0
# CUDA 渠道：cu121 覆盖 GTX16xx~RTX40 系（默认）；RTX50/Blackwell 用 cu128
ARG CUDA_CHANNEL=cu121

RUN if [ "$APT_MIRROR" != "deb.debian.org" ]; then \
      sed -i "s/deb.debian.org/${APT_MIRROR}/g" /etc/apt/sources.list.d/debian.sources && \
      sed -i "s/security.debian.org/${APT_MIRROR}/g" /etc/apt/sources.list.d/debian.sources; \
    fi

# 安装 ffmpeg / git / tzdata / tesseract (OCR) / curl (模型下载脚本用)
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    ffmpeg git tzdata curl fonts-noto-cjk libsndfile1 \
    tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone

COPY requirements.txt requirements-gpu.txt* ./

# 根据 BUILD_CUDA 选择 torch 渠道与依赖
RUN pip install --upgrade pip -i ${PIP_INDEX} && \
    if [ "$BUILD_CUDA" = "1" ]; then \
      echo "[Dockerfile] BUILD_CUDA=1: 安装 CUDA 版 torch (${CUDA_CHANNEL})"; \
      pip install --no-cache-dir --upgrade -r requirements.txt \
        -i ${PIP_INDEX} \
        --index-url https://download.pytorch.org/whl/${CUDA_CHANNEL} \
        --extra-index-url ${PIP_INDEX}; \
      pip install --no-cache-dir -r requirements-gpu.txt -i ${PIP_INDEX}; \
    else \
      echo "[Dockerfile] BUILD_CUDA=0: 安装 CPU 版 torch"; \
      pip install --no-cache-dir --upgrade -r requirements.txt \
        -i ${PIP_INDEX} \
        --extra-index-url https://download.pytorch.org/whl/cpu; \
    fi && \
    pip install --no-cache-dir --no-deps speechbrain==1.1.1 && \
    pip install --no-cache-dir --no-deps torchaudio==2.11.0+cpu --index-url https://download.pytorch.org/whl/cpu && \
    pip check

# 预下载翻译模型（多源 fallback：hf-mirror → aifasthub → 官方，规避 429 限流）
# 在 COPY app 代码之前执行，使代码变更不会使模型缓存层失效
COPY preload_models.py ./

RUN mkdir -p /app/uploads /app/subtitles /app/models /app/builtin_translator_models && \
    python preload_models.py /app/builtin_translator_models && \
    rm preload_models.py

COPY app/ ./app/
COPY app.py ./app/app.py
COPY entrypoint.sh ./

RUN chmod +x /app/entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV FLASK_APP=app.app
ENV FLASK_ENV=production
ENV TZ=Asia/Shanghai
# 默认自动探测设备：有 CUDA 走 GPU，否则 CPU（USE_GPU 可为 auto/cpu/cuda）
ENV USE_GPU=auto

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/health', timeout=8)"

EXPOSE 5000

ENTRYPOINT ["/app/entrypoint.sh"]
