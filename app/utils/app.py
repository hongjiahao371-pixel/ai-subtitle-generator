import os
import sys
import re
import json
import time
import logging
import threading
import subprocess as sp
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_from_directory, Response
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge

# 添加app目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.subtitle_generator import (
    SubtitleGenerator, load_llm_config, save_llm_config, llm_translate_ready, llm_chat, _subtitle_character_limits
)
# 2026-06-22 Phase B: TaskQueue 集成
from utils.task_queue import TaskQueue
from utils.artifacts import (BUNDLE_LOCK, FORMATS, reserve_stem, release_reservation,
    metadata_path, read_metadata, write_metadata, copy_metadata, atomic_json, layout_segments, semantic_segments, display_width)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 配置
_base_dir = os.path.dirname(os.path.abspath(__file__))
app.config['UPLOAD_FOLDER'] = os.path.join(_base_dir, '..', 'uploads')
app.config['SUBTITLE_FOLDER'] = os.path.join(_base_dir, '..', 'subtitles')
app.config['AUDIO_FOLDER'] = os.path.join(_base_dir, '..', 'audio')
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024 * 1024  # 8GB限制（内网 NAS，整部电影单文件也可上传）
app.config['ALLOWED_EXTENSIONS'] = {
    'mp4', 'avi', 'mkv', 'mov', 'wmv', 'flv', 'webm', 'm4v', 'mpg', 'mpeg',
    'mp3', 'wav', 'flac', 'aac', 'ogg', 'm4a'
}
MEDIA_EXTENSIONS = {'.' + ext for ext in app.config['ALLOWED_EXTENSIONS']}
VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg'}
SUBTITLE_EXTENSIONS = {'.srt', '.vtt', '.json', '.ass', '.txt'}
REPORT_FOLDER = os.path.join(_base_dir, '..', 'reports')

# 全局变量
subtitle_generator = None
processing_tasks = {}
processing_tasks_lock = threading.Lock()

def allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def safe_filename(filename):
    """保留中文文件名，同时移除路径和文件系统不安全字符。"""
    filename = os.path.basename(filename or '').strip()
    filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', filename)
    filename = re.sub(r'\s+', ' ', filename).strip(' .')
    if filename in ('', '.', '..'):
        filename = secure_filename(filename) or 'upload'
    return filename[:240]

def _unique_path(folder, filename):
    """目标路径已存在时追加 _2/_3 序号，避免同秒批量上传互相覆盖。

    时间戳前缀只精确到秒，同名文件在同一秒内保存两次会静默覆盖。
    """
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(folder, filename)
    n = 2
    while os.path.lexists(candidate):
        candidate = os.path.join(folder, f"{base}_{n}{ext}")
        n += 1
    return candidate

# 允许的字幕/媒体后缀（路径遍历 + 注入防御）
# 文件名字符白名单只做一件事：禁路径分隔符与控制字符。NAS 媒体文件名里
# & + # @ ' ! , 等都很常见（"Tom & Jerry's [4K].mkv"），早先把白名单收得太窄，
# 这些文件能被列出却无法预览/烧录（_safe_join 一律拒绝）。
_SAFE_FILENAME_RE = re.compile(r'^[^/\\\x00-\x1f]{1,240}$')
_SAFE_VIDEO_EXTS = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg'}
_SAFE_AUDIO_EXTS = {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a'}
_SAFE_SUBTITLE_EXTS = {'.srt', '.vtt', '.json', '.ass', '.txt'}

def _is_safe_basename(name):
    """检查文件名是否不含路径分隔符/越界字符。允许中文、空格及常见标点。"""
    if not name or not isinstance(name, str):
        return False
    if len(name) > 240 or name in ('', '.', '..'):
        return False
    if not name.strip():
        return False
    # 路径分隔符绝对禁止
    if '/' in name or '\\' in name:
        return False
    # null 字节
    if '\x00' in name:
        return False
    # 隐藏文件（前端展示不友好，且 .htaccess 等敏感文件）
    if name.startswith('.'):
        return False
    return bool(_SAFE_FILENAME_RE.match(name))

def _safe_join(base, name):
    """把 name 安全拼到 base 下，解析符号链接后必须仍在 base 内。

    任何路径越界（含 '..'、绝对路径、符号链接逃逸）都返回 None，调用方应拒绝。
    """
    if not _is_safe_basename(name):
        return None
    base_real = os.path.realpath(base)
    candidate = os.path.realpath(os.path.join(base_real, name))
    # 必须仍在 base_real 内（用 os.sep 防止 /foo/barbaz 这种前缀欺骗）
    if candidate != base_real and not candidate.startswith(base_real + os.sep):
        return None
    return candidate

def _safe_join_allow_subpath(base, name):
    """允许 <path:> 风格的多段子路径，但仍必须落在 base 下。

    拒绝：绝对路径、'..' 段、符号链接逃逸。
    """
    if not name or not isinstance(name, str):
        return None
    if len(name) > 1024:
        return None
    # 拆分后每段必须合法
    parts = name.replace('\\', '/').split('/')
    for p in parts:
        if not p or p in ('.', '..') or p.startswith('.'):
            return None
        if not _SAFE_FILENAME_RE.match(p):
            return None
    base_real = os.path.realpath(base)
    candidate = os.path.realpath(os.path.join(base_real, *parts))
    if candidate != base_real and not candidate.startswith(base_real + os.sep):
        return None
    return candidate

def _safe_relpath(base, path):
    """Return a normalized relative path if path is under base."""
    base_real = os.path.realpath(base)
    candidate = os.path.realpath(path)
    if candidate != base_real and not candidate.startswith(base_real + os.sep):
        return None
    rel = os.path.relpath(candidate, base_real)
    return '' if rel == '.' else rel.replace(os.sep, '/')

def _safe_nas_path(path_or_rel):
    """Resolve an absolute or relative NAS path under NAS_PATH."""
    nas_path = os.environ.get('NAS_PATH', '/nas/videos')
    if not path_or_rel:
        return nas_path
    value = str(path_or_rel)
    if os.path.isabs(value):
        rel = _safe_relpath(nas_path, value)
        if rel is None:
            return None
        return _safe_join_allow_subpath(nas_path, rel) if rel else os.path.realpath(nas_path)
    return _safe_join_allow_subpath(nas_path, value)

def _subtitle_candidates_for_media(media_path, include_external=True):
    base, _ = os.path.splitext(media_path)
    candidates = []
    for ext in ('.srt', '.vtt', '.ass', '.txt', '.json'):
        candidates.append(base + ext)
        candidates.append(base + '.zh' + ext)
        candidates.append(base + '.en' + ext)
        candidates.append(base + '.zh-en' + ext)
    if include_external:
        subtitle_dir = app.config['SUBTITLE_FOLDER']
        media_name = os.path.splitext(os.path.basename(media_path))[0]
        for ext in ('.srt', '.vtt', '.ass', '.txt', '.json'):
            candidates.append(os.path.join(subtitle_dir, media_name + ext))
            candidates.append(os.path.join(subtitle_dir, media_name + '.zh' + ext))
            candidates.append(os.path.join(subtitle_dir, media_name + '.en' + ext))
            candidates.append(os.path.join(subtitle_dir, media_name + '.zh-en' + ext))
    seen = set()
    result = []
    for item in candidates:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result

def _existing_subtitles_for_media(media_path):
    items = []
    for path in _subtitle_candidates_for_media(media_path):
        if os.path.isfile(path):
            items.append({
                'filename': os.path.basename(path),
                'path': path,
                'size': os.path.getsize(path),
                'modified': datetime.fromtimestamp(os.path.getmtime(path)).astimezone().isoformat(),
            })
    return items

def _build_history_item(task_id, payload, task_row, result, status):
    return {
        'task_id': task_id,
        'filename': payload.get('filename') or os.path.basename(task_row.video_path),
        'status': status,
        'model': payload.get('model'),
        'language': payload.get('language'),
        'format': payload.get('format'),
        'translate_to': payload.get('translate_to'),
        'save_strategy': payload.get('save_strategy'),
        'output_naming': payload.get('output_naming'),
        'bilingual': bool(payload.get('bilingual')),
        'is_nas_file': bool(payload.get('is_nas_file')),
        'result': result,
        'error': None if status == 'completed' else (result or {}).get('error'),
        'timestamp': datetime.now().isoformat(),
    }

def _append_history_item(item, max_items=200):
    history = _load_history()
    history.append(item)
    if len(history) > max_items:
        history = history[-max_items:]
    _save_history(history)

def _json_file_load(path, default):
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"读取 JSON 文件失败 {path}: {e}")
    return default

def _json_file_save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)

def _safe_subtitle_path(filename):
    if not _is_safe_basename(filename):
        return None
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _SAFE_SUBTITLE_EXTS:
        return None
    return _safe_join(app.config['SUBTITLE_FOLDER'], filename)

def _normalize_segment(seg, idx=0):
    try:
        start = max(0.0, float(seg.get('start', 0)))
        end = max(start + 0.01, float(seg.get('end', start + 0.01)))
    except Exception:
        start = 0.0
        end = 0.01
    return {
        'id': seg.get('id', idx + 1),
        'start': start,
        'end': end,
        'text': str(seg.get('text', '') or '').strip(),
    }

def _parse_ass_time(value):
    try:
        h, m, rest = value.strip().split(':')
        s, cs = rest.split('.')
        return int(h) * 3600 + int(m) * 60 + int(s) + int(cs.ljust(2, '0')[:2]) / 100
    except Exception:
        return 0

def _format_timestamp_ass(seconds):
    seconds = max(0.0, float(seconds or 0))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = min(99, int(round((seconds % 1) * 100)))
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"

def _parse_subtitle_file(subtitle_path):
    ext = os.path.splitext(subtitle_path)[1].lower()
    with open(subtitle_path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    if ext == '.json':
        data = json.loads(content or '{}')
        return [_normalize_segment(seg, i) for i, seg in enumerate(data.get('segments', []))]

    if ext == '.txt':
        text = content.strip()
        return [{'id': 1, 'start': 0.0, 'end': 0.01, 'text': text}] if text else []

    if ext == '.srt':
        segments = []
        for block in re.split(r'\n\s*\n', content.strip()):
            lines = [line.rstrip('\r') for line in block.splitlines() if line.strip()]
            if len(lines) < 2:
                continue
            time_line_index = 1 if '-->' in lines[1] else 0
            if '-->' not in lines[time_line_index]:
                continue
            start_str, end_str = lines[time_line_index].split('-->', 1)
            segments.append(_normalize_segment({
                'start': _parse_srt_time(start_str.strip()),
                'end': _parse_srt_time(end_str.strip().split()[0]),
                'text': '\n'.join(lines[time_line_index + 1:]).strip(),
            }, len(segments)))
        return segments

    if ext == '.vtt':
        segments = []
        lines = content.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line or line.startswith('WEBVTT') or line.startswith('NOTE'):
                i += 1
                continue
            if '-->' not in line and i + 1 < len(lines) and '-->' in lines[i + 1]:
                i += 1
                line = lines[i].strip()
            if '-->' in line:
                start_str, end_str = line.split('-->', 1)
                i += 1
                text_lines = []
                while i < len(lines) and lines[i].strip():
                    text_lines.append(lines[i].strip())
                    i += 1
                segments.append(_normalize_segment({
                    'start': _parse_vtt_time(start_str.strip()),
                    'end': _parse_vtt_time(end_str.strip().split()[0]),
                    'text': '\n'.join(text_lines).strip(),
                }, len(segments)))
            i += 1
        return segments

    if ext == '.ass':
        segments = []
        for line in content.splitlines():
            if not line.startswith('Dialogue:'):
                continue
            payload = line.split(':', 1)[1].strip()
            parts = payload.split(',', 9)
            if len(parts) < 10:
                continue
            text = parts[9].replace('\\N', '\n').replace(r'\n', '\n')
            text = re.sub(r'\{[^}]*\}', '', text).strip()
            segments.append(_normalize_segment({
                'start': _parse_ass_time(parts[1]),
                'end': _parse_ass_time(parts[2]),
                'text': text,
            }, len(segments)))
        return segments

    raise ValueError(f'不支持的字幕格式: {ext}')

def _save_ass_segments(segments, output_path):
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('[Script Info]\nScriptType: v4.00+\nPlayResX: 1280\nPlayResY: 720\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n')
        f.write('[V4+ Styles]\n')
        f.write('Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, ')
        f.write('Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, ')
        f.write('Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n')
        f.write('Style: Default,Noto Sans CJK SC,24,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,')
        f.write('100,100,0,0,1,2,0,2,30,30,28,1\n\n')
        f.write('[Events]\n')
        f.write('Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n')
        for seg in segments:
            text = str(seg.get('text', '')).strip().replace('\n', r'\N')
            f.write(f"Dialogue: 0,{_format_timestamp_ass(seg['start'])},{_format_timestamp_ass(seg['end'])},Default,,0,0,0,,{text}\n")

def _save_txt_segments(segments, output_path):
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(str(seg.get('text', '')).strip() for seg in segments if str(seg.get('text', '')).strip()))

def _save_segments_as(segments, output_path, format_type):
    normalized = [_normalize_segment(seg, i) for i, seg in enumerate(segments)]
    format_type = (format_type or '').lower()
    if format_type == 'srt':
        _save_srt_segments(normalized, output_path)
    elif format_type == 'vtt':
        _save_vtt_segments(normalized, output_path)
    elif format_type == 'json':
        _save_json_segments(normalized, output_path)
    elif format_type == 'ass':
        _save_ass_segments(normalized, output_path)
    elif format_type == 'txt':
        _save_txt_segments(normalized, output_path)
    else:
        raise ValueError(f'不支持的字幕格式: {format_type}')

# 字幕规范预设：对标主流平台/广播的可读性要求。
# max_chars  单行最大字符数（中日韩按字符计，与英文规范同量级）
# max_lines  单条字幕最大行数
# max_cps    最大阅读速度（字符/秒）
# min_dur    单条最短显示时长（秒），低于此值来不及读
# min_gap    相邻字幕最小间隔（秒），给读者留呼吸
SUBTITLE_PROFILES = {
    'default':  {'max_chars': 42, 'max_lines': 2, 'max_cps': 18, 'min_duration': 0.7, 'min_gap': 0.08},
    'netflix':  {'max_chars': 42, 'max_lines': 2, 'max_cps': 20, 'min_duration': 0.7, 'min_gap': 0.08},
    'youtube':  {'max_chars': 42, 'max_lines': 2, 'max_cps': 20, 'min_duration': 0.8, 'min_gap': 0.10},
    'bbc':      {'max_chars': 37, 'max_lines': 2, 'max_cps': 15, 'min_duration': 1.0, 'min_gap': 0.15},
}


def _quality_report_for_segments(segments, max_chars=42, max_lines=2, max_cps=18,
                                 min_duration=0.7, min_gap=0.08, profile='default', bilingual=False):
    """字幕质量检查。向后兼容：只保留原 5 类检查，另加行数/时长/间隔三类。"""
    issues = []
    empty = overlaps = long_lines = too_fast = invalid_time = 0
    too_many_lines = too_short = no_gap = 0
    previous_end = None
    latin_fast = cjk_fast = 0
    max_latin = max_cjk = 0.0
    for idx, seg in enumerate(segments, 1):
        text = str(seg.get('text', '') or '').strip()
        start = float(seg.get('start', 0) or 0)
        end = float(seg.get('end', 0) or 0)
        duration = max(end - start, 0)
        if not text:
            empty += 1
            issues.append({'index': idx, 'type': 'empty', 'message': '空字幕', 'severity': 'warning'})
        if end <= start:
            invalid_time += 1
            issues.append({'index': idx, 'type': 'invalid_time', 'message': '结束时间不大于开始时间', 'severity': 'error'})
        if idx > 1 and start < previous_end:
            overlaps += 1
            issues.append({'index': idx, 'type': 'overlap', 'message': '与上一条字幕时间重叠', 'severity': 'error'})
        elif idx > 1 and min_gap > 0 and 0 <= (start - previous_end) and start - previous_end + 1e-6 < min_gap:
            no_gap += 1
            issues.append({'index': idx, 'type': 'no_gap',
                           'message': f'与上一条间隔过小 ({start - previous_end:.2f} 秒)', 'severity': 'warning'})

        lines = [ln for ln in text.splitlines() if ln.strip()]
        is_bilingual = bilingual or seg.get('bilingual', False)
        longest_line = max([display_width(ln) if is_bilingual else len(ln) for ln in lines] or [0])
        if longest_line > max_chars:
            long_lines += 1
            issues.append({'index': idx, 'type': 'long_line',
                           'message': f'单行过长 ({longest_line} 字, 上限 {max_chars})', 'severity': 'warning'})
        language_line_counts = [sum(bool(display_width(ln)>len(ln)) for ln in lines), sum(not bool(display_width(ln)>len(ln)) for ln in lines)]
        if max_lines and (len(lines) > max_lines * (2 if is_bilingual else 1) or (is_bilingual and max(seg.get('source_lines',0),seg.get('target_lines',0),*language_line_counts) > max_lines)):
            too_many_lines += 1
            issues.append({'index': idx, 'type': 'too_many_lines',
                           'message': f'行数过多 ({len(lines)} 行, 每种语言上限 {max_lines})', 'severity': 'warning'})

        # 时长：太短读不完；太长说明可能没按语义断句
        if text and duration > 0 and duration < (max(min_duration,1.2) if is_bilingual else min_duration):
            too_short += 1
            issues.append({'index': idx, 'type': 'too_short',
                           'message': f'显示过短 ({duration:.2f} 秒, 建议 >= {max(min_duration,1.2) if is_bilingual else min_duration})',
                           'severity': 'warning'})

        if is_bilingual:
            # Languages are read separately. CJK reading limit is half Latin.
            rates = [(len(ln)/duration)*(2 if re.search(r'[\u3400-\u9fff]',ln) else 1) for ln in lines] if duration>0 else [0]
            if seg.get('source_text'):
                rates = [len(t.replace('\n',''))/duration*(2 if re.search(r'[\u3400-\u9fff]',t) else 1) for t in (seg['source_text'],seg['target_text'])] if duration>0 else [0]
            else:
                latin = ''.join(ln for ln in lines if not re.search(r'[\u3400-\u9fff]',ln)); cjk = ''.join(ln for ln in lines if re.search(r'[\u3400-\u9fff]',ln))
                rates = [len(latin)/duration,2*len(cjk)/duration] if duration>0 else [0]
            latin_text=''.join(ln for ln in lines if display_width(ln)==len(ln))
            cjk_text=''.join(ln for ln in lines if display_width(ln)>len(ln))
            latin_rate=len(latin_text)/duration if duration>0 else 0
            cjk_rate=len(cjk_text)/duration if duration>0 else 0
            latin_fast+=int(latin_rate>max_cps);cjk_fast+=int(cjk_rate>max_cps/2)
            max_latin=max(max_latin,latin_rate);max_cjk=max(max_cjk,cjk_rate)
            cps = max(rates)
        else:
            cps = len(text.replace('\n', '')) / duration if duration > 0 else 0
        if cps > max_cps:
            too_fast += 1
            issues.append({'index': idx, 'type': 'too_fast',
                           'message': f'阅读速度偏快 ({cps:.1f} 字/秒, 上限 {max_cps})', 'severity': 'warning'})

        previous_end = max(previous_end, end) if previous_end is not None else end

    penalty = (invalid_time * 12 + overlaps * 10 + empty * 5 + long_lines * 2
               + too_fast * 3 + too_many_lines * 2 + too_short * 2 + no_gap * 1)
    score = max(0, min(100, 100 - penalty))
    return {
        'score': score,
        'profile': profile,
        'thresholds': {
            'max_chars': max_chars, 'max_lines': max_lines, 'max_cps': max_cps,
            'min_duration': max(min_duration,1.2) if bilingual or any(x.get('bilingual') for x in segments) else min_duration, 'min_gap': min_gap,
        },
        'reading_by_script': {'latin_too_fast':latin_fast,'cjk_too_fast':cjk_fast,'max_latin_cps':round(max_latin,2),'max_cjk_cps':round(max_cjk,2),'latin_limit':max_cps,'cjk_limit':max_cps/2},
        'layout': '每种语言最多两行，CJK按双宽字符；阅读速度按语言分别计算' if bilingual or any(x.get('bilingual') for x in segments) else '单语',
        'total_segments': len(segments),
        'duration': segments[-1]['end'] if segments else 0,
        'summary': {
            'empty': empty,
            'overlaps': overlaps,
            'long_lines': long_lines,
            'too_fast': too_fast,
            'invalid_time': invalid_time,
            'too_many_lines': too_many_lines,
            'too_short': too_short,
            'no_gap': no_gap,
        },
        'issues': issues[:200],
    }


GLOSSARY_FILE = os.path.join(app.config['SUBTITLE_FOLDER'], '.glossaries.json')
NOTIFICATION_FILE = os.path.join(app.config['SUBTITLE_FOLDER'], '.notification.json')

def _load_glossaries():
    return _json_file_load(GLOSSARY_FILE, {})


def _merged_glossary(glossary, glossary_name=None):
    """合并术语表（2026-10-27 v1.2.0）：全局术语表（名为 global）自动生效，
    任务自带术语 > 指定命名术语表 > 全局，逐级覆盖。"""
    out = {}
    g = _load_glossaries()
    if isinstance(g.get('global'), dict):
        out.update(g['global'])
    if glossary_name and isinstance(g.get(glossary_name), dict):
        out.update(g[glossary_name])
    if isinstance(glossary, dict):
        out.update(glossary)
    return out

def _load_notification_settings():
    data = _json_file_load(NOTIFICATION_FILE, {})
    if not isinstance(data, dict):
        return {}
    return data

def _send_webhook_notification(title, message, extra=None):
    settings = _load_notification_settings()
    if not settings.get('enabled') or not settings.get('webhook_url'):
        return False
    try:
        import urllib.request
        payload = {
            'title': title,
            'message': message,
            'extra': extra or {},
            'timestamp': datetime.now().isoformat(),
        }
        req = urllib.request.Request(
            settings['webhook_url'],
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        logger.warning(f"通知发送失败: {e}")
        return False

# === 可选鉴权（环境变量控制） ===
# 留 AUTH_TOKEN=""（默认）则不强制鉴权；设置后所有非 /api/health、/、/static、/templates 请求需要 Bearer token
_AUTH_TOKEN = os.environ.get('AUTH_TOKEN', '').strip()
if _AUTH_TOKEN:
    logger.info("鉴权已启用 (AUTH_TOKEN 长度=%d)", len(_AUTH_TOKEN))
else:
    logger.warning("未设置 AUTH_TOKEN，API 端点不鉴权。建议生产环境设置 AUTH_TOKEN 环境变量启用 Bearer 鉴权。")

_PUBLIC_PATHS = {'/', '/api/health', '/favicon.ico'}

@app.before_request
def _check_auth():
    from flask import request as _req
    if _req.path in _PUBLIC_PATHS or _req.path.startswith('/static') or _req.path.startswith('/templates'):
        return None
    if not _AUTH_TOKEN:
        return None
    import hmac
    # 优先从 Authorization header 读 token
    auth = _req.headers.get('Authorization', '')
    if hmac.compare_digest(auth, f'Bearer {_AUTH_TOKEN}'):
        return None
    # 兼容 SSE（EventSource 无法设 header）：从 query param ?token= 读
    query_token = _req.args.get('token', '')
    if query_token and hmac.compare_digest(query_token, _AUTH_TOKEN):
        return None
    logger.warning(f"拒绝未授权请求: {_req.method} {_req.path} from {_req.remote_addr}")
    return jsonify({'success': False, 'error': '未授权', 'auth_required': True}), 401

@app.before_request
def _validate_json_parameters():
    if request.method != 'POST' or not request.is_json:
        return None
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(success=False, error='请求内容必须是 JSON 对象'), 400
    import math
    numeric = {'split_max_chars': (0, 200, int), 'num_speakers': (0, 6, int),
               'offset': (-86400, 86400, float), 'priority': (-100, 100, int),
               'limit': (1, 5000, int), 'days': (1, 3650, int),
               'stream_index': (0, 1000, int)}
    try:
        for key, (lo, hi, cast) in numeric.items():
            value = data.get(key)
            if value is None or value == '':
                continue
            if isinstance(value, bool):
                raise ValueError(key)
            value = cast(str(value))
            if not math.isfinite(value) or not lo <= value <= hi:
                raise ValueError(key)
        if 'files' in data and (not isinstance(data['files'], list) or
                               any(not isinstance(f, dict) for f in data['files'])):
            raise ValueError('files')
        if 'glossary' in data and not isinstance(data['glossary'], (dict, str)):
            raise ValueError('glossary')
        if 'segments' in data:
            if not isinstance(data['segments'], list) or not data['segments']:
                raise ValueError('segments')
            for seg in data['segments']:
                if not isinstance(seg, dict):
                    raise ValueError('segments')
                a, b = float(seg['start']), float(seg['end'])
                if not math.isfinite(a) or not math.isfinite(b) or a < 0 or b <= a:
                    raise ValueError('segments 时间')
    except (TypeError, ValueError, KeyError) as e:
        return jsonify(success=False, error=f'参数非法: {e}'), 400


@app.route('/api/auth/check', methods=['GET'])
def auth_check():
    """校验 token 是否有效。命中即说明鉴权通过（未启用鉴权时恒通过）。

    前端启动时和「保存 Token」前调用：401=token 无效，200=有效。
    注意本路由刻意不在 _PUBLIC_PATHS 里。
    """
    return jsonify({'success': True, 'auth_enabled': bool(_AUTH_TOKEN), 'message': 'token 有效'})

@app.route('/favicon.ico', methods=['GET'])
def favicon():
    # 内联 SVG，消除每次页面加载的 401/404 日志噪音
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
           '<rect width="64" height="64" rx="14" fill="#1f2937"/>'
           '<path d="M18 22h20a8 8 0 0 1 8 8v12a8 8 0 0 1-8 8H18a8 8 0 0 1-8-8V30a8 8 0 0 1 8-8z" fill="#3b82f6"/>'
           '<path d="M46 30l10-6v22l-10-6z" fill="#3b82f6"/></svg>')
    return Response(svg, mimetype='image/svg+xml', headers={'Cache-Control': 'public, max-age=86400'})


@app.after_request
def _cors_headers(response):
    """最小化 CORS 头：默认只允许同源 + 配置的额外源。"""
    from flask import request as _req
    allowed = os.environ.get('CORS_ALLOWED_ORIGINS', '').split(',')
    allowed = [o.strip() for o in allowed if o.strip()]
    origin = _req.headers.get('Origin', '')
    # 修复安全漏洞：原 `if origin and (not allowed or origin in allowed)` 在 allowed 为空时
    # (not allowed)=True，会反射任意 Origin，等于完全放开 CORS。现改为：未配置白名单时
    # 不返回 ACAO 头（仅同源可用），配置了才反射白名单内的 Origin。
    if origin and allowed and origin in allowed:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Vary'] = 'Origin'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        response.headers['Access-Control-Max-Age'] = '3600'
    return response

@app.errorhandler(Exception)
def _handle_unhandled(e):
    """全局异常兜底：4xx 客户端错误降级（不打 traceback），5xx 保持 ERROR + 完整 traceback。"""
    from werkzeug.exceptions import HTTPException
    from flask import jsonify as _jsonify
    if isinstance(e, HTTPException):
        # 404 是最常见噪音（favicon.ico / 探测 / 旧链接），降到 INFO 不打 traceback
        if e.code == 404:
            try:
                path = request.path
            except Exception:
                path = 'n/a'
            logger.info(f"404: {e.description} (path={path})")
        else:
            logger.warning(f"客户端错误 {e.code}: {e.description}")
        return _jsonify({'success': False, 'error': e.description, 'code': e.code}), e.code
    # 真正的 5xx 异常：保留 ERROR + 完整 traceback
    import traceback
    logger.error(f"未处理异常: {e}\n{traceback.format_exc()}")
    return _jsonify({'success': False, 'error': '服务器内部错误'}), 500

@app.route('/')
def index():
    """主页"""
    return render_template('index.html')

@app.route('/api/health', methods=['GET'])
def health_check():
    """健康检查"""
    # 检测系统资源
    system_info = {}
    try:
        import psutil
        system_info.update({
            'cpu_count': psutil.cpu_count(),
            'memory_total_gb': psutil.virtual_memory().total / (1024**3),
            'memory_available_gb': psutil.virtual_memory().available / (1024**3),
            'memory_percent': psutil.virtual_memory().percent
        })
    except ImportError:
        system_info['error'] = 'psutil未安装'
    # Health probes must not import gigabytes of optional Torch dependencies.
    # ASR uses CTranslate2; report CUDA only if its runtime is already loaded.
    torch_runtime=sys.modules.get('torch')
    system_info['cuda_available']=False
    system_info['cuda_probe']='runtime-not-loaded'
    if torch_runtime is not None and hasattr(torch_runtime,'cuda'):
        system_info['cuda_available']=torch_runtime.cuda.is_available()
        system_info['cuda_probe']='loaded-runtime'
    elif 'ctranslate2' in sys.modules:
        system_info['cuda_available']=sys.modules['ctranslate2'].get_cuda_device_count()>0
        system_info['cuda_probe']='loaded-runtime'
    
    return jsonify({
        'status': 'ok',
        'model_loaded': __import__('utils.ai_memory',fromlist=['asr_weights_loaded']).asr_weights_loaded(SubtitleGenerator._model_cache),
        'memory_mode': os.getenv('LOCAL_LLM_MEMORY_MODE','0')=='1',
        'version': '2026.10.28.1',
        'auth_enabled': bool(_AUTH_TOKEN),
        'model_size': os.getenv('MODEL_SIZE', 'base'),
        'system_info': system_info,
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/system-info', methods=['GET'])
def system_info():
    """获取详细系统信息"""
    try:
        import psutil
    except ImportError:
        return jsonify({'success': False, 'error': 'psutil未安装'}), 500
    try:
        import torch
        has_torch = True
    except ImportError:
        has_torch = False

    try:
        info = {
            'cpu': {
                'count': psutil.cpu_count(),
                'percent': psutil.cpu_percent(interval=1),
                'freq': psutil.cpu_freq()._asdict() if psutil.cpu_freq() else None
            },
            'memory': {
                'total_gb': psutil.virtual_memory().total / (1024**3),
                'available_gb': psutil.virtual_memory().available / (1024**3),
                'percent': psutil.virtual_memory().percent,
                'used_gb': psutil.virtual_memory().used / (1024**3)
            },
            'disk': {
                'total_gb': psutil.disk_usage('/').total / (1024**3),
                'free_gb': psutil.disk_usage('/').free / (1024**3),
                'percent': psutil.disk_usage('/').percent
            }
        }
        
        if has_torch and torch.cuda.is_available():
            info['gpu'] = {
                'available': True,
                'device_count': torch.cuda.device_count(),
                'device_name': torch.cuda.get_device_name(0),
                'memory_total_gb': torch.cuda.get_device_properties(0).total_memory / (1024**3),
                'memory_allocated_gb': torch.cuda.memory_allocated(0) / (1024**3),
                'memory_cached_gb': torch.cuda.memory_reserved(0) / (1024**3)
            }
        else:
            info['gpu'] = {'available': False}

        # 核显硬编可见性（2026-10-21）：安装链路是否保留 devices 直通、驱动是否可用，
        # 商店装机后无需 SSH 即可远程诊断（探针进程内缓存，首次调用多花约 1-15s）
        try:
            info['vaapi'] = {
                'dev_dri': os.path.exists('/dev/dri/renderD128'),
                'available': bool(_probe_vaapi()),
            }
        except Exception as _ve:
            info['vaapi'] = {'dev_dri': os.path.exists('/dev/dri/renderD128'), 'available': False, 'error': str(_ve)}

        # 获取当前模型信息
        if subtitle_generator:
            info['current_model'] = {
                'size': subtitle_generator.model_size,
                'device': subtitle_generator.device,
                'loaded': subtitle_generator.model is not None
            }
        
        return jsonify({
            'success': True,
            'info': info
        })
        
    except Exception as e:
        logger.error(f"获取系统信息失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/nas-files', methods=['GET'])
def list_nas_files():
    """列出NAS挂载目录中的媒体文件，支持递归、搜索和目录浏览。"""
    try:
        nas_path = os.environ.get('NAS_PATH', '/nas/videos')  # NAS挂载路径，可通过环境变量配置
        if not os.path.exists(nas_path):
            return jsonify({
                'success': False,
                'error': 'NAS视频目录未配置。请在docker-compose.yml中添加卷挂载，例如: - /your/video/path:/nas/videos',
                'hint': '编辑docker-compose.yml，在volumes下添加NAS视频路径映射后重启容器'
            }), 404

        current_rel = request.args.get('dir', '').strip('/')
        recursive = request.args.get('recursive', '0') in ('1', 'true', 'yes')
        search = request.args.get('search', '').strip().lower()
        max_files = min(max(int(request.args.get('limit', 500)), 1), 5000)
        current_dir = _safe_join_allow_subpath(nas_path, current_rel) if current_rel else os.path.realpath(nas_path)
        if current_dir is None or not os.path.isdir(current_dir):
            return jsonify({'success': False, 'error': '目录不存在或路径非法'}), 404

        directories = []
        files = []

        if recursive:
            walker = os.walk(current_dir)
        else:
            children = os.listdir(current_dir)
            child_dirs = [n for n in children if os.path.isdir(os.path.join(current_dir, n))]
            walker = [(current_dir, child_dirs, children)]

        for root, dirs, names in walker:
            dirs[:] = [d for d in dirs if not d.startswith('.') and _is_safe_basename(d)]
            if root == current_dir:
                for dirname in sorted(dirs):
                    dir_path = os.path.join(root, dirname)
                    rel = _safe_relpath(nas_path, dir_path)
                    directories.append({
                        'name': dirname,
                        'path': dir_path,
                        'relative_path': rel,
                    })

            for filename in sorted(names):
                if len(files) >= max_files:
                    break
                filepath = os.path.join(root, filename)
                if not os.path.isfile(filepath):
                    continue
                ext = os.path.splitext(filename)[1].lower()
                if ext in MEDIA_EXTENSIONS:
                    rel = _safe_relpath(nas_path, filepath)
                    if rel is None:
                        continue
                    haystack = (filename + ' ' + rel).lower()
                    if search and search not in haystack:
                        continue
                    existing = _existing_subtitles_for_media(filepath)
                    files.append({
                        'name': filename,
                        'path': filepath,
                        'relative_path': rel,
                        'directory': os.path.dirname(rel),
                        'size': os.path.getsize(filepath),
                        'modified': datetime.fromtimestamp(os.path.getmtime(filepath)).astimezone().isoformat(),
                        'has_subtitle': bool(existing),
                        'subtitles': existing,
                    })
            if len(files) >= max_files:
                break

        files.sort(key=lambda x: x['name'])
        parent = ''
        if current_rel:
            parent = os.path.dirname(current_rel).replace(os.sep, '/')

        return jsonify({
            'success': True,
            'base_path': os.path.realpath(nas_path),
            'current_dir': current_rel,
            'parent_dir': parent,
            'recursive': recursive,
            'search': search,
            'directories': directories,
            'files': files,
            'truncated': len(files) >= max_files,
        })
        
    except Exception as e:
        logger.error(f"获取NAS文件列表失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.errorhandler(RequestEntityTooLarge)
def file_too_large(e):
    return jsonify({'success': False, 'error': '文件大小超过限制(最大8GB)'}), 413

@app.route('/api/upload', methods=['POST'])
def upload_file():
    """上传视频文件"""
    try:
        # 检查是否有文件
        if 'video' not in request.files:
            return jsonify({'success': False, 'error': '没有上传文件'}), 400
        
        file = request.files['video']
        
        if file.filename == '':
            return jsonify({'success': False, 'error': '没有选择文件'}), 400
        
        if not allowed_file(file.filename):
            return jsonify({
                'success': False,
                'error': f'不支持的文件格式。支持的格式: {", ".join(app.config["ALLOWED_EXTENSIONS"])}'
            }), 400
        
        # 保存文件
        filename = safe_filename(file.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{timestamp}_{filename}"
        filepath = _unique_path(app.config['UPLOAD_FOLDER'], filename)
        filename = os.path.basename(filepath)

        file.save(filepath)
        
        logger.info(f"文件上传成功: {filename}")
        
        return jsonify({
            'success': True,
            'filename': filename,
            'filepath': filepath,
            'message': '文件上传成功'
        })
        
    except Exception as e:
        logger.error(f"上传失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/upload-batch', methods=['POST'])
def upload_batch():
    """批量上传视频文件"""
    try:
        if 'videos' not in request.files:
            return jsonify({'success': False, 'error': '没有上传文件'}), 400
        
        files = request.files.getlist('videos')
        uploaded_files = []
        failed_files = []
        
        for file in files:
            if file.filename == '':
                continue
                
            if not allowed_file(file.filename):
                failed_files.append({
                    'filename': file.filename,
                    'error': '不支持的文件格式'
                })
                continue
            
            try:
                # 保存文件
                filename = safe_filename(file.filename)
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                saved_filename = f"{timestamp}_{filename}"
                filepath = _unique_path(app.config['UPLOAD_FOLDER'], saved_filename)
                saved_filename = os.path.basename(filepath)

                file.save(filepath)
                
                uploaded_files.append({
                    'original_name': file.filename,
                    'saved_name': saved_filename,
                    'filepath': filepath,
                    'size': os.path.getsize(filepath)
                })
                
                logger.info(f"批量上传文件成功: {saved_filename}")
                
            except Exception as e:
                failed_files.append({
                    'filename': file.filename,
                    'error': str(e)
                })
                logger.error(f"批量上传文件失败 {file.filename}: {str(e)}")
        
        return jsonify({
            'success': True,
            'uploaded_files': uploaded_files,
            'failed_files': failed_files,
            'total_uploaded': len(uploaded_files),
            'total_failed': len(failed_files),
            'message': f'成功上传 {len(uploaded_files)} 个文件'
        })
        
    except Exception as e:
        logger.error(f"批量上传失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/generate', methods=['POST'])
def generate_subtitle():
    """生成字幕"""
    try:
        data = request.get_json()
        
        if not data or 'filename' not in data:
            return jsonify({'success': False, 'error': '缺少文件名参数'}), 400
        
        filename = data['filename']
        path = data.get('path')
        is_nas_file = data.get('is_nas_file', False)
        model = data.get('model', os.getenv('MODEL_SIZE', 'small'))
        language = data.get('language', 'auto')
        format = data.get('format', 'srt')
        translate_to = data.get('translate_to', None)  # 翻译目标语言
        save_to_source_dir = data.get('save_to_source_dir', False)
        save_strategy = data.get('save_strategy') or ('source' if save_to_source_dir else 'subtitles')
        output_naming = data.get('output_naming', 'plain')
        bilingual = bool(data.get('bilingual', False))
        split_max_chars = int(data.get('split_max_chars') or 0)
        glossary = data.get('glossary') or {}
        translate_engine = data.get('translate_engine') or 'auto'
        diarize = bool(data.get('diarize', False))
        num_speakers = int(data.get('num_speakers') or 0)

        if model not in ('tiny', 'base', 'small', 'medium', 'large', 'large-v3-turbo'):
            return jsonify({'success': False, 'error': f'非法模型: {model}'}), 400
        if format not in ('srt', 'vtt', 'json', 'txt', 'ass', 'all'):
            return jsonify({'success': False, 'error': f'非法格式: {format}'}), 400
        if save_strategy not in ('subtitles', 'source'):
            return jsonify({'success': False, 'error': f'非法保存策略: {save_strategy}'}), 400
        if output_naming not in ('plain', 'language', 'bilingual'):
            return jsonify({'success': False, 'error': f'非法命名策略: {output_naming}'}), 400
        if translate_engine not in ('auto', 'llm', 'local'):
            return jsonify({'success': False, 'error': f'非法翻译引擎: {translate_engine}'}), 400
        if num_speakers and num_speakers not in (2, 3, 4, 5, 6):
            return jsonify({'success': False, 'error': f'非法说话人数: {num_speakers}'}), 400

        # Bug #11 修复：path 字段强制在 NAS_PATH 下，禁止任意路径
        if is_nas_file and path:
            filepath = _safe_nas_path(path)
            if filepath is None or not os.path.isfile(filepath):
                logger.warning(f"generate 拒绝非法 path: {path!r}")
                return jsonify({'success': False, 'error': '文件不存在或路径非法'}), 404
        else:
            # 上传文件：用 _safe_join 校验 filename
            if not _is_safe_basename(filename):
                return jsonify({'success': False, 'error': '非法文件名'}), 400
            filepath = _safe_join(app.config['UPLOAD_FOLDER'], filename)
            if filepath is None or not os.path.isfile(filepath):
                return jsonify({'success': False, 'error': f'文件不存在'}), 404

        # 全局术语库合并（v1.2.0）：global 自动生效，任务级覆盖
        glossary = _merged_glossary(glossary, None)

        # C1: 走 TaskQueue（统一管理 + 重启恢复）
        q = _get_task_queue()
        task_id = q.add('whisper', filepath, {
            'model': model,
            'language': language,
            'format': format,
            'translate_to': translate_to,
            'translate_engine': translate_engine,
            'diarize': diarize,
            'num_speakers': num_speakers,
            'save_to_source_dir': save_to_source_dir,
            'save_strategy': save_strategy,
            'output_naming': output_naming,
            'bilingual': bilingual,
            'split_max_chars': split_max_chars,
            'glossary': glossary,
            'filename': filename,
            'is_nas_file': is_nas_file,
        })

        cancel_event = threading.Event()
        with processing_tasks_lock:
            processing_tasks[task_id] = {
                'status': 'pending',
                'filename': filename,
                'model': model,
                'is_nas_file': is_nas_file,
                'save_strategy': save_strategy,
                'output_naming': output_naming,
                'bilingual': bilingual,
                'type': 'whisper',
                'start_time': datetime.now().isoformat(),
                'progress': 0,
                'progress_info': '任务已入队，等待 worker...',
                '_cancel_event': cancel_event,
            }

        logger.info(f"字幕生成任务入队: {task_id}, 模型: {model}, 文件: {filepath}")

        return jsonify({
            'success': True,
            'task_id': task_id,
            'message': '字幕生成任务已入队'
        })
        
    except Exception as e:
        logger.error(f"生成字幕失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/generate-batch', methods=['POST'])
def generate_batch():
    """批量生成字幕"""
    try:
        data = request.get_json()
        
        if not data or 'files' not in data:
            return jsonify({'success': False, 'error': '缺少文件列表参数'}), 400
        
        files = data['files']
        model = data.get('model', os.getenv('MODEL_SIZE', 'small'))
        language = data.get('language', 'auto')
        format = data.get('format', 'srt')
        translate_to = data.get('translate_to', None)  # 翻译目标语言
        save_to_source_dir = data.get('save_to_source_dir', False)
        save_strategy = data.get('save_strategy') or ('source' if save_to_source_dir else 'subtitles')
        output_naming = data.get('output_naming', 'plain')
        bilingual = bool(data.get('bilingual', False))
        split_max_chars = int(data.get('split_max_chars') or 0)
        glossary = data.get('glossary') or {}
        translate_engine = data.get('translate_engine') or 'auto'
        diarize = bool(data.get('diarize', False))
        num_speakers = int(data.get('num_speakers') or 0)

        # 参数校验（对齐单文件接口 /api/generate，批量入口原来漏了这五个参数的定义导致 NameError）
        if model not in ('tiny', 'base', 'small', 'medium', 'large', 'large-v3-turbo'):
            return jsonify({'success': False, 'error': f'非法模型: {model}'}), 400
        if format not in ('srt', 'vtt', 'json', 'txt', 'ass', 'all'):
            return jsonify({'success': False, 'error': f'非法格式: {format}'}), 400
        if save_strategy not in ('subtitles', 'source'):
            return jsonify({'success': False, 'error': f'非法保存策略: {save_strategy}'}), 400
        if output_naming not in ('plain', 'language', 'bilingual'):
            return jsonify({'success': False, 'error': f'非法命名策略: {output_naming}'}), 400
        if translate_engine not in ('auto', 'llm', 'local'):
            return jsonify({'success': False, 'error': f'非法翻译引擎: {translate_engine}'}), 400
        if num_speakers and num_speakers not in (2, 3, 4, 5, 6):
            return jsonify({'success': False, 'error': f'非法说话人数: {num_speakers}'}), 400

        # 修复：追加 uuid 避免同秒并发批量任务 batch_id 冲突
        import uuid as _uuid
        batch_id = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{_uuid.uuid4().hex[:6]}"
        task_ids = []
        
        for file_info in files:
            filename = file_info.get('filename')
            path = file_info.get('path')
            is_nas_file = file_info.get('is_nas_file', False)

            if not filename:
                continue

            # Bug #11 修复：path 必须落在 NAS_PATH 下
            if is_nas_file and path:
                filepath = _safe_nas_path(path)
                if filepath is None or not os.path.isfile(filepath):
                    continue
            else:
                if not _is_safe_basename(filename):
                    continue
                filepath = _safe_join(app.config['UPLOAD_FOLDER'], filename)
                if filepath is None or not os.path.isfile(filepath):
                    continue
            
            # D1: 走 TaskQueue
            q = _get_task_queue()
            task_id = q.add('whisper', filepath, {
                'model': model,
                'language': language,
                'format': format,
                'translate_to': translate_to,
                'translate_engine': translate_engine,
                'diarize': diarize,
                'num_speakers': num_speakers,
                'save_to_source_dir': save_to_source_dir,
                'save_strategy': save_strategy,
                'output_naming': output_naming,
                'bilingual': bilingual,
                'split_max_chars': split_max_chars,
                'glossary': glossary,
                'filename': filename,
                'is_nas_file': is_nas_file,
                'batch_id': batch_id,
            })

            cancel_event = threading.Event()
            with processing_tasks_lock:
                processing_tasks[task_id] = {
                    'status': 'pending',
                    'filename': filename,
                    'model': model,
                    'is_nas_file': is_nas_file,
                    'save_strategy': save_strategy,
                    'output_naming': output_naming,
                    'bilingual': bilingual,
                    'batch_id': batch_id,
                    'type': 'whisper',
                    'start_time': datetime.now().isoformat(),
                    'progress': 0,
                    'progress_info': '任务已入队，等待 worker...',
                    '_cancel_event': cancel_event,
                }

            task_ids.append(task_id)
            logger.info(f"批量字幕生成任务入队: {task_id}, 文件: {filepath}")
        
        return jsonify({
            'success': True,
            'batch_id': batch_id,
            'task_ids': task_ids,
            'total_tasks': len(task_ids),
            'message': f'批量字幕生成任务已启动，共 {len(task_ids)} 个文件'
        })
        
    except Exception as e:
        logger.error(f"批量生成字幕失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/generate-folder', methods=['POST'])
def generate_folder():
    """按目录批量生成字幕，可跳过已有字幕的媒体。"""
    try:
        data = request.get_json() or {}
        folder = data.get('folder', '')
        recursive = bool(data.get('recursive', True))
        skip_existing = data.get('skip_existing', True)
        limit = min(max(int(data.get('limit', 500)), 1), 5000)

        folder_path = _safe_nas_path(folder)
        if folder_path is None or not os.path.isdir(folder_path):
            return jsonify({'success': False, 'error': '目录不存在或路径非法'}), 404

        files = []
        walker = os.walk(folder_path) if recursive else [(folder_path, [], os.listdir(folder_path))]
        for root, _, names in walker:
            for name in sorted(names):
                if len(files) >= limit:
                    break
                path = os.path.join(root, name)
                if not os.path.isfile(path) or os.path.splitext(name)[1].lower() not in MEDIA_EXTENSIONS:
                    continue
                existing = _existing_subtitles_for_media(path)
                if skip_existing and existing:
                    continue
                rel = _safe_relpath(os.environ.get('NAS_PATH', '/nas/videos'), path)
                files.append({
                    'filename': name,
                    'path': rel or path,
                    'is_nas_file': True,
                })
            if len(files) >= limit:
                break

        if not files:
            return jsonify({
                'success': True,
                'batch_id': None,
                'task_ids': [],
                'total_tasks': 0,
                'message': '没有需要处理的媒体文件'
            })

        data['files'] = files
        with app.test_request_context(json=data):
            return generate_batch()
    except Exception as e:
        logger.error(f"目录批量生成失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/batch-status/<batch_id>', methods=['GET'])
def get_batch_status(batch_id):
    """获取批量任务状态"""
    try:
        q = _get_task_queue()
        batch_tasks = {t.id: _task_view(t) for t in q.list_batch(batch_id)}

        if not batch_tasks:
            return jsonify({'success': False, 'error': '批量任务不存在'}), 404
        
        # 统计状态
        status_counts = {'pending': 0, 'processing': 0, 'completed': 0, 'failed': 0, 'cancelled': 0}
        for task in batch_tasks.values():
            status_counts[task['status']] = status_counts.get(task['status'], 0) + 1
        
        # 计算总体进度
        total_progress = sum(task.get('progress', 0) for task in batch_tasks.values())
        avg_progress = total_progress // len(batch_tasks) if batch_tasks else 0
        
        overall_status = 'processing'
        if status_counts['completed'] == len(batch_tasks):
            overall_status = 'completed'
        elif status_counts['failed'] + status_counts['cancelled'] == len(batch_tasks):
            overall_status = 'failed'
        elif status_counts['processing'] + status_counts['pending'] == 0:
            overall_status = 'partial_completed'
        elif status_counts['processing'] == 0:
            overall_status = 'pending'
        
        safe_tasks = {
            tid: {k: v for k, v in task.items() if k not in ('_cancel_event', '_thread')}
            for tid, task in batch_tasks.items()
        }

        return jsonify({
            'success': True,
            'batch_id': batch_id,
            'overall_status': overall_status,
            'total_tasks': len(batch_tasks),
            'status_counts': status_counts,
            'avg_progress': avg_progress,
            'tasks': safe_tasks
        })
        
    except Exception as e:
        logger.error(f"获取批量任务状态失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

def _task_view(row):
    d = row.to_dict()
    payload = json.loads(d.get('payload') or '{}')
    result = json.loads(d.get('result') or 'null')
    d.update({k: payload.get(k) for k in ('model', 'is_nas_file', 'batch_id')})
    d.update(filename=payload.get('filename') or os.path.basename(row.video_path),
             result=result, end_time=row.finished_at)
    d.pop('payload', None)
    return d


@app.route('/api/status/<task_id>', methods=['GET'])
def get_task_status(task_id):
    """获取任务状态（兼容 in-memory dict + TaskQueue 兜底）"""
    q = _get_task_queue()
    t = q.get(task_id)
    if t:
        return jsonify({'success': True, 'task': _task_view(t)})
    if task_id not in processing_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404

    task = processing_tasks[task_id]
    safe_task = {k: v for k, v in task.items() if k not in ('_cancel_event', '_thread')}
    return jsonify({
        'success': True,
        'task': safe_task
    })

@app.route('/api/cancel/<task_id>', methods=['POST'])
def cancel_task(task_id):
    """取消任务"""
    if task_id not in processing_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404

    task = processing_tasks[task_id]

    # 修复 BUG：原仅允许 processing/cancelling 取消，但任务入队时为 pending 且
    # TaskQueue handler 不改内存状态，导致 pending/排队中的任务永远无法取消。
    # 现放宽：pending 和 processing 都可取消；已完成的拒绝。
    if task['status'] in ('completed', 'failed', 'cancelled'):
        return jsonify({
            'success': False,
            'error': '任务已结束，无法取消'
        }), 400

    # 同步取消 TaskQueue 数据库中的任务（pending 任务会被直接置 cancelled）
    try:
        q = _get_task_queue()
        q.cancel(task_id)
    except Exception as e:
        logger.warning(f"TaskQueue cancel 失败: {e}")

    # pending 任务直接标记取消（无需等 worker）
    if task['status'] == 'pending':
        task['status'] = 'cancelled'
        task['end_time'] = datetime.now().isoformat()
        logger.info(f"pending 任务已取消: {task_id}")
        return jsonify({'success': True, 'status': 'cancelled', 'message': '任务已取消'})

    # processing 任务：先设 cancelling，由后台线程真正退出后改 cancelled
    task['status'] = 'cancelling'
    if '_cancel_event' in task:
        task['_cancel_event'].set()
    # 烧录任务还需要 kill 进程
    with _burn_processes_lock:
        proc = _burn_processes.get(task_id)
    if proc and proc.poll() is None:
        try: proc.terminate()
        except Exception: pass

    logger.info(f"任务已请求取消: {task_id}")

    return jsonify({
        'success': True,
        'status': 'cancelling',
        'message': '任务已请求取消'
    })

def _cleanup_old_tasks(max_keep=100):
    """清理已完成的旧任务，防止内存泄漏"""
    if len(processing_tasks) <= max_keep:
        return
    with processing_tasks_lock:
        # 找出已完成的任务，按结束时间排序，删除最旧的
        completed_tasks = [
            (tid, task) for tid, task in processing_tasks.items()
            if task.get('status') in ('completed', 'failed', 'cancelled') and task.get('end_time')
        ]
        if len(processing_tasks) - len(completed_tasks) >= max_keep:
            # 如果运行中的任务已经超过上限，只清理已完成且最旧的
            completed_tasks.sort(key=lambda x: x[1].get('end_time', ''))
            to_remove = len(processing_tasks) - max_keep
            for tid, _ in completed_tasks[:to_remove]:
                processing_tasks.pop(tid, None)
        elif len(completed_tasks) > max_keep // 2:
            completed_tasks.sort(key=lambda x: x[1].get('end_time', ''))
            to_remove = len(completed_tasks) - max_keep // 2
            for tid, _ in completed_tasks[:to_remove]:
                processing_tasks.pop(tid, None)

@app.route('/api/subtitles', methods=['GET'])
def list_subtitles():
    """列出字幕目录下的产出文件。

    只收字幕扩展名和烧录产物视频（_subtitled.mp4），并标注 kind：
    此前不过滤扩展名，烧录出的 mp4 混进字幕列表，除下载外所有按钮全部错位。
    """
    try:
        subtitles = []
        for filename in os.listdir(app.config['SUBTITLE_FOLDER']):
            # 跳过隐藏文件（.history.json / .glossaries.json 等状态文件）
            if filename.startswith('.'):
                continue
            filepath = os.path.join(app.config['SUBTITLE_FOLDER'], filename)
            if not os.path.isfile(filepath):
                continue
            # 词级时间戳旁挂文件（<字幕名>.words.json）不是字幕，不能列进列表。
            # .json 本身是合法字幕格式，所以必须按整名排除，否则会被误判为字幕
            # 并套上编辑/烧录/质检这些不属于它的按钮。
            if filename.lower().endswith(('.words.json','.recognition.json','.translation.json')):
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext in SUBTITLE_EXTENSIONS:
                kind = 'subtitle'
            elif ext in VIDEO_EXTENSIONS:
                kind = 'video'
            else:
                continue
            subtitles.append({
                'filename': filename,
                'kind': kind,
                'size': os.path.getsize(filepath),
                'created': datetime.fromtimestamp(os.path.getctime(filepath)).astimezone().isoformat()
            })

        # 按创建时间倒序排列
        subtitles.sort(key=lambda x: x['created'], reverse=True)

        return jsonify({
            'success': True,
            'subtitles': subtitles
        })

    except Exception as e:
        logger.error(f"获取字幕列表失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/video/<path:filename>', methods=['GET'])
def serve_video(filename):
    """提供NAS视频文件（用于预览，支持Range请求）；支持子目录相对路径"""
    try:
        from flask import send_file, make_response
        import mimetypes
        nas_path = os.environ.get('NAS_PATH', '/nas/videos')
        filepath = _safe_join_allow_subpath(nas_path, filename)
        if filepath is None:
            logger.warning(f"video 拒绝非法路径: {filename!r}")
            return jsonify({'success': False, 'error': '非法路径'}), 400
        if not os.path.isfile(filepath):
            return jsonify({'success': False, 'error': '视频文件不存在'}), 404
        ext_map = {'.mkv': 'video/x-matroska', '.mp4': 'video/mp4', '.avi': 'video/x-msvideo', '.webm': 'video/webm', '.mov': 'video/quicktime'}
        ext = os.path.splitext(filename)[1].lower()
        mimetype = ext_map.get(ext) or mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        response = make_response(send_file(filepath, mimetype=mimetype, conditional=True))
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['Cache-Control'] = 'public, max-age=3600'
        if ext == '.mkv':
            response.headers['X-Video-Format'] = 'matroska'
            logger.info(f"MKV文件被请求(浏览器可能不支持): {filename}")
        return response
    except Exception as e:
        logger.error(f"提供视频失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 404

@app.route('/api/upload-media/<filename>', methods=['GET'])
def serve_upload_media(filename):
    """提供上传目录中的媒体文件用于编辑器预览（视频/音频，支持Range请求）。

    原 /api/video 只服务 NAS 目录，上传模式生成的字幕在编辑器里永远找不到
    对应媒体，无法对轴预览。
    """
    try:
        from flask import send_file, make_response
        import mimetypes
        filepath = _safe_join(app.config['UPLOAD_FOLDER'], filename)
        if filepath is None:
            logger.warning(f"upload-media 拒绝非法路径: {filename!r}")
            return jsonify({'success': False, 'error': '非法路径'}), 400
        if not os.path.isfile(filepath):
            return jsonify({'success': False, 'error': '文件不存在'}), 404
        ext = os.path.splitext(filename)[1].lower()
        if ext not in MEDIA_EXTENSIONS:
            return jsonify({'success': False, 'error': '不支持的文件类型'}), 400
        mimetype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        response = make_response(send_file(filepath, mimetype=mimetype, conditional=True))
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['Cache-Control'] = 'private, max-age=3600'
        return response
    except Exception as e:
        logger.error(f"提供上传媒体失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 404

@app.route('/api/video-remux/<path:filename>', methods=['GET'])
def serve_video_remux(filename):
    """将MKV/AVI等非浏览器兼容格式remux为MP4流（无重编码）；支持子目录相对路径"""
    try:
        nas_path = os.environ.get('NAS_PATH', '/nas/videos')
        filepath = _safe_join_allow_subpath(nas_path, filename)
        if filepath is None:
            logger.warning(f"video-remux 拒绝非法路径: {filename!r}")
            return jsonify({'success': False, 'error': '非法路径'}), 400
        if not os.path.isfile(filepath):
            return jsonify({'success': False, 'error': '视频文件不存在'}), 404

        ext = os.path.splitext(filename)[1].lower()
        if ext in ['.mp4', '.webm', '.mov']:
            from flask import send_file
            import mimetypes
            ext_map = {'.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime'}
            mimetype = ext_map.get(ext) or mimetypes.guess_type(filename)[0] or 'video/mp4'
            return send_file(filepath, mimetype=mimetype, conditional=True)

        def generate():
            cmd = [
                'ffmpeg', '-i', filepath,
                '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k',
                '-f', 'mp4', '-movflags', 'frag_keyframe+empty_moov',
                '-loglevel', 'error',
                'pipe:1'
            ]
            logger.info(f"Remux视频流: {filename}")
            proc = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE)
            # 修复 BUG：原 finally 中先 stderr.read() 再 wait()，若 ffmpeg 输出大量
            # stderr 会死锁（stdout 读完后才排空 stderr，但 ffmpeg 可能阻塞在写 stderr）。
            # 且客户端中途断开时（GeneratorExit）不会 kill ffmpeg，导致进程泄漏。
            # 改为：后台线程排空 stderr；finally 中无论正常结束还是 GeneratorExit 都 kill。
            import threading as _th
            _stderr_buf = []
            def _drain_stderr():
                try:
                    for line in proc.stderr:
                        _stderr_buf.append(line)
                except Exception:
                    pass
            _stderr_t = _th.Thread(target=_drain_stderr, daemon=True)
            _stderr_t.start()
            try:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            except GeneratorExit:
                # 客户端断开，必须终止 ffmpeg 否则进程泄漏
                logger.info(f"Remux 客户端断开，终止 ffmpeg: {filename}")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except sp.TimeoutExpired:
                    proc.kill()
                raise
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except sp.TimeoutExpired:
                        proc.kill()
                _stderr_t.join(timeout=2)

        return Response(generate(), mimetype='video/mp4')
    except Exception as e:
        logger.error(f"Remux视频失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

# 自动音频提取的去重 + 并发上限（防止 DoS 放大：原实现无上限，攻击者可对每个视频名
# 发起请求瞬间拉起大量 600s ffmpeg 进程耗尽 CPU）
_audio_extracting = {}   # video_name -> True（进行中）
_audio_extracting_lock = threading.Lock()
_AUDIO_EXTRACT_MAX_CONCURRENT = 2

@app.route('/api/audio/<filename>', methods=['GET'])
def serve_audio(filename):
    """提供提取的音频文件（支持Range请求）；不存在时自动后台提取"""
    try:
        from flask import send_file, make_response
        os.makedirs(app.config['AUDIO_FOLDER'], exist_ok=True)
        filepath = _safe_join(app.config['AUDIO_FOLDER'], filename)
        if filepath is None:
            logger.warning(f"audio 拒绝非法路径: {filename!r}")
            return jsonify({'success': False, 'error': '非法路径'}), 400
        if not os.path.exists(filepath):
            video_name = os.path.splitext(filename)[0]
            nas_path = os.environ.get('NAS_PATH', '/nas/videos')
            video_path = None
            # 优先用 ?path= 指定源视频（支持子目录）；缺省再按 basename 在 NAS 根目录找
            src_path = (request.args.get('path') or '').strip()
            if src_path:
                cand = _safe_join_allow_subpath(nas_path, src_path)
                if cand and os.path.isfile(cand):
                    video_path = cand
            if video_path is None:
                for vext in _SAFE_VIDEO_EXTS:
                    candidate = _safe_join(nas_path, video_name + vext)
                    if candidate and os.path.isfile(candidate):
                        video_path = candidate
                        break
            # 兜底：uploads 里同名音频直接提供（上传纯音频生成的字幕此前无法音频对轴）
            if video_path is None:
                for aext in _SAFE_AUDIO_EXTS:
                    cand = _safe_join(app.config['UPLOAD_FOLDER'], video_name + aext)
                    if cand and os.path.isfile(cand):
                        response = make_response(send_file(cand, mimetype='audio/mpeg' if aext == '.mp3' else 'audio/wav', conditional=True))
                        response.headers['Accept-Ranges'] = 'bytes'
                        return response
                # uploads 里同名视频：提取音频供预览
                for vext in _SAFE_VIDEO_EXTS:
                    cand = _safe_join(app.config['UPLOAD_FOLDER'], video_name + vext)
                    if cand and os.path.isfile(cand):
                        video_path = cand
                        break
            if video_path:
                # 修复 DoS：去重（同一视频不重复提取）+ 并发上限
                with _audio_extracting_lock:
                    already = _audio_extracting.get(video_name, False)
                    if already:
                        return jsonify({'success': False, 'error': '音频正在提取中，请稍后刷新', 'auto_extract': True}), 202
                    if len(_audio_extracting) >= _AUDIO_EXTRACT_MAX_CONCURRENT:
                        return jsonify({'success': False, 'error': '提取队列已满，请稍后重试'}), 429
                    _audio_extracting[video_name] = True
                def _auto_extract():
                    try:
                        cmd = ['ffmpeg', '-y', '-i', video_path, '-vn', '-acodec', 'aac', '-b:a', '128k', filepath]
                        sp.run(cmd, stdout=sp.DEVNULL, stderr=sp.DEVNULL, timeout=600)
                        logger.info(f"自动提取音频完成: {filename}")
                    except Exception as ex:
                        logger.error(f"自动提取音频失败: {ex}")
                    finally:
                        with _audio_extracting_lock:
                            _audio_extracting.pop(video_name, None)
                threading.Thread(target=_auto_extract, daemon=True).start()
                return jsonify({'success': False, 'error': '音频正在提取中，请稍后刷新', 'auto_extract': True}), 202
            return jsonify({'success': False, 'error': '音频文件不存在'}), 404
        response = make_response(send_file(filepath, mimetype='audio/aac', conditional=True))
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['Cache-Control'] = 'public, max-age=3600'
        return response
    except Exception as e:
        logger.error(f"提供音频失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 404

@app.route('/api/extract-audio/<path:video_name>', methods=['POST'])
def extract_audio(video_name):
    """从视频中提取音频并转为AAC"""
    try:
        nas_path = os.environ.get('NAS_PATH', '/nas/videos')
        # 路径越界防御
        video_path = _safe_join_allow_subpath(nas_path, video_name)
        if video_path is None:
            logger.warning(f"extract-audio 拒绝非法路径: {video_name!r}")
            return jsonify({'success': False, 'error': '非法路径'}), 400
        # 后缀白名单
        ext = os.path.splitext(video_path)[1].lower()
        if ext not in _SAFE_VIDEO_EXTS:
            return jsonify({'success': False, 'error': f'不支持的视频格式: {ext}'}), 400
        if not os.path.isfile(video_path):
            return jsonify({'success': False, 'error': '视频文件不存在'}), 404

        os.makedirs(app.config['AUDIO_FOLDER'], exist_ok=True)
        # audio_name 用 basename（防止 video_name 含 .. 时把输出也写到 base 外）
        safe_basename = os.path.basename(video_name)
        audio_name = os.path.splitext(safe_basename)[0] + '.aac'
        if not _is_safe_basename(audio_name):
            return jsonify({'success': False, 'error': '非法文件名'}), 400
        audio_path = os.path.join(app.config['AUDIO_FOLDER'], audio_name)
        # 二次校验
        audio_path = _safe_join(app.config['AUDIO_FOLDER'], audio_name)
        if audio_path is None:
            return jsonify({'success': False, 'error': '非法输出路径'}), 400

        if os.path.exists(audio_path):
            return jsonify({'success': True, 'filename': audio_name, 'message': '音频已存在'})
        
        # ffmpeg extract audio to AAC
        cmd = [
            'ffmpeg', '-y', '-i', video_path,
            '-vn', '-acodec', 'aac', '-b:a', '128k',
            '-progress', 'pipe:1', '-nostats',
            audio_path
        ]
        logger.info(f"提取音频: {' '.join(cmd)}")
        process = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE, universal_newlines=True)
        # 修复 BUG：原 communicate(timeout=600) 超时后不 kill，遗留失控 ffmpeg 进程。
        # 现捕获 TimeoutExpired 并显式 kill + 排空管道。
        try:
            stdout, stderr = process.communicate(timeout=600)
        except sp.TimeoutExpired:
            process.kill()
            try:
                stdout, stderr = process.communicate(timeout=10)
            except Exception:
                pass
            logger.error(f"音频提取超时(>600s): {audio_name}")
            return jsonify({'success': False, 'error': '音频提取超时（>10分钟），文件可能过大或损坏'}), 500

        if process.returncode != 0:
            logger.error(f"音频提取失败: {stderr}")
            return jsonify({'success': False, 'error': f'FFmpeg失败: {stderr[-200:]}'}), 500
        
        size = os.path.getsize(audio_path)
        logger.info(f"音频提取完成: {audio_name} ({size} bytes)")
        return jsonify({'success': True, 'filename': audio_name, 'size': size, 'message': '音频提取成功'})
    except Exception as e:
        logger.error(f"提取音频失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

def _resolve_media_file(path_param=None, basename=None):
    """解析媒体文件真实路径：优先显式子路径(NAS) > NAS 根目录 basename > uploads basename。

    返回绝对路径或 None。前端「NAS 文件列表」传完整相对路径（可含子目录），
    旧客户端只传 basename，这里做兼容。
    """
    nas_path = os.environ.get('NAS_PATH', '/nas/videos')
    if path_param:
        cand = _safe_nas_path(path_param)
        if cand and os.path.isfile(cand):
            return cand
        cand = _safe_join(app.config['UPLOAD_FOLDER'], os.path.basename(str(path_param)))
        if cand and os.path.isfile(cand):
            return cand
    if basename:
        for base in (nas_path, app.config['UPLOAD_FOLDER']):
            cand = _safe_join(base, basename)
            if cand and os.path.isfile(cand):
                return cand
    return None

@app.route('/api/subtitle-streams/<path:video_name>', methods=['GET'])
def api_subtitle_streams(video_name):
    """探测视频内嵌字幕轨（前端 NAS 列表「📝 字幕」按钮入口）。

    该路由此前整体缺失（前端调用 404），「提取内嵌字幕」功能自上线起不可用。
    """
    try:
        from utils.subtitle_extractor import probe_subtitle_streams
        media = _resolve_media_file(request.args.get('path'), video_name)
        if not media:
            return jsonify({'success': False, 'error': '视频文件不存在或路径非法'}), 404
        streams = probe_subtitle_streams(media)
        return jsonify({'success': True, 'file': os.path.basename(media), 'streams': streams})
    except Exception as e:
        logger.error(f"探测字幕轨失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/extract-subtitle', methods=['POST'])
def api_extract_subtitle():
    """把视频内嵌文本字幕轨抽取为 SRT（入 TaskQueue，图像轨 OCR 暂不支持）。"""
    try:
        data = request.get_json() or {}
        is_nas_file = bool(data.get('is_nas_file', False))
        media = _resolve_media_file(
            data.get('path') if is_nas_file else None,
            data.get('filename'),
        )
        if not media:
            return jsonify({'success': False, 'error': '视频文件不存在或路径非法'}), 404
        try:
            stream_index = int(data.get('stream_index', 0))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'stream_index 非法'}), 400
        mode = data.get('mode', 'text')
        if mode not in ('text', 'ocr'):
            return jsonify({'success': False, 'error': f'非法 mode: {mode}'}), 400
        ocr_lang = str(data.get('ocr_lang') or 'chi_sim+eng')[:80]
        save_to_source_dir = bool(data.get('save_to_source_dir', False))

        q = _get_task_queue()
        task_id = q.add('extract_subtitle', media, {
            'stream_index': stream_index,
            'mode': mode,
            'ocr_lang': ocr_lang,
            'save_to_source_dir': save_to_source_dir,
            'filename': os.path.basename(media),
        })
        with processing_tasks_lock:
            processing_tasks[task_id] = {
                'status': 'pending',
                'filename': os.path.basename(media),
                'type': 'extract_subtitle',
                'stream_index': stream_index,
                'mode': mode,
                'start_time': datetime.now().isoformat(),
                'progress': 0,
                'progress_info': '任务已入队，等待 worker...',
                '_cancel_event': threading.Event(),
            }
        logger.info(f"字幕抽取任务入队: {task_id}, 轨道 {stream_index}, 文件: {media}")
        return jsonify({'success': True, 'task_id': task_id, 'message': '字幕抽取任务已入队'})
    except Exception as e:
        logger.error(f"启动字幕抽取失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

def _find_media_by_basename(stem):
    """按「去扩展名同名」查找媒体：uploads（平铺）→ NAS 目录（递归，扫描上限 5 万条）。

    烧录和编辑器找视频都用它：原实现只在 NAS 根目录碰运气，子目录库的视频
    永远匹配不上。
    """
    subtitle_probe = os.path.join(app.config['SUBTITLE_FOLDER'], stem + '.srt')
    source = read_metadata(subtitle_probe).get('source_media')
    if source:
        source = os.path.realpath(source)
        roots = [app.config['UPLOAD_FOLDER'], os.environ.get('NAS_PATH', '/nas/videos')]
        if any(source.startswith(os.path.realpath(root) + os.sep) for root in roots) and os.path.isfile(source):
            return source
    original_stem = stem
    stem = re.sub(r'_v[0-9]+$', '', stem)
    stem = re.sub(r'\.[a-z]{2,3}(?:-[a-z]{2,3})?$', '', stem)
    upload_dir = app.config['UPLOAD_FOLDER']
    try:
        for name in os.listdir(upload_dir):
            base, ext = os.path.splitext(name)
            if base == original_stem and ext.lower() in _SAFE_VIDEO_EXTS:
                cand = os.path.join(upload_dir, name)
                if os.path.isfile(cand):
                    return cand
    except OSError:
        pass
    nas_path = os.environ.get('NAS_PATH', '/nas/videos')
    scanned = 0
    for root, dirs, names in os.walk(nas_path):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for n in names:
            scanned += 1
            if scanned > 50000:
                logger.warning(f"resolve-media: 目录超过扫描上限，放弃: {nas_path}")
                return None
            base, ext = os.path.splitext(n)
            if base == original_stem and ext.lower() in _SAFE_VIDEO_EXTS:
                cand = os.path.join(root, n)
                if os.path.isfile(cand):
                    return cand
    if stem != original_stem:
        return _find_media_by_basename(stem)
    return None

@app.route('/api/resolve-media', methods=['GET'])
def api_resolve_media():
    """按 basename 解析媒体真实位置（含子目录），返回相对路径供前端拼 /api/video/。"""
    name = (request.args.get('name') or '').strip()
    if not _is_safe_basename(name):
        return jsonify({'success': False, 'error': '非法文件名'}), 400
    cand = _find_media_by_basename(os.path.splitext(name)[0])
    if not cand:
        return jsonify({'success': False, 'error': '未找到匹配的媒体文件'}), 404
    upload_real = os.path.realpath(app.config['UPLOAD_FOLDER'])
    if os.path.realpath(cand).startswith(upload_real + os.sep):
        return jsonify({'success': True, 'path': os.path.basename(cand), 'location': 'uploads'})
    rel = _safe_relpath(os.environ.get('NAS_PATH', '/nas/videos'), cand)
    if not rel:
        return jsonify({'success': False, 'error': '媒体文件不在允许目录内'}), 404
    return jsonify({'success': True, 'path': rel, 'location': 'nas'})

@app.route('/api/download/<filename>', methods=['GET'])
def download_subtitle(filename):
    """下载字幕文件"""
    try:
        if not _is_safe_basename(filename):
            logger.warning(f"download 拒绝非法文件名: {filename!r}")
            return jsonify({'success': False, 'error': '非法文件名'}), 400
        # send_from_directory 已做路径安全校验，但仍加一道保险
        safe_path = _safe_join(app.config['SUBTITLE_FOLDER'], filename)
        if safe_path is None:
            return jsonify({'success': False, 'error': '非法路径'}), 400
        return send_from_directory(
            app.config['SUBTITLE_FOLDER'],
            filename,
            as_attachment=True
        )
    except Exception as e:
        logger.error(f"下载字幕失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 404

@app.route('/api/preview/<filename>', methods=['GET'])
def preview_subtitle(filename):
    """预览字幕文件"""
    try:
        subtitle_path = _safe_join(app.config['SUBTITLE_FOLDER'], filename)
        if subtitle_path is None:
            logger.warning(f"preview 拒绝非法路径: {filename!r}")
            return jsonify({'success': False, 'error': '非法路径'}), 400
        
        if not os.path.exists(subtitle_path):
            return jsonify({'success': False, 'error': '字幕文件不存在'}), 404
        
        # 根据文件扩展名确定格式
        file_ext = filename.lower().split('.')[-1]
        
        # 统一用 _parse_subtitle_file 解析。原 srt 分支自造轮子且要求
        # 「序号+时间+文本」三行俱全，缺序号行或紧凑格式的 srt 解析结果为空；
        # 与编辑器/转换用的解析器行为不一致。
        segments = _parse_subtitle_file(subtitle_path)
        return jsonify({
            'success': True,
            'format': file_ext,
            'segments': segments,
        })
        
    except Exception as e:
        logger.error(f"预览字幕失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

def _words_path(path):
    return os.path.splitext(path)[0] + '.words.json'


def _invalidate_words(path, reason):
    words = _words_path(path)
    if os.path.isfile(words):
        atomic_json(words, {'version': 2, 'valid': False, 'reason': reason, 'segments': []})


def _save_edited_bundle(segments, path, fmt):
    with BUNDLE_LOCK:
        stem = os.path.splitext(path)[0]
        for extension in ('srt', 'vtt', 'ass', 'json', 'txt'):
            member = stem + '.' + extension
            if member == path or os.path.isfile(member):
                _save_segments_as(segments, member, extension)
        _invalidate_words(path, '字幕已编辑，旧逐词对齐已失效；重新生成后可恢复')


def _save_new_segments(segments, path, fmt, source_subtitle=None):
    stem, reservation = reserve_stem(os.path.dirname(path), os.path.splitext(os.path.basename(path))[0])
    path = os.path.join(os.path.dirname(path), stem + '.' + fmt)
    try:
        _save_segments_as(segments, path, fmt)
        if source_subtitle:
            copy_metadata(source_subtitle, path)
            if not read_metadata(path):
                media = _find_media_by_basename(os.path.splitext(os.path.basename(source_subtitle))[0])
                if media:
                    write_metadata(path, media)
        return path
    finally:
        release_reservation(reservation)


@app.route('/api/save-subtitle', methods=['POST'])
def save_subtitle():
    """保存编辑后的字幕"""
    try:
        data = request.get_json()

        if not data or 'filename' not in data or 'segments' not in data:
            return jsonify({'success': False, 'error': '缺少必要参数'}), 400

        filename = data['filename']
        segments = data['segments']
        format_type = data.get('format', 'srt')

        # 防止任意文件写入：白名单后缀 + 路径越界检查
        ext = os.path.splitext(filename)[1].lower()
        if ext not in _SAFE_SUBTITLE_EXTS:
            return jsonify({'success': False, 'error': f'不支持的字幕格式: {ext}'}), 400
        subtitle_path = _safe_join(app.config['SUBTITLE_FOLDER'], filename)
        if subtitle_path is None:
            logger.warning(f"save-subtitle 拒绝非法路径: {filename!r}")
            return jsonify({'success': False, 'error': '非法文件名'}), 400

        _save_edited_bundle(segments, subtitle_path, format_type)

        logger.info(f"字幕编辑已保存: {filename}")

        return jsonify({
            'success': True,
            'message': '字幕保存成功；旧逐词对齐已失效',
            'words_invalidated': True
        })

    except Exception as e:
        logger.error(f"保存字幕失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/words/<filename>', methods=['GET'])
def subtitle_words(filename):
    """读取词级时间戳旁挂文件（<字幕名>.words.json）。

    词级数据由转录时 ASR_WORD_TIMESTAMPS 落盘，供前端逐词高亮与点词校对。
    没有该文件说明转录时没开词级时间戳，返回 404。
    """
    try:
        base = re.sub(r'\.(srt|vtt|ass|json|txt)$', '', filename, flags=re.IGNORECASE)
        words_path = _safe_join(app.config['SUBTITLE_FOLDER'], base + '.words.json')
        if words_path is None or not os.path.isfile(words_path):
            return jsonify({'success': False, 'error': '无词级时间戳数据'}), 404
        with open(words_path, encoding='utf-8') as f:
            data = json.load(f)
        if data.get('valid') is False:
            return jsonify(success=False, invalidated=True, error=data.get('reason')), 404
        return jsonify({'success': True, 'filename': filename, 'words': data})
    except Exception as e:
        logger.error(f"读取词级时间戳失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/subtitle-quality/<filename>', methods=['GET','POST'])
def subtitle_quality(filename):
    """检查字幕质量：时间重叠、空字幕、长行、阅读速度等。"""
    try:
        subtitle_path = _safe_subtitle_path(filename)
        if subtitle_path is None or not os.path.isfile(subtitle_path):
            return jsonify({'success': False, 'error': '字幕文件不存在或路径非法'}), 404
        # 规范预设：default / netflix / youtube / bbc；再用 query 参数逐项覆盖
        profile = str(request.args.get('profile', 'default')).strip().lower()
        if profile not in SUBTITLE_PROFILES:
            return jsonify({
                'success': False,
                'error': f'未知规范: {profile}',
                'available_profiles': sorted(SUBTITLE_PROFILES.keys()),
            }), 400
        th = dict(SUBTITLE_PROFILES[profile])

        def _num(name, lo, hi, cast):
            raw = request.args.get(name)
            if raw is None or raw == '':
                return th[name]
            try:
                th[name] = min(max(cast(raw), lo), hi)
            except (TypeError, ValueError):
                pass
            return th[name]

        _num('max_chars', 10, 200, int)
        _num('max_lines', 1, 10, int)
        _num('max_cps', 1, 100, float)
        _num('min_duration', 0, 10, float)
        _num('min_gap', 0, 5, float)

        segments = (request.get_json().get('segments',[]) if request.method=='POST' else _parse_subtitle_file(subtitle_path))
        if not segments:return jsonify(success=False,error='缺少字幕内容'),400
        report = _quality_report_for_segments(segments, profile=profile, bilingual=bool(read_metadata(subtitle_path).get('bilingual')), **th)
        report['score_scope']='仅评排版和时间，不评识别及翻译准确率'
        report['asr_review_notes']=read_metadata(subtitle_path).get('asr_warnings',[])
        report['automatic_corrections']=read_metadata(subtitle_path).get('automatic_corrections',0)
        return jsonify({'success': True, 'filename': filename, 'report': report,
                        'available_profiles': sorted(SUBTITLE_PROFILES.keys())})
    except Exception as e:
        logger.error(f"字幕质量检查失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/subtitle-offset', methods=['POST'])
def subtitle_offset():
    """对单个字幕应用时间偏移。"""
    try:
        data = request.get_json() or {}
        filename = data.get('filename')
        offset = float(data.get('offset', 0) or 0)
        subtitle_path = _safe_subtitle_path(filename)
        if subtitle_path is None or not os.path.isfile(subtitle_path):
            return jsonify({'success': False, 'error': '字幕文件不存在或路径非法'}), 404
        output_filename = data.get('output_filename') or filename
        output_path = _safe_subtitle_path(output_filename)
        if output_path is None:
            return jsonify({'success': False, 'error': '输出文件名非法'}), 400
        segments = _parse_subtitle_file(subtitle_path)
        shifted = []
        for i, seg in enumerate(segments):
            shifted.append(_normalize_segment({
                'start': max(0.0, seg['start'] + offset),
                'end': max(0.01, seg['end'] + offset),
                'text': seg.get('text', ''),
            }, i))
        fmt = os.path.splitext(output_filename)[1].lower().lstrip('.')
        if output_path == subtitle_path:
            _save_segments_as(shifted, output_path, fmt)
        else:
            output_path = _save_new_segments(shifted, output_path, fmt, subtitle_path)
            output_filename = os.path.basename(output_path)
        words_path = _words_path(subtitle_path)
        if os.path.isfile(words_path):
            with open(words_path, encoding='utf-8') as inp:
                words = json.load(inp)
            if words.get('valid', True):
                for cue in words.get('segments', []):
                    cue['start'] = max(0.0, cue['start'] + offset)
                    cue['end'] = max(cue['start'] + 0.01, cue['end'] + offset)
                    for word in cue.get('words', []):
                        word['start'] = max(0.0, word['start'] + offset)
                        word['end'] = max(word['start'] + 0.001, word['end'] + offset)
                atomic_json(_words_path(output_path), words)
        if output_path == subtitle_path:
            stem = os.path.splitext(output_path)[0]
            for extension in ('srt', 'vtt', 'ass', 'json', 'txt'):
                member = stem + '.' + extension
                if member != output_path and os.path.isfile(member):
                    _save_segments_as(shifted, member, extension)
        return jsonify({'success': True, 'filename': output_filename, 'message': '时间偏移已应用'})
    except Exception as e:
        logger.error(f"字幕偏移失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/subtitle-convert', methods=['POST'])
def subtitle_convert():
    """转换字幕格式。"""
    try:
        data = request.get_json() or {}
        filename = data.get('filename')
        target_format = (data.get('format') or '').lower()
        if target_format not in ('srt', 'vtt', 'json', 'txt', 'ass'):
            return jsonify({'success': False, 'error': '不支持的目标格式'}), 400
        subtitle_path = _safe_subtitle_path(filename)
        if subtitle_path is None or not os.path.isfile(subtitle_path):
            return jsonify({'success': False, 'error': '字幕文件不存在或路径非法'}), 404
        stem = os.path.splitext(filename)[0]
        output_filename = data.get('output_filename') or f"{stem}.{target_format}"
        output_path = _safe_subtitle_path(output_filename)
        if output_path is None or not output_filename.lower().endswith('.' + target_format):
            return jsonify({'success': False, 'error': '输出文件名非法'}), 400
        segments = _parse_subtitle_file(subtitle_path)
        output_path = _save_new_segments(segments, output_path, target_format, subtitle_path)
        return jsonify({'success': True, 'filename': os.path.basename(output_path), 'message': '格式转换完成'})
    except Exception as e:
        logger.error(f"字幕转换失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

def _translator_model_local(source_lang: str, target_lang: str) -> bool:
    """检查目标语言对的 opus-mt 模型是否已本地可用（紧凑目录或 HF cache）。

    /api/subtitle-translate 是同步阻塞接口，模型未下载时会先在线拉模型，
    请求可能挂数分钟直到超时——预检后直接给出可操作的报错。
    """
    from utils.subtitle_generator import SubtitleGenerator
    lang_pair = f"{source_lang}-{target_lang}"
    model_name = SubtitleGenerator.TRANSLATION_MODELS.get(
        lang_pair, f"Helsinki-NLP/opus-mt-{lang_pair}"
    )

    # 位置 1：紧凑目录（/api/translator-models 下载到此，en-zh 等命名）
    cache_dir = os.environ.get('TRANSLATOR_MODELS_DIR', '')
    if cache_dir:
        candidate = os.path.join(cache_dir, lang_pair)
        if os.path.exists(os.path.join(candidate, 'config.json')):
            return True

    # 位置 2：HF hub cache（entrypoint/preload 预下载到这里）
    slug = model_name.rsplit('/', 1)[-1]
    hf_home = os.environ.get('HF_HUB_CACHE') or os.path.expanduser('~/.cache/huggingface/hub')
    model_dir = os.path.join(hf_home, f"models--{model_name.replace('/', '--')}")
    snapshots = os.path.join(model_dir, 'snapshots')
    if os.path.isdir(snapshots):
        for entry in os.listdir(snapshots):
            if os.path.exists(os.path.join(snapshots, entry, 'config.json')):
                return True
        # 目录在但没有任何完整 snapshot → 下载未完成，视为不可用
    del slug
    return False

@app.route('/api/subtitle-translate', methods=['POST'])
def subtitle_translate():
    """基于已有字幕补翻译，不重新跑 Whisper。"""
    try:
        data = request.get_json() or {}
        filename = data.get('filename')
        target_lang = (data.get('target_lang') or '').strip()
        source_lang = (data.get('source_lang') or '').strip()
        bilingual = bool(data.get('bilingual', False))
        if not re.match(r'^[a-z]{2,3}$', target_lang):
            return jsonify({'success': False, 'error': '目标语言非法'}), 400
        if not source_lang:
            source_lang = 'zh' if target_lang != 'zh' else 'en'
        if not re.match(r'^[a-z]{2,3}$', source_lang):
            return jsonify({'success': False, 'error': '源语言非法'}), 400
        # 预检：翻译模型未下载时立即报错（原实现会先在线拉模型，请求挂数分钟直到超时）
        engine = data.get('translate_engine', 'auto')
        use_llm = (engine == 'llm') or (engine == 'auto' and llm_translate_ready())
        if use_llm and not llm_translate_ready():
            return jsonify(success=False, error='请先配置并启用 AI 翻译'), 400
        if not use_llm and not _translator_model_local(source_lang, target_lang):
            pair = f"{source_lang}-{target_lang}"
            return jsonify({
                'success': False,
                'error': f'翻译模型 {pair} 尚未下载。请在工具面板的「模型信息」中确认，'
                         f'或运行 POST /api/download-translator-model 下载后重试（当前已就绪: en-zh / zh-en）'
            }), 400
        subtitle_path = _safe_subtitle_path(filename)
        if subtitle_path is None or not os.path.isfile(subtitle_path):
            return jsonify({'success': False, 'error': '字幕文件不存在或路径非法'}), 404
        segments = _parse_subtitle_file(subtitle_path)
        # Reuse real word anchors when the source bundle still matches them.
        try:
            word_path = os.path.splitext(subtitle_path)[0]+'.words.json'
            with open(word_path,encoding='utf-8') as inp: word_cues=json.load(inp).get('segments',[])
            if len(word_cues)==len(segments):
                for seg,item in zip(segments,word_cues):
                    words=item.get('words') or []
                    joined=''.join(str(w.get('word',w.get('text',''))) for w in words)
                    if words and re.sub(r'\s+','',joined)==re.sub(r'\s+','',seg['text']) and all(float(w['start'])>=seg['start']-.02 and float(w['end'])<=seg['end']+.02 for w in words):seg['words']=words
        except (OSError,ValueError,TypeError,KeyError):pass
        segments = semantic_segments(segments,target_lang=target_lang)
        glossary = data.get('glossary') or {}
        glossary_name = data.get('glossary_name')
        if glossary_name:
            glossary = _merged_glossary(glossary, glossary_name)
        temp_generator = SubtitleGenerator(model_size=os.getenv('MODEL_SIZE', 'tiny'), device='cpu' if use_llm else os.getenv('USE_GPU', 'auto'))
        source_media=read_metadata(subtitle_path).get('source_media')
        if source_media and os.path.isfile(source_media):
            temp_generator._media_duration=temp_generator._probe_duration(source_media)
        translation_audit=[]
        if use_llm:
            from utils.subtitle_generator import llm_translate_texts
            texts = llm_translate_texts([seg['text'] for seg in segments], source_lang, target_lang,
                subtitle_limits=_subtitle_character_limits(segments,getattr(temp_generator,'_media_duration',None)),glossary=glossary,audit=translation_audit)
            translated = [dict(seg, text=text) for seg, text in zip(segments, texts)]
        else:
            translated = temp_generator._translate_segments(
                [dict(seg) for seg in segments], source_lang, target_lang)
        temp_generator._apply_glossary(translated, glossary)
        final_segments = temp_generator._combine_bilingual_segments(segments, translated) if bilingual else translated
        stem, ext = os.path.splitext(filename)
        fmt = data.get('format') or ext.lstrip('.').lower() or 'srt'
        suffix = f".{source_lang}-{target_lang}" if bilingual else f".{target_lang}"
        output_filename = data.get('output_filename') or f"{stem}{suffix}.{fmt}"
        output_path = _safe_subtitle_path(output_filename)
        if output_path is None:
            return jsonify({'success': False, 'error': '输出文件名非法'}), 400
        final_segments = layout_segments(final_segments)
        output_path = _save_new_segments(final_segments, output_path, fmt, subtitle_path)
        meta = read_metadata(output_path)
        meta['bilingual'] = bilingual
        if translation_audit:
            audit_path=os.path.splitext(output_path)[0]+'.translation.json'
            atomic_json(audit_path,dict(engine='HY-MT2',segments=translation_audit,manual_edits=False))
            meta['translation_audit']=os.path.basename(audit_path)
        atomic_json(metadata_path(output_path),meta)
        warnings = [] if use_llm else ['本地机器翻译需要人工校对']
        report = _quality_report_for_segments(final_segments)
        if report['summary']['too_fast']:
            warnings.append('双语阅读速度较快，请在编辑器中校对时间和文字')
        return jsonify({'success': True, 'filename': os.path.basename(output_path),
                        'warnings': warnings, 'quality': report, 'message': '字幕补翻译完成'})
    except Exception as e:
        logger.error(f"字幕补翻译失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/subtitle-rename', methods=['POST'])
def subtitle_rename():
    """按规则重命名字幕。"""
    try:
        data = request.get_json() or {}
        filename = data.get('filename')
        subtitle_path = _safe_subtitle_path(filename)
        if subtitle_path is None or not os.path.isfile(subtitle_path):
            return jsonify({'success': False, 'error': '字幕文件不存在或路径非法'}), 404
        stem, ext = os.path.splitext(filename)
        pattern = data.get('pattern') or '{name}.{lang}{ext}'
        values = {
            'name': data.get('name') or stem,
            'lang': data.get('lang') or data.get('language') or 'zh',
            'target': data.get('target') or data.get('target_language') or '',
            'ext': ext,
        }
        output_filename = pattern.format(**values)
        if not output_filename.endswith(ext):
            output_filename += ext
        output_path = _safe_subtitle_path(output_filename)
        if output_path is None:
            return jsonify({'success': False, 'error': '重命名结果非法'}), 400
        if os.path.exists(output_path) and output_filename != filename:
            return jsonify({'success': False, 'error': '目标文件已存在'}), 409
        if output_path != subtitle_path:
            if not read_metadata(subtitle_path):
                media = _find_media_by_basename(stem)
                if media:
                    write_metadata(subtitle_path, media)
            old_stem, new_stem = os.path.splitext(subtitle_path)[0], os.path.splitext(output_path)[0]
            moves = [(old_stem + '.' + e, new_stem + '.' + e) for e in FORMATS
                     if os.path.isfile(old_stem + '.' + e)]
            if os.path.isfile(metadata_path(subtitle_path)):
                moves.append((metadata_path(subtitle_path), metadata_path(output_path)))
            with BUNDLE_LOCK:
                if any(os.path.lexists(b) for a, b in moves):
                    return jsonify(success=False, error='目标成果或配套文件已存在'), 409
                completed = []
                try:
                    for a, b in moves:
                        os.rename(a, b)
                        completed.append((a, b))
                except OSError:
                    for a, b in reversed(completed):
                        os.rename(b, a)
                    raise
        return jsonify({'success': True, 'filename': output_filename, 'message': '字幕已重命名'})
    except KeyError as e:
        return jsonify({'success': False, 'error': f'未知命名变量: {e}'}), 400
    except Exception as e:
        logger.error(f"字幕重命名失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

def _parse_srt_time(time_str):
    """解析SRT时间格式 (HH:MM:SS,mmm)"""
    try:
        time_part, ms_part = time_str.split(',')
        h, m, s = time_part.split(':')
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms_part) / 1000
    except:
        return 0

def _parse_vtt_time(time_str):
    """解析VTT时间格式 (HH:MM:SS.mmm)"""
    try:
        h, m, s = time_str.split(':')
        s_ms = s.split('.')
        seconds = int(s_ms[0])
        if len(s_ms) > 1:
            ms_str = s_ms[1].ljust(3, '0')[:3]
            seconds += int(ms_str) / 1000
        return int(h) * 3600 + int(m) * 60 + seconds
    except:
        return 0

def _save_srt_segments(segments, output_path):
    """保存SRT格式的字幕"""
    with open(output_path, "w", encoding="utf-8") as f:
        for i, segment in enumerate(segments, 1):
            start_time = _format_timestamp(segment["start"])
            end_time = _format_timestamp(segment["end"])
            text = segment["text"].strip()
            
            f.write(f"{i}\n")
            f.write(f"{start_time} --> {end_time}\n")
            f.write(f"{text}\n\n")

def _save_vtt_segments(segments, output_path):
    """保存VTT格式的字幕"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for i, segment in enumerate(segments):
            start_time = _format_timestamp_vtt(segment["start"])
            end_time = _format_timestamp_vtt(segment["end"])
            text = segment["text"].strip()
            
            f.write(f"{i+1}\n")
            f.write(f"{start_time} --> {end_time}\n")
            f.write(f"{text}\n\n")

def _save_json_segments(segments, output_path):
    """保存JSON格式的字幕"""
    data = {
        "segments": segments
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _format_timestamp(seconds):
    """格式化时间戳为SRT格式 (HH:MM:SS,mmm)"""
    total = max(0,int(round(seconds*1000)))
    hours, remainder = divmod(total,3600000)
    minutes, remainder = divmod(remainder,60000)
    secs, millisecs = divmod(remainder,1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millisecs:03d}"

def _format_timestamp_vtt(seconds):
    """格式化时间戳为VTT格式 (HH:MM:SS.mmm)"""
    total = max(0,int(round(seconds*1000)))
    hours, remainder = divmod(total,3600000)
    minutes, remainder = divmod(remainder,60000)
    secs, millisecs = divmod(remainder,1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millisecs:03d}"

@app.route('/api/delete/<filename>', methods=['DELETE'])
def delete_file(filename):
    """删除文件（视频或字幕）"""
    try:
        # 路径越界防御：必须在 SUBTITLE_FOLDER 或 UPLOAD_FOLDER 下
        subtitle_path = _safe_join(app.config['SUBTITLE_FOLDER'], filename)
        if subtitle_path and os.path.isfile(subtitle_path):
            os.remove(subtitle_path)
            logger.info(f"删除字幕文件: {filename}")
            return jsonify({'success': True, 'message': '文件删除成功'})

        video_path = _safe_join(app.config['UPLOAD_FOLDER'], filename)
        if video_path and os.path.isfile(video_path):
            os.remove(video_path)
            logger.info(f"删除视频文件: {filename}")
            return jsonify({'success': True, 'message': '文件删除成功'})

        return jsonify({'success': False, 'error': '文件不存在'}), 404

    except Exception as e:
        logger.error(f"删除文件失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/translator-models', methods=['GET'])
def list_translator_models():
    """列出已下载的翻译模型"""
    try:
        translator_models_dir = os.environ.get('TRANSLATOR_MODELS_DIR', os.path.join(os.getcwd(), 'translator_models'))
        
        models = []
        if os.path.exists(translator_models_dir):
            for lang_pair in os.listdir(translator_models_dir):
                model_path = os.path.join(translator_models_dir, lang_pair)
                if os.path.isdir(model_path):
                    # 检查模型文件是否存在
                    config_file = os.path.join(model_path, 'config.json')
                    pytorch_model_file = os.path.join(model_path, 'pytorch_model.bin')
                    
                    if os.path.exists(config_file) and (os.path.exists(pytorch_model_file) or os.path.exists(os.path.join(model_path, 'model.safetensors'))):
                        models.append({
                            'lang_pair': lang_pair,
                            'path': model_path,
                            'exists': True
                        })
        
        return jsonify({
            'success': True,
            'models': models,
            'translator_models_dir': translator_models_dir
        })
        
    except Exception as e:
        logger.error(f"获取翻译模型列表失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/download-translator-model', methods=['POST'])
def download_translator_model():
    """下载翻译模型"""
    try:
        data = request.get_json()
        
        if not data or 'lang_pair' not in data:
            return jsonify({'success': False, 'error': '缺少语言对参数'}), 400
        
        lang_pair = data['lang_pair']
        
        # 验证语言对是否支持
        from utils.subtitle_generator import SubtitleGenerator
        if lang_pair not in SubtitleGenerator.TRANSLATION_MODELS:
            return jsonify({'success': False, 'error': f'不支持的翻译语言对: {lang_pair}'}), 400
        
        model_name = SubtitleGenerator.TRANSLATION_MODELS[lang_pair]
        translator_models_dir = os.environ.get('TRANSLATOR_MODELS_DIR', os.path.join(os.getcwd(), 'translator_models'))
        os.makedirs(translator_models_dir, exist_ok=True)
        
        # 使用线程下载模型，避免阻塞
        def download_model():
            try:
                logger.info(f"开始下载翻译模型: {model_name} -> {lang_pair}")
                
                # 使用huggingface_hub下载模型
                from huggingface_hub import snapshot_download
                snapshot_download(
                    repo_id=model_name,
                    local_dir=os.path.join(translator_models_dir, lang_pair),
                    local_dir_use_symlinks=False
                )
                
                logger.info(f"翻译模型下载完成: {lang_pair}")
            except Exception as e:
                logger.error(f"翻译模型下载失败: {str(e)}")
        
        thread = threading.Thread(target=download_model)
        thread.daemon = True
        thread.start()
        
        return jsonify({
            'success': True,
            'message': f'翻译模型 {lang_pair} 下载已启动'
        })
        
    except Exception as e:
        logger.error(f"启动翻译模型下载失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'subtitles', '.history.json')
_history_lock = threading.Lock()

def _load_history():
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return []

def _save_history(history):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    tmp_path = HISTORY_FILE + '.tmp'
    with _history_lock:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, HISTORY_FILE)

@app.route('/api/history', methods=['GET'])
def get_history():
    try:
        history = _load_history()
        return jsonify({'success': True, 'history': history})
    except Exception as e:
        logger.error(f"获取历史记录失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/history', methods=['POST'])
def add_history():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': '缺少数据'}), 400
        history = _load_history()
        history.append(data)
        if len(history) > 100:
            history = history[-100:]
        _save_history(history)
        return jsonify({'success': True, 'message': '历史记录已保存'})
    except Exception as e:
        logger.error(f"保存历史记录失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/history', methods=['DELETE'])
def clear_history():
    try:
        _save_history([])
        return jsonify({'success': True, 'message': '历史记录已清空'})
    except Exception as e:
        logger.error(f"清空历史记录失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

def _ass_color(value, fallback):
    """把 #RRGGBB 转成 ASS 的 &HAABBGGRR（注意 ASS 是 BGR 序）。非法输入回退默认。"""
    m = re.match(r'^#([0-9a-fA-F]{6})$', str(value or ''))
    if not m:
        return fallback
    r, g, b = m.group(1)[0:2], m.group(1)[2:4], m.group(1)[4:6]
    return f'&H00{b}{g}{r}'.upper()

def _burn_font_config():
    """烧录字体：默认 Noto CJK（随 data 卷携带），否则回退 libass 默认。

    容器里只有 DejaVu，中文硬字幕曾整行变"豆腐块"；现把宿主机的
    NotoSansCJK-Regular.ttc 放在 data/fonts/（挂载卷，重建不丢），
    subtitles 滤镜加 fontsdir 后 libass 能正确渲染中日韩文字。
    """
    fonts_dir = os.environ.get('BURN_FONTS_DIR', '/app/data/fonts')
    font_name = os.environ.get('BURN_FONT_NAME', 'Noto Sans CJK SC')
    has_fonts = False
    try:
        has_fonts = any(f.lower().endswith(('.ttf', '.otf', '.ttc')) for f in os.listdir(fonts_dir))
    except OSError:
        pass
    if not has_fonts:
        bundled = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')
        if os.path.isfile(os.path.join(bundled, 'NotoSansCJK-Regular.ttc')):
            fonts_dir = bundled; has_fonts = True
    return (fonts_dir if has_fonts else None), font_name

def _build_force_style(style):
    """从请求 style 字典构建 ffmpeg subtitles 滤镜的 force_style 串。

    对标主流字幕工具（剪映/Arctime/Subtitle Edit）的硬字幕基本项：
    字体/字号/字色/描边色/描边宽/加粗/位置(底/中/顶)/垂直边距。
    """
    def _clamp_int(value, lo, hi, default):
        try:
            return max(lo, min(hi, int(value)))
        except (TypeError, ValueError):
            return default

    _, default_font = _burn_font_config()
    font_name = str(style.get('font_name') or default_font or 'Arial')
    font_name = re.sub(r'[^A-Za-z0-9 _\-]', '', font_name)[:60] or 'Arial'
    font_size = _clamp_int(style.get('font_size'), 8, 96, 24)
    outline_w = _clamp_int(style.get('outline_width'), 0, 10, 2)
    margin_v = _clamp_int(style.get('margin_v'), 0, 400, 28)
    # force_style uses libass's legacy alignment codes, not ASS file codes.
    alignment = {'top': 6, 'middle': 10, 'bottom': 2}.get(str(style.get('position') or 'bottom'), 2)
    bold = -1 if style.get('bold') else 0
    parts = [
        f'FontName={font_name}',
        f'FontSize={font_size}',
        f'PrimaryColour={_ass_color(style.get("font_color"), "&H00FFFFFF")}',
        f'OutlineColour={_ass_color(style.get("outline_color"), "&H00000000")}',
        f'Outline={outline_w}',
        f'BorderStyle=1',
        f'Bold={bold}',
        f'Alignment={alignment}',
        f'MarginV={margin_v}',
    ]
    return ','.join(parts)

@app.route('/api/burn-subtitle', methods=['POST'])
def burn_subtitle():
    """将字幕烧录到视频中"""
    try:
        data = request.get_json()
        if not data or 'subtitle_filename' not in data:
            return jsonify({'success': False, 'error': '缺少字幕文件名'}), 400

        subtitle_filename = data['subtitle_filename']
        subtitle_path = _safe_join(app.config['SUBTITLE_FOLDER'], subtitle_filename)
        if subtitle_path is None:
            logger.warning(f"burn-subtitle 拒绝非法路径: {subtitle_filename!r}")
            return jsonify({'success': False, 'error': '非法路径'}), 400
        
        if not os.path.exists(subtitle_path):
            return jsonify({'success': False, 'error': '字幕文件不存在'}), 404
        
        # 查找匹配的视频文件：uploads（平铺）→ NAS 目录递归（支持子目录库）
        base_name = re.sub(r'\.(srt|vtt|ass|json|txt)$', '', subtitle_filename, flags=re.IGNORECASE)
        video_path = _find_media_by_basename(base_name)

        if not video_path:
            return jsonify({'success': False, 'error': '未找到匹配的视频文件。请确保视频已上传且与字幕文件同名'}), 404
        
        # 输出文件名
        output_filename = f"{base_name}_subtitled.mp4"
        import uuid
        output_filename = f'{base_name}_subtitled_{uuid.uuid4().hex[:8]}.mp4'
        output_path = os.path.join(app.config['SUBTITLE_FOLDER'], output_filename)
        
        # 为FFmpeg准备转义后的字幕路径
        subtitle_abs = os.path.abspath(subtitle_path)
        
        task_id = f"burn_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"

        with processing_tasks_lock:
            processing_tasks[task_id] = {
                'status': 'processing',
                'filename': subtitle_filename,
                'start_time': datetime.now().isoformat(),
                'progress': 0,
                'progress_info': '烧录任务已启动...'
            }
        
        def burn_process():
            burn_ass=None
            try:
                processing_tasks[task_id]['progress'] = 10
                processing_tasks[task_id]['progress_info'] = '正在烧录字幕...'

                # 烧录样式（前端弹层可配，缺省=原内置样式）
                # 对字幕路径做滤镜转义：subtitles 是 filter，: , ; [ ] ' 都是滤镜图语法字符
                import tempfile
                fd,burn_ass=tempfile.mkstemp(prefix='burn-',suffix='.ass',dir=app.config['AUDIO_FOLDER'])
                os.close(fd)
                _save_ass_segments(_parse_subtitle_file(subtitle_abs),burn_ass)
                escaped_subtitle = burn_ass.replace('\\', '/').replace(':', '\\:') \
                    .replace("'", "\\'").replace('[', '\\[').replace(']', '\\]') \
                    .replace(',', '\\,').replace(';', '\\;')
                force_style = _build_force_style(data.get('style') or {})
                _fonts_dir, _ = _burn_font_config()
                # filename 必须是 subtitles 滤镜第一个参数，fontsdir 只能跟在后面
                _fontsdir_part = f':fontsdir={_fonts_dir}' if _fonts_dir else ''
                sub_filter = f"subtitles={escaped_subtitle}{_fontsdir_part}:force_style='{force_style}'"
                # 核显 VAAPI 硬编（BURN_USE_VAAPI=off 可强制关闭）；失败自动回退 CPU libx264
                use_vaapi = (os.environ.get('BURN_USE_VAAPI', 'auto') != 'off') and _probe_vaapi()
                def _build_cmd(hw):
                    if hw:
                        return [
                            'ffmpeg', '-y',
                            '-vaapi_device', '/dev/dri/renderD128',
                            '-i', video_path,
                            '-vf', sub_filter + ',format=nv12,hwupload',
                            '-c:v', 'h264_vaapi', '-global_quality', '23',
                            '-c:a', 'copy',
                            '-movflags', '+faststart',
                            output_path
                        ]
                    return [
                        'ffmpeg', '-y',
                        '-i', video_path,
                        '-vf', sub_filter,
                        '-c:v', 'libx264', '-crf', '23',
                        '-c:a', 'copy',
                        '-movflags', '+faststart',
                        output_path
                    ]
                cmd = _build_cmd(use_vaapi)
                encoder_name = 'h264_vaapi(核显)' if use_vaapi else 'libx264(CPU)'

                import queue as _q
                import threading as _th

                def _run_ffmpeg_burn(run_cmd):
                    """跑一次 ffmpeg 烧录（带协作取消 + wall-clock 硬超时）。

                    返回 (returncode, timed_out, cancelled)。原实现 stderr 阻塞迭代会让
                    卡死的 ffmpeg 永久挂起，现改为后台线程读 stderr + 主循环带超时轮询。
                    """
                    process = sp.Popen(run_cmd, stdout=sp.DEVNULL, stderr=sp.PIPE, universal_newlines=True)
                    with _burn_processes_lock:
                        _burn_processes[task_id] = process
                    _line_q = _q.Queue()
                    _eof_flag = object()
                    def _stderr_reader():
                        try:
                            for line in process.stderr:
                                _line_q.put(line)
                        except Exception:
                            pass
                        finally:
                            _line_q.put(_eof_flag)
                    _reader_t = _th.Thread(target=_stderr_reader, daemon=True)
                    _reader_t.start()

                    duration = None
                    was_cancelled = False
                    was_timed_out = False
                    burn_deadline = time.monotonic() + _BURN_TIMEOUT
                    try:
                        while True:
                            if task_id in processing_tasks and processing_tasks[task_id].get('status') == 'cancelling':
                                logger.info(f"烧录任务被取消: {task_id}")
                                process.kill()
                                was_cancelled = True
                                break
                            if time.monotonic() > burn_deadline:
                                logger.error(f"烧录任务超时 (>{_BURN_TIMEOUT}s): {task_id}")
                                process.kill()
                                was_timed_out = True
                                break
                            try:
                                line = _line_q.get(timeout=5.0)
                            except _q.Empty:
                                continue
                            if line is _eof_flag:
                                break
                            if 'Duration' in line and duration is None:
                                m = re.search(r'Duration: (\d+):(\d+):(\d+)\.(\d+)', line)
                                if m:
                                    duration = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
                            if 'time=' in line and duration:
                                m = re.search(r'time=(\d+):(\d+):(\d+)\.(\d+)', line)
                                if m:
                                    current_time = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
                                    pct = min(90, 10 + int(current_time / duration * 80))
                                    if task_id in processing_tasks:
                                        processing_tasks[task_id]['progress'] = pct
                                        processing_tasks[task_id]['progress_info'] = f'烧录中 {pct}%'
                    except Exception:
                        pass
                    finally:
                        if process.poll() is None:
                            process.kill()
                        try:
                            process.wait(timeout=10)
                        except Exception:
                            pass
                        _reader_t.join(timeout=2)
                        with _burn_processes_lock:
                            _burn_processes.pop(task_id, None)
                    return process.returncode, was_timed_out, was_cancelled

                # 核显优先，失败（滤镜/驱动/权限问题）自动回退 CPU，超时不重试
                attempts = ([(True, 'h264_vaapi(核显)')] if use_vaapi else []) + [(False, 'libx264(CPU)')]
                returncode, timed_out, used_encoder = 1, False, attempts[-1][1]
                for hw, enc_name in attempts:
                    used_encoder = enc_name
                    run_cmd = _build_cmd(hw)
                    logger.info(f"烧录命令({enc_name}): {' '.join(run_cmd)}")
                    returncode, timed_out, was_cancelled = _run_ffmpeg_burn(run_cmd)
                    if was_cancelled:
                        if task_id in processing_tasks:
                            processing_tasks[task_id]['status'] = 'cancelled'
                            processing_tasks[task_id]['progress_info'] = '已取消'
                            processing_tasks[task_id]['end_time'] = datetime.now().isoformat()
                        if os.path.exists(output_path):
                            try: os.remove(output_path)
                            except OSError: pass
                        logger.info(f"烧录已取消: {task_id}")
                        return
                    if timed_out:
                        break
                    if returncode == 0 and os.path.exists(output_path):
                        break
                    if hw:
                        if os.path.exists(output_path):
                            try: os.remove(output_path)
                            except OSError: pass
                        logger.warning(f"VAAPI 核显编码失败(rc={returncode})，回退 CPU libx264: {task_id}")
                        if task_id in processing_tasks:
                            processing_tasks[task_id]['progress_info'] = '核显编码失败，自动改用 CPU 重试...'

                if timed_out:
                    raise Exception(f"烧录超时（>{_BURN_TIMEOUT}秒）")

                if returncode == 0 and os.path.exists(output_path):
                    if task_id in processing_tasks:
                        processing_tasks[task_id]['status'] = 'completed'
                        processing_tasks[task_id]['progress'] = 100
                        processing_tasks[task_id]['progress_info'] = '烧录完成'
                        processing_tasks[task_id]['result'] = {
                            'output_file': output_filename,
                            'output_path': output_path,
                            'size': os.path.getsize(output_path),
                            'encoder': used_encoder,
                        }
                        processing_tasks[task_id]['end_time'] = datetime.now().isoformat()
                    logger.info(f"字幕烧录完成: {output_filename} (编码器: {used_encoder})")
                else:
                    raise Exception(f"FFmpeg failed with code {returncode}")
                    
            except Exception as e:
                logger.error(f"烧录失败: {str(e)}")
                if task_id in processing_tasks:
                    processing_tasks[task_id]['status'] = 'failed'
                    processing_tasks[task_id]['error'] = str(e)
                    processing_tasks[task_id]['end_time'] = datetime.now().isoformat()
            finally:
                if burn_ass:
                    try:os.remove(burn_ass)
                    except OSError:pass
                # Bug #12 修复：从 _burn_processes 字典清理
                with _burn_processes_lock:
                    _burn_processes.pop(task_id, None)
                # 失败时清理半成品输出
                if task_id in processing_tasks and processing_tasks[task_id].get('status') == 'failed':
                    if os.path.exists(output_path):
                        try: os.remove(output_path)
                        except OSError: pass

        thread = threading.Thread(target=burn_process)
        thread.daemon = True
        thread.start()
        
        return jsonify({
            'success': True,
            'task_id': task_id,
            'message': '字幕烧录任务已启动'
        })
        
    except Exception as e:
        logger.error(f"启动烧录失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


def cleanup_old_files(max_age_days=7):
    """仅清理可重建缓存；上传原件与字幕成果没有自动过期策略。"""
    now = time.time()
    max_age_seconds = max_age_days * 86400
    total_deleted = 0

    for folder in [app.config['AUDIO_FOLDER']]:
        if not os.path.exists(folder):
            continue
        for filename in os.listdir(folder):
            # 隐藏状态文件绝不清理：.history.json/.glossaries.json/.notification.json
            # 都存在 subtitles 目录，原实现按 mtime 一并删除，30 天不活跃+重启
            # 后历史/术语表/通知配置会被静默清空（实测发生过）
            if filename.startswith('.'):
                continue
            filepath = os.path.join(folder, filename)
            if not os.path.isfile(filepath):
                continue
            try:
                if now - os.path.getmtime(filepath) > max_age_seconds:
                    os.remove(filepath)
                    total_deleted += 1
                    logger.info(f"清理旧文件: {filename}")
            except OSError:
                pass

    # 清理转码缓存（超过7天）
    cache_dir = '/tmp/transcode_cache'
    if os.path.exists(cache_dir):
        for filename in os.listdir(cache_dir):
            filepath = os.path.join(cache_dir, filename)
            if not os.path.isfile(filepath):
                continue
            try:
                if now - os.path.getmtime(filepath) > max_age_seconds:
                    os.remove(filepath)
                    total_deleted += 1
                    logger.info(f"清理转码缓存: {filename}")
            except OSError:
                pass

    # Bug #19 修复：清理 HLS 缓存（segment .ts 文件可能残留）
    hls_cache = '/tmp/hls_cache'
    if os.path.isdir(hls_cache):
        for session_dir in os.listdir(hls_cache):
            session_path = os.path.join(hls_cache, session_dir)
            if not os.path.isdir(session_path):
                continue
            try:
                if now - os.path.getmtime(session_path) > max_age_seconds:
                    import shutil
                    shutil.rmtree(session_path, ignore_errors=True)
                    total_deleted += 1
                    logger.info(f"清理 HLS 缓存: {session_dir}")
            except OSError:
                pass
    return total_deleted

def _enforce_hls_cache_size(max_gb=2):
    """Bug #19 修复：HLS 缓存总大小上限，超限按 mtime 删最旧"""
    hls_cache = '/tmp/hls_cache'
    if not os.path.isdir(hls_cache):
        return
    try:
        total_size = 0
        entries = []
        for d in os.listdir(hls_cache):
            p = os.path.join(hls_cache, d)
            if os.path.isdir(p):
                size = 0
                for root, _, files in os.walk(p):
                    for f in files:
                        try: size += os.path.getsize(os.path.join(root, f))
                        except OSError: pass
                mtime = os.path.getmtime(p)
                entries.append((mtime, p, size))
                total_size += size
        max_bytes = max_gb * 1024 * 1024 * 1024
        if total_size <= max_bytes:
            return
        entries.sort()  # 旧的在前
        for mtime, p, size in entries:
            if total_size <= max_bytes:
                break
            import shutil
            shutil.rmtree(p, ignore_errors=True)
            total_size -= size
            logger.warning(f"HLS 缓存超限 (>{max_gb}G)，删除: {p}")
    except Exception as e:
        logger.error(f"HLS 缓存大小检查失败: {e}")



@app.route('/api/subtitle-mux', methods=['POST'])
def subtitle_mux():
    """软字幕封装（2026-10-26）：字幕作为软轨封装进 MKV 容器。

    与烧录的区别：视频流/音频流直接 copy（秒级、零画质损失），字幕是独立轨道，
    播放器可开关，随时可以用 mkvextract 抽回来。产物落字幕目录，列表里标为成片。
    """
    try:
        data = request.get_json()
        if not data or 'subtitle_filename' not in data:
            return jsonify({'success': False, 'error': '缺少字幕文件名'}), 400
        subtitle_filename = data['subtitle_filename']
        subtitle_path = _safe_join(app.config['SUBTITLE_FOLDER'], subtitle_filename)
        if subtitle_path is None:
            return jsonify({'success': False, 'error': '非法路径'}), 400
        if not os.path.exists(subtitle_path):
            return jsonify({'success': False, 'error': '字幕文件不存在'}), 404
        if not subtitle_filename.lower().endswith(('.srt', '.ass', '.vtt')):
            return jsonify({'success': False, 'error': '软字幕封装仅支持 SRT/ASS/VTT 字幕（这些格式才有可切换的软轨意义）'}), 400
        base_name = re.sub(r'\.(srt|vtt|ass|json|txt)$', '', subtitle_filename, flags=re.IGNORECASE)
        video_path = _find_media_by_basename(base_name)
        if not video_path:
            return jsonify({'success': False, 'error': '未找到匹配的视频文件。请确保视频已上传且与字幕文件同名'}), 404
        import uuid
        output_filename = f'{base_name}.softsub_{uuid.uuid4().hex[:8]}.mkv'
        output_path = os.path.join(app.config['SUBTITLE_FOLDER'], output_filename)
        task_id = f"mux_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        with processing_tasks_lock:
            processing_tasks[task_id] = {
                'status': 'processing',
                'filename': subtitle_filename,
                'start_time': datetime.now().isoformat(),
                'progress': 0,
                'progress_info': '软字幕封装已启动...'
            }

        def mux_process():
            output_path_local = output_path
            try:
                with processing_tasks_lock:
                    processing_tasks[task_id]['progress'] = 10
                    processing_tasks[task_id]['progress_info'] = '正在封装软字幕轨...'
                # 从字幕文件名猜语言标记（.en-zh → chi；供播放器字幕菜单显示）
                lang = 'und'
                m = re.search(r'\.([a-z]{2,3})(?:-[a-z]{2,3})?$', os.path.splitext(subtitle_filename)[0], re.IGNORECASE)
                if m:
                    lang = {'zh': 'chi', 'en': 'eng', 'ja': 'jpn', 'ko': 'kor',
                            'fr': 'fra', 'de': 'deu', 'es': 'spa', 'ru': 'rus'}.get(m.group(1).lower(), 'und')
                sub_title = '中文字幕' if lang == 'chi' else ('English' if lang == 'eng' else 'Subtitle')
                cmd = ['ffmpeg', '-nostdin', '-y',
                       '-i', video_path,
                       '-i', subtitle_path,
                       '-map', '0:v:0', '-map', '0:a?', '-map', '1:0',
                       '-c:v', 'copy', '-c:a', 'copy', '-c:s', 'srt',
                       '-metadata:s:s:0', f'language={lang}',
                       '-metadata:s:s:0', f'title={sub_title}',
                       '-f', 'matroska', output_path_local]
                proc = sp.run(cmd, stdout=sp.DEVNULL, stderr=sp.PIPE, timeout=3600)
                if proc.returncode != 0:
                    raise RuntimeError('ffmpeg 封装失败: ' + proc.stderr.decode('utf-8', 'replace')[-300:])
                size = os.path.getsize(output_path_local)
                with processing_tasks_lock:
                    processing_tasks[task_id].update({
                        'status': 'completed', 'progress': 100, 'progress_info': '封装完成',
                        'result': {'encoder': 'stream-copy(软字幕)', 'output_file': output_filename,
                                   'output_path': output_path_local, 'size': size}})
                logger.info(f"软字幕封装完成: {output_filename}")
            except Exception as e:
                logger.error(f"软字幕封装失败: {e}")
                with processing_tasks_lock:
                    processing_tasks[task_id].update({'status': 'failed', 'progress_info': '封装失败',
                                                      'error': str(e)[:300]})
                try:
                    os.remove(output_path_local)
                except OSError:
                    pass

        threading.Thread(target=mux_process, daemon=True).start()
        return jsonify({'success': True, 'task_id': task_id, 'message': '软字幕封装任务已启动'})
    except Exception as e:
        logger.error(f"启动软字幕封装失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# Whisper 模型管理（2026-10-26）：预下载常用档位 / 删除不用的省空间。
_WHISPER_ALLOWED = ('tiny', 'base', 'small', 'medium', 'turbo', 'large-v3-turbo', 'large-v3', 'large')
_model_dl_status = {}
_model_dl_lock = threading.Lock()

def _whisper_cache_dirs(name):
    """返回某档位可能落在的 HF cache 目录（Systran 官方 + mobius turbo 镜像）。"""
    cache_name = 'large-v3' if name == 'large' else name
    base = '/root/.cache/huggingface/hub'
    return [
        os.path.join(base, f'models--Systran--faster-whisper-{cache_name}'),
        os.path.join(base, f'models--mobiuslabsgmbh--faster-whisper-{cache_name}'),
    ]

@app.route('/api/models/whisper/download', methods=['POST'])
def download_whisper_model():
    """后台预下载指定档位的 Whisper 模型到 HF cache。"""
    try:
        data = request.get_json() or {}
        name = (data.get('model') or '').strip()
        if name not in _WHISPER_ALLOWED:
            return jsonify({'success': False, 'error': f'不支持的模型档位: {name}'}), 400
        with _model_dl_lock:
            running = {k: v for k, v in _model_dl_status.items() if v.get('status') == 'downloading'}
            if name in running:
                return jsonify({'success': False, 'error': '该模型正在下载中'}), 409
            if len(running) >= 1:
                return jsonify({'success': False, 'error': '已有模型在下载，请稍候'}), 409
            _model_dl_status[name] = {'status': 'downloading', 'started': datetime.now().isoformat(), 'error': None}

        def dl_process():
            try:
                from faster_whisper import WhisperModel
                WhisperModel(name, device='cpu', compute_type='int8')
                with _model_dl_lock:
                    _model_dl_status[name] = {'status': 'done', 'error': None,
                                              'finished': datetime.now().isoformat()}
                logger.info(f"Whisper 模型预下载完成: {name}")
            except Exception as e:
                logger.error(f"Whisper 模型预下载失败 {name}: {e}")
                with _model_dl_lock:
                    _model_dl_status[name] = {'status': 'failed', 'error': str(e)[:300]}

        threading.Thread(target=dl_process, daemon=True).start()
        return jsonify({'success': True, 'message': f'模型 {name} 下载已启动'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/models/whisper/<name>', methods=['DELETE'])
def delete_whisper_model(name):
    """删除已缓存的 Whisper 模型目录以释放空间。"""
    try:
        if name not in _WHISPER_ALLOWED:
            return jsonify({'success': False, 'error': f'不支持的模型档位: {name}'}), 400
        with _model_dl_lock:
            if _model_dl_status.get(name, {}).get('status') == 'downloading':
                return jsonify({'success': False, 'error': '该模型正在下载中，无法删除'}), 409
        removed = []
        for d in _whisper_cache_dirs(name):
            if os.path.isdir(d):
                import shutil
                shutil.rmtree(d, ignore_errors=True)
                removed.append(os.path.basename(d))
        if not removed:
            return jsonify({'success': False, 'error': '该模型未在本地缓存'}), 404
        logger.info(f"Whisper 模型已删除: {name} ({', '.join(removed)})")
        return jsonify({'success': True, 'message': f'模型 {name} 已删除', 'removed': removed})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# 监控目录自动字幕（2026-10-27 v1.2.0）：扫描监控目录，新视频落地自动排队生成。
# 配置存 data/watch.json；扫描状态存 data/watch-state.json（含已入队清单防重复）。
# 支持时段窗口（watch.window，如 "0-7" 表示 0:00-7:59 才入队，避免白天挤占）。
# ============================================================================
WATCH_CONFIG_FILE = os.path.join(os.environ.get('AISUB_DATA_DIR', '/app/data'), 'watch.json') \
    if os.path.isdir(os.environ.get('AISUB_DATA_DIR', '/app/data')) \
    else os.path.join('data', 'watch.json')
WATCH_STATE_FILE = WATCH_CONFIG_FILE.replace('watch.json', 'watch-state.json')
_watch_state_lock = threading.Lock()

def _load_watch_config():
    try:
        with open(WATCH_CONFIG_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _save_watch_config(cfg):
    os.makedirs(os.path.dirname(WATCH_CONFIG_FILE), exist_ok=True)
    tmp = WATCH_CONFIG_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, WATCH_CONFIG_FILE)

def _load_watch_state():
    try:
        with open(WATCH_STATE_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _save_watch_state(st):
    try:
        os.makedirs(os.path.dirname(WATCH_STATE_FILE), exist_ok=True)
        tmp = WATCH_STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, WATCH_STATE_FILE)
    except Exception:
        pass

def _watch_window_open(cfg):
    """时段窗口：window 形如 "0-7"（0:00-7:59）或空=全天。"""
    w = str(cfg.get('window') or '').strip()
    if not w or '-' not in w:
        return True
    try:
        a, b = w.split('-', 1)
        h = datetime.now().hour
        a, b = int(a), int(b)
        return (a <= h <= b) if a <= b else (h >= a or h <= b)
    except Exception:
        return True

def _history_filenames():
    try:
        hist = _load_history()
        return {str(h.get('filename') or '') for h in hist}
    except Exception:
        return set()

def _active_task_paths():
    out = set()
    try:
        with processing_tasks_lock:
            for t in processing_tasks.values():
                vp = t.get('_video_path') or ''
                if vp:
                    out.add(vp)
    except Exception:
        pass
    try:
        q = _get_task_queue()
        for t in q.list_tasks(limit=500):
            if t.get('status') in ('pending', 'running'):
                vp = t.get('video_path') or ''
                if vp:
                    out.add(vp)
    except Exception:
        pass
    return out

def watch_scan_once():
    """单次扫描：返回 {scanned, enqueued, skipped_active}。"""
    cfg = _load_watch_config()
    result = {'scanned': 0, 'enqueued': 0, 'skipped_active': 0, 'window_open': _watch_window_open(cfg)}
    if not cfg.get('enabled') or not cfg.get('dir'):
        return result
    if not result['window_open']:
        return result
    wdir = cfg['dir']
    if not os.path.isdir(wdir):
        return result
    state = _load_watch_state()
    sizes = state.get('sizes') or {}
    enqueued = set(state.get('enqueued') or [])
    new_sizes = {}
    nas_root = os.path.realpath(os.environ.get('NAS_PATH', '/nas/videos'))
    active = _active_task_paths()
    hist_names = _history_filenames()
    stable_age = int(cfg.get('stable_age') or 300)
    now = time.time()
    for name in os.listdir(wdir):
        fp = os.path.join(wdir, name)
        if not os.path.isfile(fp) or name.startswith('.'):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in VIDEO_EXTENSIONS:
            continue
        result['scanned'] += 1
        try:
            st = os.stat(fp)
        except OSError:
            continue
        new_sizes[fp] = st.st_size
        key = fp
        # 稳定判定：体积与上次扫描一致 + 文件龄达标
        if sizes.get(fp) != st.st_size or (now - st.st_mtime) < stable_age:
            continue
        if fp in enqueued or fp in active:
            result['skipped_active'] += 1
            continue
        # 已有同名历史（曾生成过字幕）→ 不重复排队
        if name in hist_names:
            enqueued.add(key)
            continue
        rel = os.path.relpath(fp, nas_root)
        try:
            q = _get_task_queue()
            task_id = q.add('whisper', fp, {
                'model': cfg.get('model') or 'small',
                'language': cfg.get('language') or 'auto',
                'format': 'srt',
                'translate_to': cfg.get('translate_to') or None,
                'translate_engine': 'auto',
                'diarize': bool(cfg.get('diarize')),
                'num_speakers': 0,
                'save_to_source_dir': False,
                'save_strategy': 'subtitles',
                'output_naming': 'plain',
                'bilingual': bool(cfg.get('bilingual')),
                'split_max_chars': 0,
                'glossary': {},
                'filename': rel,
                'is_nas_file': True,
            })
            enqueued.add(key)
            result['enqueued'] += 1
            logger.info(f"[监控] 自动入队: {name} (task={task_id})")
        except Exception as e:
            logger.error(f"[监控] 入队失败 {name}: {e}")
    with _watch_state_lock:
        state['sizes'] = new_sizes
        state['enqueued'] = sorted(enqueued)[-500:]
        state['last_scan'] = datetime.now().isoformat()
        state['last_result'] = result
        _save_watch_state(state)
    return result

def _watch_thread():
    logger.info("[监控] 自动字幕扫描线程已启动")
    while True:
        try:
            r = watch_scan_once()
            if r.get('enqueued'):
                logger.info(f"[监控] 本轮入队 {r['enqueued']} 个")
        except Exception as e:
            logger.error(f"[监控] 扫描异常: {e}")
        time.sleep(int(os.environ.get('WATCH_INTERVAL', '180')))

def _start_watch_thread():
    if os.environ.get('WATCH_AUTOLOAD', '1') == '0':
        return
    cfg = _load_watch_config()
    if cfg.get('enabled'):
        t = threading.Thread(target=_watch_thread, daemon=True, name='watch-subtitles')
        t.start()

@app.route('/api/watch-settings', methods=['GET'])
def api_watch_settings_get():
    cfg = _load_watch_config()
    cfg['state'] = {k: v for k, v in _load_watch_state().items() if k != 'sizes'}
    return jsonify({'success': True, 'config': cfg})

@app.route('/api/watch-settings', methods=['POST'])
def api_watch_settings_post():
    try:
        data = request.get_json() or {}
        cfg = {
            'enabled': bool(data.get('enabled')),
            'dir': str(data.get('dir') or '').strip(),
            'model': data.get('model') or 'small',
            'language': data.get('language') or 'auto',
            'bilingual': bool(data.get('bilingual')),
            'translate_to': (data.get('translate_to') or None),
            'diarize': bool(data.get('diarize')),
            'window': str(data.get('window') or '').strip(),
            'stable_age': int(data.get('stable_age') or 300),
        }
        if cfg['enabled']:
            if not cfg['dir'] or not os.path.isdir(cfg['dir']):
                return jsonify({'success': False, 'error': '监控目录不存在'}), 400
            if cfg['model'] not in ('tiny', 'base', 'small', 'medium', 'large', 'large-v3-turbo'):
                return jsonify({'success': False, 'error': f'非法模型: {cfg["model"]}'}), 400
        _save_watch_config(cfg)
        if cfg['enabled']:
            _start_watch_thread()
        return jsonify({'success': True, 'message': '监控设置已保存'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/watch-scan', methods=['POST'])
def api_watch_scan():
    """手动触发一次扫描（不受时段窗口限制，便于测试）。"""
    try:
        r = watch_scan_once()
        return jsonify({'success': True, 'result': r})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# 字幕直推媒体库（2026-10-27 v1.2.0）：把字幕按播放器规范命名复制到媒体库目录，
# Jellyfin/Emby/Infuse 等刷新即见。目标目录强制在 NAS 媒体根下。
# ============================================================================
@app.route('/api/push-subtitle', methods=['POST'])
def api_push_subtitle():
    try:
        data = request.get_json() or {}
        filename = data.get('filename')
        target_dir = str(data.get('target_dir') or '').strip()
        mode = data.get('mode') or 'same'
        if not filename:
            return jsonify({'success': False, 'error': '缺少字幕文件名'}), 400
        subtitle_path = _safe_subtitle_path(filename)
        if subtitle_path is None or not os.path.isfile(subtitle_path):
            return jsonify({'success': False, 'error': '字幕文件不存在'}), 404
        if not target_dir:
            return jsonify({'success': False, 'error': '缺少目标目录'}), 400
        nas_root = os.path.realpath(os.environ.get('NAS_PATH', '/nas/videos'))
        real_target = os.path.realpath(target_dir)
        if real_target != nas_root and not real_target.startswith(nas_root + os.sep):
            return jsonify({'success': False, 'error': '目标目录必须在 NAS 媒体目录内'}), 400
        if not os.path.isdir(real_target):
            return jsonify({'success': False, 'error': '目标目录不存在'}), 404
        base_name = re.sub(r'\.(srt|vtt|ass|json|txt)$', '', filename, flags=re.IGNORECASE)
        # 匹配媒体文件（同 basename）确认落在正确目录
        video = _find_media_by_basename(base_name)
        lang_tag = ''
        m = re.search(r'\.([a-z]{2,3})(?:-[a-z]{2,3})?$', base_name, re.IGNORECASE)
        if m:
            lang_tag = '.' + m.group(1).lower()
        if mode == 'zh':
            out_name = base_name + '.zh.srt'
        elif mode == 'same':
            out_name = base_name + '.srt'
        else:  # auto
            out_name = base_name + (lang_tag if lang_tag else '') + '.srt'
        # 若目标目录下存在同名视频，则字幕直接与视频同目录同名
        dest = os.path.join(real_target, out_name)
        if video:
            vdir = os.path.dirname(video)
            vname = os.path.splitext(os.path.basename(video))[0]
            dest = os.path.join(vdir, vname + (('.zh.srt') if mode == 'zh' else (lang_tag if mode == 'auto' and lang_tag else '') + '.srt' if mode != 'same' else '.srt'))
        import shutil
        shutil.copy2(subtitle_path, dest)
        logger.info(f"字幕已推送: {filename} -> {dest}")
        return jsonify({'success': True, 'message': '字幕已推送到媒体库', 'dest': dest})
    except Exception as e:
        logger.error(f"字幕推送失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# 说话人命名（2026-10-27 v1.2.0）：把【说话人N】批量替换成真实姓名。
# 映射持久化在 <字幕名>.speakers.json，下次打开自动带出。
# ============================================================================
@app.route('/api/speaker-names/<path:filename>', methods=['GET'])
def api_speaker_names_get(filename):
    try:
        subtitle_path = _safe_subtitle_path(filename)
        if subtitle_path is None or not os.path.isfile(subtitle_path):
            return jsonify({'success': False, 'error': '字幕文件不存在'}), 404
        with open(subtitle_path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
        speakers = sorted(set(re.findall(r'【(说话人\d+)】', text)))
        saved = {}
        sidecar = os.path.splitext(subtitle_path)[0] + '.speakers.json'
        if os.path.isfile(sidecar):
            try:
                with open(sidecar, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
            except Exception:
                saved = {}
        return jsonify({'success': True, 'speakers': speakers, 'saved': saved})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/speaker-names/<path:filename>', methods=['POST'])
def api_speaker_names_post(filename):
    try:
        data = request.get_json() or {}
        mapping = data.get('mapping') or {}
        if not mapping:
            return jsonify({'success': False, 'error': '缺少映射'}), 400
        subtitle_path = _safe_subtitle_path(filename)
        if subtitle_path is None or not os.path.isfile(subtitle_path):
            return jsonify({'success': False, 'error': '字幕文件不存在'}), 404
        if not filename.lower().endswith('.srt'):
            return jsonify({'success': False, 'error': '说话人命名仅支持 SRT'}), 400
        with open(subtitle_path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
        replaced = 0
        for spk, name in mapping.items():
            name = str(name).strip()
            if not name:
                continue
            new_text, n = re.subn(re.escape('【' + spk + '】'), '【' + name + '】', text)
            if n:
                replaced += n
                text = new_text
        with open(subtitle_path, 'w', encoding='utf-8') as f:
            f.write(text)
        sidecar = os.path.splitext(subtitle_path)[0] + '.speakers.json'
        saved = {}
        if os.path.isfile(sidecar):
            try:
                with open(sidecar, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
            except Exception:
                saved = {}
        saved.update(mapping)
        with open(sidecar, 'w', encoding='utf-8') as f:
            json.dump(saved, f, ensure_ascii=False, indent=1)
        return jsonify({'success': True, 'message': f'已替换 {replaced} 处', 'replaced': replaced})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# 云端 ASR 配置（2026-10-27 v1.2.0）
# ============================================================================
ASR_CONFIG_FILE = os.path.join(os.environ.get('AISUB_DATA_DIR', '/app/data'), 'asr.json') \
    if os.path.isdir(os.environ.get('AISUB_DATA_DIR', '/app/data')) \
    else os.path.join('data', 'asr.json')

def _load_asr_config():
    try:
        with open(ASR_CONFIG_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _save_asr_config(cfg):
    os.makedirs(os.path.dirname(ASR_CONFIG_FILE), exist_ok=True)
    tmp = ASR_CONFIG_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ASR_CONFIG_FILE)

@app.route('/api/asr-config', methods=['GET'])
def api_asr_config_get():
    cfg = _load_asr_config()
    key = str(cfg.get('api_key') or '')
    masked = (key[:6] + '***' + key[-4:]) if len(key) > 12 else ('***' if key else '')
    return jsonify({'success': True, 'config': {
        'enabled': bool(cfg.get('enabled')),
        'base_url': cfg.get('base_url') or '',
        'model': cfg.get('model') or '',
        'api_key_set': bool(key),
        'api_key_masked': masked,
    }})

@app.route('/api/asr-config', methods=['POST'])
def api_asr_config_post():
    try:
        data = request.get_json() or {}
        cfg = _load_asr_config()
        cfg['enabled'] = bool(data.get('enabled'))
        base_url = str(data.get('base_url') or '').strip().rstrip('/')
        if base_url and not re.match(r'^https?://', base_url):
            return jsonify({'success': False, 'error': 'base_url 必须以 http(s):// 开头'}), 400
        cfg['base_url'] = base_url
        cfg['model'] = str(data.get('model') or '').strip()[:120]
        new_key = str(data.get('api_key') or '').strip()
        if new_key and '*' not in new_key:
            cfg['api_key'] = new_key
        if cfg['enabled'] and not (cfg.get('base_url') and cfg.get('model')):
            return jsonify({'success': False, 'error': '启用云端 ASR 需要填全 base_url / model（api_key 视服务而定）'}), 400
        _save_asr_config(cfg)
        return jsonify({'success': True, 'message': '云端 ASR 配置已保存'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/cleanup', methods=['POST'])
def api_cleanup():
    """手动触发清理旧文件"""
    try:
        data = request.get_json(silent=True) or {}
        target = data.get('target', 'old')
        deleted = 0
        if target == 'history':
            _save_history([])
        elif target == 'failed-tasks':
            q = _get_task_queue()
            for task in q.list_tasks(status='failed', limit=200):
                if q.delete(task.id):
                    deleted += 1
            for task in q.list_tasks(status='cancelled', limit=200):
                if q.delete(task.id):
                    deleted += 1
        elif target == 'cache':
            import shutil
            for cache_dir in ('/tmp/transcode_cache', '/tmp/hls_cache'):
                if os.path.isdir(cache_dir):
                    for name in os.listdir(cache_dir):
                        path = os.path.join(cache_dir, name)
                        try:
                            if os.path.isdir(path):
                                shutil.rmtree(path, ignore_errors=True)
                            else:
                                os.remove(path)
                            deleted += 1
                        except OSError:
                            pass
        elif target == 'uploads':
            for folder in (app.config['UPLOAD_FOLDER'], app.config['AUDIO_FOLDER']):
                if os.path.isdir(folder):
                    for name in os.listdir(folder):
                        path = os.path.join(folder, name)
                        if os.path.isfile(path) and not name.startswith('.'):
                            try:
                                os.remove(path)
                                deleted += 1
                            except OSError:
                                pass
        else:
            days = int(data.get('days', 7))
            deleted = cleanup_old_files(days)
        return jsonify({'success': True, 'deleted': deleted, 'target': target, 'message': f'清理完成: {deleted} 项'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/glossaries', methods=['GET'])
def list_glossaries():
    try:
        return jsonify({'success': True, 'glossaries': _load_glossaries()})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/glossaries', methods=['POST'])
def save_glossary():
    try:
        data = request.get_json() or {}
        name = (data.get('name') or '').strip()
        terms = data.get('terms') or {}
        if not re.match(r'^[A-Za-z0-9_.\- 一-鿿]{1,80}$', name):
            return jsonify({'success': False, 'error': '术语表名称非法'}), 400
        if isinstance(terms, str):
            parsed = {}
            for line in terms.splitlines():
                if '=' in line:
                    k, v = line.split('=', 1)
                    if k.strip():
                        parsed[k.strip()] = v.strip()
            terms = parsed
        if not isinstance(terms, dict):
            return jsonify({'success': False, 'error': '术语表内容非法'}), 400
        glossaries = _load_glossaries()
        glossaries[name] = {str(k): str(v) for k, v in terms.items() if str(k).strip()}
        _json_file_save(GLOSSARY_FILE, glossaries)
        return jsonify({'success': True, 'name': name, 'message': '术语表已保存'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/glossaries/<name>', methods=['DELETE'])
def delete_glossary(name):
    try:
        glossaries = _load_glossaries()
        if name not in glossaries:
            return jsonify({'success': False, 'error': '术语表不存在'}), 404
        glossaries.pop(name, None)
        _json_file_save(GLOSSARY_FILE, glossaries)
        return jsonify({'success': True, 'message': '术语表已删除'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/media-library', methods=['GET'])
def api_media_library():
    """列出上传目录里的媒体文件（媒体库页）：文件在重启后无处可寻的补齐。"""
    try:
        items = []
        folder = app.config['UPLOAD_FOLDER']
        for name in os.listdir(folder):
            if name.startswith('.'):
                continue
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            if os.path.splitext(name)[1].lower() not in MEDIA_EXTENSIONS:
                continue
            existing = _existing_subtitles_for_media(path)
            items.append({
                'filename': name,
                'size': os.path.getsize(path),
                'modified': datetime.fromtimestamp(os.path.getmtime(path)).astimezone().isoformat(),
                'has_subtitle': bool(existing),
                'subtitles': existing,
            })
        items.sort(key=lambda x: x['modified'], reverse=True)
        return jsonify({'success': True, 'files': items})
    except Exception as e:
        logger.error(f"媒体库列表失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/summary', methods=['POST'])
def api_summary():
    """AI 转写摘要：读字幕文本，走已配置的 LLM 生成「标题/摘要/要点/章节」并保存为 .总结.md。"""
    try:
        data = request.get_json() or {}
        filename = data.get('filename')
        subtitle_path = _safe_subtitle_path(filename)
        if subtitle_path is None or not os.path.isfile(subtitle_path):
            return jsonify({'success': False, 'error': '字幕文件不存在或路径非法'}), 404
        cfg = load_llm_config()
        if not llm_translate_ready(cfg):
            return jsonify({'success': False,
                            'error': 'AI 摘要需要 LLM：请先到「工具中心 → AI 翻译」配置接口并启用'}), 400

        segments = _parse_subtitle_file(subtitle_path)
        texts = [s.get('text', '').strip() for s in segments if s.get('text', '').strip()]
        if not texts:
            return jsonify({'success': False, 'error': '字幕没有文本内容'}), 400

        # 带时间戳的稿子（每 10 条插一次时间锚点，方便 LLM 产出章节），超长截断保护
        lines = []
        for i, seg in enumerate(segments):
            t = str(seg.get('text', '') or '').strip()
            if not t:
                continue
            if i % 10 == 0:
                mm, ss = divmod(int(float(seg.get('start', 0) or 0)), 60)
                lines.append(f'[{mm:02d}:{ss:02d}]')
            lines.append(t)
        transcript = '\n'.join(lines)
        if len(transcript) > 40000:
            transcript = transcript[:40000] + '\n…（过长已截断）'

        mode = (data.get('mode') or 'summary').strip()
        if mode == 'minutes':
            system = (
                '你是专业的会议/访谈记录员。根据带时间戳的转写稿输出中文 Markdown 会议纪要，结构固定为：\n'
                '# 会议纪要\n## 会议概要\n（主题/参与人/时长，2-3句）\n## 讨论要点\n'
                '（按议题分组，每条注明发言人，引用原话时保留 [mm:ss] 时间戳）\n'
                '## 结论与决定\n（逐条列出）\n## 待办事项\n（- [ ] 格式，注明负责人与时间点）\n'
                '发言人信息来自转写稿中的【姓名】前缀；若转写稿没有说话人标记则省略发言人。\n'
                '只输出 Markdown 正文，不要寒暄和解释。'
            )
        else:
            system = (
                '你是专业的视频内容分析师。根据带时间戳的转写稿输出中文 Markdown，结构固定为：\n'
                '# 视频内容总结\n## 一句话摘要\n（50字以内）\n## 核心要点\n（3-8条，每条一行）\n'
                '## 章节时间线\n（按 [mm:ss] 给出 3-10 个章节：`[mm:ss] 章节名 — 一句话说明`）\n'
                '只输出 Markdown 正文，不要寒暄和解释。'
            )
        t0 = time.monotonic()
        summary_md = llm_chat(cfg, [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': transcript},
        ], timeout=240, max_tokens=4000)
        elapsed = int((time.monotonic() - t0) * 1000)

        stem = os.path.splitext(os.path.basename(filename))[0]
        out_name = f'{stem}.纪要.md' if mode == 'minutes' else f'{stem}.总结.md'
        out_path = os.path.join(app.config['SUBTITLE_FOLDER'], out_name)
        if not _is_safe_basename(out_name):
            return jsonify({'success': False, 'error': '输出文件名非法'}), 400
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(summary_md)
        logger.info(f"AI 摘要完成: {out_name} ({elapsed}ms, {len(summary_md)} 字)")
        return jsonify({'success': True, 'summary': summary_md, 'saved_as': out_name,
                        'elapsed_ms': elapsed, 'segments': len(texts)})
    except Exception as e:
        logger.error(f"AI 摘要失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/notification-settings', methods=['GET', 'POST'])
def notification_settings():
    try:
        if request.method == 'GET':
            settings = _load_notification_settings()
            safe = dict(settings)
            if safe.get('webhook_url'):
                safe['webhook_url_masked'] = re.sub(r'(?<=.{8}).(?=.{4})', '*', safe['webhook_url'])
            return jsonify({'success': True, 'settings': safe})
        data = request.get_json() or {}
        settings = {
            'enabled': bool(data.get('enabled')),
            'webhook_url': str(data.get('webhook_url') or '').strip(),
        }
        if settings['enabled'] and not re.match(r'^https?://', settings['webhook_url']):
            return jsonify({'success': False, 'error': 'Webhook URL 必须以 http:// 或 https:// 开头'}), 400
        _json_file_save(NOTIFICATION_FILE, settings)
        return jsonify({'success': True, 'message': '通知设置已保存'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/notification-test', methods=['POST'])
def notification_test():
    ok = _send_webhook_notification('AI Subtitle 测试通知', '通知配置可用', {'kind': 'test'})
    return jsonify({'success': ok, 'message': '测试通知已发送' if ok else '测试通知发送失败'})

# === AI 翻译引擎（LLM，可选）===
@app.route('/api/llm-config', methods=['GET'])
def api_llm_config_get():
    cfg = load_llm_config()
    key = str(cfg.get('api_key') or '')
    masked = (key[:6] + '***' + key[-4:]) if len(key) > 12 else ('***' if key else '')
    return jsonify({
        'success': True,
        'config': {
            'enabled': bool(cfg.get('enabled')),
            'base_url': cfg.get('base_url') or '',
            'model': cfg.get('model') or '',
            'api_key_set': bool(key),
            'api_key_masked': masked,
        },
    })

@app.route('/api/llm-config', methods=['POST'])
def api_llm_config_post():
    try:
        data = request.get_json() or {}
        cfg = load_llm_config()
        cfg['enabled'] = bool(data.get('enabled'))
        base_url = str(data.get('base_url') or '').strip().rstrip('/')
        if base_url and not re.match(r'^https?://', base_url):
            return jsonify({'success': False, 'error': 'base_url 必须以 http:// 或 https:// 开头'}), 400
        cfg['base_url'] = base_url
        cfg['model'] = str(data.get('model') or '').strip()[:120]
        # api_key 留空 = 保留原值（前端回显的是掩码）
        new_key = str(data.get('api_key') or '').strip()
        if new_key and '*' not in new_key:
            cfg['api_key'] = new_key
        if cfg.get('enabled') and not (cfg.get('base_url') and cfg.get('api_key') and cfg.get('model')):
            return jsonify({'success': False, 'error': '启用 LLM 翻译需要填全 base_url / api_key / model'}), 400
        save_llm_config(cfg)
        return jsonify({'success': True, 'message': 'AI 翻译引擎配置已保存'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/llm-test', methods=['POST'])
def api_llm_test():
    try:
        payload = request.get_json(silent=True) or {}
        cfg = load_llm_config()
        # 允许用表单当前值直接测试（不必先保存）；空值回落已保存配置
        if str(payload.get('base_url') or '').strip():
            cfg['base_url'] = str(payload['base_url']).strip().rstrip('/')
        if str(payload.get('api_key') or '').strip() and '*' not in payload['api_key']:
            cfg['api_key'] = str(payload['api_key']).strip()
        if str(payload.get('model') or '').strip():
            cfg['model'] = str(payload['model']).strip()
        cfg['enabled'] = True  # 测试时视作启用
        if not llm_translate_ready(cfg):
            return jsonify({'success': False, 'error': '配置不完整（需要 base_url / api_key / model 齐全）'}), 400
        target = payload.get('target') or 'zh'
        t0 = time.monotonic()
        out = llm_chat(cfg, [
            {'role': 'system', 'content': '你是字幕翻译，把用户句子翻译成目标语言，只输出译文。'},
            {'role': 'user', 'content': f'Translate to {target}: The quick brown fox jumps over the lazy dog.'},
        ], timeout=30, max_tokens=200)
        elapsed = int((time.monotonic() - t0) * 1000)
        return jsonify({'success': True, 'output': out, 'elapsed_ms': elapsed, 'message': f'连接成功（{elapsed}ms）'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'连接失败: {e}'}), 502

@app.route('/api/reports/queue', methods=['GET'])
def queue_report():
    try:
        q = _get_task_queue()
        limit = min(max(int(request.args.get('limit', 200)), 1), 1000)
        tasks = [t.to_dict() for t in q.list_tasks(limit=limit)]
        report_format = (request.args.get('format') or 'json').lower()
        if report_format == 'csv':
            import csv
            import io
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(['id', 'type', 'status', 'filename', 'progress', 'created_at', 'started_at', 'finished_at', 'error'])
            for task in tasks:
                writer.writerow([
                    task.get('id'),
                    task.get('type'),
                    task.get('status'),
                    os.path.basename(task.get('video_path') or ''),
                    task.get('progress'),
                    task.get('created_at'),
                    task.get('started_at'),
                    task.get('finished_at'),
                    task.get('error') or '',
                ])
            return Response(buf.getvalue(), mimetype='text/csv; charset=utf-8', headers={
                'Content-Disposition': f'attachment; filename=queue_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
            })
        return jsonify({'success': True, 'stats': q.get_stats(), 'tasks': tasks})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/reports/batch/<batch_id>', methods=['GET'])
def batch_report(batch_id):
    try:
        q = _get_task_queue()
        tasks = []
        for task in q.list_tasks(limit=1000):
            row = task.to_dict()
            try:
                payload = json.loads(row.get('payload') or '{}')
            except Exception:
                payload = {}
            if payload.get('batch_id') == batch_id:
                row['payload_json'] = payload
                tasks.append(row)
        stats = {}
        for task in tasks:
            stats[task.get('status')] = stats.get(task.get('status'), 0) + 1
        return jsonify({'success': True, 'batch_id': batch_id, 'stats': stats, 'tasks': tasks})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/models', methods=['GET'])
def models_info():
    """模型与缓存概览。"""
    try:
        translator_dir = os.environ.get('TRANSLATOR_MODELS_DIR', '/app/translator_models')
        whisper_dir = '/root/.cache/huggingface/hub'

        def dir_size(path):
            total = 0
            if os.path.exists(path):
                for root, _, files in os.walk(path):
                    for name in files:
                        try:
                            total += os.path.getsize(os.path.join(root, name))
                        except OSError:
                            pass
            return total

        translators = []
        if os.path.isdir(translator_dir):
            for name in sorted(os.listdir(translator_dir)):
                path = os.path.join(translator_dir, name)
                if os.path.isdir(path):
                    translators.append({'name': name, 'size': dir_size(path), 'path': path})

        whisper = []
        if os.path.isdir(whisper_dir):
            for name in sorted(os.listdir(whisper_dir)):
                if name.startswith('models--Systran--faster-whisper-'):
                    path = os.path.join(whisper_dir, name)
                    whisper.append({'name': name.replace('models--Systran--faster-whisper-', ''), 'size': dir_size(path), 'path': path})

        return jsonify({
            'success': True,
            'translator_models': translators,
            'whisper_models': whisper,
            'whisper_downloads': dict(_model_dl_status),
            'whisper_allowed': list(_WHISPER_ALLOWED),
            'worker_count': int(os.environ.get('TASK_WORKER_COUNT', '2')),
            'resource_mode': os.environ.get('RESOURCE_MODE', 'balanced'),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/models/translator/<lang_pair>', methods=['DELETE'])
def delete_translator_model(lang_pair):
    """删除指定翻译模型目录。"""
    try:
        if not re.match(r'^[a-z]{2,3}-[a-z]{2,3}$', lang_pair):
            return jsonify({'success': False, 'error': '非法语言对'}), 400
        translator_dir = os.environ.get('TRANSLATOR_MODELS_DIR', '/app/translator_models')
        path = _safe_join_allow_subpath(translator_dir, lang_pair)
        if path is None or not os.path.isdir(path):
            return jsonify({'success': False, 'error': '模型不存在'}), 404
        import shutil
        shutil.rmtree(path, ignore_errors=True)
        return jsonify({'success': True, 'message': f'已删除翻译模型 {lang_pair}'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/resource-profile', methods=['GET'])
def resource_profile():
    return jsonify({
        'success': True,
        'mode': os.environ.get('RESOURCE_MODE', 'balanced'),
        'worker_count': int(os.environ.get('TASK_WORKER_COUNT', '2')),
        'profiles': {
            'light': {'worker_count': 1, 'model': 'tiny'},
            'balanced': {'worker_count': 2, 'model': 'tiny/base'},
            'performance': {'worker_count': 4, 'model': 'base/small'},
        },
        'message': '运行中修改资源模式需要调整 docker-compose.yml 后重建容器'
    })


# 初始化


# 转码任务管理
_transcode_tasks = {}
_transcode_lock = threading.Lock()
_preview_streams = {}
_preview_streams_lock = threading.Lock()
_hls_processes = {}
_hls_processes_lock = threading.Lock()
# Bug #12 修复：烧录 ffmpeg 进程跟踪 + 超时
_burn_processes = {}
_burn_processes_lock = threading.Lock()
_BURN_TIMEOUT = int(os.environ.get('BURN_TIMEOUT', '7200'))  # 默认 2 小时
_STREAM_TIMEOUT = int(os.environ.get('STREAM_TIMEOUT', '1800'))  # 30 分钟
_HLS_TIMEOUT = int(os.environ.get('HLS_TIMEOUT', '1800'))  # 30 分钟
_HLS_MAX = int(os.environ.get('HLS_MAX_PROCESSES', '5'))  # 同时 HLS 进程上限

def _stop_preview_stream(stream_key):
    with _preview_streams_lock:
        proc = _preview_streams.pop(stream_key, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()

@app.route('/api/stop-preview/<filename>', methods=['POST'])
def stop_preview(filename):
    """停止指定视频当前的流式预览转码进程。"""
    _stop_preview_stream(filename)
    with _hls_processes_lock:
        proc = _hls_processes.pop(filename, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
    return jsonify({'success': True})

@app.route('/api/hls-start/<path:filename>', methods=['GET'])
def hls_start(filename):
    """启动从指定时间点开始的HLS预览转码，边转边播。"""
    try:
        import hashlib
        nas_path = os.environ.get('NAS_PATH', '/nas/videos')
        filepath = _safe_join_allow_subpath(nas_path, filename)
        if filepath is None:
            logger.warning(f"hls-start 拒绝非法路径: {filename!r}")
            return jsonify({'success': False, 'error': '非法路径'}), 400
        if not os.path.isfile(filepath):
            return jsonify({'success': False, 'error': '视频文件不存在'}), 404

        try:
            raw_start = float(request.args.get('start', '0') or '0')
        except ValueError:
            raw_start = 0.0
        # Bug #10 修复：clamp 到合理范围（0 ~ 24 小时）
        start_seconds = max(0.0, min(raw_start, 24 * 3600.0))

        cache_root = '/tmp/hls_cache'
        os.makedirs(cache_root, exist_ok=True)
        # 2026-06-22 优化：禁用 VAAPI/QSV 硬编
        #  - NAS Pentium Gold 8505 核显 UHD GT1 太弱，VAAPI scale_vaapi 不支持
        #  - QSV 需要 oneVPL GPU runtime (libmfx.so)，容器内没装
        #  - 实测：4 CPU libx264 ultrafast 已达 1.91x speed，VAAPI 硬编反而拖慢到 1.54x
        # 保留 VAAPI 代码分支（注释）作为未来硬件支持时启用
        use_vaapi = False  # 强制 libx264 软编
        use_qsv = False  # 同上
        hls_cache_version = 'ts-libx264-v9-30s'  # 2026-06-22 强制 libx264 软编
        session_id = hashlib.md5(f"{hls_cache_version}:{filename}:{os.path.getsize(filepath)}:{int(os.path.getmtime(filepath))}:{start_seconds:.3f}".encode('utf-8')).hexdigest()
        session_dir = os.path.join(cache_root, session_id)
        playlist_path = os.path.join(session_dir, 'index.m3u8')

        with _hls_processes_lock:
            existing = _hls_processes.get(filename)
        if os.path.exists(playlist_path):
            if existing is None or existing.poll() is None:
                return jsonify({
                    'success': True,
                    'status': 'running' if existing and existing.poll() is None else 'ready',
                    'playlist_url': f'/api/hls/{session_id}/index.m3u8',
                    'start': start_seconds
                })

        # One active HLS preview per source video keeps CPU under control.
        with _hls_processes_lock:
            old_proc = _hls_processes.pop(filename, None)
            # Bug #15 修复：超出 _HLS_MAX 时清理最旧的（HLS dict 不能无限增长）
            while len(_hls_processes) >= _HLS_MAX:
                if not _hls_processes:
                    break
                oldest_key = next(iter(_hls_processes))
                oldest_proc = _hls_processes.pop(oldest_key)
                if oldest_proc and oldest_proc.poll() is None:
                    logger.warning(f"HLS 进程数超限 ({_HLS_MAX})，kill 最旧: {oldest_key}")
                    try: oldest_proc.kill()
                    except Exception: pass
        if old_proc and old_proc.poll() is None:
            old_proc.terminate()
            try:
                old_proc.wait(timeout=3)
            except Exception:
                old_proc.kill()

        os.makedirs(session_dir, exist_ok=True)
        for old_name in os.listdir(session_dir):
            try:
                os.remove(os.path.join(session_dir, old_name))
            except OSError:
                pass

        seek_args = ['-ss', f'{start_seconds:.3f}'] if start_seconds > 0 else ['-ss', '0']
        segment_pattern = os.path.join(session_dir, 'seg_%05d.ts')
        cmd = ['ffmpeg', '-nostdin', '-y']
        if _probe_vaapi():
            # 2026-08-30：核显硬编已可用（驱动已就位 + 启动探针确认），HLS 边转边播切换 VAAPI，
            # CPU 占用大幅下降（软编 ultrafast 会吃满配额挤占 whisper 任务）
            cmd += ['-vaapi_device', '/dev/dri/renderD128']
        cmd += [*seek_args, '-i', filepath,
                '-map', '0:v:0', '-map', '0:a:0?', '-dn', '-sn', '-map_chapters', '-1']
        if _probe_vaapi():
            cmd += ['-vf', 'format=nv12,hwupload,scale_vaapi=960:-2',
                    '-c:v', 'h264_vaapi', '-b:v', '2M',
                    '-force_key_frames', 'expr:gte(t,n_forced*30)']
        else:
            cmd += ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '30',
                    '-profile:v', 'baseline', '-level', '3.1', '-pix_fmt', 'yuv420p',
                    '-vf', 'scale=960:-2', '-force_key_frames', 'expr:gte(t,n_forced*30)']
        cmd += ['-c:a', 'aac', '-b:a', '96k', '-ac', '2', '-ar', '48000',
                '-f', 'hls', '-hls_time', '30', '-hls_list_size', '0',
                '-hls_segment_type', 'mpegts',
                '-hls_playlist_type', 'event',
                '-hls_flags', 'independent_segments',
                '-hls_segment_filename', segment_pattern,
                playlist_path]

        logger.info(f"HLS预览开始 (libx264 软编): {filename} (start={start_seconds:.3f})")
        # Bug #14 修复：HLS ffmpeg 加超时（用线程包装实现）
        proc = sp.Popen(cmd, stdin=sp.DEVNULL, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        with _hls_processes_lock:
            _hls_processes[filename] = proc
        # 守护线程：超时后 kill HLS 进程
        def _hls_watchdog(p, lifetime):
            try:
                p.wait(timeout=lifetime)
            except sp.TimeoutExpired:
                logger.error(f"HLS 转码超时 (>{lifetime}s): {filename}")
                try: p.kill()
                except Exception: pass
        threading.Thread(target=_hls_watchdog, args=(proc, _HLS_TIMEOUT), daemon=True).start()

        return jsonify({
            'success': True,
            'status': 'started',
            'playlist_url': f'/api/hls/{session_id}/index.m3u8',
            'start': start_seconds
        })
    except Exception as e:
        logger.error(f"启动HLS预览失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/hls/<session_id>/<path:asset>', methods=['GET'])
def hls_asset(session_id, asset):
    """提供HLS playlist/segment文件。"""
    if not re.fullmatch(r'[a-f0-9]{32}', session_id):
        return jsonify({'success': False, 'error': '非法HLS会话'}), 400
    if '/' in asset or asset.startswith('.'):
        return jsonify({'success': False, 'error': '非法HLS资源'}), 400
    session_dir = os.path.join('/tmp/hls_cache', session_id)
    if asset.endswith('.m3u8'):
        mimetype = 'application/vnd.apple.mpegurl'
    elif asset.endswith('.ts'):
        mimetype = 'video/mp2t'
    else:
        mimetype = 'application/octet-stream'
    asset_path = os.path.join(session_dir, asset)
    if asset.endswith('.m3u8') and not os.path.exists(asset_path):
        return Response('', status=202, mimetype=mimetype)
    return send_from_directory(session_dir, asset, mimetype=mimetype, conditional=True)

def _is_stale_lock(lock_path, max_age_seconds=1800):
    """判断 lock 文件是否残留（创建/修改时间超过 max_age）。

    容器重启或进程崩溃时 lock 会留下，30 分钟前还没动的 lock 视为死锁。
    """
    try:
        mtime = os.path.getmtime(lock_path)
        return (time.time() - mtime) > max_age_seconds
    except OSError:
        return False

def _cleanup_stale_transcode_locks(max_age_seconds=1800):
    """遍历 /tmp/transcode_cache，清除残留死锁（>max_age 的 lock + 配套 progress/mp4）。

    Bug #5 修复：旧版 1 小时 timeout 错误留下的死锁通过此函数清理。
    """
    cache_dir = '/tmp/transcode_cache'
    if not os.path.isdir(cache_dir):
        return 0
    removed = 0
    try:
        for name in os.listdir(cache_dir):
            if not name.endswith('.lock'):
                continue
            lock_path = os.path.join(cache_dir, name)
            if not _is_stale_lock(lock_path, max_age_seconds):
                continue
            base = name[:-5]  # 去掉 .lock
            for ext in ('.lock', '.progress', '.mp4'):
                p = os.path.join(cache_dir, base + ext)
                try:
                    if os.path.exists(p):
                        os.remove(p)
                        removed += 1
                except OSError:
                    pass
            logger.warning(f"清理残留转码死锁: {base}")
    except OSError as e:
        logger.error(f"清理死锁失败: {e}")
    return removed

# 启动时清理一次残留死锁（容器重启时立即生效）
_startup_cleaned = _cleanup_stale_transcode_locks()
if _startup_cleaned:
    logger.info(f"启动时清理 {_startup_cleaned} 个残留转码死锁")
# Bug #19 修复：启动时也清一次 HLS 缓存大小上限
_enforce_hls_cache_size()

@app.route('/api/transcode-video/<path:filename>', methods=['GET'])
def transcode_video(filename):
    """转码为浏览器兼容的H.264 MP4(+faststart)；完成后缓存到磁盘。"""
    try:
        from flask import send_file, make_response
        import hashlib
        nas_path = os.environ.get('NAS_PATH', '/nas/videos')
        filepath = _safe_join_allow_subpath(nas_path, filename)
        if filepath is None:
            logger.warning(f"transcode-video 拒绝非法路径: {filename!r}")
            return jsonify({'success': False, 'error': '非法路径'}), 400
        if not os.path.isfile(filepath):
            return jsonify({'success': False, 'error': '视频文件不存在'}), 404

        try:
            start_seconds = max(0.0, min(float(request.args.get('start', '0') or '0'), 24 * 3600.0))
        except ValueError:
            start_seconds = 0.0
        clip_duration = (request.args.get('duration') or os.environ.get('PREVIEW_CLIP_SECONDS') or os.environ.get('PREVIEW_TRANSCODE_SECONDS', '300')).strip()
        if start_seconds > 0 and clip_duration in ('', '0', '-1', 'full'):
            clip_duration = '300'
        preview_mode = (request.args.get('mode') or 'h264').strip().lower()
        if preview_mode not in ('h264', 'remux'):
            preview_mode = 'h264'

        if request.args.get('stream') == '1':
            seek_args = ['-ss', f'{start_seconds:.3f}'] if start_seconds > 0 else ['-ss', '0']
            duration_args = []
            if clip_duration and clip_duration not in ('0', '-1', 'full'):
                duration_args = ['-t', clip_duration]
            vaapi_available = _probe_vaapi()
            if vaapi_available:
                cmd = ['ffmpeg', '-nostdin', '-y',
                       '-vaapi_device', '/dev/dri/renderD128',
                       *seek_args, *duration_args, '-i', filepath,
                       '-map', '0:v:0', '-map', '0:a:0?', '-dn', '-sn', '-map_chapters', '-1',
                       '-vf', 'format=nv12,hwupload,scale_vaapi=1280:-2',
                       '-c:v', 'h264_vaapi', '-b:v', '3M',
                       '-c:a', 'aac', '-b:a', '128k',
                       '-f', 'mp4', '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
                       '-loglevel', 'error', 'pipe:1']
            else:
                cmd = ['ffmpeg', '-nostdin', '-y',
                       *seek_args, *duration_args, '-i', filepath,
                       '-map', '0:v:0', '-map', '0:a:0?', '-dn', '-sn', '-map_chapters', '-1',
                       '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '26',
                       '-vf', 'scale=1280:-2',
                       '-c:a', 'aac', '-b:a', '128k',
                       '-f', 'mp4', '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
                       '-threads', '2', '-loglevel', 'error', 'pipe:1']

            def generate_stream():
                logger.info(f"流式预览开始: {filename} (start={start_seconds:.3f}, duration={clip_duration})")
                stream_key = filename
                _stop_preview_stream(stream_key)
                proc = sp.Popen(cmd, stdin=sp.DEVNULL, stdout=sp.PIPE, stderr=sp.DEVNULL, bufsize=0)
                with _preview_streams_lock:
                    _preview_streams[stream_key] = proc
                # Bug #14 修复：流式预览 ffmpeg watchdog（_STREAM_TIMEOUT 秒后自动 kill）
                def _stream_watchdog(p, lifetime):
                    try:
                        p.wait(timeout=lifetime)
                    except sp.TimeoutExpired:
                        logger.error(f"流式转码超时 (>{lifetime}s): {filename}")
                        try: p.kill()
                        except Exception: pass
                threading.Thread(target=_stream_watchdog, args=(proc, _STREAM_TIMEOUT), daemon=True).start()
                try:
                    while True:
                        chunk = proc.stdout.read(65536)
                        if not chunk:
                            break
                        yield chunk
                except (GeneratorExit, BrokenPipeError, ConnectionError):
                    logger.info(f"流式预览客户端断开: {filename} (start={start_seconds:.3f})")
                    raise
                finally:
                    try:
                        if proc.stdout:
                            proc.stdout.close()
                    except Exception:
                        pass
                    if proc.poll() is None:
                        proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                    with _preview_streams_lock:
                        if _preview_streams.get(stream_key) is proc:
                            _preview_streams.pop(stream_key, None)

            response = Response(generate_stream(), mimetype='video/mp4')
            response.headers['Cache-Control'] = 'no-store'
            return response

        cache_dir = '/tmp/transcode_cache'
        os.makedirs(cache_dir, exist_ok=True)
        try:
            source_stat = os.stat(filepath)
            source_sig = f"{source_stat.st_size}:{int(source_stat.st_mtime)}"
        except OSError:
            source_sig = 'unknown'
        # Bug #8 修复：早期路径也包含 source_sig，避免源文件变更时返回过期缓存
        cache_seed = f"v4:{filename}:{source_sig}:start={start_seconds:.3f}:duration={clip_duration or '0'}:mode={preview_mode}"
        cache_key = hashlib.md5(cache_seed.encode('utf-8')).hexdigest()
        output_path = os.path.join(cache_dir, f"{cache_key}.mp4")
        progress_path = os.path.join(cache_dir, f"{cache_key}.progress")
        lock_path = os.path.join(cache_dir, f"{cache_key}.lock")

        # Cache hit: serve completed file with Range support
        if os.path.exists(progress_path):
            with open(progress_path, 'r') as f:
                status = f.read().strip()
            if status == 'done' and os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
                response = make_response(send_file(output_path, mimetype='video/mp4', conditional=True))
                response.headers['Accept-Ranges'] = 'bytes'
                response.headers['Cache-Control'] = 'public, max-age=3600'
                return response

        # Already transcoding: return status for polling
        if os.path.exists(lock_path):
            # Bug #5 修复：lock 残留超过 30 分钟视为死锁，自动清理后开始新转码
            if _is_stale_lock(lock_path):
                logger.warning(f"检测到残留死锁 {lock_path}，强制清理后重新转码")
                for stale_ext in ('.lock', '.progress', '.mp4'):
                    p = lock_path[:-5] + stale_ext
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except OSError:
                        pass
            else:
                running_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
                return jsonify({
                    'status': 'running',
                    'size': running_size,
                    'poll_url': f'/api/transcode-progress/{filename}?start={start_seconds:.3f}&duration={clip_duration}&mode={preview_mode}'
                }), 202

        # Create lock atomically and start background transcode.
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(lock_fd, 'w') as f:
                f.write(str(os.getpid()))
        except FileExistsError:
            running_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
            return jsonify({
                'status': 'running',
                'size': running_size,
                'poll_url': f'/api/transcode-progress/{filename}?start={start_seconds:.3f}&duration={clip_duration}&mode={preview_mode}'
            }), 202
        with open(progress_path, 'w') as f: f.write('running')

        def _do_transcode():
            try:
                probe_cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                             '-show_entries', 'stream=codec_name', '-of',
                             'default=noprint_wrappers=1:nokey=1', filepath]
                probe = sp.run(probe_cmd, capture_output=True, text=True, timeout=30)
                video_codec = probe.stdout.strip().lower() if probe.returncode == 0 else ''
                seek_args = ['-ss', f'{start_seconds:.3f}'] if start_seconds > 0 else ['-ss', '0']
                duration_args = []
                if clip_duration and clip_duration not in ('0', '-1', 'full'):
                    duration_args = ['-t', clip_duration]
                if preview_mode == 'remux':
                    cmd = ['ffmpeg', '-y', *seek_args, *duration_args, '-i', filepath,
                           '-map', '0:v:0', '-map', '0:a:0?', '-dn', '-sn', '-map_chapters', '-1',
                           '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k',
                           '-movflags', '+faststart', output_path]
                elif video_codec == 'h264':
                    cmd = ['ffmpeg', '-y', *seek_args, *duration_args, '-i', filepath,
                           '-map', '0:v:0', '-map', '0:a:0?', '-dn', '-sn', '-map_chapters', '-1',
                           '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k',
                           '-movflags', '+faststart', output_path]
                else:
                    vaapi_available = _probe_vaapi()
                    if vaapi_available:
                        cmd = ['ffmpeg', '-y',
                               '-vaapi_device', '/dev/dri/renderD128',
                               *seek_args, *duration_args, '-i', filepath,
                               '-map', '0:v:0', '-map', '0:a:0?', '-dn', '-sn', '-map_chapters', '-1',
                               '-vf', 'format=nv12,hwupload,scale_vaapi=1280:-2',
                               '-c:v', 'h264_vaapi', '-b:v', '3M',
                               '-c:a', 'aac', '-b:a', '128k',
                               '-movflags', '+faststart', output_path]
                    else:
                        cmd = ['ffmpeg', '-y', *seek_args, *duration_args, '-i', filepath,
                               '-map', '0:v:0', '-map', '0:a:0?', '-dn', '-sn', '-map_chapters', '-1',
                               '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '26',
                               '-vf', 'scale=1280:-2', '-c:a', 'aac', '-b:a', '128k',
                               '-movflags', '+faststart', '-threads', '2', output_path]
                logger.info(f"转码开始: {filename} (mode={preview_mode}, codec={video_codec}, start={start_seconds:.3f}, duration={clip_duration})")
                # Bug #6 修复：ffmpeg 加 30 分钟硬超时；超时/失败时清理不完整 mp4，避免污染缓存
                _FFMPEG_TIMEOUT = int(os.environ.get('TRANSCODE_TIMEOUT', '1800'))
                try:
                    result = sp.run(cmd, stdout=sp.DEVNULL, stderr=sp.PIPE, timeout=_FFMPEG_TIMEOUT)
                    if result.returncode == 0:
                        with open(progress_path, 'w') as f: f.write('done')
                        logger.info(f"转码完成: {filename}")
                    else:
                        logger.error(f"转码失败 (rc={result.returncode}): {filename}, stderr: {result.stderr[-200:].decode(errors='replace') if result.stderr else ''}")
                        with open(progress_path, 'w') as f: f.write('error')
                        if os.path.exists(output_path):
                            try: os.remove(output_path)
                            except OSError: pass
                except sp.TimeoutExpired:
                    logger.error(f"转码超时 (>{_FFMPEG_TIMEOUT}s): {filename}")
                    with open(progress_path, 'w') as f: f.write('error')
                    if os.path.exists(output_path):
                        try: os.remove(output_path)
                        except OSError: pass
            except Exception as ex:
                logger.error(f"转码异常: {filename}, {ex}")
                with open(progress_path, 'w') as f: f.write('error')
                if os.path.exists(output_path):
                    try: os.remove(output_path)
                    except OSError: pass
            finally:
                if os.path.exists(lock_path):
                    try: os.remove(lock_path)
                    except OSError: pass

        threading.Thread(target=_do_transcode, daemon=True).start()
        return jsonify({'status': 'started', 'poll_url': f'/api/transcode-progress/{filename}?start={start_seconds:.3f}&duration={clip_duration}&mode={preview_mode}'}), 202
    except Exception as e:
        logger.error(f"转码请求失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/transcode-progress/<path:filename>', methods=['GET'])
def transcode_progress(filename):
    """查询转码进度"""
    import hashlib
    cache_dir = '/tmp/transcode_cache'
    nas_path = os.environ.get('NAS_PATH', '/nas/videos')
    # 修复：补 _safe_join 校验（原直接 os.path.join，与其余端点不一致）
    filepath = _safe_join_allow_subpath(nas_path, filename)
    if filepath is None:
        return jsonify({'status': 'not_started', 'size': 0, 'done': False}), 404
    try:
        start_seconds = max(0.0, float(request.args.get('start', '0') or '0'))
    except ValueError:
        start_seconds = 0.0
    clip_duration = (request.args.get('duration') or os.environ.get('PREVIEW_CLIP_SECONDS') or os.environ.get('PREVIEW_TRANSCODE_SECONDS', '300')).strip()
    if start_seconds > 0 and clip_duration in ('', '0', '-1', 'full'):
        clip_duration = '300'
    preview_mode = (request.args.get('mode') or 'h264').strip().lower()
    if preview_mode not in ('h264', 'remux'):
        preview_mode = 'h264'
    # 修复 BUG：删除旧的 md5(filename) 分支（与 transcode_video 的 v4 key 不一致，
    # 导致 start=0&duration=120&mode=h264 时进度永远查不到）。统一用 v4 key。
    try:
        source_stat = os.stat(filepath)
        source_sig = f"{source_stat.st_size}:{int(source_stat.st_mtime)}"
    except OSError:
        source_sig = 'unknown'
    cache_seed = f"v4:{filename}:{source_sig}:start={start_seconds:.3f}:duration={clip_duration or '0'}:mode={preview_mode}"
    cache_key = hashlib.md5(cache_seed.encode('utf-8')).hexdigest()
    output_path = os.path.join(cache_dir, f"{cache_key}.mp4")
    progress_path = os.path.join(cache_dir, f"{cache_key}.progress")
    status = 'not_started'
    if os.path.exists(progress_path):
        with open(progress_path, 'r') as f:
            status = f.read().strip()
    size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    return jsonify({'status': status, 'size': size, 'done': status == 'done'})

_VAAPI_AVAILABLE = None  # None=未探测；启动后缓存探测结果

def _probe_vaapi():
    """探测 VAAPI 核显硬编可用性（结果缓存，进程内只探测一次）。

    原实现只检查 /dev/dri/renderD128 设备文件是否存在——设备存在但驱动缺失时
    （如挂载的 intel-drivers 目录为空），硬编 ffmpeg 命令会失败并静默降级，
    用户看到的是"音频预览模式"而非真正的视频预览。改为启动时真实跑一次
    0.5s 的 h264_vaapi 编码探针。
    """
    global _VAAPI_AVAILABLE
    if _VAAPI_AVAILABLE is not None:
        return _VAAPI_AVAILABLE
    _VAAPI_AVAILABLE = False
    if os.path.exists('/dev/dri/renderD128'):
        try:
            r = sp.run(
                ['ffmpeg', '-hide_banner', '-loglevel', 'error',
                 '-vaapi_device', '/dev/dri/renderD128',
                 '-f', 'lavfi', '-i', 'testsrc=duration=0.5:size=320x240:rate=10',
                 '-vf', 'format=nv12,hwupload', '-c:v', 'h264_vaapi', '-f', 'null', '-'],
                stdout=sp.DEVNULL, stderr=sp.DEVNULL, timeout=15)
            _VAAPI_AVAILABLE = (r.returncode == 0)
        except Exception as e:
            logger.warning(f"VAAPI 探测异常: {e}")
    logger.info(f"VAAPI 核显硬编探测: {'可用' if _VAAPI_AVAILABLE else '不可用，预览转码使用 CPU 软编'}")
    return _VAAPI_AVAILABLE

def initialize():
    """应用初始化"""
    # 确保目录存在
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['SUBTITLE_FOLDER'], exist_ok=True)
    os.makedirs(app.config['AUDIO_FOLDER'], exist_ok=True)

    # 启动仅清理缓存，永久保留上传素材、字幕和配置
    try:
        deleted = cleanup_old_files(max_age_days=30)
        if deleted > 0:
            logger.info(f"启动时清理了 {deleted} 个旧文件")
    except Exception:
        pass

# 执行初始化
initialize()

def _janitor_loop():
    """常驻清理线程：收口内存态与磁盘缓存的无界增长。

    _cleanup_old_tasks 原是无人调用的死代码，processing_tasks/_last_broadcast
    随任务数无限增长；HLS 缓存此前只在清理按钮里触发。
    """
    while True:
        time.sleep(300)
        try:
            _cleanup_old_tasks(max_keep=100)
        except Exception as e:
            logger.warning(f"janitor: 清理旧任务状态失败: {e}")
        try:
            _enforce_hls_cache_size()
        except Exception as e:
            logger.warning(f"janitor: HLS 缓存超限清理失败: {e}")

threading.Thread(target=_janitor_loop, daemon=True, name='janitor').start()

def _prewarm_model():
    """后台预热 whisper 模型（2026-08-30）：重启后第一个任务省去模型加载等待。"""
    try:
        if os.getenv('ASR_PREWARM','1')=='0':return
        model_size = os.environ.get('MODEL_SIZE', 'tiny')
        global subtitle_generator
        generator = SubtitleGenerator(model_size=model_size, device=os.getenv('USE_GPU', 'auto'))
        generator.load_model()
        subtitle_generator = generator
        logger.info(f'模型预热完成: {model_size}')
    except Exception as e:
        logger.warning(f'模型预热失败（首个任务时将重新加载）: {e}')

threading.Thread(target=_prewarm_model, daemon=True, name='model-prewarm').start()

# ============================================================================
# 2026-06-22 Phase B + C1 + C3 + D1: TaskQueue 集成 + 队列路由 + SSE
# ============================================================================
_task_queue = None
_task_queue_lock = threading.Lock()

def _get_task_queue():
    """懒加载 TaskQueue（线程安全单例）"""
    global _task_queue
    # 修复并发 bug：原无锁，waitress 多线程下首次并发请求会各自创建 TaskQueue，
    # 启动多套 worker（4×N），导致 DB 锁竞争和任务重复处理。加锁保证单例。
    if _task_queue is None:
        with _task_queue_lock:
            if _task_queue is None:  # double-check
                worker_count = max(1, min(8, int(os.environ.get('TASK_WORKER_COUNT', '2'))))
                default_task_db = '/app/data/tasks.db' if os.path.isdir('/app') else os.path.join(_base_dir, '..', 'data', 'tasks.db')
                task_db_path = os.environ.get('TASK_DB_PATH', default_task_db)
                os.makedirs(os.path.dirname(task_db_path), exist_ok=True)
                _task_queue = TaskQueue(task_db_path, worker_count=worker_count)
                _register_handlers(_task_queue)
                _task_queue.start()
    return _task_queue

def _register_handlers(queue):
    """注册 task type handlers"""
    def handle_whisper(task_id, payload, q):
        """whisper 转录 handler"""
        task_row = q.get(task_id)
        if not task_row:
            q.fail(task_id, '任务不存在')
            return
        video_path = task_row.video_path
        model = payload.get('model', 'tiny')
        language = payload.get('language', 'auto')
        output_format = payload.get('format', 'srt')
        translate_to = payload.get('translate_to')
        translate_engine = payload.get('translate_engine', 'auto')
        diarize = bool(payload.get('diarize', False))
        num_speakers = int(payload.get('num_speakers') or 0)
        save_to_source = payload.get('save_to_source_dir', False)
        save_strategy = payload.get('save_strategy') or ('source' if save_to_source else 'subtitles')
        output_naming = payload.get('output_naming', 'plain')
        bilingual = bool(payload.get('bilingual', False))
        split_max_chars = int(payload.get('split_max_chars') or 0)
        glossary = payload.get('glossary') or {}

        with processing_tasks_lock:
            if task_id in processing_tasks:
                processing_tasks[task_id]['status'] = 'processing'
        try:
            # 进度回调节流（2026-08-30 速度优化）：原实现每个转录片段都做一次
            # is_cancel_requested（DB 读）+ update_progress（DB 写），长视频数千片段时
            # 数千次 SQLite 开销。节流到 0.5s 一次（进度跳变 >=5% 或 100% 或回退时立即处理），
            # 前端本身 3s 轮询/SSE 也是 0.5s 节流，观感无差异。
            _cb_state = {'t': 0.0, 'pct': -1.0}

            def cb(pct, info):
                now = time.monotonic()
                if (now - _cb_state['t'] >= 0.5 or pct >= 100
                        or pct - _cb_state['pct'] >= 5 or pct < _cb_state['pct']):
                    _cb_state['t'] = now
                    _cb_state['pct'] = pct
                    if q.is_cancel_requested(task_id):
                        raise Exception('任务已被取消')
                    q.update_progress(task_id, pct, info)
                    with processing_tasks_lock:
                        if task_id in processing_tasks:
                            processing_tasks[task_id]['progress'] = pct
                            processing_tasks[task_id]['progress_info'] = info

            temp_generator = SubtitleGenerator(
                model_size=model, device=os.getenv('USE_GPU', 'auto'), progress_callback=cb
            )
            temp_generator.load_model()

            global subtitle_generator
            subtitle_generator = temp_generator

            output_dir = os.path.dirname(video_path) if save_strategy == 'source' else app.config['SUBTITLE_FOLDER']
            os.makedirs(output_dir, exist_ok=True)

            cancel_event = None
            with processing_tasks_lock:
                if task_id in processing_tasks:
                    cancel_event = processing_tasks[task_id].get('_cancel_event')

            result = temp_generator.generate_subtitle(
                video_path, output_dir,
                language=language, format=output_format,
                translate_to=translate_to, cancel_event=cancel_event,
                output_naming=output_naming,
                bilingual=bilingual,
                split_max_chars=split_max_chars,
                glossary=glossary,
                translate_engine=translate_engine,
                diarize=diarize,
                num_speakers=num_speakers,
            )

            with processing_tasks_lock:
                if task_id in processing_tasks:
                    processing_tasks[task_id]['status'] = 'completed' if result.get('success') else 'failed'
                    processing_tasks[task_id]['result'] = result
                    processing_tasks[task_id]['end_time'] = datetime.now().isoformat()

            if result.get('success'):
                q.complete(task_id, result)
                _append_history_item(_build_history_item(task_id, payload, task_row, result, 'completed'))
                _send_webhook_notification('字幕生成完成', payload.get('filename') or os.path.basename(video_path), {
                    'task_id': task_id,
                    'status': 'completed',
                    'output_files': result.get('output_files', {}),
                })
            elif result.get('error') in ('任务已被取消', '任务已被用户取消') or q.is_cancel_requested(task_id):
                # 用户主动取消：标记为 cancelled（原实现落到 fail，前端显示"失败"且可被误重试）
                q.cancel(task_id)
                _append_history_item(_build_history_item(task_id, payload, task_row, result, 'cancelled'))
            else:
                q.fail(task_id, result.get('error', '字幕生成失败'))
                _append_history_item(_build_history_item(task_id, payload, task_row, result, 'failed'))
                _send_webhook_notification('字幕生成失败', result.get('error', '字幕生成失败'), {
                    'task_id': task_id,
                    'status': 'failed',
                    'filename': payload.get('filename') or os.path.basename(video_path),
                })
        except Exception as e:
            if str(e) == '任务已被取消':
                q.cancel(task_id)
            else:
                logger.exception(f"whisper task {task_id} 失败")
                q.fail(task_id, str(e))
                try:
                    _append_history_item(_build_history_item(task_id, payload, task_row, {'success': False, 'error': str(e)}, 'failed'))
                    _send_webhook_notification('字幕生成异常', str(e), {
                        'task_id': task_id,
                        'status': 'failed',
                        'filename': payload.get('filename') or os.path.basename(video_path),
                    })
                except Exception:
                    pass

    def handle_extract_subtitle(task_id, payload, q):
        """抽字幕 handler (text + ocr mode)"""
        task_row = q.get(task_id)
        if not task_row:
            q.fail(task_id, '任务不存在')
            return
        video_path = task_row.video_path
        stream_index = payload.get('stream_index', 2)
        mode = payload.get('mode', 'text')
        ocr_lang = payload.get('ocr_lang', 'chi_sim+eng')
        save_to_source = payload.get('save_to_source_dir', False)

        if not video_path or not os.path.isfile(video_path):
            q.fail(task_id, f'视频不存在: {video_path}')
            return

        video_name = os.path.splitext(os.path.basename(video_path))[0]
        if save_to_source:
            out_dir = os.path.dirname(video_path)
        else:
            out_dir = app.config['SUBTITLE_FOLDER']
        os.makedirs(out_dir, exist_ok=True)
        try:
            output_stem, reservation = reserve_stem(out_dir, video_name)
        except OSError as e:
            if e.errno not in (13, 30):
                raise
            out_dir = app.config['SUBTITLE_FOLDER']
            output_stem, reservation = reserve_stem(out_dir, video_name)
        output_path = os.path.join(out_dir, output_stem + ".srt")

        def progress_cb(pct, info):
            if q.is_cancel_requested(task_id):
                raise Exception('任务已被取消')
            q.update_progress(task_id, pct, info)

        cancel_event = None
        with processing_tasks_lock:
            if task_id in processing_tasks:
                cancel_event = processing_tasks[task_id].get('_cancel_event')

        try:
            if mode == 'ocr':
                from utils.subtitle_extractor import extract_image_subtitle_with_ocr
                result = extract_image_subtitle_with_ocr(
                    video_path, stream_index, output_path,
                    ocr_lang=ocr_lang,
                    progress_callback=progress_cb,
                    cancel_event=cancel_event,
                )
            else:
                # 修复：原调用 _extract_subtitle_stream 是未定义名字（NameError），
                # 「提取内嵌字幕」整链路坏死的一半根因（另一半是路由缺失）。
                from utils.subtitle_extractor import extract_subtitle_stream
                result = extract_subtitle_stream(
                    video_path, stream_index, output_path,
                    progress_callback=progress_cb,
                    cancel_event=cancel_event,
                )

            if result.get('success'):
                write_metadata(output_path, video_path)
        finally:
            release_reservation(reservation)

        with processing_tasks_lock:
            if task_id in processing_tasks:
                processing_tasks[task_id]['status'] = 'completed' if result.get('success') else 'failed'
                processing_tasks[task_id]['result'] = result
                processing_tasks[task_id]['end_time'] = datetime.now().isoformat()

        if result.get('success'):
            q.complete(task_id, result)
        else:
            q.fail(task_id, result.get('error', '字幕提取失败'))

    queue.register_handler('whisper', handle_whisper)
    queue.register_handler('extract_subtitle', handle_extract_subtitle)
    logger.info('TaskQueue handlers 注册完成 (whisper, extract_subtitle)')


# Phase B: 5 个队列路由
@app.route('/api/queue/add', methods=['POST'])
def queue_add():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': '缺少 body'}), 400
        task_type = data.get('type')
        video_path = data.get('video_path')
        payload = data.get('payload', {})
        priority = int(data.get('priority', 0))
        if task_type not in ('whisper', 'extract_subtitle'):
            return jsonify(success=False, error='不支持的任务类型'), 400
        if not isinstance(payload, dict):
            return jsonify(success=False, error='payload 必须为对象'), 400
        if not task_type or not video_path:
            return jsonify({'success': False, 'error': '缺少 type / video_path'}), 400
        nas_path = os.environ.get('NAS_PATH', '/nas/videos')
        upload_dir = app.config['UPLOAD_FOLDER']
        video_path_abs = os.path.realpath(video_path)
        if not (video_path_abs.startswith(os.path.realpath(nas_path) + os.sep)
                or video_path_abs.startswith(os.path.realpath(upload_dir) + os.sep)
                or video_path_abs.startswith(os.path.realpath(app.config['AUDIO_FOLDER']) + os.sep)):
            return jsonify({'success': False, 'error': 'video_path 不在允许目录内'}), 400
        if not os.path.isfile(video_path_abs):
            return jsonify({'success': False, 'error': '视频文件不存在'}), 400
        if os.path.splitext(video_path_abs)[1].lower() not in MEDIA_EXTENSIONS:
            return jsonify({'success': False, 'error': '不支持的媒体格式'}), 400
        q = _get_task_queue()
        task_id = q.add(task_type, video_path_abs, payload, priority=priority)
        return jsonify({'success': True, 'task_id': task_id, 'priority': priority, 'message': '任务已入队'})
    except Exception as e:
        logger.error(f"queue_add 失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/queue/list', methods=['GET'])
def queue_list():
    try:
        q = _get_task_queue()
        limit = min(int(request.args.get('limit', 50)), 200)
        status = request.args.get('status')
        tasks = q.list_tasks(status=status, limit=limit)
        return jsonify({'success': True, 'tasks': [t.to_dict() for t in tasks], 'stats': q.get_stats()})
    except Exception as e:
        logger.error(f"queue_list 失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/queue/<task_id>', methods=['GET'])
def queue_get(task_id):
    try:
        q = _get_task_queue()
        t = q.get(task_id)
        if not t:
            return jsonify({'success': False, 'error': '任务不存在'}), 404
        return jsonify({'success': True, 'task': t.to_dict()})
    except Exception as e:
        logger.error(f"queue_get 失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/queue/<task_id>/cancel', methods=['POST'])
def queue_cancel(task_id):
    try:
        q = _get_task_queue()
        t = q.get(task_id)
        ok = q.cancel(task_id)
        if not ok:
            return jsonify({'success': False, 'message': '任务不存在或不可取消'})
        # 运行中的任务只是被标记取消请求，真正停止要等 handler 检查点响应；
        # 文案如实区分，避免用户以为已立即停止（原"已取消"有误导）。
        if t is not None and t.to_dict().get('status') == 'processing':
            return jsonify({'success': True, 'status': 'cancelling', 'message': '已请求取消，正在运行的任务将在数秒内停止'})
        return jsonify({'success': True, 'status': 'cancelled', 'message': '已取消'})
    except Exception as e:
        logger.error(f"queue_cancel 失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/queue/<task_id>/retry', methods=['POST'])
def queue_retry(task_id):
    try:
        q = _get_task_queue()
        ok = q.retry(task_id)
        return jsonify({'success': ok, 'message': '已重新入队' if ok else '任务不存在或不可重试'})
    except Exception as e:
        logger.error(f"queue_retry 失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/queue/<task_id>', methods=['DELETE'])
def queue_delete(task_id):
    try:
        q = _get_task_queue()
        ok = q.delete(task_id)
        return jsonify({'success': ok, 'message': '已删除' if ok else '任务不存在或不允许删除'})
    except Exception as e:
        logger.error(f"queue_delete 失败: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# C3 SSE 端点
@app.route('/api/queue/stream')
def queue_stream():
    import queue as _queue
    def gen():
        q = _get_task_queue()
        # 注意：SSE 帧分隔必须是真实换行符。历史补丁曾写成 '\\n'（字面反斜杠+n），
        # 字节级无 0x0a，EventSource 永远收不到事件，全靠前端 3s 轮询兜底。
        yield 'event: connected\ndata: {"ok": true}\n\n'
        sub_queue = q.subscribe()
        try:
            while True:
                try:
                    event = sub_queue.get(timeout=30.0)
                except _queue.Empty:
                    yield ': heartbeat\n\n'
                    continue
                evt_name = event.get('event', 'message')
                import json as _json
                data_str = _json.dumps(event, ensure_ascii=False)
                yield f'event: {evt_name}\ndata: {data_str}\n\n'
        except GeneratorExit:
            pass
        finally:
            q.unsubscribe(sub_queue)
    # C3 fix: 去掉 'Connection' header（waitress 拒绝 hop-by-hop）
    return Response(gen(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
    })


# 监控自动字幕线程（v1.2.0）：仅当配置启用时启动
try:
    _start_watch_thread()
except Exception as _we:
    logger.error(f"监控线程启动失败: {_we}")

if __name__ == '__main__':
    from waitress import serve
    # 启动应用
    serve(app, host="0.0.0.0", port=5000)
