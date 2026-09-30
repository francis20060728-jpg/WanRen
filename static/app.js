const state = {
  view: 'workspace',
  search: '',
  accounts: [],
  sessions: [],
  articles: [],
  templates: [],
  audits: [],
  users: [],
  systemHealth: null,
  dashboard: null,
  editingTemplate: null,
  templateVersions: [],
  currentSession: null,
  currentAssets: [],
  allAssets: [],
  assetExpanded: (() => { try { return new Set(JSON.parse(localStorage.getItem('wanren_asset_expanded') || '[]')); } catch { return new Set(); } })(),
  assetUploadTarget: null, // {sessionId, articleId} 素材库分组上传目标
  assetAutoExpanded: false, // 当前文章账号只自动展开一次
  pendingGeneration: null, // {sessionId, instruction} 新对话生成中状态（刷新可恢复）
  pendingTimer: null,
  showPublishedTree: (() => { try { return !!localStorage.getItem('wanren_tree_show_published'); } catch { return false; } })(),
  importDrafts: [],
  currentArticleId: null,
  pendingAttachments: [],
  aiHighlightDismissedVersion: null,
  currentStage: null,
  articleMessages: [],
  articleChatBusy: false,
  articleImmersive: false,
  articleEditorDirty: false,
  articleEditorSaving: false,
  articleEditorTimer: null,
  articleEditorSavePromise: null,
  articleEditorAnchor: null,
  monitorWorkflows: { items: [], page: 1, page_size: 10, total: 0, total_pages: 1 },
  auditPage: { items: [], page: 1, page_size: 10, total: 0, total_pages: 1 },
  articleStatusFilter: 'ALL',
  token: null,
  user: null,
  authConfig: { registration_enabled: true, registration_role: 'EDITOR' },
  theme: 'mo',
};

const viewMeta = {
  workspace: ['工作台', '从素材到人工审核、公众号草稿与发布的完整闭环'],
  process: ['生产流程', '查看当前任务整体流程、所处阶段、节点进度和阶段内容'],
  sessions: ['内容任务', '按任务查看状态与产出'],
  articles: ['文章审核', '所有任务产出的文章版本'],
  assets: ['素材', '当前任务的文件、图片、视频与视觉结果'],
  accounts: ['公众号', '多账号配置与正文样式'],
  styles: ['样式模版', '8 种内置排版气质与导入的自定义样式'],
  monitor: ['监控', '队列、运行轨迹、健康度与审计'],
  users: ['用户权限', '工作区成员、角色与操作边界'],
  settings: ['设置', '视觉分析、外观与模型能力'],
};

const processStages = [
  { key: 'session', title: '创建任务', state: 'done', detail: '任务基本信息、目标公众号、目标读者、语气与硬性要求。', action: 'workspace' },
  { key: 'assets', title: '上传与解析', state: 'done', detail: '文本、表格、PDF、DOCX、图片与视频进入 Session 素材池，并完成安全校验和基础解析。', action: 'assets' },
  { key: 'context', title: '理解上下文', state: 'pending', detail: 'Context Manager 汇总任务 Brief、结构化事实、图片视觉结果和视频信息。', action: 'assets' },
  { key: 'generate', title: '生成文章', state: 'pending', detail: '后台队列执行文本适配器，生成标题、摘要、结构化段落与素材引用。', action: 'workspace' },
  { key: 'review', title: 'AI 审核', state: 'pending', detail: '规则引擎检查禁用表达、长度、来源引用、标题摘要限制，并输出可定位问题。', action: 'workspace' },
  { key: 'human', title: '人工审核', state: 'pending', detail: '编辑、重审、通过或驳回；只有人工通过后才能进入微信链路。', action: 'workspace' },
  { key: 'wechat', title: '微信草稿', state: 'pending', detail: '按公众号样式渲染正文，上传封面与正文图片，创建微信草稿。', action: 'articles' },
  { key: 'publish', title: '发布', state: 'pending', detail: '提交发布任务并记录外部 ID；真实微信结果需等待回调或状态查询。', action: 'monitor' },
];

const $ = id => document.getElementById(id);

function can(permission) {
  return Boolean(state.user?.permissions?.includes(permission));
}

async function api(path, options = {}) {
  const config = { headers: {}, ...options };
  if (state.token) config.headers.Authorization = `Bearer ${state.token}`;
  if (config.body && !(config.body instanceof FormData)) {
    config.headers['Content-Type'] = 'application/json';
    config.body = JSON.stringify(config.body);
  }
  const response = await fetch(path, config);
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!response.ok) {
    const error = new Error(data.detail || response.statusText);
    error.status = response.status;
    throw error;
  }
  return data;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => (
    { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]
  ));
}

function toast(message, error = false) {
  const node = $('toast');
  node.textContent = message;
  node.style.background = error ? '#a63a2f' : '#1d2528';
  node.style.display = 'block';
  setTimeout(() => { node.style.display = 'none'; }, 3600);
}

function statusClass(value) {
  if (/FAIL|REJECT|ERROR/i.test(value)) return 'bad';
  if (/QUEUED|REVISE|WAIT|PENDING|RUNNING/i.test(value)) return 'warn';
  return '';
}

function formatDate(value) { return new Date(value).toLocaleString(); }

function matchSearch(...values) {
  if (!state.search) return true;
  const query = state.search.toLowerCase();
  return values.some(value => String(value ?? '').toLowerCase().includes(query));
}

function switchView(view) {
  state.view = view;
  setArticleImmersive(false);
  Object.keys(viewMeta).forEach(key => { $(`${key}-view`).hidden = key !== view; });
  document.querySelectorAll('.nav-button').forEach(button => {
    button.classList.toggle('active', button.dataset.view === view);
  });
  $('view-title').textContent = viewMeta[view][0];
  $('view-sub').textContent = viewMeta[view][1];
  renderCurrentView();
}

async function refreshDashboard() {
  state.dashboard = await api('/api/dashboard');
  const d = state.dashboard;
  $('m-drafts').textContent = d.drafts;
  $('m-waiting').textContent = d.waiting_human;
  $('m-queued').textContent = d.queued;
  $('m-running').textContent = d.running;
  $('m-scheduled').textContent = d.scheduled;
  $('m-tokens').textContent = d.ai_tokens.toLocaleString();
  $('m-ai-cost').textContent = `$${d.ai_cost.toFixed(8)}`;
  $('m-ai-budget').textContent = d.ai_monthly_budget_usd > 0
    ? `本月 $${d.ai_cost_current_month.toFixed(8)} / $${d.ai_monthly_budget_usd.toFixed(2)}`
    : `本月 $${d.ai_cost_current_month.toFixed(8)} · 未设预算`;
  $('health-score').textContent = d.health;
  $('monitor-health').textContent = d.health;
  $('monitor-queued').textContent = d.queued;
  $('monitor-running').textContent = d.running;
  $('workflow-list').innerHTML = d.recent_workflows.length ? d.recent_workflows.map(item => `
    <div class="row"><div><div class="row-title">${escapeHtml(item.session_title)}</div>
    <div class="muted">${escapeHtml(item.workflow_type)} · ${formatDate(item.started_at)}</div></div>
    <span class="status ${statusClass(item.status)}">${escapeHtml(item.status)}</span></div>`).join('')
    : '<div class="empty">还没有任务运行记录</div>';
  renderMonitorWorkflows();
}

async function refreshAccounts() {
  state.accounts = await api('/api/accounts');
  const preferredAccount = state.accounts.find(a => a.token_status === 'READY')
    || state.accounts.find(a => a.token_status === 'MOCK')
    || state.accounts[0];
  const accountOptions = state.accounts.map(a => `<option value="${a.id}">${escapeHtml(a.name)}</option>`).join('');
  $('session-account').innerHTML = accountOptions;
  const modalAccount = $('new-chat-account');
  if (modalAccount) modalAccount.innerHTML = accountOptions || '<option value="">（请先在「公众号」中添加账号）</option>';
  if (preferredAccount) $('session-account').value = preferredAccount.id;
  renderAccounts();
}

async function refreshTemplates() {
  // 内容架构模板已下线：架构由大模型现场提案（样式模版见 /api/typesetting-styles）
  state.templates = [];
}

async function refreshSessions() {
  state.sessions = await api('/api/sessions');
  renderSessions();
}

async function refreshArticles() { state.articles = await api('/api/articles'); renderArticles(); }

async function refreshAudits() {
  if (!can('audit.read')) {
    state.auditPage = { items: [], page: 1, page_size: Number($('audit-page-size')?.value || 10), total: 0, total_pages: 1 };
    $('monitor-audits').textContent = '0';
    renderAudits();
    return;
  }
  const pageSize = Number($('audit-page-size')?.value || 10);
  state.auditPage = await api(`/api/audit-logs/page?page=${state.auditPage.page || 1}&page_size=${pageSize}`);
  $('monitor-audits').textContent = String(state.auditPage.total);
  renderAudits();
}

async function refreshMonitorWorkflows() {
  const pageSize = Number($('workflow-page-size')?.value || 10);
  state.monitorWorkflows = await api(`/api/workflows?page=${state.monitorWorkflows.page || 1}&page_size=${pageSize}`);
  renderMonitorWorkflows();
}

async function refreshSystemHealth() {
  state.systemHealth = await api('/api/system/health');
  renderSystemHealth();
}

async function refreshUsers() {
  if (!can('user.manage')) {
    state.users = [];
    renderUsers();
    return;
  }
  state.users = await api('/api/users');
  renderUsers();
}

function renderSessions() {
  const rows = state.sessions.filter(item => matchSearch(item.title, item.goal, item.article_title, item.id));
  $('session-list').innerHTML = rows.length ? rows.map(item => `
    <div class="row"><div><div class="row-title">${escapeHtml(item.title)}</div>
    <div class="muted">${formatDate(item.created_at)} · ${escapeHtml(item.status)} · 文章：${escapeHtml(item.article_title || '未生成')}</div></div>
    <div class="actions"><button class="btn small primary" onclick="selectSession('${item.id}')">打开任务</button></div></div>`).join('')
    : '<div class="empty">没有匹配的任务</div>';
}

function renderArticles() {
  const rows = state.articles.filter(item =>
    (state.articleStatusFilter === 'ALL' || item.status === state.articleStatusFilter)
    && matchSearch(item.title, item.summary, item.session_title, item.account_name, item.status)
  );
  $('article-total').textContent = String(rows.length);
  const statusLabels = {
    DRAFT: '本地草稿',
    AI_REVIEWED: '待人工审核',
    NEEDS_REVISION: '待修订',
    HUMAN_APPROVED: '人工已通过',
    WECHAT_DRAFT: '微信草稿',
    PUBLISHED: '已发布',
    REJECTED: '已驳回',
  };
  $('article-list').innerHTML = rows.length ? `<div class="article-grid">${rows.map(item => {
    const cover = item.preview_asset_id
      ? `<img src="/api/assets/${escapeHtml(item.preview_asset_id)}/file" alt="${escapeHtml(item.title)}封面预览" loading="lazy" />`
      : '<span class="article-cover-fallback">暂无封面</span>';
    return `
      <article class="article-card">
        <div class="article-cover">${cover}<span class="status ${statusClass(item.status)} article-card-status">${escapeHtml(statusLabels[item.status] || item.status)}</span></div>
        <div class="article-card-body">
          <strong class="article-card-title">${escapeHtml(item.title)}</strong>
          <p class="article-card-summary">${escapeHtml(item.summary || '暂无摘要')}</p>
          <div class="article-card-meta">${escapeHtml(item.account_name)}<br>创建 ${formatDate(item.created_at)}<br>更新 ${formatDate(item.updated_at)}</div>
          <div class="actions" style="margin-top:auto"><button class="btn small primary" onclick="openArticle('${item.id}')">进入审核</button></div>
        </div>
      </article>`;
  }).join('')}</div>` : '<div class="empty">没有匹配的文章</div>';
}

function renderAssetLibrary() {
  const rows = (state.allAssets || []).filter(item => {
    const metadata = JSON.parse(item.metadata_json || '{}');
    return !metadata.derivative_type && matchSearch(item.file_name, item.asset_type, item.processing_status, item.account_name || '', item.article_title || '');
  });
  // 两级结构：账号（默认折叠）→ 对话组（展开账号后可见）
  const groups = new Map();
  for (const asset of rows) {
    const account = asset.account_name || '未分组公众号';
    const dialog = asset.article_id ? (asset.article_title || '未命名对话') : '任务公共素材（生成前上传）';
    if (!groups.has(account)) groups.set(account, new Map());
    const dlg = groups.get(account);
    if (!dlg.has(dialog)) dlg.set(dialog, { sessionId: asset.session_id, articleId: asset.article_id, items: [] });
    dlg.get(dialog).items.push(asset);
  }
  const renderVision = asset => {
    if (!asset.vision_result || asset.vision_result === '{}') return '';
    let parsed = null;
    try { parsed = JSON.parse(asset.vision_result); } catch { parsed = null; }
    if (parsed && parsed.description) {
      const score = parsed.quality_score != null ? ` · 质量 ${parsed.quality_score}` : '';
      const risks = Array.isArray(parsed.risk_flags) && parsed.risk_flags.length ? ` · ⚠ ${parsed.risk_flags.length} 项风险` : '';
      return `<details class="vision-details"><summary>👁 ${escapeHtml(String(parsed.description).slice(0, 46))}${escapeHtml(score + risks)}… 展开详情</summary><pre>${escapeHtml(JSON.stringify(parsed, null, 2))}</pre></details>`;
    }
    return `<details class="vision-details"><summary>👁 视觉分析结果 展开详情</summary><pre>${escapeHtml(String(asset.vision_result))}</pre></details>`;
  };
  const renderItem = asset => {
    const metadata = JSON.parse(asset.metadata_json || '{}');
    const thumbnail = asset.asset_type === 'VIDEO' && metadata.thumbnail_asset_id
      ? `<img class="asset-thumb" src="/api/assets/${metadata.thumbnail_asset_id}/file" alt="${escapeHtml(asset.file_name)}封面帧" />` : '';
    return `<div class="row"><div><div class="row-title">${escapeHtml(asset.file_name)}</div>
    <div class="muted">${escapeHtml(asset.asset_type)} · ${escapeHtml(asset.processing_status)} · ${(asset.size_bytes / 1024).toFixed(1)} KB</div>${renderVision(asset)}</div>
    <div class="actions">${thumbnail}${['IMAGE', 'VIDEO'].includes(asset.asset_type) && can('vision.run') ? `<button class="btn small" onclick="analyzeAsset('${asset.id}')">◎ 视觉分析</button>` : ''}${asset.asset_type === 'IMAGE' ? `<a class="btn small" href="/api/assets/${asset.id}/file" target="_blank">查看</a>` : ''}</div></div>`;
  };
  // 当前打开文章所属账号首次自动展开一次；此后完全尊重用户手动折叠/展开
  let currentAccount = null;
  if (state.currentArticleId) {
    const hit = (state.allAssets || []).find(x => x.article_id === state.currentArticleId);
    if (hit) currentAccount = hit.account_name || '未分组公众号';
  }
  const expanded = state.assetExpanded || new Set();
  if (!state.assetAutoExpanded && currentAccount && groups.has(currentAccount) && !expanded.has(currentAccount)) {
    expanded.add(currentAccount);
    state.assetAutoExpanded = true;
    try { localStorage.setItem('wanren_asset_expanded', JSON.stringify([...expanded])); } catch { /* 忽略 */ }
  }
  // 搜索时自动展开全部（避免折叠挡住结果）
  const searching = !!state.search;
  const html = groups.size ? [...groups.entries()].map(([account, dialogs]) => {
    const total = [...dialogs.values()].reduce((n, d) => n + d.items.length, 0);
    const isOpen = searching || expanded.has(account);
    return `
    <div class="asset-account-card${isOpen ? ' open' : ''}">
      <button type="button" class="asset-account-head" onclick="toggleAssetAccount(this)" data-account="${escapeHtml(account)}" aria-expanded="${isOpen}">
        <span class="chevron-icon" aria-hidden="true"><svg viewBox="0 0 16 16" width="13" height="13"><path d="M6 3.5 11 8l-5 4.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
        <span class="asset-account-name">◈ ${escapeHtml(account)}</span>
        <span class="status">${total}</span>
      </button>
      <div class="asset-account-body"${isOpen ? '' : ' hidden'}>
        ${[...dialogs.entries()].map(([dialog, info]) => `
        <div class="asset-dialog-group">
          <div class="asset-dialog-head">
            <span title="${escapeHtml(dialog)}">${escapeHtml(dialog)} <small class="muted">(${info.items.length})</small></span>
            <button type="button" class="btn small" onclick="uploadToGroup('${info.sessionId}', ${info.articleId ? `'${info.articleId}'` : 'null'})">＋ 上传到此对话</button>
          </div>
          ${info.items.map(renderItem).join('')}
        </div>`).join('')}
      </div>
    </div>`;
  }).join('') : '<div class="empty">暂无素材</div>';
  $('asset-library').innerHTML = html;
}

function toggleAssetAccount(btn) {
  const account = btn.dataset.account;
  if (!state.assetExpanded) state.assetExpanded = new Set();
  if (state.assetExpanded.has(account)) state.assetExpanded.delete(account);
  else state.assetExpanded.add(account);
  try { localStorage.setItem('wanren_asset_expanded', JSON.stringify([...state.assetExpanded])); } catch { /* 忽略 */ }
  renderAssetLibrary();
}

async function loadAllAssets() {
  state.allAssets = await api('/api/assets');
}

function uploadToGroup(sessionId, articleId) {
  state.assetUploadTarget = { sessionId, articleId };
  const input = $('asset-input');
  input.value = '';
  input.click();
}


function renderAccounts() {
  $('account-total').textContent = String(state.accounts.length);
  $('account-list').innerHTML = state.accounts.map(account => {
    const settings = JSON.parse(account.settings || '{}');
    const style = settings.style || {};
    return `<div class="row"><div><div class="row-title">${escapeHtml(account.name)}</div>
      <div class="muted">AppID ${escapeHtml(account.app_id)} · ${escapeHtml(account.token_status)} · 主色 ${escapeHtml(style.accent || '默认')}</div>
      <div class="muted">SecretEnv ${escapeHtml(account.credential_env || '')}</div>
      <div class="muted">TokenEnv ${escapeHtml(account.callback_token_env || '')}</div>
      ${account.callback_url ? `<div class="muted">Callback ${escapeHtml(account.callback_url)}</div>` : ''}</div>
      <div class="actions"><span class="status ${statusClass(account.token_status)}">${escapeHtml(account.token_status)}</span>
      ${can('account.manage') ? `<button class="btn small" onclick="renameAccount('${account.id}')">✎ 同步名称</button><button class="btn small" onclick="verifyAccount('${account.id}')">◎ 检测连接</button>` : ''}</div></div>`;
  }).join('');
}

function renderTemplates() {
  $('template-total').textContent = String(state.templates.length);
  $('template-list').innerHTML = state.templates.length ? state.templates.map(template => {
    const sections = JSON.parse(template.structure_json || '[]');
    const references = JSON.parse(template.references_json || '[]');
    return `<div class="row"><div><div class="row-title">${escapeHtml(template.name)}</div>
      <div class="muted">${escapeHtml(template.source)} · ${escapeHtml(template.official_account_name || '全部账号')} · v${template.version}</div>
      <div class="muted">${escapeHtml(template.description || '')}</div>
      <div class="issues" style="margin-top:8px">${sections.map(section => `<div class="issue"><strong>${escapeHtml(section.heading)}</strong><div>${escapeHtml(section.guidance || '')} · 图片：${escapeHtml(section.image_policy || 'optional')}</div></div>`).join('')}</div></div>
      ${references.length ? `<div class="issues" style="margin-top:8px">${references.map(reference => `<div class="issue"><strong>来源：${escapeHtml(reference.title || reference.provider || 'web')}</strong><div><a href="${escapeHtml(reference.url || '#')}" target="_blank" rel="noopener noreferrer">${escapeHtml(reference.url || '无链接')}</a><div>${escapeHtml(reference.snippet || '')}</div></div></div>`).join('')}</div>` : ''}
      <div class="actions">
        <button class="btn small primary" onclick="useTemplate('${template.id}')">⇥ 新任务使用</button>
        ${template.source !== 'BUILTIN' && can('article.generate') ? `<button class="btn small" onclick="editTemplate('${template.id}')">✎ 编辑</button>` : ''}
        <button class="btn small" onclick="showTemplateHistory('${template.id}')">⏱ 历史</button>
      </div></div>`;
  }).join('') : '<div class="empty">暂无模板</div>';
}

function renderTemplateVersions() {
  const panel = $('template-history-panel');
  panel.hidden = !state.templateVersions.length;
  $('template-version-list').innerHTML = state.templateVersions.map(version => {
    const sections = JSON.parse(version.structure_json || '[]');
    return `<div class="row"><div><div class="row-title">v${version.version} · ${escapeHtml(version.name)}</div>
      <div class="muted">${formatDate(version.created_at)} · ${escapeHtml(version.change_summary || '')} · ${sections.length} 个章节</div></div>
      <span class="status">${escapeHtml(version.source)}</span></div>`;
  }).join('') || '<div class="empty">暂无历史版本</div>';
}

function renderAudits() {
  const page = state.auditPage;
  $('audit-list').innerHTML = page.items.length ? page.items.map(item => `
    <div class="audit-row"><span>${formatDate(item.created_at)}</span>
    <span>${escapeHtml(item.action)} · ${escapeHtml(item.resource_type)} · ${escapeHtml(item.resource_id)}</span>
    <span>${escapeHtml(item.actor)}</span></div>`).join('') : '<div class="empty">暂无审计记录</div>';
  $('audit-page-info').textContent = `${page.page} / ${page.total_pages || 1} · ${page.total} 条`;
  $('audit-prev-page').disabled = page.page <= 1;
  $('audit-next-page').disabled = page.page >= (page.total_pages || 1);
}

async function renameAccount(accountId) {
  const account = state.accounts.find(item => item.id === accountId);
  if (!account) throw new Error('公众号不存在');
  const name = window.prompt('输入微信后台当前公众号名称', account.name);
  if (!name || name.trim().length < 2) return;
  const updated = await api(`/api/accounts/${accountId}`, {
    method: 'PATCH',
    body: { name: name.trim() },
  });
  toast(`公众号名称已同步：${updated.name}`);
  await refreshAccounts();
  await refreshSessions();
  await refreshArticles();
}

function renderUsers() {
  $('user-total').textContent = String(state.users.length);
  $('user-list').innerHTML = state.users.length ? state.users.map(user => `
    <div class="row"><div><div class="row-title">${escapeHtml(user.name)}</div>
    <div class="muted">${escapeHtml(user.email)} · 最近登录 ${user.last_login_at ? formatDate(user.last_login_at) : '从未登录'}</div></div>
    <span class="status">${escapeHtml(user.role)}</span></div>`).join('')
    : '<div class="empty">暂无用户</div>';
}

function renderSystemHealth() {
  const health = state.systemHealth;
  if (!health) return;
  const names = {
    database: '数据库',
    storage: '对象存储',
    queue: '任务队列',
    workflow: '工作流',
    ai: 'AI 调用',
    publisher: '发布适配',
    auth: '认证权限',
  };
  $('system-health-status').textContent = `${health.status} · ${health.score}`;
  $('system-health-status').className = `status ${statusClass(health.status === 'HEALTHY' ? 'DONE' : 'RUNNING')}`;
  $('monitor-health').textContent = String(health.score);
  $('health-component-list').innerHTML = Object.entries(health.components).map(([key, value]) => `
    <div class="component-tile"><strong>${names[key] || key}</strong>
    <span class="status ${statusClass(value.status === 'HEALTHY' || value.status === 'READY' || value.status === 'CONFIGURED' ? 'DONE' : 'DEGRADED')}">${escapeHtml(value.status)}</span>
    <div class="muted">${key === 'queue' ? `等待 ${value.queued} · 运行 ${value.running} · 卡住 ${value.stale_running} · 定时 ${value.scheduled}` : key === 'workflow' ? `成功 ${value.success}/${value.total}` : key === 'ai' ? `${escapeHtml(value.provider)}/${escapeHtml(value.model)} · ${value.runs} 次 · ${value.tokens.toLocaleString()} tokens` : key === 'publisher' ? `${escapeHtml(value.mode)} · 回调 ${value.callback_processed}/${value.callback_events} · 失败 ${value.callback_failed} · 重复 ${value.callback_duplicates}` : escapeHtml(value.detail || value.mode || '')}</div></div>`).join('');
  $('alert-list').innerHTML = health.alerts.length ? health.alerts.map(alert => `
    <div class="alert-item ${alert.severity === 'CRITICAL' ? 'critical' : ''}"><strong>${escapeHtml(alert.code)} · ${escapeHtml(alert.severity)}</strong>
    <div>${escapeHtml(alert.message)}</div><div class="muted">${escapeHtml(alert.suggestion)}</div></div>`).join('')
    : '<div class="empty">暂无告警</div>';
}

function renderMonitorWorkflows() {
  const page = state.monitorWorkflows;
  const rows = page.items || [];
  $('monitor-workflows').innerHTML = rows.length ? rows.map(item => `
    <div class="row"><div><div class="row-title">${escapeHtml(item.session_title)}</div>
    <div class="muted">${escapeHtml(item.workflow_type)} · ${formatDate(item.started_at)}</div></div>
    <div class="actions"><span class="status ${statusClass(item.status)}">${escapeHtml(item.status)}</span>
    <button class="btn small" onclick="showWorkflow('${item.id}')">节点</button></div></div>`).join('')
    : '<div class="empty">暂无任务运行</div>';
  $('workflow-page-info').textContent = `${page.page} / ${page.total_pages || 1} · ${page.total} 条`;
  $('workflow-prev-page').disabled = page.page <= 1;
  $('workflow-next-page').disabled = page.page >= (page.total_pages || 1);
}

function deriveProcess(detail) {
  const session = detail.session;
  const assets = detail.assets;
  const article = detail.articles[0] || null;
  const latestRun = detail.workflows[0] || null;
  const scheduledJob = (detail.publish_jobs || []).find(job => job.status === "SCHEDULED") || null;
  const publishedJob = (detail.publish_jobs || []).find(job => job.article_url) || null;
  const stages = processStages.map(stage => ({ ...stage, state: 'pending' }));
  const update = (key, value) => { const stage = stages.find(item => item.key === key); if (stage) stage.state = value; };
  update('session', 'done');
  if (assets.length) update('assets', 'done');
  if (assets.some(asset => asset.processing_status === 'VISION_READY')) update('context', 'done');
  if (latestRun) {
    if (latestRun.status === 'RUNNING') update('generate', 'running');
    if (latestRun.status === 'SUCCESS') { update('generate', 'done'); update('context', 'done'); }
    if (latestRun.status === 'FAILED') update('generate', 'failed');
  }
  if (article) {
    update('review', 'done');
    if (['NEEDS_REVISION', 'AI_REVIEWED'].includes(article.status)) update('human', 'running');
    if (article.status === 'HUMAN_APPROVED') update('human', 'done');
    if (article.status === 'HUMAN_APPROVED' && scheduledJob) {
      update('human', 'done');
      update('wechat', 'running');
    }
    if (article.status === 'WECHAT_DRAFT') { update('human', 'done'); update('wechat', 'done'); }
    if (['PUBLISHING', 'PUBLISHED'].includes(article.status)) {
      update('human', 'done'); update('wechat', 'done'); update('publish', article.status === 'PUBLISHED' ? 'done' : 'running');
    }
    if (article.status === 'REJECTED') update('human', 'failed');
  }
  return { session, assets, article, latestRun, scheduledJob, publishedJob, stages };
}

function renderProcess(detail = null) {
  if (!detail) {
    $('process-current').innerHTML = '<div class="empty">请先选择一个内容任务</div>';
    $('process-stage-list').innerHTML = '';
    $('process-stage-detail').innerHTML = '';
    return;
  }
  const process = deriveProcess(detail);
  const currentIndex = process.stages.findIndex(stage => ['running', 'pending', 'failed'].includes(stage.state));
  const activeIndex = currentIndex === -1 ? process.stages.length - 1 : currentIndex;
  const current = state.currentStage || process.stages[activeIndex].key;
  const completed = process.stages.filter(stage => stage.state === 'done').length;
  const percent = Math.round(completed / process.stages.length * 100);
  $('process-current').innerHTML = `
    <div class="row"><div><div class="row-title">${escapeHtml(process.session.title)}</div>
    <div class="muted">整体进度 ${percent}% · ${completed}/${process.stages.length} 阶段完成 · 素材 ${process.assets.length} 个</div></div>
    <span class="status ${statusClass(process.session.status)}">${escapeHtml(process.session.status)}</span></div>`;
  $('process-stage-list').innerHTML = `<div class="process-grid">${process.stages.map((stage, index) => `
    <button type="button" class="stage-card ${stage.state} ${stage.key === current ? 'current' : ''}" onclick="showProcessStage('${stage.key}')">
      <span class="stage-index">${stage.state === 'done' ? '✓' : index + 1}</span>
      <span class="stage-title">${escapeHtml(stage.title)}</span>
      <span class="stage-copy">${escapeHtml(stage.detail)}</span>
      <span class="status ${statusClass(stage.state === 'running' ? 'RUNNING' : stage.state === 'failed' ? 'FAILED' : stage.state === 'done' ? 'DONE' : 'PENDING')}">${escapeHtml(stage.state)}</span>
    </button>`).join('')}</div>`;
  showProcessStage(current, detail);
}

function showProcessStage(stageKey, detailOverride = null) {
  state.currentStage = stageKey;
  const detail = detailOverride || (state.currentSession ? state.currentSessionDetail : null);
  if (!detail) return;
  state.currentSessionDetail = detail;
  const process = deriveProcess(detail);
  const stage = process.stages.find(item => item.key === stageKey) || process.stages[0];
  const runNodes = stage.key === 'generate' || stage.key === 'review'
    ? `<div class="muted">工作流：${process.latestRun ? `${process.latestRun.workflow_type} · ${process.latestRun.status} · ${process.latestRun.current_node}` : '暂无'}</div>` : '';
  const articleInfo = process.article && ['review', 'human', 'wechat', 'publish'].includes(stage.key)
    ? `<div class="muted">文章：${process.article.title} · ${process.article.status}</div>` : '';
  const scheduleInfo = process.scheduledJob && ['human', 'wechat', 'publish'].includes(stage.key)
    ? `<div class="muted">定时发布：${formatDate(process.scheduledJob.scheduled_at)} · ${process.scheduledJob.status}</div>` : '';
  const publishedInfo = process.publishedJob && ['wechat', 'publish'].includes(stage.key)
    ? `<div><a class="btn small" href="${escapeHtml(process.publishedJob.article_url)}" target="_blank" rel="noopener noreferrer">↗ 查看已发布文章</a></div>` : '';
  const assetInfo = stage.key === 'assets'
    ? `<div>${process.assets.map(asset => `<div class="muted">${escapeHtml(asset.file_name)} · ${escapeHtml(asset.asset_type)} · ${escapeHtml(asset.processing_status)}</div>`).join('') || '<div class="muted">暂无素材</div>'}</div>` : '';
  const stageStatus = `<span class="status ${statusClass(stage.state === 'running' ? 'RUNNING' : stage.state === 'failed' ? 'FAILED' : stage.state === 'done' ? 'DONE' : 'PENDING')}">${escapeHtml(stage.state)}</span>`;
  $('process-stage-detail').innerHTML = `<div class="stage-detail"><div class="actions" style="justify-content:space-between;align-items:center;margin:0 0 10px"><h3 style="margin:0">${escapeHtml(stage.title)}</h3>${stageStatus}</div><div class="muted">${escapeHtml(stage.detail)}</div>${runNodes}${articleInfo}${scheduleInfo}${publishedInfo}${assetInfo}<div class="actions" style="margin-top:12px"><button class="btn" onclick="switchView('${stage.action}')">查看对应工作区</button></div></div>`;
}

function renderCurrentView() {
  if (state.view === 'workspace') {
    // 提案待确认 / 生成中占位 / 已打开文章：立即渲染（刷新恢复路径也走这里）
    if (state.pendingProposal) renderPendingProposal();
    else if (state.pendingGeneration) renderPendingGeneration();
    else if (state.currentArticleId) void renderArticle().catch(() => {});
  }
  if (state.view === 'sessions') renderSessions();
  if (state.view === 'articles') renderArticles();
  if (state.view === 'assets') { void loadAllAssets().then(renderAssetLibrary).catch(() => {}); }
  if (state.view === 'accounts') renderAccounts();
  if (state.view === 'styles') { void loadTypesettingStyles().then(renderStyleGallery).catch(() => {}); }
  if (state.view === 'monitor') {
    renderMonitorWorkflows();
    renderAudits();
    void refreshSystemHealth();
    void refreshMonitorWorkflows();
    void refreshAudits();
  }
  if (state.view === 'users') renderUsers();
  if (state.view === 'process') renderProcess(state.currentSessionDetail);
  if (state.view === 'settings') void loadVisionSettings();
  void renderChatTree();
}

async function loadSession() {
  if (!state.currentSession) return;
  const detail = await api(`/api/sessions/${state.currentSession}`);
  state.currentSessionDetail = detail;
  state.currentAssets = detail.assets;
  if (detail.articles.length && !state.currentArticleId && !state.pendingGeneration) state.currentArticleId = detail.articles[0].id;
  const primaryAssetCount = detail.assets.filter(
    asset => !JSON.parse(asset.metadata_json || '{}').derivative_type
  ).length;
  $('asset-count').textContent = String(primaryAssetCount);
  renderAssetRows();
  renderAssetLibrary();
  renderProcess(detail);
  if (state.currentArticleId) await renderArticle();
}

function renderAssetRows() {
  const rows = state.currentAssets.filter(asset => !JSON.parse(asset.metadata_json || '{}').derivative_type);
  $('asset-list').innerHTML = rows.length ? rows.map(asset => {
    const metadata = JSON.parse(asset.metadata_json || '{}');
    const summary = asset.asset_type === 'VIDEO'
      ? `${metadata.width || '?'}x${metadata.height || '?'} · ${Number(metadata.duration_seconds || 0).toFixed(1)}s`
      : '';
    const thumbnail = asset.asset_type === 'VIDEO' && metadata.thumbnail_asset_id
      ? `<img class="asset-thumb" src="/api/assets/${metadata.thumbnail_asset_id}/file" alt="${escapeHtml(asset.file_name)}封面帧" />` : '';
    const errorNote = (state.assetErrors || {})[asset.id]
      ? `<div class="vision-result" style="color:var(--red)">${escapeHtml(state.assetErrors[asset.id])}</div>` : '';
    return `<div class="row"><div><div class="row-title">${escapeHtml(asset.file_name)}</div>
    <div class="muted">${escapeHtml(asset.asset_type)} · ${escapeHtml(asset.processing_status)} · ${(asset.size_bytes / 1024).toFixed(1)} KB${summary ? ' · ' + summary : ''}</div>${errorNote}</div>
    <div class="actions">${['IMAGE', 'VIDEO'].includes(asset.asset_type) && can('vision.run') ? `<button class="btn small" onclick="analyzeAsset('${asset.id}')">◎ 视觉分析</button>` : ''}${thumbnail}</div></div>`;
  }).join('') : '<div class="empty">暂无素材</div>';
}

async function selectSession(sessionId) {
  clearPendingFlows(); // 用户明确选择已有任务：残留的新对话占位同样让位
  state.currentSession = sessionId;
  state.currentArticleId = null;
  state.currentStage = null;
  await loadSession();
  switchView('process');
  toast('已打开生产流程');
}

// 显式打开已有文章 = 明确的用户意图：未完成的新对话占位（架构提案 / 生成中）必须让位。
// 否则残留的 wanren_pending_* 状态（含刷新后自动恢复）会让点击对话树永远停在
// 「架构思考中 / 生成中」骨架屏——用户看到的就是“点了对话只显示工作台”。
function clearPendingFlows() {
  if (state.pendingTimer) { clearInterval(state.pendingTimer); state.pendingTimer = null; }
  if (state.pendingProposalTimer) { clearInterval(state.pendingProposalTimer); state.pendingProposalTimer = null; }
  state.pendingProposal = null;
  state.pendingGeneration = null;
  try {
    localStorage.removeItem('wanren_pending_proposal');
    localStorage.removeItem('wanren_pending_session');
  } catch { /* 忽略 */ }
}

let openArticleSeq = 0; // 并发点击保护：更新的点击让旧的打开链路作废

async function openArticle(articleId) {
  const seq = ++openArticleSeq;
  clearPendingFlows();
  const detail = await api(`/api/articles/${articleId}`);
  if (seq !== openArticleSeq) return; // 期间用户又点了别的对话，本链路作废
  if (state.currentArticleId !== articleId) {
    // 切换对话：未发送的待发素材清空（素材按对话归属）
    state.pendingAttachments.forEach(item => { if (item.previewUrl?.startsWith('blob:')) URL.revokeObjectURL(item.previewUrl); });
    state.pendingAttachments = [];
  }
  state.currentArticleId = articleId;
  state.currentSession = detail.article.session_id;
  try { localStorage.setItem('wanren_last_article', articleId); } catch { /* 忽略 */ }
  if (detail.article.status === 'WECHAT_DRAFT' && can('publish.submit')) {
    // 草稿一致性校验放后台：不能阻塞打开（真实账号校验可能秒级，期间页面会停在旧文章，
    // 造成"要点两次才选中 / 快速切换时显示上一个"）
    void syncDraftStatus(articleId, { refresh: false, silent: true }).catch(() => {});
  }
  await loadSession();
  if (seq !== openArticleSeq) return;
  switchView('workspace');
}

async function createSession() {
  const requirements = $('session-requirements').value.split(/[,，]/).map(x => x.trim()).filter(Boolean);
  const payload = {
    title: $('session-title').value,
    official_account_id: $('session-account').value,
    goal: $('session-goal').value,
    audience: $('session-audience').value,
    tone: $('session-tone').value,
    requirements,
  };
  const created = await api('/api/sessions', { method: 'POST', body: payload });
  state.currentSession = created.session.id;
  state.currentArticleId = null;
  state.currentStage = null;
  $('article-status').textContent = '未生成';
  $('article-status').className = 'status';
  $('article-panel').innerHTML = '<div class="empty">先生成文章，再进入审核</div>';
  $('session-message').textContent = `任务已创建：${created.session.id}`;
  await loadSession();
  await refreshAll();
  toast('任务已创建');
}

async function uploadAssets() {
  // 归属优先级：素材库分组目标 > 当前对话文章 > 当前会话公共
  const target = state.assetUploadTarget || (state.currentArticleId && state.currentSession
    ? { sessionId: state.currentSession, articleId: state.currentArticleId }
    : (state.currentSession ? { sessionId: state.currentSession, articleId: null } : null));
  if (!target) throw new Error('请先选择一个对话（或在工作台创建任务）');
  const files = [...$('asset-input').files];
  if (!files.length) throw new Error('请选择素材文件');
  for (const file of files) {
    const form = new FormData();
    form.append('file', file);
    if (target.articleId) form.append('article_id', target.articleId);
    await api(`/api/sessions/${target.sessionId}/assets`, { method: 'POST', body: form });
  }
  state.assetUploadTarget = null;
  toast(`已上传 ${files.length} 个素材`);
  await loadAllAssets();
  renderAssetLibrary();
  await loadSession().catch(() => {});
  await refreshAll();
}

async function analyzeAsset(assetId) {
  const button = window.event?.target;
  button?.classList.add('loading');
  button?.setAttribute('disabled', 'true');
  state.assetErrors = state.assetErrors || {};
  try {
    const assetRow = (state.allAssets || []).find(x => x.id === assetId) || (state.currentAssets || []).find(x => x.id === assetId);
    const sessionId = assetRow?.session_id || state.currentSession;
    await api(`/api/sessions/${sessionId}/assets/${assetId}/analyze`, { method: 'POST', body: {} });
    const vision = state.visionSettings;
    toast('视觉分析完成（' + (vision && vision.provider === 'vidlens' ? 'VidLens' : '多模态模型') + '）');
    delete state.assetErrors[assetId];
    await loadSession();
  } catch (error) {
    state.assetErrors[assetId] = error.message;
    toast(error.message, true);
    renderAssetRows();
    if (state.view === 'assets') { void loadAllAssets().then(renderAssetLibrary).catch(() => {}); }
  } finally {
    button?.classList.remove('loading');
    button?.removeAttribute('disabled');
  }
}

async function transcodeAsset(assetId) {
  const preset = document.getElementById(`transcode-preset-${assetId}`)?.value || 'wechat_720p';
  const result = await api(`/api/assets/${assetId}/transcode`, {
    method: 'POST',
    body: { preset },
  });
  toast(result.status === 'QUEUED' ? '转码任务已排队' : '同预设转码已在队列中');
  await loadSession();
  await refreshAll();
}

async function generateArticle() {
  if (!state.currentSession) throw new Error('请先创建任务');
  const instruction = $('generate-instruction').value;
  // 与新对话一致的门槛：先让 AI 出架构提案，用户确认后再生成
  state.currentArticleId = null;
  state.pendingProposal = { sessionId: state.currentSession, instruction: instruction || '根据任务素材写一篇公众号文章', startedAt: Date.now() };
  try { localStorage.setItem('wanren_pending_proposal', state.currentSession); } catch { /* 忽略 */ }
  switchView('workspace');
  renderArticle();
  void pollPendingProposal();
  startProposalRequest(state.pendingProposal, state.pendingProposal.instruction);
}

async function waitForGeneratedArticle(sessionId, workflowRunId) {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    const workflow = await api(`/api/workflows/${workflowRunId}`);
    if (workflow.run.status === 'FAILED') throw new Error('生成任务失败');
    const detail = await api(`/api/sessions/${sessionId}`);
    if (workflow.run.status === 'SUCCESS' && detail.articles.length) return detail;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error('生成任务超时');
}


function renderPendingGeneration() {
  const pending = state.pendingGeneration;
  const host = $('article-panel');
  if (!pending || !host) return;
  const elapsed = Math.round((Date.now() - (pending.startedAt || Date.now())) / 1000);
  host.innerHTML = `<div class="review-score"><div class="score-ring busy" aria-label="生成中"></div>
    <div><strong>正在生成初稿</strong>
    <div class="muted">任务已提交，模型正在写作（${elapsed}s）· 刷新页面也会自动续接</div></div></div>
    <div class="article-workbench">
      <div class="article-main-column">
        <div class="article-preview"><div class="preview-zoom">
          <div class="gen-skeleton">
            <div class="gen-skeleton-title"></div>
            <div class="gen-skeleton-line w80"></div><div class="gen-skeleton-line"></div>
            <div class="gen-skeleton-line w90"></div><div class="gen-skeleton-line w60"></div>
            <div class="gen-skeleton-line"></div><div class="gen-skeleton-line w75"></div>
            <div class="muted" style="text-align:center;margin-top:18px">骨架屏：初稿就绪后自动填充</div>
          </div>
        </div></div>
      </div>
      <div class="panel-resizer" title="左右拖动调整对话面板宽度"></div>
      <section class="article-chat-panel" aria-label="生成进度">
        <header><div><strong>对话式优化</strong><small>初稿生成中，就绪后可直接继续改稿</small></div>
          <div class="chat-header-actions"><span class="status warn">生成中</span><button type="button" onclick="abortGenerationBtn(this)" title="停止等待（点两次确认；文章可能仍在后台完成）" style="height:27px;border:1px solid var(--amber,#b76a1b);border-radius:999px;background:var(--amber-soft,#fff3e3);color:var(--amber,#b76a1b);font-size:11px;font-weight:700;cursor:pointer;padding:0 10px;">■ 终止</button></div></header>
        <div class="chat-messages" id="article-chat-messages"><div class="chat-zoom">
          <div class="chat-message user"><strong>我</strong><div>${escapeHtml(pending.instruction || '请根据素材写一篇公众号文章')}</div></div>
          <div class="chat-message assistant"><strong>万仞</strong><div>正在生成初稿…<span class="typing-caret"></span></div></div>
        </div></div>
        <div class="chat-quick-actions"></div>
        <form class="chat-composer" onsubmit="return false">
          <textarea id="article-chat-input" rows="3" placeholder="初稿生成后即可继续对话改稿…" disabled></textarea>
          <button id="article-chat-send" type="button" disabled>生成中…</button>
        </form>
      </section>
    </div>`;
  applyChatWidth();
  applyPaneZoom();
  host.scrollIntoView({ behavior: 'smooth', block: 'start' }); // 新对话流程：直接来到编辑形态
  // 每 5s 刷新计时
  if (!state.pendingTimer) {
    state.pendingTimer = setInterval(() => {
      if (!state.pendingGeneration) { clearInterval(state.pendingTimer); state.pendingTimer = null; }
      else renderPendingGeneration();
    }, 5000);
  }
}

// ============ 所见即所得：编辑器直接呈现样式模版的排版（可编辑 DOM 上套样式令牌） ============

function isDarkColor(hex) {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(String(hex || ''));
  if (!m) return false;
  return 0.299 * parseInt(m[1], 16) + 0.587 * parseInt(m[2], 16) + 0.114 * parseInt(m[3], 16) < 96;
}

function quoteLikeText(text) {
  const s = String(text).trim();
  return s.length <= 80 && (/^["“「]/.test(s) || /["”」]$/.test(s));
}

// 编辑器版式装饰（与发布渲染器同一套气质）：刊头横幅 / 眉题 / 文末收束——装饰不可编辑
function editorHeaderDecor(st) {
  if (!st) return { pre: '', mid: '', post: '' };
  const kind = st.header || 'minimal';
  if (kind === 'magazine') {
    return {
      pre: `<div class="ed-decor ed-banner" style="background:${st.accent};display:flex;justify-content:space-between;align-items:center;"><span style="color:#fefefe;font-size:12px;letter-spacing:3px;font-weight:700;">WANREN</span><span style="color:#fefefe;font-size:10px;letter-spacing:2px;opacity:.85;">${'论文解读 · 2026'}</span></div>`,
      mid: `<div class="ed-eyebrow" style="color:${st.accent};font-family:${st.font};">${'深度阅读'}</div>`,
      post: '',
    };
  }
  if (kind === 'dark_banner') {
    return {
      pre: `<div class="ed-decor ed-banner" style="background:${st.accent};display:flex;justify-content:space-between;align-items:center;"><span style="color:#080808;font-size:11px;letter-spacing:3px;font-weight:700;">WANREN TECH</span><span style="color:#080808;font-size:10px;letter-spacing:2px;opacity:.75;">${'TECH REPORT'}</span></div>`,
      mid: `<div class="ed-eyebrow" style="color:${st.accent2 || st.accent};">SPECIAL REPORT</div>`,
      post: '',
    };
  }
  if (kind === 'center') {
    return {
      pre: '',
      mid: `<div class="ed-eyebrow" style="color:${st.accent};letter-spacing:4px;text-align:center;">栏目</div>`,
      post: `<div class="ed-decor" style="width:36px;height:3px;margin:14px auto 0;background:${st.accent};"></div>`,
    };
  }
  if (kind === 'frame') {
    return {
      pre: `<div class="ed-decor ed-frame" style="border:1px solid ${st.border};background:${st.accent_soft};">`,
      mid: `<div class="ed-eyebrow" style="color:${st.accent};letter-spacing:2px;">PAPER READING</div>`,
      post: `</div>`,
    };
  }
  if (kind === 'brand') {
    return { pre: '', mid: `<div class="ed-eyebrow" style="color:${st.accent};">WANREN READING</div>`, post: '' };
  }
  return { pre: '', mid: `<div class="ed-eyebrow" style="color:${st.muted};">WANREN</div>`, post: '' };
}

function editorEndDecor(st) {
  if (!st) return '';
  const kind = st.end || 'line';
  const map = {
    dots: `<div class="ed-end" style="color:${st.muted};letter-spacing:6px;">· · ·</div>`,
    ladder: `<div class="ed-end" style="color:${st.accent};letter-spacing:3px;font-weight:700;">CONTINUE READING</div>`,
    warm_note: `<div class="ed-end" style="color:${st.muted};font-family:${st.font};">谢谢读到这里 ❋</div>`,
    tip: `<div class="ed-end" style="background:${st.accent_soft};color:${st.accent};border-radius:${st.radius || 8}px;">觉得有用就点个「在看」吧</div>`,
    cta: `<div class="ed-end" style="background:${st.accent};color:#fefefe;font-weight:700;letter-spacing:1px;border-radius:${st.radius || 8}px;">关注我，下期继续 →</div>`,
    references: `<div class="ed-end" style="color:${st.muted};">本文由万仞辅助写作，内容与结论以原文为准。</div>`,
  };
  if (map[kind]) return map[kind];
  return `<div class="ed-end" style="color:${st.muted};letter-spacing:3px;">— END —</div>`;
}

function editorQuoteStyle(st) {
  if (!st) return '';
  const r = st.radius ?? 0;
  const kind = st.quote || 'outline';
  if (kind === 'accent_card') return `background:${st.accent};color:#fefefe;text-align:center;font-weight:600;padding:14px 18px;border-radius:${r}px;`;
  if (kind === 'dark_card') return `background:${st.card_bg || '#1F1F1F'};border:1px solid ${st.border};text-align:center;padding:14px 18px;border-radius:${r}px;`;
  if (kind === 'plain_center') return `text-align:center;font-weight:600;padding:10px 0;`;
  if (kind === 'warm_card' || kind === 'soft_card') return `background:${st.accent_soft};text-align:center;padding:14px 18px;border-radius:${r}px;`;
  if (kind === 'highlight') return `text-align:center;font-weight:600;`;
  if (kind === 'definition') return `border:1px solid ${st.border};background:${st.accent_soft};padding:12px 16px;border-radius:${r}px;`;
  return `border-left:3px solid ${st.accent};background:${st.accent_soft};padding:10px 14px;`;
}

function styleTokensOf(article) {
  return (state.typesettingStyles || []).find(s => s.id === article.typesetting_id)?.tokens || null;
}

// 指标数据卡（editorial.metrics）：与发布渲染器 _metric_cards 同款——两列、取前 4 项；
// 纯展示装饰（无 data-paragraph-index，编辑收集器会跳过），保存时由后端原样保留 editorial
function editorMetricsBlock(st, metrics) {
  const clean = (metrics || []).filter(m => m && typeof m === 'object').slice(0, 4);
  if (!clean.length) return '';
  const accent = escapeHtml(st?.accent || '#175641');
  const soft = escapeHtml(st?.accent_soft || '#e7f0eb');
  const border = escapeHtml(st?.border || '#dde4e1');
  const muted = escapeHtml(st?.muted || '#667376');
  const radius = st?.radius ?? 8;
  const small = st?.sizes?.small || 12;
  const rows = [];
  for (let start = 0; start < clean.length; start += 2) {
    const row = clean.slice(start, start + 2);
    const cells = row.map(metric => `
          <td style="width:50%;padding:0 6px 12px 0;border:none;vertical-align:top;">
            <div style="padding:14px 12px;background:${soft};border:1px solid ${border};border-radius:${radius}px;text-align:center;">
              <div style="margin:0 0 5px;font-size:20px;font-weight:700;color:${accent};">${escapeHtml(String(metric.value ?? ''))}</div>
              <div style="margin:0;font-size:${small}px;color:${muted};">${escapeHtml(String(metric.label ?? '指标'))}</div>
            </div>
          </td>`).join('');
    rows.push(`<tr>${cells}${row.length === 1 ? '<td style="width:50%;border:none;"></td>' : ''}</tr>`);
  }
  return `<table class="ed-metrics" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-collapse:collapse;border-spacing:0;margin:24px 0;width:100%;border:none;">${rows.join('')}</table>`;
}

function wysiwygSectionHeading(st, index, headingHtml, editableAttr) {
  if (!st) return `<h4 data-field="heading" data-section-index="${index}"${editableAttr}>${headingHtml}</h4>`;
  const kind = st.section_heading || 'thin_line';
  const sizes = st.sizes || {};
  let badge = '';
  let h4style = `font-size:${sizes.section || 18}px;color:${st.title || '#111'};font-family:${st.font || 'inherit'};margin:0 0 10px;`;
  if (kind === 'navy_bar') h4style += `border-left:4px solid ${st.accent};padding-left:12px;`;
  else if (kind === 'thin_line') h4style += `border-top:1px solid ${st.border};padding-top:12px;`;
  else if (kind === 'watermark') h4style += `border-bottom:1px solid ${st.border};padding-bottom:8px;`;
  else if (kind === 'dashed') h4style += `border-top:1px dashed ${st.border};padding-top:12px;`;
  else if (kind === 'spaced_label') badge = `<div class="sec-badge" style="color:${st.accent};letter-spacing:3px;font-size:11px;font-weight:700;">${String(index + 1).padStart(2, '0')}</div>`;
  else if (kind === 'dot') {
    return `<div style="display:flex;align-items:center;gap:10px;margin:18px 0 8px;"><span class="sec-badge" style="width:26px;height:26px;border-radius:50%;background:${st.accent};color:#fff;display:inline-flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex:0 0 auto;">${index + 1}</span><h4 data-field="heading" data-section-index="${index}"${editableAttr} style="${h4style}margin:0;">${headingHtml}</h4></div>`;
  }
  else if (kind === 'big_number') badge = `<div class="sec-badge" style="color:${st.accent};font-size:28px;font-weight:900;line-height:1;">${index + 1}</div>`;
  else if (kind === 'step') badge = `<span class="sec-badge sec-badge-step" style="border:1px solid ${st.accent};color:${st.accent};">STEP ${String(index + 1).padStart(2, '0')}</span>`;
  return `${badge}<h4 data-field="heading" data-section-index="${index}"${editableAttr} style="${h4style}">${headingHtml}</h4>`;
}

let articleRenderSeq = 0; // 渲染序号：丢弃过期渲染，避免定时刷新用旧数据覆盖新样式

async function renderArticle() {
  const seq = ++articleRenderSeq;
  if (!state.currentArticleId) {
    if (state.pendingProposal) { renderPendingProposal(); return; }
    if (state.pendingGeneration) { renderPendingGeneration(); return; }
    const host = $('article-panel');
    if (host) host.innerHTML = '<div class="empty">点击左侧「＋ 新对话」开始创作，或选择一篇已有文章</div>';
    return;
  }
  let detail;
  try {
    detail = await api(`/api/articles/${state.currentArticleId}`);
  } catch (error) {
    if (seq !== articleRenderSeq) return; // 已有更新的渲染请求，过期结果直接丢弃
    // 对话已不存在（被删除，或 localStorage 恢复了失效的最近文章）：
    // 自愈清引用，避免 backgroundRefresh 每 2s 重复抛 "Article not found"。
    state.currentArticleId = null;
    try { localStorage.removeItem('wanren_last_article'); } catch { /* 忽略 */ }
    const host = $('article-panel');
    if (host) host.innerHTML = '<div class="empty">这篇对话已不存在（可能已被删除）。请从左侧选择其他对话，或点击「＋ 新对话」开始创作。</div>';
    void refreshArticles().catch(() => {});
    return;
  }
  if (seq !== articleRenderSeq) return; // 已有更新的渲染请求，丢弃本次过期结果
  const article = detail.article;
  state.articleVersions = detail.versions || [];
  const content = JSON.parse(article.content_json);
  const review = detail.reviews.filter(x => x.review_type === 'AI').at(-1)
    || { score: '—', decision: '未预审（外部导入）', risk_level: 'UNKNOWN', created_at: article.created_at, result_json: '{}' };
  const scheduledJob = detail.publish_jobs.find(job => job.status === 'SCHEDULED') || null;
  const draftJob = detail.publish_jobs.find(job => job.status === 'WECHAT_DRAFT' && job.external_id) || null;
  const deletedDraftJob = detail.publish_jobs.find(job => job.status === 'WECHAT_DRAFT_DELETED') || null;
  const publishedJob = detail.publish_jobs.find(job => job.article_url) || null;
  $('article-status').textContent = article.status;
  $('article-status').className = `status ${statusClass(article.status)}`;
  const assetsById = new Map(state.currentAssets.map(asset => [asset.id, asset]));
  const assetCaption = asset => {
    try {
      const structured = JSON.parse(asset.structured_data || '{}');
      if (structured.figure_index) {
        return structured.page ? `论文图 ${structured.figure_index}（第 ${structured.page} 页）` : `论文图 ${structured.figure_index}`;
      }
    } catch { /* 无结构化信息时走下方统一兜底 */ }
    return '论文配图'; // 与发布渲染器 asset_caption 同一兜底：中间看到的即公众号发布后的
  };
  const renderMedia = section => (section.asset_refs || [])
    .map(assetId => assetsById.get(assetId))
    .filter(asset => asset && asset.asset_type === 'IMAGE' && ['READY', 'VISION_READY'].includes(asset.processing_status))
    .map(asset => `
      <figure class="article-figure">
        <img src="/api/assets/${encodeURIComponent(asset.id)}/file" alt="${escapeHtml(assetCaption(asset))}" />
        <figcaption>${escapeHtml(assetCaption(asset))}</figcaption>
      </figure>`)
    .join('');
  const editable = state.articleImmersive && can('article.edit');
  const editableAttr = editable ? ' contenteditable="true" spellcheck="true"' : '';
  const styleTokens = styleTokensOf(article);
  const styleAccent = (state.typesettingStyles || []).find(s => s.id === article.typesetting_id)?.accent;
  const containerStyle = styleTokens
    ? `--green:${escapeHtml(styleTokens.accent || styleAccent || '#175641')};background-color:${escapeHtml(styleTokens.page_bg || '#fefefe')};color:${escapeHtml(styleTokens.text || '#262626')};font-family:${escapeHtml(styleTokens.font || 'inherit')};`
    : (styleAccent ? `--green:${escapeHtml(styleAccent)}` : '');
  const summaryColor = !styleTokens
    ? '#667376'
    : isDarkColor(styleTokens.page_bg)
      ? (styleTokens.title || '#F0EFEB')
      : (styleTokens.muted || '#667376');
  const titleStyle = styleTokens ? ` style="font-size:${styleTokens.sizes?.cover || 26}px;color:${escapeHtml(styleTokens.title || '#111')};font-family:${escapeHtml(styleTokens.font || 'inherit')};${(styleTokens.header === 'center' || styleTokens.header === 'frame') ? 'text-align:center;' : ''}margin:4px 0 8px;text-wrap:balance;"` : '';
  const summaryStyle = styleTokens ? ` style="color:${escapeHtml(summaryColor)};${(styleTokens.header === 'center' || styleTokens.header === 'frame') ? 'text-align:center;' : ''}"` : '';
  const paragraphBase = styleTokens
    ? `font-size:${styleTokens.sizes?.body || 15}px;line-height:${styleTokens.line_height || 1.8};`
    : '';
  // 最新版本的 AI 改动高亮（只读渲染态显示；进入编辑自动隐去）
  const latestHighlight = (detail.versions || []).filter(v => v.highlight_json).at(-1);
  let highlightSet = new Set();
  try {
    const texts = JSON.parse(latestHighlight?.highlight_json || '[]');
    if (Array.isArray(texts) && texts.length) highlightSet = new Set(texts.map(t => String(t).trim()));
  } catch { /* ignore */ }
  const highlightVersion = latestHighlight?.version_no ?? 0;
  if (state.aiHighlightDismissedVersion !== highlightVersion) state.aiHighlightDismissedVersion = null;
  const highlightActive = !editable && state.aiHighlightDismissedVersion === null && highlightSet.size > 0;
  const wrapHighlight = (text) => highlightActive && highlightSet.has(String(text).trim())
    ? `<mark class="ai-changed">${escapeHtml(text)}</mark>`
    : escapeHtml(text);
  const sectionHtmls = content.sections.map((s, sectionIndex) => `
    <section class="article-editor-section" data-section-id="${escapeHtml(s.id || `sec_${sectionIndex + 1}`)}" data-asset-refs="${escapeHtml(JSON.stringify(s.asset_refs || []))}" data-style="${escapeHtml(JSON.stringify(s.style || {}))}">
      ${wysiwygSectionHeading(styleTokens, sectionIndex, escapeHtml(s.heading), editableAttr)}
      ${s.paragraphs.map((p, paragraphIndex) => `<p data-paragraph-index="${paragraphIndex}" data-section-index="${sectionIndex}"${editableAttr} style="${paragraphBase}${quoteLikeText(p) && styleTokens ? editorQuoteStyle(styleTokens) : ''}${sectionInlineStyle(s.style)}">${wrapHighlight(p)}</p>`).join('')}
      ${renderMedia(s)}
    </section>`);
  // 数据卡插入位置与发布渲染器一致：首个标题含「实验/结果/效果」的小节之后，否则全文末尾（文末收束前）
  const metricsBlock = editorMetricsBlock(styleTokens, content.editorial?.metrics);
  if (metricsBlock) {
    const after = content.sections.findIndex(s => ['实验', '结果', '效果'].some(word => String(s.heading || '').includes(word)));
    sectionHtmls.splice(after >= 0 ? after + 1 : sectionHtmls.length, 0, metricsBlock);
  }
  const sections = sectionHtmls.join('');
  const reviewJson = (() => { try { return JSON.parse(review?.result_json || '{}'); } catch { return {}; } })();
  const issues = ((reviewJson.issues) || []).map(i => `
    <div class="issue"><strong>${escapeHtml(i.type)} / ${escapeHtml(i.severity)}</strong><div>${escapeHtml(i.message)} · ${escapeHtml(i.suggestion)}</div></div>`).join('')
    || '<div class="muted">无阻塞问题</div>';
  const originalRequired = Boolean(article.original_declaration_required);
  const originalConfirmed = article.original_declaration_status === 'MANUAL_CONFIRMED'
    && article.original_declaration_version_id === article.current_version_id;
  const originalBlocked = originalRequired && !originalConfirmed;
  const actions = [
    can('article.edit') ? `<select id="article-style-select" title="排版样式模版（8 种气质）" onchange="changeArticleStyle(this.value)" style="height:36px;border:1px solid var(--line);border-radius:6px;padding:0 6px;background:var(--panel);color:var(--ink);font-size:12px">${(state.typesettingStyles || []).map(s => `<option value="${escapeHtml(s.id)}"${s.id === article.typesetting_id ? ' selected' : ''}>${escapeHtml(s.name)}</option>`).join('') || '<option value="">默认</option>'}</select>` : '',
    can('article.edit') ? '<button class="btn" onclick="enterArticleImmersiveEditor()">✎ 沉浸编辑</button>' : '',
    can('read') ? '<button class="btn" onclick="openArticleRenderPreview()" title="按当前样式渲染的公众号成品效果">▶ 公众号预览</button>' : '',
    can('review.run') ? '<button class="btn" onclick="rerunReview()">↻ 重新预审</button>' : '',
    can('review.decide') ? '<button class="btn primary" onclick="decideArticle(true)">✓ 人工通过</button><button class="btn danger" onclick="decideArticle(false)">× 驳回</button>' : '',
    can('publish.submit')
      ? `<input id="schedule-at" type="datetime-local" style="height:36px;border:1px solid var(--line);border-radius:6px;padding:0 10px;background:var(--panel);color:var(--ink)" />
        <select id="schedule-tz" title="时区" style="height:36px;border:1px solid var(--line);border-radius:6px;padding:0 6px;background:var(--panel);color:var(--ink);font-size:12px">${TIMEZONES.map(tz=>`<option value="${tz}"${tz===currentTimezone()?' selected':''}>${tz}</option>`).join('')}</select>
        <button class="btn small" type="button" onclick="quickSchedule('tonight')">今晚 20:00</button>
        <button class="btn small" type="button" onclick="quickSchedule('tomorrow')">明早 08:00</button>
         <button class="btn amber" onclick="scheduleArticle()" ${originalBlocked ? 'disabled title="先确认原创声明"' : ''}>⏱ 定时发布</button>
         ${draftJob ? '<button class="btn" onclick="syncDraftStatus()">⟳ 校验微信草稿</button>' : ''}
         <button class="btn amber" onclick="publishArticle('draft')">▶ 同步草稿</button>
         <button class="btn amber" onclick="publishArticle('publish')" ${originalBlocked ? 'disabled title="先确认原创声明"' : ''}>⇥ 发布</button>`
      : '',
  ].filter(Boolean).join('');
  $('article-panel').innerHTML = `<div class="review-score"><div class="score-ring">${review.score}</div>
    <div><strong>${escapeHtml(review.decision)}</strong><div class="muted">风险 ${escapeHtml(review.risk_level)} · ${formatDate(review.created_at)}</div></div></div>
    <div class="article-workbench"><div class="article-main-column">
    <div class="article-preview"${containerStyle ? ` style="${containerStyle}"` : ''}><div class="preview-zoom">
      <div class="article-editor-toolbar">
        <span id="article-editor-state">${state.articleEditorSaving ? '正在保存…' : state.articleEditorDirty ? '有未保存修改' : '可手动编辑'}</span>
        <span class="divider"></span>
        <span class="fmt-label">样式</span><select class="fmt-select" onchange="changeArticleStyle(this.value)" title="排版样式模版（8 种气质）">${(state.typesettingStyles || []).map(s => `<option value="${escapeHtml(s.id)}"${s.id === article.typesetting_id ? ' selected' : ''}>${escapeHtml(s.name)}</option>`).join('') || '<option value="">默认</option>'}</select>
        <span class="divider"></span>
        <select id="fmt-align" class="fmt-select" onchange="applyFormat('align', this.value)" title="对齐"><option value="">对齐</option><option value="left">左</option><option value="center">中</option><option value="right">右</option><option value="justify">两端</option></select>
        <span class="fmt-label">段前</span><select id="fmt-mt" class="fmt-select" onchange="applyFormat('margin_top', this.value)" title="段前距"><option value="">-</option>${[0,8,16,24,32,48].map(v=>`<option value="${v}">${v}</option>`).join('')}</select>
        <span class="fmt-label">段后</span><select id="fmt-mb" class="fmt-select" onchange="applyFormat('margin_bottom', this.value)" title="段后距"><option value="">-</option>${[0,8,16,24,32,48].map(v=>`<option value="${v}">${v}</option>`).join('')}</select>
        <span class="fmt-label">首行缩进</span><select id="fmt-indent" class="fmt-select" onchange="applyFormat('indent', this.value)" title="首行缩进"><option value="">无</option>${[1,1.5,2,2.5,3,4].map(v=>`<option value="${v}">${v}em</option>`).join('')}</select>
        <span class="fmt-label">行距</span><select id="fmt-lh" class="fmt-select" onchange="applyFormat('line_height', this.value)" title="行距"><option value="">-</option>${[1.25,1.5,1.75,2,2.5].map(v=>`<option value="${v}">${v}</option>`).join('')}</select>
        <span class="fmt-label">字距</span><select id="fmt-ls" class="fmt-select" onchange="applyFormat('letter_spacing', this.value)" title="字距"><option value="">-</option>${[0.5,1,1.5,2,3].map(v=>`<option value="${v}">${v}px</option>`).join('')}</select>
        <span class="divider"></span>
        <button type="button" onclick="clearFormat()">清除排版</button>
        <button type="button" onclick="addArticleParagraph()">＋段落</button>
        <button type="button" onclick="deleteArticleBlock()">－当前段</button>
        <button type="button" onclick="addArticleSection()">＋章节</button>
        <button type="button" onclick="deleteArticleSection()">－章节</button>
        <button type="button" class="primary" onclick="saveArticleEditor()">⇧ 保存</button>
      </div>
      ${editorHeaderDecor(styleTokens).pre}
      ${editorHeaderDecor(styleTokens).mid}
      <h3 data-article-field="title"${editableAttr}${titleStyle}>${escapeHtml(article.title)}</h3>
      <div class="muted article-summary-editor" data-article-field="summary"${editableAttr}${summaryStyle}>${escapeHtml(article.summary)}</div>
      ${editorHeaderDecor(styleTokens).post}
      ${highlightActive ? `<div class="ai-highlight-banner">🖍 已高亮本版 AI 新增/修改的 <b>${highlightSet.size}</b> 处段落${editable ? '' : ' <button type="button" class="link-btn" onclick="dismissAiHighlight()">清除高亮</button>'}</div>` : ''}
      ${sections}
      ${editorEndDecor(styleTokens)}
      <footer class="article-editor-footer"><span id="article-word-count">${String(article.summary || '').length}</span><small>沉浸模式可直接编辑标题、摘要、章节和段落</small></footer></div>
    </div>
    <div class="issues" style="margin-top:12px">${issues}</div>
    ${originalRequired ? `<div class="alert-item" style="margin-top:12px">
      <strong>原创声明确认</strong>
      <div>${originalConfirmed
        ? `已在公众号编辑器开启原创声明并由 ${escapeHtml(article.original_declaration_confirmed_by || '发布者')} 确认。`
        : draftJob
          ? '微信官方暂未开放原创声明 API：请在公众号编辑器打开该草稿并开启原创声明，保存后回到万仞点击“确认原创声明”。'
          : '先同步微信草稿，再在公众号编辑器开启原创声明并回到万仞确认。'}</div>
      ${originalBlocked && draftJob && can('publish.submit') ? '<div class="actions" style="margin-top:8px"><button class="btn small primary" onclick="confirmOriginalDeclaration()">✓ 确认原创声明</button></div>' : ''}
    </div>` : ''}
    ${scheduledJob ? `<div class="alert-item" style="margin-top:12px"><strong>SCHEDULED</strong><div>计划发布时间：${formatDate(scheduledJob.scheduled_at)}</div>
      <div class="actions" style="margin-top:8px">
        <input id="reschedule-at" type="datetime-local" style="height:30px;border:1px solid var(--line);border-radius:6px;padding:0 8px;background:var(--panel);color:var(--ink)" />
        <select id="reschedule-tz" title="时区" style="height:30px;border:1px solid var(--line);border-radius:6px;padding:0 6px;background:var(--panel);color:var(--ink);font-size:12px">${TIMEZONES.map(tz=>`<option value="${tz}"${tz===currentTimezone()?' selected':''}>${tz}</option>`).join('')}</select>
        <button class="btn small" onclick="rescheduleArticle()">↻ 改期</button>
        <button class="btn small danger" onclick="cancelSchedule()">× 取消定时</button>
      </div></div>` : ''}
    ${publishedJob ? `<div class="status" style="margin-top:12px">已发布：<a href="${escapeHtml(publishedJob.article_url)}" target="_blank" rel="noopener noreferrer">查看文章</a></div>` : ''}
    ${deletedDraftJob ? `<div class="alert-item" style="margin-top:12px"><strong>微信草稿已删除</strong><div>本地状态已回到人工通过，可继续对话修改后重新同步草稿。</div></div>` : ''}
    <div class="actions" style="margin-top:14px">${actions || '<span class="muted">当前角色为只读</span>'}</div></div>
    <div class="panel-resizer" title="左右拖动调整对话面板宽度（双击恢复默认）" role="separator" aria-orientation="vertical"></div>
    <section class="article-chat-panel" aria-label="文章对话优化">
      <header>
        <div><strong>对话式优化</strong><small>像编辑一样连续改稿，每轮自动保存新版本</small></div>
        <div class="chat-header-actions">
          <span class="status ${state.articleChatBusy ? 'warn' : ''}">${state.articleChatBusy ? '处理中' : '在线'}</span>
          <button type="button" onclick="syncDraftFromChat()" title="把当前文章同步到微信草稿箱" ${can('publish.submit') ? '' : 'hidden'}>⇧ 存草稿</button>
          <button id="article-immersive-toggle" type="button" onclick="toggleArticleImmersive()">${state.articleImmersive ? '退出沉浸' : '沉浸'}</button>
        </div>
      </header>
      <div class="chat-messages" id="article-chat-messages"></div>
      <div class="chat-attachments" id="article-chat-attachments" ${state.pendingAttachments.length ? '' : 'hidden'}></div>
      <div class="chat-quick-actions">
        <button type="button" onclick="useArticlePrompt('请把这篇文章精简成一版适合手机阅读的短读，保留论文信息和关键指标解释。')">精简短读</button>
        <button type="button" onclick="useArticlePrompt('请加强方法取舍和实验结果的解释，让非本领域读者也能读懂。')">加强解释</button>
        <button type="button" onclick="useArticlePrompt('请重新生成一版，标题更自然，不要测试或终验痕迹。')">自然重写</button>
      </div>
      <form class="chat-composer" onsubmit="return false">
        <textarea id="article-chat-input" rows="3" placeholder="告诉万仞这轮要怎么改，例如：更短一点、标题改得更自然、加强数据解释…" ${state.articleChatBusy || !can('article.generate') ? 'disabled' : ''}></textarea>
        <button id="article-chat-send" type="button" onclick="sendArticleChat()" ${state.articleChatBusy || !can('article.generate') ? 'disabled' : ''}>${state.articleChatBusy ? '优化中…' : '发送'}</button>
      </form>
    </section></div>`;
  if (!state.articleChatBusy) {
    // ASSET（拖拽上传回执）是本地消息，重绘时保留
    const localAssets = state.articleMessages.filter(m => m.role === 'ASSET');
    state.articleMessages = [...(detail.messages || []), ...localAssets];
  }
  renderArticleChatMessages();
  renderPendingAttachments();
}

function applyChatWidth() {
  const saved = localStorage.getItem('wanren_chat_vw');
  const workbench = document.querySelector('.article-workbench');
  if (saved && workbench) workbench.style.setProperty('--chat-w', `clamp(280px, ${saved}vw, 760px)`);
}

function applySideWidth() {
  const saved = localStorage.getItem('wanren_side_vw');
  const app = document.querySelector('.app');
  if (saved && app) app.style.setProperty('--side-w', `clamp(140px, ${saved}vw, 460px)`);
}

document.addEventListener('pointerdown', event => {
  const handle = event.target.closest?.('.panel-resizer, .side-resizer');
  if (!handle) return;
  const isSide = handle.classList.contains('side-resizer');
  const container = isSide ? document.querySelector('.app') : handle.closest('.article-workbench');
  if (!container) return;
  const startX = event.clientX;
  const targetEl = isSide
    ? document.querySelector('.side')
    : container.querySelector('.article-chat-panel');
  const startWidth = targetEl ? targetEl.getBoundingClientRect().width : 420;
  // 对话面板宽度 = 基准 --chat-w × 缩放倍数 --czw；拖动以光标 1:1 跟手为准，
  // 起始宽与位移都换算回基准坐标，避免缩放态下越拖越飘
  const zoomMul = isSide ? 1
    : (parseFloat(container.style.getPropertyValue('--czw'))
      || parseFloat(getComputedStyle(container).getPropertyValue('--czw'))
      || 1);
  const startBase = startWidth / zoomMul;
  handle.classList.add('active');
  const onMove = move => {
    const delta = isSide ? (move.clientX - startX) : (startX - move.clientX);
    const min = isSide ? 150 : 280;
    const max = isSide ? 460 : 760;
    const width = Math.min(max, Math.max(min, startBase + delta / zoomMul));
    // 以视口比例存储与应用：缩放时宽度保持视觉恒定
    const ratio = (width / window.innerWidth * 100).toFixed(1);
    const cssVar = isSide ? '--side-w' : '--chat-w';
    const clampRange = isSide ? 'clamp(140px, Xvw, 460px)' : 'clamp(280px, Xvw, 760px)';
    container.style.setProperty(cssVar, clampRange.replace('X', ratio));
    if (!isSide) applyPaneZoom(); // 基准宽已更新，重算有效倍数（空间封顶随之变化）
    try { localStorage.setItem(isSide ? 'wanren_side_vw' : 'wanren_chat_vw', ratio); } catch { /* 隐私模式忽略 */ }
  };
  const onUp = () => {
    handle.classList.remove('active');
    document.removeEventListener('pointermove', onMove);
    document.removeEventListener('pointerup', onUp);
  };
  document.addEventListener('pointermove', onMove);
  document.addEventListener('pointerup', onUp);
  event.preventDefault();
});

document.addEventListener('dblclick', event => {
  const handle = event.target.closest?.('.panel-resizer, .side-resizer');
  if (!handle) return;
  const isSide = handle.classList.contains('side-resizer');
  const container = isSide ? document.querySelector('.app') : handle.closest('.article-workbench');
  try { localStorage.removeItem(isSide ? 'wanren_side_vw' : 'wanren_chat_vw'); } catch { /* 忽略 */ }
  if (container) container.style.removeProperty(isSide ? '--side-w' : '--chat-w');
});

function renderArticleChatMessages() {
  const container = $('article-chat-messages');
  if (!container) return;
  applyChatWidth();
  applyPaneZoom();
  container.innerHTML = state.articleMessages.length ? `<div class="chat-zoom">${state.articleMessages.map(message => {
    if (message.role === 'NOTICE') {
      return `<div class="chat-notice">${escapeHtml(message.content)}</div>`;
    }
    if (message.role === 'ASSET') {
      const isImage = message.assetType === 'IMAGE';
      const thumb = isImage
        ? `<img src="/api/assets/${encodeURIComponent(message.assetId)}/file" alt="${escapeHtml(message.name || '')}" loading="lazy" />`
        : `<span class="asset-file-badge">${escapeHtml(((message.name || '').split('.').pop() || 'FILE').toUpperCase())}</span>`;
      return `<div class="chat-message asset-attachment">
        <strong>素材已上传</strong>
        <div class="asset-attach-body">
          ${thumb}
          <div class="asset-attach-meta"><span class="asset-name">${escapeHtml(message.name || '')}</span><span class="muted">${escapeHtml(message.note || '')}</span></div>
        </div>
      </div>`;
    }
    if (message.role === 'TOOL') {
      const toolBlocks = message.blocks || (() => { try { return JSON.parse(message.metadata_json || '{}').blocks; } catch { return null; } })();
      const toolName = message.name || (() => { try { return JSON.parse(message.metadata_json || '{}').tool; } catch { return ''; } })();
      const clickable = Array.isArray(toolBlocks) && toolBlocks.length && message.status === 'COMPLETED';
      const replayLabel = (toolName || '工具') + '：' + message.content;
      const active = clickable && state.toolHighlight?.label === replayLabel;
      return `<div class="chat-tool ${String(message.status).toLowerCase()}${clickable ? ' clickable' : ''}${active ? ' active' : ''}"${clickable ? ` onclick="showToolHighlight(this)" data-blocks="${escapeHtml(JSON.stringify(toolBlocks))}" data-label="${escapeHtml(replayLabel)}"` : ''}><strong>${escapeHtml(toolName || message.name || '工具')}</strong><span>${escapeHtml(message.content)}${clickable ? ' <em class="tool-hint">点击高亮</em>' : ''}</span></div>`;
    }
    const meta = (() => { try { return JSON.parse(message.metadata_json || '{}'); } catch { return {}; } })();
    const atts = message.attachments || meta.attachments;
    const attChips = (atts && atts.length)
      ? `<div class="msg-attachments">${atts.map(a => a.asset_type === 'IMAGE'
          ? `<img src="/api/assets/${encodeURIComponent(a.id)}/file" alt="${escapeHtml(a.name || '')}" title="${escapeHtml(a.name || '')}" loading="lazy" />`
          : `<span class="asset-file-badge small" title="${escapeHtml(a.name || '')}">${escapeHtml(((a.name || 'FILE').split('.').pop() || 'FILE').toUpperCase())}</span>`).join('')}</div>`
      : '';
    // AI 修订完成消息带本版改动段落（version.blocks）→ 可点击回放高亮
    const msgBlocks = message.blocks || meta.blocks;
    const replayable = message.role === 'ASSISTANT' && message.status !== 'RUNNING' && Array.isArray(msgBlocks) && msgBlocks.length;
    const replayLabel = '第' + (meta.version || '?') + '版修订';
    const replayActive = replayable && state.toolHighlight?.label === replayLabel;
    const replayBtn = replayable
      ? `<button type="button" class="replay-btn${replayActive ? ' active' : ''}" onclick="showAssistantReplay(this)" data-blocks="${escapeHtml(JSON.stringify(msgBlocks))}" data-label="${escapeHtml(replayLabel)}">🖍 ${replayActive ? '收起本版改动' : '高亮本版改动'}</button>`
      : '';
    // Codex 式回退：该轮改动不好 → 恢复到上一版（初稿 v1 无更早版本，不显示）
    const turnVersion = Number(meta.version) || 0;
    const revertBtn = message.role === 'ASSISTANT' && message.status !== 'RUNNING' && message.status !== 'FAILED' && turnVersion >= 2
      ? `<button type="button" class="revert-btn" title="这轮改动不好？回退到第 ${turnVersion - 1} 版" onclick="revertArticleVersion(${turnVersion - 1})">↩ 回退</button>`
      : '';
    return `<div class="chat-message ${message.role.toLowerCase()}">
      <strong>${message.role === 'USER' ? '我' : '万仞'}</strong>
      <div>${escapeHtml(message.content)}${message.status === 'RUNNING' ? '<span class="typing-caret"></span>' : ''}${attChips}${replayBtn}${revertBtn}</div>
    </div>`;
  }).join('')}</div>` : '<div class="chat-empty">告诉万仞这轮修改目标，它会保留每一轮版本。</div>';
  container.scrollTop = container.scrollHeight;
}

/* ====================================================================
 * 局部字号缩放（Codex 式）：Ctrl+滚轮只缩放鼠标所在面板的字体，
 * 布局宽度完全不受影响（由拖拽分隔条决定）。
 * ==================================================================== */
function _readZoom(key) {
  try { return Math.min(2, Math.max(0.7, Number(localStorage.getItem(key)) || 1)); } catch { return 1; }
}

/* 面板缩放（transform 方案）：布局宽按 1/z 排版、视觉放大 z → 面板宽度（含 677px 公众号宽）恒定；
   margin-bottom 补偿让占位高度 = 视觉高度，滚动不裁切 */
/* 编辑器缩放优先用 CSS zoom：按缩放后尺寸重新栅格化文字（清晰、不发虚），
   内部 677 单位的换行与比例完全不变 = 真·所见即所得（公众号看到啥样这里就是啥样）。
   个别内嵌 webview 不支持 zoom 时回退 transform:scale（可能轻微模糊）。 */
const CSS_ZOOM_OK = (() => {
  try {
    const probe = document.createElement('div');
    probe.style.cssText = 'position:absolute;visibility:hidden;width:100px;zoom:2';
    document.body.appendChild(probe);
    const ok = Math.abs(probe.getBoundingClientRect().width - 200) < 1;
    probe.remove();
    return ok;
  } catch { return false; }
})();

function _applyPaneScale(el, cssVar, z, useCssZoom = false) {
  if (!el) return;
  el.style.setProperty(cssVar, String(z));
  if (useCssZoom && CSS_ZOOM_OK) {
    el.style.transform = 'none'; // 压掉样式表里的 scale(var(--pz))，避免与 zoom 叠加倍增
    el.style.zoom = String(z);
    el.style.marginBottom = ''; // zoom 参与布局，高度自然增长，无需补占位
    return;
  }
  el.style.zoom = '';
  el.style.transform = '';
  const layoutH = el.offsetHeight; // 未缩放布局高
  el.style.marginBottom = ((z - 1) * layoutH).toFixed(1) + 'px';
}

function chatBaseWidth() {
  // 与 CSS 对齐：未拖动 = clamp(300px,31vw,560px)；拖动存储 = clamp(280px,Xvw,760px)
  const saved = parseFloat(localStorage.getItem('wanren_chat_vw'));
  if (saved) return Math.min(760, Math.max(280, saved / 100 * window.innerWidth));
  return Math.min(560, Math.max(300, 0.31 * window.innerWidth));
}

function applyPaneZoom() {
  // 编辑器：连续等比缩放（0.7–2.0，每档 ±0.1），布局宽恒 677 与发布成品一致
  const ez = _readZoom('wanren_editor_zoom');
  const cz = _readZoom('wanren_chat_zoom');
  _applyPaneScale(document.querySelector('.preview-zoom'), '--pz', ez, true);
  const workbench = document.querySelector('.article-workbench');
  const chatZoomEl = document.querySelector('.chat-zoom');
  if (chatZoomEl && CSS_ZOOM_OK) {
    // 对话面板：内容 CSS zoom + 面板列宽等比放大（--czw）——真·整体比例，
    // 换行与 1× 完全一致；超出可用空间时自动封顶有效倍数，保证拖宽始终可用
    let eff = cz;
    if (workbench) {
      const availChat = workbench.clientWidth - 6 - 380; // 文章列最小 380 + 分隔条
      const base = chatBaseWidth();
      if (cz > 1 && base > 0) eff = Math.min(cz, Math.max(1, availChat / base));
    }
    chatZoomEl.style.transform = 'none';
    chatZoomEl.style.zoom = String(Math.round(eff * 1000) / 1000);
    chatZoomEl.style.width = '100%';
    chatZoomEl.style.marginBottom = '';
    chatZoomEl.style.setProperty('--cz', String(cz));
    workbench?.style.setProperty('--czw', String(Math.round(eff * 1000) / 1000));
  } else {
    _applyPaneScale(chatZoomEl, '--cz', cz);
    workbench?.style.setProperty('--czw', '1');
  }
}

function bumpPaneZoom(kind, factor) {
  const key = kind === 'editor' ? 'wanren_editor_zoom' : 'wanren_chat_zoom';
  const value = Math.round(Math.min(2, Math.max(0.7, _readZoom(key) + factor)) * 10) / 10;
  try { localStorage.setItem(key, String(value)); } catch { /* 忽略 */ }
  applyPaneZoom();
}

/* 内容/窗口变化后重算占位（图片加载、消息追加等都会改变布局高） */
if (typeof window !== 'undefined') {
  window.addEventListener('resize', () => applyPaneZoom());
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(() => applyPaneZoom()).observe(document.body);
  }
}

document.addEventListener('wheel', event => {
  if (!event.ctrlKey) return;
  // 中间栏全域（含工具条、装饰、空隙）一律走编辑器局部缩放；对话栏与分隔条同理
  const editorPane = event.target.closest?.('.article-main-column');
  const chatPane = event.target.closest?.('.article-chat-panel, .panel-resizer');
  if (!editorPane && !chatPane) return; // 其他区域保持浏览器原生缩放
  event.preventDefault();
  const factor = event.deltaY < 0 ? 0.1 : -0.1;
  bumpPaneZoom(editorPane ? 'editor' : 'chat', factor);
}, { passive: false, capture: true });

// 样式/文章预览弹窗 iframe 内：Ctrl+滚轮缩放弹窗本体（iframe 会吞事件，需在帧内接管）
function hookPreviewFrameZoom(frame) {
  if (!frame) return;
  const apply = () => {
    let z = 1;
    const doc = frame.contentDocument;
    if (!doc) return;
    doc.addEventListener('wheel', event => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      z = Math.min(1.8, Math.max(0.6, z + (event.deltaY < 0 ? 0.1 : -0.1)));
      frame.style.transform = `scale(${z})`;
      frame.style.transformOrigin = 'top left';
      frame.style.width = z !== 1 ? `${100 / z}%` : '';
    }, { passive: false, capture: true });
  };
  frame.addEventListener('load', apply);
  if (frame.contentDocument?.readyState === 'complete') apply();
}

function updateArticleEditorState() {
  const stateNode = $('article-editor-state');
  if (stateNode) {
    stateNode.textContent = state.articleEditorSaving
      ? '正在保存…'
      : state.articleEditorDirty ? '有未保存修改' : '已保存';
  }
  const count = $('article-word-count');
  if (count) {
    const text = [...document.querySelectorAll('.article-preview [contenteditable="true"]')]
      .map(node => node.innerText).join('');
    count.textContent = String(text.replace(/\s/g, '').length);
  }
}

function markArticleEditorDirty() {
  state.articleEditorDirty = true;
  clearTimeout(state.articleEditorTimer);
  state.articleEditorTimer = setTimeout(() => {
    void saveArticleEditor({ silent: true, rerender: false });
  }, 2200);
  updateArticleEditorState();
}

function setArticleEditable(enabled) {
  const editable = Boolean(enabled) && can('article.edit');
  document.querySelectorAll(
    '[data-article-field], [data-field="heading"], [data-paragraph-index]'
  ).forEach(node => {
    node.contentEditable = editable ? 'true' : 'false';
    if (node.dataset.articleField === 'title') node.dataset.placeholder = '输入标题';
    else if (node.dataset.articleField === 'summary') node.dataset.placeholder = '输入摘要';
    else if (node.dataset.field === 'heading') node.dataset.placeholder = '输入章节标题';
    else node.dataset.placeholder = '输入段落内容';
  });
  document.querySelectorAll('.article-editor-toolbar button').forEach(button => {
    button.disabled = !editable;
  });
  updateArticleEditorState();
}

function collectArticleEditorContent() {
  const title = document.querySelector('[data-article-field="title"]')?.innerText.trim() || '';
  const summary = document.querySelector('[data-article-field="summary"]')?.innerText.trim() || '';
  const sections = [...document.querySelectorAll('.article-editor-section')].map((section, index) => {
    let paragraphs = [...section.querySelectorAll(':scope > [data-paragraph-index]')]
      .map(node => node.innerText.trim())
      .filter(Boolean);
    if (!paragraphs.length) paragraphs = [''];
    let assetRefs = [];
    try {
      assetRefs = JSON.parse(section.dataset.assetRefs || '[]');
    } catch { assetRefs = []; }
    let style = {};
    try {
      style = JSON.parse(section.dataset.style || '{}');
    } catch { style = {}; }
    return {
      id: section.dataset.sectionId || `sec_${String(index + 1).padStart(2, '0')}`,
      heading: section.querySelector(':scope > [data-field="heading"]')?.innerText.trim() || `章节 ${index + 1}`,
      paragraphs,
      asset_refs: assetRefs,
      style,
    };
  });
  return { title, summary, sections };
}

async function saveArticleEditor(options = {}) {
  if (!state.currentArticleId || !state.articleEditorDirty || state.articleEditorSaving) {
    if (state.articleEditorSavePromise) await state.articleEditorSavePromise;
    return null;
  }
  clearTimeout(state.articleEditorTimer);
  const content = collectArticleEditorContent();
  if (!content.title || !content.sections.length) {
    if (!options.silent) toast('标题和至少一个章节不能为空', true);
    return null;
  }
  state.articleEditorSaving = true;
  updateArticleEditorState();
  state.articleEditorSavePromise = api(`/api/articles/${state.currentArticleId}`, {
    method: 'PUT',
    body: {
      ...content,
      change_summary: options.changeSummary || '沉浸式手动编辑',
    },
  });
  try {
    await state.articleEditorSavePromise;
    state.articleEditorDirty = false;
    if (!options.silent) toast('已保存手动编辑版本');
    if (options.rerender !== false) {
      await renderArticle();
      await loadSession();
      await refreshArticles();
      await refreshDashboard();
    }
    return content;
  } catch (error) {
    if (!options.silent) toast(error.message, true);
    throw error;
  } finally {
    state.articleEditorSaving = false;
    state.articleEditorSavePromise = null;
    updateArticleEditorState();
  }
}

function makeArticleParagraph(text = '新段落') {
  const node = document.createElement('p');
  node.dataset.paragraphIndex = '0';
  node.dataset.sectionIndex = '0';
  node.innerText = text;
  node.contentEditable = can('article.edit') ? 'true' : 'false';
  node.dataset.placeholder = '输入段落内容';
  return node;
}

function addArticleParagraph() {
  const anchorSection = state.articleEditorAnchor?.closest('.article-editor-section')
    || document.querySelector('.article-editor-section:last-of-type');
  if (!anchorSection) return;
  const paragraph = makeArticleParagraph();
  const activeParagraph = state.articleEditorAnchor?.matches('[data-paragraph-index]')
    ? state.articleEditorAnchor
    : anchorSection.querySelector('[data-paragraph-index]:last-of-type');
  if (activeParagraph) activeParagraph.after(paragraph);
  else anchorSection.querySelector('[data-field="heading"]')?.after(paragraph);
  const index = [...anchorSection.querySelectorAll(':scope > [data-paragraph-index]')].indexOf(paragraph);
  paragraph.dataset.paragraphIndex = String(Math.max(0, index));
  paragraph.dataset.sectionIndex = anchorSection.dataset.sectionIndex || '0';
  state.articleEditorAnchor = paragraph;
  paragraph.focus();
  markArticleEditorDirty();
}

function deleteArticleBlock() {
  const paragraph = state.articleEditorAnchor?.matches('[data-paragraph-index]')
    ? state.articleEditorAnchor
    : null;
  if (!paragraph) {
    toast('请先把光标放在要删除的段落里', true);
    return;
  }
  const section = paragraph.closest('.article-editor-section');
  if (section && section.querySelectorAll('[data-paragraph-index]').length <= 1) {
    toast('每个章节至少保留一个段落', true);
    return;
  }
  paragraph.remove();
  state.articleEditorAnchor = null;
  markArticleEditorDirty();
}

function addArticleSection() {
  const container = document.querySelector('.article-preview');
  const previous = document.querySelector('.article-editor-section:last-of-type');
  if (!container || !previous) return;
  const section = document.createElement('section');
  const sectionCount = document.querySelectorAll('.article-editor-section').length + 1;
  section.className = 'article-editor-section';
  section.dataset.sectionId = `sec_${String(sectionCount).padStart(2, '0')}`;
  section.dataset.sectionIndex = String(sectionCount - 1);
  section.dataset.assetRefs = '[]';
  const heading = document.createElement('h4');
  heading.dataset.field = 'heading';
  heading.dataset.sectionIndex = String(sectionCount - 1);
  heading.innerText = '新章节';
  heading.contentEditable = can('article.edit') ? 'true' : 'false';
  heading.dataset.placeholder = '输入章节标题';
  const paragraph = makeArticleParagraph('在这里补充这一章的内容。');
  paragraph.dataset.sectionIndex = String(sectionCount - 1);
  section.append(heading, paragraph);
  previous.after(section);
  state.articleEditorAnchor = paragraph;
  paragraph.focus();
  markArticleEditorDirty();
}

function deleteArticleSection() {
  const sections = [...document.querySelectorAll('.article-editor-section')];
  if (sections.length <= 1) {
    toast('至少保留一个章节', true);
    return;
  }
  const section = state.articleEditorAnchor?.closest('.article-editor-section') || sections.at(-1);
  section.remove();
  state.articleEditorAnchor = null;
  markArticleEditorDirty();
}

function enterArticleImmersiveEditor() {
  setArticleImmersive(true);
  document.querySelector('[data-article-field="title"]')?.focus();
}

function setArticleImmersive(enabled) {
  state.articleImmersive = Boolean(enabled);
  document.body.classList.toggle('article-immersive', state.articleImmersive);
  if (!state.articleImmersive && state.articleEditorDirty) {
    void saveArticleEditor({ silent: true, rerender: false });
  }
  setArticleEditable(state.articleImmersive);
  const button = $('article-immersive-toggle');
  if (button) button.textContent = state.articleImmersive ? '退出沉浸' : '沉浸';
}

function toggleArticleImmersive() {
  setArticleImmersive(!state.articleImmersive);
  // 退出沉浸时回到排版预览视图（中间栏恢复按样式渲染的成品效果）
  if (!state.articleImmersive && state.articleRenderMode === 'edit') {
    state.articleRenderMode = 'render';
    void renderArticle().catch(() => {});
    return;
  }
  renderArticleChatMessages();
}

async function syncDraftStatus(
  articleId = state.currentArticleId,
  options = {},
) {
  if (!articleId) throw new Error('请先打开文章');
  const result = await api(`/api/articles/${articleId}/draft-sync`, { method: 'POST', body: {} });
  if (!options.silent) {
    if (result.status === 'DELETED') toast('微信草稿已删除，本地状态已更新');
    else if (result.status === 'EXISTS') toast('微信草稿仍存在');
    else toast('当前版本没有微信草稿');
  }
  if (options.refresh !== false) {
    await renderArticle();
    await loadSession();
    await refreshArticles();
    await refreshDashboard();
  }
  return result;
}

async function syncAllDraftStatuses() {
  const button = $('sync-draft-status');
  button.disabled = true;
  button.textContent = '同步中…';
  try {
    const result = await api('/api/articles/draft-sync', { method: 'POST', body: {} });
    toast(`已核对 ${result.checked} 篇：存在 ${result.exists}，微信端删除 ${result.deleted}`);
    await refreshArticles();
    await refreshDashboard();
    if (state.currentSession) await loadSession();
    return result;
  } finally {
    button.disabled = !can('publish.submit');
    button.textContent = '⟳ 同步微信草稿状态';
  }
}

function useArticlePrompt(text) {
  if (state.articleChatBusy) return;
  $('article-chat-input').value = text;
  $('article-chat-input').focus();
}

function addArticleToolMessage(message) {
  const runningAssistant = [...state.articleMessages].reverse().find(item => item.role === 'ASSISTANT' && item.status === 'RUNNING');
  if (runningAssistant) {
    const index = state.articleMessages.lastIndexOf(runningAssistant);
    state.articleMessages.splice(index, 0, message);
    return;
  }
  state.articleMessages.push(message);
}

function setArticleChatBusy(busy) {
  state.articleChatBusy = busy;
  const input = $('article-chat-input');
  const button = $('article-chat-send');
  if (input) input.disabled = busy || !can('article.generate');
  if (button) {
    if (busy) {
      // 改稿进行中：发送按钮变成可点的「终止改稿」（两步确认），随时可打断
      button.disabled = false;
      button.textContent = '■ 终止改稿';
      button.setAttribute('onclick', 'abortArticleChatBtn(this)');
      button.style.background = 'var(--amber-soft,#fff3e3)';
      button.style.borderColor = 'var(--amber,#b76a1b)';
      button.style.color = 'var(--amber,#b76a1b)';
    } else {
      button.disabled = !can('article.generate');
      button.textContent = '发送';
      button.setAttribute('onclick', 'sendArticleChat()');
      button.style.background = '';
      button.style.borderColor = '';
      button.style.color = '';
    }
  }
}

// ============ 两步确认终止（防误点；armed 态存 state，跨面板重渲染存活） ============

const _abortArmed = { key: null, at: 0 };
const ABORT_ARM_WINDOW_MS = 5000;

function armStopButton(btn, key, action) {
  if (_abortArmed.key === key && Date.now() - _abortArmed.at < ABORT_ARM_WINDOW_MS) {
    _abortArmed.key = null;
    action();
    return;
  }
  _abortArmed.key = key;
  _abortArmed.at = Date.now();
  if (btn) {
    btn.textContent = '再点一次终止';
    btn.style.background = 'var(--red-soft,#fdecea)';
    btn.style.borderColor = 'var(--red,#a63a2f)';
    btn.style.color = 'var(--red,#a63a2f)';
  }
}

function abortProposalBtn(btn) { armStopButton(btn, 'proposal', abortProposalThinking); }
function abortGenerationBtn(btn) { armStopButton(btn, 'generation', abortPendingGeneration); }
function abortArticleChatBtn(btn) { armStopButton(btn, 'chat', abortArticleChat); }

function abortArticleChat() {
  try { state.articleChatController?.abort(); } catch { /* 忽略 */ }
  state.articleChatController = null;
  const assistant = [...state.articleMessages].reverse().find(item => item.role === 'ASSISTANT' && item.status === 'RUNNING');
  if (assistant) assistant.status = 'FAILED';
  state.articleMessages.push({ role: 'NOTICE', content: '已终止本次改稿；服务端可能仍在后台完成，稍后可刷新查看新版本。' });
  setArticleChatBusy(false);
  renderArticleChatMessages();
}

async function sendArticleChat() {
  if (!state.currentArticleId || state.articleChatBusy) return;
  const input = $('article-chat-input');
  const instruction = input.value.trim();
  const hasAttachments = state.pendingAttachments.length > 0;
  if (instruction.length < 2 && !hasAttachments) {
    toast('请输入更具体的修改要求（或拖入素材直接发送）', true);
    return;
  }
  input.value = '';
  setArticleChatBusy(true);
  // 待发素材先上传（挂到当前文章对话），随指令一起发送
  let attachments = [];
  if (hasAttachments) attachments = await flushPendingAttachments();
  state.articleMessages.push({ role: 'USER', content: instruction || '（请根据附件素材处理）', status: 'COMPLETED', attachments });
  state.articleMessages.push({ role: 'ASSISTANT', content: '', status: 'RUNNING' });
  renderArticleChatMessages();
  let completed = null;
  try {
    if (state.articleEditorDirty || state.articleEditorSaving) {
      await saveArticleEditor({ silent: true, rerender: false, changeSummary: '对话前保存手动编辑' });
      addArticleToolMessage({ role: 'TOOL', name: '保存手动修改', content: '已把左侧未保存编辑保存为新版本', status: 'COMPLETED' });
      renderArticleChatMessages();
    }
    if (state.assistMode) {
      await sendArticleAssist(instruction || '（请根据附件素材处理）', attachments);
      return;
    }
    state.articleChatController = new AbortController();
    const response = await fetch(`/api/articles/${state.currentArticleId}/chat`, {
      method: 'POST',
      headers: {
        ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}),
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ instruction: instruction || '（请根据附件素材处理）', attachments }),
      signal: state.articleChatController.signal,
    });
    if (!response.ok || !response.body) {
      const text = await response.text();
      throw new Error(JSON.parse(text || '{}').detail || response.statusText);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split('\n\n');
      buffer = blocks.pop() || '';
      for (const block of blocks) {
        const eventLine = block.split('\n').find(line => line.startsWith('event: '));
        const dataLine = block.split('\n').find(line => line.startsWith('data: '));
        if (!eventLine || !dataLine) continue;
        const eventName = eventLine.slice(7).trim();
        const data = JSON.parse(dataLine.slice(6));
        if (eventName === 'tool') {
          addArticleToolMessage({ role: 'TOOL', name: data.name, content: data.message, status: data.status });
        } else if (eventName === 'state') {
          addArticleToolMessage({ role: 'TOOL', name: '状态', content: data.message, status: 'RUNNING' });
        } else if (eventName === 'delta') {
          const assistant = [...state.articleMessages].reverse().find(item => item.role === 'ASSISTANT');
          if (assistant) assistant.content += data.content || '';
        } else if (eventName === 'completed') {
          completed = data;
          const assistant = [...state.articleMessages].reverse().find(item => item.role === 'ASSISTANT');
          if (assistant) assistant.status = 'COMPLETED';
        } else if (eventName === 'error') {
          throw new Error(data.message || 'AI 优化失败');
        }
        renderArticleChatMessages();
      }
    }
    if (completed) {
      state.currentArticleId = completed.article.id;
      await renderArticle();
      await loadSession();
      await refreshAll();
      renderChatTree(); // 侧栏树立即反映新版本/新对话，无需手动刷新
      toast(`已保存第 ${completed.version} 版`);
    }
  } catch (error) {
    if (error && error.name === 'AbortError') return; // 用户终止：abortArticleChat 已处理界面
    const assistant = [...state.articleMessages].reverse().find(item => item.role === 'ASSISTANT');
    if (assistant) {
      assistant.status = 'FAILED';
      assistant.content = assistant.content || error.message;
    }
    renderArticleChatMessages();
    toast(error.message, true);
  } finally {
    setArticleChatBusy(false);
    renderArticleChatMessages();
  }
}

async function rerunReview() { await api(`/api/articles/${state.currentArticleId}/review`, { method: 'POST', body: {} }); await renderArticle(); await loadSession(); await refreshAll(); }

async function editArticle() {
  const detail = await api(`/api/articles/${state.currentArticleId}`);
  const content = JSON.parse(detail.article.content_json);
  const editors = content.sections.map((s, index) => `
    <div class="field"><label>段落 ${index + 1} 标题</label><input id="edit-heading-${index}" value="${escapeHtml(s.heading)}" /></div>
    <div class="field"><label>段落内容（换行分段）</label><textarea id="edit-text-${index}">${escapeHtml(s.paragraphs.join('\n'))}</textarea></div>
    <div class="field"><label>素材引用（逗号分隔）</label><input id="edit-assets-${index}" value="${escapeHtml(s.asset_refs.join(','))}" /></div>`).join('');
  $('article-panel').innerHTML = `<div class="field"><label>标题</label><input id="edit-title" value="${escapeHtml(content.title)}" /></div>
    <div class="field"><label>摘要</label><textarea id="edit-summary">${escapeHtml(content.summary)}</textarea></div>${editors}
    <div class="field"><label>修改说明</label><input id="edit-change" value="人工编辑" /></div>
    <div class="actions"><button class="btn primary" onclick="saveArticleEdit(${content.sections.length})">⇧ 保存新版本</button><button class="btn" onclick="renderArticle()">↺ 取消</button></div>`;
}

async function saveArticleEdit(count) {
  const sections = [];
  for (let i = 0; i < count; i++) {
    sections.push({
      id: `sec_${String(i + 1).padStart(2, '0')}`,
      heading: $(`edit-heading-${i}`).value,
      paragraphs: $(`edit-text-${i}`).value.split(/\n+/).map(x => x.trim()).filter(Boolean),
      asset_refs: $(`edit-assets-${i}`).value.split(/[,，]/).map(x => x.trim()).filter(Boolean),
    });
  }
  await api(`/api/articles/${state.currentArticleId}`, { method: 'PUT', body: { title: $('edit-title').value, summary: $('edit-summary').value, sections, change_summary: $('edit-change').value } });
  toast('已保存人工编辑版本');
  await renderArticle();
  await loadSession();
  await refreshAll();
}

async function decideArticle(approved) {
  await api(`/api/articles/${state.currentArticleId}/${approved ? 'approve' : 'reject'}`, { method: 'POST', body: { reviewer: 'browser-operator' } });
  toast(approved ? '人工审核通过' : '已驳回');
  await renderArticle();
  await loadSession();
  await refreshAll();
}

async function confirmOriginalDeclaration() {
  await api(`/api/articles/${state.currentArticleId}/original-declaration`, {
    method: 'POST',
    body: { reviewer: 'browser-operator', confirmed: true },
  });
  toast('原创声明已确认');
  await renderArticle();
  await loadSession();
  await refreshAll();
}

async function publishArticle(mode) {
  const job = await api(`/api/articles/${state.currentArticleId}/publish`, { method: 'POST', body: { mode } });
  toast(`发布任务 ${job.status}`);
  await renderArticle();
  await loadSession();
  await refreshAll();
}

async function scheduleArticle() {
  const value = $('schedule-at')?.value;
  if (!value) throw new Error('请选择计划发布时间');
  const job = await api(`/api/articles/${state.currentArticleId}/schedule`, {
    method: 'POST',
    body: { scheduled_at: value, timezone: $('schedule-tz')?.value || '' },
  });
  toast(`定时发布已创建：${formatDate(job.scheduled_at)}`);
  await renderArticle();
  await loadSession();
  await refreshAll();
}

async function rescheduleArticle() {
  const value = $('reschedule-at')?.value;
  if (!value) throw new Error('请选择新的发布时间');
  const job = await api(`/api/articles/${state.currentArticleId}/schedule`, {
    method: 'PUT',
    body: { scheduled_at: value, timezone: $('reschedule-tz')?.value || $('schedule-tz')?.value || '' },
  });
  toast(`定时发布已改期：${formatDate(job.scheduled_at)}`);
  await renderArticle();
  await loadSession();
  await refreshAll();
}

async function cancelSchedule() {
  const job = await api(`/api/articles/${state.currentArticleId}/schedule`, { method: 'DELETE' });
  toast(`定时发布已取消：${job.status}`);
  await renderArticle();
  await loadSession();
  await refreshAll();
}

async function createAccount() {
  const payload = {
    name: $('account-name').value,
    app_id: $('account-app-id').value,
    style: { accent: $('account-accent').value, text: $('account-text').value, font: $('account-font').value },
    callback_base_url: $('account-callback-base').value || null,
  };
  const account = await api('/api/accounts', { method: 'POST', body: payload });
  toast('公众号已保存，正在检测连接');
  await refreshAccounts();
  await refreshSessions();
  if (can('account.manage')) await verifyAccount(account.id);
}

async function verifyAccount(accountId) {
  const result = await api(`/api/accounts/${accountId}/verify`, { method: 'POST', body: {} });
  const message = result.current_ip
    ? `${result.status}：当前出口 IP ${result.current_ip}`
    : `${result.status}：${result.message || '检测完成'}`;
  toast(message, result.status !== 'READY' && result.status !== 'MOCK_MODE');
  await refreshAccounts();
  return result;
}

function useTemplate(templateId) {
  $('session-template').value = templateId;
  switchView('workspace');
  toast('模板已选择，可在新任务中使用');
}

async function createTemplate() {
  let sections;
  try {
    sections = JSON.parse($('template-sections').value);
  } catch (error) {
    throw new Error(`章节 JSON 格式错误：${error.message}`);
  }
  if (!Array.isArray(sections) || sections.length < 3) throw new Error('模板至少需要 3 个章节');
  const payload = {
    name: $('template-name').value,
    description: $('template-description').value,
    official_account_id: $('template-account').value || null,
    sections,
    style: {
      accent: $('template-accent').value,
      text: $('template-text').value,
      font: $('template-font').value,
    },
  };
  if (state.editingTemplate) {
    await api(`/api/templates/${state.editingTemplate}`, {
      method: 'PUT',
      body: { ...payload, change_summary: $('template-change-summary')?.value || '更新模板' },
    });
    toast('模板已更新为新版本');
  } else {
    await api('/api/templates', { method: 'POST', body: payload });
    toast('模板已保存');
  }
  resetTemplateEditor();
  await refreshTemplates();
}

function resetTemplateEditor() {
  state.editingTemplate = null;
  $('template-name').value = '';
  $('template-description').value = '';
  $('template-account').value = '';
  $('template-sections').value = `[
  {"id":"problem","heading":"论文要解决什么问题","guidance":"说明现有方式的限制和研究动机。","image_policy":"required"},
  {"id":"method","heading":"方法是怎么设计的","guidance":"解释核心模块与系统流程。","image_policy":"optional"},
  {"id":"results","heading":"效果有多好","guidance":"引用论文中的指标和实验条件。","image_policy":"optional"}
]`;
  $('template-accent').value = '#175641';
  $('template-text').value = '#262626';
  $('template-font').value = 'system';
  $('create-template').textContent = '＋ 保存模板';
  $('cancel-template-edit').hidden = true;
}

function editTemplate(templateId) {
  const template = state.templates.find(item => item.id === templateId);
  if (!template) throw new Error('模板不存在');
  state.editingTemplate = templateId;
  $('template-name').value = template.name;
  $('template-description').value = template.description || '';
  $('template-account').value = template.official_account_id || '';
  const style = JSON.parse(template.style_json || '{}');
  $('template-accent').value = style.accent || '#175641';
  $('template-text').value = style.text || '#262626';
  $('template-font').value = style.font || 'system';
  const container = $('cancel-template-edit').parentElement;
  let summary = $('template-change-summary');
  if (!summary) {
    summary = document.createElement('input');
    summary.id = 'template-change-summary';
    summary.placeholder = '版本说明';
    summary.style.cssText = 'height:36px;flex:1;min-width:180px;border:1px solid var(--line);border-radius:6px;padding:0 10px;background:var(--panel);color:var(--ink)';
    container.insertBefore(summary, $('cancel-template-edit'));
  }
  summary.value = '';
  summary.hidden = false;
  $('template-sections').value = JSON.stringify(JSON.parse(template.structure_json || '[]'), null, 2);
  $('create-template').textContent = '⇧ 保存新版本';
  $('cancel-template-edit').hidden = false;
  $('template-sections').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function showTemplateHistory(templateId) {
  state.templateVersions = await api(`/api/templates/${templateId}/versions`);
  renderTemplateVersions();
  $('template-history-panel').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function hideTemplateHistory() {
  state.templateVersions = [];
  renderTemplateVersions();
}

async function searchTemplateReferences() {
  const query = $('template-search-query').value.trim();
  if (!query) throw new Error('请先填写搜索关键词');
  const result = await api('/api/templates/search', { method: 'POST', body: { query } });
  $('template-reference').value = result.results.map(item => `${item.title}\n${item.url}\n${item.snippet}`).join('\n\n');
  toast(`已找到 ${result.results.length} 条参考`);
  return result;
}

async function generateTemplate() {
  const autoSearch = $('template-auto-search').checked;
  if (autoSearch && !$('template-search-query').value.trim() && $('template-prompt').value.trim().length < 8) {
    throw new Error('自动搜索需要关键词或更完整的模板需求');
  }
  await api('/api/templates/generate', {
    method: 'POST',
    body: {
      description: $('template-prompt').value,
      reference: $('template-reference').value,
      search_query: $('template-search-query').value,
      auto_search: autoSearch,
      official_account_id: $('generate-template-account').value || null,
    },
  });
  toast('AI 模板已生成');
  await refreshTemplates();
}

async function createUser() {
  await api('/api/users', {
    method: 'POST',
    body: {
      email: $('user-email').value,
      name: $('user-name').value,
      password: $('user-password').value,
      role: $('user-role').value,
    },
  });
  toast('用户已创建');
  $('user-password').value = '';
  await refreshUsers();
}

async function showWorkflow(runId) {
  const detail = await api(`/api/workflows/${runId}`);
  $('workflow-detail').innerHTML = `<div style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px"><strong>节点轨迹</strong>
    ${detail.nodes.map(node => `<div class="row"><div><div class="row-title">${escapeHtml(node.node_key)}</div><div class="muted">尝试 ${node.attempt} · ${node.duration_ms} ms</div></div><span class="status ${statusClass(node.status)}">${escapeHtml(node.status)}</span></div>`).join('')}</div>`;
}

async function changeWorkflowPage(delta) {
  const current = state.monitorWorkflows;
  const nextPage = Math.max(1, Math.min(current.page + delta, current.total_pages || 1));
  if (nextPage === current.page) return;
  current.page = nextPage;
  await refreshMonitorWorkflows();
}

async function changeWorkflowPageSize(value) {
  state.monitorWorkflows.page = 1;
  state.monitorWorkflows.page_size = Number(value);
  await refreshMonitorWorkflows();
}

async function changeAuditPage(delta) {
  const current = state.auditPage;
  const nextPage = Math.max(1, Math.min(current.page + delta, current.total_pages || 1));
  if (nextPage === current.page) return;
  current.page = nextPage;
  await refreshAudits();
}

async function changeAuditPageSize(value) {
  state.auditPage.page = 1;
  state.auditPage.page_size = Number(value);
  await refreshAudits();
}

async function refreshAll() {
  await Promise.all([
    refreshDashboard(),
    refreshSessions(),
    refreshArticles(),
    refreshAccounts(),
    refreshMonitorWorkflows(),
    refreshAudits(),
    refreshUsers(),
    loadTypesettingStyles(),
  ]);
  renderCurrentView();
}

async function backgroundRefresh() {
  try {
    await refreshDashboard();
    if (state.dashboard?.queued || state.dashboard?.running) {
      await Promise.all([refreshSessions(), refreshArticles()]);
      if (state.currentSession) await loadSession();
      renderCurrentView();
    }
  } catch { /* transient polling errors are shown by explicit actions */ }
}

function showLogin(message = '') {
  $('login-overlay').style.display = 'grid';
  $('login-message').textContent = message;
  $('login-password').value = '';
  $('login-message').textContent = message;
}

function hideLogin() {
  $('login-overlay').style.display = 'none';
  $('login-message').textContent = '';
}

function renderCurrentUser() {
  if (state.user?.email) {
    $('current-user').textContent = `${state.user.name} · ${state.user.role}`;
    $('logout-button').style.display = 'inline-flex';
  } else {
    $('current-user').textContent = 'V1 本地闭环';
    $('logout-button').style.display = 'none';
  }
  applyPermissions();
}

function applyPermissions() {
  const controls = [
    ['create-session', 'session.write'],
    ['upload-assets', 'asset.write'],
    ['generate-article', 'article.generate'],
    ['sync-draft-status', 'publish.submit'],
    ['asset-input', 'asset.write'],
    ['create-account', 'account.manage'],
    ['create-template', 'article.generate'],
    ['generate-template', 'article.generate'],
    ['create-user', 'user.manage'],
  ];
  controls.forEach(([id, permission]) => {
    const element = $(id);
    if (!element) return;
    element.disabled = !can(permission);
    element.title = can(permission) ? '' : `需要权限：${permission}`;
  });

  document.querySelector('[data-view="monitor"]').style.display = can('audit.read') ? '' : 'none';
  document.querySelector('[data-view="users"]').style.display = can('user.manage') ? '' : 'none';
  const accountCreatePanel = $('create-account')?.closest('.panel');
  if (accountCreatePanel) accountCreatePanel.style.display = can('account.manage') ? '' : 'none';
  const manualTemplatePanel = $('create-template')?.closest('.panel');
  if (manualTemplatePanel) manualTemplatePanel.style.display = can('article.generate') ? '' : 'none';
  const aiTemplatePanel = $('generate-template')?.closest('.panel');
  if (aiTemplatePanel) aiTemplatePanel.style.display = can('article.generate') ? '' : 'none';
  const userCreatePanel = $('create-user')?.closest('.panel');
  if (userCreatePanel) userCreatePanel.style.display = can('user.manage') ? '' : 'none';
}

async function login() {
  const button = $('login-button');
  button.classList.add('loading');
  button.disabled = true;
  try {
    const result = await api('/api/auth/login', {
      method: 'POST',
      body: { email: $('login-email').value, password: $('login-password').value },
    });
    state.token = result.token;
    state.user = result.user;
    renderCurrentUser();
    state.view = 'workspace';
    switchView('workspace');
    hideLogin();
    await refreshAll();
    if (!state.pendingGeneration && !state.currentArticleId && state.sessions.length && !state.currentSession) {
      state.currentSession = state.sessions[0].id;
      await loadSession();
    }
    toast(`已登录：${state.user.name}`);
  } catch (error) {
    $('login-message').textContent = error.message;
  } finally {
    button.classList.remove('loading');
    button.disabled = false;
  }
}

async function logout() {
  try {
    await api('/api/auth/logout', { method: 'POST', body: {} });
  } catch { /* an expired local token is also logged out */ }
  state.token = null;
  state.user = null;
  renderCurrentUser();
  window.location.href = '/landing';
}

const themes = [
  { key: 'mo', label: '苍青', color: '#175641' },
  { key: 'azure', label: '云蓝', color: '#0f5f8f' },
  { key: 'graphite', label: '岩灰', color: '#49a795' },
  { key: 'vermilion', label: '朱砂', color: '#a63d2a' },
  { key: 'violet', label: '紫罗兰', color: '#5d55ae' },
  { key: 'tide', label: '潮汐', color: '#22696d' },
];

function applyTheme(themeKey) {
  const selected = themes.find(theme => theme.key === themeKey) || themes[0];
  state.theme = selected.key;
  document.documentElement.dataset.theme = selected.key;
  localStorage.setItem('wanren_theme', selected.key);
  document.querySelectorAll('.theme-switcher').forEach(container => {
    container.innerHTML = themes.map(theme => `
      <button type="button" class="theme-dot ${theme.key === selected.key ? 'active' : ''}"
        data-theme-key="${theme.key}" title="${theme.label}" aria-label="${theme.label}主题"
        style="background:${theme.color}"></button>`).join('');
    container.querySelectorAll('.theme-dot').forEach(button => {
      button.onclick = () => applyTheme(button.dataset.themeKey);
    });
  });
}

function switchAuthMode(mode) {
  const loginActive = mode === 'login';
  $('login-tab').classList.toggle('active', loginActive);
  $('register-tab').classList.toggle('active', !loginActive);
  $('auth-login-panel').hidden = !loginActive;
  $('auth-register-panel').hidden = loginActive;
}

async function loadAuthConfig() {
  try {
    state.authConfig = await api('/api/auth/config');
    const enabled = Boolean(state.authConfig.registration_enabled);
    $('register-disabled').hidden = enabled;
    ['register-email', 'register-name', 'register-password', 'register-confirm', 'register-button'].forEach(id => {
      $(id).disabled = !enabled;
    });
  } catch {
    state.authConfig.registration_enabled = false;
    $('register-disabled').hidden = false;
  }
}

async function register() {
  const button = $('register-button');
  button.classList.add('loading');
  button.disabled = true;
  try {
    const result = await api('/api/auth/register', {
      method: 'POST',
      body: {
        email: $('register-email').value,
        name: $('register-name').value,
        password: $('register-password').value,
        confirm_password: $('register-confirm').value,
      },
    });
    state.token = result.token;
    state.user = result.user;
    renderCurrentUser();
    state.view = 'workspace';
    switchView('workspace');
    hideLogin();
    await refreshAll();
    toast(`注册成功：${state.user.name}`);
  } catch (error) {
    $('register-message').textContent = error.message;
  } finally {
    button.classList.remove('loading');
    button.disabled = !state.authConfig.registration_enabled;
  }
}

async function init() {
  // 会话恢复：生成中的新对话续接轮询；否则回到最近打开的文章（Codex 式回到现场）
  try {
    const pendingProposalSession = localStorage.getItem('wanren_pending_proposal');
    const pendingSession = localStorage.getItem('wanren_pending_session');
    const lastArticle = localStorage.getItem('wanren_last_article');
    if (pendingProposalSession) {
      // 提案待确认的新对话：续接轮询（提案可能已生成）
      state.pendingProposal = { sessionId: pendingProposalSession, instruction: '', startedAt: Date.now() };
      state.currentSession = pendingProposalSession;
      setTimeout(() => { void pollPendingProposal(); }, 600);
    } else if (pendingSession) {
      state.pendingGeneration = { sessionId: pendingSession, instruction: '', startedAt: Date.now() };
      state.currentSession = pendingSession;
      setTimeout(() => { void pollPendingGeneration(); }, 600); // 等 refreshAll 就绪后续接轮询
    } else if (lastArticle) {
      state.currentArticleId = lastArticle;
    }
  } catch { /* 忽略 */ }
  document.querySelectorAll('.nav-button').forEach(button => {
    button.onclick = () => switchView(button.dataset.view);
  });
  $('global-search').oninput = event => { state.search = event.target.value.trim(); renderCurrentView(); renderChatTree(); };
  $('create-session').onclick = async () => { try { await createSession(); } catch (e) { toast(e.message, true); } };
  $('upload-assets').onclick = async () => { try { await uploadAssets(); } catch (e) { toast(e.message, true); } };
  $('generate-article').onclick = async () => { try { await generateArticle(); } catch (e) { toast(e.message, true); } };
  $('create-account').onclick = async () => { try { await createAccount(); } catch (e) { toast(e.message, true); } };
  $('import-style-button').onclick = () => openStyleImport();
  $('create-user').onclick = async () => { try { await createUser(); } catch (e) { toast(e.message, true); } };
  $('sync-draft-status').onclick = async () => { try { await syncAllDraftStatuses(); } catch (e) { toast(e.message, true); } };
  $('login-button').onclick = async () => { await login(); };
  $('register-button').onclick = async () => { await register(); };
  $('logout-button').onclick = async () => { await logout(); };
  $('article-status-filter').onchange = event => {
    state.articleStatusFilter = event.target.value;
    renderArticles();
  };
  $('workflow-prev-page').onclick = async () => { try { await changeWorkflowPage(-1); } catch (e) { toast(e.message, true); } };
  $('workflow-next-page').onclick = async () => { try { await changeWorkflowPage(1); } catch (e) { toast(e.message, true); } };
  $('workflow-page-size').onchange = async event => { try { await changeWorkflowPageSize(event.target.value); } catch (e) { toast(e.message, true); } };
  $('audit-prev-page').onclick = async () => { try { await changeAuditPage(-1); } catch (e) { toast(e.message, true); } };
  $('audit-next-page').onclick = async () => { try { await changeAuditPage(1); } catch (e) { toast(e.message, true); } };
  $('audit-page-size').onchange = async event => { try { await changeAuditPageSize(event.target.value); } catch (e) { toast(e.message, true); } };
  $('login-password').addEventListener('keydown', event => { if (event.key === 'Enter') login(); });
  $('register-password').addEventListener('keydown', event => { if (event.key === 'Enter') register(); });
  $('register-confirm').addEventListener('keydown', event => { if (event.key === 'Enter') register(); });
  $('login-tab').onclick = () => switchAuthMode('login');
  $('register-tab').onclick = () => switchAuthMode('register');
  $('article-panel').addEventListener('input', event => {
    if (event.target.matches('[data-article-field], [data-field="heading"], [data-paragraph-index]')) {
      markArticleEditorDirty();
    }
  });
  $('article-panel').addEventListener('focusin', event => {
    if (event.target.matches('[data-article-field], [data-field="heading"], [data-paragraph-index]')) {
      state.articleEditorAnchor = event.target;
    }
  });
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's' && state.articleImmersive) {
      event.preventDefault();
      void saveArticleEditor();
    }
  });
  applyTheme(localStorage.getItem('wanren_theme') || 'mo');
  await loadAuthConfig();
  try {
    state.user = await api('/api/auth/me');
    renderCurrentUser();
  } catch (error) {
    if (error.status === 401) {
      window.location.href = '/landing';
      return;
    }
    throw error;
  }
  await refreshAll();
  // 恢复优先：生成中的新对话 / 最近文章不被动选择覆盖
  if (!state.pendingGeneration && !state.currentArticleId && state.sessions.length && !state.currentSession) {
    state.currentSession = state.sessions[0].id;
    await loadSession();
  }
  setInterval(backgroundRefresh, 2000);
}

window.switchView = switchView;
window.selectSession = selectSession;
window.openArticle = openArticle;
window.analyzeAsset = analyzeAsset;
window.transcodeAsset = transcodeAsset;
window.verifyAccount = verifyAccount;
window.useTemplate = useTemplate;
window.createTemplate = createTemplate;
window.resetTemplateEditor = resetTemplateEditor;
window.editTemplate = editTemplate;
window.showTemplateHistory = showTemplateHistory;
window.hideTemplateHistory = hideTemplateHistory;
window.searchTemplateReferences = searchTemplateReferences;
window.generateTemplate = generateTemplate;
window.rerunReview = rerunReview;
window.useArticlePrompt = useArticlePrompt;
window.sendArticleChat = sendArticleChat;
window.toggleArticleImmersive = toggleArticleImmersive;
window.syncDraftStatus = syncDraftStatus;
window.editArticle = editArticle;
window.saveArticleEdit = saveArticleEdit;
window.decideArticle = decideArticle;
window.confirmOriginalDeclaration = confirmOriginalDeclaration;
window.publishArticle = publishArticle;
window.scheduleArticle = scheduleArticle;
window.rescheduleArticle = rescheduleArticle;
window.cancelSchedule = cancelSchedule;
window.showProcessStage = showProcessStage;
window.showWorkflow = showWorkflow;

/* ====================================================================
 * Codex 式布局：左侧对话树（项目=公众号，对话=文章）
 * ==================================================================== */
const TIMEZONES = ['Asia/Shanghai','Asia/Tokyo','Asia/Singapore','Europe/London','Europe/Berlin','America/New_York','America/Los_Angeles','UTC'];
function currentTimezone() { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai'; } catch { return 'Asia/Shanghai'; } }
function quickSchedule(mode) {
  const input = $('schedule-at');
  if (!input) return;
  const now = new Date();
  const target = new Date(now);
  if (mode === 'tonight') { target.setHours(20, 0, 0, 0); if (target <= now) target.setDate(target.getDate() + 1); }
  else { target.setDate(target.getDate() + 1); target.setHours(8, 0, 0, 0); }
  const pad = n => String(n).padStart(2, '0');
  input.value = `${target.getFullYear()}-${pad(target.getMonth() + 1)}-${pad(target.getDate())}T${pad(target.getHours())}:${pad(target.getMinutes())}`;
}

function renderChatTree() {
  const host = $('chat-tree');
  if (!host) return;
  const accounts = state.accounts || [];
  const articles = state.articles || [];
  if (!accounts.length) { host.innerHTML = '<div class="tree-empty">还没有公众号账号<br/>先在「公众号」中添加</div>'; return; }
  const showPublished = state.showPublishedTree;
  const searching = !!state.search;
  const visible = a => (showPublished || a.status !== 'PUBLISHED') && (!searching || matchSearch(a.title));
  const publishedCount = articles.filter(a => a.status === 'PUBLISHED').length;
  const groups = accounts.map(account => ({
    account,
    articles: articles.filter(a => a.account_name === account.name),
  }));
  const renderedGroups = groups.filter(group => group.articles.some(visible) || !searching);
  host.innerHTML = renderedGroups.map(group => {
    const rows = group.articles.filter(visible);
    return `
    <div class="tree-account">
      <div class="tree-account-head"><span>◈ ${escapeHtml(group.account.name)}</span><span class="status">${rows.length}</span></div>
      ${rows.map(a => `
        <div class="tree-article-row${a.id === state.currentArticleId ? ' active' : ''}" data-article-id="${a.id}">
          <button class="tree-article${a.id === state.currentArticleId ? ' active' : ''}" type="button" onclick="openArticleFromTree('${a.id}')" title="${escapeHtml(a.title)}">
            <span class="tree-article-title">${escapeHtml(a.title)}</span>
            <span class="status${a.status === 'PUBLISHED' ? '' : (a.status === 'REJECTED' || a.status === 'FAILED') ? ' bad' : ' warn'}">${articleStatusText(a.status)}</span>
          </button>
          <button class="tree-del" type="button" onclick="confirmDeleteArticle('${a.id}')" title="删除对话" aria-label="删除对话">
            <svg viewBox="0 0 16 16" width="12" height="12"><path d="M5 2h6M3 4h10M6.5 7v4M9.5 7v4M4.5 4l.8 9.2h5.4L11.5 4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>
          </button>
        </div>`).join('') || '<div class="tree-empty" style="padding:4px 10px 8px">暂无对话</div>'}
    </div>`;
}).join('') + (searching && !articles.some(a => matchSearch(a.title))
    ? `<div class="tree-empty">没有匹配「${escapeHtml(state.search)}」的对话</div>` : '')
    + (publishedCount ? `
    <button class="tree-published-toggle" type="button" onclick="togglePublishedTree()">${showPublished ? '▾' : '▸'} 已发布对话 ${publishedCount}${showPublished ? '（点击隐藏）' : ''}</button>` : '');
}

function togglePublishedTree() {
  state.showPublishedTree = !state.showPublishedTree;
  try { localStorage.setItem('wanren_tree_show_published', state.showPublishedTree ? '1' : ''); } catch { /* 忽略 */ }
  renderChatTree();
}

async function confirmDeleteArticle(articleId) {
  const row = document.querySelector(`.tree-article-row[data-article-id="${articleId}"]`);
  if (!row) return;
  if (row.dataset.confirming === '1') return;
  row.dataset.confirming = '1';
  const del = row.querySelector('.tree-del');
  const original = del.innerHTML;
  del.innerHTML = '<span class="tree-del-confirm">确认?</span>';
  del.onclick = async () => {
    try {
      await api(`/api/articles/${articleId}`, { method: 'DELETE' });
      toast('已删除对话');
      if (state.currentArticleId === articleId) {
        state.currentArticleId = null;
        try { localStorage.removeItem('wanren_last_article'); } catch { /* 忽略 */ }
        $('article-panel').innerHTML = '<div class="empty">先生成文章，再进入审核</div>';
      }
      await refreshArticles();
      await refreshSessions();
      await refreshAll();
    } catch (error) {
      toast(`删除失败：${error.message}`, true);
      delete row.dataset.confirming;
      del.innerHTML = original;
      del.onclick = () => confirmDeleteArticle(articleId);
    }
  };
  setTimeout(() => {
    if (row.dataset.confirming === '1') {
      delete row.dataset.confirming;
      del.innerHTML = original;
      del.onclick = () => confirmDeleteArticle(articleId);
    }
  }, 3500);
}

/* ===== 微信草稿导入 ===== */
async function openImportDrafts() {
  const modal = $('import-draft-modal');
  if (!modal) return;
  modal.style.display = 'grid';
  const list = $('import-draft-list');
  list.innerHTML = '<div class="empty">正在拉取微信草稿箱…</div>';
  const accountSel = $('import-draft-account');
  accountSel.innerHTML = (state.accounts || []).map(a => `<option value="${a.id}">${escapeHtml(a.name)}${a.token_status === 'MOCK' ? '（未接入微信）' : ''}</option>`).join('');
  const preferred = (state.accounts || []).find(a => a.token_status === 'READY') || (state.accounts || []).find(a => a.token_status !== 'MOCK');
  if (preferred) accountSel.value = preferred.id;
  try {
    const data = await api(`/api/wechat/drafts?account_id=${accountSel.value}`);
    state.importDrafts = data.items || [];
    renderImportDrafts();
  } catch (error) {
    list.innerHTML = `<div class="empty">拉取失败：${escapeHtml(error.message)}</div>`;
  }
}

function renderImportDrafts() {
  const list = $('import-draft-list');
  const items = state.importDrafts || [];
  list.innerHTML = items.length ? items.map(d => `
    <div class="row import-draft-row">
      <div><div class="row-title">${escapeHtml(d.title)}</div>
      <div class="muted">更新于 ${new Date(d.update_time * 1000).toLocaleString()}</div></div>
      <div class="actions">${d.imported
        ? '<span class="status">已导入</span>'
        : `<button class="btn small primary" onclick="importWechatDraft('${d.media_id}')">⇩ 导入为对话</button>`}</div>
    </div>`).join('') : '<div class="empty">微信草稿箱为空（或当前为 mock 模式）</div>';
}

async function importWechatDraft(mediaId) {
  const accountSel = $('import-draft-account');
  try {
    const result = await api('/api/wechat/drafts/import', { method: 'POST', body: { account_id: accountSel.value, media_id: mediaId } });
    toast(`已导入：《${result.title}》`);
    $('import-draft-modal').style.display = 'none';
    await refreshArticles();
    await refreshSessions();
    await openArticle(result.article_id);
  } catch (error) {
    toast(`导入失败：${error.message}`, true);
  }
}

function closeImportDrafts() { $('import-draft-modal').style.display = 'none'; }

function articleStatusText(status) {
  const map = {
    DRAFT: '草稿', AI_REVIEWED: '待审', NEEDS_REVISION: '修订', HUMAN_APPROVED: '已通过',
    REJECTED: '驳回', WECHAT_DRAFT: '草稿箱', PUBLISHING: '发布中', PUBLISHED: '已发布', FAILED: '失败',
  };
  return map[status] || status;
}

async function openArticleFromTree(articleId) {
  clearPendingFlows(); // 先清占位再切视图，避免「架构思考中」骨架屏闪现
  switchView('workspace');
  try {
    await openArticle(articleId);
    enterArticleImmersiveEditor();
  } catch (error) {
    // 打开失败时给出可见反馈，避免界面静默停留在工作台/旧文章
    toast(`打开对话失败：${error.message}`, true);
  }
  renderChatTree();
}

function newArticleChat() {
  // Codex 式：新对话以居中对话框开始（第一轮后自动进入 中间编辑+右侧对话 形态）
  openNewChatModal();
}

function openNewChatModal() {
  const modal = $('new-chat-modal');
  if (!modal) return;
  state.modalAttachments = [];
  modal.style.display = 'grid';
  const account = $('new-chat-account');
  if (account && state.accounts.length) {
    const preferred = state.accounts.find(a => a.token_status === 'READY') || state.accounts.find(a => a.token_status === 'MOCK') || state.accounts[0];
    if (preferred) account.value = preferred.id;
  }
  state.newChatStyle = state.newChatStyle || ''; // '' = AI 推荐
  renderNewChatStyleRow();
  renderModalAttachments();
  $('new-chat-input')?.focus();
}

function closeNewChatModal() {
  const modal = $('new-chat-modal');
  if (modal) modal.style.display = 'none';
}

function renderModalAttachments() {
  const host = $('new-chat-attachments');
  if (!host) return;
  const items = state.modalAttachments || [];
  host.hidden = items.length === 0;
  host.innerHTML = items.map((item, index) => `
    <span class="attach-chip">
      ${item.type === 'IMAGE' ? `<img src="${item.previewUrl}" alt="${escapeHtml(item.name)}" />` : `<span class="asset-file-badge small">${escapeHtml((item.name.split('.').pop() || 'FILE').toUpperCase())}</span>`}
      <span class="attach-chip-name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
      <button type="button" class="attach-chip-remove" onclick="removeModalAttachment(${index})" aria-label="移除">×</button>
    </span>`).join('');
}

function removeModalAttachment(index) {
  const item = (state.modalAttachments || [])[index];
  if (item?.previewUrl?.startsWith('blob:')) URL.revokeObjectURL(item.previewUrl);
  state.modalAttachments.splice(index, 1);
  renderModalAttachments();
}

async function submitNewChat() {
  const input = $('new-chat-input');
  const instruction = (input?.value || '').trim();
  const files = state.modalAttachments || [];
  if (instruction.length < 4 && !files.length) { toast('描述一下想写的文章（至少 4 个字），或拖入素材', true); return; }
  const button = $('new-chat-send');
  button.disabled = true;
  button.textContent = '正在生成初稿…';
  try {
    const accountId = $('new-chat-account')?.value || null;
    const goal = instruction || '根据上传素材写一篇公众号文章';
    const created = await api('/api/sessions', {
      method: 'POST',
      body: { title: (instruction || '素材文章').slice(0, 24), goal, official_account_id: accountId },
    });
    const sessionId = created.session.id;
    state.currentSession = sessionId;
    // 素材先入库（会话级；单文章会话会被自动归属到生成的文章）
    for (const item of files) {
      const form = new FormData();
      form.append('file', item.file);
      await api(`/api/sessions/${sessionId}/assets`, { method: 'POST', body: form });
      if (item.previewUrl?.startsWith('blob:')) URL.revokeObjectURL(item.previewUrl);
    }
    // 立即进入编辑形态：先呈现「架构思考中」，AI 提案后由用户确认（Codex 式首问）
    state.currentArticleId = null; // 避免旧文章闪现
    state.pendingProposal = { sessionId, instruction: goal, startedAt: Date.now(), preferredStyle: state.newChatStyle || '' };
    try { localStorage.setItem('wanren_pending_proposal', sessionId); } catch { /* 忽略 */ }
    closeNewChatModal();
    switchView('workspace');
    renderArticle(); // 渲染提案等待态（含右侧对话面板）
    void pollPendingProposal();
    startProposalRequest(state.pendingProposal, goal);
  } catch (error) {
    toast(`创建失败：${error.message}`, true);
  } finally {
    button.disabled = false;
    button.textContent = '开始对话 ➤';
  }
}

async function pollPendingGeneration() {
  const pending = state.pendingGeneration;
  if (!pending) return;
  renderArticle(); // 先渲染占位（生成中骨架屏 + 右侧进度对话）
  for (let i = 0; i < 80 && state.pendingGeneration === pending; i += 1) {
    await new Promise(resolve => setTimeout(resolve, 3000));
    if (state.pendingGeneration !== pending) return; // 已被其他流程接管
    try {
      const sess = await api(`/api/sessions/${pending.sessionId}`);
      if (sess.articles && sess.articles.length) {
        state.pendingGeneration = null;
        try { localStorage.removeItem('wanren_pending_session'); } catch { /* 忽略 */ }
        state.currentSession = pending.sessionId;
        state.currentArticleId = sess.articles[0].id;
        await loadSession();
        // 与点侧栏一致：生成完成后直接进入沉浸编辑（按已绑样式呈现，可直接动手改）
        enterArticleImmersiveEditor();
        await refreshArticles();
        await refreshSessions();
        renderChatTree(); // 生成完成：新对话立即出现在左侧，无需刷新
        toast('初稿已生成，直接在样式上继续编辑或对话改稿');
        return;
      }
    } catch { /* 轮询失败继续重试 */ }
  }
  if (state.pendingGeneration === pending) {
    state.pendingGeneration = null;
    try { localStorage.removeItem('wanren_pending_session'); } catch { /* 忽略 */ }
    toast('生成超时，请稍后在左侧对话树中查看', true);
    renderArticle();
  }
}

window.revertArticleVersion = revertArticleVersion;
window.newArticleChat = newArticleChat;
window.openNewChatModal = openNewChatModal;
window.closeNewChatModal = closeNewChatModal;
window.submitNewChat = submitNewChat;

// ============ 新对话弹窗的样式选择（第一次使用即可选模版，👁 可看样例） ============

function renderNewChatStyleRow() {
  const host = $('new-chat-style-row');
  if (!host) return;
  const styles = state.typesettingStyles || [];
  if (!styles.length) { host.innerHTML = '<span class="muted" style="font-size:11.5px">样式目录加载中…</span>'; return; }
  const chosen = state.newChatStyle ?? '';
  const chips = [`<button type="button" class="style-chip${chosen === '' ? ' active' : ''}" onclick="selectNewChatStyle('')">✦ AI 推荐</button>`]
    .concat(styles.map(style => `
      <button type="button" class="style-chip${chosen === style.id ? ' active' : ''}" onclick="selectNewChatStyle('${escapeHtml(style.id)}')" title="${escapeHtml(style.vibe || '')}">
        <span class="dot" style="background:${escapeHtml(style.accent)};"></span>${escapeHtml(style.name)}
        <span class="eye" title="看排版样例" onclick="event.stopPropagation(); openStylePreview('${escapeHtml(style.id)}')">👁</span>
      </button>`))
    .join('');
  host.innerHTML = chips + '<span class="strip-hint">不选则由 AI 按内容气质推荐</span>';
}

function selectNewChatStyle(styleId) {
  state.newChatStyle = styleId || '';
  renderNewChatStyleRow();
}

// ============ 架构提案流（第一次使用：AI 先提案，用户确认 / 修改 / 取消；可中途终止） ============

function startProposalRequest(pending, instruction) {
  state.pendingProposalController = new AbortController();
  void api(`/api/sessions/${pending.sessionId}/propose`, {
    method: 'POST',
    body: { instruction },
    signal: state.pendingProposalController.signal,
  }).catch(err => {
    if (err && err.name === 'AbortError') return; // 用户主动终止：界面已切换，静默
    toast(`架构提案失败：${err.message}`, true);
    pending.error = `提案失败：${err.message}；点「重新提案」重试`;
    if (state.pendingProposal === pending) renderPendingProposal();
  });
}

function abortProposalThinking() {
  const pending = state.pendingProposal;
  if (!pending) return;
  try { state.pendingProposalController?.abort(); } catch { /* 忽略 */ }
  state.pendingProposalController = null;
  pending.aborted = true; // 停止轮询与计时，切换到「已终止」态（保留会话，可重试/取消）
  renderPendingProposal();
}

function retryProposal() {
  const pending = state.pendingProposal;
  if (!pending) return;
  pending.aborted = false;
  pending.error = null;
  pending.proposal = null;
  renderPendingProposal();
  startProposalRequest(pending, pending.instruction || '请给出写作架构');
  void pollPendingProposal();
}

function abortPendingGeneration() {
  const pending = state.pendingGeneration;
  if (!pending) return;
  state.pendingGeneration = null;
  if (state.pendingTimer) { clearInterval(state.pendingTimer); state.pendingTimer = null; }
  try { localStorage.removeItem('wanren_pending_session'); } catch { /* 忽略 */ }
  toast('已停止等待生成；文章可能仍在后台完成，稍后可在左侧对话树查看');
  renderArticle();
}

async function loadTypesettingStyles() {
  try {
    const payload = await api('/api/typesetting-styles');
    state.typesettingStyles = payload.styles || [];
  } catch { state.typesettingStyles = state.typesettingStyles || []; }
  renderNewChatStyleRow();
}

function styleCatalogOptions(selectedId) {
  const styles = state.typesettingStyles || [];
  return styles.map(style => `<option value="${escapeHtml(style.id)}"${style.id === selectedId ? ' selected' : ''}>${escapeHtml(style.name)} · ${escapeHtml(style.vibe)}</option>`).join('');
}

function proposalCardHtml(proposal) {
  const typesetting = proposal.typesetting || {};
  const preferredStyle = state.pendingProposal?.preferredStyle || '';
  const recommended = preferredStyle || typesetting.style_id || 'mono-minimal';
  const alternates = (typesetting.alternates || []).map(alt => {
    const meta = (state.typesettingStyles || []).find(s => s.id === alt.style_id);
    return meta ? `${meta.name}：${escapeHtml(alt.reason || '')}` : '';
  }).filter(Boolean).join('；');
  const sections = (proposal.sections || []).map((section, index) => `
    <div class="proposal-section">
      <span class="num">${index + 1}</span>
      <div><div class="heading">${escapeHtml(section.heading)}</div>${section.plan ? `<div class="plan">${escapeHtml(section.plan)}</div>` : ''}</div>
    </div>`).join('');
  return `<div class="proposal-card">
    <div class="proposal-head">
      <strong>拟题：《${escapeHtml(proposal.title || '')}》</strong>
      <div class="muted">${escapeHtml(proposal.summary || '')}</div>
    </div>
    <div class="proposal-sections">${sections}</div>
    <div class="proposal-style">
      <span style="font-size:12px;font-weight:650">排版样式</span>
      <select id="proposal-style-select" onchange="previewProposalStyle(this.value)">${styleCatalogOptions(recommended)}</select>
      <div class="style-reason">推荐理由：${escapeHtml(typesetting.reason || '—')}</div>
      ${alternates ? `<div class="alt">备选：${alternates}</div>` : ''}
    </div>
    ${proposal.notes ? `<div class="proposal-note">${escapeHtml(proposal.notes)}</div>` : ''}
    <div class="proposal-actions">
      <button class="btn primary" type="button" onclick="confirmProposalGenerate()">✓ 确认并开始写作</button>
      <button class="btn danger" type="button" onclick="cancelProposalSession()">✗ 取消对话</button>
    </div>
  </div>`;
}

function renderPendingProposal() {
  const pending = state.pendingProposal;
  const host = $('article-panel');
  if (!pending || !host) return;
  const proposal = pending.proposal;
  const thinking = !proposal && !pending.error && !pending.aborted;
  const elapsed = Math.round((Date.now() - (pending.startedAt || Date.now())) / 1000);
  const retryRow = `<div style="display:flex;gap:8px;margin-top:8px">
      <button class="replay-btn" type="button" onclick="retryProposal()">↻ 重新提案</button>
      <button class="revert-btn" type="button" onclick="cancelProposalSession()">✗ 取消对话</button>
    </div>`;
  const assistantBubble = pending.aborted
    ? `<div class="chat-message assistant"><strong>万仞</strong><div>已终止提案。<span class="muted">会话和素材保留，可重新提案或取消对话。</span></div>${retryRow}</div>`
    : pending.error
      ? `<div class="chat-message failed"><strong>万仞</strong><div>${escapeHtml(pending.error)}</div>${retryRow}</div>`
      : proposal
        ? `<div class="chat-message assistant"><strong>万仞</strong><div style="padding:4px 0 8px">我先拟了一个整体架构，请确认（也可直接回复修改意见）：</div>${proposalCardHtml(proposal)}</div>`
        : `<div class="chat-message assistant"><strong>万仞</strong><div>正在阅读素材并思考文章架构…（<span class="thinking-elapsed">${elapsed}s</span>）<span class="typing-caret"></span></div></div>`;
  const headerBadge = pending.aborted
    ? '<span class="status bad">已终止</span><button type="button" onclick="retryProposal()" style="height:27px;border:1px solid var(--line);border-radius:999px;background:var(--panel);color:var(--muted);font-size:11px;cursor:pointer;padding:0 10px;">↻ 重新提案</button>'
    : thinking
      ? `<span class="status warn">思考中</span><button type="button" onclick="abortProposalBtn(this)" title="中止本次提案请求（点两次确认）" style="height:27px;border:1px solid ${_abortArmed.key === 'proposal' && Date.now() - _abortArmed.at < ABORT_ARM_WINDOW_MS ? 'var(--red,#a63a2f);background:var(--red-soft,#fdecea);color:var(--red,#a63a2f)' : 'var(--amber,#b76a1b);background:var(--amber-soft,#fff3e3);color:var(--amber,#b76a1b)'};border-radius:999px;font-size:11px;font-weight:700;cursor:pointer;padding:0 10px;">${_abortArmed.key === 'proposal' && Date.now() - _abortArmed.at < ABORT_ARM_WINDOW_MS ? '再点一次终止' : '■ 终止'}</button>`
      : '<span class="status">待确认</span>';
  host.innerHTML = `<div class="review-score"><div class="score-ring busy" aria-label="思考中"></div>
    <div><strong>架构提案</strong>
    <div class="muted">AI 先想清楚再动笔 · 你来把关${pending.aborted ? ' · 已终止' : proposal ? ' · 可确认 / 修改 / 取消' : ' · 思考中'}</div></div></div>
    <div class="article-workbench">
      <div class="article-main-column">
        <div class="article-preview"><div class="preview-zoom">
          <div class="gen-skeleton">
            <div class="gen-skeleton-title"></div>
            <div class="gen-skeleton-line w80"></div><div class="gen-skeleton-line"></div>
            <div class="gen-skeleton-line w90"></div><div class="gen-skeleton-line w60"></div>
            <div class="gen-skeleton-line"></div><div class="gen-skeleton-line w75"></div>
            <div class="muted" style="text-align:center;margin-top:18px">确认架构后开始写作</div>
          </div>
        </div></div>
      </div>
      <div class="panel-resizer" title="左右拖动调整对话面板宽度"></div>
      <section class="article-chat-panel" aria-label="架构确认">
        <header><div><strong>架构确认</strong><small>确认后进入写作；回复消息即修改提案</small></div>
          <div class="chat-header-actions">${headerBadge}</div></header>
        <div class="chat-messages" id="article-chat-messages"><div class="chat-zoom">
          <div class="chat-message user"><strong>我</strong><div>${escapeHtml(pending.instruction || '请根据素材写一篇公众号文章')}</div></div>
          ${(pending.feedback || []).map(text => `<div class="chat-message user"><strong>我</strong><div>${escapeHtml(text)}</div></div>`).join('')}
          ${assistantBubble}
        </div></div>
        <div class="chat-quick-actions"></div>
        <form class="chat-composer" onsubmit="return false">
          <textarea id="article-chat-input" rows="3" placeholder="${pending.aborted ? '已终止；点「重新提案」继续，或取消对话' : proposal ? '想调整？直接说，如：第二节改成讲实验设置' : 'AI 正在思考架构…'}"></textarea>
          <button id="article-chat-send" type="button" ${thinking ? 'disabled' : ''} onclick="sendProposalFeedback()">${thinking ? '思考中…' : '发送修改'}</button>
        </form>
      </section>
    </div>`;
  applyChatWidth();
  applyPaneZoom();
  const messages = $('article-chat-messages');
  if (messages) messages.scrollTop = messages.scrollHeight;
  host.scrollIntoView({ behavior: 'smooth', block: 'start' });
  if (!state.pendingProposalTimer) {
    state.pendingProposalTimer = setInterval(() => {
      if (!state.pendingProposal || state.pendingProposal.proposal) {
        clearInterval(state.pendingProposalTimer);
        state.pendingProposalTimer = null;
        return;
      }
      // 只更新秒数文本：整面板重渲染会不断换掉终止按钮，打断两步确认的武装状态
      const el = document.querySelector('.thinking-elapsed');
      if (el) el.textContent = `${Math.round((Date.now() - (state.pendingProposal.startedAt || Date.now())) / 1000)}s`;
    }, 5000);
  }
}

async function pollPendingProposal() {
  const pending = state.pendingProposal;
  if (!pending) return;
  for (let i = 0; 60 > i && state.pendingProposal === pending && !pending.proposal && !pending.aborted; i += 1) {
    await new Promise(resolve => setTimeout(resolve, 2500));
    if (state.pendingProposal !== pending || pending.aborted) return; // 已确认、取消或用户终止
    try {
      const detail = await api(`/api/sessions/${pending.sessionId}`);
      if (detail.articles && detail.articles.length) {
        // 意外直接生成（如旧流程续接）：转到生成完成态
        state.pendingProposal = null;
        try { localStorage.removeItem('wanren_pending_proposal'); } catch { /* 忽略 */ }
        state.currentArticleId = detail.articles[0].id;
        await loadSession();
        await renderArticle();
        return;
      }
      const proposal = detail.session && detail.session.proposal;
      if (proposal) {
        pending.proposal = proposal;
        if (!state.typesettingStyles || !state.typesettingStyles.length) await loadTypesettingStyles();
        renderPendingProposal();
        return;
      }
    } catch { /* 轮询失败继续重试 */ }
  }
  if (state.pendingProposal === pending && !pending.proposal && !pending.aborted) {
    pending.error = '架构提案超时；点「重新提案」重试，或取消对话';
    renderPendingProposal();
  }
}

async function sendProposalFeedback() {
  const pending = state.pendingProposal;
  if (!pending) return;
  const hasProposal = Boolean(pending.proposal);
  const input = $('article-chat-input');
  const text = (input?.value || '').trim();
  if (hasProposal && text.length < 2) { toast('说说想怎么改（至少 2 个字）', true); return; }
  const payload = hasProposal ? text : (pending.instruction || '请给出写作架构');
  if (hasProposal) pending.feedback = [...(pending.feedback || []), text];
  const previousProposal = pending.proposal;
  pending.proposal = null;
  pending.error = null;
  if (input) input.value = '';
  renderPendingProposal();
  state.pendingProposalController = new AbortController();
  try {
    const result = await api(`/api/sessions/${pending.sessionId}/propose`, {
      method: 'POST',
      body: { instruction: payload },
      signal: state.pendingProposalController.signal,
    });
    pending.proposal = result.proposal;
  } catch (error) {
    if (error && error.name === 'AbortError') return; // 用户终止：abortProposalThinking 已切换界面
    pending.proposal = previousProposal;
    if (!previousProposal) pending.error = `提案失败：${error.message}；点「重新提案」重试`;
    toast(`提案失败：${error.message}`, true);
  }
  if (state.pendingProposal === pending) renderPendingProposal();
}

async function confirmProposalGenerate() {
  const pending = state.pendingProposal;
  if (!pending || !pending.proposal) return;
  const styleSelect = $('proposal-style-select');
  const styleId = styleSelect?.value || pending.preferredStyle || pending.proposal.typesetting?.style_id;
  try {
    await api(`/api/sessions/${pending.sessionId}/confirm-proposal`, { method: 'POST', body: { style_id: styleId } });
  } catch (error) {
    toast(`确认失败：${error.message}`, true);
    return;
  }
  // 转入生成流程（骨架屏 + 轮询；后端按已确认大纲与样式写作）
  state.pendingProposal = null;
  try { localStorage.removeItem('wanren_pending_proposal'); } catch { /* 忽略 */ }
  state.pendingGeneration = { sessionId: pending.sessionId, instruction: pending.instruction, startedAt: Date.now() };
  try { localStorage.setItem('wanren_pending_session', pending.sessionId); } catch { /* 忽略 */ }
  renderArticle();
  void pollPendingGeneration();
  void api(`/api/sessions/${pending.sessionId}/generate`, { method: 'POST', body: { instruction: pending.instruction || '按已确认大纲写作' } }).catch(err => {
    toast(`生成请求失败：${err.message}`, true);
    state.pendingGeneration = null;
    try { localStorage.removeItem('wanren_pending_session'); } catch { /* 忽略 */ }
    renderArticle();
  });
}

async function cancelProposalSession() {
  const pending = state.pendingProposal;
  if (!pending) return;
  if (!window.confirm('取消这个对话？素材与提案将一并删除。')) return;
  try {
    await api(`/api/sessions/${pending.sessionId}`, { method: 'DELETE' });
  } catch (error) {
    toast(`取消失败：${error.message}`, true);
    return;
  }
  state.pendingProposal = null;
  try { localStorage.removeItem('wanren_pending_proposal'); } catch { /* 忽略 */ }
  await refreshSessions();
  renderChatTree();
  renderArticle();
  toast('已取消对话');
}

function previewProposalStyle(styleId) {
  // 提案卡里切换样式时给个轻提示（正式生效在确认后）
  const meta = (state.typesettingStyles || []).find(s => s.id === styleId);
  if (meta) toast(`确认后将使用「${meta.name}」排版（${meta.suits}）`);
}

async function changeArticleStyle(styleId) {
  if (!state.currentArticleId || !styleId) return;
  try {
    const detail = await api(`/api/articles/${state.currentArticleId}`);
    let content = JSON.parse(detail.article.content_json);
    if (state.articleEditorDirty && typeof collectArticleEditorContent === 'function') {
      const live = collectArticleEditorContent();
      if (live.title && live.sections.length) content = live; // 未保存的编辑优先，避免样式切换覆盖
    }
    await api(`/api/articles/${state.currentArticleId}`, {
      method: 'PUT',
      body: {
        title: content.title,
        summary: content.summary || '',
        sections: (content.sections || []).map(s => ({
          id: s.id, heading: s.heading, paragraphs: s.paragraphs || [],
          asset_refs: s.asset_refs || [], style: s.style || {},
        })),
        change_summary: '切换排版样式',
        typesetting_id: styleId,
      },
    });
    toast('已切换排版样式，同步草稿后将按新样式渲染');
    await renderArticle();
  } catch (error) {
    toast(`切换失败：${error.message}`, true);
  }
}

window.confirmProposalGenerate = confirmProposalGenerate;
window.cancelProposalSession = cancelProposalSession;
window.sendProposalFeedback = sendProposalFeedback;
window.previewProposalStyle = previewProposalStyle;
window.changeArticleStyle = changeArticleStyle;
window.abortProposalThinking = abortProposalThinking;
window.retryProposal = retryProposal;
window.abortPendingGeneration = abortPendingGeneration;
window.abortProposalBtn = abortProposalBtn;
window.abortGenerationBtn = abortGenerationBtn;
window.abortArticleChatBtn = abortArticleChatBtn;

// ============ 样式模版画廊（卡片 → 点开整篇排版样例；支持导入） ============

function styleMockHtml(style) {
  // 无文字的迷你版式示意：横幅/标题条/正文线/金句块，全用该样式的配色
  const accent = style.accent || '#175641';
  const bg = style.page_bg || '#fefefe';
  const soft = '#F0F1F0';
  const line = '#E4E7E5';
  return `<div class="mock-banner" style="background:${accent};"></div>
    <div class="mock-heading"><span class="mock-num" style="background:${accent};">1</span><span class="mock-title" style="background:${accent};opacity:.75;"></span></div>
    <div class="mock-line" style="background:${line};width:92%;"></div>
    <div class="mock-line" style="background:${line};width:96%;"></div>
    <div class="mock-line" style="background:${line};width:88%;"></div>
    <div class="mock-quote" style="background:${accent};opacity:.16;"></div>
    <div class="mock-line" style="background:${line};width:94%;"></div>`;
}

async function renderStyleGallery() {
  const host = $('style-gallery');
  if (!host) return;
  const styles = state.typesettingStyles || [];
  if (!styles.length) { host.innerHTML = '<div class="empty">样式加载中…</div>'; return; }
  host.innerHTML = styles.map(style => `
    <div class="style-card" onclick="openStylePreview('${escapeHtml(style.id)}')" title="点开查看整篇排版样例">
      <div class="style-mock" style="background:${escapeHtml(style.page_bg || '#fefefe')};">${styleMockHtml(style)}</div>
      <div class="style-card-body">
        <div style="min-width:0"><strong>${escapeHtml(style.name)}</strong>${style.builtin ? '' : '<span class="style-badge">导入</span>'}
          <div class="muted">${escapeHtml(style.vibe || '')}</div></div>
        <div class="style-card-actions" onclick="event.stopPropagation()">
          <button type="button" title="导出 JSON（可改后导入为新样式）" onclick="exportStyle('${escapeHtml(style.id)}')">⇪ 导出</button>
          ${style.builtin ? '' : `<button type="button" title="删除该导入样式" onclick="deleteStyle('${escapeHtml(style.id)}')">×</button>`}
        </div>
      </div>
    </div>`).join('');
}

async function openStylePreview(styleId, sampleId) {
  try {
    const query = sampleId ? `?sample=${encodeURIComponent(sampleId)}` : '';
    const payload = await api(`/api/typesetting-styles/${encodeURIComponent(styleId)}/preview${query}`);
    $('style-preview-title').textContent = `排版样例 · ${payload.name || styleId}`;
    const bar = $('style-sample-bar');
    if (bar && Array.isArray(payload.samples)) {
      bar.innerHTML = payload.samples.map(s =>
        `<button type="button" class="${s.id === payload.sample.id ? 'active' : ''}" onclick="openStylePreview('${escapeHtml(styleId)}', '${escapeHtml(s.id)}')">${escapeHtml(s.name)}</button>`
      ).join('') + '<span class="hint">同一气质 × 不同内容形态</span>';
    }
    const frame = $('style-preview-frame');
    frame.srcdoc = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><body style="margin:0;background:#fff;">${payload.html}</body>`;
    hookPreviewFrameZoom(frame);
    $('style-preview-modal').style.display = 'grid';
  } catch (error) {
    toast(`样例加载失败：${error.message}`, true);
  }
}

async function copyStylePreviewToWechat() {
  const frame = $('style-preview-frame');
  const doc = frame?.contentDocument;
  if (!doc || !doc.body || !doc.body.innerHTML.trim()) {
    toast('样例尚未加载完成', true);
    return;
  }
  const html = `<section style="background-color:#fff;color:#333;padding:0;">${doc.body.innerHTML}</section>`;
  const text = doc.body.innerText || '';
  try {
    if (navigator.clipboard && window.ClipboardItem) {
      // 双 MIME：公众号编辑器取 text/html 分支，内联样式全保留
      const item = new ClipboardItem({
        'text/html': new Blob([html], { type: 'text/html' }),
        'text/plain': new Blob([text], { type: 'text/plain;charset=utf-8' }),
      });
      await navigator.clipboard.write([item]);
      toast('已复制：到公众号编辑器直接 ⌘/Ctrl+V 粘贴');
      return;
    }
  } catch { /* 非 HTTPS/无权限：走 execCommand 降级 */ }
  try {
    const range = doc.createRange();
    range.selectNodeContents(doc.body);
    const selection = doc.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    const ok = doc.execCommand('copy');
    selection.removeAllRanges();
    toast(ok ? '已复制：到公众号编辑器直接 ⌘/Ctrl+V 粘贴' : '复制失败，请在样例内手动全选复制', !ok);
  } catch {
    toast('复制失败，请在样例内手动全选复制', true);
  }
}

function closeStylePreview() {
  const modal = $('style-preview-modal');
  if (modal) modal.style.display = 'none';
  const frame = $('style-preview-frame');
  if (frame) frame.srcdoc = '';
}

async function exportStyle(styleId) {
  try {
    const payload = await api(`/api/typesetting-styles/${encodeURIComponent(styleId)}/export`);
    const text = JSON.stringify(payload, null, 2);
    await navigator.clipboard.writeText(text).then(
      () => toast(`已复制「${payload.name}」样式 JSON，可修改后导入为新样式`),
      () => {
        window.prompt('复制下面的样式 JSON：', text);
      }
    );
  } catch (error) {
    toast(`导出失败：${error.message}`, true);
  }
}

function openStyleImport() {
  const modal = $('style-import-modal');
  if (modal) modal.style.display = 'grid';
}

function closeStyleImport() {
  const modal = $('style-import-modal');
  if (modal) modal.style.display = 'none';
}

async function submitStyleImport() {
  const name = ($('style-import-name')?.value || '').trim();
  const tokensText = ($('style-import-textarea')?.value || '').trim();
  const message = $('style-import-message');
  if (message) message.textContent = '';
  try {
    const tokens = tokensText ? JSON.parse(tokensText) : {};
    const payload = await api('/api/typesetting-styles', {
      method: 'POST',
      body: {
        name,
        vibe: ($('style-import-vibe')?.value || '').trim(),
        suits: ($('style-import-suits')?.value || '').trim(),
        tokens,
      },
    });
    toast(`已导入样式「${payload.style.name}」，提案与编辑器立即可选`);
    closeStyleImport();
    $('style-import-name').value = '';
    $('style-import-textarea').value = '';
    await loadTypesettingStyles();
    renderStyleGallery();
  } catch (error) {
    if (message) { message.textContent = error.message; message.className = 'status bad'; }
    toast(`导入失败：${error.message}`, true);
  }
}

async function deleteStyle(styleId) {
  if (!window.confirm('删除这个导入的样式？（已选它的文章会回退品牌默认）')) return;
  try {
    await api(`/api/typesetting-styles/${encodeURIComponent(styleId)}`, { method: 'DELETE' });
    toast('已删除');
    await loadTypesettingStyles();
    renderStyleGallery();
  } catch (error) {
    toast(`删除失败：${error.message}`, true);
  }
}

async function openArticleRenderPreview() {
  if (!state.currentArticleId) return;
  try {
    const payload = await api(`/api/articles/${state.currentArticleId}/render-preview`);
    $('style-preview-title').textContent = `文章预览 · ${payload.name || '默认样式'}`;
    const bar = $('style-sample-bar');
    if (bar) bar.innerHTML = '<span class="hint">按当前排版样式渲染的公众号成品效果 · 「⧉ 复制到公众号」可直接粘贴使用</span>';
    const frame = $('style-preview-frame');
    frame.srcdoc = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><body style="margin:0;background:#fff;">${payload.html}</body>`;
    hookPreviewFrameZoom(frame);
    $('style-preview-modal').style.display = 'grid';
  } catch (error) {
    toast(`预览加载失败：${error.message}`, true);
  }
}

window.openStylePreview = openStylePreview;
window.closeStylePreview = closeStylePreview;
window.copyStylePreviewToWechat = copyStylePreviewToWechat;
window.selectNewChatStyle = selectNewChatStyle;
window.openArticleRenderPreview = openArticleRenderPreview;
window.exportStyle = exportStyle;
window.openStyleImport = openStyleImport;
window.closeStyleImport = closeStyleImport;
window.submitStyleImport = submitStyleImport;
window.deleteStyle = deleteStyle;
window.removeModalAttachment = removeModalAttachment;

/* ====================================================================
 * 设置页：视觉分析（默认多模态，VidLens 可选）与能力提示
 * ==================================================================== */
async function loadVisionSettings() {
  try {
    const settings = await api('/api/settings/vision');
    state.visionSettings = settings;
    renderSettingsView(settings);
    updateCapabilityNote(settings);
  } catch (error) {
    const hint = $('vision-hint');
    if (hint) hint.textContent = '视觉设置需要管理员权限查看：' + error.message;
  }
}

async function testModelConnection(target) {
  const suffix = target === 'vision' ? 'vision' : (target === 'vidlens' ? 'vidlens' : 'text');
  const resultEl = $(suffix + '-test-result');
  const button = $('test-' + suffix + '-connection');
  if (button) { button.disabled = true; button.textContent = '检测中…'; }
  if (resultEl) { resultEl.textContent = '正在检测…'; resultEl.className = 'status'; }
  try {
    const result = await api('/api/settings/test-connection', { method: 'POST', body: { target } });
    if (resultEl) {
      resultEl.textContent = result.message;
      resultEl.className = 'status' + (result.ok ? '' : ' bad');
    }
  } catch (error) {
    if (resultEl) { resultEl.textContent = error.message; resultEl.className = 'status bad'; }
  } finally {
    if (button) { button.disabled = false; button.textContent = '◎ 检测连接'; }
  }
}

function renderSettingsView(settings) {
  const staleStatus = $('text-test-result');
  if (staleStatus && !staleStatus.textContent) staleStatus.style.display = 'none';
  const activeMode = settings.provider === 'vidlens' ? 'vidlens' : 'multimodal';
  document.querySelectorAll('#vision-provider-cards .provider-card').forEach(card => {
    card.classList.toggle('selected', card.dataset.mode === activeMode);
  });
  const visionPanel = $('vision-config-panel');
  const textPanel = $('text-config-panel');
  const vidlensPanel = $('vidlens-status-panel');
  if (visionPanel) visionPanel.hidden = activeMode !== 'multimodal';
  if (textPanel) textPanel.hidden = activeMode !== 'vidlens';
  if (vidlensPanel) vidlensPanel.hidden = activeMode !== 'vidlens';
  const modelStatus = $('vision-model-status');
  if (modelStatus) { modelStatus.textContent = settings.configured ? '已配置' : '未配置'; modelStatus.className = 'status' + (settings.configured ? '' : ' warn'); }
  const hint = $('vision-hint');
  if (hint) {
    if (activeMode === 'multimodal') {
      const usingMainModel = !settings.api_base && !settings.model;
      if (usingMainModel && settings.configured) {
        hint.className = 'settings-hint ok';
        hint.textContent = '视觉分析：图片直接送入主模型上下文（多模态主模型自动生效，无需额外配置）。';
      } else if (usingMainModel) {
        hint.className = 'settings-hint';
        hint.innerHTML = '<strong>提醒：主模型是本地适配器（或未配置远端），无法看图。</strong><br/>若主模型是纯文本，请在下方填写单独的多模态模型配置；或改选 VidLens 本地脚本。';
      } else if (settings.configured) {
        hint.className = 'settings-hint ok';
        hint.textContent = '视觉分析：使用单独配置的多模态视觉模型（主模型可保持纯文本）。';
      } else {
        hint.className = 'settings-hint';
        hint.innerHTML = '<strong>提醒：下方多模态模型配置不完整。</strong>请填写 API 地址 / API Key / 模型名并保存，或清空以跟随主模型。';
      }
    } else {
      hint.className = 'settings-hint ' + (settings.vidlens_available ? 'ok' : '');
      hint.textContent = settings.vidlens_available
        ? '视觉分析：使用 VidLens 本地脚本（离线可用）。'
        : '视觉分析：已选择 VidLens，但本地未找到可用的 VidLens 脚本（WANREN_VIDLENS），请检查路径。';
    }
  }
  if (modelStatus) { modelStatus.textContent = settings.configured ? '已配置' : '未配置'; modelStatus.className = 'status' + (settings.configured ? '' : ' warn'); }
  const base = $('vision-api-base'); if (base && !base.dataset.touched) { base.value = settings.api_base || ''; }
  const model = $('vision-model'); if (model && !model.dataset.touched) { model.value = settings.model || ''; }
  const status = $('vision-config-status');
  if (status) { status.textContent = settings.configured ? '已配置' : '未配置'; status.className = 'status' + (settings.configured ? '' : ' warn'); }
  const textConfig = settings.text_config || {};
  const providerSelect = $('text-provider-input');
  if (providerSelect && !providerSelect.dataset.touched) providerSelect.value = textConfig.provider || 'local';
  const textModelInput = $('text-model-input');
  if (textModelInput && !textModelInput.dataset.touched) textModelInput.value = textConfig.model || '';
  const textBaseInput = $('text-api-base');
  if (textBaseInput && !textBaseInput.dataset.touched) textBaseInput.value = textConfig.api_base || '';
  const textStatus = $('text-config-status');
  if (textStatus) { textStatus.textContent = textConfig.provider === 'openai_compatible' ? '已接入远端' : '本地适配器'; textStatus.className = 'status' + (textConfig.provider === 'openai_compatible' ? '' : ' warn'); }
  const vc = settings.vidlens_config || {};
  const vlUrl = $('vidlens-api-url');
  if (vlUrl && !vlUrl.dataset.touched) vlUrl.value = vc.api_url || '';
  const vlModel = $('vidlens-model');
  if (vlModel && !vlModel.dataset.touched) vlModel.value = vc.model || '';
  const vlBadge2 = $('vidlens-model-status');
  if (vlBadge2) { vlBadge2.textContent = vc.configured ? '已配置' + (vc.source === 'db' ? '' : '（环境变量）') : '未配置'; vlBadge2.className = 'status' + (vc.configured ? '' : ' warn'); }
  const vidlensBadge = $('vidlens-status-badge');
  if (vidlensBadge) { vidlensBadge.textContent = settings.vidlens_available ? '可用' : '不可用'; vidlensBadge.className = 'status' + (settings.vidlens_available ? '' : ' bad'); }
  const vidlensPath = $('vidlens-path');
  if (vidlensPath) vidlensPath.textContent = settings.vidlens_available ? 'VidLens 脚本路径已找到，离线分析可用。' : '未找到 VidLens 脚本，请设置 WANREN_VIDLENS 环境变量。';
  const note = $('text-model-note');
  if (note) {
    note.className = 'settings-hint';
    note.innerHTML = settings.text_provider === 'openai_compatible'
      ? '文本模型：OpenAI-compatible —— 支持协作编辑（AI 逐块改稿，工具在浏览器执行、逐块流式上屏）。'
      : '文本模型：本地适配器 —— 对话改稿将以整篇修订模式运行；配置 OpenAI-compatible 模型后自动启用逐块协作编辑。';
  }
}

async function saveVisionSettings() {
  const button = $('save-vision-settings');
  button.disabled = true;
  try {
    const selectedCard = document.querySelector('#vision-provider-cards .provider-card.selected');
    const mode = selectedCard?.dataset.mode || 'multimodal';
    const provider = mode === 'vidlens' ? 'vidlens' : 'multimodal';
    // 多模态：提交输入值（留空=跟随主模型）；VidLens：清空模型配置
    const isMultimodal = mode === 'multimodal';
    const settings = await api('/api/settings/vision', {
      method: 'PUT',
      body: {
        provider,
        api_base: isMultimodal ? ($('vision-api-base')?.value || '') : '',
        api_key: isMultimodal ? ($('vision-api-key')?.value || '') : '',
        model: isMultimodal ? ($('vision-model')?.value || '') : '',
      },
    });
    state.visionSettings = settings;
    renderSettingsView(settings);
    updateCapabilityNote(settings);
    const message = $('vision-save-message');
    if (message) { message.textContent = '已保存'; setTimeout(() => { message.textContent = ''; }, 2000); }
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function updateCapabilityNote(vision) {
  const note = $('capability-note');
  if (!note) return;
  const visionText = vision
    ? (vision.provider === 'multimodal' ? (vision.configured ? '视觉：多模态' : '视觉：多模态未配置') : '视觉：VidLens')
    : '视觉：多模态';
  note.textContent = `${visionText} · 协作编辑`;
}

function selectVisionProvider(mode) {
  document.querySelectorAll('#vision-provider-cards .provider-card').forEach(card => {
    card.classList.toggle('selected', card.dataset.mode === mode);
  });
  const visionPanel = $('vision-config-panel');
  const textPanel = $('text-config-panel');
  const vidlensPanel = $('vidlens-status-panel');
  if (visionPanel) visionPanel.hidden = mode !== 'multimodal';
  if (textPanel) textPanel.hidden = mode !== 'vidlens';
  if (vidlensPanel) vidlensPanel.hidden = mode !== 'vidlens';
  const hint = $('vision-hint');
  if (hint) {
    if (mode === 'multimodal') {
      hint.className = 'settings-hint ok';
      hint.innerHTML = '多模态视觉大模型：<b>主模型支持图像输入时下方留空即可</b>（自动跟随主模型）；主模型是纯文本时，在下方填写单独配置。';
    } else {
      hint.className = 'settings-hint ok';
      hint.textContent = 'VidLens 本地脚本：在下方配置其内部视觉模型（可与主模型不同厂家）后保存。';
    }
  }
}

/* ====================================================================
 * 中文排版引擎：工具栏 -> 所在小节 style -> 段落内联样式 -> 发布随行
 * ==================================================================== */
function sectionInlineStyle(style) {
  if (!style || typeof style !== 'object') return '';
  const parts = [];
  if (style.indent) parts.push(`text-indent:${style.indent}em`);
  if (style.line_height) parts.push(`line-height:${style.line_height}`);
  if (style.letter_spacing) parts.push(`letter-spacing:${style.letter_spacing}px`);
  if (style.margin_top !== undefined && style.margin_top !== '') parts.push(`margin-top:${style.margin_top}px`);
  if (style.margin_bottom !== undefined && style.margin_bottom !== '') parts.push(`margin-bottom:${style.margin_bottom}px`);
  if (style.align) parts.push(`text-align:${style.align}`);
  return parts.join(';');
}

function currentSectionElement() {
  const anchor = state.articleEditorAnchor;
  const fromAnchor = anchor && anchor.closest ? anchor.closest('.article-editor-section') : null;
  return fromAnchor || document.querySelector('.article-editor-section:focus-within');
}

function applyFormat(key, value) {
  const section = currentSectionElement();
  if (!section) { toast('先把光标放在要排版的段落里'); return; }
  let style = {};
  try { style = JSON.parse(section.dataset.style || '{}'); } catch { style = {}; }
  if (value === '' || value === null) delete style[key];
  else style[key] = Number.isNaN(Number(value)) ? value : Number(value);
  section.dataset.style = JSON.stringify(style);
  section.querySelectorAll(':scope > [data-paragraph-index]').forEach(p => { p.setAttribute('style', sectionInlineStyle(style)); });
  markArticleEditorDirty();
  syncFormatControls(section);
  section.classList.add('fmt-applied');
  setTimeout(() => section.classList.remove('fmt-applied'), 900);
}

function clearFormat() {
  const section = currentSectionElement();
  if (!section) { toast('先把光标放在要清除排版的段落里'); return; }
  section.dataset.style = '{}';
  section.querySelectorAll(':scope > [data-paragraph-index]').forEach(p => p.removeAttribute('style'));
  syncFormatControls(section);
  markArticleEditorDirty();
}

function syncFormatControls(section) {
  let style = {};
  try { style = JSON.parse((section && section.dataset.style) || '{}'); } catch { style = {}; }
  const map = { 'fmt-align': 'align', 'fmt-mt': 'margin_top', 'fmt-mb': 'margin_bottom', 'fmt-indent': 'indent', 'fmt-lh': 'line_height', 'fmt-ls': 'letter_spacing' };
  Object.entries(map).forEach(([id, key]) => {
    const select = $(id);
    if (select) select.value = style[key] === undefined ? '' : String(style[key]);
  });
}

/* ====================================================================
 * 协作编辑（assist）：SSE 工具循环，工具在浏览器执行并逐块上屏
 * ==================================================================== */
function collectEditorDocument() {
  const content = collectArticleEditorContent();
  return { title: content.title, summary: content.summary, sections: content.sections };
}

async function postAssistResult(assistId, callId, body) {
  return api(`/api/articles/${state.currentArticleId}/assist/${assistId}/tools/${callId}/result`, { method: 'POST', body });
}

async function executeAssistTool(assistId, call) {
  const name = call.name;
  const args = call.arguments || {};
  try {
    if (name === 'read_article') {
      return { status: 'ok', message: '已读取当前文章', document: collectEditorDocument() };
    }
    if (name === 'insert_blocks' || name === 'replace_blocks') {
      const sectionEl = document.querySelector(`.article-editor-section[data-section-id="${CSS.escape(String(args.section_id || ''))}"]`);
      if (!sectionEl) return { status: 'error', message: `未找到小节 ${args.section_id}`, document: collectEditorDocument() };
      if (name === 'replace_blocks') {
        const blocks = [...sectionEl.querySelectorAll(':scope > [data-paragraph-index]')];
        const start = Math.max(0, Number(args.start) || 0);
        const end = Math.min(blocks.length, Number(args.end) || blocks.length);
        if (end > start) for (let i = end - 1; i >= start; i -= 1) blocks[i].remove();
      }
      const blocksNow = [...sectionEl.querySelectorAll(':scope > [data-paragraph-index]')];
      const insertAt = Math.min(Math.max(0, Number(args.index) || 0), blocksNow.length);
      const anchor = blocksNow[insertAt - 1] || sectionEl.querySelector(':scope > [data-field="heading"]');
      const newNodes = (args.paragraphs || []).map(text => {
        const node = makeArticleParagraph(String(text));
        node.classList.add('block-appear');
        return node;
      });
      for (let i = 0; i < newNodes.length; i += 1) {
        await new Promise(resolve => setTimeout(resolve, 90));
        const node = newNodes[i];
        if (anchor && anchor.parentNode) anchor.after(node); else sectionEl.appendChild(node);
        [...sectionEl.querySelectorAll(':scope > [data-paragraph-index]')].forEach((b, index) => { b.dataset.paragraphIndex = String(index); });
      }
      markArticleEditorDirty();
      const changedBlocks = (args.paragraphs || []).map(String).filter(Boolean);
      return { status: 'ok', message: `已${name === 'insert_blocks' ? '插入' : '替换'} ${newNodes.length} 个段落（${args.section_id}）`, document: collectEditorDocument(), changed_blocks: changedBlocks };
    }
    if (name === 'update_metadata') {
      if (args.title) { const el = document.querySelector('[data-article-field="title"]'); if (el) el.innerText = String(args.title); }
      if (args.summary) { const el = document.querySelector('[data-article-field="summary"]'); if (el) el.innerText = String(args.summary); }
      markArticleEditorDirty();
      return { status: 'ok', message: '已更新标题/摘要', document: collectEditorDocument() };
    }
    if (name === 'update_cover') {
      return { status: 'ok', message: '封面请通过素材库指定（当前版本暂不支持工具直改封面）', document: collectEditorDocument() };
    }
    return { status: 'error', message: `未知工具 ${name}`, document: collectEditorDocument() };
  } catch (error) {
    return { status: 'error', message: error.message, document: collectEditorDocument() };
  }
}

async function sendArticleAssist(instruction, attachments = []) {
  const articleId = state.currentArticleId;
  const headers = { 'Content-Type': 'application/json', ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}) };
  const response = await fetch(`/api/articles/${articleId}/assist`, { method: 'POST', headers, body: JSON.stringify({ instruction, attachments }) });
  if (!response.ok || !response.body) {
    const detail = await response.json().catch(() => ({ detail: '协作编辑请求失败' }));
    throw new Error(detail.detail || '协作编辑请求失败');
  }
  let assistId = null;
  let buffer = '';
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const dispatch = {
    state(data) { if (data.assist_id) assistId = data.assist_id; },
    notice(data) { state.articleMessages.push({ role: 'NOTICE', content: data.message, status: 'COMPLETED' }); renderArticleChatMessages(); },
    user() { /* 已本地占位 */ },
    tool_call(data) {
      state.articleMessages.push({ role: 'TOOL', name: data.name, content: `执行 ${data.name}…`, status: 'RUNNING', callId: data.call_id, blocks: (data.arguments?.paragraphs || []).map(String).filter(Boolean) });
      renderArticleChatMessages();
      void (async () => {
        const result = await executeAssistTool(assistId, data);
        try { await postAssistResult(assistId, data.call_id, result); } catch (error) { toast('工具结果回传失败：' + error.message, true); }
      })();
    },
    tool_result(data) {
      const message = state.articleMessages.find(m => m.callId === data.call_id);
      if (message) { message.status = data.status === 'ok' ? 'COMPLETED' : 'FAILED'; message.content = data.message || message.content; }
      renderArticleChatMessages();
    },
    delta(data) {
      const last = [...state.articleMessages].reverse().find(m => m.role === 'ASSISTANT');
      if (last) { last.content = (last.content || '') + data.content; renderArticleChatMessages(); }
    },
    async completed(data) {
      toast(`协作编辑完成：${data.tool_calls} 次工具调用${data.result && data.result.version ? `，已保存第 ${data.result.version} 版` : ''}`);
      const ephemeral = state.articleMessages.filter(m => m.role === 'NOTICE' || m.role === 'TOOL');
      await renderArticle();
      state.articleMessages = [...state.articleMessages, ...ephemeral];
      renderArticleChatMessages();
      await refreshArticles();
      await refreshDashboard();
      renderChatTree();
    },
    error(data) {
      const last = [...state.articleMessages].reverse().find(m => m.role === 'ASSISTANT');
      if (last) { last.status = 'FAILED'; last.content = data.message; }
      renderArticleChatMessages();
      toast(data.message, true);
    },
  };
  const handleEvent = (name, raw) => {
    let data = {};
    try { data = JSON.parse(raw); } catch { data = {}; }
    if (dispatch[name]) dispatch[name](data);
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index;
    while ((index = buffer.indexOf('\n\n')) >= 0) {
      const chunk = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      const lines = chunk.split('\n');
      const eventLine = lines.find(l => l.startsWith('event: '));
      const dataLine = lines.find(l => l.startsWith('data: '));
      if (eventLine && dataLine) handleEvent(eventLine.slice(7).trim(), dataLine.slice(6));
    }
  }
}

/* 排版控件随焦点联动 + 输入防覆盖 */
document.addEventListener('focusin', event => {
  const section = event.target.closest?.('.article-editor-section');
  if (section) syncFormatControls(section);
});
document.addEventListener('input', event => {
  if (event.target.id === 'vision-api-base' || event.target.id === 'vision-model') event.target.dataset.touched = '1';
});
document.addEventListener('click', event => {
  const card = event.target.closest?.('#vision-provider-cards .provider-card');
  if (card) selectVisionProvider(card.dataset.mode);
});
$('new-chat-button')?.addEventListener('click', newArticleChat);
$('import-draft-button')?.addEventListener('click', openImportDrafts);
applySideWidth();

/* 侧栏显示 / 隐藏（纯图标切换，Codex 式，状态记忆） */
const SVG_COLLAPSED = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m13 17 5-5-5-5"/><path d="m6 17 5-5-5-5"/></svg>';
const SVG_EXPANDED = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m11 17-5-5 5-5"/><path d="m18 17-5-5 5-5"/></svg>';
function applySideCollapsed() {
  const collapsed = localStorage.getItem('wanren_side_collapsed') === '1';
  document.body.classList.toggle('side-collapsed', collapsed);
  const topToggle = $('topnav-side-toggle');
  if (topToggle) topToggle.innerHTML = collapsed ? SVG_COLLAPSED : SVG_EXPANDED;
}
function toggleSideCollapsed() {
  const collapsed = document.body.classList.toggle('side-collapsed');
  try { localStorage.setItem('wanren_side_collapsed', collapsed ? '1' : '0'); } catch { /* 忽略 */ }
  applySideCollapsed();
}
$('topnav-side-toggle')?.addEventListener('click', toggleSideCollapsed);
$('side-collapse')?.addEventListener('click', toggleSideCollapsed);
applySideCollapsed();

/* 协作编辑模式开关（默认开启；仅远端模型时生效） */
state.assistMode = true;
window.state = state;


async function saveTextSettings() {
  const button = $('save-text-settings');
  button.disabled = true;
  try {
    const result = await api('/api/settings/text', {
      method: 'PUT',
      body: {
        provider: $('text-provider-input')?.value || 'local',
        api_base: $('text-api-base')?.value || '',
        api_key: $('text-api-key')?.value || '',
        model: $('text-model-input')?.value || '',
      },
    });
    const message = $('text-save-message');
    if (message) { message.textContent = '已保存'; setTimeout(() => { message.textContent = ''; }, 2000); }
    const note = $('text-model-note');
    if (note) {
      note.className = 'settings-hint ' + (result.provider === 'openai_compatible' ? 'ok' : '');
      note.textContent = result.provider === 'openai_compatible'
        ? `主文本模型：${result.model}（已接入远端，协作编辑可用）`
        : '主文本模型：本地适配器（离线）。';
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}


async function saveVidlensSettings() {
  const button = $('save-vidlens-settings');
  button.disabled = true;
  try {
    await api('/api/settings/vision', {
      method: 'PUT',
      body: {
        provider: 'vidlens',
        vidlens_api_url: $('vidlens-api-url')?.value || '',
        vidlens_api_key: $('vidlens-api-key')?.value || '',
        vidlens_model: $('vidlens-model')?.value || '',
      },
    });
    const message = $('vidlens-save-message');
    if (message) { message.textContent = '已保存'; message.className = 'status'; setTimeout(() => { message.textContent = ''; }, 2000); }
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

window.applyFormat = applyFormat;
window.saveTextSettings = saveTextSettings;
window.saveVidlensSettings = saveVidlensSettings;
window.clearFormat = clearFormat;
window.selectVisionProvider = selectVisionProvider;
window.saveVisionSettings = saveVisionSettings;
window.testModelConnection = testModelConnection;
window.quickSchedule = quickSchedule;
window.openArticleFromTree = openArticleFromTree;
window.newArticleChat = newArticleChat;
$('test-vision-connection')?.addEventListener('click', () => void testModelConnection('vision'));
$('save-text-settings')?.addEventListener('click', () => void saveTextSettings());
$('text-provider-input')?.addEventListener('change', () => { $('text-provider-input').dataset.touched = '1'; });
$('text-model-input')?.addEventListener('input', () => { $('text-model-input').dataset.touched = '1'; });
$('text-api-base')?.addEventListener('input', () => { $('text-api-base').dataset.touched = '1'; });
$('test-text-connection')?.addEventListener('click', () => void testModelConnection('text'));
$('test-vidlens-connection')?.addEventListener('click', () => void testModelConnection('vidlens'));
$('save-vidlens-settings')?.addEventListener('click', () => void saveVidlensSettings());
$('vidlens-api-url')?.addEventListener('input', () => { $('vidlens-api-url').dataset.touched = '1'; });
$('vidlens-model')?.addEventListener('input', () => { $('vidlens-model').dataset.touched = '1'; });

/* ============ 对话区拖拽/粘贴素材（Codex 式：先挂输入框，发送时一起发） ============ */
function articleChatDropTarget(el) {
  return !!(el && el.closest && el.closest('.article-chat-panel'));
}

function attachmentPreview(item) {
  if (item.type === 'IMAGE') {
    return `<img src="${item.previewUrl}" alt="${escapeHtml(item.name)}" />`;
  }
  return `<span class="asset-file-badge small">${escapeHtml((item.name.split('.').pop() || 'FILE').toUpperCase())}</span>`;
}

function renderPendingAttachments() {
  const host = $('article-chat-attachments');
  if (!host) return;
  host.hidden = state.pendingAttachments.length === 0;
  host.innerHTML = state.pendingAttachments.map((item, index) => `
    <span class="attach-chip">
      ${attachmentPreview(item)}
      <span class="attach-chip-name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
      <button type="button" class="attach-chip-remove" onclick="removePendingAttachment(${index})" aria-label="移除">×</button>
    </span>`).join('');
}

function removePendingAttachment(index) {
  const item = state.pendingAttachments[index];
  if (item?.previewUrl?.startsWith('blob:')) URL.revokeObjectURL(item.previewUrl);
  state.pendingAttachments.splice(index, 1);
  renderPendingAttachments();
}

function enqueueChatFiles(files) {
  const input = $('article-chat-input');
  if (!input || document.querySelector('.article-chat-panel')?.hidden) {
    toast('请先打开一篇文章的对话再添加素材', true);
    return;
  }
  for (const file of files) {
    const isImage = file.type.startsWith('image/');
    state.pendingAttachments.push({
      file,
      name: file.name || (isImage ? '粘贴图片.png' : '未命名文件'),
      type: isImage ? 'IMAGE' : 'FILE',
      previewUrl: isImage ? URL.createObjectURL(file) : '',
    });
  }
  renderPendingAttachments();
  input.focus();
}

async function flushPendingAttachments() {
  // 发送时上传：素材挂到当前文章对话（article_id），公共素材库不混入
  const uploaded = [];
  for (const item of [...state.pendingAttachments]) {
    try {
      const form = new FormData();
      form.append('file', item.file);
      if (state.currentArticleId) form.append('article_id', state.currentArticleId);
      const asset = await api(`/api/sessions/${state.currentSession}/assets`, { method: 'POST', body: form });
      uploaded.push({ id: asset.id, name: asset.file_name, asset_type: asset.asset_type });
    } catch (error) {
      toast(`${item.name}：${error.message}`, true);
    } finally {
      if (item.previewUrl?.startsWith('blob:')) URL.revokeObjectURL(item.previewUrl);
    }
  }
  state.pendingAttachments = [];
  renderPendingAttachments();
  return uploaded;
}

document.addEventListener('dragover', event => {
  if (!event.dataTransfer || ![...event.dataTransfer.types].includes('Files')) return;
  event.preventDefault(); // 任何文件拖入都阻止浏览器默认打开行为
  const panel = document.querySelector('.article-chat-panel');
  if (articleChatDropTarget(event.target)) {
    event.dataTransfer.dropEffect = 'copy';
    panel?.classList.add('drag-over');
  } else {
    event.dataTransfer.dropEffect = 'none';
  }
});
document.addEventListener('dragleave', event => {
  if (!articleChatDropTarget(event.target)) document.querySelector('.article-chat-panel')?.classList.remove('drag-over');
});
document.addEventListener('drop', event => {
  event.preventDefault(); // 兜底：拖到页面任何位置都不让浏览器直接打开文件
  document.querySelector('.article-chat-panel')?.classList.remove('drag-over');
  if (!articleChatDropTarget(event.target)) return;
  const files = [...(event.dataTransfer?.files || [])];
  if (files.length) enqueueChatFiles(files);
});
// 新对话对话框：拖入/粘贴素材 → 对话框待发区（首条消息一起发送）
const newChatDropTarget = el => !!(el && el.closest && el.closest('#new-chat-modal'));
document.addEventListener('dragover', event => {
  if (!newChatDropTarget(event.target)) return;
  event.preventDefault();
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
  $('new-chat-modal')?.classList.add('drag-over');
});
document.addEventListener('drop', event => {
  if (!newChatDropTarget(event.target)) { $('new-chat-modal')?.classList.remove('drag-over'); return; }
  event.preventDefault();
  $('new-chat-modal')?.classList.remove('drag-over');
  for (const file of [...(event.dataTransfer?.files || [])]) {
    const isImage = file.type.startsWith('image/');
    (state.modalAttachments = state.modalAttachments || []).push({
      file,
      name: file.name || '粘贴图片.png',
      type: isImage ? 'IMAGE' : 'FILE',
      previewUrl: isImage ? URL.createObjectURL(file) : '',
    });
  }
  renderModalAttachments();
});
document.addEventListener('paste', event => {
  if (!newChatDropTarget(event.target)) return;
  const files = [...(event.clipboardData?.files || [])];
  if (!files.length) return;
  event.preventDefault();
  for (const file of files) {
    const isImage = file.type.startsWith('image/');
    (state.modalAttachments = state.modalAttachments || []).push({
      file, name: file.name || '粘贴图片.png', type: isImage ? 'IMAGE' : 'FILE',
      previewUrl: isImage ? URL.createObjectURL(file) : '',
    });
  }
  renderModalAttachments();
});
document.addEventListener('keydown', event => {
  const modal = $('new-chat-modal');
  if (!modal || modal.style.display === 'none') return;
  if (event.key === 'Escape') { closeNewChatModal(); return; }
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void submitNewChat(); }
});
// 粘贴截图/图片 → 直接入待发区
document.addEventListener('paste', event => {
  if (!articleChatDropTarget(event.target) && event.target !== document.body) return;
  const files = [...(event.clipboardData?.files || [])];
  if (files.length) {
    event.preventDefault();
    enqueueChatFiles(files);
  }
});
window.removePendingAttachment = removePendingAttachment;

/* ============ 点击工具消息回放高亮 ============ */
function showToolHighlight(el) {
  const label = el.dataset.label || '';
  const same = state.toolHighlight?.label === label;
  clearToolHighlight();
  if (same) { renderArticleChatMessages(); return; }
  let blocks = [];
  try { blocks = JSON.parse(el.dataset.blocks || '[]'); } catch { blocks = []; }
  if (!blocks.length) return;
  const texts = new Set(blocks.map(t => String(t).trim()));
  let first = null;
  document.querySelectorAll('.article-editor-section p[data-paragraph-index]').forEach(p => {
    if (texts.has((p.textContent || '').trim())) {
      const mark = document.createElement('mark');
      mark.className = 'tool-replay';
      mark.textContent = p.textContent;
      p.replaceChildren(mark);
      first = first || p;
    }
  });
  if (!first) { toast('该次改动的段落已不在当前文章中', true); return; }
  state.toolHighlight = { label, count: blocks.length };
  first.scrollIntoView({ behavior: 'smooth', block: 'center' });
  const host = document.querySelector('.article-preview .preview-zoom') || document.querySelector('.article-preview');
  if (host) {
    host.insertAdjacentHTML('afterbegin', `<div class="ai-highlight-banner" id="tool-replay-banner">🔍 正在查看：${escapeHtml(label)}（${blocks.length} 段）<button type="button" class="link-btn" onclick="clearToolHighlight(true)">收起</button></div>`);
  }
  renderArticleChatMessages();
}

function clearToolHighlight(rerender) {
  state.toolHighlight = null;
  document.getElementById('tool-replay-banner')?.remove();
  document.querySelectorAll('mark.tool-replay').forEach(m => {
    const p = m.parentElement;
    if (p) p.replaceChildren(document.createTextNode(m.textContent));
  });
  if (rerender) renderArticleChatMessages();
}
function showAssistantReplay(btn) {
  // 与工具消息回放同一机制：label 换成版本号
  showToolHighlight(btn);
}
window.showToolHighlight = showToolHighlight;
window.showAssistantReplay = showAssistantReplay;

async function revertArticleVersion(toVersion) {
  if (!state.currentArticleId || state.articleChatBusy) return;
  const articleId = state.currentArticleId;
  state.articleMessages.push({ role: 'NOTICE', content: `正在回退到第 ${toVersion} 版…`, status: 'COMPLETED' });
  renderArticleChatMessages();
  try {
    const result = await api(`/api/articles/${articleId}/revert`, { method: 'POST', body: { to_version: toVersion } });
    toast(`已回退到第 ${toVersion} 版（当前第 ${result.new_version} 版）`);
    await renderArticle();
    await loadSession();
    await refreshArticles();
  } catch (error) {
    toast(`回退失败：${error.message}`, true);
  }
}
window.revertArticleVersion = revertArticleVersion;
window.uploadToGroup = uploadToGroup;
window.toggleAssetAccount = toggleAssetAccount;

function uploadToCurrentDialog() {
  if (!state.currentArticleId || !state.currentSession) {
    toast('请先在左侧打开一个对话，或点击具体分组的「＋ 上传到此对话」', true);
    return;
  }
  uploadToGroup(state.currentSession, state.currentArticleId);
}
window.uploadToCurrentDialog = uploadToCurrentDialog;
window.togglePublishedTree = togglePublishedTree;
window.confirmDeleteArticle = confirmDeleteArticle;
window.openImportDrafts = openImportDrafts;
window.closeImportDrafts = closeImportDrafts;
window.importWechatDraft = importWechatDraft;

async function syncDraftFromChat() {
  if (!state.currentArticleId || state.articleChatBusy) return;
  toast('正在同步微信草稿…');
  try { await publishArticle('draft'); } catch (error) { toast(`同步失败：${error.message}`, true); }
}
window.syncDraftFromChat = syncDraftFromChat;
window.clearToolHighlight = clearToolHighlight;

function dismissAiHighlight() {
  const latest = (state.articleVersions || []).filter(v => v.highlight_json).at(-1);
  state.aiHighlightDismissedVersion = latest?.version_no ?? 0;  // 新版本出现后自动恢复显示
  renderArticle();
}
window.dismissAiHighlight = dismissAiHighlight;

init().catch(e => toast(e.message, true));
