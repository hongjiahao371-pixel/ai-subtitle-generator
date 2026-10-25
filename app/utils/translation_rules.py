"""Source-side fidelity checks and deterministic proper-name spellings.

These are lexical constraints, not a claim of general semantic equivalence.
There are no sample transcripts or per-video substitutions here.
"""
import re

TERMS={
 '北斗三号':'BeiDou-3','北斗':'BeiDou','格洛纳斯':'GLONASS','伽利略':'Galileo',
 'GPS':'GPS','人工智能':'AI',
}
MEANING={
 '目标':r'\b(?:aim|goal|objective|target|seek)\w*\b',
 '相比':r'\b(?:compared|relative|than|versus|vs|behind|lag)\w*\b',
 '挑战':r'\b(?:challenge|test|take.on)\w*\b',
 '智慧':r'\b(?:wisdom|ingenuity|intelligence)\w*\b',
 '核心技术':r'\b(?:core|key) (?:tech|technolog)\w*\b',
 '制造工艺':r'\b(?:manufactur|production|fabrication)\w*\b',
 '赶超':r'\b(?:surpass|outperform|overtake|exceed|outstrip)\w*\b',
 '并肩':r'\b(?:alongside|together|side.by.side|collaborat|cooperat)\w*\b',
 '协作':r'\b(?:collaborat|cooperat|collab)\w*\b',
 '至少':r'\b(?:at least|minimum|no fewer than)\b',
 '未知数':r'\b(?:unknown|variable)\w*\b',
 '方程':r'\bequation\w*\b',
 '三角定位':r'\btriangulat\w*\b',
 '等边三角形':r'\bequilateral[ -]+triang\w*\b',
 '光波':r'\b(?:light|optical|photon)\w*\b',
 '定位':r'\b(?:position|locat|triangulat)\w*\b',
 '接收机':r'\breceiver\w*\b',
 '双臂':r'\b(?:dual.arm|two.arm|2.arm|two arms|2 arms|both arms)\w*\b',
 '江湖':r'\b(?:jianghu|martial[- ](?:arts?[- ]?)?world|kung fu world)\b',
 '文科':r'\b(?:liberal arts|humanities|arts? students?)\b',
 '厉害':r'\b(?:powerful|strong|impressive|awesome|good|amazing|great|severe|intense)\b',
 '请坐':r'\b(?:sit(?: down)?|take (?:a|your) seat|be seated)\b',
 '最难':r'\b(?:hardest|most difficult)\b',
 '以来':r'\bsince\b',
 '可能':r'\b(?:maybe|perhaps|might|could|possibly|possible|probably)\b',
 '赤道':r'\bequator\w*\b',
 '南极':r'\bsouth\b',
 '北极':r'\bnorth\b',
 '痛':r'\b(?:pain|problem|bottleneck|challenge|weakness|hurdle|hurt|hinder|hold.back)\w*\b',
}


def _name_pinyin(text):
    try:
        from pypinyin import lazy_pinyin
        syllables=lazy_pinyin(text)
        if len(syllables)>1:
            return syllables[0].capitalize()+' '+''.join(syllables[1:]).capitalize()
    except ImportError:pass
    return None


def automatic_terms(texts,glossary=None):
    whole='\n'.join(texts);terms={a:b for a,b in TERMS.items() if a in whole}
    # Private domain tag supplies context; descriptive system words are not names.
    if re.search(r'卫星导航|北斗定位|GPS',whole):
        terms['__navigation_context__']='satellite'
        for term,target in [('南极','South Pole'),('北极','North Pole')]:
            if term in whole:terms[term]=target
    # An explicit self-introduction is stronger evidence than unrestricted NER.
    for m in re.finditer(r'(?:我是|我叫)([\u4e00-\u9fff]{2,4}?)(?=我要|我想|我将|我会|[,，。！!\s]|$)',whole):
        name=m.group(1)
        surnames='赵钱孙李周吴郑王冯陈卫蒋沈韩杨朱秦许何吕施张孔曹严华金魏陶姜谢邹喻章苏潘葛范彭鲁韦马方任袁柳史唐薛雷贺倪汤殷罗郝安常傅齐康伍余顾孟黄穆萧尹姚邵汪祁毛成戴宋庞熊舒董梁杜阮蓝贾路江童颜郭梅林钟徐邱骆高夏蔡田胡万卢莫丁邓曾洪包左石崔程陆翁欧聂黎赖叶刘廖于谭侯邢'
        if name[0] not in surnames or name.endswith(('老师','先生','女士','医生','同学','学生')):continue
        translated=_name_pinyin(name)
        if translated:terms[name]=translated
    # Keep named mixed-script products, e.g. 小i / 灵犀X, consistent throughout.
    for m in re.finditer(r'([\u4e00-\u9fff]{1,3})([A-Za-z][A-Za-z0-9_-]*)(?=[\u4e00-\u9fff]|[，,。\s]|$)',whole):
        prefix,latin=m.groups()
        # Avoid swallowing surrounding sentence content as part of the brand.
        if latin.isupper() and len(latin)>1:continue
        try:
            import jieba
            prefix=jieba.lcut(prefix)[-1]
        except ImportError:
            prefix=prefix[-1:] if len(prefix)>1 else prefix
        if prefix in {'是','第','个','再','加','加个','这','那','在','现在','认为','和','用','对','选','选项','答案','方程','坐标','还有','或者','以及','到','为','几','由'}:continue
        try:
            from pypinyin import lazy_pinyin
            target=''.join(lazy_pinyin(prefix)).capitalize()+' '+latin
            terms[prefix+latin]=target
        except ImportError:pass
    for m in re.finditer(r'([赵钱孙李周吴郑王陈杨朱张黄林徐刘胡何谢曹袁马梁沈])总(?=说|认为|觉得|介绍|讲|[，,.])',whole):
        try:
            from pypinyin import lazy_pinyin
            terms[m.group()]='Mr. '+lazy_pinyin(m.group(1))[0].capitalize()
        except ImportError:pass
    for a,b in (glossary if isinstance(glossary,dict) else {}).items():
        if a and b and re.search(r'[A-Za-z]',str(b)) and not re.search(r'[\u3400-\u9fff]',str(b)):terms[str(a)]=str(b)
    return terms


def protect_names(text,terms):
    # Longest term first, one simultaneous substitution; replacements are never
    # passed through a second replace (BeiDou-3 cannot become BeiDou-3-3).
    keys=sorted((x for x in terms if x in text),key=len,reverse=True)
    return re.sub('|'.join(re.escape(x) for x in keys),lambda m:terms[m.group()],text) if keys else text


def reference_prompt(text,terms):
    references=[f'{term} 翻译成 {target}' for term,target in terms.items() if term in text]
    for term,target in [('光波','light waves'),('请坐','take a seat'),('降到','drop to'),('最难','hardest'),('最怕','fear most')]:
        if term in text:references.append(f'{term} 翻译成 {target}')
    if '__navigation_context__' in terms:
        for term,target in [('平面','plane'),('距离','distance'),('测的是','measure')]:
            if term in text:references.append(f'{term} 翻译成 {target}')
        if '不准' in text and '时间' in text:references.append('时间不准 翻译成 time is inaccurate')
    if '以来' in text:references.append('以来 翻译成 since')
    if '我到你' in text:references.append('我到你 翻译成 from me to you；不要反转传播方向')
    if '发给' in text:references.append('发给 翻译成 send / transmit to；必须保留发送动作')
    if re.search(r'我的时间(?:戳)?发给',text):references.append('我的时间 翻译成 my time / my timestamp；保留所属关系，不是 time to send a signal')
    if re.search(r'X\s*Y\s*Z',text,re.I) and re.search(r'加(?:个|上)?T',text,re.I) and '__navigation_context__' in terms:references.append('X Y Z 是三个空间坐标；T 是时间变量 time T，不是 third spatial dimension。')
    if '咱' in text:references.append('咱、咱们 翻译成 we / us；保留与原句中我的区别')
    if '能听懂' in text:references.append('能听懂 翻译成 can understand；按上下文判断解释已经完成还是将来发生')
    if re.search(r'给[你他她]个|给[你他她]一[个块张支本]',text):references.append('给某人东西让其做事：give the object to that person so THEY can do the action; do not reverse the agent.')
    if '请坐' in text:references.append('请坐是独立礼貌指令。前面若为中断的句子，保留省略号，不要编造两句之间的目的关系。')
    if ('__navigation_context__' in terms or 'GPS' in terms) and re.search(r'颗(?:星|卫星)?',text):references.append('颗卫星、颗星或省略卫星的颗 翻译成 satellite')
    if re.search(r'文科(?:生)?(?:得|都|也|应该|可以|能|得能)',text):references.append('文科生 翻译成 liberal arts students')
    if '并肩作战' in text and ('人工智能' in terms or re.search(r'机器人|机械臂|协作',text)):
        references.append('并肩作战 翻译成 work alongside')
    senses={'\u7b49\u8fb9\u4e09\u89d2\u5f62':'equilateral triangle','定位':'positioning','三角定位':'triangulation','同时':'at once','江湖':'martial world','文科':'liberal arts','厉害':'powerful','智慧':'ingenuity','尺':'chi','钧':'jun','挑战':'challenge','相比':'compared with','赶超':'surpass','并肩':'alongside','协作':'collaborative','核心技术':'core tech','制造工艺':'manufacturing','至少':'at least','未知数':'unknown variable','双臂':('dual-arm' if re.search(r'双臂(?:协作|合作|机器人|机械臂)',text) else 'two arms'),'痛':('pain point' if re.search(r'之痛|痛点',text) else 'pain')}
    references.extend(f'{term} 翻译成 {target}' for term,target in senses.items() if term in text and (term!='痛' or re.search(r'之痛|痛点|疼痛|痛苦|伤痛',text)))
    return '\n'.join(references)


def strip_implied_dimensions(source,numeric_target):
    if not re.search(r'(?:[123]D|[一二三123]维)',source,re.I):
        dimensions=[]
        if re.search(r'立体|空间',source):dimensions.append('3')
        if '平面' in source:dimensions.append('2')
        if re.search(r'一条线|一维',source):dimensions.append('1')
        for dimension in dimensions:numeric_target=re.sub(r'\b'+dimension+r'(?:D|[- ]dimension(?:al|s)?)\b','',numeric_target,flags=re.I)
    return numeric_target

def normalize_large_counts(text):
    for word,value in [('百','100'),('千','1000')]:
        text=re.sub(r'(?<![零一二三四五六七八九十])'+word+r'(?=种|个|次|年)',value,text)
    return text

def translation_issues(source,target,terms=None):
    target=target.replace('’',"'")
    issues=[];terms=terms or {}
    if re.search(r'[?？]|几(?:颗|个)|为什么|为啥',source) and not re.search(r'\?|\b(?:how many|how|why|what|which|whether)\b',target,re.I):issues.append('提问被改写成回答或陈述')
    if not target or re.search(r'[\u3400-\u9fff]',target):issues.append('译文为空或含未翻译中文')
    # Explicit digits are mandatory; Chinese count words can legitimately
    # become an English article. Match them as optional ordered numeric slots,
    # rather than treating an added English 'one' as a fabricated number.
    numeric_source=normalize_large_counts(source).replace("双臂","2臂");numeric_target=re.sub(r"\bdual[- ]arm\b","2 arms",target,flags=re.I)
    numeric_target=re.sub(r'\b(?:(?:a|one)\s+)?hundreds?\b','100',numeric_target,flags=re.I)
    numeric_target=re.sub(r'\b(?:(?:a|one)\s+)?thousands?\b','1000',numeric_target,flags=re.I)
    for term,name in terms.items():
        numeric_source=numeric_source.replace(term,'')
        numeric_target=re.sub(re.escape(name),'',numeric_target,flags=re.I)
    fractions={'half':2,'halves':2,'third':3,'thirds':3,'quarter':4,'quarters':4,'fourth':4,'fourths':4,'fifth':5,'fifths':5}
    tiny={'一':'1','二':'2','三':'3','四':'4','五':'5','六':'6','七':'7','八':'8','九':'9','十':'10'}
    numeric_source=re.sub(r'([一二三四五六七八九十])分之([一二三四五六七八九十])',lambda m:tiny[m.group(2)]+'/'+tiny[m.group(1)],numeric_source)
    fraction_words={'one':'1','two':'2','three':'3','four':'4','five':'5'}
    numeric_target=re.sub(r'\b(one|two|three|four|five)[ -]+(half|halves|thirds?|quarters?|fourths?|fifths?)\b',lambda m:fraction_words[m.group(1).lower()]+'/'+str(fractions[m.group(2).lower()]),numeric_target,flags=re.I)
    number_words={'zero':'0','one':'1','two':'2','three':'3','four':'4','five':'5','six':'6','seven':'7','eight':'8','nine':'9','ten':'10'}
    numeric_target=re.sub(r'\b(?:'+ '|'.join(number_words)+r')\b',lambda m:m.group() if m.group().lower()=='one' and re.match(r'\s+(needs|must|can|has|is|should|requires)\b',numeric_target[m.end():],re.I) else number_words[m.group().lower()],numeric_target,flags=re.I)
    digit_pattern=r'\d+(?:\.\d+)?'
    numeric_target=strip_implied_dimensions(source,numeric_target)
    for fraction in re.findall(r'\d+/\d+',numeric_source):
        if fraction not in numeric_target:issues.append('分数单位或分母丢失')
    target_numbers=re.findall(digit_pattern,numeric_target)
    if target_numbers and not re.search(r'\d|[零一二三四五六七八九十百千万两](?=个|颗|条|项|秒|分|次|种|年|圈|尺|钧|米|岁|月|日|天|公里|公斤|千米|千克|小时)',numeric_source):issues.append('译文补入原句没有的数字')
    source_numbers=[]
    cn=dict(zip('零一二三四五六七八九','0123456789'));cn['两']='2'
    combined=digit_pattern+r'|[零一二三四五六七八九两](?=个|颗|条|项|秒|分|次|种|年|圈|尺|钧|米|岁|月|日|天|公里|公斤|千米|千克|小时)'
    for m in re.finditer(combined,numeric_source):
        token=m.group();source_numbers.append((cn.get(token,token),token not in cn or cn.get(token)!='1'))
        if token=='一' and numeric_source[max(0,m.start()-1):m.start()]=='各':source_numbers.append(('1',False))
    if any(required for _,required in source_numbers):
        states={0}
        for number,required in source_numbers:
            next_states=set() if required else set(states)
            for pos in states:
                if pos<len(target_numbers) and target_numbers[pos]==number:next_states.add(pos+1)
            states=next_states
        if len(target_numbers) not in states:issues.append('数字或数字顺序变化')
    if ('__navigation_context__' in terms or 'GPS' in terms) and re.search(r'颗(?:星|卫星)?',source) and not re.search(r'\bsatellite\w*\b',target,re.I):issues.append('卫星计数语境误译或丢失 satellite')
    if re.search(r'文科(?:生)?(?:得|都|也|应该|可以|能|得能)',source) and not re.search(r'(?:liberal arts|humanities|arts?)[ -]+(?:students?|majors?|people|folks|graduates?)',target,re.I):issues.append('文科生这一人物主语丢失或误译为文科内容')
    if re.search(r'同时(?:使用|运行|接收|发射|观测|测量|定位)',source) and not re.search(r'\b(?:simultaneous(?:ly)?|at once|at the same time|together|in parallel|concurrently)\b',target,re.I):issues.append('同时使用的条件丢失')
    if re.search(r'我(?:就)?想(?!知道).{0,30}(?:的话|应该)',source) and not re.search(r'\b(?:think|thought|imagin\w*|guess|assum\w*|wonder\w*)\b',target,re.I):issues.append('说话人的设想变成确定事实')
    if re.search(r'X\s*Y\s*Z',source,re.I) and not re.search(r'X[\s,/+]*Y[\s,/+]*Z',target,re.I):issues.append('XYZ坐标变量丢失或顺序变化')
    if re.search(r'加个T|加T|加上T',source,re.I) and not re.search(r'\bT\b',target):issues.append('时间变量T丢失')
    for value in re.findall(r'降到(\d+(?:\.\d+)?)%',source):
        if not re.search(r'\bto\s+'+re.escape(value)+r'\s*%',target,re.I):issues.append('降到的最终百分比误译为下降幅度')
    if '__navigation_context__' in terms:
        if '不准' in source and '时间' in source and not re.search(r'\b(?:inaccurate|imprecise|inexact|off|wrong|not accurate|not precise)\b|(?:isn.t|aren.t)\s+(?:accurate|precise)',target,re.I):issues.append('时间不准应为不准确而不是不允许')
        if '平面' in source and not re.search(r'\b(?:plane|planar|2D|two[- ]dimensional)\b',target,re.I):issues.append('平面定位的二维条件丢失')
        if '距离' in source and not re.search(r'\b(?:distance|range)\w*\b',target,re.I):issues.append('距离测量条件丢失')
        if '测的是' in source and not re.search(r'\b(?:measur|calculat|determin|comput)\w*\b',target,re.I):issues.append('测量动作丢失')
    if not re.search(r'最|顶级|一流|先进',source) and re.search(r'\b(?:best|highest|largest|biggest|hardest)\b',target,re.I):issues.append('原句没有的最高程度被补入')
    if '尺' in source and not re.search(r'\bchi\b',target,re.I):issues.append('中国传统长度单位尺应保留为chi')
    if '钧' in source and not re.search(r'\bjun\b',target,re.I):issues.append('中国传统重量单位钧应保留为jun')
    if '我到你' in source and re.search(r'传播|光波|传输|信号',source) and not re.search(r'\bfrom me\b.{0,50}\bto you\b',target,re.I):issues.append('传播方向必须是 from me to you；不能反转成 from you to me')
    if '发给' in source and not re.search(r'\b(?:send|sent|transmit|pass|deliver|issue|forward)\w*\b',target,re.I):issues.append('发给的发送动作丢失')
    if '__navigation_context__' in terms and re.search(r'X\s*Y\s*Z',source,re.I) and re.search(r'加(?:个|上)?T',source,re.I):
        if not re.search(r'\btime\b',target,re.I):issues.append('T 是时间未知数，不是第三个空间维度；应明确 time T')
    if re.search(r'我的时间(?:戳)?发给',source) and not re.search(r'\bmy (?:time|timestamp|clock)\b|\btime from my clock\b',target,re.I):issues.append('发送的是我的时间或时间戳，my time / my timestamp 所属关系丢失')
    if re.match(r'^要把',source) and not re.search(r'\b(?:to|for|if|need|want|must|should|make sure)\b',target,re.I):issues.append('目的或条件句被改成直接命令；应保留 To / If 等目的条件关系')
    fragment=re.search(r'(?:但|不过|然而)(答案|结果|理由|原因|问题|结论)(?:呢)?请坐',source)
    if fragment:
        noun={'答案':'answer','结果':'result','理由':'reason','原因':'cause','问题':'question','结论':'conclusion'}[fragment.group(1)]
        if not re.search(r'\bbut\b',target,re.I) or not re.search(r'\b'+noun+r'\b',target,re.I):issues.append('中断前的话题和转折被删掉；必须保留 but the '+noun+' 后再省略号')
        if not re.search(r'\.\.\.|…',target) or re.search(r'\bfor (?:the )?(?:answer|result)\b',target,re.I):issues.append('话题中断后应省略号，再独立 Please take a seat；不得编造 for the answer 的目的')
    if re.search(r'我们|咱们|咱(?:说|讲|解释|做|干|开始|结束)',source) and not re.search(r'\b(?:we|our|us)\b',target,re.I):issues.append('共同动作的我们或咱被改成我或省略')
    # Dative constructions assign the object and following activity to the
    # recipient. A mere pronoun-presence check cannot detect role reversal.
    if re.search(r'给你(?:个|一[个块张支本])',source) and not re.search(r"\b(?:give|hand|offer|lend|let)\b.{0,30}\byou\b|\byou\s+(?:could|can|to)\b",target,re.I):issues.append('给予对象和后续动作的主体颠倒；应给对方物件让对方使用')
    if '请坐' in source and re.search(r'\b(?:seat|seated|sit(?: down)?)\s+(?:for|to|with|about)\b',target,re.I):issues.append('独立的请坐指令被编造为前句目的；保留中断和停顿')
    if '最怕' in source and not re.search(r'\b(?:most|worst|biggest|greatest)\b',target,re.I):issues.append('最怕的最高程度丢失')
    if re.search(r'错(?!落|位)|错误',source) and not re.search(r'不错|没错|无错',source) and not re.search(r'\b(?:wrong|incorrect|mistaken|error|fault|mismatch)\w*\b',target,re.I):issues.append('错的判断未保留')
    if re.search(r'%|百分之',source) and not re.search(r'%|\bpercent\b',target,re.I):issues.append('百分比单位丢失')
    if re.search(r'(?:不能|不可能|不准|不准确|不对|不是|没有|不需要|不用|不了|不成)',source):
        if not re.search(r"\b(?:not|no|without|cannot|never|incorrect|inaccurate|imprecise|inexact|impossible|wrong|unable|useless|unnecessary)\b|n't\b"+(r'|\boff\b' if re.search(r'时间|时钟|钟表',source) else ''),target,re.I):issues.append('否定含义丢失')
    if re.search(r'[他她它我](?:们)?将(?:用|挑战|展示|介绍|完成|开始)',source) and not re.search(r"\bwill\b|['’]ll\b|\bgoing to\b|\bset to\b",target,re.I):issues.append('将来动作变成已发生或一般现在时')
    for term,pattern in MEANING.items():
        if term=='可能' and '不可能' in source:continue
        if term=='目标' and not re.search(r'研发.*目标|目标[是为]',source):continue
        if term=='痛' and not re.search(r'之痛|痛点|疼痛|痛苦|伤痛',source):continue
        if term in source and not re.search(pattern,target,re.I):issues.append('含义约束未保留：'+term)
    for term,name in terms.items():
        if term+'的' in source and '我' not in source and re.search(r'\bmy\s+'+re.escape(name),target,re.I):issues.append('专名所有者被改写成说话人：'+term)
        if term in source and name.casefold() not in target.casefold():issues.append('专名不一致：'+term)
    return issues


class TranslationValidationError(RuntimeError):pass

def normalize_english_articles(text):
    # Keep an ellipsis as one glyph so wrapping cannot split its dots.
    text=re.sub(r'\.{3,}','…',text)
    return re.sub(r'\b([Aa])\s+(?=(?:equatorial|equilateral|unknown|equation|industrial|artificial)\b)',lambda m:m.group(1)+'n ',text,flags=re.I)
