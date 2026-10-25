"""说话人分离（2026-10-04）

对标主流工具（飞书妙记/剪映的说话人区分）的轻量实现：
whisper 分段（本身就是语音段）→ 每段抽 ECAPA 说话人嵌入（speechbrain
spkrec-ecapa-voxceleb，走 HF_ENDPOINT 镜像下载一次，缓存在挂载的 data 目录）
→ 余弦距离凝聚聚类（average-link，Lance-Williams 增量更新）→
按首次出现顺序标「说话人1/2/…」。

依赖 speechbrain + torchaudio（CPU 推理，1 小时视频数百段嵌入约几十秒）。
依赖缺失/模型加载失败时 raise RuntimeError，由调用方降级为不带标签继续出字幕。
"""

import os
import logging
import threading

import numpy as np

logger = logging.getLogger(__name__)

_MODEL = None
_MODEL_LOCK = threading.Lock()

# 模型缓存目录放挂载卷里，容器重建不丢
if os.path.isdir('/app/data'):
    _MODEL_DIR = os.environ.get('DIARIZE_MODEL_DIR', '/app/data/speechbrain/ecapa')
else:
    _MODEL_DIR = os.environ.get('DIARIZE_MODEL_DIR',
                                os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'data', 'speechbrain', 'ecapa'))

# 同一说话人 ECAPA 余弦相似度典型 >0.5，跨说话人典型 <0.4；取中间值，env 可调
_SIM_THRESHOLD = float(os.environ.get('DIARIZE_SIM_THRESHOLD', '0.38'))
# 超长段的嵌入窗口
_LONG_SEG_SECONDS = 30.0
_WINDOW_SECONDS = 20.0


def _get_model():
    """懒加载 + 缓存 ECAPA 模型（线程安全）。"""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        import torch  # noqa: F401  确认 torch 可用后再加载 speechbrain
        from speechbrain.inference.speaker import EncoderClassifier
        os.makedirs(_MODEL_DIR, exist_ok=True)
        logger.info(f"加载说话人嵌入模型 (speechbrain ECAPA, dir={_MODEL_DIR})...")
        _MODEL = EncoderClassifier.from_hparams(
            source='speechbrain/spkrec-ecapa-voxceleb',
            savedir=_MODEL_DIR,
            run_opts={'device': 'cpu'},
        )
        logger.info("说话人嵌入模型就绪")
    return _MODEL


def _decode_audio_16k(video_path):
    """整条媒体解码为 16k 单声道 float32（faster-whisper 自带的 PyAV 解码，无临时文件）。

    注意 faster-whisper 1.2.x 的 decode_audio 没有 mono 参数（默认即单声道）。
    """
    from faster_whisper.audio import decode_audio
    return decode_audio(video_path, sampling_rate=16000)


def _segment_embeddings(model, audio, sr, segments):
    """对每个 (start, end) 段抽说话人嵌入，返回 np.ndarray (n, dim)。"""
    import torch

    total = len(audio)
    out = []
    for start, end in segments:
        # 超长段取首/中/尾三个窗口平均（一段内多镜头混剪时更稳）
        if end - start > _LONG_SEG_SECONDS:
            windows = []
            for frac in (0.0, 0.5, 0.8):
                s = start + (end - start - _WINDOW_SECONDS) * frac
                windows.append((max(0, s), min(total / sr, s + _WINDOW_SECONDS)))
        else:
            s0, e0 = start, end
            # 过短段向两侧扩到 >=1s，提高嵌入稳定性
            if e0 - s0 < 1.0:
                pad = (1.0 - (e0 - s0)) / 2
                s0, e0 = max(0, s0 - pad), e0 + pad
            windows = [(s0, min(e0, total / sr))]

        embs = []
        for ws, we in windows:
            i0, i1 = int(ws * sr), int(min(total, we * sr))
            if i1 - i0 < int(0.2 * sr):
                continue
            wav = audio[i0:i1]
            if not np.isfinite(wav).all():
                wav = np.nan_to_num(wav)
            with torch.no_grad():
                emb = model.encode_batch(torch.tensor(wav, dtype=torch.float32).unsqueeze(0))
            embs.append(emb.squeeze().cpu().numpy().astype(np.float32))
        if embs:
            v = np.mean(embs, axis=0)
            norm = np.linalg.norm(v)
            out.append(v / norm if norm > 1e-8 else v)
        else:
            out.append(np.zeros(192, dtype=np.float32))
    return np.array(out)


def _average_link_labels(sim, sim_threshold, num_speakers=0):
    """凝聚聚类（average-link，Lance-Williams 增量更新），返回每段簇号。

    num_speakers>0 时聚到指定簇数；否则相似度低于阈值就停。
    """
    n = sim.shape[0]
    dist = 1.0 - sim
    np.fill_diagonal(dist, np.inf)
    sizes = np.ones(n)
    members = [[i] for i in range(n)]
    alive = list(range(n))
    target = num_speakers if num_speakers and num_speakers > 0 else None

    while len(alive) > 1:
        if target is not None and len(alive) <= target:
            break
        sub = dist[np.ix_(alive, alive)]
        flat = int(np.argmin(sub))
        a_local, b_local = divmod(flat, len(alive))
        if target is None and sub[a_local, b_local] > (1.0 - sim_threshold):
            break
        a, b = alive[a_local], alive[b_local]
        # 合并 b 入 a：Lance-Williams 平均链接更新
        for c in alive:
            if c in (a, b):
                continue
            dist[a, c] = (sizes[a] * dist[a, c] + sizes[b] * dist[b, c]) / (sizes[a] + sizes[b])
            dist[c, a] = dist[a, c]
        sizes[a] += sizes[b]
        members[a].extend(members[b])
        members[b] = []  # 必须清空被吸收簇，否则标签分配会被残留单例覆盖
        alive.remove(b)
        dist[b, :] = np.inf
        dist[:, b] = np.inf

    # 簇号按首次出现顺序重排 → 说话人1、说话人2...
    label_map = {}
    labels = [None] * n
    for ci, idxs in enumerate(members):
        if not idxs:
            continue
        first = min(idxs)
        label_map[first] = ci
    order = sorted(label_map)
    for new_idx, first in enumerate(order, start=1):
        for i in members[label_map[first]]:
            labels[i] = new_idx
    return labels


def diarize_segments(video_path, segments, num_speakers=0, progress_callback=None):
    """给 whisper 分段打说话人标签。

    Returns:
        (labels, n_speakers)：labels 是与 segments 等长的整数列表（1 起）。
    """
    try:
        model = _get_model()
    except ImportError as e:
        raise RuntimeError(f'说话人分离依赖未安装（需 speechbrain + torchaudio）: {e}')
    except Exception as e:
        raise RuntimeError(f'说话人模型加载失败: {e}')

    valid = [(i, float(s.get('start', 0) or 0), float(s.get('end', 0) or 0))
             for i, s in enumerate(segments)
             if float(s.get('end', 0) or 0) > float(s.get('start', 0) or 0)]
    if len(valid) < 2:
        return ([1] * len(segments), 1 if valid else 0)

    audio = _decode_audio_16k(video_path)
    sr = 16000
    embeddings = _segment_embeddings(model, audio, sr, [(s, e) for _, s, e in valid])
    sim = embeddings @ embeddings.T
    labels = _average_link_labels(sim, _SIM_THRESHOLD, num_speakers=num_speakers)

    out = [0] * len(segments)
    for (i, _, _), lab in zip(valid, labels):
        out[i] = lab or 1
    n_speakers = len(set(out))
    if progress_callback:
        progress_callback(84.5, f'说话人分离完成：{n_speakers} 人')
    return (out, n_speakers)
