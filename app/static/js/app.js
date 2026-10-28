// 全局变量
let uploadedFile = null;
let currentTaskId = null;
let statusCheckInterval = null;
let currentSource = 'upload'; // 'upload', 'batch', or 'nas'
let currentModel = 'small'; // 当前选择的模型（2026-10-02 基准实测 small 4.4x实时且准确率/分段远优于 tiny）
let batchFiles = []; // 批量文件列表
let currentBatchId = null; // 当前批量任务ID
let batchStatusInterval = null; // 批量状态检查定时器
let subtitleSearchTerm = '';
let subtitleSortBy = 'created'; // 'created', 'name', 'size'
let subtitleSortOrder = 'desc'; // 'asc', 'desc'
let allSubtitles = [];
let searchDebounceTimer = null;
let nasCurrentDir = '';
let nasParentDir = '';

// 鉴权 token（后端启用 AUTH_TOKEN 时前端需携带）
// 优先从 localStorage 读取；URL 带 ?token= 时自动保存并从地址栏抹掉
let AUTH_TOKEN = (function() {
    try {
        const params = new URLSearchParams(window.location.search);
        const tokenFromUrl = params.get('token') || '';
        if (tokenFromUrl) {
            localStorage.setItem('authToken', tokenFromUrl);
            params.delete('token');
            const nextQuery = params.toString();
            const nextUrl = window.location.pathname + (nextQuery ? '?' + nextQuery : '') + window.location.hash;
            window.history.replaceState({}, document.title, nextUrl);
            return tokenFromUrl;
        }
        return localStorage.getItem('authToken') || '';
    } catch(e) {
        return '';
    }
})();

// ===== 鉴权门槛 =====
// 原实现 token 失效时所有 API 静默 401：列表全空、生成失败、界面毫无提示，
// 用户看起来就是"整个应用坏了"（本服务 2026-10-02 判定"完全不可用"的真正原因）。
// 现在任何 API 返回 401 都弹全屏令牌输入层，保存前先调 /api/auth/check 校验。
let _authGateShown = false;
function onAuthRequired() {
    if (_authGateShown) return;
    _authGateShown = true;
    const modal = document.getElementById('authGateModal');
    if (!modal) return;
    modal.style.display = 'flex';
    const input = document.getElementById('authGateInput');
    if (input) {
        input.value = localStorage.getItem('authToken') || '';
        setTimeout(function() { input.focus(); }, 100);
    }
}

async function submitAuthGate() {
    const input = document.getElementById('authGateInput');
    const err = document.getElementById('authGateError');
    const value = (input ? input.value : '').trim();
    if (!value) {
        if (err) { err.textContent = '请输入 Token'; err.style.display = 'block'; }
        return;
    }
    try {
        const resp = await fetch('/api/auth/check', { headers: { 'Authorization': 'Bearer ' + value } });
        if (resp.status === 200) {
            AUTH_TOKEN = value;
            const remember = document.getElementById('rememberAuthToken');
            if (remember && remember.checked) localStorage.setItem('authToken', value);
            else localStorage.removeItem('authToken');
            document.getElementById('authGateModal').style.display = 'none';
            _authGateShown = false;
            if (input) input.value = '';
            loadSubtitles();
            return;
        }
        if (err) { err.textContent = 'Token 无效，请核对后重试'; err.style.display = 'block'; }
    } catch (e) {
        if (err) { err.textContent = '校验失败: ' + e.message; err.style.display = 'block'; }
    }
}

async function verifyAuthOnStart() {
    try {
        const resp = await apiFetch('/api/auth/check');
        if (resp.status === 401) onAuthRequired();
    } catch (e) { /* 网络异常不弹门槛，交给各请求自己的错误提示 */ }
}

// 统一的 fetch 封装：自动注入 Authorization 头（启用鉴权时必需）
async function apiFetch(url, options = {}) {
    if (AUTH_TOKEN) {
        options.headers = options.headers || {};
        if (!options.headers['Authorization']) {
            options.headers['Authorization'] = 'Bearer ' + AUTH_TOKEN;
        }
    }
    const resp = await fetch(url, options);
    if (resp.status === 401) onAuthRequired();
    return resp;
}

function withAuthQuery(url) {
    if (!AUTH_TOKEN) return url;
    try {
        const parsed = new URL(url, window.location.origin);
        parsed.searchParams.set('token', AUTH_TOKEN);
        return parsed.pathname + parsed.search + parsed.hash;
    } catch (e) {
        return url;
    }
}

// Safe DOM getter
function $(id) { const el = document.getElementById(id); if (!el) console.warn('Missing element:', id); return el; }

const uploadArea = $('uploadArea');
const fileInput = $('fileInput');
const uploadProgress = $('uploadProgress');
const progressFill = $('progressFill');
const statusProgressFill = $('statusProgressFill');
const progressText = $('progressText');
const generateBtn = $('generateBtn');
const cancelBtn = $('cancelBtn');
const modelSelect = $('modelSelect');
const languageSelect = $('languageSelect');
const formatSelect = $('formatSelect');
const statusSection = $('statusSection');
const statusIcon = $('statusIcon');
const statusText = $('statusText');
const statusDetails = $('statusDetails');
const progressContainer = $('progressContainer');
const progressLabel = $('progressLabel');
const progressPercent = $('progressPercent');
const progressInfo = $('progressInfo');
const subtitlesList = $('subtitlesList');
const toast = $('toast');
const nasPathSection = $('nasPathSection');
const nasPathInput = $('nasPath');
const fileBrowser = $('fileBrowser');
const fileList = $('fileList');
const batchUploadSection = $('batchUploadSection');
const batchUploadArea = $('batchUploadArea');
const batchFileInput = $('batchFileInput');
const batchFileList = $('batchFileList');
const batchFileItems = $('batchFileItems');
const batchUploadProgress = $('batchUploadProgress');
const batchProgressFill = $('batchUploadProgressFill');
const batchProgressText = $('batchProgressText');
const batchStatusSection = $('batchStatusSection');
const batchTasksList = $('batchTasksList');

function parseGlossary() {
    const text = (document.getElementById('glossaryText') || {}).value || '';
    const glossary = {};
    text.split('\n').forEach(line => {
        const idx = line.indexOf('=');
        if (idx > 0) {
            const key = line.slice(0, idx).trim();
            const value = line.slice(idx + 1).trim();
            if (key) glossary[key] = value;
        }
    });
    return glossary;
}

function getGenerationOptions() {
    return {
        model: modelSelect.value,
        language: languageSelect.value,
        format: formatSelect.value,
        translate_to: document.getElementById('translateSelect').value || null,
        save_to_source_dir: document.getElementById('saveToSourceDir').checked,
        save_strategy: document.getElementById('saveStrategySelect').value,
        output_naming: document.getElementById('outputNamingSelect').value,
        bilingual: document.getElementById('bilingualSubtitle').checked,
        split_max_chars: parseInt(document.getElementById('splitMaxChars').value || '0', 10) || 0,
        glossary: parseGlossary(),
        diarize: document.getElementById('diarizeToggle') ? document.getElementById('diarizeToggle').checked : false,
        num_speakers: parseInt((document.getElementById('numSpeakersSelect') || {}).value || '0', 10) || 0
    };
}

// Initialization moved to bottom of file

// 切换文件来源
// ==== 生成设置持久化（2026-10-26）：记住上次的模型/语言/格式等选择 ====
var GEN_SETTINGS_IDS = ['modelSelect', 'languageSelect', 'formatSelect', 'translateSelect',
                        'saveStrategySelect', 'outputNamingSelect'];
function saveGenSettings() {
    try {
        var st = {};
        GEN_SETTINGS_IDS.forEach(function(id) {
            var el = document.getElementById(id);
            if (el) st[id] = el.value;
        });
        var lb = document.getElementById('splitMaxChars');
        if (lb) st.sentenceLength = lb.value;
        localStorage.setItem('aisub_genset', JSON.stringify(st));
    } catch (e) {}
}
function restoreGenSettings() {
    try {
        var st = JSON.parse(localStorage.getItem('aisub_genset') || '{}');
        GEN_SETTINGS_IDS.forEach(function(id) {
            var el = document.getElementById(id);
            if (el && st[id]) el.value = st[id];
        });
        var lb = document.getElementById('splitMaxChars');
        if (lb && st.sentenceLength) lb.value = st.sentenceLength;
    } catch (e) {}
}

function toggleSource() {
    const selected = document.querySelector('input[name="source"]:checked').value;
    currentSource = selected;
    
    if (selected === 'upload') {
        uploadArea.style.display = 'block';
        batchUploadSection.style.display = 'none';
        if (nasPathSection) nasPathSection.style.display = 'none';
        uploadedFile = null;
        batchFiles = [];
    } else if (selected === 'batch') {
        uploadArea.style.display = 'none';
        batchUploadSection.style.display = 'block';
        if (nasPathSection) nasPathSection.style.display = 'none';
        uploadedFile = null;
    } else {
        uploadArea.style.display = 'none';
        batchUploadSection.style.display = 'none';
        if (nasPathSection) nasPathSection.style.display = 'block';
        uploadedFile = null;
        batchFiles = [];
        // 首次切入自动加载目录列表（2026-10-26：省掉手动点「刷新」的死步骤）
        if (typeof listNasFiles === 'function' && !window.__nasAutoLoaded) {
            window.__nasAutoLoaded = true;
            listNasFiles();
        }
    }
}

// 取消生成
async function cancelGeneration() {
    if (currentTaskId) {
        try {
            const response = await apiFetch(`/api/cancel/${currentTaskId}`, {
                method: 'POST'
            });
            
            const result = await response.json();
            
            if (result.success) {
                // 停止状态检查
                if (statusCheckInterval) {
                    clearTimeout(statusCheckInterval);
                    statusCheckInterval = null;
                }
                
                // 重置UI
                currentTaskId = null;
                statusSection.style.display = 'none';
                generateBtn.disabled = false;
                generateBtn.innerHTML = '<span class="btn-icon">⚡</span> 生成字幕';
                cancelBtn.style.display = 'none';
                
                showToast('已取消生成任务', 'info');
            } else {
                showToast('取消失败: ' + result.error, 'error');
            }
        } catch (error) {
            showToast('取消失败: ' + error.message, 'error');
        }
    }
}

// 浏览NAS文件
async function listNasFiles() {
    try {
        const params = new URLSearchParams();
        if (nasCurrentDir) params.set('dir', nasCurrentDir);
        const search = document.getElementById('nasSearchInput')?.value || '';
        if (search) params.set('search', search);
        if (document.getElementById('nasRecursive')?.checked) params.set('recursive', '1');
        const response = await apiFetch('/api/nas-files?' + params.toString());
        const result = await response.json();
        
        if (result.success) {
            nasCurrentDir = result.current_dir || '';
            nasParentDir = result.parent_dir || '';
            const pathLabel = document.getElementById('nasCurrentPath');
            if (pathLabel) pathLabel.textContent = 'NAS: /' + (nasCurrentDir || '');
            renderFileList(result);
            if (fileBrowser) fileBrowser.style.display = 'block';
        } else {
            showToast('无法访问NAS文件: ' + result.error, 'error');
        }
    } catch (error) {
        showToast('获取文件列表失败: ' + error.message, 'error');
    }
}

// 渲染文件列表
function renderFileList(result) {
    if (!fileList) return;
    fileList.innerHTML = '';
    const files = result.files || [];
    const directories = result.directories || [];
    
    if (files.length === 0 && directories.length === 0) {
        fileList.innerHTML = '<div class="empty-state"><p>未找到视频文件</p></div>';
        return;
    }

    directories.forEach(dir => {
        const item = document.createElement('div');
        item.className = 'file-item directory-item';
        item.innerHTML = `
            <span class="file-icon">📁</span>
            <span class="file-name">${escapeHtml(dir.name)}</span>
            <span class="file-size"></span>
        `;
        item.onclick = () => {
            nasCurrentDir = dir.relative_path || '';
            listNasFiles();
        };
        fileList.appendChild(item);
    });
    
    files.forEach(file => {
        const item = document.createElement('div');
        item.className = 'file-item';
        const subtitleBadge = file.has_subtitle ? '<span class="status-good">已有字幕</span>' : '<span class="status-bad">无字幕</span>';
        // 2026-06-22 新增：提取字幕按钮（事件 stopPropagation 避免触发 selectNasFile）
        item.innerHTML = `
            <span class="file-icon">🎬</span>
            <span class="file-name">${escapeHtml(file.relative_path || file.name)}</span>
            ${subtitleBadge}
            <span class="file-size">${formatFileSize(file.size)}</span>
            <button class="btn btn-secondary btn-tiny extract-subtitle-btn" title="提取内嵌字幕">📝 字幕</button>
        `;
        item.querySelector('.extract-subtitle-btn').addEventListener('click', event => {
            event.stopPropagation();
            checkAndExtractSubtitle(file.path);
        });
        item.onclick = () => selectNasFile(file.path);
        fileList.appendChild(item);
    });
}

function goNasParent() {
    nasCurrentDir = nasParentDir || '';
    listNasFiles();
}

// 选择NAS文件
function selectNasFile(path) {
    if (nasPathInput) nasPathInput.value = path;
    closeFileBrowser();
    
    // 设置为已选择文件
    uploadedFile = {
        name: path.split('/').pop(),
        path: path,
        isNasFile: true
    };
    
    generateBtn.disabled = false;
    showToast('已选择NAS文件: ' + path, 'success');
}

// 关闭文件浏览器
function closeFileBrowser() {
    if (fileBrowser) fileBrowser.style.display = 'none';
}

// 初始化上传区域
function initUploadArea() {
    // 点击上传
    uploadArea.addEventListener('click', () => {
        fileInput.click();
    });

    // 文件选择
    fileInput.addEventListener('change', handleFileSelect);

    // 拖拽上传
    // 生成设置：启动恢复 + 变化即存
    restoreGenSettings();
    GEN_SETTINGS_IDS.concat(['splitMaxChars']).forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.addEventListener('change', saveGenSettings);
    });

    uploadArea.addEventListener('dragover', (e) => {
        e.preventDefault();
        uploadArea.classList.add('dragover');
    });

    uploadArea.addEventListener('dragleave', (e) => {
        e.preventDefault();
        uploadArea.classList.remove('dragover');
    });

    uploadArea.addEventListener('drop', (e) => {
        e.preventDefault();
        uploadArea.classList.remove('dragover');
        
        const files = e.dataTransfer.files;
        if (files.length > 0) {
            handleFile(files[0]);
        }
    });

    // 生成按钮
    generateBtn.addEventListener('click', () => {
        if (currentSource === 'batch') {
            generateBatchSubtitle();
        } else {
            generateSubtitle();
        }
    });
}

// 处理文件选择
function handleFileSelect(e) {
    const file = e.target.files[0];
    if (file) {
        handleFile(file);
    }
}

// 处理文件
function handleFile(file) {
    const MAX_SIZE = 8 * 1024 * 1024 * 1024; // 8GB（与后端 MAX_CONTENT_LENGTH 一致，原前端 2GB 是旧限制）
    const fileExt = file.name.split('.').pop().toLowerCase();
    const allowedExts = ['mp4', 'avi', 'mkv', 'mov', 'wmv', 'flv', 'webm', 'm4v', 'mpg', 'mpeg', 'mp3', 'wav', 'flac', 'aac', 'ogg', 'm4a'];
    
    if (!allowedExts.includes(fileExt)) {
        showToast('不支持的文件格式', 'error');
        return;
    }
    
    if (file.size > MAX_SIZE) {
        showToast('文件过大，最大支持8GB', 'error');
        return;
    }

    uploadedFile = file;
    uploadFile(file);
}

// 上传文件 (XHR with real progress)
function uploadFile(file) {
    const formData = new FormData();
    formData.append('video', file);
    
    const xhr = new XMLHttpRequest();
    
    uploadProgress.style.display = 'block';
    progressFill.style.width = '0%';
    progressText.textContent = '准备上传...';
    
    xhr.upload.addEventListener('progress', function(e) {
        if (e.lengthComputable) {
            const pct = Math.round((e.loaded / e.total) * 100);
            progressFill.style.width = pct + '%';
            progressText.textContent = `上传中 ${formatFileSize(e.loaded)} / ${formatFileSize(e.total)}`;
        }
    });
    
    xhr.addEventListener('load', function() {
        if (xhr.status >= 200 && xhr.status < 300) {
            const result = JSON.parse(xhr.responseText);
            if (result.success) {
                progressFill.style.width = '100%';
                progressText.textContent = '上传成功！';
                uploadedFile = { name: result.filename, path: result.filepath };
                generateBtn.disabled = false;
                showToast('文件上传成功', 'success');
                setTimeout(function() { uploadProgress.style.display = 'none'; }, 2000);
            } else {
                uploadProgress.style.display = 'none';
                showToast('上传失败: ' + (result.error || '未知错误'), 'error');
            }
        } else {
            uploadProgress.style.display = 'none';
            showToast('上传失败: 服务器错误 ' + xhr.status, 'error');
        }
    });
    
    xhr.addEventListener('error', function() {
        uploadProgress.style.display = 'none';
        showToast('上传失败: 网络错误', 'error');
    });
    
    xhr.open('POST', '/api/upload');
    if (AUTH_TOKEN) xhr.setRequestHeader('Authorization', 'Bearer ' + AUTH_TOKEN);
    xhr.send(formData);
}

// 生成字幕
async function generateSubtitle() {
    if (!uploadedFile) {
        showToast('请先选择视频文件', 'error');
        return;
    }

    const options = getGenerationOptions();

    generateBtn.disabled = true;
    generateBtn.innerHTML = '<span class="loading"></span> 处理中...';
    cancelBtn.style.display = 'inline-flex';

    try {
        const response = await apiFetch('/api/generate', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                filename: uploadedFile.name,
                path: uploadedFile.path || null,
                is_nas_file: uploadedFile.isNasFile || false,
                ...options
            })
        });

        const result = await response.json();

        if (result.success) {
            currentTaskId = result.task_id;
            showStatusSection('processing');
            startStatusCheck();
            showToast('字幕生成任务已启动', 'info');
        } else {
            throw new Error(result.error);
        }
    } catch (error) {
        generateBtn.disabled = false;
        generateBtn.innerHTML = '<span class="btn-icon">⚡</span> 生成字幕';
        cancelBtn.style.display = 'none';
        showToast('生成失败: ' + error.message, 'error');
    }
}

// 显示状态区域
function showStatusSection(status) {
    try {
        var dot = document.getElementById('navTaskDot');
        if (dot) dot.className = (status === 'processing') ? 'running' : '';
    } catch (e) {}
    statusSection.style.display = 'block';
    const statusCard = statusSection.querySelector('.status-card');
    
    statusCard.className = 'status-card ' + status;
    
    if (status === 'processing') {
        statusIcon.textContent = '⏳';
        statusText.textContent = '正在生成字幕...';
        statusDetails.textContent = '这可能需要几分钟时间，请耐心等待。';
        progressContainer.style.display = 'block';
        // 重置 ETA 跟踪（每轮任务的 phase 重新计时）
        resetProgressTracking();
        updateProgress(0, '正在加载模型...');
    } else if (status === 'completed') {
        statusIcon.textContent = '✅';
        statusText.textContent = '字幕生成完成！';
        progressContainer.style.display = 'none';
    } else if (status === 'failed') {
        statusIcon.textContent = '❌';
        statusText.textContent = '生成失败';
        progressContainer.style.display = 'none';
    }
}

// 更新进度
// 进度条 ETA 跟踪（2026-06-02 进度条准确度优化）
let phaseStartTime = 0;
let phaseStartPercent = 0;
let lastPercent = 0;

function resetProgressTracking() {
    phaseStartTime = Date.now();
    phaseStartPercent = 0;
    lastPercent = 0;
}

function formatEta(seconds) {
    if (!isFinite(seconds) || seconds < 0 || seconds > 3600) return '';
    if (seconds < 5) return ' 即将完成';
    if (seconds < 60) return ` 预计还需 ${Math.round(seconds)} 秒`;
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return ` 预计还需 ${m} 分 ${s} 秒`;
}

function updateProgress(percent, info) {
    // 支持小数 percent（后端转录/翻译阶段用 0.1% 步进，配合 CSS transition 0.4s 丝滑滑动）
    const p = Math.max(0, Math.min(100, parseFloat(percent) || 0));
    if (statusProgressFill) {
        // 保留 1 位小数让 CSS transition 看到渐变
        statusProgressFill.style.width = p.toFixed(1) + '%';
    }
    progressPercent.textContent = Math.round(p) + '%';

    // ETA 计算：基于 phase 开始时间和当前进度
    let etaText = '';
    if (p >= 100) {
        etaText = '';
        lastPercent = p;
    } else if (p < lastPercent - 5) {
        // 进度大幅回退（切 phase / 取消 / 重置）→ 重新跟踪
        phaseStartTime = Date.now();
        phaseStartPercent = p;
        lastPercent = p;
    } else if (p > lastPercent + 0.05 && phaseStartTime > 0) {
        // 进度有进展时估算剩余时间
        const elapsedSec = (Date.now() - phaseStartTime) / 1000;
        const pctDelta = p - phaseStartPercent;
        if (pctDelta > 0.5 && elapsedSec > 1) {
            const secPerPct = elapsedSec / pctDelta;
            const etaSec = secPerPct * (100 - p);
            etaText = formatEta(etaSec);
        }
        lastPercent = p;
    }

    progressInfo.textContent = (info || '') + etaText;
}

// 开始状态检查
// Bug #20 修复：标签页隐藏时暂停轮询，节省资源
let _savedStatusCheckInterval = null;
let _savedBatchStatusInterval = null;
let _savedBurnStatusInterval = null;
document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        if (statusCheckInterval) { _savedStatusCheckInterval = statusCheckInterval; clearTimeout(statusCheckInterval); statusCheckInterval = null; }
        if (batchStatusInterval) { _savedBatchStatusInterval = batchStatusInterval; clearTimeout(batchStatusInterval); batchStatusInterval = null; }
        if (burnStatusInterval) { _savedBurnStatusInterval = burnStatusInterval; clearTimeout(burnStatusInterval); burnStatusInterval = null; }
    } else {
        if (currentTaskId && !statusCheckInterval) { scheduleNextPoll(); }
        if (currentBatchId && !batchStatusInterval) { scheduleNextBatchPoll && scheduleNextBatchPoll(); }
        if (burnTaskId && !burnStatusInterval) { checkBurnStatus(); }
    }
});

let pollFailures = 0;
function startStatusCheck() {
    if (statusCheckInterval) {
        clearTimeout(statusCheckInterval);
    }
    pollFailures = 0;
    scheduleNextPoll();
}

function scheduleNextPoll() {
    const baseDelay = 2000;
    const maxDelay = 30000;
    const delay = Math.min(baseDelay * Math.pow(2, pollFailures), maxDelay);
    statusCheckInterval = setTimeout(checkStatus, delay);
}

// 检查状态
async function checkStatus() {
    if (!currentTaskId) return;

    try {
        const response = await apiFetch(`/api/status/${encodeURIComponent(currentTaskId)}`);
        const result = await response.json();

        if (!result.success || !result.task) throw new Error(result.error || "任务状态暂不可用");
        if (result.success && result.task) {
            pollFailures = 0;
            const task = result.task;

            // 更新进度
            if (task.progress !== undefined) {
                updateProgress(task.progress, task.progress_info || '处理中...');
            }

            if (task.status === 'completed') {
                clearTimeout(statusCheckInterval);
                showStatusSection('completed');
                
                if (task.result) {
                    const details = `
                        <strong>视频名称:</strong> ${escapeHtml(task.result.video_name)}<br>
                        <strong>检测语言:</strong> ${escapeHtml(task.result.detected_language)}<br>
                        <strong>视频时长:</strong> ${formatDuration(task.result.duration)}<br>
                        <strong>使用设备:</strong> ${escapeHtml(task.result.device_used || 'cpu')}<br>
                        <strong>模型大小:</strong> ${escapeHtml(task.result.model_used || 'base')}<br>
                        <strong>生成时间:</strong> ${new Date(task.end_time).toLocaleString()}<br>
                        ${(task.result.warnings || []).map(w => '<strong>提示:</strong> ' + escapeHtml(w)).join('<br>')}
                    `;
                    statusDetails.innerHTML = details;
                    
                    // 添加到历史记录
                    addHistoryItem({
                        filename: task.filename,
                        status: 'completed',
                        model: task.result.model_used,
                        language: languageSelect.value,
                        format: formatSelect.value,
                        device: task.result.device_used,
                        result: task.result,
                        is_nas_file: task.is_nas_file
                    });
                }

                generateBtn.disabled = false;
                generateBtn.innerHTML = '<span class="btn-icon">⚡</span> 生成字幕';
                cancelBtn.style.display = 'none';
                currentTaskId = null;
                pollFailures = 0;
                
                loadSubtitles();
                showToast('字幕生成成功！', 'success');
                notifyDone('字幕生成完成', task.filename || '任务已完成');
            } else if (task.status === 'failed') {
                clearTimeout(statusCheckInterval);
                showStatusSection('failed');
                statusDetails.textContent = task.error || '生成失败，请重试';
                
                generateBtn.disabled = false;
                generateBtn.innerHTML = '<span class="btn-icon">⚡</span> 生成字幕';
                cancelBtn.style.display = 'none';
                currentTaskId = null;
                pollFailures = 0;
                
                showToast('字幕生成失败', 'error');
                notifyDone('字幕生成失败', task.error || '请查看任务详情');
            } else if (task.status === 'cancelling') {
                // Bug #13 修复：cancelling 是"已请求取消"中间态，继续轮询等待 cancelled
                if (cancelBtn) cancelBtn.disabled = true;
                if (statusText) statusText.textContent = '正在取消...';
            } else if (task.status === 'cancelled') {
                clearTimeout(statusCheckInterval);
                showStatusSection('failed');
                statusDetails.textContent = '任务已被取消';

                generateBtn.disabled = false;
                generateBtn.innerHTML = '<span class="btn-icon">⚡</span> 生成字幕';
                cancelBtn.style.display = 'none';
                currentTaskId = null;
                pollFailures = 0;

                showToast('任务已取消', 'info');
            }
        }
    } catch (error) {
        console.error('检查状态失败:', error);
        pollFailures++;
    } finally {
        if (currentTaskId) scheduleNextPoll();
    }
}

// 格式化时长
function formatDuration(seconds) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);

    if (hours > 0) {
        return `${hours}小时${minutes}分${secs}秒`;
    } else if (minutes > 0) {
        return `${minutes}分${secs}秒`;
    } else {
        return `${secs}秒`;
    }
}

// 加载字幕列表
async function loadSubtitles() {
    try {
        const response = await apiFetch('/api/subtitles');
        const result = await response.json();
        if (result.success) {
            allSubtitles = result.subtitles;
            filterAndRenderSubtitles();
        }
    } catch (error) {
        console.error('加载字幕列表失败:', error);
    }
}

function filterAndRenderSubtitles() {
    if (searchDebounceTimer) clearTimeout(searchDebounceTimer);
    searchDebounceTimer = setTimeout(doFilterAndRender, 200);
}

function doFilterAndRender() {
    var filtered = allSubtitles;
    if (subtitleSearchTerm) {
        var term = subtitleSearchTerm.toLowerCase();
        filtered = allSubtitles.filter(function(s) {
            return s.filename.toLowerCase().indexOf(term) !== -1;
        });
    }
    filtered.sort(function(a, b) {
        var cmp;
        if (subtitleSortBy === 'name') {
            cmp = a.filename.localeCompare(b.filename);
        } else if (subtitleSortBy === 'size') {
            cmp = a.size - b.size;
        } else {
            cmp = new Date(a.created) - new Date(b.created);
        }
        return subtitleSortOrder === 'desc' ? -cmp : cmp;
    });
    renderSubtitles(filtered);
}

// 渲染字幕列表
function renderSubtitles(subtitles) {
    if (subtitles.length === 0) {
        var emptyMsg = subtitleSearchTerm ? '没有匹配的字幕文件' : '暂无生成的字幕';
        subtitlesList.innerHTML = '<div class="empty-state"><div class="empty-icon">📝</div><p>' + emptyMsg + '</p></div>';
        return;
    }

    subtitlesList.innerHTML = subtitles.map(subtitle => {
        const filenameAttr = escapeAttr(subtitle.filename);
        // 烧录产物视频（kind=video）：只提供下载/删除，字幕专属按钮一律不给
        // （原实现 mp4 混进字幕列表，预览/编辑/烧录等按钮全部错位）
        if (subtitle.kind === 'video') {
            return `
            <div class="subtitle-item">
                <div class="subtitle-main">
                    <div class="subtitle-info">
                        <div class="subtitle-name">${escapeHtml(subtitle.filename)}</div>
                        <div class="subtitle-meta">
                            <span class="format-badge">🎬 成片</span>
                            <span>${formatFileSize(subtitle.size)}</span>
                            <span class="meta-dot"></span>
                            <span>${new Date(subtitle.created).toLocaleString()}</span>
                        </div>
                    </div>
                    <div class="subtitle-actions">
                        <button class="btn btn-secondary btn-small subtitle-action" data-action="download" data-filename="${filenameAttr}">
                            下载
                        </button>
                        <button class="btn btn-ghost-danger btn-small subtitle-action" data-action="delete" data-filename="${filenameAttr}">
                            删除
                        </button>
                    </div>
                </div>
            </div>
        `;
        }
        const format = subtitle.filename.toLowerCase().split('.').pop().toUpperCase();
        return `
            <div class="subtitle-item">
                <div class="subtitle-main">
                    <div class="subtitle-info">
                        <div class="subtitle-name">${escapeHtml(subtitle.filename)}</div>
                        <div class="subtitle-meta">
                            <span class="format-badge">${escapeHtml(format)}</span>
                            <span>${formatFileSize(subtitle.size)}</span>
                            <span class="meta-dot"></span>
                            <span>${new Date(subtitle.created).toLocaleString()}</span>
                        </div>
                    </div>
                    <div class="subtitle-actions">
                        <button class="btn btn-secondary btn-small subtitle-action" data-action="preview" data-filename="${filenameAttr}">
                            预览
                        </button>
                        <button class="btn btn-secondary btn-small subtitle-action" data-action="edit" data-filename="${filenameAttr}">
                            编辑
                        </button>
                        <button class="btn btn-secondary btn-small subtitle-action" data-action="download" data-filename="${filenameAttr}">
                            下载
                        </button>
                        <span class="action-divider"></span>
                        <button class="btn btn-ghost btn-small more-toggle" onclick="toggleMoreActions(this)" title="更多操作">
                            更多
                        </button>
                        <span class="more-group">
                            <button class="btn btn-ghost btn-small subtitle-action" data-action="burn" data-filename="${filenameAttr}" title="烧录到视频">
                                烧录
                            </button>
                            <button class="btn btn-ghost btn-small subtitle-action" data-action="mux" data-filename="${filenameAttr}" title="封装软字幕到 MKV（不重编码，播放器可开关字幕）">
                                封装
                            </button>
                            <button class="btn btn-ghost btn-small subtitle-action" data-action="quality" data-filename="${filenameAttr}">
                                质检
                            </button>
                            <button class="btn btn-ghost btn-small subtitle-action" data-action="convert" data-filename="${filenameAttr}">
                                转换
                            </button>
                            <button class="btn btn-ghost btn-small subtitle-action" data-action="translate" data-filename="${filenameAttr}">
                                翻译
                            </button>
                            <button class="btn btn-ghost btn-small subtitle-action" data-action="summary" data-filename="${filenameAttr}" title="AI 生成内容总结">
                                总结
                            </button>
                            <button class="btn btn-ghost btn-small subtitle-action" data-action="minutes" data-filename="${filenameAttr}" title="AI 生成会议纪要">
                                纪要
                            </button>
                            <button class="btn btn-ghost btn-small subtitle-action" data-action="push" data-filename="${filenameAttr}" title="推送到媒体库目录（按播放器规范命名）">
                                推送
                            </button>
                            <button class="btn btn-ghost btn-small subtitle-action" data-action="speakers" data-filename="${filenameAttr}" title="说话人命名">
                                说话人
                            </button>
                            <button class="btn btn-ghost btn-small subtitle-action" data-action="clip" data-filename="${filenameAttr}" title="按时间段导出视频片段">
                                片段
                            </button>
                            <button class="btn btn-ghost btn-small subtitle-action" data-action="burnbatch" data-filename="" title="批量烧录多份字幕">
                                批量烧录
                            </button>
                            <button class="btn btn-ghost btn-small subtitle-action" data-action="rename" data-filename="${filenameAttr}">
                                重命名
                            </button>
                            <button class="btn btn-ghost-danger btn-small subtitle-action" data-action="delete" data-filename="${filenameAttr}">
                                删除
                            </button>
                        </span>
                    </div>
                </div>
            </div>
        `;
    }).join('');

    subtitlesList.querySelectorAll('.subtitle-action').forEach(function(button) {
        button.addEventListener('click', function(event) {
            event.stopPropagation();
            var filename = button.getAttribute('data-filename') || '';
            var action = button.getAttribute('data-action');
            var row = button.closest('.subtitle-actions');
            if (row && row.classList.contains('open')) row.classList.remove('open');
            if (action === 'preview') previewSubtitle(filename);
            else if (action === 'edit') editSubtitle(filename);
            else if (action === 'download') downloadSubtitle(filename);
            else if (action === 'burn') burnSubtitle(filename);
            else if (action === 'mux') muxSubtitle(filename);
            else if (action === 'quality') {
                var _isBilingual = /(-zh|-en|zh-en|en-zh|双语)/i.test(filename);
                if (_isBilingual) qcCompare(filename); else qualitySubtitle(filename);
            }
            else if (action === 'convert') convertSubtitle(filename);
            else if (action === 'translate') translateSubtitle(filename);
            else if (action === 'summary') summarizeSubtitle(filename, 'summary');
            else if (action === 'minutes') summarizeSubtitle(filename, 'minutes');
            else if (action === 'push') pushSubtitle(filename);
            else if (action === 'speakers') speakerNaming(filename);
            else if (action === 'clip') clipExport(filename);
            else if (action === 'burnbatch') showBurnBatch();
            else if (action === 'rename') renameSubtitle(filename);
            else if (action === 'delete') deleteSubtitle(filename);
        });
    });
}

// 格式化文件大小
function formatFileSize(bytes) {
    if (bytes === 0) return '0 B';
    
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

// 下载字幕
function downloadSubtitle(filename) {
    window.location.href = withAuthQuery('/api/download/' + encodeURIComponent(filename));
}

async function qualitySubtitle(filename) {
    try {
        const resp = await apiFetch('/api/subtitle-quality/' + encodeURIComponent(filename));
        const result = await resp.json();
        if (!result.success) throw new Error(result.error || '质检失败');
        const report = result.report || {};
        const summary = report.summary || {};
        showTools(true);
        writeToolsOutput({
            file: filename,
            score: report.score,
            score_scope: report.score_scope,
            total_segments: report.total_segments,
            duration: report.duration,
            layout: report.layout,
            reading_by_script: report.reading_by_script,
            asr_review_notes: report.asr_review_notes || [],
            thresholds: report.thresholds,
            summary: summary,
            issues: report.issues || []
        });
        showToast('排版与时间检查完成，评分 ' + report.score, report.score >= 80 ? 'success' : 'warning');
    } catch (e) {
        showToast('质检失败: ' + e.message, 'error');
    }
}

async function convertSubtitle(filename) {
    const targetFormat = (await uiPrompt('转换字幕', '转换为哪种格式？可选: srt, vtt, ass, json, txt', {
        defaultValue: 'srt',
        validate: function(v) { return ['srt','vtt','ass','json','txt'].indexOf(v.trim().toLowerCase()) === -1 ? '格式必须是 srt / vtt / ass / json / txt 之一' : null; }
    }));
    if (!targetFormat) return;
    try {
        const resp = await apiFetch('/api/subtitle-convert', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: filename, format: targetFormat.trim().toLowerCase() })
        });
        const result = await resp.json();
        if (!result.success) throw new Error(result.error || '转换失败');
        showToast('已生成 ' + result.filename + ((result.warnings || []).length ? '；' + result.warnings.join('；') : ''), (result.warnings || []).length ? 'warning' : 'success');
        loadSubtitles();
    } catch (e) {
        showToast('转换失败: ' + e.message, 'error');
    }
}

async function translateSubtitle(filename) {
    const targetLang = (await uiPrompt('翻译字幕', '翻译为哪个语言？例如 zh / en / ja', {
        defaultValue: 'en',
        validate: function(v) { return /^[a-z]{2,3}$/.test(v.trim().toLowerCase()) ? null : '语言代码需为 2-3 位小写字母，如 zh / en / ja'; }
    }));
    if (!targetLang) return;
    const sourceLang = await uiPrompt('翻译字幕', '源语言？留空则自动按目标语言推断', { defaultValue: '' });
    if (sourceLang === null) return;
    const bilingual = await uiConfirmAsync('翻译字幕', '是否生成双语字幕？（原文+译文各一行）', { okText: '生成双语', danger: false });
    try {
        const resp = await apiFetch('/api/subtitle-translate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                filename: filename,
                target_lang: targetLang.trim().toLowerCase(),
                source_lang: (sourceLang || '').trim().toLowerCase(),
                bilingual: bilingual,
                glossary: parseGlossary(),
                translate_engine: 'auto'
            })
        });
        const result = await resp.json();
        if (!result.success) throw new Error(result.error || '翻译失败');
        showToast('已生成 ' + result.filename + ((result.warnings || []).length ? '；' + result.warnings.join('；') : ''), (result.warnings || []).length ? 'warning' : 'success');
        loadSubtitles();
    } catch (e) {
        showToast('补翻译失败: ' + e.message, 'error');
    }
}

async function renameSubtitle(filename) {
    const stem = filename.replace(/\.[^.]+$/, '');
    const pattern = await uiPrompt('重命名字幕', '命名规则：可用 {name} {lang} {target} {ext}', {
        defaultValue: '{name}.{lang}{ext}',
        validate: function(v) { return v.indexOf('{ext}') === -1 ? '规则需包含 {ext}（保留原扩展名）' : null; }
    });
    if (!pattern) return;
    const lang = await uiPrompt('重命名字幕', '语言标记，例如 zh / en / zh-en', { defaultValue: 'zh' });
    if (lang === null) return;
    try {
        const resp = await apiFetch('/api/subtitle-rename', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: filename, name: stem, lang: lang.trim(), pattern: pattern })
        });
        const result = await resp.json();
        if (!result.success) throw new Error(result.error || '重命名失败');
        showToast('已重命名为 ' + result.filename, 'success');
        loadSubtitles();
    } catch (e) {
        showToast('重命名失败: ' + e.message, 'error');
    }
}


// ===== BURN SUBTITLE =====
var burnTaskId = null;
var burnStatusInterval = null;

async function burnSubtitle(subtitleFilename) {
    // 打开样式弹层（字号/颜色/描边/位置），对标主流剪辑工具的硬字幕设置
    const modal = document.getElementById('burnStyleModal');
    if (!modal) { doBurnSubtitle(subtitleFilename, {}); return; }
    document.getElementById('burnStyleFileName').textContent = subtitleFilename;
    modal.dataset.filename = subtitleFilename;
    applyBurnPreset('default');
    // 实时预览：绑定控件事件并按默认预设先画一帧
    bindBurnPreview();
    updateBurnPreview();
    modal.style.display = 'flex';
}

function closeBurnStyleModal() {
    const modal = document.getElementById('burnStyleModal');
    if (modal) modal.style.display = 'none';
}

function applyBurnPreset(name) {
    const presets = {
        default: { font_size: 24, font_color: '#FFFFFF', outline_color: '#000000', outline_width: 2, position: 'bottom', margin_v: 28, bold: false },
        big:     { font_size: 40, font_color: '#FFFFFF', outline_color: '#000000', outline_width: 3, position: 'bottom', margin_v: 40, bold: false },
        strong:  { font_size: 28, font_color: '#FFFFFF', outline_color: '#000000', outline_width: 4, position: 'bottom', margin_v: 28, bold: true },
        top:     { font_size: 26, font_color: '#FFEB3B', outline_color: '#000000', outline_width: 2, position: 'top',    margin_v: 40, bold: false },
    };
    const p = presets[name] || presets.default;
    document.getElementById('burnFontSize').value = p.font_size;
    document.getElementById('burnFontColor').value = p.font_color;
    document.getElementById('burnOutlineColor').value = p.outline_color;
    document.getElementById('burnOutlineWidth').value = p.outline_width;
    document.getElementById('burnPosition').value = p.position;
    document.getElementById('burnMarginV').value = p.margin_v;
    document.getElementById('burnBold').checked = p.bold;
}

function collectBurnStyle() {
    return {
        font_size: parseInt(document.getElementById('burnFontSize').value, 10) || 24,
        font_color: document.getElementById('burnFontColor').value || '#FFFFFF',
        outline_color: document.getElementById('burnOutlineColor').value || '#000000',
        outline_width: parseInt(document.getElementById('burnOutlineWidth').value, 10) || 2,
        position: document.getElementById('burnPosition').value || 'bottom',
        margin_v: parseInt(document.getElementById('burnMarginV').value, 10) || 28,
        bold: document.getElementById('burnBold').checked,
    };
}

// ==== 烧录样式实时预览（2026-10-04）====
// ASS 尺寸单位以 PlayResY=720 为基准；这里按 1080p 等比换算到预览框高度，
// 保证「字号 40 会不会撑满画面」这类判断是可信的。
var BURN_PREVIEW_H = 288;                 // 预览框渲染高度（px）
var BURN_ASPECT_REF_H = 1080;             // 参考视频高度
var BURN_ASPECT_REF_RES = 720;            // ASS PlayResY
function burnScale() { return BURN_PREVIEW_H / BURN_ASPECT_REF_H; }

function updateBurnPreview() {
    var canvas = document.getElementById('burnPreview');
    var text = document.getElementById('burnPreviewText');
    if (!canvas || !text) return;

    var g = function(id) { var el = document.getElementById(id); return el ? el.value : ''; };
    var size = parseInt(g('burnFontSize'), 10); if (isNaN(size)) size = 24;
    var outlineW = parseInt(g('burnOutlineWidth'), 10); if (isNaN(outlineW)) outlineW = 2;
    var marginV = parseInt(g('burnMarginV'), 10); if (isNaN(marginV)) marginV = 28;
    var pos = g('burnPosition') || 'bottom';
    var boldEl = document.getElementById('burnBold');
    var bold = boldEl && boldEl.checked;
    var fontColor = g('burnFontColor') || '#FFFFFF';
    var outlineColor = g('burnOutlineColor') || '#000000';

    var k = burnScale();
    // ASS 单位 -> 参考视频像素 -> 预览框像素
    var fontPx = size * (BURN_ASPECT_REF_H / BURN_ASPECT_REF_RES) * k;
    var marginPx = marginV * (BURN_ASPECT_REF_H / BURN_ASPECT_REF_RES) * k;
    var strokePx = outlineW * (BURN_ASPECT_REF_H / BURN_ASPECT_REF_RES) * k;

    text.style.fontSize = fontPx.toFixed(1) + 'px';
    text.style.color = fontColor;
    text.style.fontWeight = bold ? '700' : '400';
    text.style.marginBottom = (pos === 'bottom' ? marginPx : 0) + 'px';
    text.style.marginTop = (pos === 'top' ? marginPx : 0) + 'px';
    if (strokePx > 0.2) {
        text.style.webkitTextStrokeWidth = strokePx.toFixed(2) + 'px';
        text.style.webkitTextStrokeColor = outlineColor;
    } else {
        text.style.webkitTextStrokeWidth = '0';
    }

    // 位置：bottom 贴底 / top 贴顶 / middle 居中
    canvas.style.alignItems = pos === 'top' ? 'flex-start'
                           : (pos === 'middle' ? 'center' : 'flex-end');

    // 溢出提示：文字高度超过画面 80% 就提醒（libass 阈值取经验值，仅作提示）
    var overflow = text.scrollHeight > canvas.clientHeight * 0.8;
    text.style.color = overflow ? '#ff9f9f' : fontColor;
}

function bindBurnPreview() {
    var ids = ['burnFontSize', 'burnFontColor', 'burnOutlineColor',
               'burnOutlineWidth', 'burnPosition', 'burnMarginV', 'burnBold'];
    ids.forEach(function(id) {
        var el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('input', updateBurnPreview);
        el.addEventListener('change', updateBurnPreview);
    });
    // 预设按钮改完值后刷新
    ['default', 'big', 'strong', 'top'].forEach(function(name) {
        var btn = document.querySelector("button[onclick=\"applyBurnPreset('" + name + "')\"]");
        if (btn) btn.addEventListener('click', function() { setTimeout(updateBurnPreview, 0); });
    });
}

function confirmBurnSubtitle() {
    const modal = document.getElementById('burnStyleModal');
    const filename = modal ? modal.dataset.filename : null;
    if (!filename) return;
    const style = collectBurnStyle();
    closeBurnStyleModal();
    doBurnSubtitle(filename, style);
}

async function doBurnSubtitle(subtitleFilename, style) {
    try {
        var resp = await apiFetch('/api/burn-subtitle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ subtitle_filename: subtitleFilename, style: style || {} })
        });
        var result = await resp.json();
        if (result.success) {
            burnTaskId = result.task_id;
            showStatusSection('processing');
            document.getElementById('statusText').textContent = '正在烧录字幕...';
            document.getElementById('progressContainer').style.display = 'block';
            scheduleBurnPoll();
            showToast('烧录任务已启动', 'info');
        } else {
            showToast('烧录失败: ' + result.error, 'error');
        }
    } catch (e) {
        showToast('烧录失败: ' + e.message, 'error');
    }
}

function toggleMoreActions(btn) {
    var row = btn.closest('.subtitle-actions');
    if (row) row.classList.toggle('open');
}

// ==== 软字幕封装（2026-10-26）：字幕作为软轨封装进 MKV，不重编码、播放器可开关 ====
var muxTaskId = null;
var muxStatusInterval = null;

function muxSubtitle(subtitleFilename) {
    uiConfirmAsync('软字幕封装', '将字幕作为软字幕轨封装进 MKV 容器？视频不重新编码（秒级完成），播放器里可随时开关字幕。', {
        okText: '封装'
    }).then(function(ok) {
        if (!ok) return;
        doMuxSubtitle(subtitleFilename);
    });
}

async function doMuxSubtitle(subtitleFilename) {
    try {
        var resp = await apiFetch('/api/subtitle-mux', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ subtitle_filename: subtitleFilename })
        });
        var result = await resp.json();
        if (result.success) {
            muxTaskId = result.task_id;
            showStatusSection('processing');
            document.getElementById('statusText').textContent = '正在封装软字幕...';
            document.getElementById('progressContainer').style.display = 'block';
            scheduleMuxPoll();
            showToast('软字幕封装已启动', 'info');
        } else {
            showToast('封装失败: ' + result.error, 'error');
        }
    } catch (e) {
        showToast('封装失败: ' + e.message, 'error');
    }
}

function scheduleMuxPoll() {
    muxStatusInterval = setTimeout(checkMuxStatus, 2000);
}

async function checkMuxStatus() {
    if (!muxTaskId) return;
    try {
        var resp = await apiFetch('/api/status/' + muxTaskId);
        var result = await resp.json();
        if (result.success && result.task) {
            var task = result.task;
            updateProgress(task.progress || 0, task.progress_info || '');
            if (task.status === 'completed') {
                muxStatusInterval = null;
                showStatusSection('completed');
                document.getElementById('statusText').textContent = '封装完成！';
                var details = '输出文件: <strong>' + escapeHtml(task.result.output_file) + '</strong><br>' +
                    '文件大小: <strong>' + formatFileSize(task.result.size) + '</strong>';
                document.getElementById('statusDetails').innerHTML = details;
                document.getElementById('generateBtn').disabled = false;
                document.getElementById('cancelBtn').style.display = 'none';
                loadSubtitles();
                showToast('软字幕封装完成！', 'success');
                muxTaskId = null;
            } else if (task.status === 'failed') {
                muxStatusInterval = null;
                showStatusSection('failed');
                document.getElementById('statusDetails').textContent = task.error || '封装失败';
                document.getElementById('generateBtn').disabled = false;
                document.getElementById('cancelBtn').style.display = 'none';
                showToast('封装失败', 'error');
                muxTaskId = null;
            }
        }
    } catch (e) { /* 网络抖动下一轮重试 */ }
}

function scheduleBurnPoll() {
    burnStatusInterval = setTimeout(checkBurnStatus, 2000);
}

async function checkBurnStatus() {
    if (!burnTaskId) return;
    try {
        var resp = await apiFetch('/api/status/' + burnTaskId);
        var result = await resp.json();
        if (result.success && result.task) {
            var task = result.task;
            updateProgress(task.progress || 0, task.progress_info || '');
            if (task.status === 'completed') {
                burnStatusInterval = null;
                showStatusSection('completed');
                document.getElementById('statusText').textContent = '烧录完成！';
                var details = '输出文件: <strong>' + escapeHtml(task.result.output_file) + '</strong><br>' +
                    '文件大小: <strong>' + formatFileSize(task.result.size) + '</strong>';
                document.getElementById('statusDetails').innerHTML = details;
                document.getElementById('generateBtn').disabled = false;
                document.getElementById('cancelBtn').style.display = 'none';
                loadSubtitles();
                showToast('字幕烧录完成！', 'success');
                burnTaskId = null;
            } else if (task.status === 'failed') {
                burnStatusInterval = null;
                showStatusSection('failed');
                document.getElementById('statusDetails').textContent = task.error || '烧录失败';
                document.getElementById('generateBtn').disabled = false;
                document.getElementById('cancelBtn').style.display = 'none';
                showToast('烧录失败', 'error');
                burnTaskId = null;
            }
        }
    } catch (e) { console.error('Burn status error:', e); }
}

// ===== DELETE SUBTITLE =====
async function deleteSubtitle(filename) {
    showConfirm('删除确认', '确定要删除 ' + filename + ' 吗？此操作不可撤销。', async function() {

        try {
            var response = await apiFetch('/api/delete/' + encodeURIComponent(filename), {
                method: 'DELETE'
            });
            var result = await response.json();
            if (result.success) {
                showToast('删除成功', 'success');
                loadSubtitles();
            } else {
                showToast('删除失败: ' + result.error, 'error');
            }
        } catch (error) {
            showToast('删除失败: ' + error.message, 'error');
        }
    }, '删除', true);
}


// ===== TIME OFFSET =====
function applyTimeOffsetToAll() {
    var offset = parseFloat(document.getElementById('timeOffsetValue').value) || 0;
    if (offset === 0) {
        showToast('请输入偏移秒数（正数延后，负数提前）', 'warning');
        return;
    }
    if (allSubtitles.length === 0) {
        showToast('没有字幕文件可处理', 'error');
        return;
    }
    showConfirm('时间偏移', '将把所有字幕的时间轴偏移 ' + (offset > 0 ? '+' : '') + offset + ' 秒，确定？', function() {
        doApplyOffset(offset);
    }, '确定');
}

function doApplyOffset(offset) {
    var processed = 0;
    var total = allSubtitles.length;
    
    function processNext(index) {
        if (index >= total) {
            showToast('已完成 ' + processed + ' 个字幕文件的偏移', 'success');
            loadSubtitles();
            return;
        }
        var sub = allSubtitles[index];
        apiFetch('/api/preview/' + encodeURIComponent(sub.filename))
            .then(function(r) { return r.json(); })
            .then(function(result) {
                if (result.success && result.segments) {
                    var segments = result.segments.map(function(seg) {
                        return {
                            start: Math.max(0, seg.start + offset),
                            end: Math.max(0.1, seg.end + offset),
                            text: seg.text
                        };
                    });
                    var fmt = sub.filename.split('.').pop().toLowerCase();
                    if (fmt === 'json') fmt = 'json';
                    if (fmt === 'txt') fmt = 'srt';
                    return apiFetch('/api/save-subtitle', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ filename: sub.filename, format: fmt, segments: segments })
                    });
                }
            })
            .then(function() { processed++; })
            .catch(function(err) { console.error('Offset error:', err); })
            .finally(function() { processNext(index + 1); });
    }
    processNext(0);
}

// ===== KEYBOARD SHORTCUTS =====
document.addEventListener('keydown', function(e) {
    var modal = document.getElementById('subtitleEditorModal');
    if (!modal || modal.style.display !== 'flex') return;
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
        if (e.key === 'Escape') { e.target.blur(); return; }
        if (e.key === 's' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            saveSubtitleChanges();
        }
        return;
    }
    
    switch (e.key) {
        case 'Escape':
            closeSubtitleEditor();
            break;
        case ' ':
            e.preventDefault();
            var video = document.getElementById('editorVideo');
            if (video.style.display !== 'none') {
                video.paused ? video.play() : video.pause();
            } else {
                var embeddedAudio = document.querySelector('#videoPlaceholder audio');
                if (embeddedAudio) {
                    embeddedAudio.paused ? embeddedAudio.play() : embeddedAudio.pause();
                }
            }
            break;
        case 'ArrowUp':
            e.preventDefault();
            if (editorActiveIndex > 0) seekToSegment(editorActiveIndex - 1);
            break;
        case 'ArrowDown':
            e.preventDefault();
            if (editorActiveIndex < editedSegments.length - 1) seekToSegment(editorActiveIndex + 1);
            break;
        case 's':
            if (e.ctrlKey || e.metaKey) {
                e.preventDefault();
                saveSubtitleChanges();
            }
            break;
    }
});

// ===== INITIALIZATION =====
document.addEventListener('DOMContentLoaded', function() {
    initTheme();
    initUploadArea();
    initBatchUploadArea();
    // 页面加载时重置状态（任务状态仅会话有效，刷新后失效）
    if (statusSection) statusSection.style.display = 'none';
    currentTaskId = null;
    if (generateBtn) {
        generateBtn.disabled = false;
        generateBtn.innerHTML = '<span class="btn-icon">⚡</span> 生成字幕';
    }
    if (cancelBtn) cancelBtn.style.display = 'none';
    loadSubtitles();
    verifyAuthOnStart();

    modelSelect.addEventListener('change', function(e) {
        currentModel = e.target.value;
    });

    // 搜索框事件
    var searchInput = document.getElementById('subtitleSearch');
    if (searchInput) {
        searchInput.addEventListener('input', function() {
            subtitleSearchTerm = this.value;
            filterAndRenderSubtitles();
        });
    }

    // 排序下拉事件
    var sortSelect = document.getElementById('subtitleSort');
    if (sortSelect) {
        sortSelect.addEventListener('change', function() {
            var v = this.value;
            if (v === 'name_asc') { subtitleSortBy = 'name'; subtitleSortOrder = 'asc'; }
            else if (v === 'name_desc') { subtitleSortBy = 'name'; subtitleSortOrder = 'desc'; }
            else if (v === 'size_asc') { subtitleSortBy = 'size'; subtitleSortOrder = 'asc'; }
            else if (v === 'size_desc') { subtitleSortBy = 'size'; subtitleSortOrder = 'desc'; }
            else if (v === 'created_desc') { subtitleSortBy = 'created'; subtitleSortOrder = 'desc'; }
            else { subtitleSortBy = 'created'; subtitleSortOrder = 'asc'; }
            filterAndRenderSubtitles();
        });
    }
});

// 初始化批量上传区域
function initBatchUploadArea() {
    // 点击批量上传
    batchUploadArea.addEventListener('click', () => {
        batchFileInput.click();
    });

    // 文件选择
    batchFileInput.addEventListener('change', handleBatchFileSelect);

    // 拖拽上传
    batchUploadArea.addEventListener('dragover', (e) => { e.preventDefault(); batchUploadArea.classList.add('dragover'); });

    batchUploadArea.addEventListener('dragleave', (e) => { e.preventDefault(); batchUploadArea.classList.remove('dragover'); });

    batchUploadArea.addEventListener('drop', (e) => { e.preventDefault(); batchUploadArea.classList.remove('dragover');
        const files = Array.from(e.dataTransfer.files);
        handleBatchFiles(files);
    });
}

// 处理批量文件选择
function handleBatchFileSelect(e) {
    const files = Array.from(e.target.files);
    handleBatchFiles(files);
}

// 处理批量文件
function handleBatchFiles(files) {
    const allowedExts = ['mp4', 'avi', 'mkv', 'mov', 'wmv', 'flv', 'webm', 'm4v', 'mpg', 'mpeg', 'mp3', 'wav', 'flac', 'aac', 'ogg', 'm4a'];
    
    // 限制文件数量
    if (files.length > 20) {
        showToast('最多只能选择20个文件', 'error');
        return;
    }
    
    // 过滤有效文件
    const validFiles = files.filter(file => {
        const fileExt = file.name.split('.').pop().toLowerCase();
        return allowedExts.includes(fileExt);
    });
    
    if (validFiles.length === 0) {
        showToast('没有有效的视频文件', 'error');
        return;
    }
    
    if (validFiles.length < files.length) {
        showToast(`已过滤 ${files.length - validFiles.length} 个无效文件`, 'warning');
    }
    
    // 添加到批量文件列表
    batchFiles = [...batchFiles, ...validFiles];
    renderBatchFileList();
    batchFileList.style.display = 'block';
    
    showToast(`已添加 ${validFiles.length} 个文件到批量列表`, 'success');
}

// 渲染批量文件列表
function renderBatchFileList() {
    batchFileItems.innerHTML = '';
    
    batchFiles.forEach((file, index) => {
        const item = document.createElement('div');
        item.className = 'batch-file-item';
        item.innerHTML = `
            <span class="file-info">
                <span class="file-name">${escapeHtml(file.name)}</span>
                <span class="file-size">${formatFileSize(file.size)}</span>
            </span>
            <button class="btn btn-small btn-danger" onclick="removeBatchFile(${index})">删除</button>
        `;
        batchFileItems.appendChild(item);
    });
}

// 删除批量文件
function removeBatchFile(index) {
    batchFiles.splice(index, 1);
    renderBatchFileList();
    
    if (batchFiles.length === 0) {
        batchFileList.style.display = 'none';
    }
}

// 清空批量文件列表
function clearBatchFiles() {
    batchFiles = [];
    batchFileList.style.display = 'none';
    batchFileInput.value = '';
}

// 批量上传文件 (XHR with real progress)
function uploadBatchFiles() {
    if (batchFiles.length === 0) {
        showToast('没有文件可上传', 'error');
        return;
    }
    
    const formData = new FormData();
    batchFiles.forEach(function(file) {
        formData.append('videos', file);
    });
    
    const xhr = new XMLHttpRequest();
    
    batchUploadProgress.style.display = 'block';
    batchProgressFill.style.width = '0%';
    batchProgressText.textContent = '准备上传...';
    
    xhr.upload.addEventListener('progress', function(e) {
        if (e.lengthComputable) {
            var pct = Math.round((e.loaded / e.total) * 100);
            batchProgressFill.style.width = pct + '%';
            batchProgressText.textContent = '批量上传中 ' + pct + '%';
        }
    });
    
    xhr.addEventListener('load', function() {
        if (xhr.status >= 200 && xhr.status < 300) {
            var result = JSON.parse(xhr.responseText);
            if (result.success) {
                batchProgressFill.style.width = '100%';
                batchProgressText.textContent = '上传完成！成功 ' + result.total_uploaded + ' 个，失败 ' + result.total_failed + ' 个';
                clearBatchFiles();
                batchUploadProgress.style.display = 'none';
                uploadedFile = { type: 'batch', files: result.uploaded_files };
                generateBtn.disabled = false;
                showToast('批量上传成功！', 'success');
                if (result.failed_files && result.failed_files.length > 0) {
                    var failedNames = result.failed_files.map(function(f) { return f.filename; }).join(', ');
                    showToast('以下文件上传失败: ' + failedNames, 'warning');
                }
            } else {
                batchUploadProgress.style.display = 'none';
                showToast('批量上传失败: ' + (result.error || '未知错误'), 'error');
            }
        } else {
            batchUploadProgress.style.display = 'none';
            showToast('批量上传失败: 服务器错误 ' + xhr.status, 'error');
        }
    });
    
    xhr.addEventListener('error', function() {
        batchUploadProgress.style.display = 'none';
        showToast('批量上传失败: 网络错误', 'error');
    });
    
    xhr.open('POST', '/api/upload-batch');
    if (AUTH_TOKEN) xhr.setRequestHeader('Authorization', 'Bearer ' + AUTH_TOKEN);
    xhr.send(formData);
}

// 批量生成字幕
async function generateBatchSubtitle() {
    if (!uploadedFile || uploadedFile.type !== 'batch') {
        showToast('请先批量上传视频文件', 'error');
        return;
    }

    const options = getGenerationOptions();

    generateBtn.disabled = true;
    generateBtn.innerHTML = '<span class="loading"></span> 批量处理中...';
    cancelBtn.style.display = 'inline-flex';
    
    try {
        const response = await apiFetch('/api/generate-batch', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                files: uploadedFile.files.map(f => ({
                    filename: f.saved_name,
                    path: f.filepath,
                    is_nas_file: false
                })),
                ...options
            })
        });
        
        const result = await response.json();
        
        if (result.success) {
            currentBatchId = result.batch_id;
            showBatchStatusSection();
            startBatchStatusCheck();
            showToast(`批量字幕生成任务已启动，共 ${result.total_tasks} 个文件`, 'info');
        } else {
            throw new Error(result.error);
        }
    } catch (error) {
        generateBtn.disabled = false;
        generateBtn.innerHTML = '<span class="btn-icon">⚡</span> 生成字幕';
        cancelBtn.style.display = 'none';
        showToast('批量生成失败: ' + error.message, 'error');
    }
}

// 显示批量状态区域
function showBatchStatusSection() {
    batchStatusSection.style.display = 'block';
}

// 开始批量状态检查
var batchPollFailures = 0;
function startBatchStatusCheck() {
    if (batchStatusInterval) {
        clearTimeout(batchStatusInterval);
    }
    batchPollFailures = 0;
    scheduleNextBatchPoll();
}

function scheduleNextBatchPoll() {
    var baseDelay = 3000;
    var maxDelay = 30000;
    var delay = Math.min(baseDelay * Math.pow(1.5, batchPollFailures), maxDelay);
    batchStatusInterval = setTimeout(checkBatchStatus, delay);
}

// 检查批量状态
async function checkBatchStatus() {
    if (!currentBatchId) return;
    
    try {
        const response = await apiFetch(`/api/batch-status/${currentBatchId}`);
        const result = await response.json();
        
        if (!result.success) throw new Error(result.error || "批量状态暂不可用");
        if (result.success) {
            batchPollFailures = 0;
            updateBatchStatusDisplay(result);
            
            if (result.overall_status === 'completed' || result.overall_status === 'failed' || result.overall_status === 'partial_completed') {
                clearTimeout(batchStatusInterval);
                batchStatusInterval = null;
                currentBatchId = null;
                batchPollFailures = 0;
                
                generateBtn.disabled = false;
                generateBtn.innerHTML = '<span class="btn-icon">⚡</span> 生成字幕';
                cancelBtn.style.display = 'none';
                
                loadSubtitles();
                showToast(result.overall_status === 'completed' ? '批量任务全部完成！' : '批量任务已结束，请检查失败项目', result.overall_status === 'completed' ? 'success' : 'warning');
            }
        }
    } catch (error) {
        console.error('检查批量状态失败:', error);
        batchPollFailures++;
    } finally {
        if (currentBatchId) scheduleNextBatchPoll();
    }
}

// 更新批量状态显示
function updateBatchStatusDisplay(data) {
    // 更新总体进度
    document.getElementById('batchProgressPercent').textContent = data.avg_progress + '%';
    document.getElementById('batchProgressFill').style.width = data.avg_progress + '%';
    
    // 更新统计数据
    document.getElementById('batchStatProcessing').textContent = data.status_counts.processing || 0;
    document.getElementById('batchStatCompleted').textContent = data.status_counts.completed || 0;
    document.getElementById('batchStatFailed').textContent = data.status_counts.failed || 0;
    
    // 更新任务列表
    const tasksHtml = Object.entries(data.tasks).map(([taskId, task]) => `
        <div class="batch-task-item ${escapeHtml(task.status)}">
            <div class="task-name">${escapeHtml(task.filename)}</div>
            <div class="task-status">
                <span class="status-indicator ${escapeHtml(task.status)}">${escapeHtml(getStatusText(task.status))}</span>
                <span class="task-progress">${task.progress || 0}%</span>
            </div>
        </div>
    `).join('');
    
    batchTasksList.innerHTML = tasksHtml;
}

// 获取状态文本
function getStatusText(status) {
    const statusMap = {
        'processing': '处理中',
        'completed': '已完成',
        'failed': '失败',
        'cancelled': '已取消'
    };
    return statusMap[status] || status;
}

// 字幕编辑器相关变量
let currentEditingSubtitle = null;
let originalSegments = [];
let editedSegments = [];

// 预览字幕
async function previewSubtitle(filename) {
    try {
        const response = await apiFetch('/api/preview/' + encodeURIComponent(filename));
        const result = await response.json();
        
        if (result.success) {
            openSubtitleEditor(filename, result.format, result.segments, true);
        } else {
            showToast('预览失败: ' + result.error, 'error');
        }
    } catch (error) {
        showToast('预览失败: ' + error.message, 'error');
    }
}

// 编辑字幕
async function editSubtitle(filename) {
    try {
        const response = await apiFetch('/api/preview/' + encodeURIComponent(filename));
        const result = await response.json();
        
        if (result.success) {
            openSubtitleEditor(filename, result.format, result.segments, false);
        } else {
            showToast('加载字幕失败: ' + result.error, 'error');
        }
    } catch (error) {
        showToast('加载字幕失败: ' + error.message, 'error');
    }
}

// 打开字幕编辑器
let editorActiveIndex = -1;
let editorVideoFile = null;
let editorVideoOffset = 0;
let editorVideoLoader = null;
let editorVideoStopUrl = null;
let editorVideoLoadToken = 0;
let editorHls = null;

function openSubtitleEditor(filename, format, segments, readOnly) {
    if (readOnly === undefined) readOnly = false;
    currentEditingSubtitle = filename;
    originalSegments = JSON.parse(JSON.stringify(segments));
    editedSegments = JSON.parse(JSON.stringify(segments));
    editorActiveIndex = -1;
    
    document.getElementById('editorFilename').textContent = filename;
    document.getElementById('editorFormat').textContent = format.toUpperCase();
    document.getElementById('editorTitle').textContent = (readOnly ? '预览: ' : '编辑: ') + filename;
    
    // Try to find matching video
    findAndLoadVideo(filename);
    
    renderSegments(readOnly);
    updateEditorStats();
    loadEditorWords(filename);
    
    document.getElementById('subtitleEditorModal').style.display = 'flex';
    document.body.style.overflow = 'hidden';
    
    // Focus for keyboard shortcuts
    setTimeout(function() {
        document.getElementById('subtitleEditorModal').focus();
    }, 100);
}


function findAndLoadVideo(subFilename) {
    var video = document.getElementById('editorVideo');
    var placeholder = document.getElementById('videoPlaceholder');
    var audioEl = document.getElementById('editorAudio');
    var extractBtn = document.getElementById('extractAudioBtn');
    var baseName = subFilename.replace(/\.(srt|vtt|ass|json|txt)$/i, '');

    // Reset
    video.style.display = 'none';
    video.removeAttribute('src');
    video.muted = false;
    video.removeAttribute('muted');
    audioEl.style.display = 'none';
    audioEl.removeAttribute('src');
    if (extractBtn) extractBtn.style.display = 'none';
    placeholder.innerHTML = '<div class="placeholder-icon">🎬</div><p style="color:#888;font-size:0.9rem">正在查找视频...</p>';
    placeholder.style.display = 'flex';
    placeholder.style.flexDirection = 'column';
    placeholder.style.alignItems = 'center';
    placeholder.style.justifyContent = 'center';
    editorVideoFile = null;
    editorVideoOffset = 0;
    editorVideoLoader = null;
    stopActivePreviewStream();
    editorVideoLoadToken++;

    var extensions = ['.mp4', '.webm', '.mov', '.mkv', '.avi'];

    function loadHlsPreview(encoded, baseName, originalTime, autoPlay) {
        originalTime = Math.max(0, Number(originalTime) || 0);
        var loadToken = ++editorVideoLoadToken;
        stopActivePreviewStream();
        if (editorHls) {
            editorHls.destroy();
            editorHls = null;
        }
        editorVideoOffset = originalTime;
        editorVideoStopUrl = '/api/stop-preview/' + encoded;

        placeholder.innerHTML = '<div style="width:100%;padding:20px;text-align:center">' +
            '<div class="spinner" style="margin:0 auto 12px;width:32px;height:32px;border:3px solid rgba(255,255,255,0.2);border-top-color:#fff;border-radius:50%;animation:spin 0.8s linear infinite"></div>' +
            '<p style="color:#aaa;font-size:0.85rem">正在启动边转边播预览...</p>' +
            '</div>';
        placeholder.style.display = 'flex';
        video.style.display = 'none';
        video.removeAttribute('src');
        video.muted = false;
        video.removeAttribute('muted');

        apiFetch('/api/hls-start/' + encoded + '?start=' + encodeURIComponent(originalTime.toFixed(3)))
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (loadToken !== editorVideoLoadToken) return;
                if (!data.success || !data.playlist_url) {
                    throw new Error(data.error || 'HLS启动失败');
                }
                waitForHlsPlaylist(withAuthQuery(data.playlist_url), 0);
            })
            .catch(function(err) {
                if (loadToken !== editorVideoLoadToken) return;
                console.warn('HLS preview failed, falling back to MP4 transcode:', err);
                loadTranscodedVideo(encoded, baseName, originalTime, autoPlay, 'h264');
            });

        function waitForHlsPlaylist(playlistUrl, attempt) {
            if (loadToken !== editorVideoLoadToken) return;
            apiFetch(playlistUrl, { cache: 'no-store' }).then(function(r) {
                if (!r.ok) throw new Error('playlist not ready');
                return r.text();
            }).then(function(text) {
                if (loadToken !== editorVideoLoadToken) return;
                var hasSegment = text.indexOf('#EXTINF') !== -1 ||
                    text.indexOf('.ts') !== -1 ||
                    text.indexOf('.m4s') !== -1;
                if (!hasSegment && attempt < 120) {
                    setTimeout(function() { waitForHlsPlaylist(playlistUrl, attempt + 1); }, 500);
                    return;
                }
                attachHls(playlistUrl);
            }).catch(function() {
                if (attempt < 120) {
                    setTimeout(function() { waitForHlsPlaylist(playlistUrl, attempt + 1); }, 500);
                } else if (loadToken === editorVideoLoadToken) {
                    loadTranscodedVideo(encoded, baseName, originalTime, autoPlay, 'h264');
                }
            });
        }

        function attachHls(playlistUrl) {
            if (loadToken !== editorVideoLoadToken) return;
            setupVideoSync();
            video.onerror = function() {
                if (loadToken !== editorVideoLoadToken) return;
                loadTranscodedVideo(encoded, baseName, originalTime, autoPlay, 'h264');
            };
            if (window.Hls && window.Hls.isSupported()) {
                editorHls = new Hls({
                    lowLatencyMode: false,
                    backBufferLength: 90,           // 30 → 90s，容服务端 4K HEVC 转码抖动
                    maxBufferLength: 60,            // 前向缓冲 60s
                    maxMaxBufferLength: 120,        // 动态最大缓冲
                    maxBufferSize: 60 * 1000 * 1000, // 60MB 缓冲上限
                    liveSyncDuration: 0,            // 点播不需要直播同步
                    manifestLoadingMaxRetry: 10,
                    levelLoadingMaxRetry: 10,
                    fragLoadingMaxRetry: 10,
                    xhrSetup: function(xhr) {
                        if (AUTH_TOKEN) xhr.setRequestHeader('Authorization', 'Bearer ' + AUTH_TOKEN);
                    },
                });
                var fatalHlsErrors = 0;
                editorHls.loadSource(playlistUrl);
                editorHls.attachMedia(video);
                editorHls.on(Hls.Events.MANIFEST_PARSED, function() {
                    if (loadToken !== editorVideoLoadToken) return;
                    placeholder.style.display = 'none';
                    video.style.display = 'block';
                    if (autoPlay) video.play().catch(function() {});
                });
                editorHls.on(Hls.Events.ERROR, function(event, data) {
                    if (data && data.fatal && loadToken === editorVideoLoadToken) {
                        fatalHlsErrors++;
                        console.warn('HLS fatal error:', data.type, data.details, data.reason || '', data.error || '');
                        if (data.type === Hls.ErrorTypes.NETWORK_ERROR && fatalHlsErrors < 5) {
                            editorHls.startLoad();
                        } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR && fatalHlsErrors < 5) {
                            editorHls.recoverMediaError();
                        } else {
                            editorHls.destroy();
                            editorHls = null;
                            loadTranscodedVideo(encoded, baseName, originalTime, autoPlay, 'h264');
                        }
                    }
                });
            } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
                video.src = withAuthQuery(playlistUrl);
                video.onloadedmetadata = function() {
                    if (loadToken !== editorVideoLoadToken) return;
                    placeholder.style.display = 'none';
                    video.style.display = 'block';
                    if (autoPlay) video.play().catch(function() {});
                };
            } else {
                loadTranscodedVideo(encoded, baseName, originalTime, autoPlay, 'h264');
            }
        }
    }

    // Load video via background transcode with polling (202 until done, then 200)
    function loadTranscodedVideo(encoded, baseName, originalTime, autoPlay, mode) {
        originalTime = Math.max(0, Number(originalTime) || 0);
        mode = mode || 'remux';
        var clipStart = 0;
        var targetLocalTime = originalTime;
        var loadToken = ++editorVideoLoadToken;
        var transcodeUrl = '/api/transcode-video/' + encoded +
            '?start=' + encodeURIComponent(clipStart.toFixed(3)) +
            '&duration=0&mode=' + encodeURIComponent(mode);
        stopActivePreviewStream();
        editorVideoOffset = clipStart;
        editorVideoStopUrl = null;

        // Show progress placeholder while waiting
        placeholder.innerHTML = '<div style="width:100%;padding:20px;text-align:center">' +
            '<div class="spinner" style="margin:0 auto 12px;width:32px;height:32px;border:3px solid rgba(255,255,255,0.2);border-top-color:#fff;border-radius:50%;animation:spin 0.8s linear infinite"></div>' +
            '<p style="color:#aaa;font-size:0.85rem">' + (mode === 'remux' ? '正在快速准备整部影片预览...' : '正在兼容转码整部影片预览...') + '</p>' +
            '</div>';
        placeholder.style.display = 'flex';
        placeholder.style.flexDirection = 'column';
        placeholder.style.alignItems = 'center';
        placeholder.style.justifyContent = 'center';

        function fallbackToAudio() {
            if (loadToken !== editorVideoLoadToken) return;
            if (mode === 'remux') {
                showToast('快速预览不受当前浏览器支持，切换到兼容转码', 'warning');
                loadTranscodedVideo(encoded, baseName, originalTime, autoPlay, 'h264');
                return;
            }
            video.style.display = 'none';
            video.removeAttribute('src');
            video.onerror = null;
            placeholder.innerHTML = '<div style="width:100%;padding:20px;text-align:center">' +
                '<div style="font-size:2rem;margin-bottom:8px;opacity:0.5">♫</div>' +
                '<p style="color:#aaa;font-size:0.85rem;margin-bottom:12px">视频转码失败，音频预览模式</p>' +
                '</div>';
            placeholder.style.display = 'flex';
            placeholder.style.flexDirection = 'column';
            placeholder.style.alignItems = 'center';
            audioEl.style.display = 'block';
            audioEl.style.width = '100%';
            audioEl.style.maxWidth = '420px';
            audioEl.style.margin = '0';
            var aacUrl = withAuthQuery('/api/audio/' + encodeURIComponent(baseName + '.aac'));
            audioEl.src = aacUrl;
            audioEl.play().catch(function() {});
            setupAudioVideoSync(video, audioEl);
        }

        function startPolling(pollUrl) {
            var startTime = Date.now();
            var poll = setInterval(function() {
                if (loadToken !== editorVideoLoadToken) {
                    clearInterval(poll);
                    return;
                }
                apiFetch(pollUrl).then(function(r) { return r.json(); }).then(function(data) {
                    if (loadToken !== editorVideoLoadToken) {
                        clearInterval(poll);
                        return;
                    }
                    var elapsed = Math.floor((Date.now() - startTime) / 1000);
                    if (data.status === 'done') {
                        clearInterval(poll);
                        // Transcode complete — load the video
                        video.muted = false;
                        video.removeAttribute('muted');
                        video.src = withAuthQuery(transcodeUrl);
                        video.style.display = 'block';
                        placeholder.style.display = 'none';
                        setupVideoSync();
                        video.onerror = fallbackToAudio;
                        video.onloadedmetadata = function() {
                            if (loadToken !== editorVideoLoadToken) return;
                            video.currentTime = Math.min(targetLocalTime, Math.max(0, video.duration - 0.5));
                            if (autoPlay) video.play().catch(function() {});
                        };
                    } else if (data.status === 'error') {
                        clearInterval(poll);
                        fallbackToAudio();
                    } else {
                        // Still running — update progress
                        var sizeMB = data.size ? (data.size / 1048576).toFixed(1) : '0.0';
                        placeholder.innerHTML = '<div style="width:100%;padding:20px;text-align:center">' +
                            '<div class="spinner" style="margin:0 auto 12px;width:32px;height:32px;border:3px solid rgba(255,255,255,0.2);border-top-color:#fff;border-radius:50%;animation:spin 0.8s linear infinite"></div>' +
                            '<p style="color:#aaa;font-size:0.85rem">' + (mode === 'remux' ? '正在快速生成整部影片预览...' : '正在兼容转码整部影片预览...') + ' ' + sizeMB + 'MB (' + elapsed + '秒)</p>' +
                            '</div>';
                    }
                }).catch(function() {
                    // Poll failed — keep trying
                });
            }, 1500);
        }

        apiFetch(transcodeUrl).then(function(res) {
            if (loadToken !== editorVideoLoadToken) return;
            if (res.status === 200) {
                // Cache hit — play directly
                video.muted = false;
                video.removeAttribute('muted');
                video.src = withAuthQuery(transcodeUrl);
                video.style.display = 'block';
                placeholder.style.display = 'none';
                setupVideoSync();
                video.onerror = fallbackToAudio;
                video.onloadedmetadata = function() {
                    if (loadToken !== editorVideoLoadToken) return;
                    video.currentTime = Math.min(targetLocalTime, Math.max(0, video.duration - 0.5));
                    if (autoPlay) video.play().catch(function() {});
                };
            } else {
                // 202 or other — parse JSON for poll_url
                res.json().then(function(data) {
                    if (loadToken !== editorVideoLoadToken) return;
                    if (data.poll_url) {
                        startPolling(data.poll_url);
                    } else {
                        fallbackToAudio();
                    }
                }).catch(function() {
                    fallbackToAudio();
                });
            }
        }).catch(function() {
            fallbackToAudio();
        });
    }

    // 用完整字幕名读取已保存的原媒体映射，另存/转换后也能预览同一原视频。
    // resolvedDirEncoded 是已逐段 encodeURIComponent 的目录前缀（含结尾斜杠），空 = NAS 根目录。
    var resolvedDirEncoded = '';
    var resolvedMediaPath = '';
    var resolvedLocation = '';
    apiFetch('/api/resolve-media?name=' + encodeURIComponent(subFilename))
        .then(function(r) { return r.json(); })
        .then(function(info) {
            if (info && info.success && info.path && ['nas', 'uploads'].indexOf(info.location) !== -1) {
                resolvedMediaPath = info.path;
                resolvedLocation = info.location;
                var parts = info.path.split('/');
                parts.pop();
                if (parts.length > 0) {
                    resolvedDirEncoded = parts.map(encodeURIComponent).join('/') + '/';
                }
                var extResolved = ('.' + (info.path.split('.').pop() || '')).toLowerCase();
                if (extensions.indexOf(extResolved) !== -1) {
                    extensions = [extResolved].concat(extensions.filter(function(e) { return e !== extResolved; }));
                }
            }
        })
        .catch(function() {})
        .then(function() {
            if (resolvedLocation === 'uploads') {
                var ext = ('.' + resolvedMediaPath.split('.').pop()).toLowerCase();
                loadUploadedMedia(resolvedMediaPath, uploadAudioExts.indexOf(ext) !== -1);
            } else {
                tryFind(0);
            }
        });

    function tryFind(idx) {
        if (idx >= extensions.length) {
            // NAS 目录没有同名媒体 → 尝试上传目录（先视频后音频）
            tryUploadFind(0);
            return;
        }
        var ext = extensions[idx];
        var encoded = resolvedLocation === 'nas' && idx === 0
            ? resolvedMediaPath.split('/').map(encodeURIComponent).join('/')
            : resolvedDirEncoded + encodeURIComponent(baseName + ext);
        var headUrl = '/api/video/' + encoded;

        var xhr = new XMLHttpRequest();
        xhr.open('HEAD', headUrl);
        if (AUTH_TOKEN) xhr.setRequestHeader('Authorization', 'Bearer ' + AUTH_TOKEN);
        xhr.onload = function() {
            if (xhr.status === 200) {
                var isNative = ['.mp4', '.webm', '.mov'].indexOf(ext) !== -1;
                editorVideoFile = headUrl;

                if (isNative) {
                    editorVideoOffset = 0;
                    // Native browser formats: direct playback
                    video.src = withAuthQuery(headUrl);
                    video.style.display = 'block';
                    placeholder.style.display = 'none';
                    setupVideoSync();

                    video.onerror = function() {
                        console.warn('Video playback failed, falling back to audio');
                        video.style.display = 'none';
                        video.removeAttribute('src');
                        video.onerror = null;
                        placeholder.innerHTML = '<div style="width:100%;padding:20px;text-align:center">' +
                            '<div style="font-size:2rem;margin-bottom:8px;opacity:0.5">♫</div>' +
                            '<p style="color:#aaa;font-size:0.85rem;margin-bottom:12px">视频编解码器不支持，音频预览模式</p>' +
                            '</div>';
                        placeholder.style.display = 'flex';
                        placeholder.style.flexDirection = 'column';
                        placeholder.style.alignItems = 'center';
                        audioEl.style.display = 'block';
                        audioEl.style.width = '100%';
                        audioEl.style.maxWidth = '420px';
                        audioEl.style.margin = '0';
                        // 视频播不出来时退到"纯音频预览"；带 path= 让后端能定位子目录里的源视频
                        var audioQ = resolvedMediaPath ? '?path=' + encodeURIComponent(resolvedMediaPath) : '';
                        var aacUrl = withAuthQuery('/api/audio/' + encodeURIComponent(baseName + '.aac') + audioQ);
                        audioEl.src = aacUrl;
                        audioEl.play().catch(function() {});
                        setupAudioVideoSync(video, audioEl);
                    };
                } else {
                    // Non-native (MKV/AVI): use HLS so playback can start while ffmpeg keeps transcoding.
                    editorVideoLoader = function(originalTime, autoPlay) {
                        loadHlsPreview(encoded, baseName, originalTime, autoPlay);
                    };
                    loadHlsPreview(encoded, baseName, 0, false);
                }
            } else {
                tryFind(idx + 1);
            }
        };
        xhr.onerror = function() { tryFind(idx + 1); };
        xhr.send();
    }

    // ===== 上传目录媒体探测（/api/video 只服务 NAS 目录，上传的媒体走 /api/upload-media）=====
    var uploadVideoExts = ['.mp4', '.webm', '.mov', '.mkv', '.avi'];
    var uploadAudioExts = ['.mp3', '.wav', '.flac', '.m4a', '.aac', '.ogg'];

    function probeUpload(idx, exts, onHit, onMiss) {
        if (idx >= exts.length) {
            onMiss();
            return;
        }
        var fname = baseName + exts[idx];
        var probeUrl = '/api/upload-media/' + encodeURIComponent(fname);
        var xhr = new XMLHttpRequest();
        xhr.open('HEAD', probeUrl);
        if (AUTH_TOKEN) xhr.setRequestHeader('Authorization', 'Bearer ' + AUTH_TOKEN);
        xhr.onload = function() {
            if (xhr.status === 200) {
                onHit(fname);
            } else {
                probeUpload(idx + 1, exts, onHit, onMiss);
            }
        };
        xhr.onerror = function() { probeUpload(idx + 1, exts, onHit, onMiss); };
        xhr.send();
    }

    function tryUploadFind(idx) {
        probeUpload(idx, uploadVideoExts, function(fname) {
            loadUploadedMedia(fname, false);
        }, function() {
            tryUploadAudio(0);
        });
    }

    function tryUploadAudio(idx) {
        probeUpload(idx, uploadAudioExts, function(fname) {
            loadUploadedMedia(fname, true);
        }, function() {
            placeholder.innerHTML = '<div class="placeholder-icon">⚠️</div><p style="color:#888;font-size:0.9rem">未找到匹配的视频文件</p>';
        });
    }

    // 直接加载上传目录中的媒体（原生播放，不做转码/HLS）
    function loadUploadedMedia(fname, isAudio) {
        var loadToken = ++editorVideoLoadToken;
        stopActivePreviewStream();
        var url = withAuthQuery('/api/upload-media/' + encodeURIComponent(fname));
        editorVideoFile = '/api/upload-media/' + encodeURIComponent(fname);
        editorVideoOffset = 0;
        editorVideoStopUrl = null;
        placeholder.style.display = 'none';
        if (isAudio) {
            video.style.display = 'none';
            video.removeAttribute('src');
            audioEl.style.display = 'block';
            audioEl.style.width = '100%';
            audioEl.style.maxWidth = '420px';
            audioEl.style.margin = '0';
            audioEl.src = url;
            audioEl.play().catch(function() {});
            setupAudioVideoSync(video, audioEl);
        } else {
            video.src = url;
            video.style.display = 'block';
            setupVideoSync();
            video.onerror = function() {
                if (loadToken !== editorVideoLoadToken) return;
                video.style.display = 'none';
                placeholder.innerHTML = '<div class="placeholder-icon">⚠️</div><p style="color:#888;font-size:0.9rem">该视频格式浏览器无法直接播放</p>';
                placeholder.style.display = 'flex';
            };
        }
    }
}
function testVideoUrl(url, video, placeholder, callback) {
    var xhr = new XMLHttpRequest();
    xhr.open('HEAD', url);
    xhr.onload = function() {
        if (xhr.status === 200) {
            video.src = withAuthQuery(url);
            video.style.display = 'block';
            placeholder.style.display = 'none';
            editorVideoFile = url;
            setupVideoSync();
            if (callback) callback(true);
        } else {
            if (callback) callback(false);
        }
    };
    xhr.onerror = function() {
        if (callback) callback(false);
    };
    if (AUTH_TOKEN) xhr.setRequestHeader('Authorization', 'Bearer ' + AUTH_TOKEN);
    xhr.send();
}

function setupVideoSync() {
    var video = document.getElementById('editorVideo');
    video.ontimeupdate = function() {
        var currentTime = video.currentTime + editorVideoOffset;
        var newActive = -1;
        for (var i = 0; i < editedSegments.length; i++) {
            if (currentTime >= editedSegments[i].start && currentTime <= editedSegments[i].end) {
                newActive = i;
                break;
            }
        }
        if (newActive !== editorActiveIndex) {
            editorActiveIndex = newActive;
            highlightActiveSegment();
        }
        updateActiveWord(currentTime);
    };
}

function stopActivePreviewStream() {
    if (editorHls) {
        editorHls.destroy();
        editorHls = null;
    }
    if (!editorVideoStopUrl) return;
    var url = editorVideoStopUrl;
    editorVideoStopUrl = null;
    try {
        apiFetch(url, { method: 'POST', keepalive: true }).catch(function() {});
    } catch (e) {}
}

function setupAudioVideoSync(video, audioEl) {
    // Sync play/pause between video and external audio track
    video.onplay = function() { if (audioEl.paused) audioEl.play(); };
    video.onpause = function() { if (!audioEl.paused) audioEl.pause(); };
    video.onseeking = function() { audioEl.currentTime = video.currentTime; };
    video.onratechange = function() { audioEl.playbackRate = video.playbackRate; };
    audioEl.onplay = function() { if (video.paused) video.play(); };
    audioEl.onpause = function() { if (!video.paused) video.pause(); };
    audioEl.onseeking = function() { video.currentTime = audioEl.currentTime; };
}

function seekToSegment(index) {
    var video = document.getElementById('editorVideo');
    if (editedSegments[index]) {
        var start = editedSegments[index].start;
        if (video.style.display !== 'none') {
            var localTime = start - editorVideoOffset;
            var canSeekCurrentClip = localTime >= 0 && isFinite(video.duration) && localTime < video.duration - 0.5;
            if (canSeekCurrentClip) {
                video.currentTime = localTime;
                video.play().catch(function() {});
            } else if (editorVideoLoader) {
                editorVideoLoader(start, true);
            }
        } else {
            if (editorVideoLoader) {
                editorVideoLoader(start, true);
                editorActiveIndex = index;
                highlightActiveSegment();
                return;
            }
            // Audio-only mode: find the embedded audio in placeholder
            var embeddedAudio = document.querySelector('#videoPlaceholder audio');
            if (embeddedAudio && embeddedAudio.src) {
                embeddedAudio.currentTime = start;
                embeddedAudio.play().catch(function() {});
            }
        }
    }
    editorActiveIndex = index;
    highlightActiveSegment();
}

// ==== 词级时间戳逐词条（2026-10-04）====
var editorWordData = null;      // {segments:[{start,end,text,words:[...]}]}
var editorActiveWordIdx = -1;   // 当前高亮的词在当前片段内的下标
var WORD_LOW_CONF = 0.5;        // 低于该置信度视为可疑词

// 载入 <字幕名>.words.json；没有就隐藏逐词条（功能自动降级）
function loadEditorWords(filename) {
    var strip = document.getElementById('wordStrip');
    editorWordData = null;
    editorActiveWordIdx = -1;
    if (strip) strip.style.display = 'none';
    apiFetch('/api/words/' + encodeURIComponent(filename))
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (!d || !d.success || !d.words || !d.words.segments) return;
            editorWordData = d.words;
            renderWordStrip(-1, -1);
        })
        .catch(function() { /* 无词级数据，属正常情况 */ });
}

// 取某个片段的词数组
function wordsOfSegment(segIndex) {
    if (!editorWordData || !editorWordData.segments) return null;
    var seg = editorWordData.segments[segIndex];
    return (seg && seg.words && seg.words.length) ? seg.words : null;
}

// 渲染当前片段的词条
function renderWordStrip(segIndex, activeWordIdx) {
    var strip = document.getElementById('wordStrip');
    var chips = document.getElementById('wordChips');
    var title = document.getElementById('wordStripTitle');
    if (!strip || !chips) return;

    var words = (segIndex >= 0) ? wordsOfSegment(segIndex) : null;
    if (!words) {
        // 没有词就退回提示，而不是留一条空条
        if (segIndex >= 0 && editorWordData) {
            strip.style.display = 'block';
            title.textContent = '逐词 · 第 ' + (segIndex + 1) + ' 段';
            chips.innerHTML = '<div class="word-strip-empty">该片段无词级数据（可能转录时未开启词级时间戳）</div>';
        } else {
            strip.style.display = 'none';
        }
        return;
    }

    strip.style.display = 'block';
    title.textContent = '逐词 · 第 ' + (segIndex + 1) + ' 段 / 共 ' + editorWordData.segments.length + ' 段';
    chips.innerHTML = '';
    words.forEach(function(w, i) {
        var el = document.createElement('span');
        el.className = 'word-chip';
        if (w.probability !== null && w.probability !== undefined && w.probability < WORD_LOW_CONF) {
            el.classList.add('low-conf');
        }
        if (i === activeWordIdx) el.classList.add('active');
        el.textContent = w.text;
        el.title = (w.probability !== null && w.probability !== undefined)
            ? (w.start.toFixed(2) + 's · 置信度 ' + w.probability.toFixed(2))
            : (w.start.toFixed(2) + 's');
        el.onclick = function(e) {
            e.stopPropagation();
            seekToWord(segIndex, w.start);
        };
        chips.appendChild(el);
    });

    var act = chips.querySelector('.word-chip.active');
    if (act && act.scrollIntoView) act.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

// 播放中：找出当前词并更新高亮（只改 class，不整条重绘，避免闪烁）
function updateActiveWord(currentTime) {
    if (!editorWordData) return;
    var segIndex = editorActiveIndex;
    if (segIndex < 0) { editorActiveWordIdx = -1; return; }
    var words = wordsOfSegment(segIndex);
    if (!words) return;

    var idx = -1;
    for (var i = 0; i < words.length; i++) {
        if (currentTime >= words[i].start && currentTime < words[i].end) { idx = i; break; }
    }
    // 词间缝隙（currentTime 落在两词之间）时，保持上一个词高亮更符合观感
    if (idx === editorActiveWordIdx) return;
    if (idx === -1 && currentTime <= words[words.length - 1].end) {
        idx = Math.min(editorActiveWordIdx < 0 ? 0 : editorActiveWordIdx, words.length - 1);
        if (currentTime < words[0].start) idx = -1;
    }
    editorActiveWordIdx = idx;
    renderWordStrip(segIndex, idx);
}

// 点词跳转
function seekToWord(segIndex, t) {
    var video = document.getElementById('editorVideo');
    var local = t - editorVideoOffset;
    if (video && video.style.display !== 'none'
        && local >= 0 && isFinite(video.duration) && local < video.duration - 0.5) {
        video.currentTime = local;
        video.play().catch(function() {});
    } else if (editorVideoLoader) {
        editorVideoLoader(t, true);
    } else {
        seekToSegment(segIndex);
        return;
    }
    editorActiveIndex = segIndex;
    editorActiveWordIdx = wordsOfSegment(segIndex)
        ? wordsOfSegment(segIndex).findIndex(function(w) { return w.start >= t; }) - 1 : -1;
    highlightActiveSegment();
}

function highlightActiveSegment() {
    var items = document.querySelectorAll('.segment-item');
    items.forEach(function(item, i) {
        if (i === editorActiveIndex) {
            item.classList.add('active');
            item.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        } else {
            item.classList.remove('active');
        }
    });
}

// 关闭字幕编辑器
function closeSubtitleEditor() {
    var video = document.getElementById('editorVideo');
    var audio = document.getElementById('editorAudio');
    if (video) { video.pause(); video.src = ''; }
    if (audio) { audio.pause(); audio.src = ''; }
    stopActivePreviewStream();
    document.getElementById('subtitleEditorModal').style.display = 'none';
    document.body.style.overflow = 'auto';
    currentEditingSubtitle = null;
    originalSegments = [];
    editedSegments = [];
}

// 渲染字幕片段
function renderSegments(readOnly) {
    if (readOnly === undefined) readOnly = false;
    var container = document.getElementById('segmentsContainer');
    container.innerHTML = '';
    
    editedSegments.forEach(function(segment, index) {
        var div = document.createElement('div');
        div.className = 'segment-item' + (index === editorActiveIndex ? ' active' : '');
        div.setAttribute('data-index', index);
        div.onclick = function(e) {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'BUTTON') return;
            seekToSegment(index);
        };
        
        var totalDuration = editedSegments.length > 0 ? editedSegments[editedSegments.length - 1].end : 1;
        var barLeft = (segment.start / totalDuration * 100).toFixed(1);
        var barWidth = ((segment.end - segment.start) / totalDuration * 100).toFixed(1);
        if (barWidth < 0.5) barWidth = 0.5;
        
        div.innerHTML = 
            '<div class="segment-header">' +
                '<span class="segment-number">' + (index + 1) + '</span>' +
                '<div class="segment-time">' +
                    '<input type="text" class="time-input" value="' + formatSecondsToTime(segment.start) + '" ' +
                        'onchange="updateSegmentTime(' + index + ', \'start\', this.value)"' +
                        (readOnly ? 'readonly' : '') + '>' +
                    '<span>&rarr;</span>' +
                    '<input type="text" class="time-input" value="' + formatSecondsToTime(segment.end) + '" ' +
                        'onchange="updateSegmentTime(' + index + ', \'end\', this.value)"' +
                        (readOnly ? 'readonly' : '') + '>' +
                '</div>' +
                '<div class="segment-actions">' +
                    '<button class="btn btn-small btn-secondary" onclick="event.stopPropagation();moveSegment(' + index + ',-1)" ' +
                        (index === 0 || readOnly ? 'disabled' : '') + ' title="上移">&uarr;</button>' +
                    '<button class="btn btn-small btn-secondary" onclick="event.stopPropagation();moveSegment(' + index + ',1)" ' +
                        (index === editedSegments.length - 1 || readOnly ? 'disabled' : '') + ' title="下移">&darr;</button>' +
                    '<button class="btn btn-small btn-secondary" onclick="event.stopPropagation();splitSegment(' + index + ')" ' +
                        (readOnly ? 'disabled' : '') + ' title="拆分">&harr;</button>' +
                    '<button class="btn btn-small btn-secondary" onclick="event.stopPropagation();mergeWithNext(' + index + ')" ' +
                        (index === editedSegments.length - 1 || readOnly ? 'disabled' : '') + ' title="与下一个合并">+</button>' +
                    '<button class="btn btn-small btn-danger" onclick="event.stopPropagation();deleteSegment(' + index + ')" ' +
                        (readOnly ? 'disabled' : '') + ' title="删除">&times;</button>' +
                '</div>' +
            '</div>' +
            '<textarea class="segment-text" rows="' + Math.max(2, Math.min(10, String(segment.text).split('\n').length)) + '" placeholder="字幕内容..." oninput="updateSegmentText(' + index + ',this.value)" ' +
                (readOnly ? 'readonly' : '') + '>' + escapeHtml(segment.text) + '</textarea>' +
            '<div class="segment-timing-bar"><div class="segment-timing-fill" style="left:' + barLeft + '%;width:' + barWidth + '%"></div></div>';
        container.appendChild(div);
    });
}

function escapeHtml(str) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(str == null ? '' : String(str)));
    return div.innerHTML;
}

function escapeAttr(str) {
    return String(str == null ? '' : str)
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function jsString(str) {
    return JSON.stringify(str == null ? '' : String(str));
}

// 格式化秒数为时间字符串
function formatSecondsToTime(seconds) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    const ms = Math.floor((seconds - Math.floor(seconds)) * 1000);
    return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')},${ms.toString().padStart(3, '0')}`;
}

// 解析时间字符串为秒数
function parseTimeToSeconds(timeStr) {
    const parts = timeStr.split(':');
    if (parts.length === 3) {
        const [h, m, s_ms] = parts;
        const [s, ms] = s_ms.split(',');
        return parseInt(h) * 3600 + parseInt(m) * 60 + parseInt(s) + (parseInt(ms || 0) / 1000);
    }
    return 0;
}

// 更新片段时间
function updateSegmentTime(index, type, value) {
    const seconds = parseTimeToSeconds(value);
    editedSegments[index][type] = seconds;
    updateEditorStats();
}

// 更新片段文本
function updateSegmentText(index, value) {
    editedSegments[index].text = value;
}

// 添加新片段
function addSegment() {
    var lastSegment = editedSegments[editedSegments.length - 1];
    var newSegment = {
        start: lastSegment ? lastSegment.end : 0,
        end: lastSegment ? lastSegment.end + 3 : 3,
        text: '新字幕片段'
    };
    editedSegments.push(newSegment);
    renderSegments();
    updateEditorStats();
}

// 合并当前片段与下一个片段
function mergeWithNext(index) {
    if (index >= editedSegments.length - 1) return;
    var current = editedSegments[index];
    var next = editedSegments[index + 1];
    current.end = next.end;
    current.text = current.text.trim() + ' ' + next.text.trim();
    editedSegments.splice(index + 1, 1);
    renderSegments();
    updateEditorStats();
    showToast('已合并片段 ' + (index + 1) + ' 和 ' + (index + 2), 'info');
}

// 拆分当前片段 (在播放位置或中点)
function splitSegment(index) {
    var seg = editedSegments[index];
    var video = document.getElementById('editorVideo');
    var splitAt;
    var originalTime = video.currentTime + editorVideoOffset;
    if (video.style.display !== 'none' && originalTime > seg.start && originalTime < seg.end) {
        splitAt = originalTime;
    } else {
        splitAt = (seg.start + seg.end) / 2;
    }
    var seg1 = { start: seg.start, end: splitAt, text: seg.text };
    var seg2 = { start: splitAt, end: seg.end, text: '' };
    editedSegments.splice(index, 1, seg1, seg2);
    renderSegments();
    updateEditorStats();
    showToast('已在 ' + formatSecondsToTime(splitAt) + ' 处拆分', 'info');
}

// 删除片段
function deleteSegment(index) {
    if (editedSegments.length > 1) {
        editedSegments.splice(index, 1);
        renderSegments();
        updateEditorStats();
    } else {
        showToast('至少需要保留一个字幕片段', 'warning');
    }
}

// 移动片段
function moveSegment(index, direction) {
    const newIndex = index + direction;
    if (newIndex >= 0 && newIndex < editedSegments.length) {
        [editedSegments[index], editedSegments[newIndex]] = [editedSegments[newIndex], editedSegments[index]];
        renderSegments();
        updateEditorStats();
    }
}

// 重置更改
function resetChanges() {
    showConfirm('重置更改', '确定要重置所有更改吗？', function() {
        editedSegments = JSON.parse(JSON.stringify(originalSegments));
        renderSegments();
        updateEditorStats();
        showToast('已重置所有更改', 'info');
    }, '重置', true);
}

function shiftEditedSegments(delta) {
    delta = Number(delta) || 0;
    if (!editedSegments.length || delta === 0) return;
    editedSegments = editedSegments.map(function(seg) {
        return {
            start: Math.max(0, (Number(seg.start) || 0) + delta),
            end: Math.max(0.01, (Number(seg.end) || 0) + delta),
            text: seg.text || ''
        };
    });
    renderSegments();
    updateEditorStats();
    showToast('已整体' + (delta > 0 ? '延后 ' : '提前 ') + Math.abs(delta) + ' 秒', 'success');
}

async function checkCurrentSubtitleQuality() {
    if (!currentEditingSubtitle) return;
    try {
        const visible = editedSegments.map(function(seg) { return Object.assign({}, seg); });
        document.querySelectorAll('#segmentsContainer .segment-text').forEach(function(area,index) {
            if (visible[index] && !area.readOnly) visible[index].text=area.value;
        });
        const response = await apiFetch('/api/subtitle-quality/' + encodeURIComponent(currentEditingSubtitle), {
            method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({segments:visible})
        });
        const result=await response.json();
        if (!result.success) throw new Error(result.error || '质检失败');
        const localReport=result.report;
        showTools(true);
        writeToolsOutput({
            file: currentEditingSubtitle,
            score: localReport.score,
            score_scope: localReport.score_scope,
            reading_by_script: localReport.reading_by_script,
            thresholds: localReport.thresholds,
            total_segments: localReport.total_segments,
            summary: localReport.summary,
            issues: localReport.issues
        });
        showToast('当前内容排版与时间评分 ' + localReport.score, localReport.score >= 80 ? 'success' : 'warning');
    } catch (e) {
        showToast('质检失败: ' + e.message, 'error');
    }
}

function buildLocalQualityReport(segments) {
    var issues = [];
    var summary = { empty: 0, overlaps: 0, long_lines: 0, too_fast: 0, invalid_time: 0 };
    var previousEnd = 0;
    segments.forEach(function(seg, idx) {
        var text = (seg.text || '').trim();
        var start = Number(seg.start) || 0;
        var end = Number(seg.end) || 0;
        var duration = Math.max(end - start, 0);
        if (!text) { summary.empty++; issues.push({ index: idx + 1, type: 'empty', message: '空字幕' }); }
        if (end <= start) { summary.invalid_time++; issues.push({ index: idx + 1, type: 'invalid_time', message: '结束时间不大于开始时间' }); }
        if (idx > 0 && start < previousEnd) { summary.overlaps++; issues.push({ index: idx + 1, type: 'overlap', message: '与上一条字幕重叠' }); }
        var longest = text.split('\n').reduce(function(max, line) { return Math.max(max, line.length); }, 0);
        if (longest > 42) { summary.long_lines++; issues.push({ index: idx + 1, type: 'long_line', message: '单行过长 (' + longest + ' 字)' }); }
        var cps = duration > 0 ? text.replace(/\n/g, '').length / duration : 0;
        if (cps > 18) { summary.too_fast++; issues.push({ index: idx + 1, type: 'too_fast', message: '阅读速度偏快 (' + cps.toFixed(1) + ' 字/秒)' }); }
        previousEnd = Math.max(previousEnd, end);
    });
    var penalty = summary.invalid_time * 12 + summary.overlaps * 10 + summary.empty * 5 + summary.long_lines * 2 + summary.too_fast * 3;
    return { score: Math.max(0, Math.min(100, 100 - penalty)), total_segments: segments.length, summary: summary, issues: issues.slice(0, 200) };
}

// 保存字幕更改
async function saveSubtitleChanges() {
    // Capture the visible values even when the focused field has not blurred.
    document.querySelectorAll('#segmentsContainer .segment-text').forEach(function(area, index) {
        if (editedSegments[index] && !area.readOnly) editedSegments[index].text = area.value;
    });
    try {
        const response = await apiFetch('/api/save-subtitle', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                filename: currentEditingSubtitle,
                format: document.getElementById('editorFormat').textContent.toLowerCase(),
                segments: editedSegments
            })
        });
        
        const result = await response.json();
        
        if (result.success) {
            editorWordData = null;
            showToast(result.message || '字幕保存成功', 'success');
            closeSubtitleEditor();
            loadSubtitles(); // 刷新字幕列表
        } else {
            showToast('保存失败: ' + result.error, 'error');
        }
    } catch (error) {
        showToast('保存失败: ' + error.message, 'error');
    }
}

// 更新编辑器统计信息
function updateEditorStats() {
    const totalSegments = editedSegments.length;
    const totalDuration = editedSegments.length > 0 
        ? editedSegments[editedSegments.length - 1].end 
        : 0;
    
    document.getElementById('totalSegments').textContent = totalSegments;
    document.getElementById('totalDuration').textContent = formatDuration(totalDuration);
}

// 主题切换
function toggleTheme() {
    const html = document.documentElement;
    const themeIcon = document.getElementById('themeIcon');
    const themeText = document.getElementById('themeText');

    if (html.getAttribute('data-theme') === 'dark') {
        html.removeAttribute('data-theme');
        if (themeIcon) themeIcon.textContent = '🌙';
        if (themeText) themeText.textContent = '深色';
        localStorage.setItem('theme', 'light');
    } else {
        html.setAttribute('data-theme', 'dark');
        if (themeIcon) themeIcon.textContent = '☀️';
        if (themeText) themeText.textContent = '浅色';
        localStorage.setItem('theme', 'dark');
    }
}

// 初始化主题
function initTheme() {
    const savedTheme = localStorage.getItem('theme');
    const themeIcon = document.getElementById('themeIcon');
    const themeText = document.getElementById('themeText');

    if (savedTheme === 'dark') {
        document.documentElement.setAttribute('data-theme', 'dark');
        if (themeIcon) themeIcon.textContent = '☀️';
        if (themeText) themeText.textContent = '浅色';
    } else {
        if (themeIcon) themeIcon.textContent = '🌙';
        if (themeText) themeText.textContent = '深色';
    }
    // 检测系统主题偏好
    if (!savedTheme && window.matchMedia('(prefers-color-scheme: dark)').matches) {
        document.documentElement.setAttribute('data-theme', 'dark');
        if (themeIcon) themeIcon.textContent = '☀️';
        if (themeText) themeText.textContent = '浅色';
    }
}

// 显示历史记录
function showHistory() {
    loadHistory();
    document.getElementById('historyModal').style.display = 'flex';
    document.body.style.overflow = 'hidden';
}

// 关闭历史记录
function closeHistory() {
    document.getElementById('historyModal').style.display = 'none';
    document.body.style.overflow = 'auto';
}

// 加载历史记录
async function loadHistory() {
    var history = [];
    try {
        var resp = await apiFetch('/api/history');
        var result = await resp.json();
        if (result.success) {
            history = result.history || [];
        }
    } catch (e) {
        history = JSON.parse(localStorage.getItem('subtitleHistory') || '[]');
    }
    if (history.length === 0) {
        history = JSON.parse(localStorage.getItem('subtitleHistory') || '[]');
    }
    renderHistory(history);
}

// 渲染历史记录
function renderHistory(history) {
    const container = document.getElementById('historyContainer');
    
    if (history.length === 0) {
        container.innerHTML = '<div class="empty-state"><p>暂无历史记录</p></div>';
        return;
    }
    
    // 按时间倒序排列
    const sortedHistory = history.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
    
    const html = sortedHistory.map(item => `
        <div class="history-item">
            <div class="history-header">
                <div class="history-filename">${escapeHtml(item.filename)}</div>
                <div class="history-status ${escapeHtml(item.status)}">${escapeHtml(getStatusText(item.status))}</div>
            </div>
            <div class="history-details">
                <div class="history-detail">
                    <span>模型:</span>
                    <span>${escapeHtml(item.model || 'N/A')}</span>
                </div>
                <div class="history-detail">
                    <span>语言:</span>
                    <span>${escapeHtml(item.language || 'auto')}</span>
                </div>
                <div class="history-detail">
                    <span>格式:</span>
                    <span>${escapeHtml(item.format || 'srt')}</span>
                </div>
                <div class="history-detail">
                    <span>设备:</span>
                    <span>${escapeHtml(item.device || 'cpu')}</span>
                </div>
            </div>
            ${item.result ? `
                <div class="history-details">
                    <div class="history-detail">
                        <span>检测语言:</span>
                        <span>${escapeHtml(item.result.detected_language || 'unknown')}</span>
                    </div>
                    <div class="history-detail">
                        <span>时长:</span>
                        <span>${formatDuration(item.result.duration || 0)}</span>
                    </div>
                </div>
            ` : ''}
            ${item.error ? `
                <div class="history-error" style="color: var(--error-color); font-size: 0.9rem; margin-top: 8px;">
                    错误: ${escapeHtml(item.error)}
                </div>
            ` : ''}
            <div class="history-time">处理时间: ${new Date(item.timestamp).toLocaleString()}</div>
        </div>
    `).join('');
    
    container.innerHTML = html;
    container.querySelectorAll('[data-queue-action]').forEach(button => {
        button.addEventListener('click', () => {
            const actions = {cancel: cancelQueueTask, retry: retryQueueTask, delete: deleteQueueTask};
            actions[button.dataset.queueAction](button.dataset.taskId);
        });
    });
}

// 添加历史记录
function addHistoryItem(data) {
    var item = { ...data, timestamp: new Date().toISOString() };
    // 同步到服务端
    apiFetch('/api/history', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(item)
    }).catch(function() {});
    // localStorage 作为 fallback
    var history = JSON.parse(localStorage.getItem('subtitleHistory') || '[]');
    history.push(item);
    if (history.length > 100) history.splice(0, history.length - 100);
    localStorage.setItem('subtitleHistory', JSON.stringify(history));
}

// 清空历史记录
function clearHistory() {
    showConfirm('清空历史', '确定要清空所有历史记录吗？此操作不可撤销。', function() {
        apiFetch('/api/history', { method: 'DELETE' }).catch(function() {});
        localStorage.removeItem('subtitleHistory');
        loadHistory();
        showToast('历史记录已清空', 'success');
    }, '清空', true);
}

// 导出历史记录
function exportHistory() {
    const history = JSON.parse(localStorage.getItem('subtitleHistory') || '[]');
    const dataStr = JSON.stringify(history, null, 2);
    const dataBlob = new Blob([dataStr], {type: 'application/json'});
    const url = URL.createObjectURL(dataBlob);
    
    const link = document.createElement('a');
    link.href = url;
    link.download = `subtitle_history_${new Date().toISOString().split('T')[0]}.json`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
    
    showToast('历史记录已导出', 'success');
}

// 显示系统信息
async function showSystemInfo() {
    try {
        const response = await apiFetch('/api/system-info');
        const result = await response.json();
        
        if (result.success) {
            renderSystemInfo(result.info);
            document.getElementById('systemInfoModal').style.display = 'flex';
            document.body.style.overflow = 'hidden';
        } else {
            showToast('获取系统信息失败: ' + result.error, 'error');
        }
    } catch (error) {
        showToast('获取系统信息失败: ' + error.message, 'error');
    }
}

// 关闭系统信息
function closeSystemInfo() {
    document.getElementById('systemInfoModal').style.display = 'none';
    document.body.style.overflow = 'auto';
}

// 渲染系统信息
function renderSystemInfo(info) {
    const container = document.getElementById('systemInfoContainer');
    
    let html = '<div class="system-info-grid">';
    
    // CPU信息
    if (info.cpu) {
        html += `
            <div class="info-card">
                <h4>🖥️ CPU信息</h4>
                <div class="info-item">
                    <span>核心数:</span>
                    <span>${info.cpu.count}</span>
                </div>
                <div class="info-item">
                    <span>使用率:</span>
                    <span>${info.cpu.percent.toFixed(1)}%</span>
                </div>
                ${info.cpu.freq ? `
                <div class="info-item">
                    <span>频率:</span>
                    <span>${info.cpu.freq.current.toFixed(0)} MHz</span>
                </div>` : ''}
            </div>
        `;
    }
    
    // 内存信息
    if (info.memory) {
        html += `
            <div class="info-card">
                <h4>💾 内存信息</h4>
                <div class="info-item">
                    <span>总内存:</span>
                    <span>${info.memory.total_gb.toFixed(2)} GB</span>
                </div>
                <div class="info-item">
                    <span>可用内存:</span>
                    <span>${info.memory.available_gb.toFixed(2)} GB</span>
                </div>
                <div class="info-item">
                    <span>使用率:</span>
                    <span>${info.memory.percent.toFixed(1)}%</span>
                </div>
                <div class="progress-bar small">
                    <div class="progress-fill" style="width: ${info.memory.percent}%"></div>
                </div>
            </div>
        `;
    }
    
    // GPU信息
    if (info.gpu) {
        html += `
            <div class="info-card">
                <h4>🎮 GPU信息</h4>
                ${info.gpu.available ? `
                    <div class="info-item">
                        <span>状态:</span>
                        <span class="status-good">可用</span>
                    </div>
                    <div class="info-item">
                        <span>设备名称:</span>
                        <span>${info.gpu.device_name || 'N/A'}</span>
                    </div>
                    <div class="info-item">
                        <span>总显存:</span>
                        <span>${info.gpu.memory_total_gb.toFixed(2)} GB</span>
                    </div>
                    <div class="info-item">
                        <span>已分配:</span>
                        <span>${info.gpu.memory_allocated_gb.toFixed(2)} GB</span>
                    </div>
                    <div class="info-item">
                        <span>缓存:</span>
                        <span>${info.gpu.memory_cached_gb.toFixed(2)} GB</span>
                    </div>
                    <div class="progress-bar small">
                        <div class="progress-fill" style="width: ${(info.gpu.memory_allocated_gb / info.gpu.memory_total_gb * 100)}%"></div>
                    </div>
                ` : `
                    <div class="info-item">
                        <span>状态:</span>
                        <span class="status-bad">不可用</span>
                    </div>
                `}
            </div>
        `;
    }
    
    // 磁盘信息
    if (info.disk) {
        html += `
            <div class="info-card">
                <h4>💿 磁盘信息</h4>
                <div class="info-item">
                    <span>总空间:</span>
                    <span>${info.disk.total_gb.toFixed(2)} GB</span>
                </div>
                <div class="info-item">
                    <span>可用空间:</span>
                    <span>${info.disk.free_gb.toFixed(2)} GB</span>
                </div>
                <div class="info-item">
                    <span>使用率:</span>
                    <span>${info.disk.percent.toFixed(1)}%</span>
                </div>
                <div class="progress-bar small">
                    <div class="progress-fill" style="width: ${info.disk.percent}%"></div>
                </div>
            </div>
        `;
    }
    
    // 当前模型信息
    if (info.current_model) {
        html += `
            <div class="info-card">
                <h4>🤖 当前模型</h4>
                <div class="info-item">
                    <span>模型大小:</span>
                    <span>${info.current_model.size.toUpperCase()}</span>
                </div>
                <div class="info-item">
                    <span>运行设备:</span>
                    <span>${info.current_model.device.toUpperCase()}</span>
                </div>
                <div class="info-item">
                    <span>加载状态:</span>
                    <span class="${info.current_model.loaded ? 'status-good' : 'status-bad'}">
                        ${info.current_model.loaded ? '已加载' : '未加载'}
                    </span>
                </div>
            </div>
        `;
    }
    
    html += '</div>';
    
    container.innerHTML = html;
}

// 显示Toast通知
function showToast(message, type = 'info') {
    toast.textContent = message;
    toast.className = 'toast ' + type + ' show';

    setTimeout(function() {
        toast.className = 'toast ' + type;
    }, 3000);
}

// ===== CUSTOM CONFIRM DIALOG =====
var _confirmCallback = null;

function showConfirm(title, message, callback, okText, dangerMode) {
    okText = okText || '确定';
    document.getElementById('confirmTitle').textContent = title;
    document.getElementById('confirmMessage').textContent = message;
    var okBtn = document.getElementById('confirmOkBtn');
    okBtn.textContent = okText;
    okBtn.className = 'btn ' + (dangerMode ? 'btn-danger' : 'btn-primary');
    
    document.getElementById('confirmModal').style.display = 'flex';
    
    _confirmCallback = callback;
}

document.addEventListener('DOMContentLoaded', function() {
    var confirmOk = document.getElementById('confirmOkBtn');
    var confirmCancel = document.getElementById('confirmCancelBtn');
    if (confirmOk && confirmCancel) {
        confirmOk.addEventListener('click', function() {
            document.getElementById('confirmModal').style.display = 'none';
            if (_confirmCallback) { var cb = _confirmCallback; _confirmCallback = null; cb(); }
        });
        confirmCancel.addEventListener('click', function() {
            document.getElementById('confirmModal').style.display = 'none';
            _confirmCallback = null;
        });
    }
});

// ===== 页面内输入对话框（替代原生 prompt/confirm：内嵌 WebView 会禁用原生弹窗）=====
// uiPrompt 返回 Promise<string|null>：null=取消；空字符串=用户提交了空值（对"留空表示默认行为"的调用方有意义）
function uiPrompt(title, message, options) {
    options = options || {};
    return new Promise(function(resolve) {
        var modal = document.getElementById('inputModal');
        var field = document.getElementById('inputModalField');
        var errBox = document.getElementById('inputModalError');
        document.getElementById('inputModalTitle').textContent = title || '输入';
        document.getElementById('inputModalMessage').textContent = message || '';
        field.value = options.defaultValue != null ? options.defaultValue : '';
        field.placeholder = options.placeholder || '';
        errBox.style.display = 'none';
        errBox.textContent = '';
        var okBtn = document.getElementById('inputModalOkBtn');
        okBtn.textContent = options.okText || '确定';
        modal.style.display = 'flex';

        function done(value) {
            modal.style.display = 'none';
            okBtn.removeEventListener('click', onOk);
            field.removeEventListener('keydown', onKey);
            document.removeEventListener('keydown', onEsc, true);
            resolve(value);
        }
        function onOk() {
            var v = field.value;
            if (options.validate) {
                var err = options.validate(v);
                if (err) { errBox.textContent = err; errBox.style.display = 'block'; return; }
            }
            done(v);
        }
        function onKey(e) {
            if (e.key === 'Enter') { e.preventDefault(); onOk(); }
        }
        function onEsc(e) {
            if (e.key === 'Escape') { e.stopPropagation(); done(null); }
        }
        okBtn.addEventListener('click', onOk);
        document.getElementById('inputModalCancelBtn').onclick = function() { done(null); };
        field.addEventListener('keydown', onKey);
        document.addEventListener('keydown', onEsc, true);
        setTimeout(function() { field.focus(); field.select(); }, 80);
    });
}

// Promise 化确认对话框（showConfirm 的 async 版本，旧回调式保留兼容）
function uiConfirmAsync(title, message, options) {
    options = options || {};
    return new Promise(function(resolve) {
        var modal = document.getElementById('confirmModal');
        var okBtn = document.getElementById('confirmOkBtn');
        document.getElementById('confirmTitle').textContent = title || '确认操作';
        document.getElementById('confirmMessage').textContent = message || '';
        okBtn.textContent = options.okText || '确定';
        okBtn.className = 'btn ' + (options.danger ? 'btn-danger' : 'btn-primary');
        modal.style.display = 'flex';

        function done(result) {
            modal.style.display = 'none';
            okBtn.removeEventListener('click', onOk);
            document.getElementById('confirmCancelBtn').removeEventListener('click', onCancel);
            document.removeEventListener('keydown', onEsc, true);
            resolve(result);
        }
        function onOk() { done(true); }
        function onCancel() { done(false); }
        function onEsc(e) {
            if (e.key === 'Escape') { e.stopPropagation(); done(false); }
        }
        okBtn.addEventListener('click', onOk);
        document.getElementById('confirmCancelBtn').addEventListener('click', onCancel);
        document.addEventListener('keydown', onEsc, true);
    });
}

// 翻译功能相关代码已移除（功能未实现）


// ============================================================================
// 2026-06-22 新增：内嵌字幕轨提取（Phase A）
// ============================================================================

let currentExtractTaskId = null;
let extractPollInterval = null;

async function checkAndExtractSubtitle(filePath) {
    // 探测视频字幕轨，如果有可抽的字幕则弹层选 stream；没有则提示
    try {
        const resp = await apiFetch(`/api/subtitle-streams/${encodeURIComponent(filePath.split('/').pop())}?is_nas_file=true&path=${encodeURIComponent(filePath)}`);
        const data = await resp.json();
        if (!data.success) {
            showToast('探测字幕轨失败: ' + (data.error || '未知错误'), 'error');
            return;
        }
        const streams = data.streams || [];
        const textStreams = streams.filter(s => s.is_text);
        if (textStreams.length === 0) {
            showToast(`该视频无文本字幕轨（共 ${streams.length} 条轨道${streams.length > 0 ? '，但都是图像字幕（PGS/DVB），暂不支持' : ''}）`, 'warning');
            return;
        }
        showExtractSubtitleModal(filePath, streams, textStreams);
    } catch (e) {
        showToast('探测失败: ' + e.message, 'error');
    }
}

function showExtractSubtitleModal(filePath, allStreams, textStreams) {
    const fileName = filePath.split('/').pop();
    const modal = document.getElementById('extractSubtitleModal');
    const container = document.getElementById('extractSubtitleStreams');
    if (!modal || !container) {
        showToast('弹层 DOM 缺失', 'error');
        return;
    }
    document.getElementById('extractSubtitleFileName').textContent = fileName;
    document.getElementById('extractSubtitleModal').dataset.filePath = filePath;

    container.innerHTML = '';
    textStreams.forEach(s => {
        const langLabel = s.language || '未知语种';
        const titleLabel = s.title ? ` (${s.title})` : '';
        const codecLabel = s.codec_name;
        const item = document.createElement('div');
        item.className = 'subtitle-stream-item';
        item.innerHTML = `
            <div class="stream-info">
                <strong>轨 #${s.index}</strong>
                <span class="stream-codec">${escapeHtml(codecLabel)}</span>
                <span class="stream-lang">${escapeHtml(langLabel + titleLabel)}</span>
            </div>
            <button class="btn btn-primary btn-small" onclick="triggerExtractSubtitle(${s.index})">抽取此轨</button>
        `;
        container.appendChild(item);
    });

    if (textStreams.length === 0) {
        container.innerHTML = '<div class="empty-state">无可抽取的文本字幕轨</div>';
    }

    // 提示还有多少图像字幕（不可抽）
    const imageStreams = allStreams.filter(s => !s.is_text);
    if (imageStreams.length > 0) {
        container.innerHTML += `<div class="hint">另有 ${imageStreams.length} 条图像字幕（PGS/DVB）暂不支持抽取</div>`;
    }

    modal.style.display = 'flex';
}

function closeExtractSubtitleModal() {
    document.getElementById('extractSubtitleModal').style.display = 'none';
    currentExtractTaskId = null;
    if (extractPollInterval) {
        clearInterval(extractPollInterval);
        extractPollInterval = null;
    }
}

async function triggerExtractSubtitle(streamIndex) {
    const modal = document.getElementById('extractSubtitleModal');
    const filePath = modal.dataset.filePath;
    if (!filePath) return;

    try {
        const resp = await apiFetch('/api/extract-subtitle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                filename: filePath.split('/').pop(),
                path: filePath,
                is_nas_file: true,
                stream_index: streamIndex,
                save_to_source_dir: false,
            }),
        });
        const data = await resp.json();
        if (!data.success) {
            showToast('启动抽取失败: ' + (data.error || '未知'), 'error');
            return;
        }
        currentExtractTaskId = data.task_id;
        showToast(`字幕抽取任务已启动 (${streamIndex})`, 'success');

        // 弹层内显示进度
        document.getElementById('extractSubtitleProgress').style.display = 'block';
        if (extractPollInterval) clearInterval(extractPollInterval);
        extractPollInterval = setInterval(pollExtractStatus, 1000);
        pollExtractStatus();  // 立刻拉一次
    } catch (e) {
        showToast('启动失败: ' + e.message, 'error');
    }
}

async function pollExtractStatus() {
    if (!currentExtractTaskId) return;
    try {
        const resp = await apiFetch(`/api/status/${currentExtractTaskId}`);
        const data = await resp.json();
        // /api/status 返回 { success, task: {...} }——原实现直接读顶层字段，
        // 进度永远是 0、完成状态永远检测不到（轮询弹层卡在 0%）
        const task = data.task || {};
        const pct = (task.progress || 0).toFixed(1);
        const info = task.progress_info || '';
        document.getElementById('extractSubtitleProgressText').textContent = `[${pct}%] ${info}`;

        if (task.status === 'completed') {
            clearInterval(extractPollInterval);
            extractPollInterval = null;
            currentExtractTaskId = null;
            showToast('✅ 字幕提取完成！已保存到字幕列表', 'success');
            loadSubtitles();
            setTimeout(closeExtractSubtitleModal, 2000);
        } else if (task.status === 'failed' || task.status === 'cancelled') {
            clearInterval(extractPollInterval);
            extractPollInterval = null;
            currentExtractTaskId = null;
            showToast(`❌ 字幕提取${task.status === 'failed' ? '失败' : '已取消'}: ${task.error || ''}`, 'error');
        }
    } catch (e) {
        // 静默忽略（轮询临时错误）
    }
}


// ============================================================================
// 2026-06-22 新增：任务队列管理（Phase B）
// ============================================================================

let queuePollInterval = null;

let queueEventSource = null;  // 2026-06-22 C3: SSE EventSource

function showQueue() {
    document.getElementById('queueModal').style.display = 'flex';
    pollQueueList();
    if (queuePollInterval) clearInterval(queuePollInterval);
    queuePollInterval = setInterval(() => {
        if (!document.getElementById('queueAutoRefresh').checked) return;
        if (document.getElementById('queueModal').style.display === 'none') {
            clearInterval(queuePollInterval);
            queuePollInterval = null;
            return;
        }
        pollQueueList();
    }, 3000);

    // 2026-06-22 C3: EventSource 订阅（收到事件立即刷新）
    if (queueEventSource) {
        try { queueEventSource.close(); } catch (e) {}
        queueEventSource = null;
    }
    if (typeof EventSource !== 'undefined') {
        try {
            // EventSource 不支持自定义 header，启用鉴权时通过 query param 传 token
            const sseUrl = AUTH_TOKEN ? '/api/queue/stream?token=' + encodeURIComponent(AUTH_TOKEN) : '/api/queue/stream';
            queueEventSource = new EventSource(sseUrl);
            queueEventSource.addEventListener('task_added', () => pollQueueList());
            queueEventSource.addEventListener('task_progress', () => pollQueueList());
            queueEventSource.addEventListener('task_completed', () => pollQueueList());
            queueEventSource.addEventListener('task_failed', () => pollQueueList());
            queueEventSource.addEventListener('task_cancelled', () => pollQueueList());
            queueEventSource.onerror = (e) => {
                // 连接断开，依赖轮询兜底
                console.warn('SSE error, fallback to polling', e);
            };
        } catch (e) {
            console.warn('EventSource not available, use polling only', e);
        }
    }
}

function closeQueue() {
    document.getElementById('queueModal').style.display = 'none';
    if (queuePollInterval) {
        clearInterval(queuePollInterval);
        queuePollInterval = null;
    }
    if (queueEventSource) {
        try { queueEventSource.close(); } catch (e) {}
        queueEventSource = null;
    }
}


// E1: 添加任务相关函数
function showAddTaskModal() {
    document.getElementById('addTaskModal').style.display = 'flex';
    // 清空消息
    const msgEl = document.getElementById('addTaskMessage');
    if (msgEl) { msgEl.textContent = ''; msgEl.style.color = ''; }
    // 默认填一个 NAS 视频路径示例
    const pathInput = document.getElementById('addTaskVideoPath');
    if (pathInput && !pathInput.value) {
        pathInput.value = '/nas/videos/example.mkv';
    }
}

function closeAddTaskModal() {
    document.getElementById('addTaskModal').style.display = 'none';
}

async function submitNewTask() {
    const videoPath = document.getElementById('addTaskVideoPath').value.trim();
    const taskType = document.getElementById('addTaskType').value;
    const priority = parseInt(document.getElementById('addTaskPriority').value);
    const msgEl = document.getElementById('addTaskMessage');

    if (!videoPath) {
        msgEl.textContent = '请填写视频路径';
        msgEl.style.color = 'red';
        return;
    }

    // 构造 payload
    let payload = {};
    if (taskType === 'extract_subtitle') {
        const streamIndex = parseInt(document.getElementById('addTaskStreamIndex').value) || 0;
        const mode = document.getElementById('addTaskMode').value;
        const ocrLang = document.getElementById('addTaskOcrLang').value;
        payload = {stream_index: streamIndex, mode: mode, ocr_lang: ocrLang};
    }

    msgEl.textContent = '提交中...';
    msgEl.style.color = 'blue';

    try {
        const resp = await apiFetch('/api/queue/add', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                type: taskType,
                video_path: videoPath,
                payload: payload,
                priority: priority,
            }),
        });
        const data = await resp.json();
        if (data.success) {
            msgEl.textContent = '✅ 任务已入队: ' + data.task_id;
            msgEl.style.color = 'green';
            pollQueueList();  // 立即刷新队列列表
            // 2 秒后关闭弹层
            setTimeout(() => {
                closeAddTaskModal();
                if (typeof showToast === 'function') {
                    showToast('任务已入队: ' + data.task_id, 'success');
                }
            }, 1500);
        } else {
            msgEl.textContent = '❌ 失败: ' + (data.error || '未知错误');
            msgEl.style.color = 'red';
        }
    } catch (e) {
        msgEl.textContent = '❌ 异常: ' + e.message;
        msgEl.style.color = 'red';
    }
}

async function pollQueueList() {
    try {
        const resp = await apiFetch('/api/queue/list?limit=100');
        const data = await resp.json();
        if (!data.success) return;
        renderQueueStats(data.stats);
        renderQueueList(data.tasks);
    } catch (e) {
        // 静默忽略
    }
}

function renderQueueStats(stats) {
    document.getElementById('queueStatWorkers').textContent = stats.active_workers || 0;
    document.getElementById('queueStatProcessing').textContent = stats.processing || 0;
    document.getElementById('queueStatPending').textContent = stats.pending || 0;
    document.getElementById('queueStatCompleted').textContent = stats.completed || 0;
    document.getElementById('queueStatFailed').textContent = stats.failed || 0;
}

// TaskQueue stores NAS local time without an offset. Keep that wall-clock
// value intact; only explicitly zoned timestamps should be converted.
function formatQueueTime(timeStr) {
    if (!timeStr) return '';
    if (/[zZ]$|[+-]\d{2}:?\d{2}$/.test(timeStr)) {
        const d = new Date(timeStr.replace(' ', 'T'));
        return isNaN(d.getTime()) ? timeStr : d.toLocaleString();
    }
    return timeStr.replace('T', ' ');
}

function renderQueueList(tasks) {
    const container = document.getElementById('queueContainer');
    if (!tasks || tasks.length === 0) {
        container.innerHTML = '<div class="empty-state"><p>队列为空</p></div>';
        return;
    }

    // 按 status 分组
    const groups = { processing: [], pending: [], completed: [], failed: [], cancelled: [] };
    tasks.forEach(t => {
        if (!groups[t.status]) groups[t.status] = [];
        groups[t.status].push(t);
    });

    const groupOrder = ['processing', 'pending', 'failed', 'completed', 'cancelled'];
    const groupLabels = {
        processing: '🔄 处理中',
        pending: '⏳ 等待中',
        failed: '❌ 失败',
        completed: '✅ 完成',
        cancelled: '🚫 已取消',
    };

    let html = '';
    groupOrder.forEach(status => {
        const items = groups[status];
        if (!items || items.length === 0) return;
        html += `<div class="queue-group"><h4>${groupLabels[status]} (${items.length})</h4>`;
        items.forEach(t => {
            const filename = escapeHtml((t.video_path || '').split('/').pop() || '');
            const type = escapeHtml(t.type || '');
            const progress = (t.progress || 0).toFixed(1);
            const info = escapeHtml(t.progress_info || '');
            const taskId = escapeAttr(t.id);
            const createdAt = t.created_at || '';
            const error = t.error ? escapeHtml(t.error.substring(0, 80)) : '';

            html += `<div class="queue-item status-${status}">
                <div class="queue-item-header">
                    <span class="queue-item-type">${type}</span>
                    <span class="queue-item-name">${filename}</span>
                </div>
                <div class="queue-item-progress">
                    <div class="progress-bar"><div class="progress-fill" style="width:${progress}%"></div></div>
                    <span class="progress-text">${progress}%</span>
                </div>
                <div class="queue-item-info">${info}</div>
                ${error ? `<div class="queue-item-error">${error}</div>` : ''}
                <div class="queue-item-meta">${escapeHtml(formatQueueTime(createdAt))}</div>
                <div class="queue-item-actions">
                    ${status === 'pending' || status === 'processing'
                        ? `<button class="btn btn-secondary btn-tiny" data-queue-action="cancel" data-task-id="${taskId}">取消</button>`
                        : ''}
                    ${status === 'failed' || status === 'cancelled'
                        ? `<button class="btn btn-primary btn-tiny" data-queue-action="retry" data-task-id="${taskId}">重试</button>` : ''}
                    ${status === 'completed' || status === 'failed' || status === 'cancelled'
                        ? `<button class="btn btn-secondary btn-tiny" data-queue-action="delete" data-task-id="${taskId}">删除</button>` : ''}
                </div>
            </div>`;
        });
        html += '</div>';
    });

    container.innerHTML = html;
}

async function cancelQueueTask(taskId) {
    const ok = await uiConfirmAsync('取消任务', '确认取消任务 ' + taskId.substring(0, 50) + '...?', { okText: '取消任务' });
    if (!ok) return;
    try {
        const resp = await apiFetch('/api/queue/' + encodeURIComponent(taskId) + '/cancel', { method: 'POST' });
        const data = await resp.json();
        showToast(data.message || '操作完成', data.success ? 'success' : 'error');
        pollQueueList();
    } catch (e) {
        showToast('取消失败: ' + e.message, 'error');
    }
}

async function retryQueueTask(taskId) {
    try {
        const resp = await apiFetch('/api/queue/' + encodeURIComponent(taskId) + '/retry', { method: 'POST' });
        const data = await resp.json();
        showToast(data.message || '操作完成', data.success ? 'success' : 'error');
        pollQueueList();
    } catch (e) {
        showToast('重试失败: ' + e.message, 'error');
    }
}

async function deleteQueueTask(taskId) {
    const ok = await uiConfirmAsync('删除任务记录', '确认删除任务记录?', { okText: '删除', danger: true });
    if (!ok) return;
    try {
        const resp = await apiFetch('/api/queue/' + encodeURIComponent(taskId), { method: 'DELETE' });
        const data = await resp.json();
        showToast(data.message || '操作完成', data.success ? 'success' : 'error');
        pollQueueList();
    } catch (e) {
        showToast('删除失败: ' + e.message, 'error');
    }
}

async function generateCurrentFolder() {
    const options = getGenerationOptions();
    try {
        const resp = await apiFetch('/api/generate-folder', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                folder: nasCurrentDir,
                recursive: !!document.getElementById('nasRecursive')?.checked,
                skip_existing: true,
                ...options
            })
        });
        const data = await resp.json();
        if (data.success) {
            if (data.batch_id) {
                currentBatchId = data.batch_id;
                showBatchStatusSection();
                startBatchStatusCheck();
            }
            showToast(data.message || '目录任务已创建', 'success');
        } else {
            showToast(data.error || '目录任务创建失败', 'error');
        }
    } catch (e) {
        showToast('目录任务创建失败: ' + e.message, 'error');
    }
}

function notifyDone(title, body) {
    try {
        if (!('Notification' in window)) return;
        if (Notification.permission === 'granted') {
            new Notification(title, { body });
        } else if (Notification.permission === 'default') {
            Notification.requestPermission();
        }
    } catch (e) {}
}

function showTokenSettings() {
    const modal = document.getElementById('tokenModal');
    const input = document.getElementById('tokenInput');
    if (input) input.value = localStorage.getItem('authToken') || '';
    if (modal) modal.style.display = 'flex';
}

function closeTokenSettings() {
    document.getElementById('tokenModal').style.display = 'none';
}

async function saveTokenSettings() {
    const value = document.getElementById('tokenInput').value.trim();
    if (!value) { showToast('请输入 Token', 'error'); return; }
    try {
        const resp = await fetch('/api/auth/check', { headers: { 'Authorization': 'Bearer ' + value } });
        if (resp.status !== 200) { showToast('Token 无效，请核对后重试', 'error'); return; }
    } catch (e) { showToast('校验失败: ' + e.message, 'error'); return; }
    localStorage.setItem('authToken', value);
    AUTH_TOKEN = value;
    closeTokenSettings();
    showToast('Token 已保存，页面将刷新', 'success');
    setTimeout(() => window.location.reload(), 500);
}

function clearSavedToken() {
    localStorage.removeItem('authToken');
    closeTokenSettings();
    showToast('Token 已清除，页面将刷新', 'info');
    setTimeout(() => window.location.reload(), 500);
}

function showTools(skipLoad) {
    document.getElementById('toolsModal').style.display = 'flex';
    if (!skipLoad) loadModelsInfo();
}

function closeTools() {
    document.getElementById('toolsModal').style.display = 'none';
}

function writeToolsOutput(data) {
    document.getElementById('toolsOutput').textContent = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
}

async function runCleanup(target) {
    const resp = await apiFetch('/api/cleanup', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({target})
    });
    writeToolsOutput(await resp.json());
}

var _whisperPollTimer = null;

async function showWhisperModels() {
    var box = document.getElementById('whisperModelsBox');
    if (box) box.style.display = 'block';
    await refreshWhisperModels();
    if (_whisperPollTimer) { clearTimeout(_whisperPollTimer); _whisperPollTimer = null; }
    pollWhisperDownloads();
}

async function refreshWhisperModels() {
    try {
        var resp = await apiFetch('/api/models');
        var data = await resp.json();
        renderWhisperModels(data);
    } catch (e) {
        var box = document.getElementById('whisperModelsBox');
        if (box) box.innerHTML = '<div style="color:var(--danger,#e74c3c)">加载失败: ' + escapeHtml(e.message) + '</div>';
    }
}

function renderWhisperModels(data) {
    var box = document.getElementById('whisperModelsBox');
    if (!box) return;
    var models = data.whisper_models || [];
    var dls = data.whisper_downloads || {};
    var allowed = data.whisper_allowed || [];
    var sizeMap = {};
    models.forEach(function(m) { sizeMap[m.name] = m.size; });
    var labelMap = { tiny: 'Tiny (最快)', base: 'Base (平衡)', small: 'Small (较准)', medium: 'Medium (准确)',
                     turbo: 'Turbo (速度优先)', 'large-v3-turbo': 'Large V3 Turbo', 'large-v3': 'Large V3 (高精度)', large: 'Large (同 V3)' };
    var order = ['tiny', 'base', 'small', 'medium', 'turbo', 'large-v3-turbo', 'large-v3', 'large'];
    var html = '<div style="font-weight:600;margin-bottom:8px">Whisper 语音识别模型</div>';
    html += '<div style="font-size:0.82rem;color:var(--text-secondary,#98A2B3);margin-bottom:10px">预下载后离线可用；删除已缓存的模型可释放空间（正在使用的档位删掉后下次任务会自动重新下载）</div>';
    order.forEach(function(name) {
        if (allowed.indexOf(name) === -1) return;
        var size = sizeMap[name];
        var dl = dls[name] || {};
        var downloading = dl.status === 'downloading';
        var stateHtml = downloading
            ? '<span style="color:#3b82f6">下载中...</span>'
            : (dl.status === 'failed' ? '<span style="color:var(--danger,#e74c3c)" title="' + escapeAttr(dl.error || '') + '">上次下载失败</span>'
               : (size ? '<span style="color:#22c55e">已缓存 ' + formatFileSize(size) + '</span>' : '<span style="color:var(--text-secondary,#98A2B3)">未下载</span>'));
        var btn = downloading
            ? ''
            : (size
                ? '<button class="btn btn-ghost-danger btn-small" onclick="whisperDelete(\'' + name + '\')">删除</button>'
                : '<button class="btn btn-secondary btn-small" onclick="whisperDownload(\'' + name + '\')">下载</button>');
        html += '<div style="display:flex;align-items:center;gap:10px;padding:7px 10px;border:1px solid var(--border,#333);border-radius:8px;margin-bottom:6px">' +
                '<div style="flex:1"><strong>' + (labelMap[name] || name) + '</strong> <span style="font-size:0.78rem;color:var(--text-secondary,#98A2B3)">' + name + '</span></div>' +
                '<div style="font-size:0.82rem">' + stateHtml + '</div>' + btn + '</div>';
    });
    html += '<div style="font-size:0.78rem;color:var(--text-secondary,#98A2B3);margin-top:6px">首次使用某档位时也会自动按需下载（走 hf-mirror 镜像）</div>';
    box.innerHTML = html;
}

async function whisperDownload(name) {
    try {
        var resp = await apiFetch('/api/models/whisper/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: name })
        });
        var r = await resp.json();
        if (!r.success) { showToast(r.error || '下载启动失败', 'error'); return; }
        showToast('模型 ' + name + ' 下载已启动', 'info');
        await refreshWhisperModels();
        pollWhisperDownloads();
    } catch (e) { showToast('下载启动失败: ' + e.message, 'error'); }
}

async function whisperDelete(name) {
    var ok = await uiConfirmAsync('删除 Whisper 模型', '确认删除已缓存的 ' + name + ' 模型？正在使用的档位会在下次任务时自动重新下载。', { okText: '删除', danger: true });
    if (!ok) return;
    try {
        var resp = await apiFetch('/api/models/whisper/' + encodeURIComponent(name), { method: 'DELETE' });
        var r = await resp.json();
        if (r.success) { showToast('模型已删除', 'success'); await refreshWhisperModels(); }
        else showToast(r.error || '删除失败', 'error');
    } catch (e) { showToast('删除失败: ' + e.message, 'error'); }
}

function pollWhisperDownloads() {
    if (_whisperPollTimer) clearTimeout(_whisperPollTimer);
    _whisperPollTimer = setTimeout(async function() {
        try {
            var resp = await apiFetch('/api/models');
            var data = await resp.json();
            var dls = data.whisper_downloads || {};
            var any = Object.keys(dls).some(function(k) { return dls[k].status === 'downloading'; });
            var box = document.getElementById('whisperModelsBox');
            if (box && box.style.display !== 'none') renderWhisperModels(data);
            if (any) pollWhisperDownloads();
        } catch (e) {}
    }, 3000);
}

async function loadModelsInfo() {
    const resp = await apiFetch('/api/models');
    writeToolsOutput(await resp.json());
}

async function loadResourceProfile() {
    const resp = await apiFetch('/api/resource-profile');
    writeToolsOutput(await resp.json());
}

async function showGlossaryManager() {
    try {
        const resp = await apiFetch('/api/glossaries');
        const data = await resp.json();
        const existing = data.glossaries || {};
        const name = await uiPrompt('术语表', '术语表名称；留空只查看现有术语表', { defaultValue: '' });
        if (name === null) return;
        if (!name) {
            writeToolsOutput(existing);
            return;
        }
        const terms = await uiPrompt('术语表', '输入术语，每行 source=target', {
            defaultValue: 'Whisper=Whisper\nOpenAI=OpenAI',
            placeholder: 'source=target',
            validate: function(v) {
                var lines = v.split('\n').filter(function(l) { return l.trim(); });
                var bad = lines.filter(function(l) { return l.indexOf('=') === -1; });
                return bad.length ? '第 ' + (lines.indexOf(bad[0]) + 1) + ' 行缺少 "="，格式应为 source=target' : null;
            }
        });
        if (terms === null) return;
        const saveResp = await apiFetch('/api/glossaries', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name.trim(), terms: terms })
        });
        const result = await saveResp.json();
        writeToolsOutput(result);
        if (result.success) showToast('术语表已保存', 'success');
        else showToast('术语表保存失败: ' + result.error, 'error');
    } catch (e) {
        showToast('术语表操作失败: ' + e.message, 'error');
    }
}

async function showNotificationSettings() {
    try {
        const currentResp = await apiFetch('/api/notification-settings');
        const current = await currentResp.json();
        const existing = (current.settings || {}).webhook_url || '';
        const webhookUrl = await uiPrompt('通知设置', 'Webhook URL；留空则关闭通知', { defaultValue: existing, placeholder: 'https://...' });
        if (webhookUrl === null) {
            writeToolsOutput(current);
            return;
        }
        const resp = await apiFetch('/api/notification-settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: !!webhookUrl.trim(), webhook_url: webhookUrl.trim() })
        });
        const result = await resp.json();
        writeToolsOutput(result);
        showToast(result.message || '通知设置已更新', result.success ? 'success' : 'error');
    } catch (e) {
        showToast('通知设置失败: ' + e.message, 'error');
    }
}

async function loadQueueReport() {
    try {
        const resp = await apiFetch('/api/reports/queue?limit=200');
        const data = await resp.json();
        writeToolsOutput(data);
    } catch (e) {
        showToast('报告加载失败: ' + e.message, 'error');
    }
}

// ===== AI 翻译引擎（LLM，可选）=====
async function showLlmSettings() {
    try {
        const resp = await apiFetch('/api/llm-config');
        const data = await resp.json();
        const cfg = (data && data.config) || {};
        document.getElementById('llmEnabled').checked = !!cfg.enabled;
        document.getElementById('llmBaseUrl').value = cfg.base_url || '';
        document.getElementById('llmApiKey').value = '';
        document.getElementById('llmApiKey').placeholder = cfg.api_key_set
            ? ('已保存 ' + (cfg.api_key_masked || '***') + '，留空保持不变')
            : '留空 = 保留已保存的 Key';
        document.getElementById('llmModel').value = cfg.model || '';
        const box = document.getElementById('llmTestResult');
        box.style.display = 'none';
        document.getElementById('llmModal').style.display = 'flex';
    } catch (e) {
        showToast('读取配置失败: ' + e.message, 'error');
    }
}

function closeLlmSettings() {
    document.getElementById('llmModal').style.display = 'none';
}

async function saveLlmConfig() {
    try {
        const resp = await apiFetch('/api/llm-config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                enabled: document.getElementById('llmEnabled').checked,
                base_url: document.getElementById('llmBaseUrl').value.trim(),
                api_key: document.getElementById('llmApiKey').value.trim(),
                model: document.getElementById('llmModel').value.trim(),
            })
        });
        const data = await resp.json();
        showToast(data.message || (data.success ? '已保存' : (data.error || '保存失败')), data.success ? 'success' : 'error');
        if (data.success) showLlmSettings(); // 重新回填掩码
    } catch (e) {
        showToast('保存失败: ' + e.message, 'error');
    }
}

async function testLlmConfig() {
    const box = document.getElementById('llmTestResult');
    box.style.display = 'block';
    box.textContent = '测试中…';
    try {
        // 直接用表单当前值测试（后端支持未保存配置的临时测试）
        const resp = await apiFetch('/api/llm-test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                target: 'zh',
                base_url: document.getElementById('llmBaseUrl').value.trim(),
                api_key: document.getElementById('llmApiKey').value.trim(),
                model: document.getElementById('llmModel').value.trim(),
            })
        });
        const data = await resp.json();
        if (data.success) {
            box.textContent = '✅ ' + (data.message || '连接成功') + '，示例输出: ' + (data.output || '').slice(0, 60);
        } else {
            box.textContent = '❌ ' + (data.error || '连接失败');
        }
    } catch (e) {
        box.textContent = '❌ ' + e.message;
    }
}

// ===== AI 转写摘要 =====
let _lastSummaryFile = null;
async function summarizeSubtitle(filename, mode) {
    mode = mode || 'summary';
    const modal = document.getElementById('summaryModal');
    if (!modal) return;
    _lastSummaryFile = filename;
    _lastSummaryMode = mode;
    document.getElementById('summaryFileName').textContent = filename + (mode === 'minutes' ? '（会议纪要）' : '');
    document.getElementById('summaryContent').textContent = '';
    const status = document.getElementById('summaryStatus');
    const dl = document.getElementById('summaryDownloadBtn');
    dl.style.display = 'none';
    status.style.display = 'block';
    status.textContent = '生成中，约需十几秒…';
    modal.style.display = 'flex';
    try {
        const resp = await apiFetch('/api/summary', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: filename, mode: mode || 'summary' })
        });
        const data = await resp.json();
        if (data.success) {
            status.style.display = 'none';
            document.getElementById('summaryContent').textContent = data.summary || '';
            dl.style.display = 'inline-flex';
            showToast('AI 总结完成，已保存为 ' + data.saved_as, 'success');
        } else {
            status.textContent = '❌ ' + (data.error || '生成失败');
            if ((data.error || '').indexOf('LLM') !== -1) showLlmSettings();
        }
    } catch (e) {
        status.textContent = '❌ ' + e.message;
    }
}

function closeSummaryModal() {
    document.getElementById('summaryModal').style.display = 'none';
}

function downloadSummary() {
    if (_lastSummaryFile) {
        const stem = _lastSummaryFile.replace(/\.[^.]+$/, '');
        downloadSubtitle(stem + '.总结.md');
    }
}

// ===== 媒体库（已上传文件列表）=====
async function showMediaLibrary() {
    const modal = document.getElementById('mediaLibraryModal');
    const list = document.getElementById('mediaLibraryList');
    if (!modal || !list) return;
    modal.style.display = 'flex';
    list.innerHTML = '<div class="empty-state"><p>加载中...</p></div>';
    try {
        const resp = await apiFetch('/api/media-library');
        const data = await resp.json();
        const files = (data && data.files) || [];
        if (!files.length) {
            list.innerHTML = '<div class="empty-state"><div class="empty-icon">📁</div><p>还没有上传过媒体文件</p></div>';
            return;
        }
        list.innerHTML = files.map(f => {
            const attr = escapeAttr(f.filename);
            const badge = f.has_subtitle ? '<span class="status-good">已有字幕</span>' : '<span class="status-bad">无字幕</span>';
            return `
            <div class="subtitle-item">
                <div class="subtitle-main">
                    <div class="subtitle-info">
                        <div class="subtitle-name">${escapeHtml(f.filename)}</div>
                        <div class="subtitle-meta">
                            ${badge}
                            <span>${formatFileSize(f.size)}</span>
                            <span class="meta-dot"></span>
                            <span>${new Date(f.modified).toLocaleString()}</span>
                        </div>
                    </div>
                    <div class="subtitle-actions">
                        <button class="btn btn-primary btn-small" data-library-action="generate" data-filename="${attr}">生成字幕</button>
                        <button class="btn btn-ghost-danger btn-small" data-library-action="delete" data-filename="${attr}">删除</button>
                    </div>
                </div>
            </div>`;
        }).join('');
        list.querySelectorAll('[data-library-action]').forEach(button => {
            button.addEventListener('click', () => {
                const fn = button.dataset.libraryAction === 'generate' ? generateFromLibrary : deleteFromLibrary;
                fn(button.dataset.filename);
            });
        });
    } catch (e) {
        list.innerHTML = '<div class="empty-state"><p>加载失败: ' + escapeHtml(e.message) + '</p></div>';
    }
}

function closeMediaLibrary() {
    document.getElementById('mediaLibraryModal').style.display = 'none';
}

function generateFromLibrary(filename) {
    // 媒体库文件 = 上传目录媒体：置为当前选中文件并滚动到生成区
    uploadedFile = { name: filename, path: filename, isNasFile: false };
    closeMediaLibrary();
    if (generateBtn) {
        generateBtn.disabled = false;
        generateBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    showToast('已选择: ' + filename + '，点「生成字幕」开始', 'success');
}

async function deleteFromLibrary(filename) {
    showConfirm('删除媒体', '确定删除 ' + filename + ' 吗？此操作不可撤销。', async function() {
        try {
            const resp = await apiFetch('/api/delete/' + encodeURIComponent(filename), { method: 'DELETE' });
            const data = await resp.json();
            if (data.success) {
                showToast('已删除', 'success');
                showMediaLibrary();
            } else {
                showToast('删除失败: ' + (data.error || ''), 'error');
            }
        } catch (e) {
            showToast('删除失败: ' + e.message, 'error');
        }
    }, '删除', true);
}

// ============================================================================
// v1.2.0：字幕推送 / 说话人命名 / 监控自动字幕 / 云端 ASR / 双语对照质检
// ============================================================================

// ---- 字幕推送到媒体库 ----
var _pushFile = null;

function pushSubtitle(filename) {
    _pushFile = filename;
    var modal = document.getElementById('pushModal');
    if (!modal) return;
    document.getElementById('pushFileName').textContent = filename;
    var dirInput = document.getElementById('pushTargetDir');
    dirInput.value = localStorage.getItem('aisub_push_dir') || '';
    modal.style.display = 'flex';
}

function closePushModal() {
    var m = document.getElementById('pushModal');
    if (m) m.style.display = 'none';
}

async function doPushSubtitle() {
    var dir = document.getElementById('pushTargetDir').value.trim();
    var mode = document.getElementById('pushMode').value;
    if (!dir) { showToast('请填写目标目录', 'error'); return; }
    try {
        var resp = await apiFetch('/api/push-subtitle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: _pushFile, target_dir: dir, mode: mode })
        });
        var r = await resp.json();
        if (r.success) {
            localStorage.setItem('aisub_push_dir', dir);
            showToast('已推送到媒体库', 'success');
            closePushModal();
        } else {
            showToast('推送失败: ' + (r.error || ''), 'error');
        }
    } catch (e) { showToast('推送失败: ' + e.message, 'error'); }
}

// ---- 说话人命名 ----
var _speakerFile = null;

function speakerNaming(filename) {
    _speakerFile = filename;
    var modal = document.getElementById('speakerModal');
    if (!modal) return;
    document.getElementById('speakerFileName').textContent = filename;
    document.getElementById('speakerRows').innerHTML = '<div class="loading-spinner">加载中...</div>';
    modal.style.display = 'flex';
    apiFetch('/api/speaker-names/' + encodeURIComponent(filename))
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (!data.success) { document.getElementById('speakerRows').innerHTML = '<p>' + escapeHtml(data.error || '加载失败') + '</p>'; return; }
            var speakers = data.speakers || [];
            var saved = data.saved || {};
            if (!speakers.length) {
                document.getElementById('speakerRows').innerHTML = '<p style="color:var(--text-secondary,#98A2B3)">该字幕没有【说话人N】标记。请先用带说话人分离的设置重新生成字幕。</p>';
                return;
            }
            var html = '';
            speakers.forEach(function(spk) {
                var val = saved[spk] || '';
                html += '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">' +
                    '<span style="min-width:84px;font-weight:600">' + escapeHtml(spk) + '</span>' +
                    '<span>→</span>' +
                    '<input type="text" class="speaker-name-input" data-spk="' + escapeAttr(spk) + '" value="' + escapeAttr(val) + '" placeholder="真实姓名" style="flex:1">' +
                    '</div>';
            });
            document.getElementById('speakerRows').innerHTML = html;
        })
        .catch(function(e) {
            document.getElementById('speakerRows').innerHTML = '<p>' + escapeHtml(e.message) + '</p>';
        });
}

function closeSpeakerModal() {
    var m = document.getElementById('speakerModal');
    if (m) m.style.display = 'none';
}

async function doSpeakerNaming() {
    var inputs = document.querySelectorAll('#speakerRows .speaker-name-input');
    var mapping = {};
    inputs.forEach(function(inp) {
        var v = inp.value.trim();
        if (v) mapping[inp.getAttribute('data-spk')] = v;
    });
    if (!Object.keys(mapping).length) { showToast('请至少填写一个姓名', 'error'); return; }
    try {
        var resp = await apiFetch('/api/speaker-names/' + encodeURIComponent(_speakerFile), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mapping: mapping })
        });
        var r = await resp.json();
        if (r.success) {
            showToast('已替换 ' + r.replaced + ' 处', 'success');
            closeSpeakerModal();
            loadSubtitles();
        } else {
            showToast('失败: ' + (r.error || ''), 'error');
        }
    } catch (e) { showToast('失败: ' + e.message, 'error'); }
}

// ---- 监控自动字幕 ----
function showWatchSettings() {
    var modal = document.getElementById('watchModal');
    if (!modal) return;
    modal.style.display = 'flex';
    apiFetch('/api/watch-settings')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var c = data.config || {};
            document.getElementById('watchEnabled').checked = !!c.enabled;
            document.getElementById('watchDir').value = c.dir || '';
            document.getElementById('watchModel').value = c.model || 'small';
            document.getElementById('watchTranslate').value = c.translate_to || '';
            document.getElementById('watchBilingual').checked = !!c.bilingual;
            document.getElementById('watchDiarize').checked = !!c.diarize;
            document.getElementById('watchWindow').value = c.window || '';
            document.getElementById('watchStableAge').value = c.stable_age || 300;
            var st = c.state || {};
            var last = st.last_scan ? st.last_scan.replace('T', ' ').slice(0, 19) : '从未';
            var lastR = st.last_result || {};
            document.getElementById('watchState').textContent =
                '上次扫描: ' + last + ' · 扫描 ' + (lastR.scanned || 0) + ' 个 · 入队 ' + (lastR.enqueued || 0) + ' 个';
        })
        .catch(function(e) { showToast('读取监控设置失败: ' + e.message, 'error'); });
}

function closeWatchModal() {
    var m = document.getElementById('watchModal');
    if (m) m.style.display = 'none';
}

async function saveWatchSettings() {
    var payload = {
        enabled: document.getElementById('watchEnabled').checked,
        dir: document.getElementById('watchDir').value.trim(),
        model: document.getElementById('watchModel').value,
        translate_to: document.getElementById('watchTranslate').value || null,
        bilingual: document.getElementById('watchBilingual').checked,
        diarize: document.getElementById('watchDiarize').checked,
        window: document.getElementById('watchWindow').value.trim(),
        stable_age: parseInt(document.getElementById('watchStableAge').value, 10) || 300
    };
    try {
        var resp = await apiFetch('/api/watch-settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        var r = await resp.json();
        if (r.success) {
            showToast('监控设置已保存' + (payload.enabled ? '，扫描线程已启动' : ''), 'success');
            closeWatchModal();
        } else {
            showToast(r.error || '保存失败', 'error');
        }
    } catch (e) { showToast('保存失败: ' + e.message, 'error'); }
}

async function watchScanNow() {
    try {
        var resp = await apiFetch('/api/watch-scan', { method: 'POST' });
        var r = await resp.json();
        if (r.success) {
            var res = r.result || {};
            showToast('扫描完成: ' + (res.scanned || 0) + ' 个文件, 入队 ' + (res.enqueued || 0), 'info');
            showWatchSettings();
        } else {
            showToast(r.error || '扫描失败', 'error');
        }
    } catch (e) { showToast('扫描失败: ' + e.message, 'error'); }
}

// ---- 云端 ASR ----
function showAsrSettings() {
    var modal = document.getElementById('asrModal');
    if (!modal) return;
    modal.style.display = 'flex';
    apiFetch('/api/asr-config')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var c = data.config || {};
            document.getElementById('asrEnabled').checked = !!c.enabled;
            document.getElementById('asrBaseUrl').value = c.base_url || '';
            document.getElementById('asrModel').value = c.model || '';
            document.getElementById('asrApiKey').value = '';
            document.getElementById('asrApiKey').placeholder = c.api_key_masked || 'sk-...';
        })
        .catch(function(e) { showToast('读取云端 ASR 配置失败: ' + e.message, 'error'); });
}

function closeAsrModal() {
    var m = document.getElementById('asrModal');
    if (m) m.style.display = 'none';
}

async function saveAsrSettings() {
    var payload = {
        enabled: document.getElementById('asrEnabled').checked,
        base_url: document.getElementById('asrBaseUrl').value.trim(),
        model: document.getElementById('asrModel').value.trim(),
        api_key: document.getElementById('asrApiKey').value.trim()
    };
    try {
        var resp = await apiFetch('/api/asr-config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        var r = await resp.json();
        if (r.success) {
            showToast('云端 ASR 配置已保存', 'success');
            closeAsrModal();
        } else {
            showToast(r.error || '保存失败', 'error');
        }
    } catch (e) { showToast('保存失败: ' + e.message, 'error'); }
}

// ---- 片段导出（v1.3.0）----
function clipExport(mediaFilename) {
    var modal = document.getElementById('clipModal');
    if (!modal) return;
    document.getElementById('clipFileName').textContent = mediaFilename;
    document.getElementById('clipStart').value = 0;
    document.getElementById('clipEnd').value = '';
    document.getElementById('clipAccurate').checked = false;
    modal.dataset.filename = mediaFilename;
    modal.style.display = 'flex';
}

function closeClipModal() {
    var m = document.getElementById('clipModal');
    if (m) m.style.display = 'none';
}

async function doClipExport() {
    var modal = document.getElementById('clipModal');
    var fn = modal.dataset.filename;
    var start = parseFloat(document.getElementById('clipStart').value) || 0;
    var endRaw = document.getElementById('clipEnd').value.trim();
    var accurate = document.getElementById('clipAccurate').checked;
    var body = { filename: fn, start: start, accurate: accurate };
    if (endRaw !== '') {
        var end = parseFloat(endRaw);
        if (isNaN(end) || end <= start) { showToast('结束时间必须大于开始时间', 'error'); return; }
        body.end = end;
    }
    try {
        var resp = await apiFetch('/api/clip-export', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        var r = await resp.json();
        if (r.success) {
            showToast('片段导出已启动', 'info');
            closeClipModal();
            var poll = setInterval(async function() {
                try {
                    var sr = await (await apiFetch('/api/status/' + r.task_id)).json();
                    var t = sr.task || sr;
                    if (t.status === 'completed') {
                        clearInterval(poll);
                        showToast('片段导出完成: ' + (t.result || {}).output_file, 'success');
                        loadSubtitles();
                    } else if (t.status === 'failed') {
                        clearInterval(poll);
                        showToast('片段导出失败: ' + (t.error || ''), 'error');
                    }
                } catch (e) {}
            }, 3000);
        } else {
            showToast(r.error || '启动失败', 'error');
        }
    } catch (e) { showToast('启动失败: ' + e.message, 'error'); }
}

// ---- 批量烧录（v1.3.0）----
function showBurnBatch() {
    var modal = document.getElementById('burnBatchModal');
    if (!modal) return;
    apiFetch('/api/subtitles')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var subs = (data.subtitles || []).filter(function(x) {
                return x.kind === 'subtitle' && ['.srt', '.ass', '.vtt'].indexOf(x.filename.toLowerCase().split('.').pop().toLowerCase().replace(/\.$/, '')) !== -1 ||
                       x.kind === 'subtitle' && /\.(srt|ass|vtt)$/i.test(x.filename);
            });
            subs = subs.filter(function(x) { return /\.(srt|ass|vtt)$/i.test(x.filename); });
            if (!subs.length) {
                document.getElementById('burnBatchList').innerHTML = '<p style="color:var(--text-secondary,#98A2B3)">没有可烧录的字幕文件</p>';
                return;
            }
            var html = '';
            subs.forEach(function(x) {
                html += '<label style="display:flex;align-items:center;gap:8px;padding:5px 4px">' +
                    '<input type="checkbox" class="burnbatch-chk" value="' + escapeAttr(x.filename) + '" checked>' +
                    '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escapeHtml(x.filename) + '</span></label>';
            });
            document.getElementById('burnBatchList').innerHTML = html;
        })
        .catch(function(e) { showToast('加载字幕列表失败: ' + e.message, 'error'); });
    modal.style.display = 'flex';
}

function closeBurnBatchModal() {
    var m = document.getElementById('burnBatchModal');
    if (m) m.style.display = 'none';
}

async function doBurnBatch() {
    var files = [...document.querySelectorAll('#burnBatchList .burnbatch-chk:checked')].map(function(c) { return c.value; });
    if (!files.length) { showToast('请至少勾选一份字幕', 'error'); return; }
    try {
        var resp = await apiFetch('/api/burn-batch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filenames: files, style: {} })
        });
        var r = await resp.json();
        if (r.success) {
            showToast('批量烧录已启动（' + files.length + ' 份）', 'info');
            closeBurnBatchModal();
            var tid = r.task_id;
            var poll = setInterval(async function() {
                try {
                    var sr = await (await apiFetch('/api/status/' + tid)).json();
                    var t = sr.task || sr;
                    if (t.status === 'completed') {
                        clearInterval(poll);
                        var res = t.result || {};
                        showToast('批量烧录完成: 成功 ' + (res.ok_count || 0) + ' 失败 ' + (res.fail_count || 0), res.fail_count ? 'error' : 'success');
                        loadSubtitles();
                    } else if (t.status === 'failed') {
                        clearInterval(poll);
                        showToast('批量烧录失败: ' + (t.error || ''), 'error');
                    }
                } catch (e) {}
            }, 5000);
        } else {
            showToast(r.error || '启动失败', 'error');
        }
    } catch (e) { showToast('启动失败: ' + e.message, 'error'); }
}

// ---- 统计面板（v1.3.0）----
async function showStatsPanel() {
    try {
        var resp = await apiFetch('/api/stats');
        var data = await resp.json();
        if (!data.success) { showToast(data.error || '加载失败', 'error'); return; }
        var st = data.stats;
        var gb = function(n) { return (n / 1073741824).toFixed(2) + ' GB'; };
        var lines = [];
        lines.push('=== 字幕库 ===');
        lines.push('字幕文件: ' + st.subtitle_count + ' · 成片: ' + st.video_count + ' · 文档: ' + (st.doc_count || 0));
        lines.push('格式分布: ' + Object.keys(st.format_count || {}).map(function(k) { return k + '×' + st.format_count[k]; }).join(', '));
        lines.push('字幕目录合计: ' + gb(st.total_size || 0) + '（成片 ' + gb(st.video_size || 0) + '）');
        if (st.tasks) {
            lines.push('');
            lines.push('=== 任务 ===');
            lines.push('完成 ' + (st.tasks.completed || 0) + ' · 总计 ' + (st.tasks.total || 0));
        }
        if (st.storage) {
            lines.push('');
            lines.push('=== 存储占用 ===');
            Object.keys(st.storage).forEach(function(k) { lines.push(k + ': ' + gb(st.storage[k] || 0)); });
        }
        writeToolsOutput(lines.join('\n'));
        showToast('统计已生成', 'info');
    } catch (e) { showToast('统计失败: ' + e.message, 'error'); }
}

// ---- 双语对照质检 ----
var _qcFile = null;
var _qcCues = [];

function parseSrtCues(text) {
    var cues = [];
    var blocks = text.replace(/\r/g, '').split(/\n\n+/);
    blocks.forEach(function(b) {
        var lines = b.split('\n').filter(function(l) { return l.trim() !== ''; });
        if (lines.length < 2) return;
        var idx = 0;
        if (/^\d+$/.test(lines[0].trim())) idx = 1;
        var tm = lines[idx] || '';
        var mm = tm.match(/(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})/);
        if (!mm) return;
        var toSec = function(h, m, s, ms) { return (+h) * 3600 + (+m) * 60 + (+s) + (+ms) / 1000; };
        var start = toSec(mm[1], mm[2], mm[3], mm[4]);
        var end = toSec(mm[5], mm[6], mm[7], mm[8]);
        var textLines = lines.slice(idx + 1).filter(function(l) { return l.trim() !== ''; });
        cues.push({ start: start, end: end, lines: textLines });
    });
    return cues;
}

function isMostlyCJK(s) {
    var cjk = (s.match(/[\u4e00-\u9fff]/g) || []).length;
    var latin = (s.match(/[A-Za-z]/g) || []).length;
    return cjk > latin;
}

async function qcCompare(filename) {
    _qcFile = filename;
    _qcCues = [];
    var modal = document.getElementById('qcCompareModal');
    if (!modal) return;
    document.getElementById('qcCompareFile').textContent = filename;
    document.getElementById('qcCompareBody').innerHTML = '<div class="loading-spinner">加载与质检中...</div>';
    modal.style.display = 'flex';
    try {
        var resp = await apiFetch('/api/download/' + encodeURIComponent(filename));
        var text = await resp.text();
        var cues = parseSrtCues(text);
        // 仅对双语（既有中文又有英文行的文件）启用对照视图
        var hasZh = cues.some(function(c) { return c.lines.some(isMostlyCJK); });
        var hasEn = cues.some(function(c) { return c.lines.some(function(l) { return !isMostlyCJK(l) && /[A-Za-z]/.test(l); }); });
        if (!hasZh || !hasEn) {
            document.getElementById('qcCompareBody').innerHTML = '<p style="color:var(--text-secondary,#98A2B3)">该字幕不是双语文件（需要中英混排），请直接用编辑器。</p>';
            return;
        }
        var qResp = await apiFetch('/api/subtitle-quality/' + encodeURIComponent(filename));
        var qData = await qResp.json();
        var issuesByIndex = {};
        ((qData.report || {}).issues || []).forEach(function(it) {
            var k = it.index;
            (issuesByIndex[k] = issuesByIndex[k] || []).push(it);
        });
        _qcCues = cues.map(function(c, i) {
            var zh = '', en = [];
            c.lines.forEach(function(l) {
                if (isMostlyCJK(l)) zh = (zh ? zh + ' ' : '') + l;
                else en.push(l);
            });
            return { start: c.start, end: c.end, en: en.join(' '), zh: zh, issues: issuesByIndex[i] || [], idx: i };
        });
        renderQcCompare();
    } catch (e) {
        document.getElementById('qcCompareBody').innerHTML = '<p>' + escapeHtml(e.message) + '</p>';
    }
}

function renderQcCompare() {
    var html = '<table style="width:100%;border-collapse:collapse;font-size:0.88rem">' +
        '<tr style="position:sticky;top:0;background:var(--bg-secondary,#1A1614)">' +
        '<th style="text-align:left;padding:6px;width:110px">时间</th>' +
        '<th style="text-align:left;padding:6px">原文 (EN)</th>' +
        '<th style="text-align:left;padding:6px">译文 (ZH，可编辑)</th>' +
        '<th style="text-align:left;padding:6px;width:180px">质检提示</th></tr>';
    _qcCues.forEach(function(c) {
        var warn = '';
        (c.issues || []).forEach(function(it) {
            warn += '<div style="color:' + (it.severity === 'error' ? 'var(--danger,#e74c3c)' : '#e6a23c') + '">' +
                escapeHtml(it.message || it.type || '') + '</div>';
        });
        html += '<tr style="border-top:1px solid var(--border,#333)">' +
            '<td style="padding:6px;vertical-align:top;color:var(--text-secondary,#98A2B3)">' +
            fmtQcTime(c.start) + '<br>' + fmtQcTime(c.end) + '</td>' +
            '<td style="padding:6px;vertical-align:top">' + escapeHtml(c.en) + '</td>' +
            '<td style="padding:6px;vertical-align:top"><textarea data-qc-idx="' + c.idx + '" rows="2" style="width:100%">' + escapeHtml(c.zh) + '</textarea></td>' +
            '<td style="padding:6px;vertical-align:top">' + (warn || '<span style="color:#22c55e">✓</span>') + '</td>' +
            '</tr>';
    });
    html += '</table>';
    document.getElementById('qcCompareBody').innerHTML = html;
    var issueCount = _qcCues.filter(function(c) { return (c.issues || []).length; }).length;
    document.getElementById('qcCompareStats').textContent = '共 ' + _qcCues.length + ' 条 · ' + issueCount + ' 条有质检提示';
}

function fmtQcTime(sec) {
    var m = Math.floor(sec / 60), s = Math.floor(sec % 60);
    return m + ':' + (s < 10 ? '0' : '') + s;
}

function closeQcCompare() {
    var m = document.getElementById('qcCompareModal');
    if (m) m.style.display = 'none';
}

async function saveQcCompare() {
    try {
        document.querySelectorAll('#qcCompareBody textarea[data-qc-idx]').forEach(function(ta) {
            var idx = parseInt(ta.getAttribute('data-qc-idx'), 10);
            if (_qcCues[idx]) _qcCues[idx].zh = ta.value;
        });
        // 重组字幕：原文行 + 中文行
        var segments = _qcCues.map(function(c) {
            return { start: c.start, end: c.end, text: (c.en ? c.en + '\n' : '') + c.zh };
        });
        var resp = await apiFetch('/api/save-subtitle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: _qcFile, segments: segments, format: 'srt' })
        });
        var r = await resp.json();
        if (r.success) {
            showToast('已保存 ' + segments.length + ' 条', 'success');
            closeQcCompare();
            loadSubtitles();
        } else {
            showToast('保存失败: ' + (r.error || ''), 'error');
        }
    } catch (e) { showToast('保存失败: ' + e.message, 'error'); }
}
