// ============================================
// NapCat_Go 插件前端逻辑
// ============================================

console.log('[napcat] bridge.js 开始加载');

const $ = (id) => document.getElementById(id);

const els = {
    running: $('s-running'),
    qq: $('s-qq'),
    napcat: $('s-napcat'),
    port: $('s-port'),
    webuiToken: $('s-webui-token'),
    reverseToken: $('s-reverse-token'),
    reverseWs: $('s-reverse-ws'),
    logContainer: $('log-container'),
    modal: $('config-modal'),
    toast: $('toast'),
};

let bridge = null;
let statusTimer = null;
let logTimer = null;
let logOffset = 0;
let statusFetching = false;
let logFetching = false;

const MAX_LOG_LINES = 500;
const STATUS_INTERVAL = 5000;
const LOG_INTERVAL = 5000;

function toast(message, duration = 2500) {
    if (!els.toast) return;
    els.toast.textContent = message;
    els.toast.classList.add('visible');
    setTimeout(() => els.toast.classList.remove('visible'), duration);
}

async function initBridge() {
    if (!window.AstrBotPluginPage) {
        console.error('[napcat] AstrBotPluginPage 未注入');
        toast('Bridge 未就绪，请刷新页面');
        return;
    }

    bridge = window.AstrBotPluginPage;
    console.log('[napcat] 找到 AstrBotPluginPage，等待 ready()');

    try {
        await bridge.ready();
        console.log('[napcat] bridge.ready() 完成');
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
        updateStatus(data);
    } catch (e) {
        console.error('[napcat] 拉取状态失败:', e);
    } finally {
        statusFetching = false;
    }
}

function updateStatus(d) {
    if (!d) return;

    setText(els.running, d.running ? '运行中' : '未运行');
    setText(els.qq, d.qq_installed ? '已安装' : '未安装');
    setText(els.napcat, d.napcat_installed ? '已就位' : '未就位');
    setText(els.port, d.napcat_port ?? '-');

    if (d.napcat_token) {
        setText(els.webuiToken, d.napcat_token.substring(0, 8) + '...');
    } else {
        setText(els.webuiToken, '无');
    }

    const reverseToken = d.config?.reverse_ws_token || '';
    setText(els.reverseToken, reverseToken ? reverseToken.substring(0, 8) + '...' : '无');

    if (d.reverse_ws_port) {
        setText(els.reverseWs, `ws://${d.reverse_ws_host}:${d.reverse_ws_port}/ws/`);
    } else {
        setText(els.reverseWs, '未配置');
    }

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

    const cfg = d.config || {};
    if ($('cfg-min')) $('cfg-min').value = cfg.reverse_ws_port_min ?? 6100;
    if ($('cfg-max')) $('cfg-max').value = cfg.reverse_ws_port_max ?? 6200;
    if ($('cfg-token')) $('cfg-token').value = cfg.reverse_ws_token ?? '';
    if ($('cfg-apikey')) $('cfg-apikey').value = cfg.astrbot_api_key ?? '';
    if ($('cfg-mirror')) $('cfg-mirror').value = cfg.napcat_download_mirror ?? '';
}

function setText(el, text) {
    if (el) el.textContent = String(text);
}

async function refreshLogs() {
    if (!bridge || logFetching || !els.logContainer) return;
    logFetching = true;
    try {
        const data = await bridge.apiGet('logs', { since: logOffset });
        if (data.lines && data.lines.length > 0) {
            const newText = data.lines.join('\n');
            els.logContainer.textContent +=
                (els.logContainer.textContent ? '\n' : '') + newText;

            // 裁剪旧日志
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

async function postAction(endpoint, successMsg) {
    if (!bridge) {
        toast('Bridge 未就绪');
        return;
    }
    try {
        await bridge.apiPost(endpoint);
        if (successMsg) toast(successMsg);
        await refreshStatus();
    } catch (e) {
        console.error(`[napcat] ${endpoint} 失败:`, e);
        toast(`操作失败: ${e.message || e}`);
    }
}

async function openWebUI() {
    if (!bridge) return;
    try {
        const data = await bridge.apiGet('status');
        if (!data.napcat_webui_url) {
            toast('WebUI 地址尚未获取，请先启动 NapCat 并等待日志输出');
            return;
        }
        await bridge.apiPost('open-webui');
        toast('已在系统浏览器打开 WebUI');
    } catch (e) {
        console.error('[napcat] 打开 WebUI 失败:', e);
        toast(`打开失败: ${e.message || e}`);
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

    const min = parseInt($('cfg-min').value, 10);
    const max = parseInt($('cfg-max').value, 10);
    const token = $('cfg-token').value.trim();
    const apiKey = $('cfg-apikey').value.trim();
    const mirror = $('cfg-mirror')?.value.trim() || '';

    if (isNaN(min) || isNaN(max) || min >= max) {
        toast('端口范围无效：最小端口必须小于最大端口');
        return;
    }
    if (min < 1 || max > 65535) {
        toast('端口范围必须在 1-65535 之间');
        return;
    }

    try {
        await bridge.apiPost('config/save', {
            reverse_ws_port_min: min,
            reverse_ws_port_max: max,
            reverse_ws_token: token,
            astrbot_api_key: apiKey,
            napcat_download_mirror: mirror,
        });
        toast('配置已保存');
        closeConfig();
        await refreshStatus();
    } catch (e) {
        console.error('[napcat] 保存配置失败:', e);
        toast(`保存失败: ${e.message || e}`);
    }
}

function bindEvents() {
    const bind = (id, handler) => {
        const el = $(id);
        if (el) el.addEventListener('click', handler);
        else console.warn(`[napcat] 未找到元素 #${id}`);
    };

    bind('btn-toggle-power', async () => {
        if (!bridge) return;
        try {
            const data = await bridge.apiGet('status');
            const endpoint = data.running ? 'stop' : 'start';
            await postAction(
                endpoint,
                data.running ? '停止指令已发送' : '启动指令已发送'
            );
        } catch (e) {
            console.error('[napcat] 切换电源状态失败:', e);
            toast(`操作失败: ${e.message || e}`);
        }
    });

    bind('btn-restart', () => postAction('restart', '重启指令已发送'));
    bind('btn-download', async () => {
        if (!confirm('确定要重新下载 NapCat 吗？这会覆盖 napcat/ 目录。')) return;
        await postAction('download-napcat', '下载已触发，请查看日志');
    });
    bind('btn-open-webui', openWebUI);
    bind('btn-cleanup', async () => {
        if (!confirm('将强制终止所有 NapCat 相关进程（包括新增的 QQ.exe）。继续？')) return;
        await postAction('cleanup', '已清理残留进程');
    });
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
    console.log('[napcat] DOM 已加载，绑定事件');
    bindEvents();
    initBridge();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', main);
} else {
    main();
}