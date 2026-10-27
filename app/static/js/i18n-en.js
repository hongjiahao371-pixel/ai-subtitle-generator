// ============================================================================
// 界面英文化运行时引擎（2026-10-26）
// 方案：zh→en 精确词典 + MutationObserver，对渲染后的 DOM 做文本节点翻译。
// 不侵入 app.js 既有逻辑（回归风险最低）；用户数据（文件名/字幕内容）不在词典里，
// 永远不会被改动。关闭英文化 = 恢复原始中文，直接刷新页面即可。
// ============================================================================
(function () {
    'use strict';

    var DICT = {
        // 导航
        '系统': 'System', '历史': 'History', '队列': 'Queue', '媒体库': 'Media Library',
        '工具': 'Tools', 'Token': 'Token', '口令': 'Access Token', '更多': 'More', '任务进行中': 'Task running', '浅色': 'Light', '深色': 'Dark',
        // 主界面
        '选择视频或音频': 'Select Video or Audio', '上传文件': 'Upload File', '批量上传': 'Batch Upload',
        'NAS文件': 'NAS Files', 'NAS 文件': 'NAS Files', '点击或拖拽文件到此处处': 'Click or drop files here',
        '点击或拖拽文件到此处': 'Click or drop files here', '生成设置': 'Generation Settings',
        '模型大小': 'Model Size', '语言': 'Language', '字幕格式': 'Subtitle Format', '翻译为': 'Translate To',
        '保存位置': 'Save Location', '命名': 'Naming', '自动断句字数': 'Auto Line-break Length',
        '术语表': 'Glossary', '生成字幕': 'Generate Subtitles', '生成结果': 'Results',
        '字幕目录': 'Subtitles folder', '源文件目录': 'Source file folder', '视频名.srt': '<video>.srt',
        '自动检测': 'Auto detect', '不翻译': 'No translation',
        '字幕保存到源文件目录': 'Save subtitles next to source',
        '双语字幕（每种语言最多两行）': 'Bilingual subtitles (max 2 lines per language)',
        '说话人分离（[说话人N] 前缀）': 'Speaker diarization ([Speaker N] prefix)',
        'Tiny (最快)': 'Tiny (fastest)', 'Base (平衡)': 'Base (balanced)', 'Small (较准)': 'Small (accurate)',
        'Medium (准确)': 'Medium (more accurate)', 'Large V3 (高精度·较慢)': 'Large V3 (best, slower)',
        'Turbo (速度优先)': 'Turbo (speed first)', 'SRT 字幕': 'SRT subtitles', 'VTT 字幕': 'VTT subtitles',
        'JSON 数据': 'JSON data', '纯文本 TXT': 'Plain text TXT', 'ASS 字幕': 'ASS subtitles', '全部格式': 'All formats',
        '中文': 'Chinese', '英语': 'English', '日语': 'Japanese', '韩语': 'Korean', '西班牙语': 'Spanish',
        '法语': 'French', '德语': 'German',
        '全局时间偏移': 'Global time offset', '应用到所有字幕': 'Apply to all subtitles',
        '正数=延后，负数=提前': 'Positive = delay, negative = advance', '搜索字幕...': 'Search subtitles...',
        '时间（新→旧）': 'Time (new→old)',
        // 字幕操作按钮
        '预览': 'Preview', '编辑': 'Edit', '下载': 'Download', '烧录': 'Burn', '封装': 'Soft-sub Mux',
        '质检': 'QC', '转换': 'Convert', '翻译': 'Translate', '总结': 'Summarize', '重命名': 'Rename',
        '删除': 'Delete', '成片': 'Video', '已有字幕': 'Subtitled',
        // 状态与提示
        '处理历史': 'Processing History', '清空': 'Clear', '导出': 'Export', '已完成': 'Done',
        '模型:': 'Model:', '格式:': 'Format:', '设备:': 'Device:', '检测语言:': 'Detected:', '时长:': 'Duration:',
        '处理时间:': 'Processed at:', '正在生成字幕...': 'Generating subtitles...',
        '字幕生成任务已入队': 'Subtitle task queued', '烧录任务已启动': 'Burn task started',
        '正在烧录字幕...': 'Burning subtitles...', '烧录完成！': 'Burn complete!',
        '字幕烧录完成！': 'Subtitles burned!', '正在封装软字幕...': 'Muxing soft subtitles...',
        '封装完成！': 'Mux complete!', '软字幕封装完成！': 'Soft subtitles muxed!',
        '软字幕封装已启动': 'Soft-sub mux started', '字幕烧录完成': 'Subtitles burned',
        '上传成功': 'Upload successful', '上传失败': 'Upload failed', '文件删除成功': 'Deleted',
        '下载已启动': 'Download started', '已取消': 'Canceled', '任务已被取消': 'Task canceled',
        // 媒体库
        '媒体库（已上传文件）': 'Media Library (uploaded files)',
        // 工具中心
        '工具中心': 'Tools', '清理旧文件': 'Clean old files', '清理预览缓存': 'Clean preview cache',
        '清理失败任务': 'Clean failed tasks', '清空历史': 'Clear history', '模型信息': 'Model Info',
        'Whisper 模型管理': 'Whisper Models', '资源模式': 'Resource Profile', '通知': 'Notifications',
        'AI 翻译': 'AI Translation', '任务报告': 'Task Report', 'Whisper 语音识别模型': 'Whisper ASR Models',
        '已缓存': 'Cached', '未下载': 'Not downloaded', '下载中...': 'Downloading...',
        '下载': 'Download', '删除': 'Delete',
        '预下载后离线可用；删除已缓存的模型可释放空间（正在使用的档位删掉后下次任务会自动重新下载）':
            'Pre-download for offline use; deleting cached models frees space (in-use models re-download automatically on next task)',
        '首次使用某档位时也会自动按需下载（走 hf-mirror 镜像）':
            'Models also auto-download on first use (via hf-mirror)',
        // 编辑器
        '片段:': 'Cues:', '时长:': 'Duration:', '保存': 'Save', '重置': 'Reset', '+ 片段': '+ Cue',
        '提前 0.5s': '−0.5s', '延后 0.5s': '+0.5s', '播放暂停': 'Play/Pause', '切换片段': 'Switch cue',
        '字幕编辑器': 'Subtitle Editor',
        // 设置弹层
        'AI 翻译引擎配置已保存': 'AI translation settings saved',
        '烧录样式设置': 'Burn Style', '默认': 'Default', '大号': 'Large', '加粗': 'Bold', '顶部': 'Top',
        '确认操作': 'Confirm', '取消': 'Cancel', '确定': 'OK', '输入': 'Input',
        // 系统信息
        '系统信息': 'System Info', '加载中...': 'Loading...',
        // 页脚
        '基于 faster-whisper · 本地运行 · 数据不上传': 'Powered by faster-whisper · Runs locally · Data never leaves your NAS',
        '推送': 'Push', '说话人': 'Speakers', '纪要': 'Minutes', '更多': 'More',
        '推送字幕到媒体库': 'Push subtitles to library', '目标目录（NAS 媒体目录内）': 'Target folder (inside NAS media dir)',
        '命名方式': 'Naming', '自动（按字幕语言加后缀）': 'Auto (suffix by language)', '同名 .srt': 'Same name .srt',
        '中文 .zh.srt': 'Chinese .zh.srt', '推送到媒体库': 'Push', '推送': 'Push',
        '说话人命名': 'Speaker Naming', '应用命名': 'Apply Names', '真实姓名': 'Real name',
        '监控自动字幕': 'Auto Subtitles', '云端 ASR': 'Cloud ASR',
        '启用监控（自动为目录内新视频生成字幕）': 'Watch folder: auto-generate subtitles for new videos',
        '监控目录（NAS 媒体目录内的子目录）': 'Watch folder (subfolder of NAS media dir)',
        '模型': 'Model', '翻译为': 'Translate To', '双语字幕': 'Bilingual', '说话人分离': 'Diarization',
        '运行时段（小时，如 0-7；留空=全天）': 'Run window (hours, e.g. 0-7; empty=all day)',
        '文件稳定等待（秒）': 'Stability wait (sec)', '立即扫描': 'Scan Now', '保存': 'Save',
        '云端转写（优先云端，失败自动回退本地 Whisper；说话人分离任务始终用本地）':
            'Cloud transcription (falls back to local Whisper on failure; diarization tasks always use local)',
        '接口地址（OpenAI 兼容，自动补 /audio/transcriptions）': 'Endpoint (OpenAI compatible; /audio/transcriptions appended)',
        '模型名': 'Model name', 'API Key（留空保留原值）': 'API Key (leave empty to keep)',
        '双语对照质检': 'Bilingual QC', '原文 (EN)': 'Source (EN)', '译文 (ZH，可编辑)': 'Translation (ZH, editable)',
        '质检提示': 'QC Notes', '保存修改': 'Save Changes', '关闭': 'Close',
        '会议纪要': 'Meeting Minutes'
    };

    var enabled = false;
    try { enabled = localStorage.getItem('aisub_lang') === 'en'; } catch (e) {}
    var observer = null;
    var debounceTimer = null;

    // 含插值的前缀规则（精确词典覆盖不到的动态文本）
    var PREFIX_RULES = [
        [/^已缓存/, 'Cached '],
        [/^下载已启动/, 'Download started: '],
        [/^正在下载/, 'Downloading: ']
    ];

    function translateTextNode(node) {
        var t = node.nodeValue;
        if (!t) return;
        var key = t.trim();
        if (!key) return;
        var hit = DICT[key];
        if (hit && hit !== key) {
            node.nodeValue = t.replace(key, hit);
            return;
        }
        for (var i = 0; i < PREFIX_RULES.length; i++) {
            if (PREFIX_RULES[i][0].test(key)) {
                node.nodeValue = t.replace(key, t.replace(PREFIX_RULES[i][0], PREFIX_RULES[i][1]));
                return;
            }
        }
    }

    function walk(root) {
        if (!root) return;
        var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
        var batch = [];
        var n;
        while ((n = w.nextNode())) batch.push(n);
        for (var i = 0; i < batch.length; i++) translateTextNode(batch[i]);
    }

    function translateAttributes(root) {
        var els = root.querySelectorAll ? root.querySelectorAll('[placeholder],[title]') : [];
        for (var i = 0; i < els.length; i++) {
            var el = els[i];
            var ph = el.getAttribute('placeholder');
            if (ph && DICT[ph]) el.setAttribute('placeholder', DICT[ph]);
            var ti = el.getAttribute('title');
            if (ti && DICT[ti]) el.setAttribute('title', DICT[ti]);
        }
    }

    function translateAll() {
        walk(document.body);
        translateAttributes(document.body);
    }

    function startObserver() {
        if (observer) return;
        observer = new MutationObserver(function () {
            if (debounceTimer) clearTimeout(debounceTimer);
            debounceTimer = setTimeout(function () { translateAll(); }, 80);
        });
        observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    }

    function updateButton() {
        var b = document.getElementById('langToggle');
        if (!b) return;
        b.textContent = enabled ? '中' : 'EN';
        b.title = enabled ? '切换到中文' : 'Switch to English';
    }

    window.toggleUiLanguage = function () {
        enabled = !enabled;
        try { localStorage.setItem('aisub_lang', enabled ? 'en' : 'zh'); } catch (e) {}
        if (enabled) {
            document.documentElement.setAttribute('lang', 'en');
            startObserver();
            translateAll();
        } else {
            location.reload(); // 恢复中文：最简单可靠的方式是回到原始 DOM
            return;
        }
        updateButton();
    };

    function init() {
        updateButton();
        if (enabled) {
            document.documentElement.setAttribute('lang', 'en');
            startObserver();
            translateAll();
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
