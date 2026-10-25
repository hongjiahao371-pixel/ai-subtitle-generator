"""Local instruction-following caption refinement, separate from translation.

No transcript substitutions. Each accepted result must pass source fidelity,
word wrapping, and the budget derived from the real media interval.
"""
import os,re,urllib.parse,json
from utils.artifacts import wrap_display
from utils.translation_rules import translation_issues,reference_prompt,TranslationValidationError,normalize_large_counts,normalize_english_articles
from utils.ai_memory import wait_for_local_sleep

def editor_config(cfg):
    url=os.environ.get('SUBTITLE_EDITOR_URL','').strip()
    if not url or urllib.parse.urlparse(str(cfg.get('base_url',''))).hostname!='subtitle-translator':return None
    return dict(cfg,base_url=url,model='Qwen3-4B-Instruct-2507',chat_template_kwargs={'enable_thinking':False})

def refine_captions(sources,targets,limits,terms,cfg,chat,safe,audit=None,cancel_event=None,progress_callback=None):
    editor=editor_config(cfg)
    indices=[i for i,t in enumerate(targets) if len(t)>limits[i] or len(wrap_display(t))>2 or translation_issues(sources[i],t,terms)]
    if not editor or not indices:return targets
    wait_for_local_sleep(cfg,include_editor=False)
    results=list(targets)
    for n,i in enumerate(indices):
        source=sources[i];original=targets[i];limit=limits[i]
        original_issues=translation_issues(source,original,terms)
        feedback='' if not original_issues else 'REQUIRED: repair these errors from the authoritative Chinese caption: '+ '; '.join(original_issues)+'\n'
        for attempt in range(3):
            if cancel_event and cancel_event.is_set():raise RuntimeError('任务已被取消')
            system=('You write faithful, concise English subtitles. Return a JSON object with exactly 3 different concise English caption candidates in the candidates array. '
                    'Keep the Chinese meaning, speaker and subject, tense, negation, numbers in the original clause order, units, named entities and comparison direction. '
                    'No commentary or Chinese. Do not answer questions or fact-correct the speaker. Prefer compact natural phrases and Arabic digits. '
                    'Use natural grammar whenever the budget allows. Preserve incomplete speech as ellipses; never invent a connection to a following imperative. '
                    'For give-you-an-object-to-use constructions, YOU receive and use the object. Do not reverse roles. Do not explain terminology.')
            context=''  # The first translation already carries context; don't import other cues.
            small={'zero':'0','one':'1','two':'2','three':'3','four':'4','five':'5','six':'6','seven':'7','eight':'8','nine':'9','ten':'10'}
            numbered=re.sub(r'\bone[- ]third\b','1/3',original,flags=re.I)
            numbered=re.sub(r'\b(?:'+'|'.join(small)+r')\b',lambda m:m.group() if m.group().lower()=='one' and re.match(r'\s+(needs|must|can|has|is|should)\b',numbered[m.end():],re.I) else small[m.group().lower()],numbered,flags=re.I)
            required=', '.join(re.findall(r'\d+(?:[./]\d+)*',normalize_large_counts(source) if original_issues else numbered))
            user=(f'Required numeric tokens: {required or "none"}. Keep every one.\nChinese caption: {source}\nEnglish translation: {numbered}\n'+
                  ('REQUIRED: explicitly include LIGHT WAVES (or light-wave). Do not omit the signal propagation medium.\n' if '光波' in source else '')+
                  ('REQUIRED: include MY TIME or MY TIMESTAMP. It is the timestamp I send, not merely the time needed to send a signal.\n' if re.search(r'我的时间(?:戳)?发给',source) else '')+
                  ('REQUIRED: begin with To or If to preserve purpose or condition, not an imperative.\n' if source.startswith('要把') else '')+
                  ('REQUIRED: keep BUT and the interrupted topic noun (answer/result/reason/cause/question/conclusion) before an ellipsis (...), then a separate Please take a seat. Do not drop the interrupted topic. Do not connect seating with for the answer.\n' if re.search(r'(?:但|不过|然而)(?:答案|结果|理由|原因|问题|结论)(?:呢)?请坐',source) else '')+
                  ('REQUIRED: include the English word MOST. The Chinese says this is what they fear MOST, not merely fear.\n' if '最怕' in source else '')+
                  ('REQUIRED: include MAYBE or MIGHT. This is speculation, not a certain fact.\n' if '可能' in source and '不可能' not in source else '')+
                  ('REQUIRED: do not add best/highest/hardest. Keep an unfinished clause as an ellipsis.\n' if '原句没有的最高程度被补入' in original_issues else '')+
                  ('REQUIRED: keep this as a QUESTION, never answer it or invent a number. Keep how many/why/whether as a question.\n' if re.search(r'[?？]|几(?:颗|个)|为什么|为啥',source) else '')+
                  f'Required name spellings: {dict((a,b) for a,b in terms.items() if a in source)}\nTerm references: {reference_prompt(source,terms)}\n'+('REQUIRED: explicitly include WILL or \'ll; the action has not happened yet.\n' if re.search(r"\bwill\b|['’]ll\b",original,re.I) else '')+
                  f'Write a faithful caption no longer than {limit} characters including spaces. '
                  'Replace spelled number words with Arabic digits. Omit redundant filler and articles. At most 2 lines of 42 characters each. '
                  'Use concise wording rather than dropping facts. '+('REQUIRED: preserve I/my as the speaker who knows or thinks. ' if re.search(r'我(?:知道|认为|想|猜|估计)',source) and re.search(r'\bI\b',original) else '')+feedback)
            if feedback:
                role_source=re.sub(r'(?:我跟你[讲说]|我告诉你|你知道吗|你看)[,，。!?！？ ]*','',source)
                roles=[]
                for chinese,english in [('我(?!们)','I|my|me'),('我们','we|our|us'),('你','you|your'),('他(?!们)','he|his|him'),('她(?!们)','she|her'),('它(?!们)','it|its'),('[他她它]们','they|their|them')]:
                    if re.search(chinese,role_source) and re.search(r'\b(?:'+english+r')\b',original,re.I):roles.append(english.replace('|','/'))
                if roles:user+='\nREQUIRED: keep at least one pronoun from EACH group: '+', '.join(roles)+'. Keep the same speaker/owner; do not omit these.'
            editor['response_format']={'type':'json_schema','json_schema':{'name':'captions','strict':True,'schema':{'type':'object','properties':{'candidates':{'type':'array','minItems':3,'maxItems':3,'items':{'type':'string','maxLength':limit}}},'required':['candidates'],'additionalProperties':False}}}
            response=chat(editor,[{'role':'system','content':system},{'role':'user','content':user}],max_tokens=512).strip()
            try:candidates=json.loads(response)['candidates']
            except (ValueError,TypeError,KeyError):candidates=[response.strip('"')]
            evaluations=[]
            for candidate in candidates:
                result=normalize_english_articles(str(candidate).strip());issues=translation_issues(source,result,terms)
                if not original_issues and not safe(original,result,source_text=source):issues.append('主语、时态、数字或极性变化；或未缩短')
                if len(result)>limit or len(wrap_display(result))>2:issues.append('超出字符预算或两行')
                evaluations.append((issues,result))
            # The first valid candidate follows the fidelity-first generation order.
            # Choosing the shortest valid candidate over-compressed adequate cues.
            issues,result=min(evaluations,key=lambda x:len(x[0]))
            accepted=not issues
            if audit is not None:audit[i]['attempts'].append(dict(kind='local_caption_editor',engine='Qwen3-4B-Instruct-2507',text=result,accepted=accepted,issues=issues))
            if accepted:
                results[i]=result
                if audit is not None:audit[i]['target']=result
                break
            feedback=f'Last result had {len(result)} characters and {len(result.split())} words, so it was rejected: '+result+'\nRequired fixes: '+'; '.join(issues)+'\nWrite a substantially shorter caption. Preserve ALL technical facts.'
        else:raise TranslationValidationError(f'第 {i+1} 条字幕自动精简未通过忠实性及阅读检查：'+ '; '.join(issues))
        if progress_callback:progress_callback(round(95+4*(n+1)/len(indices),1),f'本地字幕精简 ({n+1}/{len(indices)})')
    return results
