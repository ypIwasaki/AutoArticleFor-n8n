const state = {
  dashboard: null,
  view: 'talents',
  selected: null,
  filters: { search: '', status: '', organization: '', confidence: 0, linkedOnly: false, summarizedOnly: false },
};

const elements = {
  table: document.querySelector('#data-table'),
  notice: document.querySelector('#data-notice'),
  search: document.querySelector('#search-input'),
  status: document.querySelector('#status-filter'),
  organization: document.querySelector('#organization-filter'),
  confidence: document.querySelector('#confidence-filter'),
  confidenceValue: document.querySelector('#confidence-value'),
  linkedOnly: document.querySelector('#linked-only'),
  summarizedOnly: document.querySelector('#summarized-only'),
  detail: document.querySelector('#detail-content'),
};

function html(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[character]));
}

function date(value) {
  if (!value) return '-';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value).slice(0, 10);
  return new Intl.DateTimeFormat('ja-JP', { dateStyle: 'medium' }).format(parsed);
}

function compactDate(value) {
  return value ? String(value).slice(0, 10) : '-';
}

function safeUrl(value) {
  try {
    const parsed = new URL(String(value || ''));
    return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : '';
  } catch {
    return '';
  }
}

function aliases(value) {
  try { return Array.isArray(value) ? value : JSON.parse(value || '[]'); } catch { return []; }
}

function statusBadge(status) {
  const normalized = ['pending', 'approved', 'rejected'].includes(status) ? status : 'unknown';
  return `<span class="status ${normalized}">${html(normalized)}</span>`;
}

function confidence(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(2) : '-';
}

function setSource(dashboard) {
  document.querySelector('#source-badge').textContent = dashboard.source === 'n8n-data-tables' ? 'n8n Data Tables' : 'Proposal files';
  document.querySelector('#generated-at').textContent = `更新: ${date(dashboard.generatedAt)}`;
  if (dashboard.sourceError) {
    elements.notice.hidden = false;
    elements.notice.textContent = `n8n Data Tablesを読み込めないため、Git管理の提案JSONを表示しています。${dashboard.sourceError}`;
  } else {
    elements.notice.hidden = true;
  }
}

function renderMetrics(summary) {
  document.querySelector('#metric-talents').textContent = summary.talents.toLocaleString('ja-JP');
  document.querySelector('#metric-articles').textContent = summary.articles.toLocaleString('ja-JP');
  document.querySelector('#metric-relations').textContent = summary.relations.toLocaleString('ja-JP');
  document.querySelector('#metric-summaries').textContent = summary.articleSummaries.toLocaleString('ja-JP');
  const points = summary.dailyVolume;
  document.querySelector('#activity-range').textContent = points.length ? `${points[0].date} - ${points[points.length - 1].date}` : 'データなし';
  drawActivity(points);
}

function drawActivity(points) {
  const canvas = document.querySelector('#activity-chart');
  const context = canvas.getContext('2d');
  const bounds = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(bounds.width * ratio));
  canvas.height = Math.max(1, Math.floor(bounds.height * ratio));
  context.scale(ratio, ratio);
  const width = bounds.width;
  const height = bounds.height;
  context.clearRect(0, 0, width, height);
  context.fillStyle = '#66727f';
  context.font = '11px system-ui';
  if (!points.length) { context.fillText('記事データがありません', 8, 24); return; }
  const maximum = Math.max(...points.map((point) => point.count), 1);
  const gap = 9;
  const barWidth = Math.max(16, (width - gap * (points.length - 1)) / points.length);
  points.forEach((point, index) => {
    const barHeight = Math.max(5, ((height - 34) * point.count) / maximum);
    const x = index * (barWidth + gap);
    const y = height - 20 - barHeight;
    context.fillStyle = '#0d7d74';
    context.fillRect(x, y, barWidth, barHeight);
    context.fillStyle = '#66727f';
    context.fillText(String(point.count), x, Math.max(11, y - 5));
    context.fillText(point.date.slice(5), x, height - 4);
  });
}

function filterRows() {
  const { dashboard, view, filters } = state;
  if (!dashboard) return [];
  const query = filters.search.trim().toLocaleLowerCase('ja-JP');
  const includes = (value) => String(value ?? '').toLocaleLowerCase('ja-JP').includes(query);
  if (view === 'talents') {
    return dashboard.talents.filter((row) => {
      const haystack = [row.display_name, row.organization, ...aliases(row.aliases_json)].some(includes);
      return (!query || haystack)
        && (!filters.status || row.status === filters.status)
        && (!filters.organization || row.organization === filters.organization)
        && (!filters.linkedOnly || row.article_count > 0);
    });
  }
  if (view === 'articles') {
    return dashboard.articles.filter((row) => {
      const haystack = [row.title, row.source, row.excerpt, row.ai_summary, ...(row.talents || []).map((talent) => talent.display_name)].some(includes);
      return (!query || haystack)
        && (!filters.linkedOnly || (row.talents || []).length > 0)
        && (!filters.summarizedOnly || Boolean(row.ai_summary));
    });
  }
  return dashboard.relations.filter((row) => {
    const haystack = [row.talent?.display_name, row.talent?.organization, row.article?.title, row.evidence_text].some(includes);
    return (!query || haystack)
      && (!filters.status || row.talent?.status === filters.status)
      && (!filters.organization || row.talent?.organization === filters.organization)
      && Number(row.confidence || 0) >= filters.confidence;
  });
}

function renderTable() {
  const rows = filterRows();
  const { view, selected } = state;
  const headers = view === 'talents'
    ? ['タレント', '組織', '状態', '記事数', '最終確認']
    : view === 'articles'
      ? ['記事', '公開日', 'AI要約', '関連タレント', '情報源']
      : ['タレント', '記事', '信頼度', '検出方法'];
  elements.table.querySelector('thead').innerHTML = `<tr>${headers.map((header) => `<th>${header}</th>`).join('')}</tr>`;
  elements.table.querySelector('tbody').innerHTML = rows.length ? rows.map((row) => rowMarkup(row, view, selected)).join('') : `<tr><td colspan="${headers.length}"><p class="empty-detail">条件に一致するデータはありません。</p></td></tr>`;
  document.querySelector('#result-count').textContent = `${rows.length} 件`;
  document.querySelector('#view-title').textContent = view === 'talents' ? 'タレント候補' : view === 'articles' ? '取得記事' : '記事・タレント関係';
  elements.table.querySelectorAll('tbody tr[data-key]').forEach((row) => row.addEventListener('click', () => selectRow(row.dataset.key)));
}

function rowMarkup(row, view, selected) {
  const key = view === 'talents' ? row.talent_id : view === 'articles' ? row.article_key : row.relation_key;
  const className = selected?.key === key ? 'is-selected' : '';
  if (view === 'talents') {
    return `<tr class="${className}" data-key="${html(key)}"><td><div class="cell-title">${html(row.display_name)}</div><div class="cell-subtitle">${html(aliases(row.aliases_json).join(' / ') || '別名なし')}</div></td><td>${html(row.organization || '-')}</td><td>${statusBadge(row.status)}</td><td>${row.article_count || 0}</td><td>${compactDate(row.last_seen_at)}</td></tr>`;
  }
  if (view === 'articles') {
    const chips = (row.talents || []).slice(0, 3).map((talent) => `<span class="talent-chip">${html(talent.display_name)}</span>`).join('') || '<span class="muted">未紐付け</span>';
    const summaryStatus = row.ai_summary
      ? `<span class="summary-status complete">要約あり</span><div class="cell-subtitle">${html(row.summary_date)}</div>`
      : '<span class="summary-status missing">未作成</span>';
    return `<tr class="${className}" data-key="${html(key)}"><td><div class="cell-title">${html(row.title || '(無題)')}</div><div class="cell-subtitle">${html(row.excerpt || row.url || '')}</div></td><td>${compactDate(row.published_at || row.last_seen_at)}</td><td>${summaryStatus}</td><td>${chips}</td><td>${html(row.source || '-')}</td></tr>`;
  }
  return `<tr class="${className}" data-key="${html(key)}"><td><div class="cell-title">${html(row.talent?.display_name || '-')}</div><div class="cell-subtitle">${html(row.talent?.organization || '')}</div></td><td><div class="cell-title">${html(row.article?.title || '-')}</div><div class="cell-subtitle">${compactDate(row.article?.published_at || row.last_seen_at)}</div></td><td>${confidence(row.confidence)}</td><td>${html(row.detection_method || '-')}</td></tr>`;
}

function selectRow(key) {
  const { view, dashboard } = state;
  const row = view === 'talents'
    ? dashboard.talents.find((item) => item.talent_id === key)
    : view === 'articles'
      ? dashboard.articles.find((item) => item.article_key === key)
      : dashboard.relations.find((item) => item.relation_key === key);
  if (!row) return;
  state.selected = { key, row };
  renderTable();
  renderDetail(row, view);
}

function renderDetail(row, view) {
  const relations = state.dashboard.relations;
  if (view === 'talents') {
    const links = relations.filter((relation) => relation.talent_id === row.talent_id);
    elements.detail.innerHTML = `<p class="detail-kicker">TALENT</p><h2 class="detail-title">${html(row.display_name)}</h2><p class="detail-meta">${html(row.organization || '組織未設定')} &nbsp; ${statusBadge(row.status)}</p>${keyValues([['検索有効', row.search_enabled ? '有効' : '無効'], ['別名', aliases(row.aliases_json).join(', ') || '-'], ['関連記事', `${links.length} 件`], ['最終確認', date(row.last_seen_at)]])}<section class="detail-section"><h3>関連記事</h3><ul class="detail-list">${links.map((relation) => articleListItem(relation.article, relation)).join('') || '<li class="muted">関連記事はありません。</li>'}</ul></section>`;
    return;
  }
  if (view === 'articles') {
    const links = relations.filter((relation) => relation.article_key === row.article_key);
    const articleUrl = safeUrl(row.url);
    const summarySection = row.ai_summary
      ? `<section class="detail-section"><h3>AI要約</h3><p class="summary-date">保存日: ${html(row.summary_date || '-')}</p><p class="evidence">${html(row.ai_summary)}</p></section>`
      : '<section class="detail-section"><h3>AI要約</h3><p class="muted">要約未作成</p></section>';
    elements.detail.innerHTML = `<p class="detail-kicker">ARTICLE</p><h2 class="detail-title">${html(row.title || '(無題)')}</h2><p class="detail-meta">${date(row.published_at || row.last_seen_at)} &nbsp; ${html(row.source || '情報源未設定')}</p>${articleUrl ? `<p><a class="article-link" href="${html(articleUrl)}" target="_blank" rel="noreferrer">元記事を開く</a></p>` : ''}${summarySection}<section class="detail-section"><h3>RSS抜粋</h3><p class="evidence">${html(row.excerpt || '抜粋なし')}</p></section><section class="detail-section"><h3>関連タレント</h3><ul class="detail-list">${links.map((relation) => `<li><strong>${html(relation.talent?.display_name || '-')}</strong> ${statusBadge(relation.talent?.status)}<p class="evidence">${html(relation.evidence_text || '')}</p></li>`).join('') || '<li class="muted">関連タレントはありません。</li>'}</ul></section>`;
    return;
  }
  elements.detail.innerHTML = `<p class="detail-kicker">RELATION</p><h2 class="detail-title">${html(row.talent?.display_name || '-')}</h2><p class="detail-meta">記事との関連 / 信頼度 ${confidence(row.confidence)}</p>${keyValues([['組織', row.talent?.organization || '-'], ['検出方法', row.detection_method || '-'], ['対象フィールド', row.matched_fields || '-'], ['最終確認', date(row.last_seen_at)]])}<section class="detail-section"><h3>根拠</h3><p class="evidence">${html(row.evidence_text || '-')}</p></section><section class="detail-section"><h3>記事</h3><ul class="detail-list">${articleListItem(row.article, null)}</ul></section>`;
}

function keyValues(items) { return `<dl class="key-value">${items.map(([key, value]) => `<dt>${html(key)}</dt><dd>${html(value)}</dd>`).join('')}</dl>`; }
function articleListItem(article, relation) {
  const title = html(article?.title || '(記事不明)');
  const url = safeUrl(article?.url);
  const link = url ? `<a href="${html(url)}" target="_blank" rel="noreferrer">${title}</a>` : `<strong>${title}</strong>`;
  return `<li>${link}<div class="cell-subtitle">${compactDate(article?.published_at || relation?.last_seen_at)}${relation?.confidence != null ? ` / 信頼度 ${confidence(relation.confidence)}` : ''}</div></li>`;
}

function renderAll() { if (!state.dashboard) return; renderMetrics(state.dashboard.summary); renderTable(); }

async function loadDashboard() {
  const button = document.querySelector('#reload-button');
  button.disabled = true;
  try {
    const response = await fetch('/api/dashboard', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.dashboard = await response.json();
    state.selected = null;
    setSource(state.dashboard);
    populateOrganizations(state.dashboard.summary.organizations || []);
    renderAll();
    elements.detail.innerHTML = '<p class="empty-detail">選択なし</p>';
  } catch (error) {
    elements.notice.hidden = false;
    elements.notice.textContent = `データを読み込めませんでした: ${error.message}`;
  } finally { button.disabled = false; }
}

function populateOrganizations(organizations) {
  const current = state.filters.organization;
  elements.organization.innerHTML = '<option value="">すべて</option>' + organizations.map((organization) => `<option value="${html(organization)}">${html(organization)}</option>`).join('');
  elements.organization.value = current;
}

document.querySelectorAll('.tab-button').forEach((button) => button.addEventListener('click', () => {
  state.view = button.dataset.view;
  state.selected = null;
  document.querySelectorAll('.tab-button').forEach((tab) => { const active = tab === button; tab.classList.toggle('is-active', active); tab.setAttribute('aria-selected', String(active)); });
  elements.detail.innerHTML = '<p class="empty-detail">選択なし</p>';
  renderTable();
}));

elements.search.addEventListener('input', () => { state.filters.search = elements.search.value; renderTable(); });
elements.status.addEventListener('change', () => { state.filters.status = elements.status.value; renderTable(); });
elements.organization.addEventListener('change', () => { state.filters.organization = elements.organization.value; renderTable(); });
elements.confidence.addEventListener('input', () => { state.filters.confidence = Number(elements.confidence.value); elements.confidenceValue.value = state.filters.confidence.toFixed(2); renderTable(); });
elements.linkedOnly.addEventListener('change', () => { state.filters.linkedOnly = elements.linkedOnly.checked; renderTable(); });
elements.summarizedOnly.addEventListener('change', () => { state.filters.summarizedOnly = elements.summarizedOnly.checked; renderTable(); });
document.querySelector('#clear-filters').addEventListener('click', () => { state.filters = { search: '', status: '', organization: '', confidence: 0, linkedOnly: false, summarizedOnly: false }; elements.search.value = ''; elements.status.value = ''; elements.organization.value = ''; elements.confidence.value = '0'; elements.confidenceValue.value = '0.00'; elements.linkedOnly.checked = false; elements.summarizedOnly.checked = false; renderTable(); });
document.querySelector('#reload-button').addEventListener('click', loadDashboard);
document.querySelector('#close-detail').addEventListener('click', () => { state.selected = null; elements.detail.innerHTML = '<p class="empty-detail">選択なし</p>'; renderTable(); });
window.addEventListener('resize', () => state.dashboard && drawActivity(state.dashboard.summary.dailyVolume));
loadDashboard();
