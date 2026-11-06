'use strict';

/* --------------------------------------------------------------------------
 * 配置界面单页应用：无框架、无外部依赖。
 * 所有写操作都带上启动时生成的 X-Config-Token；GET 只读。
 * ------------------------------------------------------------------------ */

const state = {
  token: '',
  bootstrap: null,
  tab: 'overview',
  feeds: null,
  feedsDirty: false,
  pendingRename: null,
  feedsErrors: [],
  integrations: null,
  modelTest: null,
  mailTest: null,
  settings: null,
  scheduler: null,
  rsshub: null,
  data: null,
  health: null,
  history: [],
  run: { id: null, offset: 0, timer: null, record: null, mode: 'collect', started: false },
  archive: { overview: null, date: '', files: [], active: null },
  publishOutput: '',
  legacyShown: false,
};

const TABS = {
  overview: ['总览', '配置、运行与状态一页掌握'],
  feeds: ['订阅源', '增删改、排序、测试抓取与导入导出'],
  integrations: ['集成配置', '模型、邮件与 RSSHub 连接'],
  run: ['运行', '一键触发、实时日志与结构化摘要'],
  system: ['系统', '路径、定时任务、数据维护与发布'],
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function attr(value) {
  return esc(value);
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatRelative(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const diff = Math.max(0, Date.now() - date.getTime());
  if (diff < 60_000) return '刚刚';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  return date.toLocaleString('zh-CN', { hour12: false });
}

function badge(status, text) {
  const map = { ok: 'ok', warn: 'warn', error: 'error', info: 'info', neutral: '' };
  return `<span class="badge ${map[status] ?? ''}">${esc(text)}</span>`;
}

function sourceTag(source, label) {
  if (!source) return '';
  return `<span class="source ${esc(source)}" title="生效来源">${esc(label || source)}</span>`;
}

function busyTime(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}

/* -------------------------------------------------------------------------- */
/*  API                                                                       */
/* -------------------------------------------------------------------------- */

async function api(path, options = {}) {
  const method = options.method || 'GET';
  const headers = { Accept: 'application/json' };
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET' && state.token) headers['X-Config-Token'] = state.token;
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      cache: 'no-store',
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    });
  } catch (error) {
    throw new Error(`无法连接本地服务：${error.message}`);
  }
  let payload = {};
  try {
    payload = await response.json();
  } catch (error) {
    payload = {};
  }
  if (!response.ok) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.status = response.status;
    error.data = payload;
    if (payload.code === 'token_required') showTokenBanner(true);
    if (payload.code === 'legacy_unconfirmed' && payload.legacy) {
      state.legacyShown = false;
      showLegacyModal(payload.legacy);
    }
    throw error;
  }
  return payload;
}

function toast(message, type = 'ok', timeout = 4200) {
  const root = $('#toast-root');
  const item = document.createElement('div');
  item.className = `toast ${type}`;
  item.textContent = message;
  root.appendChild(item);
  window.setTimeout(() => item.remove(), timeout);
}

function showTokenBanner(invalid = false) {
  const root = $('#banner-root');
  const existing = $('#token-banner');
  if (existing) {
    if (invalid) {
      existing.innerHTML = '<div><strong>写入令牌无效。</strong>配置服务重启后令牌会变化，请使用终端最新打印的带 token 链接重新打开。</div>';
    }
    return;
  }
  const node = document.createElement('div');
  node.id = 'token-banner';
  node.className = 'banner warn';
  node.innerHTML = `<div><strong>${invalid ? '写入令牌无效。' : '当前是只读模式。'}</strong>${
    invalid ? '配置服务重启后令牌会变化，请使用终端最新打印的带 token 链接重新打开。' : '请使用终端启动时打印的带 token 链接打开页面，才能保存配置或触发运行。'
  }</div>`;
  root.appendChild(node);
}

/* -------------------------------------------------------------------------- */
/*  启动与导航                                                                 */
/* -------------------------------------------------------------------------- */

document.addEventListener('DOMContentLoaded', () => {
  init().catch((error) => {
    renderFatal(error);
  });
});

async function init() {
  const params = new URLSearchParams(window.location.search);
  const token = params.get('token');
  if (token) {
    state.token = token;
    sessionStorage.setItem('configUiToken', token);
    params.delete('token');
    const search = params.toString();
    window.history.replaceState(null, '', window.location.pathname + (search ? `?${search}` : '') + window.location.hash);
  } else {
    state.token = sessionStorage.getItem('configUiToken') || '';
  }
  bindEvents();
  await refreshBootstrap();
  if (!state.token) showTokenBanner();
  const hash = window.location.hash.replace(/^#/, '');
  await switchTab(TABS[hash] ? hash : 'overview');
}

function bindEvents() {
  $('#nav').addEventListener('click', (event) => {
    const button = event.target.closest('[data-tab]');
    if (button) switchTab(button.dataset.tab);
  });
  document.addEventListener('click', handleClick);
  document.addEventListener('change', handleChange);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && $('#modal-root').innerHTML) closeModal();
  });
  $('#modal-root').addEventListener('click', (event) => {
    if (event.target.matches('[data-modal-backdrop]')) closeModal();
  });
  window.addEventListener('hashchange', () => {
    const hash = window.location.hash.replace(/^#/, '');
    if (TABS[hash] && hash !== state.tab) switchTab(hash);
  });
}

function renderFatal(error) {
  $('#banner-root').innerHTML = `<div class="banner error"><div><strong>界面无法启动：</strong>${esc(error.message || error)}</div></div>`;
  $('#tab-overview').innerHTML = `<div class="card"><div class="loading"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div></div>`;
}

async function refreshBootstrap() {
  state.bootstrap = await api('/api/bootstrap');
  if (state.bootstrap.legacy?.detected && !state.bootstrap.legacy.confirmed) {
    showLegacyModal(state.bootstrap.legacy);
  }
  renderChrome();
  if (state.tab === 'overview') renderOverview();
}

function renderChrome() {
  const data = state.bootstrap;
  const sideDot = $('#side-dot');
  const sideState = $('#side-state');
  const root = $('#side-root');
  if (!data) {
    sideDot.className = 'dot';
    sideState.textContent = '正在连接…';
    return;
  }
  const modelReady = data.requirements?.model?.ready;
  const mailReady = data.requirements?.mail?.ready;
  const errors = (data.settings?.errors?.length || 0) + (data.feeds?.errors?.length || 0);
  sideDot.className = `dot ${errors ? 'error' : modelReady ? 'ok' : 'warn'}`;
  sideState.textContent = errors ? '配置有错误' : modelReady ? '配置可用' : '模型未配置';
  root.textContent = data.project_root;
  $('#top-status').innerHTML = [
    badge('accent', `${data.feeds?.enabled_feeds ?? 0}/${data.feeds?.total_feeds ?? 0} 个源启用`),
    modelReady ? badge('ok', '模型就绪') : badge('warn', '模型未配置'),
    mailReady ? badge('ok', '邮件就绪') : badge('warn', '邮件未配置'),
    data.scheduler?.loaded ? badge('ok', '定时任务已加载') : data.scheduler?.installed ? badge('warn', '定时任务未加载') : badge('neutral', '未安装定时任务'),
  ].join('');
  $('#side-state').title = modelReady ? '模型配置完整' : `缺少：${(data.requirements?.model?.missing || []).join(', ')}`;
}

async function switchTab(tab) {
  if (!TABS[tab]) return;
  state.tab = tab;
  $$('#nav [data-tab]').forEach((button) => button.classList.toggle('active', button.dataset.tab === tab));
  $$('.tab-panel').forEach((panel) => {
    panel.hidden = panel.id !== `tab-${tab}`;
  });
  $('#page-title').textContent = TABS[tab][0];
  $('#page-subtitle').textContent = TABS[tab][1];
  window.history.replaceState(null, '', `#${tab}`);
  if (tab !== 'run') stopRunPolling();
  if (tab === 'overview') {
    renderOverview();
    refreshBootstrap().catch((error) => toast(error.message, 'error'));
  } else if (tab === 'feeds') {
    await loadFeeds();
  } else if (tab === 'integrations') {
    await loadIntegrations();
  } else if (tab === 'run') {
    await loadRunTab();
  } else if (tab === 'system') {
    await loadSystem();
  }
}

function loadingHtml(text = '正在读取配置…') {
  return `<div class="card"><div class="row muted">${esc(text)}</div><div class="loading mt-12"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div></div>`;
}

/* -------------------------------------------------------------------------- */
/*  总览                                                                       */
/* -------------------------------------------------------------------------- */

function renderOverview() {
  const data = state.bootstrap;
  if (!data) {
    $('#tab-overview').innerHTML = loadingHtml();
    return;
  }
  const feeds = data.feeds || {};
  const model = data.model || {};
  const mail = data.mail || {};
  const rsshub = data.rsshub || {};
  const db = data.data || {};
  const modes = data.requirements?.modes || {};
  const lastRun = data.last_run;
  const legacy = data.legacy;
  const warnings = [];
  if (data.settings?.errors?.length) warnings.push(...data.settings.errors);
  if (feeds.errors?.length) warnings.push(...feeds.errors);
  if (!legacy?.confirmed && legacy?.detected) warnings.push('首次启动：需要先确认旧布局配置预览，确认后才允许写入。');

  const runButtons = [
    ['collect', '仅采集', '拉取所有启用的订阅源'],
    ['report', '仅生成日报', '抓取后调用模型生成日报'],
    ['workflow', '完整工作流', '采集 + 日报 + 邮件'],
    ['site', '仅构建站点', '把 reports/ 构建为静态站点'],
  ]
    .map(([mode, label, sub]) => {
      const info = modes[mode] || { enabled: true, reason: '' };
      const disabled = !info.enabled || (mode === 'workflow' && info.send_enabled === false);
      return `<button class="btn ${mode === 'workflow' ? 'primary' : ''}" data-action="quick-run" data-mode="${mode}" ${disabled ? 'disabled title="' + attr(info.reason) + '"' : ''}>${esc(label)}<span class="tiny muted">${esc(sub)}</span></button>`;
    })
    .join('');

  const sourceRows = [
    ...(data.settings?.items || []).map((item) => ({
      label: item.label,
      key: item.key,
      value: item.value,
      source: item.source,
      sourceLabel: item.source_label,
      note: item.source === 'environment' ? `被环境变量 ${item.env_name} 覆盖` : '—',
    })),
    { label: '模型 Base URL', key: 'OPENAI_BASE_URL', value: model.base_url, source: model.sources?.OPENAI_BASE_URL, sourceLabel: model.source_labels?.OPENAI_BASE_URL },
    { label: '模型名', key: 'OPENAI_MODEL', value: model.model, source: model.sources?.OPENAI_MODEL, sourceLabel: model.source_labels?.OPENAI_MODEL },
    { label: '模型 API Key', key: 'OPENAI_API_KEY', value: model.api_key || '未配置', source: model.sources?.OPENAI_API_KEY, sourceLabel: model.source_labels?.OPENAI_API_KEY },
    { label: 'SMTP 主机', key: 'SMTP_HOST', value: mail.host, source: mail.sources?.SMTP_HOST, sourceLabel: mail.source_labels?.SMTP_HOST },
    { label: 'SMTP 端口', key: 'SMTP_PORT', value: mail.port, source: mail.sources?.SMTP_PORT, sourceLabel: mail.source_labels?.SMTP_PORT },
    { label: '发件人', key: 'MAIL_FROM', value: mail.sender, source: mail.sources?.MAIL_FROM, sourceLabel: mail.source_labels?.MAIL_FROM },
    { label: '收件人', key: 'MAIL_TO', value: (mail.recipients || []).join(', '), source: mail.sources?.MAIL_TO, sourceLabel: mail.source_labels?.MAIL_TO },
    { label: 'SMTP 密码', key: 'SMTP_PASSWORD', value: mail.password || '未配置', source: mail.sources?.SMTP_PASSWORD, sourceLabel: mail.source_labels?.SMTP_PASSWORD },
    { label: 'RSSHub 主机', key: 'RSSHUB_HOST', value: rsshub.host, source: rsshub.sources?.RSSHUB_HOST, sourceLabel: rsshub.source_labels?.RSSHUB_HOST },
    { label: 'RSSHub 端口', key: 'RSSHUB_PORT', value: rsshub.port, source: rsshub.sources?.RSSHUB_PORT, sourceLabel: rsshub.source_labels?.RSSHUB_PORT },
    { label: 'RSSHub 代理', key: 'RSSHUB_PROXY', value: rsshub.proxy || '未设置', source: rsshub.sources?.RSSHUB_PROXY, sourceLabel: rsshub.source_labels?.RSSHUB_PROXY },
    { label: 'RSSHub Token', key: 'GITHUB_ACCESS_TOKEN', value: rsshub.token || '未配置', source: rsshub.sources?.GITHUB_ACCESS_TOKEN, sourceLabel: rsshub.source_labels?.GITHUB_ACCESS_TOKEN },
  ]
    .map(
      (item) => `<tr>
        <td>${esc(item.label)}</td>
        <td class="mono">${esc(item.key)}</td>
        <td>${esc(item.value)}${sourceTag(item.source, item.sourceLabel)}</td>
        <td class="muted small">${esc(item.note || (item.source === 'environment' ? '被环境变量覆盖' : '—'))}</td>
      </tr>`,
    )
    .join('');

  const readiness = [
    ['订阅源', feeds.total_feeds ? 'ok' : 'warn', `${feeds.enabled_feeds ?? 0}/${feeds.total_feeds ?? 0} 个启用`],
    ['模型', model.ready ? 'ok' : 'error', model.ready ? `${model.model} @ ${model.base_url}` : `缺少 ${(model.missing || []).join(', ')}`],
    ['邮件', mail.ready ? 'ok' : 'warn', mail.ready ? `${mail.host}:${mail.port} · ${mail.recipients?.length || 0} 个收件人` : `缺少 ${(mail.missing || []).join(', ')}`],
    ['定时任务', data.scheduler?.loaded ? 'ok' : data.scheduler?.installed ? 'warn' : 'warn', data.scheduler?.loaded ? `下次执行 ${data.scheduler.next_run || '未知'}` : data.scheduler?.installed ? '已安装但未加载' : '未安装'],
    ['去重数据库', db.error ? 'error' : db.exists ? 'ok' : 'warn', db.error || (db.exists ? `${db.total} 条 · ${formatBytes(db.size)}` : '尚未创建')],
    ['配置来源', data.settings?.errors?.length ? 'error' : 'ok', data.legacy?.preview?.config_exists === false ? '尚未生成 config.json，使用默认值' : 'config.json 已读取'],
  ]
    .map(
      ([label, status, detail]) => `<div class="list-item"><div><div class="title">${esc(label)}</div><div class="sub">${esc(detail)}</div></div>${badge(status, status === 'ok' ? '正常' : status === 'error' ? '需处理' : '注意')}</div>`,
    )
    .join('');

  const lastRunHtml = lastRun
    ? `<div class="row-between">
        <div><div class="row">${badge(lastRun.status === 'done' ? 'ok' : lastRun.status === 'failed' ? 'error' : 'info', lastRun.mode_label || lastRun.mode)}<span class="muted small">${esc(busyTime(lastRun.finished_at || lastRun.started_at))}</span></div>
        <div class="small muted mt-8">${esc(runSummaryLine(lastRun.summary))}</div></div>
        <a class="btn small" href="#run" data-action="goto-tab" data-tab="run">查看运行记录</a>
      </div>`
    : '<div class="muted">还没有通过界面运行过任务。左侧「运行」页可以一键开始。</div>';

  $('#tab-overview').innerHTML = `
    <div class="grid grid-4">
      <div class="stat accent"><div class="stat-label">订阅源</div><div class="stat-value">${feeds.enabled_feeds ?? 0}<span class="small muted"> / ${feeds.total_feeds ?? 0}</span></div><div class="stat-sub">启用 / 总数</div></div>
      <div class="stat"><div class="stat-label">已归档条目</div><div class="stat-value">${db.total ?? 0}</div><div class="stat-sub">去重库记录 · ${formatBytes(db.size)}</div></div>
      <div class="stat"><div class="stat-label">模型</div><div class="stat-value">${model.ready ? '就绪' : '缺项'}</div><div class="stat-sub">${esc(model.model || '尚未配置模型名')}</div></div>
      <div class="stat"><div class="stat-label">定时任务</div><div class="stat-value">${data.scheduler?.loaded ? '运行中' : data.scheduler?.installed ? '未加载' : '未安装'}</div><div class="stat-sub">${esc(data.scheduler?.next_run || '—')}</div></div>
    </div>

    ${warnings.length ? `<div class="card mt-16"><div class="notice warn"><strong>需要关注：</strong>${warnings.map(esc).join('；')}</div></div>` : ''}

    <div class="grid grid-2 mt-16">
      <div class="card">
        <div class="card-head"><div><h2>快捷操作</h2><p>启动后可在「运行」页查看实时日志。</p></div></div>
        <div class="row">${runButtons}</div>
        ${!model.ready || !mail.ready ? `<div class="help-box mt-16">未配置的入口已置灰：${[!model.ready ? `模型缺少 ${(model.missing || []).join(', ')}` : '', !mail.ready ? `邮件缺少 ${(mail.missing || []).join(', ')}` : ''].filter(Boolean).map(esc).join('；')}。可先到「集成配置」补全。</div>` : ''}
        <div class="divider"></div>
        <div class="row-between"><span class="small muted">最近一次运行</span>${lastRun ? badge(lastRun.status === 'done' ? 'ok' : lastRun.status === 'failed' ? 'error' : 'info', ({ done: '完成', failed: '失败', running: '运行中', stopped: '已停止' })[lastRun.status] || lastRun.status || '') : ''}</div>
        <div class="mt-8">${lastRunHtml}</div>
      </div>
      <div class="card">
        <div class="card-head"><div><h2>就绪度</h2><p>按当前真实生效值检查关键配置。</p></div><button class="btn small" data-action="health-run">配置体检</button></div>
        <div class="list">${readiness}</div>
      </div>
    </div>

    <div class="card mt-16">
      <div class="card-head"><div><h2>真实生效值与来源</h2><p>优先级：CLI 参数 &gt; 环境变量 &gt; config.json &gt; 默认值。模型/邮件密钥来自 .env 或环境变量。</p></div></div>
      <div class="table-wrap"><table><thead><tr><th>项目</th><th>键</th><th>生效值</th><th>说明</th></tr></thead><tbody>${sourceRows}</tbody></table></div>
    </div>

    <div class="card mt-16">
      <div class="card-head"><div><h2>项目</h2><p>配置文件仍是唯一真相源，界面只负责读写与校验。</p></div></div>
      <dl class="kv">
        <dt>项目目录</dt><dd class="mono">${esc(data.project_root)}</dd>
        <dt>feeds.txt</dt><dd class="mono">${esc(feeds.path || '')}</dd>
        <dt>.env</dt><dd class="mono">${esc(data.settings?.env_path || '')}</dd>
        <dt>config.json</dt><dd class="mono">${esc(data.settings?.config_path || '')}</dd>
        <dt>界面地址</dt><dd class="mono">127.0.0.1:${esc(data.port)}（仅本机）</dd>
      </dl>
    </div>
  `;
}

function runSummaryLine(summary) {
  if (!summary || typeof summary !== 'object') return '—';
  const parts = [];
  if (summary.collect?.saved !== undefined) parts.push(`新增 ${summary.collect.saved} 条`);
  if (summary.collect?.failed_feeds?.length) parts.push(`失败源 ${summary.collect.failed_feeds.length} 个`);
  if (summary.article_count !== undefined) parts.push(`文章 ${summary.article_count} 篇`);
  if (summary.report_path) parts.push(`日报 ${summary.report_path}`);
  if (summary.mail?.status) parts.push(`邮件 ${summary.mail.status === 'sent' ? '已发送' : summary.mail.status === 'skipped' ? '未发送' : summary.mail.status}`);
  if (summary.error) parts.push(summary.error);
  return parts.join(' · ') || '完成';
}

/* -------------------------------------------------------------------------- */
/*  事件分发                                                                   */
/* -------------------------------------------------------------------------- */

async function handleClick(event) {
  const trigger = event.target.closest('[data-action]');
  if (!trigger) return;
  if (trigger.matches('a[href^="#"]') && trigger.dataset.action === 'goto-tab') {
    event.preventDefault();
    switchTab(trigger.dataset.tab);
    return;
  }
  const action = trigger.dataset.action;
  try {
    switch (action) {
      case 'goto-tab':
        await switchTab(trigger.dataset.tab);
        break;
      case 'quick-run':
        await quickRun(trigger.dataset.mode);
        break;
      case 'health-run':
        await runHealth();
        break;
      case 'legacy-confirm':
        await confirmLegacy();
        break;
      case 'modal-close':
        closeModal();
        break;
      case 'confirm-cancel':
        closeModal();
        break;
      case 'confirm-ok': {
        const resolve = modalConfirmResolve;
        modalConfirmResolve = null;
        closeModal();
        if (resolve) resolve(true);
        break;
      }
      case 'copy-export':
        await copyExportText();
        break;
      case 'feed-add':
        openFeedModal(null);
        break;
      case 'feed-edit':
        openFeedModal(Number(trigger.dataset.index));
        break;
      case 'feed-delete':
        await deleteFeed(Number(trigger.dataset.index));
        break;
      case 'feed-move':
        moveFeed(Number(trigger.dataset.index), trigger.dataset.direction === 'up' ? -1 : 1);
        break;
      case 'feed-test':
        await testFeedAt(Number(trigger.dataset.index));
        break;
      case 'feeds-save':
        await saveFeeds();
        break;
      case 'feeds-import':
        await openImportModal();
        break;
      case 'feeds-export':
        await openExportModal();
        break;
      case 'feeds-import-confirm':
        await importFeeds();
        break;
      case 'feed-modal-save':
        saveFeedModal();
        break;
      case 'feed-modal-test':
        await testFeedForm();
        break;
      case 'model-save':
      case 'mail-save':
      case 'rsshub-save':
        await saveIntegration(action.replace('-save', ''));
        break;
      case 'model-test':
        await testModel();
        break;
      case 'mail-test':
        await testMail();
        break;
      case 'rsshub-refresh':
        await loadRsshub(true);
        break;
      case 'rsshub-action':
        await rsshubAction(trigger.dataset.mode);
        break;
      case 'run-start':
        await startRun();
        break;
      case 'run-stop':
        await stopRun();
        break;
      case 'run-copy':
        await copyRunLog();
        break;
      case 'run-history':
        await loadHistoryRun(trigger.dataset.id);
        break;
      case 'run-refresh':
        await loadRunTab();
        break;
      case 'settings-save':
        await saveSettings();
        break;
      case 'scheduler-action':
        await schedulerAction(trigger.dataset.mode);
        break;
      case 'data-refresh':
        await loadSystem(true);
        break;
      case 'data-reset':
        await resetFeedData(trigger.dataset.feed, trigger.dataset.display);
        break;
      case 'archive-date':
        await loadArchiveDate(trigger.dataset.date);
        break;
      case 'archive-file':
        await loadArchiveFile(trigger.dataset.path);
        break;
      case 'site-build':
        await quickRun('site');
        break;
      case 'site-preview':
        window.open('/preview/', '_blank', 'noopener');
        break;
      case 'publish':
        await publishSite();
        break;
      case 'copy-text':
        await copyText(trigger.dataset.text || '');
        break;
      default:
        break;
    }
  } catch (error) {
    if (error.status === 401) return;
    toast(error.message || String(error), 'error', 6000);
  }
}

async function handleChange(event) {
  const target = event.target;
  if (target.matches('[data-run-mode]')) {
    state.run.mode = target.value;
    renderRun();
    return;
  }
  if (target.matches('[data-feed-enabled]')) {
    const index = Number(target.dataset.feedEnabled);
    if (state.feeds?.feeds[index]) {
      state.feeds.feeds[index].enabled = target.checked;
      state.feedsDirty = true;
      renderFeeds();
    }
    return;
  }
  if (target.id === 'model-preset') {
    const preset = (state.integrations?.presets || []).find((item) => item.id === target.value);
    if (preset && preset.id !== 'custom') {
      $('#model-base-url').value = preset.base_url;
      $('#model-name').value = preset.model;
    }
    return;
  }
  if (target.id === 'mail-port') {
    const port = String(target.value || '').trim();
    if (port === '465') {
      $('#mail-ssl').checked = true;
      $('#mail-starttls').checked = false;
    } else if (port === '587') {
      $('#mail-ssl').checked = false;
      $('#mail-starttls').checked = true;
    }
    updateMailWarnings();
    return;
  }
  if (target.id === 'mail-ssl' || target.id === 'mail-starttls') {
    updateMailWarnings();
    return;
  }
}

/* -------------------------------------------------------------------------- */
/*  订阅源                                                                     */
/* -------------------------------------------------------------------------- */

async function loadFeeds() {
  $('#tab-feeds').innerHTML = loadingHtml('正在读取 feeds.txt…');
  state.feeds = await api('/api/feeds');
  state.feedsDirty = false;
  state.feedsErrors = [];
  state.pendingRename = null;
  renderFeeds();
}

function slugify(value) {
  return String(value || '')
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s.-]/gu, '')
    .replace(/[\s_]+/g, '-')
    .replace(/^[.-]+|[.-]+$/g, '')
    .slice(0, 90);
}

function normalizeFeedUrl(url) {
  try {
    const parsed = new URL(url);
    if (!['http:', 'https:'].includes(parsed.protocol)) return null;
    const path = parsed.pathname.replace(/\/+$/, '');
    return `${parsed.protocol}//${parsed.host.toLowerCase()}${path}${parsed.search}`;
  } catch (error) {
    return null;
  }
}

function validateFeedRows(rows) {
  const issues = new Map();
  const add = (index, message) => {
    const list = issues.get(index) || [];
    if (!list.includes(message)) list.push(message);
    issues.set(index, list);
  };
  const urls = new Map();
  const names = new Map();
  rows.forEach((feed, index) => {
    const name = String(feed.name || '').trim();
    const url = String(feed.url || '').trim();
    if (!name) add(index, '名称不能为空');
    if (name.includes('|')) add(index, '名称不能包含 |');
    if (name.length > 120) add(index, '名称过长（最多 120 字符）');
    const slug = slugify(name);
    if (name) {
      if (names.has(slug) && slug) add(index, `归档目录与第 ${names.get(slug) + 1} 行相同`);
      else names.set(slug, index);
    }
    if (!url) {
      add(index, 'URL 不能为空');
    } else {
      const normalized = normalizeFeedUrl(url);
      if (normalized === null) {
        add(index, '仅支持 http:// 或 https:// 的合法 URL');
      } else if (urls.has(normalized)) {
        add(index, `URL 与第 ${urls.get(normalized) + 1} 行重复（忽略末尾 / 与域名大小写）`);
        add(urls.get(normalized), `URL 与第 ${index + 1} 行重复`);
      } else {
        urls.set(normalized, index);
      }
    }
    if (String(feed.note || '').length > 200) add(index, '备注过长（最多 200 字符）');
  });
  return issues;
}

function renderFeeds() {
  if (!state.feeds) {
    $('#tab-feeds').innerHTML = loadingHtml();
    return;
  }
  const data = state.feeds;
  const rows = data.feeds || [];
  const issues = validateFeedRows(rows);
  const writable = Boolean(state.token);
  const rowHtml = rows
    .map((feed, index) => {
      const messages = issues.get(index) || [];
      const status = feed.last_result;
      const resultBadge = status
        ? status.ok
          ? badge('ok', '成功')
          : badge('error', '失败')
        : badge('neutral', '未测试');
      const resultText = status
        ? `<div class="small muted">${esc(formatRelative(status.at))}${status.count !== undefined ? ` · ${status.count} 条` : ''}${status.error ? ` · ${esc(status.error)}` : ''}</div>`
        : '<div class="small muted">—</div>';
      return `<tr class="${messages.length ? 'row-error' : ''} ${feed.enabled ? '' : 'row-disabled'}">
        <td class="row-index">${index + 1}</td>
        <td><label class="check"><input type="checkbox" data-feed-enabled="${index}" ${feed.enabled ? 'checked' : ''} ${writable ? '' : 'disabled'}>${feed.enabled ? '启用' : '跳过'}</label></td>
        <td>
          <div class="feed-name">${esc(feed.name)}${feed.note ? `<div class="small muted">${esc(feed.note)}</div>` : ''}</div>
          ${messages.length ? `<div class="field-error mt-8">${messages.map(esc).join('；')}</div>` : ''}
        </td>
        <td><div class="feed-url">${esc(feed.url)}</div><div class="row mt-8">${badge('neutral', feed.source_type || 'RSS/Atom')} ${feed.valid_url === false ? badge('error', 'URL 无效') : ''}</div></td>
        <td><div>${feed.archived ?? 0} 条</div><div class="small muted">${feed.last_saved_at ? esc(formatRelative(feed.last_saved_at)) : '尚无归档'}</div></td>
        <td>${resultBadge}${resultText}</td>
        <td><div class="cell-actions">
          <button class="btn small" data-action="feed-test" data-index="${index}" ${writable ? '' : 'disabled'}>测试抓取</button>
          <button class="btn small" data-action="feed-edit" data-index="${index}">编辑</button>
          <button class="btn icon small" data-action="feed-move" data-index="${index}" data-direction="up" ${index === 0 ? 'disabled' : ''} title="上移">↑</button>
          <button class="btn icon small" data-action="feed-move" data-index="${index}" data-direction="down" ${index === rows.length - 1 ? 'disabled' : ''} title="下移">↓</button>
          <button class="btn small danger" data-action="feed-delete" data-index="${index}">删除</button>
        </div></td>
      </tr>`;
    })
    .join('');

  const errors = [...(data.errors || []), ...(state.feedsErrors || []).map((item) => item.message || item)];
  const pending = state.pendingRename
    ? `<div class="notice warn mt-12"><strong>待保存的重命名：</strong>${esc(state.pendingRename.old_name)} → ${esc(state.pendingRename.new_name)}（${esc(renameModeLabel(state.pendingRename.mode))}）。保存后才会生效。</div>`
    : '';
  const envWarnings = (state.bootstrap?.settings?.items || []).filter((item) => item.source === 'environment' && item.key.startsWith('paths.'));
  $('#tab-feeds').innerHTML = `
    <div class="card">
      <div class="card-head">
        <div><h2>订阅源（${rows.length}）</h2><p>名称即归档目录名；修改名称会提示历史目录与去重表的影响。</p></div>
        <div class="card-actions">
          <button class="btn" data-action="feeds-import" ${writable ? '' : 'disabled'}>导入</button>
          <button class="btn" data-action="feeds-export">导出</button>
          <button class="btn" data-action="feed-add" ${writable ? '' : 'disabled'}>新增订阅源</button>
          <button class="btn primary" data-action="feeds-save" ${!writable || !state.feedsDirty ? 'disabled' : ''}>保存到 feeds.txt</button>
        </div>
      </div>
      ${errors.length ? `<div class="notice error mb-12">${errors.map(esc).join('<br>')}</div>` : ''}
      ${pending}
      ${envWarnings.length ? `<div class="notice warn mt-12">${envWarnings.map((item) => `环境变量 ${esc(item.env_name)} 正在覆盖路径 ${esc(item.label)}，在界面保存后仍会以环境变量为准。`).join('<br>')}</div>` : ''}
      <div class="table-wrap mt-12">
        <table>
          <thead><tr><th>#</th><th>状态</th><th>名称 / 备注</th><th>URL / 类型</th><th>已归档</th><th>最近抓取结果</th><th>操作</th></tr></thead>
          <tbody>${rowHtml || '<tr><td colspan="7" class="muted">还没有订阅源，点击右上角「新增订阅源」。</td></tr>'}</tbody>
        </table>
      </div>
      <div class="help-box mt-16">
        <strong>feeds.txt 格式：</strong><code>名称 | URL</code>，可选追加 <code>| enabled=false</code> 与 <code>| note=备注</code>。禁用只在采集与日报环节跳过，不删除历史归档。<br>
        <strong>重命名说明：</strong>归档目录与去重表主键使用名称生成的归档名。对已有历史的源改名时，推荐「同步重写去重表」，避免下次抓取重复归档。
      </div>
    </div>
  `;
}

function renameModeLabel(mode) {
  return { display: '仅改显示名', migrate: '同步迁移历史目录', rekey: '同步重写去重表' }[mode] || mode;
}

function openFeedModal(index) {
  const isEdit = index !== null && index >= 0;
  const feed = isEdit ? state.feeds.feeds[index] : { name: '', url: '', enabled: true, note: '', archived: 0 };
  state.editingFeed = { index: isEdit ? index : null, original: { ...feed } };
  const renameOptions = isEdit && ((feed.archived || 0) > 0 || feed.archive_exists)
    ? `<div class="divider"></div>
       <div class="field">
         <span class="label">如果修改了名称</span>
         <label class="check"><input type="radio" name="rename-mode" value="rekey" checked> 同步重写去重表（推荐，避免重复归档）</label>
         <label class="check"><input type="radio" name="rename-mode" value="migrate"> 同步迁移历史目录（仍可能重复归档）</label>
         <label class="check"><input type="radio" name="rename-mode" value="display"> 仅改显示名（会重复归档${feed.archived ? ` ${feed.archived} 条历史` : '已有历史文件'}）</label>
         <div class="hint">${feed.archived ? `该源已有 ${feed.archived} 条归档记录` : '该源已有历史归档目录'}。重命名会先写入 feeds.txt，再迁移归档目录与去重表；失败时回滚 feeds.txt。</div>
       </div>`
    : '';
  openModal({
    title: isEdit ? '编辑订阅源' : '新增订阅源',
    subtitle: isEdit ? `归档名：${feed.archive_name || slugify(feed.name)}` : '保存后仍需点击列表右上角「保存到 feeds.txt」。',
    body: `
      <div class="field-row">
        <label class="field"><span class="label">名称</span><input id="feed-modal-name" type="text" maxlength="120" value="${attr(feed.name)}" placeholder="例如：Hacker News"></label>
        <label class="field"><span class="label">启用</span><label class="check"><input id="feed-modal-enabled" type="checkbox" ${feed.enabled ? 'checked' : ''}>参与采集与日报</label></label>
      </div>
      <label class="field mt-12"><span class="label">URL</span><input id="feed-modal-url" type="url" value="${attr(feed.url)}" placeholder="https://example.com/rss.xml"></label>
      <label class="field mt-12"><span class="label">备注 <span class="hint">可选，只用于展示</span></span><input id="feed-modal-note" type="text" maxlength="200" value="${attr(feed.note || '')}" placeholder="例如：需要代理"></label>
      ${renameOptions}
      <div id="feed-test-result" class="mt-12"></div>
    `,
    footer: `<button class="btn" data-action="modal-close">取消</button><button class="btn" data-action="feed-modal-test" ${state.token ? '' : 'disabled'}>测试抓取</button><button class="btn primary" data-action="feed-modal-save">应用到列表</button>`,
  });
}

function readFeedModal() {
  return {
    name: $('#feed-modal-name').value.trim(),
    url: $('#feed-modal-url').value.trim(),
    enabled: $('#feed-modal-enabled').checked,
    note: $('#feed-modal-note').value.trim(),
  };
}

function saveFeedModal() {
  const editing = state.editingFeed;
  const value = readFeedModal();
  if (!value.name) {
    toast('订阅源名称不能为空', 'error');
    return;
  }
  if (normalizeFeedUrl(value.url) === null) {
    toast('URL 必须是以 http:// 或 https:// 开头的合法地址', 'error');
    return;
  }
  if (editing.index === null) {
    state.feeds.feeds.push({ ...value, archived: 0, archive_name: slugify(value.name), source_type: 'RSS/Atom', last_result: null, last_saved_at: null });
  } else {
    const old = state.feeds.feeds[editing.index];
    const nameChanged = old.name !== value.name;
    if (nameChanged && ((old.archived || 0) > 0 || old.archive_exists)) {
      const mode = ($('input[name="rename-mode"]:checked') || {}).value || 'rekey';
      if (state.pendingRename && (state.pendingRename.old_name !== old.name || state.pendingRename.new_name !== value.name)) {
        toast('一次只能重命名一个订阅源，请先保存当前重命名或取消。', 'error');
        return;
      }
      state.pendingRename = { old_name: old.name, new_name: value.name, mode };
    } else if (state.pendingRename && state.pendingRename.old_name === old.name) {
      state.pendingRename = null;
    }
    state.feeds.feeds[editing.index] = { ...old, ...value };
  }
  state.feedsDirty = true;
  closeModal();
  renderFeeds();
}

async function testFeedForm() {
  const value = readFeedModal();
  const box = $('#feed-test-result');
  box.innerHTML = '<div class="row muted">正在抓取（只读，不写文件与去重库）…</div>';
  try {
    const result = await api('/api/feeds/test', { method: 'POST', body: { name: value.name, url: value.url, timeout: 20 } });
    if (!result.ok) {
      box.innerHTML = `<div class="notice error"><strong>抓取失败：</strong>${esc(result.error)}<div class="small muted mt-8">${esc(result.error_type || '')}</div></div>`;
      return;
    }
    const items = (result.items || [])
      .map((item) => `<li><a href="${attr(item.link)}" target="_blank" rel="noreferrer noopener">${esc(item.title)}</a><span class="small muted"> ${esc(item.published || '')}</span></li>`)
      .join('');
    box.innerHTML = `<div class="notice ok"><strong>成功：</strong>共 ${result.count} 条，耗时 ${result.elapsed_ms}ms。最近 ${Math.min(5, result.items.length)} 条：</div><ul class="mt-8">${items || '<li class="muted">没有条目</li>'}</ul>`;
  } catch (error) {
    box.innerHTML = `<div class="notice error">${esc(error.message)}</div>`;
  }
}

async function testFeedAt(index) {
  const feed = state.feeds.feeds[index];
  openModal({
    title: `测试抓取 · ${feed.name}`,
    subtitle: '只读探测：不写归档文件，也不写去重库。',
    body: `<div id="row-test-result"><div class="row muted">正在抓取…</div></div>`,
    footer: `<button class="btn" data-action="modal-close">关闭</button>`,
  });
  try {
    const result = await api('/api/feeds/test', { method: 'POST', body: { name: feed.name, url: feed.url, timeout: 20 } });
    const box = $('#row-test-result');
    if (!box) return;
    if (!result.ok) {
      box.innerHTML = `<div class="notice error"><strong>失败：</strong>${esc(result.error)}</div>`;
    } else {
      const items = (result.items || [])
        .map((item) => `<li><a href="${attr(item.link)}" target="_blank" rel="noreferrer noopener">${esc(item.title)}</a><span class="small muted"> ${esc(item.published || '')}</span></li>`)
        .join('');
      box.innerHTML = `<div class="notice ok"><strong>成功：</strong>共 ${result.count} 条，耗时 ${result.elapsed_ms}ms</div><ul class="mt-8">${items || '<li class="muted">没有条目</li>'}</ul>`;
    }
    const row = state.feeds.feeds[index];
    row.last_result = result.ok
      ? { ok: true, at: result.at, count: result.count, source: 'test', error: '' }
      : { ok: false, at: result.at, error: result.error, source: 'test' };
    renderFeeds();
  } catch (error) {
    const box = $('#row-test-result');
    if (box) box.innerHTML = `<div class="notice error">${esc(error.message)}</div>`;
  }
}

async function deleteFeed(index) {
  const feed = state.feeds.feeds[index];
  const ok = await confirmDialog({
    title: '删除订阅源',
    message: `确定从 feeds.txt 删除「${feed.name}」？历史归档目录与去重记录不会被删除。`,
    confirmText: '删除',
    danger: true,
  });
  if (!ok) return;
  state.feeds.feeds.splice(index, 1);
  if (state.pendingRename && state.pendingRename.old_name === feed.name) state.pendingRename = null;
  state.feedsDirty = true;
  renderFeeds();
}

function moveFeed(index, delta) {
  const target = index + delta;
  if (target < 0 || target >= state.feeds.feeds.length) return;
  const rows = state.feeds.feeds;
  [rows[index], rows[target]] = [rows[target], rows[index]];
  state.feedsDirty = true;
  renderFeeds();
}

async function saveFeeds() {
  const issues = validateFeedRows(state.feeds.feeds);
  if (issues.size) {
    state.feedsErrors = [...issues.entries()].flatMap(([index, messages]) => messages.map((message) => ({ index, message })));
    renderFeeds();
    toast('请先修正高亮行的错误', 'error');
    return;
  }
  const body = {
    feeds: state.feeds.feeds.map((feed) => ({ name: feed.name, url: feed.url, enabled: feed.enabled, note: feed.note })),
    revision: state.feeds.revision,
    rename: state.pendingRename,
  };
  const result = await api('/api/feeds', { method: 'PUT', body });
  state.feeds = result;
  state.feedsDirty = false;
  state.feedsErrors = [];
  state.pendingRename = null;
  renderFeeds();
  if (result.rename?.warnings?.length) {
    toast(`已保存，但${result.rename.warnings.join(' ')}`, 'warn', 8000);
  } else {
    toast('订阅源已保存到 feeds.txt');
  }
  refreshBootstrap().catch(() => {});
}

async function openExportModal() {
  const result = await api('/api/feeds/export');
  openModal({
    title: '导出 feeds.txt',
    subtitle: '内容与 CLI 读取格式一致，可直接备份或粘贴到其他机器。',
    body: `<textarea id="export-text" class="mono" readonly>${esc(result.text)}</textarea>`,
    footer: `<button class="btn" data-action="modal-close">关闭</button><button class="btn primary" data-action="copy-export">复制内容</button>`,
  });
}

async function openImportModal() {
  const current = await api('/api/feeds/export');
  openModal({
    title: '导入 feeds.txt',
    subtitle: '会先校验并显示预览；确认后替换当前 feeds.txt，原文件会备份。',
    body: `
      <label class="field"><span class="label">粘贴 feeds.txt 内容</span>
        <textarea id="import-text" class="mono" spellcheck="false">${esc(current.text)}</textarea>
        <span class="hint">支持注释、空行、<code>名称 | URL</code>、<code>| enabled=false</code> 与 <code>| note=...</code>。</span>
      </label>
      <div id="import-result" class="mt-12"></div>
    `,
    footer: `<button class="btn" data-action="modal-close">取消</button><button class="btn primary" data-action="feeds-import-confirm">校验并导入</button>`,
  });
}

async function importFeeds() {
  const text = $('#import-text').value;
  const box = $('#import-result');
  box.innerHTML = '<div class="row muted">正在校验…</div>';
  const check = await api('/api/feeds/import', { method: 'POST', body: { text, dry_run: true } });
  if (!check.ok) {
    box.innerHTML = `<div class="notice error">${[...(check.document_errors || []), ...(check.errors || []).map((item) => item.message)].map(esc).join('<br>')}</div>`;
    return;
  }
  const count = (check.feeds || []).length;
  const ok = await confirmDialog({
    title: '确认导入',
    message: `将用 ${count} 个订阅源替换当前 feeds.txt。当前文件会备份，注释与未知扩展列按导入内容保留。`,
    confirmText: '确认导入',
  });
  if (!ok) return;
  const result = await api('/api/feeds/import', {
    method: 'POST',
    body: { text, revision: state.feeds?.revision, dry_run: false },
  });
  state.feeds = result;
  state.feedsDirty = false;
  state.pendingRename = null;
  closeModal();
  renderFeeds();
  toast(`已导入 ${count} 个订阅源`);
  refreshBootstrap().catch(() => {});
}

/* -------------------------------------------------------------------------- */
/*  集成配置                                                                   */
/* -------------------------------------------------------------------------- */

async function loadIntegrations() {
  $('#tab-integrations').innerHTML = loadingHtml('正在读取 .env 与 .env.rsshub…');
  const [integrations, rsshubDetail] = await Promise.all([api('/api/integrations'), api('/api/rsshub')]);
  state.integrations = integrations;
  state.rsshubDetail = rsshubDetail;
  state.modelTest = null;
  state.mailTest = null;
  renderIntegrations();
}

function modelPresetId(model) {
  const base = (model.base_url || '').replace(/\/+$/, '');
  if (base === 'https://api.deepseek.com') return 'deepseek';
  if (base === 'https://api.openai.com/v1') return 'openai';
  return 'custom';
}

function renderIntegrations() {
  if (!state.integrations) {
    $('#tab-integrations').innerHTML = loadingHtml();
    return;
  }
  const data = state.integrations;
  const model = data.model || {};
  const mail = data.mail || {};
  const rsshubDetail = state.rsshubDetail || {};
  const rsshub = rsshubDetail.config || data.rsshub || {};
  const writable = Boolean(state.token);
  const preset = modelPresetId(model);
  const modelSources = model.sources || {};
  const modelLabels = model.source_labels || {};
  const mailSources = mail.sources || {};
  const mailLabels = mail.source_labels || {};
  const rsshubSources = rsshub.sources || {};
  const rsshubLabels = rsshub.source_labels || {};
  const presetOptions = (data.presets || [])
    .map((item) => `<option value="${attr(item.id)}" ${item.id === preset ? 'selected' : ''}>${esc(item.label)}</option>`)
    .join('');
  const envOverrides = [...Object.entries(modelSources), ...Object.entries(mailSources), ...Object.entries(rsshubSources)]
    .filter(([, source]) => source === 'environment')
    .map(([key]) => key);
  const envNotice = envOverrides.length
    ? `<div class="notice warn mb-12"><strong>环境变量正在覆盖：</strong>${envOverrides.map(esc).join(', ')}。launchd 定时任务不会继承 shell 环境变量；如需定时任务使用这些值，请把密钥等值保存进 .env。</div>`
    : '';

  $('#tab-integrations').innerHTML = `
    ${envNotice}
    <div class="grid grid-2">
      <div class="card">
        <div class="card-head">
          <div><h2>模型</h2><p>OpenAI 兼容的 Chat Completions 接口；密钥写入 .env。</p></div>
          <div class="row">${model.ready ? badge('ok', '已配置') : badge('warn', `缺少 ${(model.missing || []).length} 项`)}</div>
        </div>
        ${model.errors?.length ? `<div class="notice error mb-12">${esc(model.errors.join('；'))}</div>` : ''}
        <div class="stack">
          <label class="field"><span class="label">预设</span><select id="model-preset">${presetOptions}</select></label>
          <label class="field"><span class="label">Base URL ${sourceTag(modelSources.OPENAI_BASE_URL, modelLabels.OPENAI_BASE_URL)}</span><input id="model-base-url" type="url" value="${attr(model.base_url)}" placeholder="https://api.deepseek.com"><span class="hint">通常以 /v1 结尾（DeepSeek 可用 https://api.deepseek.com）。</span></label>
          <label class="field"><span class="label">模型名 ${sourceTag(modelSources.OPENAI_MODEL, modelLabels.OPENAI_MODEL)}</span><input id="model-name" type="text" value="${attr(model.model)}" placeholder="deepseek-chat"></label>
          <label class="field"><span class="label">API Key ${sourceTag(modelSources.OPENAI_API_KEY, modelLabels.OPENAI_API_KEY)}</span><input id="model-api-key" type="password" autocomplete="new-password" placeholder="${model.api_key_saved ? `已保存（${attr(model.api_key)}），留空保持不变` : '尚未配置'}"><span class="hint">页面不会显示明文；保存后 .env 权限会收敛为 600。</span></label>
        </div>
        <div id="model-test-result" class="mt-12">${renderModelTest(state.modelTest)}</div>
        <div class="form-actions"><button class="btn" data-action="model-test" ${writable ? '' : 'disabled'}>测试连接</button><button class="btn primary" data-action="model-save" ${writable ? '' : 'disabled'}>保存模型配置</button></div>
      </div>

      <div class="card">
        <div class="card-head">
          <div><h2>邮件</h2><p>SMTP 发送日报；密钥写入 .env。</p></div>
          <div class="row">${mail.ready ? badge('ok', '已配置') : badge('warn', `缺少 ${(mail.missing || []).length} 项`)}</div>
        </div>
        ${mail.errors?.length ? `<div class="notice error mb-12">${esc(mail.errors.join('；'))}</div>` : ''}
        <div class="field-row">
          <label class="field"><span class="label">SMTP 主机 ${sourceTag(mailSources.SMTP_HOST, mailLabels.SMTP_HOST)}</span><input id="mail-host" type="text" value="${attr(mail.host)}" placeholder="smtp.example.com"></label>
          <label class="field"><span class="label">端口 ${sourceTag(mailSources.SMTP_PORT, mailLabels.SMTP_PORT)}</span><input id="mail-port" type="number" min="1" max="65535" value="${attr(mail.port)}" placeholder="465"><span class="hint">465 建议 SSL；587 建议 STARTTLS。</span></label>
        </div>
        <div class="field-row mt-12">
          <label class="field"><span class="label">用户名 ${sourceTag(mailSources.SMTP_USERNAME, mailLabels.SMTP_USERNAME)}</span><input id="mail-username" type="text" value="${attr(mail.username)}" autocomplete="username"></label>
          <label class="field"><span class="label">密码 / 授权码 ${sourceTag(mailSources.SMTP_PASSWORD, mailLabels.SMTP_PASSWORD)}</span><input id="mail-password" type="password" autocomplete="new-password" placeholder="${mail.password_saved ? `已保存（${attr(mail.password)}），留空保持不变` : '尚未配置'}"></label>
        </div>
        <div class="field-row mt-12">
          <label class="field"><span class="label">发件人 ${sourceTag(mailSources.MAIL_FROM, mailLabels.MAIL_FROM)}</span><input id="mail-sender" type="text" value="${attr(mail.sender)}" placeholder="your-email@example.com"></label>
          <label class="field"><span class="label">主题前缀 ${sourceTag(mailSources.MAIL_SUBJECT_PREFIX, mailLabels.MAIL_SUBJECT_PREFIX)}</span><input id="mail-subject-prefix" type="text" value="${attr(mail.subject_prefix)}" placeholder="RSS 日报"></label>
        </div>
        <label class="field mt-12"><span class="label">收件人 ${sourceTag(mailSources.MAIL_TO, mailLabels.MAIL_TO)}</span><input id="mail-recipients" type="text" value="${attr(mail.recipients_text || '')}" placeholder="a@example.com, b@example.com"><span class="hint">多个收件人用逗号或分号分隔。</span></label>
        <div class="row mt-12">
          <label class="check"><input id="mail-ssl" type="checkbox" ${mail.use_ssl ? 'checked' : ''}> SSL（通常 465）</label>
          <label class="check"><input id="mail-starttls" type="checkbox" ${mail.use_starttls ? 'checked' : ''}> STARTTLS（通常 587）</label>
        </div>
        <div id="mail-warning" class="mt-8">${(mail.warnings || []).map((warning) => `<div class="notice warn">${esc(warning)}</div>`).join('')}</div>
        <div id="mail-test-result" class="mt-12">${renderMailTest(state.mailTest)}</div>
        <div class="form-actions"><button class="btn" data-action="mail-test" ${writable ? '' : 'disabled'}>发送测试邮件</button><button class="btn primary" data-action="mail-save" ${writable ? '' : 'disabled'}>保存邮件配置</button></div>
      </div>
    </div>

    <div class="card mt-16">
      <div class="card-head">
        <div><h2>RSSHub</h2><p>主机、端口、镜像与代理写入 .env.rsshub；Docker Compose 与 launchd 仍可独立使用。</p></div>
        <div class="row">
          ${rsshubDetail.docker_available ? badge('ok', '已安装 docker') : badge('warn', '未找到 docker')}
          ${rsshubDetail.health?.ok ? badge('ok', `健康 ${rsshubDetail.health.latency_ms}ms`) : badge('warn', '健康检查未通过')}
        </div>
      </div>
      ${rsshub.errors?.length ? `<div class="notice error mb-12">${esc(rsshub.errors.join('；'))}</div>` : ''}
      <div class="field-row three">
        <label class="field"><span class="label">主机 ${sourceTag(rsshubSources.RSSHUB_HOST, rsshubLabels.RSSHUB_HOST)}</span><input id="rsshub-host" type="text" value="${attr(rsshub.host)}" placeholder="127.0.0.1"></label>
        <label class="field"><span class="label">端口 ${sourceTag(rsshubSources.RSSHUB_PORT, rsshubLabels.RSSHUB_PORT)}</span><input id="rsshub-port" type="number" min="1" max="65535" value="${attr(rsshub.port)}"></label>
        <label class="field"><span class="label">代理 ${sourceTag(rsshubSources.RSSHUB_PROXY, rsshubLabels.RSSHUB_PROXY)}</span><input id="rsshub-proxy" type="url" value="${attr(rsshub.proxy)}" placeholder="http://127.0.0.1:7890"><span class="hint">留空表示不使用代理。</span></label>
      </div>
      <div class="field-row three mt-12">
        <label class="field"><span class="label">RSSHub 镜像 ${sourceTag(rsshubSources.RSSHUB_IMAGE, rsshubLabels.RSSHUB_IMAGE)}</span><input id="rsshub-image" type="text" value="${attr(rsshub.image)}"></label>
        <label class="field"><span class="label">Browserless 镜像</span><input id="rsshub-browserless-image" type="text" value="${attr(rsshub.browserless_image)}"></label>
        <label class="field"><span class="label">Redis 镜像</span><input id="rsshub-redis-image" type="text" value="${attr(rsshub.redis_image)}"></label>
      </div>
      <label class="field mt-12"><span class="label">GitHub Token（可选） ${sourceTag(rsshubSources.GITHUB_ACCESS_TOKEN, rsshubLabels.GITHUB_ACCESS_TOKEN)}</span><input id="rsshub-token" type="password" autocomplete="new-password" placeholder="${rsshub.token_saved ? `已保存（${attr(rsshub.token)}），留空保持不变` : '尚未配置'}"><span class="hint">用于需要 token 的 RSSHub 路由（如 GitHub Trending）。</span></label>
      ${rsshubDetail.health ? `<div class="help-box mt-12">${rsshubDetail.health.ok ? `健康检查：${esc(rsshubDetail.health.url)} · HTTP ${esc(rsshubDetail.health.status)} · ${rsshubDetail.health.latency_ms}ms` : `健康检查：${esc(rsshubDetail.health.url || '')} · ${esc(rsshubDetail.health.error || '失败')}`}</div>` : ''}
      ${rsshubDetail.containers ? `<div class="mt-12"><div class="small muted">容器状态</div><pre class="mono mt-8" style="white-space:pre-wrap;background:#f8fafc;border:1px solid var(--line);border-radius:8px;padding:10px;max-height:180px;overflow:auto">${esc(rsshubDetail.containers)}</pre></div>` : ''}
      <div class="form-actions">
        <button class="btn" data-action="rsshub-refresh">刷新状态</button>
        <button class="btn" data-action="rsshub-action" data-mode="down" ${writable && rsshubDetail.docker_available ? '' : 'disabled'}>停止</button>
        <button class="btn" data-action="rsshub-action" data-mode="restart" ${writable && rsshubDetail.docker_available ? '' : 'disabled'}>重启</button>
        <button class="btn blue" data-action="rsshub-action" data-mode="up" ${writable && rsshubDetail.docker_available ? '' : 'disabled'}>启动</button>
        <button class="btn primary" data-action="rsshub-save" ${writable ? '' : 'disabled'}>保存 RSSHub 配置</button>
      </div>
    </div>
  `;
}

function renderModelTest(result) {
  if (!result) return '';
  if (result.ok) {
    return `<div class="notice ok"><strong>连接成功：</strong>HTTP ${esc(result.status)} · ${esc(result.latency_ms)}ms · 模型 ${esc(result.model)}${result.reply ? `<div class="small mt-8">回复：${esc(result.reply)}</div>` : ''}</div>`;
  }
  return `<div class="notice error"><strong>连接失败：</strong>${esc(result.error)}${result.hint ? `<div class="small mt-8">${esc(result.hint)}</div>` : ''}${result.raw ? `<pre class="mono mt-8" style="white-space:pre-wrap;max-height:140px;overflow:auto">${esc(result.raw)}</pre>` : ''}</div>`;
}

function renderMailTest(result) {
  if (!result) return '';
  if (result.ok) {
    return `<div class="notice ok"><strong>发送成功：</strong>${esc(result.stage_label)} · ${esc(result.latency_ms)}ms · ${esc((result.recipients || []).join(', '))}</div>`;
  }
  return `<div class="notice error"><strong>${esc(result.stage_label || '测试失败')}：</strong>${esc(result.error)}${result.hint ? `<div class="small mt-8">${esc(result.hint)}</div>` : ''}</div>`;
}

function mailFormValues() {
  return {
    host: $('#mail-host').value.trim(),
    port: $('#mail-port').value.trim(),
    username: $('#mail-username').value.trim(),
    password: $('#mail-password').value,
    sender: $('#mail-sender').value.trim(),
    recipients: $('#mail-recipients').value.trim(),
    subject_prefix: $('#mail-subject-prefix').value.trim(),
    use_ssl: $('#mail-ssl').checked,
    use_starttls: $('#mail-starttls').checked,
  };
}

function updateMailWarnings() {
  const port = String($('#mail-port').value || '').trim();
  const ssl = $('#mail-ssl').checked;
  const starttls = $('#mail-starttls').checked;
  const warnings = [];
  if (ssl && starttls) warnings.push('SSL 与 STARTTLS 同时开启：多数服务器只支持其中一种。');
  if (port === '587' && ssl) warnings.push('端口 587 通常使用 STARTTLS 而不是 SSL。');
  if (port === '465' && starttls) warnings.push('端口 465 通常使用 SSL 而不是 STARTTLS。');
  $('#mail-warning').innerHTML = warnings.map((warning) => `<div class="notice warn">${esc(warning)}</div>`).join('');
}

async function saveIntegration(section) {
  const body = { env_revision: state.integrations.revisions?.env };
  if (section === 'model') {
    body.model = {
      base_url: $('#model-base-url').value.trim(),
      model: $('#model-name').value.trim(),
      api_key: $('#model-api-key').value,
    };
  } else if (section === 'mail') {
    body.mail = mailFormValues();
  } else if (section === 'rsshub') {
    body.host = $('#rsshub-host').value.trim();
    body.port = $('#rsshub-port').value.trim();
    body.image = $('#rsshub-image').value.trim();
    body.browserless_image = $('#rsshub-browserless-image').value.trim();
    body.redis_image = $('#rsshub-redis-image').value.trim();
    body.proxy = $('#rsshub-proxy').value.trim();
    body.token = $('#rsshub-token').value;
    body.revision = state.integrations.revisions?.rsshub_env;
    const result = await api('/api/rsshub', { method: 'PUT', body });
    state.rsshubDetail = result;
    state.integrations = { ...state.integrations, rsshub: result.config, revisions: { ...state.integrations.revisions, rsshub_env: result.saved_revision } };
    renderIntegrations();
    toast('RSSHub 配置已保存到 .env.rsshub');
    return;
  }
  const result = await api('/api/integrations', { method: 'PUT', body });
  state.integrations = result;
  renderIntegrations();
  toast(section === 'model' ? '模型配置已保存到 .env' : '邮件配置已保存到 .env');
  if (result.warnings?.length) toast(result.warnings.join(' '), 'warn', 7000);
  refreshBootstrap().catch(() => {});
}

async function testModel() {
  const box = $('#model-test-result');
  box.innerHTML = '<div class="row muted">正在请求最小 chat completion…</div>';
  const result = await api('/api/model/test', {
    method: 'POST',
    body: {
      base_url: $('#model-base-url').value.trim(),
      model: $('#model-name').value.trim(),
      api_key: $('#model-api-key').value,
      timeout: 30,
    },
  });
  state.modelTest = result;
  box.innerHTML = renderModelTest(result);
}

async function testMail() {
  const box = $('#mail-test-result');
  box.innerHTML = '<div class="row muted">正在连接 SMTP 并发送测试邮件…</div>';
  const result = await api('/api/mail/test', { method: 'POST', body: { ...mailFormValues(), timeout: 15 } });
  state.mailTest = result;
  box.innerHTML = renderMailTest(result);
}

async function loadRsshub(onlyRefresh = false) {
  if (onlyRefresh) {
    const data = await api('/api/rsshub');
    state.rsshubDetail = data;
    state.integrations = { ...state.integrations, rsshub: data.config, revisions: { ...state.integrations.revisions, rsshub_env: data.revision } };
    renderIntegrations();
    toast('RSSHub 状态已刷新');
  }
}

async function rsshubAction(mode) {
  const labels = { up: '启动 RSSHub 容器', down: '停止 RSSHub 容器（保留 Redis 数据卷）', restart: '重启 RSSHub 容器' };
  const ok = await confirmDialog({
    title: labels[mode] || 'RSSHub 操作',
    message: '将通过 docker compose 操作本机容器，可能需要几十秒。确认继续？',
    confirmText: '执行',
  });
  if (!ok) return;
  const result = await api('/api/rsshub/action', { method: 'POST', body: { action: mode, confirm: true } });
  const data = await api('/api/rsshub');
  state.rsshubDetail = data;
  state.integrations = { ...state.integrations, rsshub: data.config, revisions: { ...state.integrations.revisions, rsshub_env: data.revision } };
  renderIntegrations();
  const box = $('#tab-integrations');
  if (box) {
    box.insertAdjacentHTML('afterbegin', `<div class="notice ${result.ok ? 'ok' : 'error'} mb-12"><strong>${result.ok ? '执行完成' : '执行失败'}：</strong><pre class="mono mt-8" style="white-space:pre-wrap;max-height:180px;overflow:auto">${esc(result.output || '')}</pre></div>`);
  }
}

/* -------------------------------------------------------------------------- */
/*  运行                                                                       */
/* -------------------------------------------------------------------------- */

async function loadRunTab() {
  $('#tab-run').innerHTML = loadingHtml('正在读取运行记录…');
  await refreshBootstrap();
  state.history = (await api('/api/runs')).runs || [];
  if (state.run.id) {
    try {
      const status = await api(`/api/run/${state.run.id}`);
      state.run.record = status;
      if (status.status === 'running' && !state.run.timer) {
        pollRun();
      }
    } catch (error) {
      state.run = { id: null, offset: 0, timer: null, record: null, mode: state.run.mode, log: '' };
    }
  }
  if (!state.run.id) {
    const running = state.history.find((item) => item.status === 'running');
    if (running) {
      await loadHistoryRun(running.id);
      return;
    }
  }
  renderRun();
}

function runModeInfo(mode) {
  return state.bootstrap?.requirements?.modes?.[mode] || { enabled: true, reason: '', send_enabled: true, send_reason: '' };
}

function renderRun() {
  const data = state.bootstrap;
  if (!data) {
    $('#tab-run').innerHTML = loadingHtml();
    return;
  }
  const modes = [
    ['collect', '仅采集', '抓取所有启用的 RSS 源，写入归档目录与去重库。'],
    ['report', '仅生成日报', '抓取后调用模型生成指定日期的日报。'],
    ['workflow', '完整工作流', '采集 + 日报 + 邮件；可关闭抓取或发送。'],
    ['site', '仅构建站点', '把 reports/ 构建为 docs/ 静态站点。'],
  ];
  const selected = state.run.mode || 'collect';
  const selectedInfo = runModeInfo(selected);
  const current = state.run.record;
  const running = current?.status === 'running' || Boolean(state.run.timer);
  const modeCards = modes
    .map(([mode, label, description]) => {
      const info = runModeInfo(mode);
      const disabled = !info.enabled;
      return `<label class="mode-card ${selected === mode ? 'selected' : ''} ${disabled ? 'disabled' : ''}">
        <input type="radio" name="run-mode" value="${mode}" data-run-mode ${selected === mode ? 'checked' : ''} ${disabled ? 'disabled' : ''}>
        <span><strong>${esc(label)}</strong><span>${esc(disabled ? info.reason : description)}</span></span>
      </label>`;
    })
    .join('');
  const showDate = selected !== 'collect' && selected !== 'site';
  const showFetch = selected === 'report' || selected === 'workflow';
  const showSend = selected === 'workflow';
  const sendInfo = runModeInfo('workflow');
  const modelMissing = !data.requirements?.model?.ready;
  const mailMissing = !data.requirements?.mail?.ready;
  const summary = current?.summary;
  const logText = state.run.log || '';
  const historyHtml = (state.history || [])
    .map(
      (item) => `<div class="history-item">
        <div><span class="badge ${item.status === 'done' ? 'ok' : item.status === 'failed' ? 'error' : 'info'}">${esc(item.mode_label || item.mode)}</span>
        <span class="muted small"> ${esc(busyTime(item.started_at))}</span></div>
        <div class="row"><span class="small muted">${esc(runSummaryLine(item.summary))}</span><button class="btn small" data-action="run-history" data-id="${attr(item.id)}">查看日志</button></div>
      </div>`,
    )
    .join('');
  $('#tab-run').innerHTML = `
    <div class="run-layout">
      <div class="stack">
        <div class="card">
          <div class="card-head"><div><h2>启动任务</h2><p>运行使用项目真实路径与当前生效配置。</p></div></div>
          <div class="mode-list">${modeCards}</div>
          <div class="divider"></div>
          ${showDate ? `<label class="field"><span class="label">日报日期</span><input id="run-date" type="date" value="${attr(data.default_report_date || '')}"><span class="hint">留空则使用「系统」页配置的默认偏移（今天前 ${data.settings?.items?.find((item) => item.key === 'run.date_offset')?.value ?? 1} 天）。</span></label>` : ''}
          ${showFetch ? `<label class="check mt-12"><input id="run-fetch" type="checkbox" checked> 运行前先抓取 RSS</label>` : ''}
          ${showSend ? `<label class="check mt-12"><input id="run-send" type="checkbox" ${mailMissing ? 'disabled' : 'checked'}> 发送邮件${mailMissing ? `（${esc(sendInfo.send_reason || '邮件未配置')}）` : ''}</label>` : ''}
          ${selectedInfo.reason ? `<div class="notice warn mt-12">${esc(selectedInfo.reason)}</div>` : ''}
          ${modelMissing && (selected === 'report' || selected === 'workflow') ? `<div class="notice warn mt-12">模型未配置，相关运行入口已置灰。请到「集成配置」补全。</div>` : ''}
          <div class="form-actions">
            ${running ? `<button class="btn danger" data-action="run-stop">停止当前任务</button>` : `<button class="btn primary" data-action="run-start" ${!state.token || !selectedInfo.enabled ? 'disabled' : ''}>开始运行</button>`}
          </div>
        </div>
        <div class="card">
          <div class="card-head"><div><h2>运行历史</h2><p>最近 20 次，日志保存在 logs/ui-runs/。</p></div><button class="btn small" data-action="run-refresh">刷新</button></div>
          <div class="history-list">${historyHtml || '<div class="muted">还没有运行记录。</div>'}</div>
        </div>
      </div>
      <div class="stack">
        <div class="card">
          <div class="card-head">
            <div><h2>${current ? esc(`${current.mode_label || current.mode} · ${current.status}`) : '实时日志'}</h2><p>${current ? esc(busyTime(current.started_at)) : '启动任务后这里会滚动显示 stdout/stderr。'}</p></div>
            <div class="row">
              ${current ? badge(current.status === 'done' ? 'ok' : current.status === 'failed' ? 'error' : 'info', current.status) : ''}
              <button class="btn small" data-action="run-copy">复制日志</button>
            </div>
          </div>
          <div class="log-panel">
            <div class="log-head"><span>logs/ui-runs/${current ? esc(current.id) : '…'}.log</span><span class="small">${running ? '运行中…' : current ? '已结束' : '待运行'}</span></div>
            <pre class="log-body" id="run-log">${esc(logText)}</pre>
          </div>
        </div>
        <div class="card">
          <div class="card-head"><div><h2>结构化摘要</h2><p>结束状态优先读取脚本写出的 summary JSON。</p></div></div>
          <div id="run-summary">${renderRunSummary(current)}</div>
        </div>
      </div>
    </div>
  `;
  const log = $('#run-log');
  if (log) log.scrollTop = log.scrollHeight;
}

function renderRunSummary(current) {
  if (!current) return '<div class="muted">还没有运行结果。</div>';
  if (current.status === 'running') return '<div class="muted">任务运行中，结束后显示新增条数、失败源、日报路径与邮件结果。</div>';
  const summary = current.summary || {};
  const items = [];
  const add = (key, value) => {
    if (value !== undefined && value !== null && value !== '') items.push([key, value]);
  };
  add('新增条目', summary.collect?.saved);
  add('跳过已有', summary.collect?.skipped_existing);
  add('文章数', summary.article_count);
  add('批次数', summary.batch_count);
  add('日报路径', summary.report_path);
  add('邮件', summary.mail?.status === 'sent' ? '已发送' : summary.mail?.status === 'skipped' ? '未发送' : summary.mail?.status);
  add('站点页数', summary.reports);
  add('站点目录', summary.site_dir);
  add('退出码', current.returncode);
  const failures = summary.collect?.failed_feeds || [];
  const failedHtml = failures.length
    ? `<div class="notice error mt-12"><strong>失败源（${failures.length}）：</strong><ul>${failures.map((item) => `<li>${esc(item.name || '')} · ${esc(item.error || '')}</li>`).join('')}</ul></div>`
    : '';
  const errorHtml = summary.error || current.error ? `<div class="notice error mt-12">${esc(summary.error || current.error)}</div>` : '';
  if (!items.length && !failures.length && !errorHtml) return '<div class="muted">任务完成，没有更多摘要。</div>';
  return `<div class="summary-grid">${items.map(([key, value]) => `<div class="summary-item"><div class="k">${esc(key)}</div><div class="v">${esc(value)}</div></div>`).join('')}</div>${failedHtml}${errorHtml}`;
}

async function startRun() {
  const mode = state.run.mode || 'collect';
  const body = {
    mode,
    date: $('#run-date')?.value || '',
    fetch: $('#run-fetch') ? $('#run-fetch').checked : true,
    send: $('#run-send') ? $('#run-send').checked : false,
  };
  const result = await api('/api/run', { method: 'POST', body });
  state.run = { id: result.id, offset: 0, timer: null, record: result, mode, log: '' };
  $('#run-log').textContent = '';
  renderRun();
  pollRun();
}

async function pollRun() {
  if (!state.run.id) return;
  try {
    const result = await api(`/api/run/${state.run.id}/log?offset=${state.run.offset}`);
    if (result.text) {
      state.run.log = (state.run.log || '') + result.text;
      const log = $('#run-log');
      if (log) {
        const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
        log.textContent = state.run.log;
        if (nearBottom) log.scrollTop = log.scrollHeight;
      }
    }
    state.run.offset = result.offset;
    state.run.record = result;
    if (!result.done) {
      state.run.timer = window.setTimeout(pollRun, 700);
      return;
    }
    state.run.timer = null;
    try {
      state.run.record = await api(`/api/run/${state.run.id}`);
    } catch (error) {
      state.run.record = result;
    }
    renderRun();
    state.history = (await api('/api/runs')).runs || [];
    if (state.tab === 'run') {
      const history = $('#tab-run .history-list');
      if (history) {
        history.innerHTML = state.history
          .map((item) => `<div class="history-item"><div><span class="badge ${item.status === 'done' ? 'ok' : item.status === 'failed' ? 'error' : 'info'}">${esc(item.mode_label || item.mode)}</span><span class="muted small"> ${esc(busyTime(item.started_at))}</span></div><div class="row"><span class="small muted">${esc(runSummaryLine(item.summary))}</span><button class="btn small" data-action="run-history" data-id="${attr(item.id)}">查看日志</button></div></div>`)
          .join('');
      }
    }
    toast(result.status === 'done' ? '任务完成' : result.status === 'failed' ? '任务失败，请查看日志' : '任务已停止', result.status === 'done' ? 'ok' : result.status === 'failed' ? 'error' : 'warn');
    refreshBootstrap().catch(() => {});
  } catch (error) {
    state.run.timer = null;
    toast(error.message, 'error', 6000);
  }
}

function stopRunPolling() {
  if (state.run.timer) {
    window.clearTimeout(state.run.timer);
    state.run.timer = null;
  }
}

async function stopRun() {
  if (!state.run.id) return;
  const ok = await confirmDialog({ title: '停止任务', message: '将向正在运行的子进程发送终止信号。确认停止？', confirmText: '停止', danger: true });
  if (!ok) return;
  await api(`/api/run/${state.run.id}/stop`, { method: 'POST', body: {} });
  toast('正在停止…', 'warn');
  pollRun();
}

async function loadHistoryRun(id) {
  stopRunPolling();
  const status = await api(`/api/run/${id}`);
  let text = '';
  let offset = 0;
  for (let page = 0; page < 20; page += 1) {
    const chunk = await api(`/api/run/${id}/log?offset=${offset}`);
    text += chunk.text || '';
    offset = chunk.offset || offset;
    if (chunk.done || !chunk.text) break;
  }
  state.run = { id, offset, timer: null, record: status, mode: status.mode || 'collect', log: text };
  renderRun();
  if (status.status === 'running') pollRun();
}

async function copyRunLog() {
  await copyText(state.run.log || $('#run-log')?.textContent || '');
}

/* -------------------------------------------------------------------------- */
/*  系统                                                                       */
/* -------------------------------------------------------------------------- */

async function loadSystem(force = false) {
  $('#tab-system').innerHTML = loadingHtml('正在读取路径、定时任务与数据概览…');
  const [settings, scheduler, rsshubDetail, data] = await Promise.all([
    api('/api/settings'),
    api('/api/scheduler'),
    api('/api/rsshub'),
    api('/api/data'),
  ]);
  state.settings = settings;
  state.scheduler = scheduler;
  state.rsshubDetail = rsshubDetail;
  state.data = data;
  state.archive.overview = data.archive;
  if (!state.integrations) {
    state.integrations = { rsshub: rsshubDetail.config, revisions: { rsshub_env: rsshubDetail.revision } };
  }
  if (!state.archive.date && data.archive?.dates?.length) {
    await loadArchiveDate(data.archive.dates[0].date, false);
  }
  renderSystem();
}

function settingItem(key) {
  return (state.settings?.settings?.items || []).find((item) => item.key === key) || { key, value: '', source: '', source_label: '', absolute: '', path_status: null };
}

function renderSystem() {
  if (!state.settings) {
    $('#tab-system').innerHTML = loadingHtml();
    return;
  }
  const settings = state.settings.settings || {};
  const items = settings.items || [];
  const scheduler = state.scheduler || {};
  const data = state.data || {};
  const db = data.db || {};
  const archive = state.archive.overview || data.archive || {};
  const writable = Boolean(state.token);
  const pathFields = [
    ['archive_dir', '归档目录', 'RSS 归档 Markdown 的根目录'],
    ['report_dir', '日报目录', '日报 Markdown 输出目录'],
    ['db_path', '去重数据库', 'SQLite 文件，记录已归档条目'],
    ['site_dir', '站点目录', 'build_site.py 输出的静态站点目录'],
  ];
  const pathInputs = pathFields
    .map(([key, label, hint]) => {
      const item = settingItem(`paths.${key}`);
      const status = item.path_status || {};
      return `<label class="field">
        <span class="label">${esc(label)} ${sourceTag(item.source, item.source_label)}</span>
        <input id="setting-path-${key}" type="text" value="${attr(item.value)}" ${writable ? '' : 'disabled'}>
        <span class="hint">${esc(hint)} · 解析后：<code>${esc(item.absolute || '')}</code></span>
        <span class="small ${status.writable ? 'muted' : 'field-error'}">${status.exists ? (status.writable ? '存在且可写' : `不可写：${esc(status.reason || '')}`) : esc(status.reason || '不存在')}</span>
      </label>`;
    })
    .join('');
  const envOverrides = items.filter((item) => item.source === 'environment');
  const timeout = settingItem('run.timeout');
  const fetchLimit = settingItem('run.fetch_limit');
  const dateOffset = settingItem('run.date_offset');
  const temperature = settingItem('model.temperature');
  const maxArticle = settingItem('model.max_chars_per_article');
  const maxBatch = settingItem('model.max_chars_per_batch');
  const hour = settingItem('schedule.hour');
  const minute = settingItem('schedule.minute');
  const healthHtml = renderHealthChecks();
  const schedulerBadge = scheduler.loaded ? badge('ok', '已加载') : scheduler.installed ? badge('warn', '已安装未加载') : badge('neutral', '未安装');
  const pathBadge = scheduler.path_valid === false ? badge('error', '路径失效') : scheduler.installed ? badge('ok', '路径有效') : '';

  const byFeedRows = (db.by_feed || [])
    .map(
      (item) => `<tr><td>${esc(item.display_name || item.feed_name)}<div class="small muted mono">${esc(item.feed_name)}</div></td><td>${item.count}</td><td class="small muted">${esc(item.last_saved_at || '')}</td><td><button class="btn small danger" data-action="data-reset" data-feed="${attr(item.feed_name)}" data-display="${attr(item.display_name || item.feed_name)}" ${writable ? '' : 'disabled'}>重置去重</button></td></tr>`,
    )
    .join('');

  const dates = archive.dates || [];
  const activeDate = state.archive.date;
  const dateButtons = dates
    .map((item) => `<button class="archive-date ${activeDate === item.date ? 'active' : ''}" data-action="archive-date" data-date="${attr(item.date)}"><span>${esc(item.date)}</span><span class="badge">${item.count}</span></button>`)
    .join('');
  const fileHtml = (state.archive.files || [])
    .map((file) => `<div class="list-item"><div><div class="title">${esc(file.title || file.name)}</div><div class="sub">${esc(file.feed)} · ${formatBytes(file.size)} · ${esc(file.mtime)}</div></div><button class="btn small" data-action="archive-file" data-path="${attr(file.path)}">预览</button></div>`)
    .join('');
  const preview = state.archive.active
    ? `<div class="row-between mb-12"><strong>${esc(state.archive.active.path)}</strong><span class="small muted">${formatBytes(state.archive.active.content.length)}</span></div><div class="preview">${renderMarkdown(state.archive.active.content)}</div>`
    : '<div class="preview preview-empty">选择左侧日期，再点「预览」查看 Markdown。</div>';

  $('#tab-system').innerHTML = `
    <div class="card">
      <div class="card-head"><div><h2>配置体检</h2><p>检查必填项、路径权限、连通性与定时任务路径。连通性检查会实际请求模型、SMTP 与 RSSHub。</p></div><button class="btn primary" data-action="health-run" ${writable ? '' : 'disabled'}>运行体检</button></div>
      <div id="health-status">${healthHtml}</div>
    </div>

    <div class="card mt-16">
      <div class="card-head"><div><h2>路径与运行参数</h2><p>保存到 config.json。CLI 参数仍可临时覆盖。</p></div><button class="btn primary" data-action="settings-save" ${writable ? '' : 'disabled'}>保存设置</button></div>
      ${envOverrides.length ? `<div class="notice warn mb-12">${envOverrides.map((item) => `环境变量 <code>${esc(item.env_name)}</code> 正在覆盖「${esc(item.label)}」，在界面保存后仍会以环境变量为准。`).join('<br>')}</div>` : ''}
      <div id="settings-errors"></div>
      <div class="grid grid-2">
        <div class="stack">${pathInputs}</div>
        <div class="stack">
          <div class="field-row">
            <label class="field"><span class="label">网络超时（秒） ${sourceTag(timeout.source, timeout.source_label)}</span><input id="setting-timeout" type="number" min="1" max="600" value="${attr(timeout.value)}"></label>
            <label class="field"><span class="label">每源抓取条数 ${sourceTag(fetchLimit.source, fetchLimit.source_label)}</span><input id="setting-fetch-limit" type="number" min="0" max="10000" value="${attr(fetchLimit.value)}"><span class="hint">0 表示不限制。</span></label>
          </div>
          <label class="field"><span class="label">默认日报日期偏移 ${sourceTag(dateOffset.source, dateOffset.source_label)}</span><input id="setting-date-offset" type="number" min="0" max="7" value="${attr(dateOffset.value)}"><span class="hint">0 = 今天，1 = 昨天。daily_report.py 与 daily_workflow.py 共用。</span></label>
          <div class="divider"></div>
          <div class="field-row three">
            <label class="field"><span class="label">temperature ${sourceTag(temperature.source, temperature.source_label)}</span><input id="setting-temperature" type="number" min="0" max="2" step="0.1" value="${attr(temperature.value)}"></label>
            <label class="field"><span class="label">单篇截断字符</span><input id="setting-max-article" type="number" min="500" max="200000" value="${attr(maxArticle.value)}"></label>
            <label class="field"><span class="label">单批字符上限</span><input id="setting-max-batch" type="number" min="1000" max="1000000" value="${attr(maxBatch.value)}"></label>
          </div>
          <div class="field-row">
            <label class="field"><span class="label">定时小时 ${sourceTag(hour.source, hour.source_label)}</span><input id="setting-hour" type="number" min="0" max="23" value="${attr(hour.value)}"></label>
            <label class="field"><span class="label">定时分钟 ${sourceTag(minute.source, minute.source_label)}</span><input id="setting-minute" type="number" min="0" max="59" value="${attr(minute.value)}"></label>
          </div>
          <div class="help-box">路径可以写相对项目目录的路径，也可以写绝对路径。保存前会检查目录是否存在、是否可写；不存在但上级可写时会提示“保存后自动创建”。</div>
        </div>
      </div>
    </div>

    <div class="card mt-16">
      <div class="card-head">
        <div><h2>定时任务</h2><p>macOS 使用 launchd；非 macOS 只显示 crontab 示例，不写系统配置。</p></div>
        <div class="row">${schedulerBadge} ${pathBadge}</div>
      </div>
      ${scheduler.is_macos ? '' : `<div class="notice info mb-12">当前平台 ${esc(scheduler.platform)}：不会自动写入系统定时配置。可复制下面的 crontab 示例手动安装。</div>`}
      <div class="field-row">
        <label class="field"><span class="label">执行时间</span><div class="row"><input id="scheduler-hour" type="number" min="0" max="23" value="${attr(scheduler.schedule?.hour ?? hour.value)}" style="max-width:90px"><span>:</span><input id="scheduler-minute" type="number" min="0" max="59" value="${attr(scheduler.schedule?.minute ?? minute.value)}" style="max-width:90px"></div></label>
        <label class="field"><span class="label">下次执行</span><input type="text" value="${attr(scheduler.next_run || '—')}" disabled></label>
      </div>
      <dl class="kv mt-12">
        <dt>plist 路径</dt><dd class="mono">${esc(scheduler.plist_path || '')}</dd>
        <dt>Program</dt><dd class="mono">${esc(scheduler.program || '—')}</dd>
        <dt>WorkingDirectory</dt><dd class="mono">${esc(scheduler.working_directory || '—')}</dd>
        <dt>Python</dt><dd class="mono">${esc(scheduler.python || '')}</dd>
      </dl>
      ${scheduler.path_valid === false ? `<div class="notice error mt-12">已安装的 plist 中路径不存在（项目可能被移动）。重新安装会用当前真实路径生成。</div>` : ''}
      ${scheduler.plist_error ? `<div class="notice error mt-12">${esc(scheduler.plist_error)}</div>` : ''}
      <div class="form-actions">
        ${scheduler.is_macos ? `
          <button class="btn" data-action="scheduler-action" data-mode="disable" ${writable && scheduler.loaded ? '' : 'disabled'}>停用</button>
          <button class="btn" data-action="scheduler-action" data-mode="enable" ${writable && scheduler.installed && !scheduler.loaded ? '' : 'disabled'}>启用</button>
          <button class="btn danger" data-action="scheduler-action" data-mode="uninstall" ${writable && scheduler.installed ? '' : 'disabled'}>卸载</button>
          <button class="btn primary" data-action="scheduler-action" data-mode="${scheduler.installed ? 'update' : 'install'}" ${writable ? '' : 'disabled'}>${scheduler.installed ? '更新并重新加载' : '安装定时任务'}</button>
        ` : `<button class="btn" data-action="copy-text" data-text="${attr(scheduler.crontab || '')}" ${writable ? '' : 'disabled'}>复制 crontab</button>`}
      </div>
      ${!scheduler.is_macos ? `<pre class="mono mt-12" style="white-space:pre-wrap;background:#f8fafc;border:1px solid var(--line);border-radius:8px;padding:12px">${esc(scheduler.crontab || '')}</pre>` : ''}
      ${scheduler.launchctl_output ? `<pre class="mono mt-12" style="white-space:pre-wrap;background:#f8fafc;border:1px solid var(--line);border-radius:8px;padding:12px;max-height:180px;overflow:auto">${esc(scheduler.launchctl_output)}</pre>` : ''}
      <div class="mt-12"><div class="small muted">最近日志（${esc(scheduler.last_log_path || '无')}）</div><pre class="mono mt-8" style="white-space:pre-wrap;background:#0b1220;color:#d1fae5;border-radius:8px;padding:12px;max-height:220px;overflow:auto">${esc(scheduler.last_log || '暂无日志')}</pre></div>
    </div>

    <div class="card mt-16">
      <div class="card-head"><div><h2>数据维护</h2><p>去重库概览、按源重置与归档浏览。</p></div><button class="btn" data-action="data-refresh">刷新</button></div>
      <div class="grid grid-3">
        <div class="stat"><div class="stat-label">总记录</div><div class="stat-value">${db.total ?? 0}</div><div class="stat-sub">${esc(db.path || '')}</div></div>
        <div class="stat"><div class="stat-label">文件大小</div><div class="stat-value">${formatBytes(db.size)}</div><div class="stat-sub">最近写入：${esc(db.last_saved_at || '—')}</div></div>
        <div class="stat"><div class="stat-label">归档文件</div><div class="stat-value">${archive.total_files ?? 0}</div><div class="stat-sub">${esc(archive.dir || '')}</div></div>
      </div>
      ${db.error ? `<div class="notice error mt-12">${esc(db.error)}</div>` : ''}
      <div class="table-wrap mt-16">
        <table><thead><tr><th>订阅源</th><th>记录数</th><th>最近写入</th><th>操作</th></tr></thead><tbody>${byFeedRows || '<tr><td colspan="4" class="muted">尚无去重记录。</td></tr>'}</tbody></table>
      </div>
      <div class="divider"></div>
      <div class="archive-layout">
        <div><div class="row-between mb-8"><strong>按日期浏览</strong><span class="small muted">${dates.length} 天</span></div><div class="archive-dates">${dateButtons || '<div class="muted small">暂无归档。</div>'}</div>
          <div class="mt-12"><div class="small muted mb-8">${activeDate ? esc(activeDate) : ''}</div><div class="list">${fileHtml || (activeDate ? '<div class="muted small">这一天没有 Markdown。</div>' : '')}</div></div>
        </div>
        <div>${preview}</div>
      </div>
    </div>

    <div class="card mt-16">
      <div class="card-head"><div><h2>站点构建与发布</h2><p>构建 docs/ 静态站点；发布前会严格排除 .env、.sqlite3、rss-downloads/ 与 logs/。</p></div></div>
      <div class="row">
        <button class="btn" data-action="site-build" ${writable ? '' : 'disabled'}>构建站点</button>
        <button class="btn" data-action="site-preview">本地预览</button>
        <button class="btn blue" data-action="publish" ${writable ? '' : 'disabled'}>确认式发布（git add reports docs → commit → push）</button>
      </div>
      ${state.publishOutput ? `<pre class="mono mt-12" style="white-space:pre-wrap;background:#f8fafc;border:1px solid var(--line);border-radius:8px;padding:12px;max-height:260px;overflow:auto">${esc(state.publishOutput)}</pre>` : ''}
    </div>
  `;
}

function renderHealthChecks() {
  if (!state.health) {
    return '<div class="notice info">尚未运行体检。点击右上角「运行体检」，会检查必填项、路径权限、连通性与定时任务路径。</div>';
  }
  const checks = state.health.checks || [];
  const rows = checks
    .map(
      (check) => `<div class="list-item"><div><div class="title">${esc(check.label)}</div><div class="sub">${esc(check.detail)}${check.fix ? `<div class="tiny">建议：${esc(check.fix)}</div>` : ''}</div></div>${badge(check.status, check.status === 'ok' ? '正常' : check.status === 'error' ? '错误' : check.status === 'warn' ? '注意' : '跳过')}</div>`,
    )
    .join('');
  return `<div class="row mb-12">${badge(state.health.ok ? 'ok' : 'error', state.health.ok ? '通过' : '有错误')}${badge('warn', `${state.health.warnings} 个注意项`)}</div><div class="list">${rows}</div>`;
}

async function runHealth() {
  const box = $('#health-status');
  if (box) box.innerHTML = '<div class="row muted">正在检查（含模型、SMTP、RSSHub 连通性）…</div>';
  state.health = await api('/api/health?connectivity=1');
  renderSystem();
  toast(state.health.ok ? '体检通过' : '体检发现错误，请查看清单', state.health.ok ? 'ok' : 'error');
}

async function saveSettings() {
  const paths = {
    archive_dir: $('#setting-path-archive_dir').value.trim(),
    report_dir: $('#setting-path-report_dir').value.trim(),
    db_path: $('#setting-path-db_path').value.trim(),
    site_dir: $('#setting-path-site_dir').value.trim(),
  };
  const body = {
    paths,
    run: {
      timeout: Number($('#setting-timeout').value),
      fetch_limit: Number($('#setting-fetch-limit').value),
      date_offset: Number($('#setting-date-offset').value),
    },
    advanced: {
      temperature: Number($('#setting-temperature').value),
      max_chars_per_article: Number($('#setting-max-article').value),
      max_chars_per_batch: Number($('#setting-max-batch').value),
    },
    schedule: {
      hour: Number($('#setting-hour').value),
      minute: Number($('#setting-minute').value),
    },
    config_revision: state.settings.revisions?.config,
  };
  try {
    const result = await api('/api/settings', { method: 'PUT', body });
    state.settings = result;
    if (state.bootstrap) state.bootstrap.settings = result.settings;
    state.scheduler = await api('/api/scheduler');
    state.data = await api('/api/data');
    state.archive.overview = state.data.archive;
    state.archive.date = '';
    state.archive.files = [];
    state.archive.active = null;
    if (state.data.archive?.dates?.length) await loadArchiveDate(state.data.archive.dates[0].date, false);
    renderSystem();
    toast('设置已保存到 config.json');
  } catch (error) {
    if (error.data?.errors?.length) {
      const box = $('#settings-errors');
      if (box) box.innerHTML = `<div class="notice error mb-12">${error.data.errors.map((item) => `${esc(item.field || '')}：${esc(item.message)}`).join('<br>')}</div>`;
    }
    throw error;
  }
}

async function schedulerAction(mode) {
  const labels = {
    install: '安装定时任务',
    update: '更新定时任务',
    disable: '停用定时任务',
    enable: '启用定时任务',
    uninstall: '卸载定时任务',
  };
  const confirmText = mode === 'uninstall' ? '卸载会停止 launchd 任务并删除 plist 文件。确认继续？' : '将修改 launchd 配置，确认继续？';
  const ok = await confirmDialog({ title: labels[mode] || '定时任务操作', message: confirmText, confirmText: '执行', danger: mode === 'uninstall' });
  if (!ok) return;
  const body = {
    action: mode,
    hour: Number($('#scheduler-hour')?.value ?? state.scheduler?.schedule?.hour ?? 4),
    minute: Number($('#scheduler-minute')?.value ?? state.scheduler?.schedule?.minute ?? 0),
    confirm: true,
  };
  const result = await api('/api/scheduler', { method: 'PUT', body });
  if (result.skipped) {
    toast(result.message || '当前平台不支持自动安装', 'warn', 7000);
  } else {
    toast(labels[mode] ? `${labels[mode]}完成` : '操作完成');
  }
  state.scheduler = result.scheduler || result;
  renderSystem();
  refreshBootstrap().catch(() => {});
}

async function resetFeedData(feed, display) {
  const row = (state.data?.db?.by_feed || []).find((item) => item.feed_name === feed);
  const count = row?.count ?? 0;
  const ok = await confirmDialog({
    title: '按源重置去重记录',
    message: `将删除「${display || feed}」的 ${count} 条去重记录。下次采集会把这些历史内容重新归档 ${count} 条；归档文件本身不会被删除。确认继续？`,
    confirmText: '重置',
    danger: true,
  });
  if (!ok) return;
  const result = await api('/api/data/reset', { method: 'POST', body: { feed, confirm: true } });
  state.data = result;
  state.archive.overview = result.archive;
  renderSystem();
  toast(`已删除 ${result.reset?.deleted_rows ?? count} 条去重记录`);
}

async function loadArchiveDate(date, rerender = true) {
  const result = await api(`/api/archive?date=${encodeURIComponent(date)}`);
  state.archive.date = date;
  state.archive.files = result.files || [];
  state.archive.active = null;
  if (rerender) renderSystem();
}

async function loadArchiveFile(path) {
  const result = await api(`/api/archive/file?path=${encodeURIComponent(path)}`);
  state.archive.active = result;
  renderSystem();
  const preview = $('.preview');
  if (preview) preview.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function publishSite() {
  const ok = await confirmDialog({
    title: '确认发布站点',
    message: '将执行 git add reports docs、git commit、git push。会自动拦截 .env、.env.rsshub、.sqlite3、rss-downloads/ 与 logs/。确认发布？',
    confirmText: '确认发布',
  });
  if (!ok) return;
  const result = await api('/api/publish', { method: 'POST', body: { confirm: true } });
  state.publishOutput = result.output || result.message || '';
  renderSystem();
  toast(result.skipped ? result.message : '已提交并推送', result.skipped ? 'warn' : 'ok');
}

async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const area = document.createElement('textarea');
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand('copy');
      area.remove();
    }
    toast('已复制到剪贴板');
  } catch (error) {
    toast('复制失败，请手动选择文本', 'error');
  }
}

/* -------------------------------------------------------------------------- */
/*  快捷运行、旧布局确认、弹窗与 Markdown 预览                                  */
/* -------------------------------------------------------------------------- */

async function quickRun(mode) {
  if (!state.token) {
    showTokenBanner();
    toast('请使用带 token 的启动链接后再运行', 'error');
    return;
  }
  const info = runModeInfo(mode);
  if (!info.enabled) {
    toast(info.reason || '该运行入口当前不可用', 'warn', 6000);
    return;
  }
  const send = mode === 'workflow' ? Boolean(state.bootstrap?.requirements?.mail?.ready) : false;
  const result = await api('/api/run', { method: 'POST', body: { mode, date: '', fetch: true, send } });
  state.run = { id: result.id, offset: 0, timer: null, record: result, mode, log: '' };
  await switchTab('run');
  toast('任务已启动', 'ok');
}

async function confirmLegacy() {
  const result = await api('/api/legacy/confirm', { method: 'POST', body: {} });
  if (state.bootstrap) state.bootstrap.legacy = result.legacy;
  closeModal();
  toast('已确认旧布局配置，可以开始保存。');
  await refreshBootstrap();
  if (state.tab === 'feeds') await loadFeeds();
  if (state.tab === 'integrations') await loadIntegrations();
  if (state.tab === 'system') await loadSystem();
}

function showLegacyModal(legacy) {
  if (state.legacyShown) return;
  state.legacyShown = true;
  const preview = legacy.preview || {};
  const feedLines = (preview.feeds || [])
    .slice(0, 40)
    .map((feed) => `<li><strong>${esc(feed.name)}</strong><span class="small muted"> · ${esc(feed.url)}</span></li>`)
    .join('');
  const model = preview.model || {};
  const mail = preview.mail || {};
  const rsshub = preview.rsshub || {};
  openModal({
    wide: true,
    title: '首次启动：确认将读取的配置',
    subtitle: '检测到旧布局（feeds.txt / .env / .env.rsshub），但还没有 config.json。确认后才会允许写入任何配置文件。',
    body: `
      <div class="notice info">界面不会自动迁移或改动文件；确认后仍会在你点击保存时写文件，并先备份。</div>
      <div class="grid grid-2 mt-12">
        <div class="card" style="box-shadow:none">
          <div class="card-head"><div><h2>feeds.txt</h2><p>${preview.feeds_count || 0} 个源</p></div></div>
          ${preview.feed_errors?.length ? `<div class="notice error">${esc(preview.feed_errors.join('；'))}</div>` : ''}
          <ul class="mt-8">${feedLines || '<li class="muted">未检测到 feeds.txt</li>'}</ul>
        </div>
        <div class="card" style="box-shadow:none">
          <div class="card-head"><div><h2>现有密钥与集成</h2><p>只显示掩码，不会在页面出现明文。</p></div></div>
          <dl class="kv">
            <dt>模型</dt><dd>${esc(model.model || '未配置')} · ${esc(model.base_url || '')}<br><span class="muted small">API Key：${esc(model.api_key || '未配置')}</span></dd>
            <dt>SMTP</dt><dd>${esc(mail.host || '未配置')}:${esc(mail.port || '')}<br><span class="muted small">密码：${esc(mail.password || '未配置')}</span></dd>
            <dt>收件人</dt><dd>${esc(mail.recipients_text || '未配置')}</dd>
            <dt>RSSHub</dt><dd>${esc(rsshub.host || '')}:${esc(rsshub.port || '')} · ${esc(rsshub.image || '')}</dd>
            <dt>config.json</dt><dd>${preview.config_exists ? '已存在' : '不存在，将使用默认值并在首次保存时创建'}</dd>
          </dl>
        </div>
      </div>
      <div class="help-box mt-12">生效优先级：CLI 参数 &gt; 环境变量 &gt; .env / .env.rsshub &gt; config.json &gt; 默认值。密钥继续留在 .env，不写入 config.json。</div>
    `,
    footer: `<button class="btn primary" data-action="legacy-confirm">确认并继续</button>`,
  });
}

let modalConfirmResolve = null;

function openModal({ title, subtitle = '', body = '', footer = '', wide = false }) {
  $('#modal-root').innerHTML = `
    <div class="modal-backdrop" data-modal-backdrop>
      <div class="modal ${wide ? 'wide' : ''}" role="dialog" aria-modal="true" aria-label="${attr(title)}">
        <div class="modal-head">
          <div><h2>${esc(title)}</h2>${subtitle ? `<p>${esc(subtitle)}</p>` : ''}</div>
          <button class="modal-close" data-action="modal-close" aria-label="关闭">×</button>
        </div>
        <div class="modal-body">${body}</div>
        <div class="modal-foot">${footer || '<button class="btn" data-action="modal-close">关闭</button>'}</div>
      </div>
    </div>`;
  const firstInput = $('#modal-root input, #modal-root textarea, #modal-root select');
  if (firstInput) window.setTimeout(() => firstInput.focus(), 30);
}

function closeModal() {
  $('#modal-root').innerHTML = '';
  if (modalConfirmResolve) {
    const resolve = modalConfirmResolve;
    modalConfirmResolve = null;
    resolve(false);
  }
}

function confirmDialog({ title, message, confirmText = '确认', danger = false }) {
  return new Promise((resolve) => {
    modalConfirmResolve = resolve;
    openModal({
      title,
      body: `<div style="white-space:pre-wrap">${esc(message)}</div>`,
      footer: `<button class="btn" data-action="confirm-cancel">取消</button><button class="btn ${danger ? 'danger' : 'primary'}" data-action="confirm-ok">${esc(confirmText)}</button>`,
    });
  });
}

/* -------------------------------------------------------------------------- */
/*  Markdown 预览（仅渲染转义后的安全子集）                                      */
/* -------------------------------------------------------------------------- */

function renderMarkdown(markdown) {
  let text = String(markdown || '').replace(/^---\n[\s\S]*?\n---\n/, '');
  const lines = text.split('\n');
  const html = [];
  let paragraph = [];
  let listType = null;
  let inCode = false;
  let codeLines = [];

  const inline = (raw) => {
    let value = esc(raw);
    value = value.replace(/`([^`]+)`/g, '<code>$1</code>');
    value = value.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    value = value.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, label, url) => {
      const clean = url.replaceAll('&amp;', '&');
      if (/^https?:\/\//i.test(clean)) {
        return `<a href="${esc(clean)}" target="_blank" rel="noreferrer noopener">${label}</a>`;
      }
      return label;
    });
    return value;
  };
  const flush = () => {
    if (paragraph.length) {
      html.push(`<p>${inline(paragraph.join(' ').trim())}</p>`);
      paragraph = [];
    }
  };
  const closeList = () => {
    if (listType) {
      html.push(`</${listType}>`);
      listType = null;
    }
  };

  for (const rawLine of lines) {
    const line = rawLine.replace(/\s+$/, '');
    if (inCode) {
      if (line.trim().startsWith('```')) {
        html.push(`<pre><code>${esc(codeLines.join('\n'))}</code></pre>`);
        inCode = false;
        codeLines = [];
      } else {
        codeLines.push(rawLine);
      }
      continue;
    }
    if (line.trim().startsWith('```')) {
      flush();
      closeList();
      inCode = true;
      codeLines = [];
      continue;
    }
    if (!line.trim()) {
      flush();
      closeList();
      continue;
    }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      flush();
      closeList();
      const level = heading[1].length;
      html.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }
    if (/^(-{3,}|\*{3,})$/.test(line.trim())) {
      flush();
      closeList();
      html.push('<hr>');
      continue;
    }
    const unordered = /^\s*[-*]\s+(.+)$/.exec(line);
    if (unordered) {
      flush();
      if (listType !== 'ul') {
        closeList();
        html.push('<ul>');
        listType = 'ul';
      }
      html.push(`<li>${inline(unordered[1])}</li>`);
      continue;
    }
    const ordered = /^\s*\d+\.\s+(.+)$/.exec(line);
    if (ordered) {
      flush();
      if (listType !== 'ol') {
        closeList();
        html.push('<ol>');
        listType = 'ol';
      }
      html.push(`<li>${inline(ordered[1])}</li>`);
      continue;
    }
    const quote = /^>\s?(.*)$/.exec(line);
    if (quote) {
      flush();
      closeList();
      html.push(`<blockquote>${inline(quote[1])}</blockquote>`);
      continue;
    }
    paragraph.push(line.trim());
  }
  flush();
  closeList();
  if (inCode) html.push(`<pre><code>${esc(codeLines.join('\n'))}</code></pre>`);
  return html.join('\n') || '<div class="preview-empty">内容为空</div>';
}

/* -------------------------------------------------------------------------- */
/*  事件补充                                                                   */
/* -------------------------------------------------------------------------- */

async function copyExportText() {
  const area = $('#export-text');
  if (area) await copyText(area.value);
}
