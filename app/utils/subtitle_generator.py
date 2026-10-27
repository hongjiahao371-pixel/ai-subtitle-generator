from utils.ai_memory import exclusive_ai_phase, local_memory_mode, wait_for_local_sleep, release_asr_weights
from utils.translation_rules import automatic_terms, protect_names, reference_prompt, translation_issues, TranslationValidationError, strip_implied_dimensions
from utils.artifacts import (reserve_stem, release_reservation, write_metadata, atomic_json, layout_segments, bilingual_segments, semantic_segments, wrap_display, readable_monolingual_segments)
import os
import re
import logging
from datetime import datetime
import json
import io
import sys
import threading
import time

logger = logging.getLogger(__name__)

# ============================================================================
# LLM 翻译引擎（2026-10-04）
# 本地 opus-mt 只有十来个语言对且质量一般；主流字幕工具（剪映/飞书妙记/HappyScribe）
# 都接了更强的机器翻译。这里提供可选的 OpenAI 兼容 LLM 翻译（base_url/api_key/model
# 存 data/llm.json），启用后任意语言对都能翻，未启用时自动回退本地模型。
# ============================================================================

_LLM_LANG_NAMES = {
    'zh': '中文', 'en': '英语', 'ja': '日语', 'ko': '韩语', 'fr': '法语',
    'de': '德语', 'es': '西班牙语', 'ru': '俄语', 'it': '意大利语', 'pt': '葡萄牙语',
}


# ============================================================================
# 幻听字幕清洗（2026-10-26）：Whisper 对 BGM/静音/尾奏段的已知幻听输出黑名单。
# 只做「整句精确匹配」（归一化大小写/空白/首尾标点后），不做子串匹配，避免误杀真实台词。
# 可用 ASR_SCRUB_HALLUCINATIONS=0 关闭。
# ============================================================================
_HALLUCINATION_TEXTS = frozenset(x.strip() for x in """
字幕由Amara.org社区提供
字幕由Amara.org
字幕由社区提供
由Amara.org社区提供
请不客气地观看
请不吝点赞订阅转发打赏支持明德与普法
谢谢观看
感谢收看
字幕
字幕组
Subtitles by the Amara.org community
Subtitles by Amara.org
Subtitle by Amara.org
Subtitles by the Amara Community
Thank you for watching
Thanks for watching!
Thanks for watching
Please subscribe to my channel
Please subscribe
Subscribe to my channel
ご視聴ありがとうございました
おしまい
最後までご視聴いただきありがとうございます
MBC 뉴스
KBS 뉴스
""".split("\n") if x.strip())
def _hallucination_key(text):
    """归一化：去所有空白与标点、小写，用于整句精确比对。"""
    t = re.sub(r'[\W]+', '', text or '', flags=re.UNICODE)
    return t.lower()


_HALLUCINATION_KEYS = frozenset(_hallucination_key(t) for t in _HALLUCINATION_TEXTS)


def scrub_hallucination_segments(segments):
    """剔除整句命中幻听黑名单的片段；返回 (新列表, 剔除数)。"""
    kept, dropped = [], 0
    for seg in segments:
        text = (seg.get('text') or seg.get('words_text') or '')
        key = _hallucination_key(text)
        if key and key in _HALLUCINATION_KEYS:
            dropped += 1
            continue
        kept.append(seg)
    return kept, dropped



# ============================================================================
# 云端 ASR 引擎（2026-10-27，v1.2.0）：可选的 OpenAI 兼容 /audio/transcriptions。
# 配置 data/asr.json {enabled, base_url, api_key, model}；启用后转写优先走云端，
# 失败自动回退本地 Whisper。说话人分离任务不适用（云端无分离能力，自动跳过云端）。
# ============================================================================
def _cloud_asr_config_path():
    custom = os.environ.get('ASR_CONFIG_PATH')
    if custom:
        return custom
    if os.path.isdir('/app/data'):
        return '/app/data/asr.json'
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'data', 'asr.json')


def _load_cloud_asr_config():
    try:
        with open(_cloud_asr_config_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get('enabled'):
            if data.get('base_url') and data.get('model'):
                return data
    except Exception:
        pass
    return {}


_LANG_NAME_TO_CODE = {
    'chinese': 'zh', 'mandarin': 'zh', 'english': 'en', 'japanese': 'ja', 'japanese language': 'ja',
    'korean': 'ko', 'french': 'fr', 'german': 'de', 'spanish': 'es', 'russian': 'ru',
    'italian': 'it', 'portuguese': 'pt',
}


def _cloud_transcribe(media_path, cfg, language, progress_callback=None):
    """调用 OpenAI 兼容 /audio/transcriptions（verbose_json），返回 (segments, info)。

    音频先抽成 16k 单声道 mp3 控制上传体积；视频文件同样适用。
    抛任何异常由调用方回退本地。
    """
    import subprocess as _sp
    import tempfile as _tempfile
    import urllib.request as _urllib
    import uuid as _uuid
    from types import SimpleNamespace as _NS

    base = str(cfg['base_url']).rstrip('/')
    url = base + '/audio/transcriptions'
    if progress_callback:
        progress_callback(12, '云端 ASR：抽取音频...')
    fd, tmp_audio = _tempfile.mkstemp(prefix='cloudasr-', suffix='.mp3')
    os.close(fd)
    try:
        r = _sp.run(['ffmpeg', '-nostdin', '-y', '-i', media_path,
                     '-vn', '-ac', '1', '-ar', '16000', '-b:a', '48k', tmp_audio],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=3600)
        if r.returncode != 0 or os.path.getsize(tmp_audio) < 1024:
            raise RuntimeError('云端 ASR 音频抽取失败')
        if progress_callback:
            progress_callback(15, '云端 ASR：上传转写中...')
        boundary = '----aisub' + _uuid.uuid4().hex
        body = io.BytesIO()
        def part(name, value):
            body.write(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                        % (boundary, name, value)).encode('utf-8'))
        part('model', str(cfg['model']))
        part('response_format', 'verbose_json')
        if language and language != 'auto':
            part('language', language)
        fname = os.path.basename(tmp_audio)
        body.write(('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                    'Content-Type: audio/mpeg\r\n\r\n' % (boundary, fname)).encode('utf-8'))
        with open(tmp_audio, 'rb') as f:
            body.write(f.read())
        body.write(('\r\n--%s--\r\n' % boundary).encode('utf-8'))
        payload = body.getvalue()
        req = _urllib.Request(url, data=payload, method='POST', headers={
            'Content-Type': 'multipart/form-data; boundary=' + boundary,
            'Authorization': 'Bearer ' + str(cfg.get('api_key') or ''),
        })
        with _urllib.urlopen(req, timeout=7200) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        segs = []
        for s in data.get('segments') or []:
            segs.append({'start': float(s.get('start', 0)), 'end': float(s.get('end', 0)),
                         'text': (s.get('text') or '').strip()})
        if not segs and data.get('text'):
            segs = [{'start': 0.0, 'end': float(data.get('duration') or 0), 'text': data['text'].strip()}]
        if not segs:
            raise RuntimeError('云端 ASR 返回空转写结果')
        lang = (data.get('language') or language or 'auto').lower()
        lang = _LANG_NAME_TO_CODE.get(lang, lang)
        if progress_callback:
            progress_callback(18, '云端 ASR 完成')
        return segs, _NS(language=lang, duration=float(data.get('duration') or 0))
    finally:
        try:
            os.remove(tmp_audio)
        except OSError:
            pass


def _llm_config_path():
    custom = os.environ.get('LLM_CONFIG_PATH')
    if custom:
        return custom
    if os.path.isdir('/app/data'):
        return '/app/data/llm.json'
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'data', 'llm.json')

def load_llm_config():
    try:
        with open(_llm_config_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}

def save_llm_config(cfg):
    path = _llm_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def llm_translate_ready(cfg=None):
    cfg = cfg or load_llm_config()
    return bool(cfg.get('enabled') and cfg.get('base_url') and cfg.get('api_key') and cfg.get('model'))

def llm_chat(cfg, messages, timeout=300, max_tokens=4000):
    """调用 OpenAI 兼容 /chat/completions，返回文本。"""
    import urllib.request
    base = str(cfg['base_url']).rstrip('/')
    payload = json.dumps({
        'model': cfg['model'],
        'messages': messages,
        'temperature': 0.0,
        'stream': False,
        'max_tokens': max_tokens,
        **({'chat_template_kwargs':cfg['chat_template_kwargs']} if cfg.get('chat_template_kwargs') else {}),
        **({'response_format':cfg['response_format']} if cfg.get('response_format') else {}),
    }, ensure_ascii=False).encode('utf-8')
    cache_path=None
    if local_memory_mode(cfg):
        import hashlib
        identity=os.environ.get('SUBTITLE_LOCAL_MODELS_ID','')
        key=hashlib.sha256((base+identity).encode()+payload).hexdigest()
        folder=os.path.join(os.environ.get('LLM_CONFIG_DIR','/app/data'),'llm-response-cache')
        os.makedirs(folder,mode=0o700,exist_ok=True);cache_path=os.path.join(folder,key+'.json')
        try:
            with open(cache_path,encoding='utf-8') as cached:return json.load(cached)['text']
        except (OSError,ValueError,KeyError):pass
    req = urllib.request.Request(
        base + '/chat/completions', data=payload,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + str(cfg['api_key'])},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    choice = (data.get('choices') or [{}])[0]
    if choice.get('finish_reason') == 'length':
        raise RuntimeError('翻译响应被长度限制截断，请减少批量后重试')
    content = choice.get('message', {}).get('content', '')
    # 推理模型可能带 <think> 块，剥掉
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.S).strip()
    if cache_path and content:atomic_json(cache_path,dict(text=content,model=cfg['model']))
    return content

def _subtitle_character_limits(segments, media_duration=None):
    limits=[]
    for i,seg in enumerate(segments):
        cap=segments[i+1]['start']-.08 if i+1<len(segments) else (media_duration or seg['end'])
        # Include real available silence, and reserve the same gap used by the
        # final bilingual layout. A fixed floor of 32 made short cues too fast.
        duration=max(.1,float(cap)-float(seg['start']))
        limits.append(min(84,max(18,int(duration*18))))
    return limits


def _compact_english_caption(text):
    # Conservative, literal equivalents. These do not guess an ASR correction
    # or replace a proper name, and retain collaboration and arm count.
    rules=[
        (r'\bartificial intelligence\b','AI'),
        (r'\bhas the ability to\b','can'),
        (r'\bhas the capability to\b','can'),
        (r'\babout to make its appearance\b','coming onstage'),
        (r'\btechnological research and development\b','R&D'),
        (r'\bindustrial robot market\b','industrial robot market'),
        (r'\baccounting for\b','with'),
        (r'\bwas developed as a result\b','was developed'),
        (r'\bat a level where it can\b','able to'),
        (r'\btwo[- ]armed collaborative robotic arm\b','dual-arm collaborative robot'),
        (r'\bdual[- ]arm collaborative robotic arm\b','dual-arm collaborative robot'),
        (r'\bChinese technology\b','Chinese tech'),
        (r'\bcore technologies?\b','core tech'),
        (r'\bmanufacturing processes?\b','manufacturing'),
        (r'\bthree feet of sword\b','a three-foot sword'),
        (r'\bas the need arose\b','when needed'),
        (r'\bafter ten years of effort\b','After a decade of work'),
        (r'\btechnological monopoly\b','tech monopoly'),
        (r'\btechnological monopolies\b','tech monopolies'),
    ]
    for pattern,replacement in rules:text=re.sub(pattern,replacement,text,flags=re.I)
    from utils.translation_rules import normalize_english_articles
    return normalize_english_articles(text)


def _safe_subtitle_rewrite(original, rewrite, source_text=None):
    # A shorter result is not automatically a faithful result. Reject obvious
    # changes in numbers, polarity, comparison direction and anchored names.
    original=original.replace('’',"'");rewrite=rewrite.replace('’',"'")
    if not rewrite or len(rewrite)>=len(original) or re.search(r'[\u3400-\u9fff]',rewrite):return False
    def numbers(t):
        small={'zero':'0','one':'1','two':'2','three':'3','four':'4','five':'5','six':'6','seven':'7','eight':'8','nine':'9','ten':'10'}
        t=re.sub(r'\bone (?=needs|must|can|has|is|should)\b','',t,flags=re.I)
        t=re.sub(r'\b(?:a|one)[ -]+hundred\b','100',t,flags=re.I)
        t=re.sub(r'\b(?:a|one)[ -]+thousand\b','1000',t,flags=re.I)
        t=re.sub(r'\bhundreds?\b','100',t,flags=re.I)
        t=re.sub(r'\bthousands?\b','1000',t,flags=re.I)
        t=re.sub(r'\bone[- ]third\b','1/3',t,flags=re.I)
        t=re.sub(r'\b(\d+(?:\.\d+)?)k\b',lambda m:str(int(float(m.group(1))*1000)),t,flags=re.I)
        t=re.sub(r'\b(?:'+'|'.join(small)+r')\b',lambda m:small[m.group().lower()],t,flags=re.I)
        if source_text is not None:t=strip_implied_dimensions(source_text,t)
        values=[n.replace(',','') for n in re.findall(r'\d+(?:[.,]\d+)*',t)]
        if source_text and '各一' in source_text:values=[n for i,n in enumerate(values) if n!='1' or i==0 or values[i-1]!='1']
        return values
    if numbers(original)!=numbers(rewrite):return False
    negative=r"\b(?:not|no|without|cannot|never|wrong|incorrect|mistaken|inaccurate|imprecise|inexact|impossible|unable|useless|unnecessary)\b|n't\b"
    polarity_original,polarity_rewrite=original,rewrite
    if source_text and re.search(r'没关系|没问题',source_text):
        polite=r'\b(?:no (?:problems?|worries|issues?)|not a problem)\b'
        polarity_original=re.sub(polite,'',polarity_original,flags=re.I)
        polarity_rewrite=re.sub(polite,'',polarity_rewrite,flags=re.I)
    if (source_text and re.search(r'时间|时钟|钟表',source_text)) or re.search(r'\b(?:clock|time)\b',original,re.I):negative+=r'|\boff\b'
    if bool(re.search(negative,re.sub(r'\bnot only\b','',polarity_original,flags=re.I),re.I))!=bool(re.search(negative,re.sub(r'\bnot only\b','',polarity_rewrite,flags=re.I),re.I)):return False
    role_source=re.sub(r'(?:我跟你[讲说]|我告诉你|你知道吗|你看)[,，。!?！？ ]*','',source_text or '')
    for pronoun in ('I','we','you','he','she','it','they'):
        if source_text is not None and not re.search({'I':'我(?!们)','we':'我们','you':'你','he':'他(?!们)','she':'她(?!们)','it':'它(?!们)','they':'[他她它]们'}[pronoun],role_source):continue
        forms={'I':'I|my|me','we':'we|our|us','you':'you|your','he':'he|his|him','she':'she|her','it':'it|its','they':'they|their|them'}[pronoun]
        pattern=r'\b(?:'+forms+r')\b'
        if re.search(pattern,original,re.I) and not re.search(pattern,rewrite,re.I):return False
    future=r"\bwill\b|['’]ll\b|\bgoing to\b"
    if bool(re.search(future,original,re.I))!=bool(re.search(future,rewrite,re.I)):return False
    for group in [r'\b(largest|biggest|greatest|maximum)\b',r'\b(highest|best|top|leading|most advanced)\b',r'\b(surpass|exceed|outperform|overtake|outstrip)\w*\b',r'\b(alongside|together|collaborat)\w*\b']:
        if re.search(group,original,re.I) and not re.search(group,rewrite,re.I):return False
    for name in re.findall(r'\b[A-Z][a-z]+(?: [A-Z][a-z]+)+\b',original):
        if name in ('North Pole','South Pole') and re.search(r'\b'+name.split()[0]+r'\b',rewrite,re.I):continue
        if name.casefold() not in rewrite.casefold():return False
    return True


@exclusive_ai_phase
def llm_translate_texts(texts, source_lang, target_lang, progress_callback=None, cancel_event=None, subtitle_limits=None, glossary=None, audit=None):
    """批量翻译字符串列表（LLM），返回与输入等长且顺序一致的译文列表。"""
    cfg = load_llm_config()
    from utils.subtitle_editor import editor_config,refine_captions
    editor=editor_config(cfg) if subtitle_limits else None
    if editor and local_memory_mode(cfg):wait_for_local_sleep(editor,include_editor=False)
    if not llm_translate_ready(cfg):
        raise RuntimeError('LLM 翻译未配置完整（需要在「工具中心 → AI 翻译引擎」填写并启用）')
    if local_memory_mode(cfg):
        with SubtitleGenerator._cache_lock:release_asr_weights(SubtitleGenerator._model_cache)
    src = _LLM_LANG_NAMES.get(source_lang, source_lang or '原语言')
    dst = _LLM_LANG_NAMES.get(target_lang, target_lang)
    if str(cfg.get('model','')).upper().startswith('HY-MT2'):
        # One complete phrase per request: there is no cross-row mapping for a
        # model to merge, omit or reorder. Use the official translation prompt.
        results=[]
        terms=automatic_terms(texts,glossary) if source_lang=='zh' and target_lang=='en' else {}
        for index,text in enumerate(texts):
            if cancel_event and cancel_event.is_set():raise RuntimeError('任务已被取消')
            prepared=protect_names(text,terms)
            # First translate faithfully without a length budget. A length-first
            # prompt caused the initial translation itself to omit meaning.
            prompt=f'将以下文本翻译为{dst}，注意只需要输出翻译后的结果，不要额外解释：\n\n{prepared}'
            refs=reference_prompt(text,terms)
            if refs:prompt='参考下面的翻译：\n'+refs+'\n\n'+prompt
            if source_lang=='zh' and target_lang=='en':
                limit=subtitle_limits[index] if subtitle_limits else 84
                context='\n'.join(texts[max(0,index-3):index])[-600:]
                if context and (re.match(r'^(?:[他她它这那我你与在]|也|而|是|占|帮|还有|还能|打破|挑战|有能|所以|因为|其实|刚才|目前|平面|空间|今天[他她它])',text)):
                    prompt=('背景信息（只供理解当前句的主语和语境，不是当前要翻译的内容）：\n'+context+'\n'+
                            '参考上面的信息，'+prompt.replace('将以下文本翻译为','把下面的文本翻译成').replace('只需要输出翻译后的结果','不需要翻译背景，只输出当前文本的译文'))
            translated=llm_chat(cfg,[{'role':'user','content':prompt}],max_tokens=512).strip()
            if not translated:raise RuntimeError(f'增强翻译返回空译文，第 {index+1} 条')
            raw=translated;attempts=[]
            if source_lang=='zh' and target_lang=='en':
                translated=_compact_english_caption(translated)
                issues=translation_issues(text,translated,terms)
                for retry_index in range(0 if editor else 2):
                    if not issues:break
                    constraint=('上述结果有以下不忠实之处：'+ '；'.join(issues)+'。重新翻译当前文本，必须保留原文的数字、否定含义和专名；不要翻译背景，也不要解释。')
                    retry=llm_chat(cfg,[{'role':'user','content':prompt+'\n\n'+constraint}],max_tokens=512).strip()
                    retry=_compact_english_caption(retry);new_issues=translation_issues(text,retry,terms)
                    attempts.append(dict(kind='fidelity',text=retry,issues=new_issues))
                    if len(new_issues)<len(issues):translated,issues=retry,new_issues
                if issues and not editor:
                    logger.warning('译文约束未通过 (%s): %s -> %s',issues,text,translated)
                    raise TranslationValidationError(f'第 {index+1} 条译文未通过自动忠实性检查：'+ '；'.join(issues))
                for shortening_round in range(0 if editor else 3):
                    if len(translated)<=limit and len(wrap_display(translated))<=2:break
                    word_limit=max(4,min(15,limit//6))
                    rewrite_prompt=(('当前中文原文：'+text+'\n'+refs+'\n')+f'将以下英文改写成自然、简洁的视频字幕，最多{word_limit}个英文单词，最多{limit}个英文字符（含空格）。'
                                    '保留原意、主语、动作、将来时、全部数字、单位、否定、比较关系和专名；可用数字及通用缩写代替英文数词。'
                                    '无需重复套话，不要解释，只输出精简后的英文：\n\n'+translated+('\n上次改写不合格，必须保留：'+ '; '.join(translation_issues(text,attempts[-1].get('text',''),terms)) if attempts and attempts[-1].get('accepted') is False else ''))
                    retry=_compact_english_caption(llm_chat(cfg,[{'role':'user','content':rewrite_prompt}],max_tokens=512).strip())
                    safe=_safe_subtitle_rewrite(translated,retry,source_text=text) and not translation_issues(text,retry,terms)
                    attempts.append(dict(kind='shorten',text=retry,accepted=safe))
                    if safe:translated=retry
                    elif len(retry)>=len(translated):break
            if audit is not None:audit.append(dict(source=text,target=translated,initial_translation=raw,attempts=attempts,terms={a:b for a,b in terms.items() if a in text}))
            results.append(translated)
            if progress_callback:progress_callback(round(85+14*(index+1)/max(len(texts),1),1),f'本地增强翻译 {src}→{dst} ({index+1}/{len(texts)})')
        if source_lang=='zh' and target_lang=='en' and subtitle_limits:
            results=refine_captions(texts,results,subtitle_limits,terms,cfg,llm_chat,_safe_subtitle_rewrite,audit,cancel_event,progress_callback)
        return results
    # Use the model's official formatting task to translate an entire sentence
    # while preserving timed source units. Snippet tags are validated strictly.
    if str(cfg.get('model', '')).startswith('HY-MT'):
        import html
        import xml.etree.ElementTree as ET
        results, index = [], 0
        while index < len(texts):
            if cancel_event and cancel_event.is_set():
                raise RuntimeError('任务已被取消')
            chunk = []
            for text in texts[index:index+4]:
                chunk.append(text)
                if re.search(r'[。！？.!?]["”\']?$',text.strip()):break
            if len(chunk)==1:
                prompt = f'将以下文本翻译为{dst}，注意只需要输出翻译后的结果，不要额外解释：\n\n{chunk[0]}'
                translated = llm_chat(cfg,[{'role':'user','content':prompt}],max_tokens=512).strip()
                if not translated:raise RuntimeError('翻译引擎返回空译文')
                results.append(translated)
            else:
                source = ' '.join('<sn>'+html.escape(t)+'</sn>' for t in chunk)
                prompt = (f'将以下<source></source>之间的文本翻译为{dst}，注意只需要输出翻译后的结果，不要额外解释，'
                          '原文中的<sn></sn>标签表示标签内文本包含格式信息，需要在译文中相应的位置尽量保留该标签。'
                          '输出格式为：<target>str</target>\n\n<source>'+source+'</source>')
                parsed = None
                for attempt in range(2):
                    content = llm_chat(cfg,[{'role':'user','content':prompt}],max_tokens=1024)
                    try:
                        root = ET.fromstring(content.strip())
                        children = list(root)
                        if root.tag != 'target' or len(children)!=len(chunk) or (root.text or '').strip():raise ValueError('tag count')
                        if any(c.tag!='sn' or len(list(c)) or not (c.text or '').strip() or (c.tail or '').strip() for c in children):raise ValueError('invalid snippet')
                        parsed = [c.text.strip() for c in children];break
                    except (ET.ParseError,ValueError):
                        logger.warning('HY-MT 字幕标签未完整返回，重试 %s',attempt+1)
                if parsed is None:raise RuntimeError('增强翻译未完整返回字幕标签，未生成错位的双语字幕')
                results.extend(parsed)
            index += len(chunk)
            if progress_callback:
                progress_callback(round(85+14*index/max(len(texts),1),1),f'本地增强翻译 {src}→{dst} ({index}/{len(texts)})')
        return results
    system = (
        f'你是专业影视字幕翻译。把用户给出的编号行从{src}翻译成{dst}。'
        '要求：保持编号不变，逐行输出「编号. 译文」；不合并、不拆分、不解释、不加注释；'
        '译文口语化、简洁，长度尽量与原文相近，适合做字幕。'
    )
    batch_size = int(os.environ.get('LLM_BATCH_SIZE', '20') or 20)

    # 滑动上下文：每批带上邻近原文，避免逐批独立导致的断章取义。
    # 上下文行不加数字前缀，解析正则匹配不到，因此不会污染译文行映射。
    def _env_int(name, default):
        try:
            return max(0, int(os.environ.get(name, default)))
        except (TypeError, ValueError):
            return default

    ctx_before = _env_int('LLM_CTX_BEFORE', 3)
    ctx_after = _env_int('LLM_CTX_AFTER', 2)
    if ctx_before or ctx_after:
        system += (
            '\n用户会在编号行前后附带 [上文] / [下文] 标记的行，那些只是让你理解语境的，'
            '绝对不要输出、不要给它们编号；只翻译带「数字. 」的编号行。'
        )

    results = []
    total_batches = (len(texts) + batch_size - 1) // batch_size
    for bi, i in enumerate(range(0, len(texts), batch_size)):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError('任务已被取消')
        chunk = texts[i:i + batch_size]

        parts = []
        if ctx_before:
            for t in texts[max(0, i - ctx_before):i]:
                if t.strip():
                    parts.append('[上文] ' + t.strip())
        # 全局连续编号：保证解析出的顺序与输入严格对应
        for n, t in enumerate(chunk):
            parts.append(f'{i + n + 1}. {t}')
        if ctx_after:
            for t in texts[i + len(chunk):i + len(chunk) + ctx_after]:
                if t.strip():
                    parts.append('[下文] ' + t.strip())

        user = '\n'.join(parts)
        content = llm_chat(cfg, [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}])
        line_map = {}
        for line in content.splitlines():
            m = re.match(r'^\s*(\d+)\s*[.、:：)）]\s*(.*)$', line.strip())
            if m:
                number = int(m.group(1))
                if number in line_map:
                    raise RuntimeError('翻译编号重复，无法安全匹配原文')
                line_map[number] = m.group(2).strip()
        for n, t in enumerate(chunk):
            # 编号是全局连续的（i + n + 1），解析失败/漏行兜底保留原文，绝不串行错位
            if not line_map.get(i + n + 1):
                raise RuntimeError(f'翻译引擎漏掉第 {i+n+1} 行，请重试；未用原文冒充译文')
            results.append(line_map[i + n + 1])
        if progress_callback:
            done = bi + 1
            pct = 85 + (done / total_batches) * 14
            progress_callback(round(pct, 1), f'LLM 翻译中 {src}→{dst} ({done}/{total_batches} 批)')
    return results


# Keep heavy inference imports off the HTTP startup path. Model prewarming
# and inference load them under the existing per-model lock.
import importlib.util
FASTER_WHISPER_AVAILABLE = importlib.util.find_spec('faster_whisper') is not None
if not FASTER_WHISPER_AVAILABLE:
    logger.warning("faster-whisper未安装，将使用备用方案")

def _normalize_glossary(glossary):
    """把术语表统一成 {源词: 目标词} 字典。

    兼容三种入参：dict、'a=b' 每行一条的字符串、None。
    原来这段逻辑只存在于 _apply_glossary 里，转录前拿不到，
    导致术语表只能在转写完成后做字符串替换，无法在解码阶段纠错。
    """
    if not glossary:
        return {}
    if isinstance(glossary, str):
        pairs = {}
        for line in glossary.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                pairs[k.strip()] = v.strip()
        glossary = pairs
    if not isinstance(glossary, dict):
        return {}
    return {str(k): str(v) for k, v in glossary.items() if k and str(v).strip()}


def _n_chunks(duration, chunk_sec):
    """按 chunk_sec 切分总时长，返回片数。"""
    import math
    return max(1, math.ceil(duration / chunk_sec))


def _words_to_list(words):
    """faster-whisper 的 segment.words 转成轻量 list，供逐词高亮/精修使用。

    拿不到就返回空列表，保证调用方无需判空。
    """
    out = []
    for w in (words or []):
        try:
            out.append({
                "start": round(float(w.start), 3),
                "end": round(float(w.end), 3),
                "text": w.word,
                "probability": round(float(w.probability), 4) if w.probability is not None else None,
            })
        except (AttributeError, TypeError, ValueError):
            continue
    return out


class SubtitleGenerator:
    """基于Faster-Whisper的字幕生成器"""

    # Whisper模型缓存
    _model_cache = {}
    # 翻译模型缓存: model_name -> (tokenizer, model)
    _translation_model_cache = {}
    _cache_lock = threading.Lock()
    # Bug #23 修复：per-key 加载锁，避免第一个加载阻塞其他 15 个 waiter 线程
    _model_loading_locks = {}  # cache_key -> Lock
    _model_loading_locks_meta = threading.Lock()  # 保护 _model_loading_locks 自身

    # 翻译模型映射: 语言对 -> HuggingFace模型名
    TRANSLATION_MODELS = {
        'en-zh': 'Helsinki-NLP/opus-mt-en-zh',
        'zh-en': 'Helsinki-NLP/opus-mt-zh-en',
        'ja-zh': 'Helsinki-NLP/opus-mt-ja-zh',
        'ja-en': 'Helsinki-NLP/opus-mt-ja-en',
        'ko-zh': 'Helsinki-NLP/opus-mt-ko-zh',
        'ko-en': 'Helsinki-NLP/opus-mt-ko-en',
        'fr-en': 'Helsinki-NLP/opus-mt-fr-en',
        'de-en': 'Helsinki-NLP/opus-mt-de-en',
        'es-en': 'Helsinki-NLP/opus-mt-es-en',
        'en-fr': 'Helsinki-NLP/opus-mt-en-fr',
        'en-de': 'Helsinki-NLP/opus-mt-en-de',
        'en-es': 'Helsinki-NLP/opus-mt-en-es',
        'en-ja': 'Helsinki-NLP/opus-mt-en-jap',
        'zh-ja': 'Helsinki-NLP/opus-mt-zh-jap',
    }

    def __init__(self, model_size="tiny", device="auto", progress_callback=None, max_retries=3):
        """
        初始化字幕生成器

        Args:
            model_size: 模型大小 (tiny, base, small, medium, large, large-v3-turbo)
            device: 运行设备 (auto, cpu, cuda)
            progress_callback: 进度回调函数 callback(percent, info)
            max_retries: 最大重试次数
        """
        self.model_size = model_size
        # GPU 支持（MOD-GPU）：device 默认走自动探测。
        #   auto/cuda -> _resolve_device 会探测 CUDA，命中则用 GPU，否则回退 CPU
        #   显式传 cpu 则强制 CPU（向后兼容）
        self.device = self._resolve_device(device)
        self.model = None
        self.progress_callback = progress_callback
        self.max_retries = max_retries

        logger.info(f"字幕生成器初始化，模型大小: {model_size}, 设备: {self.device}")

    @staticmethod
    def _resolve_device(requested):
        """决定推理设备（MOD-GPU）。优先级：显式非auto参数 > USE_GPU环境变量 > 自动探测。"""
        import os as _os
        if requested and requested != "auto":
            return requested
        pref = (_os.environ.get("USE_GPU") or "auto").strip().lower()
        want_cuda = pref in ("cuda", "auto", "1", "true", "yes")
        if not want_cuda:
            return "cpu"
        cuda_ok, n = SubtitleGenerator._cuda_available()
        if cuda_ok:
            logger.info(f"[MOD-GPU] 检测到 CUDA 设备 x{n}，使用 GPU 加速")
            return "cuda"
        if pref == "cuda":
            logger.warning("[MOD-GPU] USE_GPU=cuda 但未检测到 CUDA 设备，回退 CPU")
        elif not SubtitleGenerator._gpu_hint_logged:
            SubtitleGenerator._gpu_hint_logged = True
            # 说清楚引擎的真实能力边界，免得 USE_GPU=auto 看着像配了加速其实没有
            has_dri = os.path.isdir('/dev/dri')
            logger.info(
                "[MOD-GPU] 未检测到 NVIDIA CUDA 设备，转写走 CPU。\n"
                "           说明：faster-whisper 底层是 CTranslate2，只实现了 NVIDIA CUDA 后端，"
                "Intel/AMD 核显与 NPU 均无法用于转写推理（烧录的 VAAPI 硬编是另一条链路，不受影响）。"
                + ("\n           注意：/dev/dri 可见但本引擎用不上它，别被核显迷惑。"
                   if has_dri else "")
                + "\n           想提速：换 faster-whisper 以外的引擎（whisper.cpp / OpenVINO / "
                "Qwen3-ASR），或把 ASR 交给带 GPU 的机器，字幕文件再拷回本机烧录。"
            )
        return "cpu"

    _gpu_hint_logged = False  # GPU 回落提示每进程只打一次

    @staticmethod
    def _cuda_available():
        """探测是否有可用 NVIDIA GPU。返回 (bool, count)。"""
        try:
            import ctranslate2
            n = ctranslate2.get_cuda_device_count()
            return (n > 0, n)
        except Exception:
            return (False, 0)

    def _compute_type(self):
        """按当前设备返回 faster-whisper compute_type。"""
        return "float16" if self.device == "cuda" else "int8"

    @exclusive_ai_phase
    def load_model(self):
        """加载Faster-Whisper模型（Bug #23 修复：per-key 锁 + 双重检查）"""
        wait_for_local_sleep(load_llm_config(),self.progress_callback)
        cache_key = f"{self.model_size}_{self.device}"

        # 第一重检查（无锁）
        with self._cache_lock:
            if cache_key in self._model_cache:
                self.model = self._model_cache[cache_key]
                if not self.model.model.model_is_loaded:self.model.model.load_model()
                logger.info(f"使用缓存的模型: {self.model_size}")
                return self.model

        # 获取 per-key 加载锁（避免阻塞其他模型的请求）
        with self._model_loading_locks_meta:
            per_key_lock = self._model_loading_locks.get(cache_key)
            if per_key_lock is None:
                per_key_lock = threading.Lock()
                self._model_loading_locks[cache_key] = per_key_lock

        with per_key_lock:
            # 第二重检查（锁内）
            with self._cache_lock:
                if cache_key in self._model_cache:
                    self.model = self._model_cache[cache_key]
                    return self.model

            logger.info(f"正在加载Faster-Whisper模型: {self.model_size}")
            if self.progress_callback:
                self.progress_callback(0, f'正在加载 {self.model_size} 模型...')

            try:
                # MOD-2 修复：优先用本地缓存（local_files_only），与翻译模型对称。
                # entrypoint.sh 已预下载模型到 HF cache，这里先尝试离线加载；
                # 若缓存不存在再联网下载（避免每次启动都发 API 请求触发 429）。
                from faster_whisper import WhisperModel
                from faster_whisper.utils import download_model as _fw_download
                try:
                    model_path = _fw_download(self.model_size, local_files_only=True)
                    logger.info(f"使用缓存的模型: {self.model_size}")
                except Exception:
                    # 本地无缓存，联网下载（多源 fallback 由 HF_ENDPOINT 控制）
                    logger.info(f"本地无 {self.model_size} 缓存，联网下载...")
                    model_path = _fw_download(self.model_size)
                # compute_type 按设备映射（MOD-GPU）：
                #   cuda -> float16（GPU 速度/精度最佳）
                #   cpu  -> int8（原行为，更快更省内存）
                # cpu_threads 显式对齐容器 CPU 配额（默认 4，防默认值漂移；env 可覆盖）
                try:
                    cpu_threads = int(os.environ.get('WHISPER_CPU_THREADS', '0')) or min(4, os.cpu_count() or 4)
                except ValueError:
                    cpu_threads = 4
                self.model = WhisperModel(
                    model_path,
                    device=self.device,
                    compute_type=self._compute_type(),
                    cpu_threads=cpu_threads if self.device == 'cpu' else 0,
                )

                with self._cache_lock:
                    # 限制缓存大小，最多保留2个模型
                    if len(self._model_cache) >= 2:
                        oldest_key = next(iter(self._model_cache))
                        del self._model_cache[oldest_key]
                        logger.info(f"已从缓存中移除模型: {oldest_key}")
                    self._model_cache[cache_key] = self.model
                logger.info(f"{self.model_size} 模型加载完成")
                if self.progress_callback:
                    self.progress_callback(0, f'{self.model_size} 模型加载完成')

            except Exception as e:
                logger.error(f"模型加载失败: {str(e)}")
                raise e
            finally:
                # BUG-4 修复：原 per-key 锁清理逻辑是 dead code（在 with per_key_lock 内部
                # acquire(blocking=False) 恒返回 False）。改为 finally 后直接清理。
                with self._model_loading_locks_meta:
                    self._model_loading_locks.pop(cache_key, None)

        return self.model

    def _cleanup_incomplete_downloads(self, cache_dir):
        """清理未完成的模型下载文件，防止卡死后续下载"""
        if not cache_dir or not os.path.isdir(cache_dir):
            return
        import glob
        for incomplete in glob.glob(os.path.join(cache_dir, '**', '*.incomplete'), recursive=True):
            try:
                os.remove(incomplete)
                logger.warning(f"已清理未完成的下载文件: {incomplete}")
            except OSError:
                pass

    def _load_translation_model(self, model_name):
        """加载翻译模型并缓存，使用TRANSLATOR_MODELS_DIR作为缓存目录"""
        from transformers import MarianMTModel, MarianTokenizer

        cache_dir = os.environ.get('TRANSLATOR_MODELS_DIR', None)
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
            self._cleanup_incomplete_downloads(cache_dir)

        # Bug #17 修复：整个加载逻辑进锁，避免并发下载同一模型
        with self._cache_lock:
            if model_name in self._translation_model_cache:
                logger.info(f"使用缓存的翻译模型: {model_name}")
                return self._translation_model_cache[model_name]

            logger.info(f"正在加载翻译模型: {model_name} (cache_dir={cache_dir})")
            kwargs = {}
            local_model_path = None
            if cache_dir:
                kwargs['cache_dir'] = cache_dir
                model_slug = model_name.rsplit('/', 1)[-1]
                if model_slug.startswith('opus-mt-'):
                    compact_name = model_slug[len('opus-mt-'):]
                    candidate = os.path.join(cache_dir, compact_name)
                    if os.path.exists(os.path.join(candidate, 'config.json')):
                        local_model_path = candidate

            # Prefer compact mounted model folders such as /app/translator_models/en-zh.
            if local_model_path:
                try:
                    tokenizer = MarianTokenizer.from_pretrained(local_model_path, local_files_only=True)
                    model = MarianMTModel.from_pretrained(local_model_path, local_files_only=True)
                    logger.info(f"翻译模型从紧凑目录加载: {local_model_path}")
                    # MOD-GPU：翻译模型也搬到当前设备（CPU 时为无操作）
                    model = model.to(self.device)
                    self._translation_model_cache[model_name] = (tokenizer, model)
                    return tokenizer, model
                except Exception as e:
                    logger.warning(f"紧凑目录加载失败，回退 HuggingFace cache: {local_model_path}: {e}")

            # Bug #16 修复：不再改全局 HF_HUB_OFFLINE，只用 local_files_only 参数
            # 避免多线程下污染其他线程的 HF 设置
            # MOD-1 修复：在线加载遇 429/网络错误时加退避重试（最多 3 次）
            tokenizer = None
            model = None
            last_error = None
            for local_only in (True, False):
                if local_only:
                    try:
                        call_kwargs = {**kwargs, 'local_files_only': True}
                        tokenizer = MarianTokenizer.from_pretrained(model_name, **call_kwargs)
                        model = MarianMTModel.from_pretrained(model_name, **call_kwargs)
                        logger.info(f"翻译模型从本地缓存加载: {model_name}")
                        break
                    except Exception as e:
                        last_error = e
                        logger.info(f"本地缓存未命中，尝试在线加载: {model_name}")
                        continue
                # 在线加载（带重试）
                call_kwargs = {**kwargs, 'local_files_only': False}
                for retry in range(3):
                    try:
                        tokenizer = MarianTokenizer.from_pretrained(model_name, **call_kwargs)
                        model = MarianMTModel.from_pretrained(model_name, **call_kwargs)
                        break
                    except Exception as e:
                        last_error = e
                        if retry < 2:
                            wait = (retry + 1) * 5
                            logger.warning(f"翻译模型在线加载失败({retry+1}/3)，{wait}s 后重试: {model_name}: {e}")
                            time.sleep(wait)
                        else:
                            logger.error(f"翻译模型加载失败(已重试3次): {model_name}: {e}")
                if tokenizer and model:
                    break

            if not (tokenizer and model):
                if last_error:
                    raise RuntimeError(
                        f"本地翻译模型 {model_name} 不可用（{last_error}）。"
                        "可在「工具中心 → AI 翻译引擎」启用 LLM 翻译，或更换语言对。"
                    )
                raise RuntimeError(f"翻译模型加载失败: {model_name}")

            # MOD-3 优化：按可用内存动态调整缓存上限（原固定 3 槽，14 语言对会反复驱逐）
            try:
                import psutil
                avail_gb = psutil.virtual_memory().available / (1024**3)
                # 每 GB 内存允许缓存 1 个模型（每个约 300MB），最少 3，最多 8
                max_cache = max(3, min(8, int(avail_gb)))
            except Exception:
                max_cache = 4
            if len(self._translation_model_cache) >= max_cache:
                oldest_key = next(iter(self._translation_model_cache))
                del self._translation_model_cache[oldest_key]
                logger.info(f"已从缓存中移除翻译模型: {oldest_key} (缓存上限{max_cache})")
            # MOD-GPU：翻译模型搬到当前设备（CPU 时为无操作，GPU 时加速翻译）
            model = model.to(self.device)
            self._translation_model_cache[model_name] = (tokenizer, model)
            logger.info(f"翻译模型加载完成: {model_name}")
            return tokenizer, model

    @exclusive_ai_phase
    def generate_subtitle(self, video_path, output_dir, language="auto", format="srt", translate_to=None,
                          cancel_event=None, output_naming="plain", bilingual=False,
                          split_max_chars=0, glossary=None, translate_engine='auto',
                          diarize=False, num_speakers=0):
        """
        生成字幕文件

        Args:
            video_path: 视频文件路径
            output_dir: 输出目录
            language: 语言代码 (auto, zh, en, ja等)
            format: 字幕格式 (srt, vtt, json)
            translate_to: 翻译目标语言 (如 'zh', 'en', 'ja'等)
            cancel_event: threading.Event，用于协作式取消

        Returns:
            dict: 包含字幕文件路径和处理结果的信息
        """
        if not FASTER_WHISPER_AVAILABLE:
            return {
                "success": False,
                "error": "faster-whisper未安装",
                "message": "请先安装 faster-whisper"
            }

        video_name = os.path.splitext(os.path.basename(video_path))[0]
        self._asr_warnings = []
        self._recognition_evidence = None
        self._translation_audit = []

        # PERF 优化：转录是整个流程最耗时的阶段。若转录成功但后续（翻译/保存）因意外
        # 异常重试，缓存已转录的 segments 避免重新转录。
        _cached_segments = None
        _cached_info = None

        for attempt in range(self.max_retries):
            try:
                logger.info(f"开始处理视频: {video_path} (尝试 {attempt + 1}/{self.max_retries})")

                os.makedirs(output_dir, exist_ok=True)

                if self.progress_callback:
                    self.progress_callback(5, '正在加载模型...')

                # 加载模型
                model = self.load_model()

                if _cached_segments is not None:
                    # 重试时复用已转录结果，跳过最耗时的转录阶段
                    logger.info(f"复用上次转录结果（{len(_cached_segments)} 片段），跳过重新转录")
                    all_segments = _cached_segments
                    info = _cached_info
                    if self.progress_callback:
                        self.progress_callback(80, '正在保存字幕（复用转录结果）...')
                else:
                    if self.progress_callback:
                        self.progress_callback(20, '正在转录音频...')

                    # 中文质量优化（2026-08-30）：whisper 对中文默认输出繁体且标点弱。
                    # initial_prompt="以下是普通话的句子。" 可让输出变为简体+规范标点（实测 tiny/base 均有效，
                    # 对纯中文内容无速度损失；混合语言时英文段被跳过是锁定 zh 的固有行为，与 prompt 无关）。
                    # env INITIAL_PROMPT 可覆盖，设为空串禁用。
                    zh_prompt = os.environ.get('INITIAL_PROMPT', '以下是普通话的句子。')

                    # ==== 转录质量参数（2026-10-04 加固）====
                    # ASR_BEAM_SIZE  : 恢复 Whisper 默认束搜索 5。原值 1 是贪心解码，
                    #                   准确率明显低于束搜索，CPU 上这点耗时很划算。
                    # ASR_WORD_TIMESTAMPS : 打开词级时间戳，供逐词高亮/精修；
                    #                   同时是 hallucination_silence_threshold 的前置条件。
                    # ASR_HOTWORDS   : 全局热词（逗号分隔），叠加在术语表目标词之后。
                    # ASR_CONDITION_ON_PREV_TEXT : 默认关闭。Whisper 官方说明开启后
                    #                   模型会自我重复，且会破坏防幻觉阈值的作用。
                    def _env_int(name, default):
                        try:
                            return int(os.environ.get(name, default))
                        except (TypeError, ValueError):
                            return default

                    def _env_float(name, default):
                        try:
                            return float(os.environ.get(name, default))
                        except (TypeError, ValueError):
                            return default

                    _beam = max(1, _env_int('ASR_BEAM_SIZE', 5))
                    _word_ts = os.environ.get('ASR_WORD_TIMESTAMPS', '1').strip().lower() \
                        not in ('0', 'false', 'no', '')
                    _no_speech = _env_float('ASR_NO_SPEECH_THRESHOLD', 0.6)
                    _cond_prev = os.environ.get('ASR_CONDITION_ON_PREV_TEXT', '0').strip().lower() \
                        in ('1', 'true', 'yes')
                    _hallu_raw = os.environ.get('ASR_HALLUCINATION_SILENCE', '2.0').strip().lower()
                    _hallu_disabled = _hallu_raw in ('', '0', 'off', 'no', 'false')

                    # 术语表接到解码器：喂「目标词」而不是等转完再替换字符串。
                    # 之前 _apply_glossary 属于事后纠错，模型压根不知道该往哪个方向识别。
                    _hw = []
                    for _dst in _normalize_glossary(glossary).values():
                        if _dst.strip():
                            _hw.append(_dst.strip())
                    _hw += [w.strip() for w in os.environ.get('ASR_HOTWORDS', '').split(',') if w.strip()]
                    _hw = list(dict.fromkeys(_hw))  # 去重且保序
                    _hotwords = ', '.join(_hw) if _hw else None

                    _tune = {
                        'beam_size': _beam,
                        'vad_filter': True,
                        'log_progress': True,
                        'word_timestamps': _word_ts,
                        'no_speech_threshold': _no_speech,
                        'condition_on_previous_text': _cond_prev,
                    }
                    if _hotwords:
                        _tune['hotwords'] = _hotwords
                    if not _hallu_disabled and _word_ts and not _cond_prev:
                        _tune['hallucination_silence_threshold'] = _env_float('ASR_HALLUCINATION_SILENCE', 2.0)

                    if _hotwords:
                        logger.info(f"启用解码热词({len(_hw)}个): {_hotwords}")
                    logger.info(f"转录参数: beam_size={_beam} word_timestamps={_word_ts} "
                                f"no_speech={_no_speech} condition_on_prev={_cond_prev}")

                    # 执行转录（长视频自动分片 + 断点续跑，短片走单次路径）
                    # 云端 ASR（v1.2.0）：配置启用且任务不带说话人分离时优先云端，失败回退本地
                    all_segments = None
                    _cloud_cfg = {} if diarize else _load_cloud_asr_config()
                    if _cloud_cfg:
                        try:
                            all_segments, info = _cloud_transcribe(
                                video_path, _cloud_cfg, language,
                                progress_callback=self.progress_callback)
                            logger.info(f"云端 ASR 完成: {len(all_segments)} 段 "
                                        f"(lang={getattr(info, 'language', language)})")
                        except Exception as _ce:
                            logger.warning(f"云端 ASR 失败，回退本地 Whisper: {_ce}")
                            all_segments = None
                    if all_segments is None:
                        if _cloud_cfg:
                            if self.progress_callback:
                                self.progress_callback(18, '云端失败，回退本地 Whisper...')
                        language_param = None if language == "auto" else language
                        all_segments, info = self._transcribe_with_chunks(
                            model, video_path, _tune, language_param, zh_prompt,
                            progress_callback=self.progress_callback,
                            cancel_event=cancel_event)
                    if all_segments is None:  # 已取消
                        return {"success": False, "error": "任务已被用户取消",
                                "message": "canceled"}

                    logger.info(f"检测到的语言: {info.language}")

                    # 幻听清洗（2026-10-26）：整句命中已知幻听黑名单的片段直接剔除，
                    # 主要打击 BGM/尾奏段的「谢谢观看/Amara 字幕」类幻觉输出。
                    if os.environ.get('ASR_SCRUB_HALLUCINATIONS', '1').strip().lower() not in ('0', 'false', 'no', 'off'):
                        _before = len(all_segments)
                        all_segments, _dropped = scrub_hallucination_segments(all_segments)
                        if _dropped:
                            logger.info(f"幻听清洗: 剔除 {_dropped} 条疑似幻听片段（{_before} -> {len(all_segments)}）")

                    # 收集所有片段 (支持协作式取消)
                    total_segments = 0
                    total_duration = getattr(info, 'duration_after_vad', None) or getattr(info, 'duration', None)
                    last_progress_pct = 20.0
                    for seg in all_segments:
                        total_segments += 1

                    logger.info(f"转录完成，共 {total_segments} 个片段")
                    if info.language == 'zh':
                        from utils.chinese_grounding import extract_video_text, correct_segments
                        raw_segments = self._copy_segments(all_segments)
                        # Keep the untouched native result, but match visual
                        # phrases against adjacent spoken fragments together.
                        # Otherwise a four-character greeting or half a proper
                        # name cannot meet the conservative evidence gate.
                        correction_input = self._copy_segments(raw_segments)
                        if os.environ.get('MERGE_FRAGMENTS','1')!='0':
                            correction_input = self._merge_short_fragments(correction_input)
                        try:
                            if local_memory_mode(load_llm_config()):
                                with self._cache_lock:release_asr_weights(self._model_cache)
                            if self.progress_callback:self.progress_callback(79.5,'正在自动核对中文文字与专名...')
                            visual=extract_video_text(video_path,cancel_event,self.progress_callback,segments=correction_input)
                            all_segments,review=correct_segments(correction_input,visual.get('frames',[]))
                            self._recognition_evidence=dict(raw_segments=raw_segments,correction_input_segments=correction_input,visual=visual,automatic_review=review)
                        except Exception as evidence_error:
                            if '已被取消' in str(evidence_error):raise
                            warning='自动文字证据不可用，已保留原始音频识别：'+str(evidence_error)
                            self._asr_warnings.append(warning)
                            self._recognition_evidence=dict(raw_segments=raw_segments,status='unavailable',error=str(evidence_error))

                    # 保险：剥离可能被模型复读出来的提示文本（实测概率低，防御性处理）
                    if zh_prompt:
                        for seg in all_segments:
                            if zh_prompt in seg['text']:
                                seg['text'] = seg['text'].replace(zh_prompt, '').strip()
                    # 碎段合并：whisper 常输出 <0.5s 的碎片字幕，肉眼难读；将间隔极小的相邻段合并
                    if os.environ.get('MERGE_FRAGMENTS', '1') != '0':
                        merged = self._merge_short_fragments(all_segments)
                        if len(merged) != len(all_segments):
                            logger.info(f"碎段合并: {len(all_segments)} -> {len(merged)} 片段")
                            all_segments = merged
                    # 缓存转录结果供潜在的重试复用
                    _cached_segments = all_segments
                    _cached_info = info

                if self.progress_callback:
                    self.progress_callback(80, '正在保存字幕...')

                # 未显式指定 split_max_chars 时按 ASR_MAX_CUE_CHARS 兜底（默认 42，Netflix 每行上限）。
                # 不兜底的话，beam_size 调大后切句变粗，单块能冲到 80+ 字一整行没法看。
                _cue_limit = split_max_chars
                if not _cue_limit:
                    try:
                        _cue_limit = int(os.environ.get('ASR_MAX_CUE_CHARS', '42'))
                    except (TypeError, ValueError):
                        _cue_limit = 42
                original_segments = self._copy_segments(all_segments)
                before_glossary = [seg["text"] for seg in original_segments]
                self._apply_glossary(original_segments, glossary)
                for seg, before in zip(original_segments, before_glossary):
                    if seg["text"] != before:
                        seg.pop("words", None)
                if translate_to and translate_to != info.language:
                    original_segments = semantic_segments(original_segments,target_lang=translate_to)
                all_segments = self._copy_segments(original_segments)

                self._layout_width = max(10, (_cue_limit or 42) - (8 if diarize else 0))
                # 翻译字幕 (如果指定了目标语言)
                translated_language = None
                translate_note = None
                if translate_to and translate_to != info.language:
                    if self.progress_callback:
                        self.progress_callback(85, f'正在翻译字幕到 {translate_to}...')
                    try:
                        engine = (translate_engine or 'auto').lower()
                        use_llm = llm_translate_ready() if engine == 'auto' else engine == 'llm'
                        segments_to_translate = self._copy_segments(original_segments)
                        self._media_duration = self._probe_duration(video_path)
                        if use_llm:
                            try:
                                llm_texts = llm_translate_texts(
                                    [s['text'].strip() for s in segments_to_translate if s['text'].strip()],
                                    info.language, translate_to,
                                    progress_callback=self.progress_callback,
                                    cancel_event=cancel_event,
                                    subtitle_limits=_subtitle_character_limits(original_segments,self._media_duration),
                                    glossary=glossary,audit=self._translation_audit,
                                )
                                it = iter(llm_texts)
                                for seg in segments_to_translate:
                                    if seg['text'].strip():
                                        seg['text'] = next(it)
                                translated_segments = segments_to_translate
                                logger.info(f"翻译引擎: LLM ({info.language}->{translate_to})")
                            except TranslationValidationError:
                                raise
                            except Exception as le:
                                # LLM 失败自动回退本地 opus-mt，不让翻译整段落空
                                logger.warning(f"LLM 翻译失败，回退本地模型: {le}")
                                translate_note = f'LLM 翻译失败已回退本地模型: {le}'
                                translated_segments = self._translate_segments(
                                    self._copy_segments(original_segments),
                                    info.language,
                                    translate_to,
                                    self.progress_callback,
                                    cancel_event
                                )
                        else:
                            translated_segments = self._translate_segments(
                                self._copy_segments(original_segments),
                                info.language,
                                translate_to,
                                self.progress_callback,
                                cancel_event
                            )
                        self._apply_glossary(translated_segments, glossary)
                        self._media_duration = self._probe_duration(video_path)
                        all_segments = self._combine_bilingual_segments(original_segments, translated_segments) if bilingual else translated_segments
                        translated_language = translate_to
                        logger.info(f"字幕翻译完成: {info.language} -> {translate_to}")
                    except Exception as te:
                        logger.error(f"翻译失败: {str(te)}")
                        translate_note = f'翻译失败: {str(te)}'
                        # 翻译失败不影响字幕生成，只是没有翻译

                if not translated_language and _cue_limit > 0:
                    all_segments = self._split_long_segments(all_segments, _cue_limit)

                # 说话人分离（可选）：放在翻译之后，保证 [说话人N] 前缀不被翻译结果覆盖；
                # 失败降级为不带标签继续出字幕。进度回调保持 99.5 不回跳。
                diarize_note = None
                speakers_found = 0
                if diarize:
                    if self.progress_callback:
                        self.progress_callback(99.5, '说话人分离中（首次需下载模型，请稍候）...')
                    try:
                        from utils.diarizer import diarize_segments
                        labels, speakers_found = diarize_segments(
                            video_path, all_segments,
                            num_speakers=int(num_speakers or 0),
                        )
                        for seg, lab in zip(all_segments, labels):
                            t = str(seg.get('text', '') or '').strip()
                            if t and lab:
                                seg['text'] = f'[说话人{lab}] {t}'
                        if self.progress_callback:
                            self.progress_callback(99.5, f'说话人分离完成：{speakers_found} 人，正在保存...')
                        logger.info(f"说话人分离完成: {speakers_found} 人")
                    except Exception as de:
                        logger.warning(f"说话人分离失败（继续不带标签）: {de}")
                        diarize_note = f'说话人分离失败（已继续，无标签）: {de}'

                all_segments = layout_segments(all_segments, max(10, _cue_limit or 42))
                if info.language=='zh' and not translated_language and not diarize:
                    all_segments=readable_monolingual_segments(all_segments,self._probe_duration(video_path),max(10,_cue_limit or 42))
                if not all_segments:
                    return {"success": False, "error": "未识别到语音，未生成空字幕", "message": "没有可保存的字幕"}

                # 保存字幕文件（带进度回调：让前端知道"保存中"独立阶段，不再假 100%）
                result = {
                    "language": translated_language or info.language,
                    "original_language": info.language,
                    "segments": all_segments,
                    "original_segments": original_segments if translated_language else None,
                    "asr_warnings":list(getattr(self,'_asr_warnings',[])),
                    "recognition_evidence":self._recognition_evidence,
                    "translation_audit":self._translation_audit,
                    "bilingual": bool(bilingual and translated_language),
                    "source_media": os.path.abspath(video_path),
                }
                if translate_note:
                    result["translate_note"] = translate_note
                if diarize_note:
                    result["diarize_note"] = diarize_note
                if speakers_found:
                    result["speakers"] = speakers_found

                # 保存阶段进度回调（独立文案，让用户看到"翻译完→保存中→完成"的清晰阶段）
                if self.progress_callback:
                    self.progress_callback(99.5, '正在保存字幕文件...')

                try:
                    output_files = self._save_subtitle_files(result, output_dir, video_name, format, output_naming)
                except OSError as e:
                    if e.errno == 30:  # Read-only file system
                        fallback_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'subtitles'))
                        logger.warning(f"输出目录只读({output_dir})，回退到: {fallback_dir}")
                        if self.progress_callback:
                            self.progress_callback(95, '源目录只读，保存到默认字幕目录...')
                        os.makedirs(fallback_dir, exist_ok=True)
                        output_files = self._save_subtitle_files(result, fallback_dir, video_name, format, output_naming)
                    else:
                        raise

                if self.progress_callback:
                    self.progress_callback(100, '字幕生成完成！')

                return {
                    "success": True,
                    "warnings": list(getattr(self,'_asr_warnings',[])) + [n for n in (translate_note, diarize_note,
                        "本地机器翻译需要人工校对" if translated_language else None) if n],
                    "translate_note": translate_note,
                    "diarize_note": diarize_note,
                    "speakers": speakers_found,
                    "translated_language": translated_language,
                    "save_directory": os.path.dirname(next(iter(output_files.values()))),
                    "video_name": video_name,
                    "detected_language": info.language,
                    "duration": self._probe_duration(video_path) or (all_segments[-1]["end"] if all_segments else 0),
                    "output_files": output_files,
                    "device_used": self.device,
                    "model_used": self.model_size,
                    "message": "字幕生成成功"
                }

            except OSError as e:
                # 文件系统错误（如只读）不重试，直接失败
                logger.error(f"字幕保存失败（文件系统错误）: {str(e)}")
                return {
                    "success": False,
                    "error": f"无法写入输出目录: {str(e)}",
                    "message": "字幕生成失败：输出目录无法写入"
                }
            except Exception as e:
                # 协作式取消：立即终止，不进入重试循环（否则会重新转录 max_retries-1 次才停）
                # 两个取消来源都要认，只认 cancel_event 会漏掉队列路径：
                #   1) threading.Event —— /api/generate 直调路径会传 cancel_event
                #   2) 哨兵异常文案   —— TaskQueue worker 路径下 cancel_event 是 None
                #      （processing_tasks 里没有该任务条目），改由 app.py 的进度回调
                #      检测到 cancel_requested 后抛 Exception("任务已被取消")
                if (cancel_event is not None and cancel_event.is_set()) or "已被取消" in str(e):
                    logger.info(f"字幕生成已取消: {video_path}")
                    return {
                        "success": False,
                        "error": "任务已被取消",
                        "message": "canceled"
                    }
                logger.error(f"字幕生成失败 (尝试 {attempt + 1}/{self.max_retries}): {str(e)}")
                if attempt < self.max_retries - 1:
                    time.sleep(5)
                else:
                    return {
                        "success": False,
                        "error": str(e),
                    "message": f"字幕生成失败"
                }

    def _copy_segments(self, segments):
        # 防御性拷贝：保留全部字段（含 words 词级时间戳）。
        # 之前只挑 start/end/text，把 word_timestamps 的成果在这步抹掉了。
        # 用浅拷贝即可——词级 dict 全程只读，深拷贝在长视频上会白翻倍内存。
        return [dict(seg) for seg in segments]

    def _apply_glossary(self, segments, glossary):
        glossary = _normalize_glossary(glossary)
        if not glossary:
            return segments
        for seg in segments:
            text = seg.get('text', '')
            for source, target in glossary.items():
                if source:
                    text = text.replace(str(source), str(target))
            seg['text'] = text
        return segments

    def _merge_short_fragments(self, segments, max_gap=0.4, max_duration=6.0, max_chars=60):
        """合并碎片字幕：相邻段间隔 < max_gap 且合并后时长/字数都在阈值内时合并。

        whisper（尤其 tiny/base）在停顿密集的音频上会输出大量 <0.5s 的碎片，
        单条字幕闪现不可读。只做保守合并（不改变任何文本内容，仅拼接并集时间轴）。
        """
        if len(segments) < 2:
            return segments

        def _join_text(a, b):
            a, b = a.strip(), b.strip()
            if not a:
                return b
            if not b:
                return a
            # 两侧有一方以 CJK 结尾/开头则直接拼接，西文之间保留空格
            cjk = lambda ch: '\u4e00' <= ch <= '\u9fff'
            if cjk(a[-1]) or cjk(b[0]):
                return a + b
            return a + ' ' + b

        merged = []
        for seg in segments:
            text = (seg.get('text') or '').strip()
            if not text:
                # 空文本段直接丢弃（时间轴无效字幕）
                continue
            if merged:
                prev = merged[-1]
                gap = seg['start'] - prev['end']
                span = seg['end'] - prev['start']
                new_text = _join_text(prev['text'], text)
                if gap < max_gap and span <= max_duration and len(new_text) <= max_chars:
                    prev['end'] = max(prev['end'], seg['end'])
                    prev['text'] = new_text
                    # 词级数据一并串接，否则合并后逐词高亮就断了。
                    # 原先这里只重建 start/end/text，words 被整个丢掉，
                    # 导致 words.json 写不出来（段数少的模型恰好不触发合并，看不出来）。
                    pw = prev.get('words') or []
                    sw = seg.get('words') or []
                    if pw or sw:
                        prev['words'] = sorted(pw + sw, key=lambda w: w.get('start', 0))
                    continue
            # 同样保留原 segment 的其余字段（含 words）
            item = dict(seg)
            item['start'] = seg['start']
            item['end'] = seg['end']
            item['text'] = text
            merged.append(item)
        return merged

    def _split_long_segments(self, segments, max_chars):
        """把超长字幕块按标点 + 词边界拆开。

        2026-10-04：beam_size 调大并打开 word_timestamps 后，whisper 的切句会变粗
        （同一段 58 秒演示由 10 块变成 7 块，个别块冲到 80+ 字一整行），可读性反而
        下降。这里做兜底：有词级时间戳时按词的真实时间给子块分配时间轴（比均分精确，
        切出来的字幕对得上音）；没有词级时间戳时退化为按字符比例分配。
        切分点优先落在标点后，其次才按字数硬切。
        """
        try:
            max_chars = int(max_chars or 0)
        except (TypeError, ValueError):
            max_chars = 0
        if max_chars <= 0:
            return segments

        PUNCT = ('\u3002', '\uff01', '\uff1f', '!', '?', '\uff0c', '\u3001', '\uff1b', ';', ',')

        def _split_tokens(text):
            """在标点后切分，并记录每段在原串中的 [start, end) 字符偏移。"""
            marks = []
            for ch in PUNCT:
                i = 0
                while True:
                    j = text.find(ch, i)
                    if j < 0:
                        break
                    # 半角逗号夹在数字之间是千分位，不当切点
                    if ch == ',' and j > 0 and j + 1 < len(text) \
                            and text[j - 1].isdigit() and text[j + 1].isdigit():
                        i = j + 1
                        continue
                    i = j + len(ch)
                    marks.append(i)
            marks.append(len(text))
            marks.sort()

            out, prev = [], 0
            for m in marks:
                if m <= prev:
                    continue
                raw = text[prev:m]
                seg = raw.strip()
                if seg:
                    lead = len(raw) - len(raw.lstrip())
                    out.append((seg, prev + lead, prev + lead + len(seg)))
                prev = m
            return out

        def _pack(toks, limit):
            """把 token 装进不超过 limit 字的块，返回 [(text, start_off, end_off)]。"""
            out, buf, b0, b1 = [], '', None, None
            for seg, ts, te in toks:
                if len(seg) > limit:
                    # 单 token 就超长（整段没有标点）→ 硬切
                    if buf:
                        out.append((buf, b0, b1))
                        buf, b0, b1 = '', None, None
                    for k in range(0, len(seg), limit):
                        out.append((seg[k:k + limit], ts + k, ts + min(k + limit, len(seg))))
                    continue
                if buf and len(buf) + 1 + len(seg) > limit:
                    out.append((buf, b0, b1))
                    buf, b0, b1 = seg, ts, te
                else:
                    if not buf:
                        buf, b0, b1 = seg, ts, te
                    else:
                        buf = buf + ' ' + seg
                        b1 = te
            if buf:
                out.append((buf, b0, b1))
            return out

        result = []
        for seg in segments:
            text = (seg.get('text') or '').strip()
            if len(text) <= max_chars:
                result.append(seg)
                continue

            parts = _pack(_split_tokens(text), max_chars)
            if len(parts) <= 1:
                parts = [(text[i:i + max_chars], i, min(i + max_chars, len(text)))
                         for i in range(0, len(text), max_chars)]

            words = seg.get('words') or []
            # 每个词在段文本中的字符跨度，用于把子块映射回具体词
            spans, acc = [], 0
            for w in words:
                wt = w.get('text') or ''
                lead = len(wt) - len(wt.lstrip())
                core = len(wt.strip())
                spans.append((acc + lead, acc + lead + core))
                acc += len(wt)

            total = max(len(text), 1)
            lo, hi = seg['start'], seg['end']
            dur = max(hi - lo, 0.01)
            for ptxt, ps, pe in parts:
                sub = [w for w, (ws, we) in zip(words, spans) if we > ps and ws < pe]

                # 词级时间可信才用：子块用到的词必须都落在父段范围内，
                # 否则说明对齐数据不可用，退回按字符比例分配（均分是下策，
                # 但远好过所有子块都塌成父段全宽而互相重叠）。
                usable = bool(sub) and all(
                    w.get('start') is not None and w.get('end') is not None
                    and lo <= w['start'] < w['end'] <= hi
                    for w in sub
                )
                if usable:
                    st, en = sub[0]['start'], sub[-1]['end']
                else:
                    sub = []
                    st, en = lo + dur * (ps / total), lo + dur * (pe / total)

                # 兜底钳制：浮点误差或极端数据下仍保证子块严格落在父段内且不重叠
                st = min(max(st, lo), hi)
                en = min(max(en, lo), hi)
                if en <= st:
                    st = min(lo, max(lo, hi - 0.04))
                    en = min(hi, st + 0.04)
                result.append({'start': st, 'end': en, 'text': ptxt, 'words': sub})
        return result

    def _combine_bilingual_segments(self, original_segments, translated_segments):
        width = max(10, int(os.environ.get('ASR_MAX_CUE_CHARS', '42')))
        return bilingual_segments(original_segments, translated_segments, getattr(self, '_layout_width', width),getattr(self,'_media_duration',None))

    def _translate_segments(self, segments, source_lang, target_lang, progress_callback=None, cancel_event=None):
        """翻译字幕片段（使用缓存的翻译模型）

        翻译加速优化（2026-06-02）：
        - batch_size 由 env TRANSLATE_BATCH_SIZE 控制（默认 24，6G 限额下调过 16 验证过 OOM）
        - torch.inference_mode() 代替 no_grad（省 view-tracking 内存）
        - generate 显式 num_beams=1（贪心）+ max_new_tokens=256 + early_stopping=True
        - 长度预过滤：>400 字符超长单句从批中拆出单独推理，避免拖累整批 padding
        """
        import torch

        # 线程调优（2026-08-30 基准）：容器 4 核配额下 torch 默认 6 线程过度订阅，
        # OpenMP 争抢反而慢（6.3 seg/s）；2 线程最快（实测 22-24 seg/s，3.5x+）。
        # whisper 走 ctranslate2 不受影响。TORCH_THREADS env 可覆盖，0=不调整。
        try:
            _torch_threads = int(os.environ.get('TORCH_THREADS', '2'))
        except ValueError:
            _torch_threads = 2
        if _torch_threads > 0:
            torch.set_num_threads(_torch_threads)

        if cancel_event and cancel_event.is_set():
            logger.info("翻译已被取消")
            return segments

        lang_pair = f"{source_lang}-{target_lang}"

        # 查找或构建模型名
        if lang_pair in self.TRANSLATION_MODELS:
            model_name = self.TRANSLATION_MODELS[lang_pair]
        else:
            model_name = f"Helsinki-NLP/opus-mt-{source_lang}-{target_lang}"

        # 使用缓存的翻译模型
        tokenizer, model = self._load_translation_model(model_name)

        # 批量翻译以提高效率
        owners, texts = [], []
        for index, seg in enumerate(segments):
            text = seg['text'].strip()
            if not text:
                continue
            clauses = [text]  # Keep comma clauses in the same translation context.
            for clause in clauses:
                if clause.strip():
                    owners.append(index)
                    texts.append(clause.strip())
        if not texts:
            return segments

        logger.info(f"开始翻译 {len(texts)} 个片段 {source_lang}->{target_lang}")
        translated_texts = []
        # 翻译加速：env 可覆盖，OOM 时一键回退（2026-08-30 基准：4 核配额下 batch 48 最优）
        batch_size = int(os.environ.get('TRANSLATE_BATCH_SIZE', '48'))
        LENGTH_THRESHOLD = 400
        # generate 显式参数：贪心解码 + 限制生成长度
        # max_length=None 显式覆盖 MarianMT config 默认的 max_length=512，避免 transformers 警告
        # early_stopping 在 transformers 4.41+ 已弃用，去掉
        GEN_KWARGS = {
            'num_beams': max(2, min(8, int(os.environ.get('TRANSLATE_BEAMS', '4')))),
            'no_repeat_ngram_size': 3,
            'early_stopping': True,
            'max_new_tokens': 256,
            'max_length': None,
            'length_penalty': 1.0,
        }
        total_batches = (len(texts) + batch_size - 1) // batch_size
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i+batch_size]
            # 长度预过滤：超长单句单独翻译，避免拖累整批 padding
            # 修复 BUG：原实现分别 extend(long) + extend(short) 改变了批内顺序，
            # 导致翻译结果与原句错位。现改为保留原顺序：按原 index 放回。
            long_ones = [t for t in batch if len(t) > LENGTH_THRESHOLD]
            short_ones = [t for t in batch if len(t) <= LENGTH_THRESHOLD]
            long_results = []
            short_results = []
            if long_ones:
                logger.debug(f"本批含 {len(long_ones)} 个超长单句，单独处理")
                # 注意：tokenizer 不传 max_length，避免与 generate 的 max_new_tokens 冲突触发 transformers 警告
                long_inputs = tokenizer(long_ones, return_tensors="pt", padding=True, truncation=True)
                with torch.inference_mode():
                    long_outputs = model.generate(**long_inputs, **GEN_KWARGS)
                long_results = [tokenizer.decode(t, skip_special_tokens=True) for t in long_outputs]
            if short_ones:
                # 注意：tokenizer 不传 max_length，避免与 generate 的 max_new_tokens 冲突触发 transformers 警告
                inputs = tokenizer(short_ones, return_tensors="pt", padding=True, truncation=True)
                with torch.inference_mode():
                    outputs = model.generate(**inputs, **GEN_KWARGS)
                short_results = [tokenizer.decode(t, skip_special_tokens=True) for t in outputs]
            # 按原 batch 顺序合并结果（长句/短句各取各的，保持原序）
            li = si = 0
            for t in batch:
                if len(t) > LENGTH_THRESHOLD:
                    translated_texts.append(long_results[li])
                    li += 1
                else:
                    translated_texts.append(short_results[si])
                    si += 1
            batch_num = i // batch_size + 1
            # 检查取消
            if cancel_event and cancel_event.is_set():
                logger.info("翻译已被用户取消")
                return segments
            # 翻译进度: 85%-99.5%，用浮点 + 节流到 0.1%，让前端进度条丝滑滑动
            # 最后一批不直接给 100%，留给"保存完成"那一刻给 100%，避免 100%→99.5% 回退
            if progress_callback:
                if batch_num == total_batches:
                    # 最后一批完成：给 99.5%，文案"翻译完成，正在保存..."让前端明确进入下一阶段
                    progress_callback(99.5, f'翻译完成 {total_batches}/{total_batches} 批次，正在保存字幕文件...')
                else:
                    translate_pct = 85 + (batch_num / total_batches) * 14.5  # 上限 99.5%
                    progress_callback(round(translate_pct, 1), f'翻译中 {source_lang}→{target_lang} ({batch_num}/{total_batches} 批次)')
            if batch_num % 10 == 0 or batch_num == total_batches or batch_num == 1:
                logger.info(f"翻译进度: {batch_num}/{total_batches} 批次")

        # 将翻译结果填回segments
        by_owner = {}
        for owner, source_text, text in zip(owners, texts, translated_texts):
            if not text.strip():
                raise ValueError('翻译模型返回空文本')
            by_owner.setdefault(owner, []).append({'source': source_text, 'target': text.strip()})
        for owner, texts_for_cue in by_owner.items():
            segments[owner]['text'] = ' '.join(p['target'] for p in texts_for_cue)
            segments[owner]['translation_parts'] = texts_for_cue
            segments[owner].pop('words', None)

        return segments

    def _save_subtitle_files(self, result, output_dir, video_name, format, output_naming="plain"):
        suffix = ''
        if output_naming == 'language' and result.get('language'):
            suffix = '.' + result['language']
        elif output_naming == 'bilingual' and result.get('original_language') and result.get('language'):
            suffix = '.' + result['original_language'] + '-' + result['language']
        stem, reservation = reserve_stem(output_dir, video_name + suffix)
        try:
            files = self._write_subtitle_files(result, output_dir, stem, format, 'plain')
            if result.get('source_media'):
                evidence_file=None
                if result.get('recognition_evidence'):
                    evidence_file=os.path.join(output_dir,stem+'.recognition.json')
                    atomic_json(evidence_file,result['recognition_evidence'])
                    files['recognition_evidence']=evidence_file
                write_metadata(next(iter(files.values())), result['source_media'], bilingual=bool(result['segments'] and result['segments'][0].get('bilingual')),asr_warnings=result.get('asr_warnings',[]),recognition_evidence=os.path.basename(evidence_file) if evidence_file else None,automatic_corrections=len((result.get('recognition_evidence') or {}).get('automatic_review',{}).get('changes',[])))

            return files
        finally:
            release_reservation(reservation)

    def _write_subtitle_files(self, result, output_dir, video_name, format, output_naming="plain"):
        """保存字幕文件"""
        output_files = {}
        suffix = ''
        if output_naming == 'language' and result.get('language'):
            suffix = f".{result['language']}"
        elif output_naming == 'bilingual' and result.get('original_language') and result.get('language'):
            suffix = f".{result['original_language']}-{result['language']}"
        stem = f"{video_name}{suffix}"

        # 只在请求JSON或全部格式时写入JSON
        if format in ("json", "all"):
            json_path = os.path.join(output_dir, f"{stem}.json")
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            output_files["json"] = json_path

        if format == "srt" or format == "all":
            srt_path = os.path.join(output_dir, f"{stem}.srt")
            self._save_srt(result["segments"], srt_path)
            output_files["srt"] = srt_path

        if format == "vtt" or format == "all":
            vtt_path = os.path.join(output_dir, f"{stem}.vtt")
            self._save_vtt(result["segments"], vtt_path)
            output_files["vtt"] = vtt_path

        if format == "txt" or format == "all":
            txt_path = os.path.join(output_dir, f"{stem}.txt")
            self._save_txt(result["segments"], txt_path)
            output_files["txt"] = txt_path

        if format == "ass" or format == "all":
            ass_path = os.path.join(output_dir, f"{stem}.ass")
            self._save_ass(result["segments"], ass_path)
            output_files["ass"] = ass_path

        # 词级时间戳旁挂文件：ASR_WORD_TIMESTAMPS 抓到的 words 只在内存里，
        # 页面一刷新就没了，而 /api/preview 读的是磁盘 SRT 也没有。
        # 这里额外落一份 <stem>.words.json，编辑器和前端可按需加载做逐词高亮。
        if os.environ.get('ASR_SAVE_WORDS', '1').strip().lower() not in ('0', 'false', 'no', ''):
            try:
                segs_with_words = [sg for sg in result.get("segments", []) if sg.get("words")]
                if segs_with_words:
                    words_path = os.path.join(output_dir, f"{stem}.words.json")
                    with open(words_path, 'w', encoding='utf-8') as f:
                        json.dump({
                            "version": 1,
                            "video": video_name,
                            "segments": [
                                {
                                    "start": sg.get("start"),
                                    "end": sg.get("end"),
                                    "text": sg.get("text"),
                                    "words": sg.get("words") or [],
                                }
                                for sg in result.get("segments", [])
                            ],
                        }, f, ensure_ascii=False)
                    output_files["words"] = words_path
            except Exception as e:  # 旁挂文件写失败不该影响主流程
                logger.warning(f"词级时间戳落盘失败（已忽略）: {e}")

        return output_files

    def _probe_duration(self, media_path):
        """用 ffprobe 取媒体时长（秒）。拿不到返回 0。"""
        try:
            import subprocess
            out = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', media_path],
                capture_output=True, text=True, timeout=120)
            val = (out.stdout or '').strip()
            return float(val) if val and val != 'N/A' else 0.0
        except Exception:
            return 0.0

    def _chunk_state_path(self, video_path, fingerprint):
        """断点文件路径。指纹变化即换文件，旧断点自然失效。"""
        import hashlib
        cache_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'asr_chunks')
        cache_dir = os.path.abspath(cache_dir)
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError:
            return None
        h = hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:20]
        stem = os.path.splitext(os.path.basename(video_path))[0][:40]
        safe = ''.join(c for c in stem if c.isalnum() or c in '-_') or 'media'
        return os.path.join(cache_dir, f"{safe}.{h}.json")

    def _extract_chunk_audio(self, media_path, start, dur, out_path):
        """用 ffmpeg 抽取指定时间段的音频（16kHz 单声道 wav）。失败返回 False。"""
        import subprocess
        try:
            r = subprocess.run(
                ['ffmpeg', '-v', 'error', '-y', '-ss', '%.3f' % start, '-t', '%.3f' % dur,
                 '-i', media_path, '-vn', '-ar', '16000', '-ac', '1',
                 '-c:a', 'pcm_s16le', out_path],
                capture_output=True, timeout=600)
            return r.returncode == 0 and os.path.isfile(out_path) \
                and os.path.getsize(out_path) > 1024
        except Exception as e:
            logger.warning(f"抽取音频失败 {start:.1f}s+{dur:.1f}s: {e}")
            return False

    @staticmethod
    def _coverage_state(rows, info):
        duration=float(getattr(info,'duration',0) or 0)
        speech_duration=float(getattr(info,'duration_after_vad',0) or duration)
        cursor=0.0;covered=0.0;gaps=[]
        for row in sorted(rows,key=lambda r:r['start']):
            start=max(0.0,float(row['start']));end=min(duration or float(row['end']),float(row['end']))
            if start>cursor:gaps.append((cursor,start))
            covered+=max(0.0,end-max(cursor,start));cursor=max(cursor,end)
        if duration>cursor:gaps.append((cursor,duration))
        suspect=bool(speech_duration>=15 and covered<speech_duration*.7 and any(b-a>=6 for a,b in gaps))
        return covered,suspect,gaps

    def _collect_transcription_checked(self, model, path, tune, language, prompt, cancel_event=None):
        """Detect likely alignment omissions and retry with decoder sentence times.

        VAD duration is only a diagnostic signal, not proof of speech. A retry
        must recover substantially more text and coverage to replace the draft.
        """
        last_pct=20.0
        def collect(options):
            nonlocal last_pct
            stream,info=self._transcribe_once(model,path,options,language,prompt)
            rows=[]
            for seg in stream:
                if cancel_event and cancel_event.is_set():return None,None
                rows.append(dict(start=seg.start,end=seg.end,text=seg.text,
                                 words=_words_to_list(getattr(seg,'words',None)),
                                 timing='word' if getattr(seg,'words',None) else 'segment'))
                duration=float(getattr(info,'duration',0) or 0)
                if self.progress_callback and duration>0:
                    pct=round(min(79,20+seg.end/duration*60),1)
                    if pct>last_pct+.1:
                        last_pct=pct;self.progress_callback(pct,'正在转录音频...')
            return rows,info
        rows,info=collect(tune)
        if rows is None:return None,None
        covered,suspect,gaps=self._coverage_state(rows,info)
        if suspect and tune.get('word_timestamps'):
            logger.warning('词级对齐疑似漏段：覆盖 %.1fs，空白区间 %s，重试句级识别',covered,gaps)
            if self.progress_callback:self.progress_callback(max(65,last_pct),'检测到疑似漏段，正在补转写...')
            fallback=dict(tune,word_timestamps=False)
            fallback.pop('hallucination_silence_threshold',None)
            recovered,recovered_info=collect(fallback)
            if recovered is None:return None,None
            new_covered,new_suspect,_=self._coverage_state(recovered,recovered_info)
            old_chars=sum(len(r['text'].strip()) for r in rows)
            new_chars=sum(len(r['text'].strip()) for r in recovered)
            repetitive=any(re.search(r'(.{2,20})(?:\1){3,}',re.sub(r'[\s，。,.!?！？]','',r['text'])) for r in recovered)
            if new_covered>=covered+3 and new_chars>=old_chars*1.15 and not repetitive:
                rows,info=recovered,recovered_info;suspect=new_suspect
                warning='词级对齐疑似漏段，已补转写；本次使用句级时间，细分字幕时间需校对'
                self._asr_warnings=list(getattr(self,'_asr_warnings',[]))+[warning]
        if suspect:
            warning='检测到较长语音覆盖空白，可能漏识别；请结合原视频核对，不能视为完整转写'
            self._asr_warnings=list(getattr(self,'_asr_warnings',[]))+[warning]
        return rows,info

    def _transcribe_once(self, model, audio_path, tune, language_param, zh_prompt):
        """对单个音频文件做一次常规转录，返回 (segment 生成器, info)。"""
        if language_param == 'zh' and zh_prompt:
            return model.transcribe(audio_path, language='zh', initial_prompt=zh_prompt, **tune)
        segments, info = model.transcribe(audio_path, language=language_param, **tune)
        # auto 模式：语言检测已完成（segments 惰性未解码，丢弃无浪费）。
        # 检出中文则带提示重新解码，换简体+标点输出。
        if language_param is None and info.language == 'zh' and zh_prompt:
            logger.info('检测到中文，注入简体/标点提示重新解码')
            segments, info = model.transcribe(audio_path, language='zh',
                                              initial_prompt=zh_prompt, **tune)
        return segments, info

    def _transcribe_with_chunks(self, model, video_path, tune, language_param, zh_prompt,
                                progress_callback=None, cancel_event=None):
        """长视频分片转录 + 断点续跑。短视频退化为单次转录，行为与原来一致。

        返回 (segments 列表, info)；用户取消时返回 (None, None)。
        """
        def _env_int(name, default):
            try:
                return int(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default

        chunk_sec = max(60, _env_int('ASR_CHUNK_SECONDS', 900))
        overlap = max(0, _env_int('ASR_CHUNK_OVERLAP', 3))
        duration = self._probe_duration(video_path)

        # 短片（或拿不到时长）→ 单次路径
        if duration <= chunk_sec * 1.5:
            return self._collect_transcription_checked(model,video_path,tune,language_param,zh_prompt,cancel_event)

        # ---- 分片 ----
        try:
            st = os.stat(video_path)
            fingerprint = json.dumps({
                'version': 3, 'source': os.path.realpath(video_path),
                'mtime_ns': st.st_mtime_ns, 'size': st.st_size,
                'model': self.model_size, 'device': self.device,
                'tune': tune, 'language': language_param, 'prompt': zh_prompt,
                'chunk_seconds': chunk_sec, 'overlap': overlap,
            }, sort_keys=True, ensure_ascii=False)
        except OSError:
            st, fingerprint = None, None
        state_file = self._chunk_state_path(video_path, fingerprint) if fingerprint else None
        state = {'chunks': {}}
        if state_file and os.path.isfile(state_file):
            try:
                with open(state_file, encoding='utf-8') as f:
                    loaded = json.load(f)
                if loaded.get('fingerprint') == fingerprint:
                    state = loaded
                    logger.info(f"命中转录断点: 已完成 {len(state['chunks'])}/{_n_chunks(duration, chunk_sec)} 片")
                else:
                    logger.info("转录断点参数不匹配，重新开始")
            except Exception as e:
                logger.warning(f"读取断点失败（忽略）: {e}")

        n = _n_chunks(duration, chunk_sec)
        logger.info(f"长视频分片转录: 总时长 {duration/60:.1f} 分钟, {n} 片 "
                    f"(每片 {chunk_sec}s, 重叠 {overlap}s), 断点={state_file or '无'}")
        info = None
        merged = []
        for idx in range(n):
            if cancel_event and cancel_event.is_set():
                return None, None
            cs = idx * chunk_sec
            ce = min(duration, (idx + 1) * chunk_sec + overlap)
            done = state['chunks'].get(str(idx))
            if str(idx) in state['chunks']:
                merged.extend(done)
                if progress_callback:
                    progress_callback(20 + (idx + 1) / n * 60,
                                      f'转录中… 复用断点 {idx+1}/{n} 片')
                continue

            t0 = time.time()
            # 物理切片：ffmpeg 抽出本段音频，单独转录，再把时间戳平移回全局。
            # 不用 clip_timestamps——实测那会让输出转成繁体并在边界截断半句。
            import tempfile
            audio_dir = os.path.dirname(state_file) if state_file else tempfile.gettempdir()
            fd, chunk_audio = tempfile.mkstemp(prefix='chunk-', suffix='.wav', dir=audio_dir)
            os.close(fd)
            if not self._extract_chunk_audio(video_path, cs, ce - cs, chunk_audio):
                try:
                    os.remove(chunk_audio)
                except OSError:
                    pass
                raise RuntimeError(f'第 {idx+1}/{n} 片抽取音频失败（{cs:.0f}-{ce:.0f}s），任务未完成，请重试')
            try:
                segments, cinfo = self._collect_transcription_checked(
                    model, chunk_audio, tune, language_param, zh_prompt,cancel_event)
                if segments is None:return None,None
                if info is None:
                    info = cinfo
                state['info'] = {'language': cinfo.language, 'duration': duration}
                part = []
                for seg in segments:
                    if cancel_event and cancel_event.is_set():
                        return None, None
                    part.append({
                        # 平移回全局时间轴
                        'start': seg['start'] + cs, 'end': seg['end'] + cs, 'text': seg['text'],
                        'words': [dict(w, start=w['start'] + cs, end=w['end'] + cs)
                                  for w in seg.get('words',[])],
                        'timing':seg.get('timing','segment'),
                    })
            finally:
                try:
                    os.remove(chunk_audio)
                except OSError:
                    pass
            state['chunks'][str(idx)] = part
            merged.extend(part)
            # 每片完成立刻落盘：崩了/被抢占都只重跑未完成的那片
            if state_file:
                try:
                    atomic_json(state_file, {'fingerprint': fingerprint,
                                             'chunks': state['chunks'],
                                             'info': state.get('info', {})})
                except Exception as e:
                    logger.warning(f"断点写入失败（继续）: {e}")
            logger.info(f"第 {idx+1}/{n} 片完成 ({cs:.0f}-{ce:.0f}s, "
                        f"{len(part)} 片段, {time.time()-t0:.1f}s)")
            if progress_callback:
                progress_callback(20 + (idx + 1) / n * 60,
                                  f'转录中… {idx+1}/{n} 片 ({len(merged)} 片段)')

        # 去掉重叠区里被两片各写一份的重复段
        deduped = []
        for seg in merged:
            if deduped and seg['start'] < deduped[-1]['end'] - 0.25:
                continue
            deduped.append(seg)
        if len(deduped) != len(merged):
            logger.info(f"分片重叠去重: {len(merged)} -> {len(deduped)} 片段")
        if info is None:
            info = type('Info', (), {'language': state.get('info', {}).get('language') or language_param or 'unknown',
                                      'duration': duration})()
        return deduped, info

    def _save_srt(self, segments, output_path):
        """保存为SRT格式"""
        with open(output_path, "w", encoding="utf-8") as f:
            for i, segment in enumerate(segments, 1):
                start_time = self._format_timestamp(segment["start"])
                end_time = self._format_timestamp(segment["end"])
                text = segment["text"].strip()

                f.write(f"{i}\n")
                f.write(f"{start_time} --> {end_time}\n")
                f.write(f"{text}\n\n")

    def _save_vtt(self, segments, output_path):
        """保存为VTT格式"""
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("WEBVTT\n\n")
            for i, segment in enumerate(segments):
                start_time = self._format_timestamp_vtt(segment["start"])
                end_time = self._format_timestamp_vtt(segment["end"])
                text = segment["text"].strip()

                f.write(f"{i+1}\n")
                f.write(f"{start_time} --> {end_time}\n")
                f.write(f"{text}\n\n")

    def _save_txt(self, segments, output_path):
        """保存为纯文本格式"""
        with open(output_path, "w", encoding="utf-8") as f:
            for seg in segments:
                text = seg["text"].strip()
                if text:
                    f.write(text + "\n")

    def _save_ass(self, segments, output_path):
        """保存为ASS格式"""
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("[Script Info]\nScriptType: v4.00+\nPlayResX: 1280\nPlayResY: 720\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n")
            f.write("[V4+ Styles]\n")
            f.write("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, ")
            f.write("Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, ")
            f.write("Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n")
            f.write("Style: Default,Noto Sans CJK SC,24,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,")
            f.write("100,100,0,0,1,2,0,2,30,30,28,1\n\n")
            f.write("[Events]\n")
            f.write("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
            for segment in segments:
                start_time = self._format_timestamp_ass(segment["start"])
                end_time = self._format_timestamp_ass(segment["end"])
                text = segment["text"].strip().replace("\n", r"\N")
                f.write(f"Dialogue: 0,{start_time},{end_time},Default,,0,0,0,,{text}\n")

    def _format_timestamp(self, seconds):
        """格式化时间戳为SRT格式"""
        total = max(0,int(round(seconds*1000)))
        hours, remainder = divmod(total,3600000)
        minutes, remainder = divmod(remainder,60000)
        secs, millisecs = divmod(remainder,1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millisecs:03d}"

    def _format_timestamp_vtt(self, seconds):
        """格式化时间戳为VTT格式"""
        total = max(0,int(round(seconds*1000)))
        hours, remainder = divmod(total,3600000)
        minutes, remainder = divmod(remainder,60000)
        secs, millisecs = divmod(remainder,1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millisecs:03d}"

    def _format_timestamp_ass(self, seconds):
        """格式化时间戳为ASS格式"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        centisecs = int((seconds - int(seconds)) * 100)
        return f"{hours}:{minutes:02d}:{secs:02d}.{centisecs:02d}"
