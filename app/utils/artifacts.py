"""Versioned subtitle bundles, atomic metadata and final text layout."""
import json
import os
import re
import tempfile
import threading
import unicodedata
import jieba

jieba.setLogLevel(30)

BUNDLE_LOCK = threading.RLock()
FORMATS = ('srt', 'vtt', 'ass', 'json', 'txt', 'words.json')


def atomic_json(path, data):
    fd, tmp = tempfile.mkstemp(prefix='.write-', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def metadata_path(path):
    return os.path.join(os.path.dirname(path), '.' + os.path.splitext(os.path.basename(path))[0] + '.media.json')


def read_metadata(path):
    try:
        with open(metadata_path(path), encoding='utf-8') as inp:
            return json.load(inp)
    except (OSError, ValueError):
        return {}


def write_metadata(path, source, **extra):
    atomic_json(metadata_path(path), dict(source_media=os.path.realpath(source), **extra))


def copy_metadata(source, target):
    data = read_metadata(source)
    if data:
        atomic_json(metadata_path(target), data)


def reserve_stem(folder, stem):
    """Reserve the complete bundle, including across processes; never overwrite."""
    os.makedirs(folder, exist_ok=True)
    with BUNDLE_LOCK:
        number = 1
        while True:
            name = stem if number == 1 else f'{stem}_v{number}'
            lock = os.path.join(folder, '.' + name + '.reserve')
            if any(os.path.lexists(os.path.join(folder, name + '.' + ext)) for ext in FORMATS):
                number += 1
                continue
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                return name, lock
            except FileExistsError:
                number += 1


def release_reservation(lock):
    try:
        os.unlink(lock)
    except FileNotFoundError:
        pass


def wrap_text(text, width=42):
    """Break at whitespace when possible, hard-wrap unspaced CJK/long tokens."""
    lines = []
    for paragraph in str(text).splitlines():
        paragraph = paragraph.strip()
        while len(paragraph) > width:
            cut = paragraph.rfind(' ', 0, width + 1)
            if cut < width // 2:
                cut = width
            lines.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        if paragraph:
            lines.append(paragraph)
    return lines


def layout_segments(segments, width=42, max_lines=2):
    """Layout after translation/labels. Split times remain inside the parent cue."""
    out = []
    for seg in segments:
        if seg.get('bilingual'):
            out.append(dict(seg))
            continue
        lines = wrap_text(seg.get('text', ''), width)
        if not lines:
            continue
        groups = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]
        weight = sum(len(''.join(g)) for g in groups)
        cursor = float(seg['start'])
        duration = float(seg['end']) - cursor
        for i, group in enumerate(groups):
            end = float(seg['end']) if i == len(groups) - 1 else cursor + duration * len(''.join(group)) / max(weight, 1)
            item = dict(seg, start=cursor, end=end, text='\n'.join(group))
            if len(groups) > 1:
                item.pop('words', None)  # Proportional splits are not word alignment.
            out.append(item)
            cursor = end
    return out


def readable_monolingual_segments(segments, media_duration=None, width=42):
    """Join touching spoken clauses and spend only real trailing silence.

    Genuine word times remain intact. Never move the onset, clip an aligned
    word, or extend the final cue past the source media.
    """
    out=[]
    for item in segments:
        cue=dict(item)
        if out and -.02<=cue['start']-out[-1]['end']<.08:
            prev=out[-1];left=prev['text'].replace('\n','');right=cue['text'].replace('\n','')
            text=left+('' if re.search(r'[\u3400-\u9fff]',left+right) else ' ')+right
            span=cue['end']-prev['start'];cps=len(text)/max(span,.01)*(2 if re.search(r'[\u3400-\u9fff]',text) else 1)
            if len(text)<=width*2 and span<=9 and cps<=18:
                words=(prev['words']+cue['words']) if prev.get('words') and cue.get('words') else []
                out[-1]=dict(prev,end=cue['end'],text='\n'.join(_balanced_lines(text,max(1,(len(text)+width-1)//width),width)),words=words)
                continue
        out.append(cue)
    for i,cue in enumerate(out):
        cap=out[i+1]['start']-.08 if i+1<len(out) else float(media_duration or cue['end'])
        text=cue['text'].replace('\n','');rate=9 if re.search(r'[\u3400-\u9fff]',text) else 18
        need=max(1.2,len(text)/rate)
        if cap>cue['end']:cue['end']=min(cap,max(cue['end'],cue['start']+need))
    return out


def _balanced_lines(text, count, width):
    """Balanced safe token boundaries; never create a 43-char line by word extension."""
    text = str(text).replace('\n', ' ').strip()
    tokens = []
    for token in re.findall(r"[\u3400-\u9fff]+|[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*|\s+|[^A-Za-z0-9\s]", text):
        tokens.extend(jieba.lcut(token, HMM=False) if re.fullmatch(r'[\u3400-\u9fff]+', token) else [token])
    tokens = [part for token in tokens for part in
              ([token[i:i+width] for i in range(0,len(token),width)] if len(token)>width else [token])]
    positions = [0]
    for token in tokens:
        positions.append(positions[-1]+len(token))
    target = len(text)/max(count,1)
    dp = {(0,0):(0,[])}
    for k in range(1,count+1):
        for end in range(1,len(tokens)+1):
            best = None
            for begin in range(end-1,-1,-1):
                part = text[positions[begin]:positions[end]].strip()
                if len(part)>width:
                    break
                previous = dp.get((k-1,begin))
                if not previous or not part:
                    continue
                cost = previous[0] + (len(part)-target)**2
                if part[-1:] in '，。！？,.;!?':
                    cost -= 5
                if part[:1] in '，。！？,.;!?':
                    cost += 40
                if best is None or cost<best[0]:
                    best=(cost,previous[1]+[part])
            if best is not None:
                dp[(k,end)] = best
    result = dp.get((count,len(tokens)))
    if result:
        return result[1]
    return wrap_text(text,width)+['']*max(0,count-len(wrap_text(text,width)))


def display_width(text):
    return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in str(text))


def wrap_display(text, width=42):
    """CJK counts as two cells. Prefer whole words and Chinese lexical tokens."""
    tokens = []
    for token in re.findall(r"[\u3400-\u9fff]+|[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*|\s+|[^A-Za-z0-9\s]", str(text).replace('\n', ' ')):
        tokens.extend(jieba.lcut(token, HMM=False) if re.fullmatch(r'[\u3400-\u9fff]+', token) else [token])
    lines, line = [], ''
    for token in tokens:
        # An unusually long unbroken identifier must still fit on screen.
        pieces = [token]
        if display_width(token) > width:
            pieces = list(token)
        for piece in pieces:
            if line and display_width(line + piece) > width:
                lines.append(line.strip()); line = ''
            line += piece.lstrip() if not line else piece
    if line.strip(): lines.append(line.strip())
    if len(lines)>1 and re.fullmatch(r'[。！？；，,.!?;:：]+',lines[-1]):
        tail = jieba.lcut(lines[-2],HMM=False)[-1]
        lines[-2]=lines[-2][:-len(tail)].rstrip();lines[-1]=tail+lines[-1]
    if len(lines)==2:
        # Balance two lines at lexical boundaries instead of leaving a lone
        # Chinese character or final English word on the second line.
        best=None
        for cut in range(1,len(tokens)):
            left=''.join(tokens[:cut]).strip();right=''.join(tokens[cut:]).strip()
            if not left or not right or max(display_width(left),display_width(right))>width:continue
            cost=abs(display_width(left)-display_width(right))
            if re.search(r'[。！？.!?]$',left):cost-=12
            elif re.search(r'[，,；;]$',left):cost-=4
            if re.search(r'[的和与又]$',left):cost+=20
            if best is None or cost<best[0]:best=(cost,[left,right])
        if best:lines=best[1]
    return [line for line in lines if line]


def semantic_segments(segments, width=84, max_seconds=9.0, target_lang=None):
    """Use complete phrases and source word times, not character-ratio pairing.

    DP favours punctuation/conjunctions and avoids cutting after articles,
    prepositions or inside named phrases. Missing word alignment is disclosed.
    """
    units=[];native_boundaries=set()
    for seg in segments:
        text=str(seg.get('text','')).strip()
        if not text:continue
        words=seg.get('words') or []
        joined=''.join(str(w.get('word',w.get('text',''))) for w in words)
        valid=bool(words and re.sub(r'\s+','',joined)==re.sub(r'\s+','',text))
        if valid:
            items=[(str(w.get('word',w.get('text',''))),float(w['start']),float(w['end']),'word') for w in words]
        else:
            tokens=re.findall(r'\s*\S+',text) if re.search('[A-Za-z]',text) and ' ' in text else jieba.lcut(text,HMM=False)
            length=max(sum(len(t) for t in tokens),1);cursor=float(seg['start']);duration=float(seg['end'])-cursor;items=[]
            for token in tokens:
                end=cursor+duration*len(token)/length;items.append((token,cursor,end,'estimated'));cursor=end
        if units and items and re.search('[A-Za-z]',items[0][0]) and not items[0][0].startswith(' '):
            t,a,b,k=items[0];items[0]=(' '+t,a,b,k)
        units.extend(items)
        native_boundaries.add(len(units))
    full_text=''.join(u[0] for u in units)
    cjk=bool(re.search(r'[\u3400-\u9fff]',full_text))
    # Chinese expands when translated into Latin-script subtitles. Plan shorter
    # source phrases before translation instead of overflowing or truncating it.
    if cjk and target_lang in {'en','fr','de','es','it','pt'}:
        width=min(width,56)
    offsets=[0]
    for u in units:offsets.append(offsets[-1]+len(u[0]))
    inside_word=set()
    if cjk:
        for token,start,end in jieba.tokenize(full_text,HMM=False):
            if re.search(r'[\u3400-\u9fff]',token):
                inside_word.update(range(start+1,end))
    n=len(units);dp={n:(0,[])}
    dangling={'a','an','the','to','for','of','on','in','with','and','or','is','are','be','will','can','my','your','our','that','this'}
    starts={'and','but','when','while','as','for','to','with','so'}
    for i in range(n-1,-1,-1):
        text='';best=None
        for j in range(i,n):
            if j>i and units[j][1]-units[j-1][2]>.7:
                if units[j][1]-units[j-1][2]>1.4 or not re.search(r'(一道|一个|一种|一条|以来|情况下|前提下)$',text.strip()):break
            text+=units[j][0];duration=units[j][2]-units[i][1]
            if display_width(text.strip())>width or duration>max_seconds:break
            if len(wrap_display(text.strip()))>2:continue
            following=dp.get(j+1)
            if following is None:continue
            cost=6+((duration-4.0)/3)**2
            if j+1 in native_boundaries:cost-=2
            if cjk and target_lang in {'en','fr','de','es','it','pt'}:
                # Already-readable source cues are valuable sentence boundaries.
                # Merging several such clauses forces faithful English into one
                # two-line screen. Short fragments still pay the penalty below.
                if j+1 in native_boundaries:cost-=6
                pass  # Keep complete modifiers; a fixed shorter budget split noun phrases.
                # Preserve clause boundaries so independent phrases do not grow
                # into a long literal translation. Keep lexical anchors intact.
                cost+=2*len(re.findall(r'[。！？，,;；.!?]',text.strip().rstrip('。！？，,;；.!?')))
                cost+=8*len(re.findall(r'(我要|经过|拥有)',text.strip()[2:]))
            if duration<1.2:cost+=40*(1.2-duration)+15
            last=units[j][0].strip().lower().strip(',.!?;:')
            if last in dangling:cost+=15
            if cjk and j+1<n:
                if offsets[j+1] in inside_word:continue
                following_text=''.join(u[0] for u in units[j+1:j+8]).strip()
                if re.match(r'(的|了|着|过|之|以来|能听|得能|可以|应该|才能|还有)',following_text):cost+=50
                if re.search(r'(?:在|把|让|向|对)[你我他她它]$',text.strip()):cost+=50
                if len(text.strip())>1 and re.search(r'(?:以来|之后|以后|然后|但是|不过|所以|因为|而|但)[我你他她它]$',text.strip()) and re.match(r'[\u3400-\u9fff]',following_text):cost+=50
                if re.search(r'(最|更|非常|特别|很)$',text.strip()) and re.match(r'[\u3400-\u9fff]',following_text):cost+=50
                if re.search(r'(?:必须|想|要|得)说$',text.strip()) and re.match(r'[这那]是',following_text):cost-=12
                if re.search(r'(目标|水平|能力|差距|原因|问题|结果|方法|答案)$',text.strip()) and re.match(r'(是|为|包括)',following_text):cost+=50
                if re.search(r'(将用|要用)[^。！？.!?]{1,15}$',text.strip()) and re.match(r'(挑战|完成|展示|解决|证明|验证)',following_text):cost+=50
                if re.search(r'(至少|同时|因为|所以|比如|也就是说|这就是说|再加|一个|两个|一道|一种|一条|那应该|的话|情况下|前提下|以来|目前|X|Y)$',text.strip()):cost+=50
                if re.search(r'(加入|看到|成为|属于|使用|需要|确定|测量|提供|考虑|认为|发现|知道|理解|告诉)$',text.strip()) and re.search(r'[\u3400-\u9fff]',following_text):cost+=50
                if re.match(r'(我要|还有|经过|拥有|打破|赶超|挑战|然后|也就是说|实际上|其实|但|所以)',following_text):cost-=4
                if re.match(r'(经过|拥有|我们|今天|它将|我要|打破|赶超|挑战|还有|是中国)',following_text):cost-=3
                if re.search(r'(的|和|与|是|有|将|把|在|为|让|我们|这个|那个|它的|它将)$',text.strip()):cost+=12
            if re.search(r'[。！？.!?]$',text.strip()):cost-=2
            elif re.search(r'[，,；;]$',text.strip()):cost-=.8
            if j+1<n:
                next_word=units[j+1][0].strip().lower().strip(',.!?;:')
                if next_word in starts:cost-=.5
                if (last,next_word) in {('back','to'),('study','with'),('bring','with'),('mission','to'),('ready','for'),('start','out'),('going','to'),('came','time'),('get','ready')} :cost+=30
            candidate=(cost+following[0],[(i,j+1)]+following[1])
            if best is None or candidate[0]<best[0]:best=candidate
            if cjk and target_lang and re.search(r'[。！？.!?]$',text.strip()):break
        if best is not None:dp[i]=best
    if not n:return []
    if 0 not in dp:
        # Preserve exceptional long words / long silence spans for review.
        return [dict(seg,timing='estimated') for seg in segments if str(seg.get('text','')).strip()]
    out=[]
    for i,j in dp[0][1]:
        group=units[i:j]
        out.append(dict(start=group[0][1],end=group[-1][2],text=''.join(u[0] for u in group).strip(),
                        words=[dict(text=u[0],start=u[1],end=u[2]) for u in group] if all(u[3]=='word' for u in group) else [],
                        timing='word' if all(u[3]=='word' for u in group) else 'estimated'))
    return out


def bilingual_segments(original, translated, width=42, media_duration=None):
    """Each language gets up to two lines; retain semantic source/target pairs.

    Overflow is preserved and reported, never re-paired by character ratios.
    """
    if len(original) != len(translated):
        raise ValueError('Translation count differs from source')
    out = []
    for src, dst in zip(original, translated):
        source = wrap_display(src.get('text', ''), width)
        target = wrap_display(dst.get('text', ''), width)
        out.append(dict(start=float(src['start']), end=float(src['end']), text='\n'.join(source+target),
                        bilingual=True, source_text='\n'.join(source), target_text='\n'.join(target),
                        source_lines=len(source), target_lines=len(target), timing=src.get('timing', 'source')))
    # Merge intact neighbouring pairs when a fast short cue can become readable
    # without overflowing either language. Never split/re-pair translated text.
    def rate(text,duration):
        return len(text.replace('\n',''))/max(duration,.001)*(2 if re.search(r'[\u3400-\u9fff]',text) else 1)
    merged=[]
    for cue_index,cue in enumerate(out):
        if merged:
            prev=merged[-1];native_duration=cue['end']-prev['start']
            cap_after=out[cue_index+1]['start']-.08 if cue_index+1<len(out) else (media_duration or cue['end'])
            duration=min(7,max(native_duration,float(cap_after)-prev['start']))
            fast=max(rate(prev['source_text'],prev['end']-prev['start']),rate(prev['target_text'],prev['end']-prev['start']),rate(cue['source_text'],cue['end']-cue['start']),rate(cue['target_text'],cue['end']-cue['start']))>18
            def join_language(key):
                left=prev[key].replace('\n','' if re.search(r'[\u3400-\u9fff]',prev[key]) else ' ')
                right=cue[key].replace('\n','' if re.search(r'[\u3400-\u9fff]',cue[key]) else ' ')
                return left+('' if re.search(r'[\u3400-\u9fff]',left+right) else ' ')+right
            source=join_language('source_text');target=join_language('target_text')
            sl=wrap_display(source,width);tl=wrap_display(target,width)
            if fast and -.02<=cue['start']-prev['end']<=.3 and native_duration<=7 and len(sl)<=2 and len(tl)<=2 and max(rate(source,duration),rate(target,duration))<=18:
                merged[-1]=dict(prev,end=cue['end'],text='\n'.join(sl+tl),source_text='\n'.join(sl),target_text='\n'.join(tl),source_lines=len(sl),target_lines=len(tl),timing='merged original intervals')
                continue
        merged.append(cue)
    out=merged
    for i, cue in enumerate(out):
        # Spend available silence to make short captions readable, never delay onset.
        cap = out[i+1]['start']-.08 if i+1<len(out) else float(media_duration or cue['end'])
        source_rate = 9 if re.search(r'[\u3400-\u9fff]',cue['source_text']) else 18
        target_rate = 9 if re.search(r'[\u3400-\u9fff]',cue['target_text']) else 18
        need = max(1.2, len(cue['source_text'].replace('\n',''))/source_rate, len(cue['target_text'].replace('\n',''))/target_rate)+.02
        # A brief discourse cue may have no trailing silence but a real
        # preceding pause. Allow at most 250ms of caption lead, without
        # crossing the previous cue, rather than flashing it for <1.2s.
        earliest=max(0.0,cue['start']-.25,out[i-1]['end']+.08 if i else 0.0)
        if cap-cue['start']<1.22 and cap-earliest>=1.22:
            cue['start']=max(earliest,cap-1.22)
            cue['timing']='source interval with <=250ms lead in preceding silence'
        cue['end'] = max(cue['start']+.001, min(max(cue['end'],cue['start']+need), cap))
    return out
