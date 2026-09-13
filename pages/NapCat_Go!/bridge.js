console.log('[napcat] bridge.js 开始加载');

const $ = (id) => document.getElementById(id);

const els = {
    subtitle: $('subtitle'),
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
    napcatFile: $('s-napcat-file'),
    guide: $('guide'),
    modal: $('config-modal'),
    toast: $('toast'),
    logContainer: $('log-container'),
    // 下载面板
    downloadPanel: $('download-panel'),
    dlIcon: $('dl-icon'),
    dlTitleText: $('dl-title-text'),
    dlMirror: $('dl-mirror'),
    dlProgressFill: $('dl-progress-fill'),
    dlPercent: $('dl-percent'),
    dlSize: $('dl-size'),
    dlSpeed: $('dl-speed'),
};

let bridge = null;
let statusTimer = null;
let logTimer = null;
let logOffset = 0;
let statusFetching = false;
let logFetching = false;
let isWindows = false;
let downloadHideTimer = null;

const STATUS_INTERVAL = 2000;   // 加快轮询，让下载进度更实时
const LOG_INTERVAL = 3000;
const MAX_LOG_LINES = 500;

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
    statusTimer = setInterval(refreshStatus, STATUS_INTERVAL);
    logTimer = setInterval(refreshLogs, LOG_INTERVAL);
    console.log('[napcat] 轮询已启动');
}

async function refreshStatus() {
    if (!bridge || statusFetching) return;
    statusFetching = true;
    try {
        const data = await bridge.apiGet('status');
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

    // 下载进度
    updateDownloadPanel(d.download_state);

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
        setText(els.webuiToken, tk ? tk.substring(0, 8) + '...' : '无', tk ? 'ok' : '');

        const powerBtn = $('btn-toggle-power');
        if (powerBtn) {
            if (d.running) {
                powerBtn.textContent = '停止';
                powerBtn.classList.remove('btn-primary');
                powerBtn.classList.add('btn-danger');
            } else {
                powerBtn.textContent = '启动';
                powerBtn.classList.remove('btn-danger');
                powerBtn.classList.add('btn-primary');
            }
        }
    } else {
        setText(els.webuiToken, '-', '');
    }

    const fullPath = d.napcat_config_file || '';
    if (fullPath) {
        const fileName = fullPath.split(/[\\/]/).pop() || fullPath;
        setTextMono(els.napcatFile, fileName, 'ok', fullPath);
    } else {
        setTextMono(els.napcatFile, '未找到', 'err', '');
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
    setText(els.napcatDir, d.napcat_config_dir || '未找到，请在配置中手动指定',
            d.napcat_config_dir ? 'ok' : 'err');

    const cfg = d.config || {};
    if ($('cfg-napcat-dir')) $('cfg-napcat-dir').value = cfg.napcat_config_dir || '';
    if ($('cfg-astrbot-path')) $('cfg-astrbot-path').value = cfg.astrbot_config_path || '';
    if ($('cfg-qq-number')) $('cfg-qq-number').value = cfg.qq_number || '';
    if ($('cfg-mirror')) $('cfg-mirror').value = cfg.napcat_download_mirror || '';

    updateGuide(d);
}

function updateDownloadPanel(dl) {
    const p = els.downloadPanel;
    if (!p) return;

    if (!dl) {
        p.classList.remove('visible', 'done', 'failed');
        return;
    }

    const phase = dl.phase || 'idle';
    const isActive = !!dl.downloading;
    const isDone = phase === 'done';
    const isFailed = phase === 'failed';

    // 活跃中 / 刚完成 / 刚失败 都显示
    if (!isActive && !isDone && !isFailed) {
        p.classList.remove('visible', 'done', 'failed');
        return;
    }

    p.classList.add('visible');
    p.classList.toggle('done', isDone);
    p.classList.toggle('failed', isFailed);

    // 图标和标题
    if (isActive) {
        els.dlIcon.textContent = '⬇️';
        if (phase === 'extracting') {
            els.dlIcon.textContent = '📦';
            els.dlTitleText.textContent = '正在解压 NapCat.Shell.zip';
        } else {
            els.dlTitleText.textContent = '正在下载 NapCat.Shell.zip';
        }
    } else if (isDone) {
        els.dlIcon.textContent = '✅';
        els.dlTitleText.textContent = dl.message || '下载完成';
    } else if (isFailed) {
        els.dlIcon.textContent = '❌';
        els.dlTitleText.textContent = dl.message || '下载失败';
    }

    // 镜像信息
    if (isActive && dl.current_mirror && dl.total_mirrors > 0) {
        els.dlMirror.textContent = `${dl.current_index}/${dl.total_mirrors} · ${dl.current_mirror}`;
    } else if (isDone && dl.current_mirror) {
        els.dlMirror.textContent = dl.current_mirror;
    } else {
        els.dlMirror.textContent = '';
    }

    // 进度条
    let percent = dl.percent || 0;
    if (isDone) percent = 100;
    if (isFailed) percent = 0;
    els.dlProgressFill.style.width = percent + '%';

    // 文本
    els.dlPercent.textContent = percent + '%';

    if (dl.total_mb > 0) {
        els.dlSize.textContent = `${(dl.downloaded_mb || 0).toFixed(1)} / ${dl.total_mb.toFixed(1)} MB`;
    } else if (dl.downloaded_mb > 0) {
        els.dlSize.textContent = `${dl.downloaded_mb.toFixed(1)} MB`;
    } else {
        els.dlSize.textContent = '';
    }

    if (isActive && dl.speed_kbps > 0) {
        els.dlSpeed.textContent = `${dl.speed_kbps.toFixed(0)} KB/s`;
    } else {
        els.dlSpeed.textContent = '';
    }

    // 完成后 5 秒自动隐藏
    if (downloadHideTimer) {
        clearTimeout(downloadHideTimer);
        downloadHideTimer = null;
    }
    if (isDone || isFailed) {
        downloadHideTimer = setTimeout(() => {
            p.classList.remove('visible', 'done', 'failed');
        }, 5000);
    }
}

function setText(el, text, className = '') {
    if (!el) return;
    el.textContent = String(text);
    el.className = 'value' + (className ? ' ' + className : '');
}

function setTextMono(el, text, className = '', title = '') {
    if (!el) return;
    el.textContent = String(text);
    el.className = 'value mono-small' + (className ? ' ' + className : '');
    el.title = title || '';
}

function updateGuide(d) {
    const g = els.guide;
    if (!g) return;

    if (d.astrbot_bot_found && d.napcat_config_file && d.last_sync_ok) {
        g.className = 'guide success visible';
        g.innerHTML = `
            <div style="font-weight:600;margin-bottom:6px;">✅ 配置已同步</div>
            <div>反向 WS 地址：<code>ws://${escapeHtml(d.astrbot_bot_host)}:${escapeHtml(d.astrbot_bot_port)}/ws/</code></div>
            <div style="margin-top:6px;font-size:11px;color:#a6adc8;">
                ${isWindows ? '若 NapCat 已在运行，请点「重启」让新配置生效。' : '请重启 NapCat 让新配置生效。'}
            </div>
        `;
        return;
    }

    if (!d.astrbot_bot_found) {
        g.className = 'guide visible';
        g.innerHTML = `
            <div style="font-weight:600;margin-bottom:6px;">📋 先在 AstrBot 创建机器人</div>
            <div style="line-height:1.9;">
                <b>1.</b> 打开 AstrBot 左侧栏 <strong>机器人</strong> → 点击 <strong>+ 创建机器人</strong><br>
                <b>2.</b> 类型选择 <strong>OneBot v11</strong><br>
                <b>3.</b> 反向 WebSocket 主机地址填 <code>0.0.0.0</code><br>
                <b>4.</b> 反向 WebSocket 端口填一个未被占用的端口（如 <code>6199</code>）<br>
                <b>5.</b> Token 按需填写<br>
                <b>6.</b> 保存后回到本页面点 <strong>「立即同步」</strong>
            </div>
        `;
        return;
    }

    if (!d.napcat_config_dir) {
        g.className = 'guide warn visible';
        g.innerHTML = `
            <div style="font-weight:600;margin-bottom:6px;">⚠ 未找到 NapCat 配置目录</div>
            <div style="line-height:1.9;">
                请点击右上角 <strong>「配置」</strong>，填写 <strong>「NapCat 配置目录」</strong>：
            </div>
            <div style="margin-top:6px;font-size:11px;line-height:1.9;">
                · Windows：<code>C:\\Users\\你的用户名\\NapCat\\config</code> 或插件目录下的 <code>napcat\\config</code><br>
                · Linux：<code>/opt/QQ/resources/app/app_launcher/napcat/config</code>
            </div>
        `;
        return;
    }

    if (d.napcat_config_dir && !d.last_sync_ok) {
        g.className = 'guide error visible';
        g.innerHTML = `
            <div style="font-weight:600;margin-bottom:6px;">✗ 同步失败</div>
            <div>${escapeHtml(d.last_sync_msg || '未知错误')}</div>
        `;
        return;
    }

    g.className = 'guide';
}

async function refreshLogs() {
    if (!bridge || logFetching || !els.logContainer || !isWindows) return;
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
    if (!bridge) { toast('Bridge 未就绪', 3000, 'err'); return null; }
    try {
        const r = await bridge.apiPost(endpoint);
        if (successMsg) toast(successMsg, 2000, 'ok');
        return r;
    } catch (e) {
        console.error(`[napcat] ${endpoint} 失败:`, e);
        toast(`操作失败: ${e.message || e}`, 3000, 'err');
        return null;
    }
}

async function doSync() {
    const r = await postAction('sync');
    if (r) {
        await refreshStatus();
        if (r.ok) toast('同步成功', 2000, 'ok');
        else toast(`同步失败: ${r.message || ''}`, 4000, 'err');
    }
}

async function doRefresh() {
    const r = await postAction('refresh');
    if (r) { await refreshStatus(); toast('已重新探测', 1500, 'ok'); }
}

async function openWebUI() {
    if (!bridge) return;
    try {
        await bridge.apiPost('open-webui');
        toast('已在系统浏览器打开 WebUI');
    } catch (e) {
        toast(`打开失败: ${e.message || e}`, 3000, 'err');
    }
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
                <div class="dir">📁 ${escapeHtml(c.dir)}</div>
                <div class="file">📄 ${escapeHtml(c.file_name)}</div>
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

    bind('btn-toggle-power', async () => {
        if (!bridge) return;
        try {
            const d = await bridge.apiGet('status');
            const ep = d.running ? 'stop' : 'start';
            await postAction(ep, d.running ? '停止指令已发送' : '启动指令已发送');
        } catch (e) {
            toast(`操作失败: ${e.message || e}`, 3000, 'err');
        }
    });
    bind('btn-restart', () => postAction('restart', '重启指令已发送'));
    bind('btn-open-webui', openWebUI);
    bind('btn-cleanup', async () => {
        if (!confirm('将强制终止所有 NapCat 相关进程。继续？')) return;
        await postAction('cleanup', '已清理残留进程');
    });
    bind('btn-sync', doSync);
    bind('btn-refresh', doRefresh);
    bind('btn-scan', scanCandidates);
    bind('btn-clear-log', clearLog);
    bind('btn-open-config', openConfig);
    bind('btn-config-cancel', closeConfig);
    bind('btn-config-save', saveConfig);

    if (els.modal) {
        els.modal.addEventListener('click', (e) => {
            if (e.target === els.modal) closeConfig();
        });
    }
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