(() => {
  const pageState = window.TalentIndexPageState;
  const saved = pageState?.get('weekly-reports', {}) || {};
  const state = {
    reports: [],
    selectedWeekStart: String(saved.selectedWeekStart || ''),
    restoringScroll: true,
  };
  const elements = {
    list: document.querySelector('#weekly-report-list'),
    count: document.querySelector('#report-count'),
    notice: document.querySelector('#report-notice'),
    title: document.querySelector('#weekly-report-title'),
    period: document.querySelector('#weekly-report-period'),
    meta: document.querySelector('#weekly-report-meta'),
    content: document.querySelector('#weekly-report-content'),
    reload: document.querySelector('#reload-button'),
  };

  function html(value) {
    return String(value ?? '').replace(/[&<>'"]/g, (character) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      "'": '&#39;',
      '"': '&quot;',
    }[character]));
  }

  function safeUrl(value) {
    try {
      const parsed = new URL(String(value || ''));
      return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : '';
    } catch {
      return '';
    }
  }

  function formatDate(value) {
    if (!value) return '-';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value);
    return new Intl.DateTimeFormat('ja-JP', { dateStyle: 'medium' }).format(parsed);
  }

  function setNotice(message) {
    elements.notice.hidden = !message;
    elements.notice.textContent = message || '';
  }

  async function requestJson(url) {
    const response = await fetch(url, { cache: 'no-store' });
    const body = await response.text();
    let payload;
    try {
      payload = JSON.parse(body);
    } catch {
      const isHtml = body.trim().startsWith('<');
      throw new Error(isHtml
        ? 'レポートAPIではない応答を受信しました。ダッシュボードを再起動してください。'
        : 'レポートAPIからJSONを読み取れませんでした。');
    }
    if (!response.ok) {
      throw new Error(payload.error || 'レポートを取得できませんでした。');
    }
    return payload;
  }

  function persist() {
    pageState?.set('weekly-reports', { selectedWeekStart: state.selectedWeekStart });
  }

  function renderInline(value) {
    const links = [];
    const tokenized = String(value || '').replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (match, label, url) => {
      const destination = safeUrl(url);
      if (!destination) return match;
      const token = '@@WEEKLY_LINK_' + links.length + '@@';
      links.push({ label, destination });
      return token;
    });
    let rendered = html(tokenized);
    links.forEach((link, index) => {
      const token = '@@WEEKLY_LINK_' + index + '@@';
      rendered = rendered.replace(token, '<a class="article-link" href="' + html(link.destination) + '" target="_blank" rel="noreferrer">' + html(link.label) + '</a>');
    });
    return rendered;
  }

  function cells(line) {
    return line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim());
  }

  function markdownToHtml(markdown) {
    const body = String(markdown || '').replace(/^---\s*\n[\s\S]*?\n---\s*\n/, '').replace(/\r\n/g, '\n');
    const lines = body.split('\n');
    const output = [];
    let index = 0;

    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        const level = heading[1].length;
        output.push('<h' + level + '>' + renderInline(heading[2]) + '</h' + level + '>');
        index += 1;
        continue;
      }
      if (line.trim().startsWith('|')) {
        const rows = [];
        while (index < lines.length && lines[index].trim().startsWith('|')) {
          rows.push(cells(lines[index]));
          index += 1;
        }
        if (rows.length >= 2 && rows[1].every((cell) => /^:?-{3,}:?$/.test(cell))) {
          const header = rows[0];
          const bodyRows = rows.slice(2);
          const head = '<thead><tr>' + header.map((cell) => '<th>' + renderInline(cell) + '</th>').join('') + '</tr></thead>';
          const tableBody = '<tbody>' + bodyRows.map((row) => '<tr>' + header.map((_, column) => '<td>' + renderInline(row[column] || '') + '</td>').join('') + '</tr>').join('') + '</tbody>';
          output.push('<div class="weekly-report-table-scroll"><table class="weekly-report-table">' + head + tableBody + '</table></div>');
        } else {
          output.push('<p>' + renderInline(rows.map((row) => row.join(' | ')).join(' ')) + '</p>');
        }
        continue;
      }
      if (/^-\s+/.test(line)) {
        const items = [];
        while (index < lines.length && /^-\s+/.test(lines[index])) {
          items.push(lines[index].replace(/^-\s+/, ''));
          index += 1;
        }
        output.push('<ul>' + items.map((item) => '<li>' + renderInline(item) + '</li>').join('') + '</ul>');
        continue;
      }
      const paragraphs = [line.trim()];
      index += 1;
      while (index < lines.length && lines[index].trim() && !/^(#{1,3}\s+|-\s+|\|)/.test(lines[index])) {
        paragraphs.push(lines[index].trim());
        index += 1;
      }
      output.push('<p>' + renderInline(paragraphs.join(' ')) + '</p>');
    }
    return output.join('');
  }

  function renderList() {
    elements.count.textContent = state.reports.length + ' 週';
    if (!state.reports.length) {
      elements.list.innerHTML = '<p class="empty-detail">保存済みの週間レポートはありません。</p>';
      return;
    }
    elements.list.innerHTML = state.reports.map((report) => {
      const active = report.weekStart === state.selectedWeekStart;
      const range = report.weekEnd ? report.weekStart + ' - ' + report.weekEnd : report.weekStart;
      return '<button type="button" class="weekly-report-list-item' + (active ? ' is-active' : '') + '" data-week-start="' + html(report.weekStart) + '" aria-pressed="' + String(active) + '"><span class="weekly-report-list-date">' + html(range) + '</span><strong>' + html(report.title || 'Weekly News Research Report') + '</strong><span>' + html(report.summary || '内容を表示') + '</span></button>';
    }).join('');
    elements.list.querySelectorAll('[data-week-start]').forEach((button) => {
      button.addEventListener('click', () => {
        const next = String(button.dataset.weekStart || '');
        if (!next || next === state.selectedWeekStart) return;
        state.selectedWeekStart = next;
        persist();
        renderList();
        loadReport();
      });
    });
  }

  async function loadReport() {
    if (!state.selectedWeekStart) {
      elements.title.textContent = '週間レポートはありません';
      elements.content.innerHTML = '<p class="empty-detail">n8nの日次取得後に週間レポート指示書を処理すると、ここに表示されます。</p>';
      return;
    }
    elements.content.innerHTML = '<p class="empty-detail">レポートを読み込み中です。</p>';
    try {
      const report = await requestJson('/api/weekly-reports/' + encodeURIComponent(state.selectedWeekStart));
      document.title = report.weekStart + ' 週間レポート | Talent Index';
      elements.title.textContent = report.title || 'Weekly News Research Report';
      elements.period.textContent = report.weekStart + (report.weekEnd ? ' - ' + report.weekEnd : '');
      elements.meta.textContent = '収集済み: ' + (report.coveredThrough || '-') + (report.generatedAt ? ' / 更新: ' + formatDate(report.generatedAt) : '');
      elements.content.innerHTML = markdownToHtml(report.markdown);
      if (state.restoringScroll) {
        pageState?.restoreScroll();
        state.restoringScroll = false;
      }
    } catch (error) {
      elements.title.textContent = 'レポートを表示できません';
      elements.content.innerHTML = '<p class="empty-detail">' + html(error.message) + '</p>';
    }
  }

  async function loadReports() {
    setNotice('');
    try {
      const payload = await requestJson('/api/weekly-reports');
      state.reports = Array.isArray(payload.reports) ? payload.reports : [];
      if (!state.reports.some((report) => report.weekStart === state.selectedWeekStart)) {
        state.selectedWeekStart = state.reports[0]?.weekStart || '';
      }
      persist();
      renderList();
      await loadReport();
    } catch (error) {
      state.reports = [];
      renderList();
      setNotice(error.message);
      elements.title.textContent = 'レポートを表示できません';
      elements.content.innerHTML = '<p class="empty-detail">' + html(error.message) + '</p>';
    }
  }

  elements.reload.addEventListener('click', loadReports);
  loadReports();
})();
