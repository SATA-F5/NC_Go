console.log('[napcat] bridge.js 开始加载');

const $ = (id) => document.getElementById(id);

const els = {
    subtitle: $('subtitle'),
    powerLabel: $('power-label'),
    running: $('s-running'),
    qq: $('s-qq'),
    qqLogin: $('s-qq-login'),
    port: $('s-port'),
    webuiToken: $('s-webui-token'),
    botId: $('s-bot-id'),
    botPort: $('s-bot-port'),
    botToken: $('s-bot-token'),
    lastTime: $('s-last-time'),
    astrbotPath: $('s-astrbot-path'),
    napcatDir: $('s-napcat-dir'),
    guide: $('guide'),
    modal: $('config-modal'),
    toast: $('toast'),
    logContainer: $('log-container'),
    logCount: $('log-count'),
    webuiDetails: $('webui-details'),
    webuiSummary: $('btn-open-webui-summary'),
};

let bridge = null;
let statusTimer = null;
let logTimer = null;
let logOffset = 0;
let statusFetching = false;
let logFetching = false;
let isWindows = false;
let lastStatus = null;

const STATUS_INTERVAL = 3000;
const LOG_INTERVAL = 3000;
const MAX_LOG_LINES = 1000;
const LOCAL_HOSTS = ['127.0.0.1', 'localhost', '0.0.0.0', '[::]', '::'];

function escapeHtml(s) {
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function toast(message, duration = 2500, kind = '') {
    if (!els.toast) return;
    els.toast.textContent = message;
    els.toast.className = 'toast' + (kind ? ' ' + kind : '');
    els.toast.classList.add('visible');
    setTimeout(() => els.toast.classList.remove('visible'), duration);
}

// ============================================================
// 把 URL 里的 127.0.0.1/localhost 替换为当前访问 AstrBot 的 hostname
// ============================================================
function rewriteUrlForCurrentHost(url) {
    if (!url) return url;
    const currentHost = window.location.hostname;
    if (!currentHost) return url;

    try {
        const u = new URL(url);
        if (LOCAL_HOSTS.includes(u.hostname)) {
            u.hostname = currentHost;
        }
        return u.toString();
    } catch (e) {
        return String(url).replace(
            /127\.0\.0\.1|localhost|0\.0\.0\.0|\[::\]/g,
            currentHost
        );
    }
}

// ============================================================
// 通用的"打开 URL"提示框
// ============================================================
function showUrlDialog(title, url, hint) {
    // 移除旧的
    const old = document.getElementById('napcat-url-dialog');
    if (old) old.remove();

    const dialog = document.createElement('div');
    dialog.id = 'napcat-url-dialog';
    dialog.style.cssText = `
        position: fixed; inset: 0;
        background: rgba(0,0,0,0.65);
        display: flex; justify-content: center; align-items: center;
        z-index: 3000; padding: 20px;
    `;

    const box = document.createElement('div');
    box.style.cssText = `
        background: #313244; border-radius: 12px; padding: 22px;
        width: 520px; max-width: 100%;
        box-shadow: 0 12px 40px rgba(0,0,0,0.6);
        color: #cdd6f4;
    `;

    const titleEl = document.createElement('div');
    titleEl.textContent = title || '打开 WebUI';
    titleEl.style.cssText = 'font-size: 15px; font-weight: 600; margin-bottom: 14px;';

    const hintEl = document.createElement('div');
    hintEl.textContent = hint || '如果浏览器没有自动打开新标签页，请复制下面的地址，粘贴到你自己的浏览器里打开。';
    hintEl.style.cssText = 'font-size: 12px; color: #a6adc8; line-height: 1.6; margin-bottom: 12px;';

    const urlBox = document.createElement('textarea');
    urlBox.value = url;
    urlBox.readOnly = true;
    urlBox.style.cssText = `
        width: 100%; min-height: 72px;
        background: #1e1e2e; color: #f9e2af;
        border: 1px solid #45475a; border-radius: 8px;
        padding: 10px 12px; font-family: Consolas, monospace;
        font-size: 12px; line-height: 1.5;
        resize: vertical; outline: none;
    `;

    const btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex; gap:8px; justify-content:flex-end; margin-top:16px;';

    const btnCopy = document.createElement('button');
    btnCopy.textContent = '复制地址';
    btnCopy.className = 'btn btn-primary';
    btnCopy.onclick = async () => {
        try {
            await navigator.clipboard.writeText(url);
            toast('已复制到剪贴板', 1500, 'ok');
        } catch (e) {
            // 老浏览器降级
            urlBox.select();
            document.execCommand('copy');
            toast('已复制（请手动 Ctrl+C）', 2000, 'ok');
        }
    };

    const btnRetry = document.createElement('button');
    btnRetry.textContent = '再次尝试打开';
    btnRetry.className = 'btn';
    btnRetry.onclick = () => {
        tryOpenUrl(url);
    };

    const btnClose = document.createElement('button');
    btnClose.textContent = '关闭';
    btnClose.className = 'btn';
    btnClose.onclick = () => dialog.remove();

    btnRow.appendChild(btnRetry);
    btnRow.appendChild(btnCopy);
    btnRow.appendChild(btnClose);

    box.appendChild(titleEl);
    box.appendChild(hintEl);
    box.appendChild(urlBox);
    box.appendChild(btnRow);
    dialog.appendChild(box);

    dialog.addEventListener('click', (e) => {
        if (e.target === dialog) dialog.remove();
    });

    document.body.appendChild(dialog);
    urlBox.focus();
    urlBox.select();
}

// ============================================================
// 尝试用多种方式打开 URL（不涉及后端）
// ============================================================
function tryOpenUrl(url) {
    let opened = false;

    // 方式 1: <a target="_blank"> 点击
    try {
        const a = document.createElement('a');
        a.href = url;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        setTimeout(() => {
            try { document.body.removeChild(a); } catch (e) {}
        }, 100);
        opened = true;
    } catch (e) {
        console.warn('[napcat] a.click 失败:', e);
    }

    // 方式 2: window.open
    if (!opened) {
        try {
            const w = window.open(url, '_blank');
            if (w && !w.closed) opened = true;
        } catch (e) {
            console.warn('[napcat] window.open 失败:', e);
        }
    }

    // 方式 3: 从顶层窗口打开（同源时可行）
    if (!opened) {
        try {
            if (window.top && window.top !== window) {
                window.top.open(url, '_blank');
                opened = true;
            }
        } catch (e) {
            console.warn('[napcat] window.top.open 失败:', e);
        }
    }

    return opened;
}

async function initBridge() {
    if (!window.AstrBotPluginPage) {
        console.error('[napcat] AstrBotPluginPage 未注入');
        toast('Bridge 未就绪，请刷新页面', 3000, 'err');
        return;
    }
    bridge = window.AstrBotPluginPage;
    try {
        await bridge.ready();
    } catch (e) {
        console.error('[napcat] bridge.ready() 失败:', e);
        return;
    }
    await refreshStatus();
    await refreshLogs();
    statusTimer = setInterval(refreshStatus, STATUS_INTERVAL);
    logTimer = setInterval(refreshLogs, LOG_INTERVAL);
    console.log('[napcat] 轮询已启动');
}

async function refreshStatus() {
    if (!bridge || statusFetching) return;
    statusFetching = true;
    try {
        const data = await bridge.apiGet('status');
        lastStatus = data;
        updateStatus(data);
    } catch (e) {
        console.error('[napcat] 拉取状态失败:', e);
    } finally {
        statusFetching = false;
    }
}

function updateStatus(d) {
    if (!d) return;

    isWindows = !!d.is_windows;

    document.querySelectorAll('.win-only').forEach(el => {
        el.style.display = isWindows ? '' : 'none';
    });

    if (els.subtitle) {
        els.subtitle.textContent = isWindows
            ? 'Windows：启动/停止 NapCat，同步 AstrBot 机器人配置'
            : 'Linux/macOS：从 AstrBot 读取配置，写入 NapCat 的 onebot11_*.json';
    }

    if (isWindows) {
        setText(els.running, d.running ? '运行中' : '未运行', d.running ? 'ok' : 'warn');
        setText(els.qq, d.qq_installed ? '已安装' : '未安装', d.qq_installed ? 'ok' : 'err');

        if (d.qq_logged_in) {
            const name = d.qq_nickname || (d.qq_user_id ? `QQ ${d.qq_user_id}` : '已登录');
            setText(els.qqLogin, name, 'ok');
        } else {
            setText(els.qqLogin, '未登录', 'warn');
        }

        setText(els.port, d.napcat_port ?? '-');

        const tk = d.napcat_token || '';
        setText(els.webuiToken, tk || '无', tk ? 'ok' : '');
    } else {
        setText(els.webuiToken, '-', '');
    }

    const powerBtn = $('btn-toggle-power');
    if (powerBtn) {
        if (d.running) {
            if (els.powerLabel) els.powerLabel.textContent = '停止';
            powerBtn.classList.remove('power-on');
            powerBtn.classList.add('power-off');
        } else {
            if (els.powerLabel) els.powerLabel.textContent = '启动';
            powerBtn.classList.remove('power-off');
            powerBtn.classList.add('power-on');
        }
    }

    const webuiReady = !!d.napcat_webui_url;
    if (els.webuiSummary) {
        if (webuiReady) {
            els.webuiSummary.disabled = false;
            els.webuiSummary.style.opacity = '1';
            els.webuiSummary.style.cursor = 'pointer';
        } else {
            els.webuiSummary.disabled = true;
            els.webuiSummary.style.opacity = '0.4';
            els.webuiSummary.style.cursor = 'not-allowed';
            if (els.webuiDetails) els.webuiDetails.open = false;
        }
    }

    if (d.astrbot_bot_found) {
        setText(els.botId, d.astrbot_bot_id || '（无 ID）', 'ok');
        setText(els.botPort, d.astrbot_bot_port ?? '-', 'ok');
        const btk = d.astrbot_bot_token || '';
        setText(els.botToken, btk ? btk.substring(0, 4) + '...' + (btk.length > 8 ? btk.slice(-4) : '') : '（无）',
                btk ? 'ok' : 'warn');
    } else {
        setText(els.botId, '未找到', 'err');
        setText(els.botPort, '-', '');
        setText(els.botToken, '-', '');
    }

    setText(els.lastTime, d.last_sync_time || '未同步',
            d.last_sync_ok ? 'ok' : (d.last_sync_time ? 'err' : ''));

    setText(els.astrbotPath, d.astrbot_config_path || '未找到', d.astrbot_config_path ? 'ok' : 'err');
    setText(els.napcatDir, d.napcat_config_dir || '未找到', d.napcat_config_dir ? 'ok' : 'err');

    const cfg = d.config || {};
    if ($('cfg-napcat-dir')) $('cfg-napcat-dir').value = cfg.napcat_config_dir || '';
    if ($('cfg-astrbot-path')) $('cfg-astrbot-path').value = cfg.astrbot_config_path || '';
    if ($('cfg-qq-number')) $('cfg-qq-number').value = cfg.qq_number || '';
    if ($('cfg-mirror')) $('cfg-mirror').value = cfg.napcat_download_mirror || '';

    updateGuide(d);
}

function setText(el, text, className = '') {
    if (!el) return;
    el.textContent = String(text);
    el.className = 'value' + (className ? ' ' + className : '');
}

function updateGuide(d) {
    const g = els.guide;
    if (!g) return;

    if (d.astrbot_bot_found && d.napcat_config_file && d.last_sync_ok) {
        g.className = 'guide success visible';
        g.innerHTML = `
            <div style="font-weight:600;margin-bottom:4px;">配置已同步</div>
            <div>反向 WS：<code>ws://${escapeHtml(d.astrbot_bot_host)}:${escapeHtml(d.astrbot_bot_port)}/ws/</code></div>
        `;
        return;
    }

    if (!d.astrbot_bot_found) {
        g.className = 'guide visible';
        g.innerHTML = `
            <div style="font-weight:600;margin-bottom:4px;">先在 AstrBot 创建 OneBot v11 机器人</div>
            <div>AstrBot 左侧栏 -&gt; 机器人 -&gt; 创建机器人 -&gt; OneBot v11，端口填未被占用的（如 6199），保存后回到此处点「立即同步」。</div>
        `;
        return;
    }

    if (!d.napcat_config_dir) {
        g.className = 'guide warn visible';
        g.innerHTML = `
            <div style="font-weight:600;margin-bottom:4px;">未找到 NapCat 配置目录</div>
            <div>点右上角「配置」，手动填写 NapCat 的 config 目录路径。</div>
        `;
        return;
    }

    if (d.napcat_config_dir && !d.last_sync_ok) {
        g.className = 'guide error visible';
        g.innerHTML = `
            <div style="font-weight:600;margin-bottom:4px;">同步失败</div>
            <div>${escapeHtml(d.last_sync_msg || '未知错误')}</div>
        `;
        return;
    }

    g.className = 'guide';
}

async function refreshLogs() {
    if (!bridge || logFetching || !els.logContainer) return;
    logFetching = true;
    try {
        const data = await bridge.apiGet('logs', { since: logOffset });
        if (data.lines && data.lines.length > 0) {
            const newText = data.lines.join('\n');
            els.logContainer.textContent += (els.logContainer.textContent ? '\n' : '') + newText;
            const lines = els.logContainer.textContent.split('\n');
            if (lines.length > MAX_LOG_LINES) {
                els.logContainer.textContent = lines.slice(-MAX_LOG_LINES).join('\n');
            }
            logOffset = data.total;
            els.logContainer.scrollTop = els.logContainer.scrollHeight;
        }
        if (els.logCount) {
            els.logCount.textContent = `${data.total || 0} 行`;
        }
    } catch (e) {
        console.error('[napcat] 拉取日志失败:', e);
    } finally {
        logFetching = false;
    }
}

function clearLog() {
    if (els.logContainer) els.logContainer.textContent = '';
    logOffset = 0;
    toast('日志已清屏');
}

async function postAction(endpoint, successMsg = '') {
    if (!bridge) {
        toast('Bridge 未就绪', 3000, 'err');
        return null;
    }
    try {
        const r = await bridge.apiPost(endpoint);
        if (successMsg) toast(successMsg, 2000, 'ok');
        await refreshStatus();
        return r;
    } catch (e) {
        console.error(`[napcat] ${endpoint} 失败:`, e);
        toast(`操作失败: ${e.message || e}`, 3000, 'err');
        return null;
    }
}

async function togglePower() {
    if (!bridge) return;
    try {
        const d = await bridge.apiGet('status');
        const ep = d.running ? 'stop' : 'start';
        await postAction(ep, d.running ? '停止指令已发送' : '启动指令已发送');
    } catch (e) {
        toast(`操作失败: ${e.message || e}`, 3000, 'err');
    }
}

async function doSync() {
    const r = await postAction('sync');
    if (r) {
        if (r.ok) toast('同步成功', 2000, 'ok');
        else toast(`同步失败: ${r.message || ''}`, 4000, 'err');
    }
}

// ============================================================
// 打开 WebUI - 在当前访问者的浏览器
// ============================================================
// 逻辑：
//   1. 重写 URL（127.0.0.1 -> 当前 hostname）
//   2. 尝试用 <a>、window.open、window.top.open 打开新标签页
//   3. 不管成功与否，都弹出对话框显示 URL + 复制按钮
//      —— 这样即使 iframe 沙箱阻止了弹窗，用户也能手动复制
async function openWebUINewPage() {
    closeWebUIDetails();
    if (!bridge) return;
    try {
        const d = lastStatus || await bridge.apiGet('status');
        const rawUrl = d.napcat_webui_url;
        if (!rawUrl) {
            toast('WebUI 地址尚未获取，请先启动 NapCat', 3000, 'err');
            return;
        }

        const url = rewriteUrlForCurrentHost(rawUrl);
        console.log('[napcat] WebUI URL (rewritten):', url);

        const opened = tryOpenUrl(url);

        // 无论是否打开，都弹框显示 URL 供复制
        showUrlDialog(
            '打开 NapCat WebUI',
            url,
            opened
                ? '已尝试在新标签页打开。如果没有自动弹出，请复制下面的地址粘贴到浏览器打开。'
                : '当前环境阻止了自动弹出新窗口，请复制下面的地址，粘贴到你自己的浏览器里打开。'
        );
    } catch (e) {
        console.error('[napcat] 打开 WebUI 失败:', e);
        toast(`打开失败: ${e.message || e}`, 3000, 'err');
    }
}

// ============================================================
// 打开 WebUI - 在 AstrBot 服务器所在机器的浏览器
// ============================================================
async function openWebUIBrowser() {
    closeWebUIDetails();
    if (!bridge) return;
    try {
        const d = lastStatus || await bridge.apiGet('status');
        if (!d.napcat_webui_url) {
            toast('WebUI 地址尚未获取，请先启动 NapCat', 3000, 'err');
            return;
        }
        await bridge.apiPost('open-webui');
        toast('已调用系统浏览器打开（AstrBot 所在机器）', 2000, 'ok');
    } catch (e) {
        console.error('[napcat] 打开 WebUI 失败:', e);
        toast(`打开失败: ${e.message || e}`, 3000, 'err');
    }
}

function closeWebUIDetails() {
    if (els.webuiDetails) els.webuiDetails.open = false;
}

function openConfig() {
    if (els.modal) els.modal.classList.add('visible');
    refreshStatus();
}

function closeConfig() {
    if (els.modal) els.modal.classList.remove('visible');
}

async function saveConfig() {
    if (!bridge) return;
    const napcatDir = $('cfg-napcat-dir').value.trim();
    const astrbotPath = $('cfg-astrbot-path').value.trim();
    const qqNumber = $('cfg-qq-number').value.trim();
    const mirror = $('cfg-mirror')?.value.trim() || '';

    try {
        const r = await bridge.apiPost('config/save', {
            napcat_config_dir: napcatDir,
            astrbot_config_path: astrbotPath,
            qq_number: qqNumber,
            napcat_download_mirror: mirror,
        });
        closeConfig();
        await refreshStatus();
        if (r && r.sync_ok) toast('配置已保存，同步成功', 2500, 'ok');
        else if (r && r.sync_msg) toast(`已保存，同步失败: ${r.sync_msg}`, 4000, 'err');
        else toast('配置已保存', 2000, 'ok');
    } catch (e) {
        toast(`保存失败: ${e.message || e}`, 3000, 'err');
    }
}

async function scanCandidates() {
    if (!bridge) return;
    const resultEl = $('scan-results');
    if (resultEl) resultEl.innerHTML = '<div style="font-size:10px;color:#6c7086;">正在扫描...</div>';
    try {
        const resp = await bridge.apiPost('scan');
        const candidates = (resp && resp.candidates) || [];
        if (!resultEl) return;
        if (candidates.length === 0) {
            resultEl.innerHTML = '<div class="scan-result-empty">未找到候选路径，请手动填写。</div>';
            return;
        }
        let html = `<div style="font-size:10px;color:#6c7086;margin-bottom:6px;">找到 ${candidates.length} 个候选，点击选择：</div>`;
        candidates.forEach((c, i) => {
            html += `<div class="scan-result-item" data-dir="${escapeHtml(c.dir)}" data-idx="${i}">
                <div class="dir">目录：${escapeHtml(c.dir)}</div>
                <div class="file">文件：${escapeHtml(c.file_name)}</div>
            </div>`;
        });
        resultEl.innerHTML = html;
        resultEl.querySelectorAll('.scan-result-item').forEach(el => {
            el.addEventListener('click', () => {
                const dir = el.getAttribute('data-dir');
                const input = $('cfg-napcat-dir');
                if (input) input.value = dir;
                resultEl.querySelectorAll('.scan-result-item').forEach(x => x.style.borderColor = '#45475a');
                el.style.borderColor = '#89b4fa';
                toast('已选择，点「保存并同步」生效', 2000, 'ok');
            });
        });
    } catch (e) {
        if (resultEl) resultEl.innerHTML = `<div class="scan-result-empty">扫描失败: ${escapeHtml(e.message || e)}</div>`;
    }
}

function bindEvents() {
    const bind = (id, handler) => {
        const el = $(id);
        if (el) el.addEventListener('click', handler);
    };

    bind('btn-toggle-power', togglePower);
    bind('btn-sync', doSync);
    bind('btn-open-webui-newpage', openWebUINewPage);
    bind('btn-open-webui-browser', openWebUIBrowser);
    bind('btn-restart', () => postAction('restart', '重启指令已发送'));
    bind('btn-cleanup', async () => {
        if (!confirm('将强制终止所有 NapCat 相关进程。继续？')) return;
        await postAction('cleanup', '已清理残留进程');
    });
    bind('btn-refresh', async () => {
        const r = await postAction('refresh');
        if (r) toast('已重新探测', 1500, 'ok');
    });
    bind('btn-clear-log', clearLog);
    bind('btn-open-config', openConfig);
    bind('btn-config-cancel', closeConfig);
    bind('btn-config-save', saveConfig);
    bind('btn-scan', scanCandidates);

    if (els.modal) {
        els.modal.addEventListener('click', (e) => {
            if (e.target === els.modal) closeConfig();
        });
    }

    document.addEventListener('click', (e) => {
        if (!els.webuiDetails || !els.webuiDetails.open) return;
        if (!e.target.closest('.webui-details')) {
            els.webuiDetails.open = false;
        }
    });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && els.webuiDetails && els.webuiDetails.open) {
            els.webuiDetails.open = false;
        }
    });
}

function main() {
    console.log('[napcat] DOM 已加载');
    bindEvents();
    initBridge();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', main);
} else {
    main();
}