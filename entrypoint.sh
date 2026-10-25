#!/bin/bash
set -e

# 时区优化（2026-06-02）：显式 export TZ + 固化 /etc/localtime + 启动 Python 时调 time.tzset()
# 即使 compose 传了 TZ env，也再设一次防止 env 漂移
export TZ=${TZ:-Asia/Shanghai}
if [ -f /usr/share/zoneinfo/$TZ ]; then
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime
fi
echo $TZ > /etc/timezone

# 模型下载协议修复（2026-08-30）：新版 huggingface_hub 对 base 及以上档位默认走 xet 分块协议，
# hf-mirror 无法代理 xet 的 CAS 服务（401 Unauthorized），导致 tiny 以外的模型在线下载必失败。
# 禁用 xet 回退到普通 HTTP 下载，hf-mirror 可正常代理。
export HF_HUB_DISABLE_XET=${HF_HUB_DISABLE_XET:-1}

MODEL_SIZE=${MODEL_SIZE:-tiny}
MODEL_DIR="/root/.cache/huggingface/hub"
MODEL_FOUND=0
MODEL_CACHE_NAME="$MODEL_SIZE"
if [ "$MODEL_SIZE" = "large" ]; then MODEL_CACHE_NAME="large-v3"; fi

for pattern in "models--Systran--faster-whisper-${MODEL_CACHE_NAME}" "models--mobiuslabsgmbh--faster-whisper-${MODEL_CACHE_NAME}" "faster-whisper-${MODEL_CACHE_NAME}"; do
    if [ -d "$MODEL_DIR/$pattern" ]; then
        echo "[entrypoint] Model '${MODEL_SIZE}' found in cache: $MODEL_DIR/$pattern"
        MODEL_FOUND=1
        break
    fi
done

if [ "$MODEL_FOUND" -eq 0 ]; then
    echo "[entrypoint] Model '${MODEL_SIZE}' not found, downloading..."
    python -c "
from faster_whisper import WhisperModel
import sys
try:
    model = WhisperModel('${MODEL_SIZE}', device='cpu', compute_type='int8')
    print('[entrypoint] Model downloaded successfully')
except Exception as e:
    print(f'[entrypoint] Model download failed: {e}', file=sys.stderr)
    sys.exit(1)
"
fi

TRANSLATOR_DIR="${TRANSLATOR_MODELS_DIR:-/app/translator_models}"
BUILTIN_DIR="/app/builtin_translator_models"
mkdir -p "$TRANSLATOR_DIR"
if [ -d "$BUILTIN_DIR" ] && [ -n "$(ls -A "$BUILTIN_DIR" 2>/dev/null)" ] && [ ! "$(ls -A "$TRANSLATOR_DIR" 2>/dev/null)" ]; then
    echo "[entrypoint] Copying built-in translation models..."
    cp -r "$BUILTIN_DIR"/* "$TRANSLATOR_DIR"/
    echo "[entrypoint] Translation models ready"
fi

echo "[entrypoint] Starting application with optimized waitress (TZ=$TZ)..."
# 时区优化：Python 启动后立即调 time.tzset()，让 logging.Formatter 拿本地时区
exec python -c "import os, time; os.environ['TZ']=os.environ.get('TZ','Asia/Shanghai'); time.tzset(); from waitress import serve; from app.app import app; serve(app, host='0.0.0.0', port=5000, threads=16, channel_timeout=600, cleanup_interval=60)"
