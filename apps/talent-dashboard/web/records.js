(() => {
  const page = document.body.dataset.page || '';
  const state = { dashboard: null, pageNumber: 1 };

  const elements = {
    notice: document.querySelector('#data-notice'),
    sourceBadge: document.querySelector('#source-badge'),
    generatedAt: document.querySelector('#generated-at'),
    reload: document.querySelector('#reload-button'),
    table: document.querySelector('#records-table'),
    resultCount: document.querySelector('#result-count'),
    recordCount: document.querySelector('#record-count'),
    pagination: document.querySelector('#pagination'),
    detail: document.querySelector('#record-detail'),
    search: document.querySelector('#search-input'),
    status: document.querySelector('#status-filter'),
    organization: document.querySelector('#organization-filter'),
    source: document.querySelector('#source-filter'),
    linkedOnly: document.querySelector('#linked-only'),
    summarizedOnly: document.querySelector('#summarized-only'),
    clearFilters: document.querySelector('#clear-filters'),
  };

  function html(value) {
    const escaped = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
    return String(value ?? '').replace(/[&<>"']/g, (character) => escaped[character]);
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

  function aliases(value) {
    try { return Array.isArray(value) ? value : JSON.parse(value || '[]'); } catch { return []; }
  }

  function safeUrl(value) {
    try {
      const parsed = new URL(String(value || ''));
      return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : '';
    } catch {
      return '';
    }
  }

  function recordUrl(kind, key) {
    return kind + '.html?id=' + encodeURIComponent(String(key || ''));
  }

  function statusBadge(status) {
    const normalized = ['pending', 'approved', 'rejected'].includes(status) ? status : 'unknown';
    return '<span class="status ' + normalized + '">' + html(normalized) + '</span>';
  }

  function sourceLabel(dashboard) {
    return dashboard.source === 'n8n-data-tables' ? 'n8n Data Tables' : 'Proposal files';
  }

  function setSource(dashboard) {
    if (elements.sourceBadge) elements.sourceBadge.textContent = sourceLabel(dashboard);
    if (elements.generatedAt) elements.generatedAt.textContent = '更新: ' + date(dashboard.generatedAt);
    if (dashboard.sourceError) {
      showNotice('n8n Data Tablesを読み込めないため、Git管理の提案JSONを表示しています。' + dashboard.sourceError);
    } else {
      hideNotice();
    }
  }

  function showNotice(message) {
    if (!elements.notice) return;
    elements.notice.hidden = false;
    elements.notice.textContent = message;
  }

  function hideNotice() {
    if (elements.notice) elements.notice.hidden = true;
  }

  function haystackIncludes(query, values) {
    if (!query) return true;
    return values.some((value) => String(value ?? '').toLocaleLowerCase('ja-JP').includes(query));
  }

  function populateOptions(select, values) {
    if (!select) return;
    const current = select.value;
    select.innerHTML = '<option value="">すべて</option>' + values.map((value) => '<option value="' + html(value) + '">' + html(value) + '</option>').join('');
    select.value = current;
  }

  function filteredTalents() {
    const query = (elements.search?.value || '').trim().toLocaleLowerCase('ja-JP');
    const status = elements.status?.value || '';
    const organization = elements.organization?.value || '';
    const linkedOnly = Boolean(elements.linkedOnly?.checked);
    return state.dashboard.talents.filter((talent) => {
      return haystackIncludes(query, [talent.display_name, talent.organization].concat(aliases(talent.aliases_json)))
        && (!status || talent.status === status)
        && (!organization || talent.organization === organization)
        && (!linkedOnly || Number(talent.article_count || 0) > 0);
    });
  }

  function filteredArticles() {
    const query = (elements.search?.value || '').trim().toLocaleLowerCase('ja-JP');
    const source = elements.source?.value || '';
    const linkedOnly = Boolean(elements.linkedOnly?.checked);
    const summarizedOnly = Boolean(elements.summarizedOnly?.checked);
    return state.dashboard.articles.filter((article) => {
      const talentNames = (article.talents || []).map((talent) => talent.display_name);
      return haystackIncludes(query, [article.title, article.source, article.excerpt, article.ai_summary].concat(talentNames))
        && (!source || article.source === source)
        && (!linkedOnly || (article.talents || []).length > 0)
        && (!summarizedOnly || Boolean(article.ai_summary));
    });
  }

  function renderPagination(total, pageSize, render) {
    if (!elements.pagination) return;
    const pages = Math.max(1, Math.ceil(total / pageSize));
    state.pageNumber = Math.min(Math.max(1, state.pageNumber), pages);
    if (pages === 1) {
      elements.pagination.innerHTML = '';
      return;
    }
    const current = state.pageNumber;
    const visible = [];
    for (let number = Math.max(1, current - 2); number <= Math.min(pages, current + 2); number += 1) visible.push(number);
    const button = (label, target, disabled, active) => '<button type="button" class="page-button' + (active ? ' is-active' : '') + '" data-page-number="' + target + '"' + (disabled ? ' disabled' : '') + '>' + label + '</button>';
    let markup = button('前へ', current - 1, current === 1, false);
    if (visible[0] > 1) markup += button('1', 1, false, false) + '<span class="pagination-ellipsis">…</span>';
    markup += visible.map((number) => button(String(number), number, false, number === current)).join('');
    if (visible[visible.length - 1] < pages) markup += '<span class="pagination-ellipsis">…</span>' + button(String(pages), pages, false, false);
    markup += button('次へ', current + 1, current === pages, false);
    elements.pagination.innerHTML = markup;
    elements.pagination.querySelectorAll('button[data-page-number]').forEach((buttonElement) => {
      buttonElement.addEventListener('click', () => {
        state.pageNumber = Number(buttonElement.dataset.pageNumber);
        render();
        window.scrollTo({ top: 0, behavior: 'smooth' });
      });
    });
  }

  function renderTalentList() {
    const rows = filteredTalents();
    const pageSize = 40;
    const pages = Math.max(1, Math.ceil(rows.length / pageSize));
    state.pageNumber = Math.min(Math.max(1, state.pageNumber), pages);
    const start = (state.pageNumber - 1) * pageSize;
    const pageRows = rows.slice(start, start + pageSize);
    elements.recordCount.textContent = Number(state.dashboard.summary.talents || 0).toLocaleString('ja-JP') + ' 件';
    elements.resultCount.textContent = rows.length + ' 件';
    elements.table.querySelector('thead').innerHTML = '<tr><th>タレント</th><th>組織</th><th>状態</th><th>記事数</th><th>最終確認</th></tr>';
    elements.table.querySelector('tbody').innerHTML = pageRows.length
      ? pageRows.map((talent) => '<tr><td><div class="cell-title"><a class="table-link" href="' + html(recordUrl('talent', talent.talent_id)) + '">' + html(talent.display_name) + '</a></div><div class="cell-subtitle">' + html(aliases(talent.aliases_json).join(' / ') || '別名なし') + '</div></td><td>' + html(talent.organization || '-') + '</td><td>' + statusBadge(talent.status) + '</td><td>' + Number(talent.article_count || 0) + '</td><td>' + compactDate(talent.last_seen_at) + '</td></tr>').join('')
      : '<tr><td colspan="5"><p class="empty-detail">条件に一致するタレントはいません。</p></td></tr>';
    renderPagination(rows.length, pageSize, renderTalentList);
  }

  function renderArticleList() {
    const rows = filteredArticles();
    const pageSize = 30;
    const pages = Math.max(1, Math.ceil(rows.length / pageSize));
    state.pageNumber = Math.min(Math.max(1, state.pageNumber), pages);
    const start = (state.pageNumber - 1) * pageSize;
    const pageRows = rows.slice(start, start + pageSize);
    elements.recordCount.textContent = Number(state.dashboard.summary.articles || 0).toLocaleString('ja-JP') + ' 件';
    elements.resultCount.textContent = rows.length + ' 件';
    elements.table.querySelector('thead').innerHTML = '<tr><th>記事</th><th>公開日</th><th>要約</th><th>関連タレント</th><th>情報源</th></tr>';
    elements.table.querySelector('tbody').innerHTML = pageRows.length
      ? pageRows.map((article) => {
        const chips = (article.talents || []).slice(0, 3).map((talent) => '<a class="talent-chip" href="' + html(recordUrl('talent', talent.talent_id)) + '">' + html(talent.display_name) + '</a>').join('') || '<span class="muted">未紐付け</span>';
        const summary = article.ai_summary ? '<span class="summary-status complete">本文確認済み</span>' : '<span class="summary-status missing">未作成</span>';
        return '<tr><td><div class="cell-title"><a class="table-link" href="' + html(recordUrl('article', article.article_key)) + '">' + html(article.title || '(無題)') + '</a></div><div class="cell-subtitle">' + html(article.excerpt || article.url || '') + '</div></td><td>' + compactDate(article.published_at || article.last_seen_at) + '</td><td>' + summary + '</td><td>' + chips + '</td><td>' + html(article.source || '-') + '</td></tr>';
      }).join('')
      : '<tr><td colspan="5"><p class="empty-detail">条件に一致する記事はありません。</p></td></tr>';
    renderPagination(rows.length, pageSize, renderArticleList);
  }

  function keyValues(items) {
    return '<dl class="key-value">' + items.map((item) => '<dt>' + html(item[0]) + '</dt><dd>' + html(item[1]) + '</dd>').join('') + '</dl>';
  }

  function recordNotFound(label) {
    elements.detail.innerHTML = '<section class="record-detail-panel"><p class="detail-kicker">NOT FOUND</p><h2 class="detail-title">' + label + 'が見つかりません</h2><p class="evidence">一覧に戻り、対象のデータが存在するか確認してください。</p></section>';
  }

  function bindSummaryToggles() {
    elements.detail.querySelectorAll('.summary-toggle').forEach((button) => {
      button.addEventListener('click', () => {
        const summaryRow = button.closest('tr')?.nextElementSibling;
        if (!summaryRow || !summaryRow.classList.contains('article-summary-row')) return;
        const expanded = summaryRow.hidden;
        summaryRow.hidden = !expanded;
        button.textContent = expanded ? '▼' : '▶';
        button.setAttribute('aria-expanded', String(expanded));
        button.setAttribute('aria-label', expanded ? '要約を閉じる' : '要約を表示');
        button.title = expanded ? '要約を閉じる' : '要約を表示';
      });
    });
  }

  function renderTalentDetail() {
    const identifier = new URLSearchParams(window.location.search).get('id') || '';
    const talent = state.dashboard.talents.find((row) => String(row.talent_id) === identifier);
    if (!talent) {
      recordNotFound('タレント');
      return;
    }
    document.title = talent.display_name + ' | Talent Index';
    const relations = state.dashboard.relations.filter((relation) => String(relation.talent_id) === String(talent.talent_id));
    const articlesByKey = new Map(state.dashboard.articles.map((article) => [String(article.article_key), article]));
    const articles = relations.map((relation) => ({ relation, article: articlesByKey.get(String(relation.article_key)) })).filter((item) => item.article);
    const rows = articles.map((item) => {
      const article = item.article;
      const summaryControl = article.ai_summary
        ? '<span class="summary-status complete">本文確認済み</span><button class="summary-toggle" type="button" aria-expanded="false" aria-label="要約を表示" title="要約を表示">▶</button>'
        : '<span class="summary-status missing">未作成</span>';
      const summaryRow = article.ai_summary
        ? '<tr class="article-summary-row" hidden><td colspan="4"><div class="article-summary-inline"><p class="summary-date">保存日: ' + html(article.summary_date || '-') + '</p><p>' + html(article.ai_summary) + '</p></div></td></tr>'
        : '';
      return '<tr><td><div class="cell-title"><a class="table-link" href="' + html(recordUrl('article', article.article_key)) + '">' + html(article.title || '(無題)') + '</a></div><div class="cell-subtitle">' + html(article.source || '-') + '</div></td><td>' + compactDate(article.published_at || article.last_seen_at) + '</td><td><div class="summary-cell">' + summaryControl + '</div></td><td><p class="evidence">' + html(item.relation.evidence_text || '-') + '</p></td></tr>' + summaryRow;
    }).join('') || '<tr><td colspan="4"><p class="empty-detail">紐づく記事はありません。</p></td></tr>';
    elements.detail.innerHTML = '<section class="record-detail-panel"><p class="detail-kicker">TALENT</p><h2 class="record-title">' + html(talent.display_name) + '</h2><p class="detail-meta">' + html(talent.organization || '組織未設定') + ' &nbsp; ' + statusBadge(talent.status) + '</p>' + keyValues([['検索キーワード', talent.search_enabled ? '有効' : '無効'], ['別名', aliases(talent.aliases_json).join(', ') || '-'], ['関連記事', articles.length + ' 件'], ['最終確認', date(talent.last_seen_at)]]) + '</section><section class="detail-table-section"><div class="detail-section-heading"><div><p class="eyebrow">LINKED ARTICLES</p><h2>関連記事</h2></div><span class="source-badge">' + articles.length + ' 件</span></div><div class="detail-table-scroll"><table><thead><tr><th>記事</th><th>公開日</th><th>要約</th><th>紐づけ根拠</th></tr></thead><tbody>' + rows + '</tbody></table></div></section>';
    bindSummaryToggles();
  }

  function renderArticleDetail() {
    const identifier = new URLSearchParams(window.location.search).get('id') || '';
    const article = state.dashboard.articles.find((row) => String(row.article_key) === identifier);
    if (!article) {
      recordNotFound('記事');
      return;
    }
    document.title = (article.title || '記事') + ' | Talent Index';
    const relationMap = new Map(state.dashboard.relations.filter((relation) => String(relation.article_key) === String(article.article_key)).map((relation) => [String(relation.talent_id), relation]));
    const sourceUrl = safeUrl(article.url);
    const talents = article.talents || [];
    const talentRows = talents.map((talent) => {
      const relation = relationMap.get(String(talent.talent_id));
      return '<tr><td><div class="cell-title"><a class="table-link" href="' + html(recordUrl('talent', talent.talent_id)) + '">' + html(talent.display_name) + '</a></div><div class="cell-subtitle">' + html(talent.organization || '-') + '</div></td><td>' + statusBadge(talent.status) + '</td><td><p class="evidence">' + html(relation?.evidence_text || '-') + '</p></td></tr>';
    }).join('') || '<tr><td colspan="3"><p class="empty-detail">関連タレントはありません。</p></td></tr>';
    const summary = article.ai_summary
      ? '<section class="article-summary"><div class="detail-section-heading"><div><p class="eyebrow">SAVED SUMMARY</p><h2>記事要約</h2></div><span class="summary-status complete">保存日: ' + html(article.summary_date || '-') + '</span></div><p>' + html(article.ai_summary) + '</p></section>'
      : '<section class="article-summary"><div class="detail-section-heading"><div><p class="eyebrow">SAVED SUMMARY</p><h2>記事要約</h2></div><span class="summary-status missing">未作成</span></div><p class="muted">要約指示書に基づく保存済み要約はありません。</p></section>';
    elements.detail.innerHTML = '<section class="record-detail-panel"><p class="detail-kicker">ARTICLE</p><h2 class="record-title">' + html(article.title || '(無題)') + '</h2><p class="detail-meta">' + date(article.published_at || article.last_seen_at) + ' &nbsp; ' + html(article.source || '情報源未設定') + '</p>' + (sourceUrl ? '<p><a class="article-link" href="' + html(sourceUrl) + '" target="_blank" rel="noreferrer">元記事を開く</a></p>' : '') + keyValues([['公開日', date(article.published_at || article.last_seen_at)], ['情報源', article.source || '-'], ['関連タレント', talents.length + ' 件'], ['保存済み要約', article.ai_summary ? 'あり' : 'なし']]) + '</section>' + summary + '<section class="detail-table-section"><div class="detail-section-heading"><div><p class="eyebrow">RELATED TALENTS</p><h2>関連タレント</h2></div><span class="source-badge">' + talents.length + ' 件</span></div><div class="detail-table-scroll"><table><thead><tr><th>タレント</th><th>状態</th><th>紐づけ根拠</th></tr></thead><tbody>' + talentRows + '</tbody></table></div></section><section class="excerpt-section"><p class="eyebrow">RSS EXCERPT</p><h2>取得時の抜粋</h2><p>' + html(article.excerpt || '抜粋なし') + '</p></section>';
  }

  function resetFilters() {
    if (elements.search) elements.search.value = '';
    if (elements.status) elements.status.value = '';
    if (elements.organization) elements.organization.value = '';
    if (elements.source) elements.source.value = '';
    if (elements.linkedOnly) elements.linkedOnly.checked = false;
    if (elements.summarizedOnly) elements.summarizedOnly.checked = false;
    state.pageNumber = 1;
    if (page === 'talent-list') renderTalentList();
    if (page === 'article-list') renderArticleList();
  }

  function bindListEvents(render) {
    [elements.search, elements.status, elements.organization, elements.source, elements.linkedOnly, elements.summarizedOnly].filter(Boolean).forEach((element) => {
      const eventName = element.type === 'search' ? 'input' : 'change';
      element.addEventListener(eventName, () => { state.pageNumber = 1; render(); });
    });
    elements.clearFilters?.addEventListener('click', resetFilters);
  }

  function renderPage() {
    if (page === 'talent-list') {
      populateOptions(elements.organization, state.dashboard.summary.organizations || []);
      bindListEvents(renderTalentList);
      renderTalentList();
      return;
    }
    if (page === 'article-list') {
      const sources = [...new Set(state.dashboard.articles.map((article) => String(article.source || '').trim()).filter(Boolean))].sort((left, right) => left.localeCompare(right, 'ja'));
      populateOptions(elements.source, sources);
      bindListEvents(renderArticleList);
      renderArticleList();
      return;
    }
    if (page === 'talent-detail') {
      renderTalentDetail();
      return;
    }
    if (page === 'article-detail') renderArticleDetail();
  }

  async function loadDashboard() {
    if (elements.reload) elements.reload.disabled = true;
    try {
      const response = await fetch('/api/dashboard', { cache: 'no-store' });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      state.dashboard = await response.json();
      setSource(state.dashboard);
      renderPage();
    } catch (error) {
      showNotice('データを読み込めませんでした: ' + error.message);
      if (elements.detail) elements.detail.innerHTML = '<section class="record-detail-panel"><p class="empty-detail">データを読み込めませんでした。</p></section>';
    } finally {
      if (elements.reload) elements.reload.disabled = false;
    }
  }

  if (['talent-list', 'article-list', 'talent-detail', 'article-detail'].includes(page)) {
    elements.reload?.addEventListener('click', loadDashboard);
    loadDashboard();
  }
})();
