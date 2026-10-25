"""Conservative Chinese spelling evidence from the input video's own text.

No transcript, filename, programme metadata or clip-specific substitutions are
used. Audio remains primary: a repeated visual phrase may only amend a nearby,
mostly matching, same-length audio span. Never substitute a whole OCR caption.
"""
import collections
import difflib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unicodedata


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKC',str(text or '')) if c.isalnum())


def canonical(text):
    text=normalize(text)
    digits=dict(zip('零一二三四五六七八九','0123456789'))
    return re.sub(r'[零一二三四五六七八九](?=个|颗|科|条|项|秒|分|次|种)',lambda m:digits[m.group()],text)

def stable_phrases(frames):
    seen=collections.defaultdict(list)
    for frame in frames:
        for line in frame.get('lines',[]):
            text=canonical(line.get('text',''));score=float(line.get('score',0));engine=line.get('engine','single')
            if len(text)>=2 and re.search(r'[\u3400-\u9fff]',text) and score>=.94:
                seen[text].append((float(frame['time']),score,engine))
    phrases=[]
    for text,hits in seen.items():
        times=sorted(set(t for t,_,_ in hits));votes={}
        for engine in set(e for _,_,e in hits):
            ticks=sorted(set(t for t,_,e in hits if e==engine))
            if ticks:votes[engine]=ticks
        score=sum(s for _,s,_ in hits)/len(hits)
        brief=bool(len(times)==1 and len(votes)>=2 and score>=.985 and (len(text)>=4 or re.match(r'^\d[颗个条项次]$',text)))
        if (len(times)>=2 and times[-1]-times[0]>=.3) or brief:
            phrases.append(dict(text=text,times=times,score=score,engine_votes=votes,support=len(times),single_frame_consensus=brief))
    phrases=sorted(phrases,key=lambda p:p['times'][0]);joined=[]
    for a in phrases:
        for b in phrases:
            if .05<=b['times'][0]-a['times'][-1]<=1.5 and b['times'][-1]-a['times'][0]<=10 and len(a['text']+b['text'])<=48:
                votes={e:sorted(set(a['engine_votes'][e]+b['engine_votes'][e])) for e in set(a['engine_votes'])&set(b['engine_votes'])}
                joined.append(dict(text=a['text']+b['text'],times=sorted(set(a['times']+b['times'])),score=min(a['score'],b['score']),engine_votes=votes,support=min(a['support'],b['support'])))
    return phrases+joined


def _phonetic(a,b):
    if a.casefold()==b.casefold():return True
    try:
        from pypinyin import lazy_pinyin
        letters={'a':'ei','b':'bi','c':'xi','d':'di','e':'yi','g':'ji','i':'ai','k':'kei','o':'ou','p':'pi','q':'qiu','t':'ti','u':'you','v':'wei','y':'wai','z':'zi'}
        pa=letters.get(a.casefold(),lazy_pinyin(a)[0]);pb=letters.get(b.casefold(),lazy_pinyin(b)[0])
        return pa==pb or (pa.rstrip('g')==pb.rstrip('g') and (pa.endswith('ng') or pb.endswith('ng')))
    except ImportError:return False


def correct_segments(segments,frames):
    """Keep audio's words/times; admit only bounded substitutions, with an audit."""
    phrases=stable_phrases(frames);rows=[];changes=[]
    for index,seg in enumerate(segments):
        row=dict(seg);text=row['text'];offsets=[i for i,c in enumerate(text) if normalize(c)]
        plain=canonical(text);start=float(row['start']);end=float(row['end'])
        proposals={};votes=collections.defaultdict(set);agreements=set()
        for phrase in phrases:
            times=[t for t in phrase['times'] if start-.8<=t<=end+.8]
            if len(times)<2 and not (times and phrase.get('single_frame_consensus')):continue
            target=phrase['text'];n=len(target)
            # Match only a complete visual phrase to a same-length part of audio.
            if n>len(plain) or n<2:continue
            for pos in range(len(plain)-n+1):
                original=plain[pos:pos+n]
                different=[k for k,(a,b) in enumerate(zip(original,target)) if a!=b]
                if len(different)>3:continue
                # Numbers are content, never silently corrected from picture text.
                if any(original[k].isdigit() or target[k].isdigit() for k in different):continue
                # Both model agreement and agreement with the existing audio
                # spelling are collected before choosing any amendment.
                if n>=4 and len(different)<=max(1,n//4):
                    nearby_engines={e for e,ticks in phrase.get('engine_votes',{}).items() if sum(start-.8<=t<=end+.8 for t in ticks)>=(1 if phrase.get('single_frame_consensus') else 2)}
                    for k,char in enumerate(target):
                        votes[(pos+k,char)].update(nearby_engines)
                        if char==original[k]:agreements.add(pos+k)
                if not different:continue
                nonphonetic=[k for k in different if not _phonetic(original[k],target[k])]
                if len(different)>max(1,min(3,n//4)) and not (not nonphonetic and n>=4 and len(different)<=2 and phrase['score']>=.98):continue
                # A single nonhomophone needs a long near-exact phrase, repeated
                # three times; other changes in it must still be homophones.
                if nonphonetic and not (n>=8 and len(nonphonetic)==1 and len(times)>=3 and phrase['score']>=.965):continue
                if n<4 and not (not nonphonetic and re.match(r'^\d[颗个条项次]$',target) and phrase['score']>=.98):continue
                # Latin-name spellings need the longer audio anchor as well.
                if any(target[k].isascii() or original[k].isascii() for k in different) and n<8 and not (n>=5 and not nonphonetic and len(different)==1):continue
                quality=(int(not nonphonetic),min(len(times),phrase.get('support',len(times))),n-len(different),phrase['score'])
                for k in different:
                    slot=pos+k;item=(target[k],quality,phrase)
                    old=proposals.get(slot)
                    if old is None or quality>old[1]:proposals[slot]=item
                    elif old[0]!=target[k] and quality==old[1]:proposals[slot]=('CONFLICT',quality,phrase)
        revised=list(text);accepted=[]
        for slot,(char,quality,phrase) in sorted(proposals.items()):
            if char=='CONFLICT' or slot>=len(offsets) or slot in agreements:continue
            if not _phonetic(plain[slot],char) and len(votes[(slot,char)])<2:continue
            at=offsets[slot];revised[at]=char
            accepted.append(dict(position=slot,before=plain[slot],after=char,visual_phrase=phrase['text'],visual_times=phrase['times'],confidence=round(phrase['score'],4)))
        corrected=''.join(revised)
        if corrected!=text:
            row['text']=corrected;row['text_evidence']='audio+repeated-video-text'
            # Same-length substitutions retain genuine audio word boundaries.
            words=row.get('words') or [];joined=''.join(w.get('text',w.get('word','')) for w in words)
            if canonical(joined)==plain:
                out=[];cursor=0;corrected_plain=normalize(corrected)
                for w in words:
                    ww=dict(w);key='text' if 'text' in ww else 'word';chars=list(ww.get(key,''))
                    for j,c in enumerate(chars):
                        if normalize(c):
                            chars[j]=corrected_plain[cursor];cursor+=1
                    if ''.join(chars)!=ww.get(key,''):ww['original_text']=ww.get(key,'')
                    ww[key]=''.join(chars);out.append(ww)
                row['words']=out
            elif words:
                row['words']=[];row['timing']='segment'
            changes.append(dict(segment=index,start=start,end=end,before=text,after=corrected,evidence=accepted))
        rows.append(row)
    names={}
    for phrase in phrases:
        for match in re.finditer(r'([\u4e00-\u9fff])([A-Za-z][A-Za-z0-9_-]*)',phrase['text']):
            name=match.group()
            if name[0] in '个为加是或选对在用说和与':continue
            if any(name in row['text'] for row in rows):names[name]=phrase
    for index,row in enumerate(rows):
        old_text=row['text'];new_text=old_text;name_evidence=[]
        for name,phrase in names.items():
            size=len(name)
            for pos in range(len(new_text)-size+1):
                variant=new_text[pos:pos+size]
                if variant!=name and variant[0]==name[0] and all(_phonetic(a,b) for a,b in zip(variant,name)):
                    new_text=new_text[:pos]+name+new_text[pos+size:]
                    name_evidence.append(dict(before=variant,after=name,method='same-pronunciation-established-name',visual_phrase=phrase['text'],visual_times=phrase['times'],confidence=round(phrase['score'],4)))
        if new_text!=old_text:
            row['text']=new_text;row['text_evidence']='audio+established-video-name'
            words=row.get('words') or [];joined=''.join(w.get('text',w.get('word','')) for w in words)
            if normalize(joined)==normalize(old_text):
                revised=normalize(new_text);cursor=0;out=[]
                for w in words:
                    ww=dict(w);key='text' if 'text' in ww else 'word';chars=list(ww.get(key,''))
                    for j,char in enumerate(chars):
                        if normalize(char):chars[j]=revised[cursor];cursor+=1
                    if ''.join(chars)!=ww.get(key,''):ww['original_text']=ww.get(key,'')
                    ww[key]=''.join(chars);out.append(ww)
                row['words']=out
            else:row['words']=[];row['timing']='segment'
            existing=next((c for c in changes if c['segment']==index),None)
            if existing:existing['after']=new_text;existing['evidence'].extend(name_evidence)
            else:changes.append(dict(segment=index,start=row['start'],end=row['end'],before=old_text,after=new_text,evidence=name_evidence))
    # Learn complete dictionary terms from accepted visual corrections, then
    # resolve the same homophonic term in other turns. Never propagate single
    # pronouns or ordinary location words; nearby contradictory text vetoes it.
    learned={}
    import jieba.posseg
    for change in list(changes):
        before=normalize(change['before']);after=normalize(change['after']);cursor=0
        for token in jieba.posseg.cut(after):
            term=token.word;old=before[cursor:cursor+len(term)];cursor+=len(term)
            if 3<=len(term)<=8 and token.flag in ('n','nz','i') and len(old)==len(term) and sum(a!=b for a,b in zip(old,term))>=2 and all(_phonetic(a,b) for a,b in zip(old,term)):
                learned[old]=(term,change['evidence'])
    for index,row in enumerate(rows):
        before=row['text'];after=before;proof=[]
        for old,(term,evidence) in learned.items():
            if old not in after:continue
            if any(old in p['text'] and any(float(row['start'])-.8<=t<=float(row['end'])+.8 for t in p['times']) for p in phrases):continue
            after=after.replace(old,term);proof.append(dict(before=old,after=term,method='same-pronunciation-established-term',source_evidence=evidence))
        if before!=after:
            row['text']=after;row['text_evidence']='audio+established-video-term'
            joined=normalize(''.join(w.get('text',w.get('word','')) for w in row.get('words',[])))
            if joined==normalize(before):
                revised=normalize(after);cursor=0;out=[]
                for w in row.get('words',[]):
                    ww=dict(w);key='text' if 'text' in ww else 'word';chars=list(ww.get(key,''))
                    for j,char in enumerate(chars):
                        if normalize(char):chars[j]=revised[cursor];cursor+=1
                    if ''.join(chars)!=ww.get(key,''):ww['original_text']=ww.get(key,'')
                    ww[key]=''.join(chars);out.append(ww)
                row['words']=out
            else:row['words']=[];row['timing']='segment'
            existing=next((c for c in changes if c['segment']==index),None)
            if existing:existing['after']=after;existing['evidence'].extend(proof)
            else:changes.append(dict(segment=index,start=row['start'],end=row['end'],before=before,after=after,evidence=proof))
    # A terse answer can lose its noun and turn a spoken classifier into a
    # homophone (科/颗/棵). Resolve only a repeated, visually supported counter
    # in an unambiguous topic; do not change counts or general sentences.
    for index,row in enumerate(rows):
        bare=normalize(row['text']);match=re.fullmatch(r'([零一二三四五六七八九十两\d]+)([科颗棵])',bare)
        if not match:continue
        previous=''.join(r['text'] for r in rows[:index]);local=''.join(r['text'] for r in rows[max(0,index-3):index+1])
        preferred='颗' if '卫星' in previous and not re.search(r'学科|课程|功课|树木|植物',local) else ('棵' if re.search(r'树木|植物',local) else None)
        if not preferred or preferred==match.group(2):continue
        target=canonical(match.group(1)+preferred)
        if canonical(previous).count(target)<2:continue
        evidence=next((p for p in phrases if target in p['text'] or preferred in p['text']),None)
        if not evidence:continue
        before=row['text'];after=before.replace(match.group(2),preferred)
        row['text']=after;row['text_evidence']='audio+repeated-counter-and-topic'
        if normalize(''.join(w.get('text',w.get('word','')) for w in row.get('words',[])))==bare:
            row['words']=[dict(w,**{('text' if 'text' in w else 'word'):w.get('text',w.get('word','')).replace(match.group(2),preferred)}) for w in row.get('words',[])]
        else:row['words']=[];row['timing']='segment'
        changes.append(dict(segment=index,start=row['start'],end=row['end'],before=before,after=after,evidence=[dict(method='repeated-counter-and-topic',visual_phrase=evidence['text'],visual_times=evidence['times'])]))
    return rows,dict(method='audio-with-repeated-video-text',stable_phrases=len(phrases),changes=changes,names=list(names))


def extract_video_text(path, cancel_event=None, progress_callback=None, segments=None):
    """OCR in a disposable process: inference memory is released before the next stage."""
    if os.environ.get('ASR_VISUAL_TEXT','auto').lower() in ('0','off','false'):return dict(status='disabled',frames=[])
    with tempfile.TemporaryDirectory(prefix='subtitle-grounding-') as tmp:
        output=os.path.join(tmp,'evidence.json')
        rawfile=os.path.join(tmp,'audio-text.json')
        with open(rawfile,'w',encoding='utf-8') as f:json.dump(segments or [],f,ensure_ascii=False)
        proc=subprocess.Popen([sys.executable,'-m','utils.chinese_grounding','extract',os.path.abspath(path),output,rawfile],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import time
        begin=time.monotonic()
        try:
            while proc.poll() is None:
                if time.monotonic()-begin>900:raise RuntimeError('视频文字提取超过时间限制')
                if progress_callback:progress_callback(79.5,'正在自动核对视频文字...')
                if cancel_event and cancel_event.wait(.2):raise RuntimeError('任务已被取消')
                if cancel_event is None:time.sleep(.2)
        except BaseException:
            proc.terminate();proc.wait(timeout=10);raise
        error=proc.communicate()[1]
        if proc.returncode:raise RuntimeError('视频文字提取失败：'+error.decode('utf-8',errors='replace')[-300:])
        with open(output,encoding='utf-8') as f:return json.load(f)


def _extract(path,output,rawfile=None):
    from rapidocr_onnxruntime import RapidOCR
    import cv2
    audio_rows=[]
    if rawfile:
        with open(rawfile,encoding='utf-8') as f:audio_rows=json.load(f)
    probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',path],text=True))
    if not any(s['codec_type']=='video' for s in probe['streams']):
        result=dict(status='no-video',frames=[])
    else:
        duration=float(probe['format'].get('duration',0));step=max(.5,duration/900)
        # Limit CPU oversubscription; keep the source coordinates for gating.
        rec_path=os.environ.get('OCR_REC_MODEL','/app/ocr_models/ch_PP-OCRv5_rec_server.onnx')
        options=dict(intra_op_num_threads=2,inter_op_num_threads=1,det_limit_side_len=960,det_limit_type='max')
        engine=RapidOCR(**options)
        secondary=RapidOCR(**dict(options,rec_model_path=rec_path)) if os.path.isfile(rec_path) else None
        frames=[]
        with tempfile.TemporaryDirectory(prefix='subtitle-ocr-frames-') as tmp:
            subprocess.run(['ffmpeg','-v','error','-i',path,'-vf',f'fps=1/{step},scale=960:-2','-frames:v','900','-q:v','3',os.path.join(tmp,'%05d.jpg')],check=True,timeout=max(120,min(900,duration)))
            for i,name in enumerate(sorted(os.listdir(tmp))):
                full=cv2.imread(os.path.join(tmp,name));full_h,w=full.shape[:2];crop_offset=full_h//2
                img=full[crop_offset:];h=img.shape[0]
                result,_=engine(img);lines=[]
                for box,text,score in result or []:
                    ys=[(point[1]+crop_offset)/full_h for point in box];height=max(ys)-min(ys)
                    if .55<=sum(ys)/4<=.96 and .01<=height<=.16 and float(score)>=.9:
                        lines.append(dict(text=text,score=round(float(score),4),engine='PP-OCRv4',box=[[round(float(x)/w,4),round((float(y)+crop_offset)/full_h,4)] for x,y in box]))
                if secondary and lines:
                    moment=(i+.5)*step
                    nearby=''.join(canonical(row['text']) for row in audio_rows if float(row['start'])-.8<=moment<=float(row['end'])+.8)
                    verify=[]
                    for line in lines:
                        text=canonical(line['text']);n=len(text)
                        if audio_rows:
                            if not nearby or text in nearby:continue
                            score=0.0
                            for length in range(max(2,n-2),min(len(nearby),n+2)+1):
                                for pos in range(len(nearby)-length+1):score=max(score,difflib.SequenceMatcher(None,text,nearby[pos:pos+length]).ratio())
                            if score<(.5 if n<=3 else .7):continue
                        verify.append(line)
                    crops=[]
                    for line in verify:
                        pts=line['box'];x0=max(0,int(min(x for x,y in pts)*w)-2);x1=min(w,int(max(x for x,y in pts)*w)+3)
                        y0=max(0,int(min(y for x,y in pts)*full_h)-crop_offset-2);y1=min(h,int(max(y for x,y in pts)*full_h)-crop_offset+3)
                        crops.append(img[y0:y1,x0:x1])
                    if crops:
                        results,_=secondary.text_rec(crops)
                        for line,(text,score) in zip(verify,results):
                            if float(score)>=.9:lines.append(dict(line,text=text,score=round(float(score),4),engine='PP-OCRv5'))
                frames.append(dict(time=round((i+.5)*step,3),lines=lines))
        result=dict(status='complete',engine='RapidOCR-ONNX/PP-OCRv4+v5-rec-consensus' if secondary else 'RapidOCR-ONNX/PP-OCRv4',sample_step=step,duration=duration,frames=frames)
    with open(output,'w',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False)

if __name__=='__main__':
    if len(sys.argv) in (4,5) and sys.argv[1]=='extract':_extract(sys.argv[2],sys.argv[3],sys.argv[4] if len(sys.argv)==5 else None)
