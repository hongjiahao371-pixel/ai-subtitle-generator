#!/usr/bin/env python3
"""字幕轨探测 + ffmpeg 抽文本字幕 (2026-06-22 新增)

支持：
  - 文本字幕：subrip / ass / ssa / webvtt → ffmpeg -c:s srt 直接抽（默认 mode=text）
  - 图像字幕：hdmv_pgs_subtitle / dvd_subtitle / dvb_subtitle / xsub → OCR 识别（mode=ocr）

需要：
  - tesseract-ocr + tesseract-ocr-chi-sim + tesseract-ocr-eng（系统包）
  - pytesseract + Pillow（pip 包）

不做：
  - 字幕时间校正 (假定源字幕时间码准确)
"""
import json
import logging
import os
import re
import subprocess
import threading
import time

logger = logging.getLogger(__name__)


def _run_proc_with_timeout(proc, cancel_event=None, line_timeout=30, on_progress=None,
                           on_end=None, progress_keywords=('out_time_ms', 'progress=end')):
    """带超时地迭代 ffmpeg/ffprobe 的 stdout progress 行。

    修复 BUG：原 `proc.stdout.readline()` 是阻塞读，ffmpeg 卡死（不输出也不退出）时
    会永久阻塞 worker 线程，取消也无法生效。本函数用后台线程把 stdout 行喂入 queue，
    主循环带超时 poll，超时或取消时 kill 进程。

    Args:
        proc: 已启动的 Popen 对象（stdout=PIPE, text=True）
        cancel_event: threading.Event，set() 时终止
        line_timeout: 连续两行之间的最大间隔秒数，超时则认为 ffmpeg 卡死
        on_progress: callback(line) 处理每一行（返回 'end' 可提前结束）
        on_end: callback() 循环正常结束后调用
    Returns: ('completed'|'cancelled'|'timeout', returncode)
    """
    import queue as _queue
    q = _queue.Queue()
    eof = object()

    def _reader():
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:
            pass
        finally:
            q.put(eof)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    status = 'completed'
    try:
        while True:
            if cancel_event and cancel_event.is_set():
                status = 'cancelled'
                break
            try:
                line = q.get(timeout=line_timeout)
            except _queue.Empty:
                # 超时无输出，判定卡死
                logger.warning(f"ffmpeg {line_timeout}s 无输出，判定卡死，终止进程")
                status = 'timeout'
                break
            if line is eof:
                break
            if on_progress:
                if on_progress(line) == 'end':
                    break
        if on_end:
            on_end()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        # 确保读线程退出（排空 queue）
        try:
            while True:
                if q.get_nowait() is eof:
                    break
        except _queue.Empty:
            pass
        t.join(timeout=2)
    return status, proc.returncode

# 文本字幕 codec：ffmpeg -c:s srt 能直接转码的
TEXT_SUBTITLE_CODECS = {
    'subrip',       # .srt
    'srt',          # subrip 别名
    'ass',          # .ass
    'ssa',          # .ssa
    'webvtt',       # .vtt
    'mov_text',     # MP4 内嵌文本字幕（罕见）
    'ttml',         # 字幕文本标记语言
}

# 图像字幕 codec（暂不支持，需要 OCR）
IMAGE_SUBTITLE_CODECS = {
    'hdmv_pgs_subtitle',  # 蓝光 PGS
    'dvd_subtitle',       # DVD SUB
    'dvb_subtitle',       # DVB
    'xsub',               # XSUB
}

ALL_SUPPORTED = TEXT_SUBTITLE_CODECS  # 实际只支持文本字幕


def probe_subtitle_streams(video_path):
    """ffprobe 列出视频内嵌字幕轨

    Returns:
        list of dict: [{index, codec_name, codec_type, profile, language, title}, ...]
        失败返回 []
    """
    if not os.path.isfile(video_path):
        logger.error(f"probe_subtitle_streams: 文件不存在 {video_path}")
        return []

    cmd = [
        'ffprobe', '-v', 'error',
        '-select_streams', 's',
        '-show_entries', 'stream=index,codec_name,codec_type,profile:stream_tags=language,title',
        '-of', 'json',
        video_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            logger.warning(f"ffprobe 失败: {proc.stderr[:200]}")
            return []
        data = json.loads(proc.stdout)
        streams = []
        for s in data.get('streams', []):
            tags = s.get('tags', {}) or {}
            codec = s.get('codec_name', '')
            streams.append({
                'index': s.get('index'),
                'codec_name': codec,
                'codec_type': s.get('codec_type', 'subtitle'),
                'profile': s.get('profile', ''),
                'language': tags.get('language', ''),
                'title': tags.get('title', ''),
                'is_text': codec in TEXT_SUBTITLE_CODECS,
                'is_image': codec in IMAGE_SUBTITLE_CODECS,
                'is_supported': codec in TEXT_SUBTITLE_CODECS or codec in IMAGE_SUBTITLE_CODECS,
                'ocr_required': codec in IMAGE_SUBTITLE_CODECS,
            })
        return streams
    except subprocess.TimeoutExpired:
        logger.error(f"ffprobe timeout ({video_path})")
        return []
    except Exception as e:
        logger.error(f"ffprobe 异常: {e}")
        return []


def get_video_duration(video_path):
    """ffprobe 视频时长（秒），失败返回 None"""
    try:
        proc = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=nw=1:nk=1', video_path],
            capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return float(proc.stdout.strip())
    except Exception:
        pass
    return None


def extract_subtitle_stream(video_path, stream_index, output_path,
                             progress_callback=None, cancel_event=None):
    """ffmpeg 抽字幕轨到 SRT 文件

    Args:
        video_path: 视频文件路径
        stream_index: 字幕轨 index (从 probe_subtitle_streams 获取)
        output_path: 输出 .srt 文件路径
        progress_callback: callable(percent, info) → percent 0-100
        cancel_event: threading.Event, set() 时取消

    Returns:
        dict: {success, output_path, codec_name, duration, error}
    """
    if not os.path.isfile(video_path):
        return {'success': False, 'error': f'视频文件不存在: {video_path}'}

    # 先 probe 验证 stream_index + 拿 codec_name
    streams = probe_subtitle_streams(video_path)
    target = None
    for s in streams:
        if s['index'] == stream_index:
            target = s
            break
    if target is None:
        return {'success': False, 'error': f'字幕轨 index={stream_index} 不存在'}
    if not target['is_supported']:
        return {
            'success': False,
            'error': f'暂不支持 {target["codec_name"]} 字幕（需 OCR, NAS 上暂无此功能）',
            'codec_name': target['codec_name'],
        }

    if progress_callback:
        progress_callback(5, f'开始抽字幕 (codec={target["codec_name"]})...')

    # 准备输出目录
    out_dir = os.path.dirname(output_path)
    if out_dir and not os.path.isdir(out_dir):
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            return {'success': False, 'error': f'无法创建输出目录: {e}'}

    # 视频总时长（用于进度计算）
    duration = get_video_duration(video_path)

    # ffmpeg 命令：-c:s srt 转文本字幕
    # 修复 BUG：原用 '0:s:{stream_index}' 把全局流索引当成字幕流相对索引，
    # 多字幕轨视频会抽错轨或越界失败。stream_index 来自 ffprobe 的全局 index，
    # 应用 '0:{stream_index}'（与 OCR 路径 extract_image_subtitle_with_ocr 一致）。
    cmd = [
        'ffmpeg', '-nostdin', '-y',
        '-progress', 'pipe:1',  # 进度输出到 stdout
        '-i', video_path,
        '-map', f'0:{stream_index}',
        '-c:s', 'srt',  # 统一输出 SRT
        output_path,
    ]
    logger.info(f"ffmpeg 抽字幕: {' '.join(cmd)}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except Exception as e:
        return {'success': False, 'error': f'启动 ffmpeg 失败: {e}'}

    # 解析 -progress 输出（带超时，防止 ffmpeg 卡死阻塞 worker）
    last_pct = 5.0

    def _on_line(line):
        nonlocal last_pct
        line = line.strip()
        m = re.match(r'^out_time_ms=(\d+)$', line)
        if m and duration and duration > 0:
            out_time_s = int(m.group(1)) / 1_000_000.0
            pct = 5 + (out_time_s / duration) * 90  # 5-95%
            pct = min(95.0, max(5.0, pct))
            if pct > last_pct + 1.0:
                last_pct = pct
                if progress_callback:
                    progress_callback(round(pct, 1), f'抽取中... {out_time_s:.1f}s / {duration:.1f}s')
        if line == 'progress=end':
            return 'end'

    status, rc = _run_proc_with_timeout(proc, cancel_event=cancel_event,
                                         line_timeout=30, on_progress=_on_line)

    if status == 'cancelled':
        return {'success': False, 'error': '任务被取消', 'cancelled': True}
    if status == 'timeout':
        return {'success': False, 'error': 'ffmpeg 处理超时（30s 无输出，可能文件损坏）'}
    stderr_tail = proc.stderr.read()[-500:] if proc.stderr else ''

    if rc != 0:
        logger.error(f"ffmpeg 抽字幕失败 rc={rc}: {stderr_tail}")
        return {'success': False, 'error': f'ffmpeg 退出码 {rc}', 'stderr': stderr_tail}

    if not os.path.isfile(output_path):
        return {'success': False, 'error': '输出文件未生成'}

    file_size = os.path.getsize(output_path)
    if file_size == 0:
        return {'success': False, 'error': '输出文件为空（可能字幕轨本身无内容）'}

    if progress_callback:
        progress_callback(100, f'抽取完成 ({file_size/1024:.1f} KB)')

    return {
        'success': True,
        'output_path': output_path,
        'codec_name': target['codec_name'],
        'language': target.get('language', ''),
        'title': target.get('title', ''),
        'file_size': file_size,
        'duration': duration,
    }



def extract_image_subtitle_with_ocr(video_path, stream_index, output_path,
                                      ocr_lang='chi_sim+eng',
                                      progress_callback=None, cancel_event=None):
    """OCR 抽取图像字幕轨（PGS/DVD/DVB）→ SRT

    流程：
      1. ffmpeg 抽 PNG 序列 + 每帧时间戳（-c:s png -fps_mode passthrough）
      2. pytesseract.image_to_string 识别每张图
      3. 用 ffprobe -show_packets 拿每帧的 pts_time（时间戳）
      4. 输出 SRT（顺序合并）

    Args:
        ocr_lang: tesseract 语言包（默认 chi_sim+eng 中英文混排）
        progress_callback: callable(percent, info)
        cancel_event: threading.Event
    Returns:
        dict: {success, output_path, frames_processed, error}
    """
    import tempfile

    if not os.path.isfile(video_path):
        return {'success': False, 'error': f'视频文件不存在: {video_path}'}

    if progress_callback:
        progress_callback(5, 'OCR 初始化...')

    # 1. probe 验证
    streams = probe_subtitle_streams(video_path)
    target = None
    for s in streams:
        if s['index'] == stream_index:
            target = s
            break
    if target is None:
        return {'success': False, 'error': f'字幕轨 index={stream_index} 不存在'}
    if not target.get('ocr_required'):
        return {'success': False, 'error': f'字幕 codec={target["codec_name"]} 不是图像字幕，不需要 OCR'}

    # 准备输出
    out_dir = os.path.dirname(output_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    duration = get_video_duration(video_path)

    with tempfile.TemporaryDirectory() as tmpdir:
        # 2. ffmpeg 抽 PNG
        png_pattern = os.path.join(tmpdir, 'frame_%05d.png')

        # 用 -vf showinfo 输出每帧 pts 到 stderr（解析）
        ffmpeg_cmd = [
            'ffmpeg', '-nostdin', '-y',
            '-progress', 'pipe:1',
            '-i', video_path,
            '-map', f'0:{stream_index}',  # global index（与 probe 一致）
            '-c:s', 'png',
            '-fps_mode', 'passthrough',
            '-start_number', '0',
            png_pattern,
        ]
        logger.info(f"OCR ffmpeg 抽帧: {' '.join(ffmpeg_cmd)}")

        proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        last_pct = 5.0

        def _ocr_on_line(line):
            nonlocal last_pct
            line = line.strip()
            m = re.match(r'^out_time_ms=(\d+)$', line)
            if m and duration and duration > 0:
                pct = 5 + (int(m.group(1)) / 1_000_000.0 / duration) * 70  # 5-75%
                pct = min(75.0, max(5.0, pct))
                if pct > last_pct + 1.0:
                    last_pct = pct
                    if progress_callback:
                        progress_callback(round(pct, 1), 'OCR 抽帧中...')
            if line == 'progress=end':
                return 'end'

        ocr_status, rc = _run_proc_with_timeout(proc, cancel_event=cancel_event,
                                                 line_timeout=60, on_progress=_ocr_on_line)
        if ocr_status == 'cancelled':
            return {'success': False, 'error': '任务被取消', 'cancelled': True}
        if ocr_status == 'timeout':
            return {'success': False, 'error': 'OCR 抽帧超时（60s 无输出，可能文件损坏）'}

        stderr_tail = proc.stderr.read()[-500:] if proc.stderr else ''
        if rc != 0:
            logger.error(f"OCR ffmpeg 抽帧失败 rc={rc}: {stderr_tail}")
            return {'success': False, 'error': f'ffmpeg 退出码 {rc}'}

        # 3. 列出 PNG 文件，按文件名排序
        try:
            from PIL import Image
            import pytesseract
        except ImportError as e:
            return {'success': False, 'error': f'OCR 依赖未安装: {e}（需要 pytesseract + Pillow）'}

        png_files = sorted([f for f in os.listdir(tmpdir) if f.endswith('.png')])
        if not png_files:
            return {'success': False, 'error': 'ffmpeg 抽帧结果为空（字幕轨可能没有内容）'}

        if progress_callback:
            progress_callback(80, f'开始 OCR 识别 {len(png_files)} 帧...')

        # 4. 用 ffprobe 拿每帧 pts_time（每张 PNG 对应一个 packet）
        probe_cmd = [
            'ffprobe', '-v', 'error',
            '-select_streams', f'{stream_index}',
            '-show_packets', '-show_entries', 'packet=pts_time,duration_time,size',
            '-of', 'csv=p=0',
            video_path,
        ]
        try:
            probe_proc = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=60)
            timestamps = []
            for line in probe_proc.stdout.strip().split(chr(10)):
                parts = line.split(',')
                if len(parts) >= 3:
                    try:
                        timestamps.append((float(parts[0]) if parts[0] else 0.0,
                                          float(parts[1]) if parts[1] else 0.0))
                    except ValueError:
                        continue
        except Exception as e:
            logger.warning(f"ffprobe 拿时间戳失败: {e}, 用估算")
            timestamps = [(i * 2.0, 2.0) for i in range(len(png_files))]

        # 对齐 PNG 和时间戳（数量可能不等，用较短）
        n = min(len(png_files), len(timestamps))
        if len(timestamps) == 0:
            timestamps = [(i * 2.0, 2.0) for i in range(len(png_files))]
            n = len(png_files)

        # 5. OCR 识别每帧
        srt_entries = []
        for i in range(n):
            if cancel_event and cancel_event.is_set():
                return {'success': False, 'error': '任务被取消', 'cancelled': True}
            png_path = os.path.join(tmpdir, png_files[i])
            try:
                img = Image.open(png_path)
                text = pytesseract.image_to_string(img, lang=ocr_lang, config='--psm 6').strip()
            except Exception as e:
                logger.warning(f"OCR 帧 {i} 失败: {e}")
                continue

            if text:
                start_s = timestamps[i][0]
                end_s = start_s + max(timestamps[i][1], 1.5)  # 至少 1.5s
                srt_entries.append((start_s, end_s, text))

            # 进度 75% → 95%
            if progress_callback and n > 0:
                pct = 80 + (i + 1) / n * 15
                if i % 5 == 0 or i == n - 1:
                    progress_callback(round(pct, 1), f'OCR 识别 {i+1}/{n}')

        # 6. 写 SRT
        def fmt(t):
            ms = int((t - int(t)) * 1000)
            s = int(t)
            return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d},{ms:03d}"

        with open(output_path, 'w', encoding='utf-8') as f:
            for idx, (start, end, text) in enumerate(srt_entries, 1):
                f.write(f"{idx}\n{fmt(start)} --> {fmt(end)}\n{text}\n\n")

        if not srt_entries:
            return {'success': False, 'error': 'OCR 没识别出任何文字（字幕可能为空或图像质量太差）'}

        file_size = os.path.getsize(output_path)

    if progress_callback:
        progress_callback(100, f'OCR 完成 ({len(srt_entries)} 条字幕, {file_size/1024:.1f} KB)')

    return {
        'success': True,
        'output_path': output_path,
        'codec_name': target['codec_name'],
        'language': target.get('language', ''),
        'title': target.get('title', ''),
        'file_size': file_size,
        'frames_processed': len(srt_entries),
        'duration': duration,
    }
