(function () {
  "use strict";

  var state = {
    dates: [],
    date: "",
    articles: [],
    keywords: [],
    filtered: [],
    selectedKey: "",
    selected: null,
    dirty: false,
    busy: false,
    requestToken: 0,
    viewMode: window.localStorage.getItem("article-review-view") || "original"
  };

  var decisionLabels = {
    pending: "未評価",
    approved: "可",
    rejected: "不可",
    needs_refetch: "再取得"
  };

  var statusLabels = {
    verified: "本文あり",
    partial: "一部取得",
    metadata_only: "メタデータのみ",
    unavailable: "取得不能",
    unknown: "状態不明"
  };

  var elements = {};

  function byId(id) {
    return document.getElementById(id);
  }

  function create(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
  }

  function cacheElements() {
    [
      "date-select", "decision-filter", "keyword-filter", "status-filter",
      "search-input", "article-list", "list-empty", "result-count",
      "next-pending-button", "progress-label", "progress-value", "progress-bar",
      "refresh-button", "reader-empty", "reader-content", "article-status",
      "article-publisher", "article-date", "article-title", "article-keywords",
      "original-tab", "markdown-tab", "original-view", "original-frame",
      "original-domain", "original-external-link", "copy-key-button", "markdown-view", "review-form",
      "youtube-details", "youtube-title", "youtube-channel", "youtube-provider", "youtube-description",
      "reason-field", "note-count", "validation-message", "save-button",
      "save-next-button", "save-state", "loading-overlay", "loading-message",
      "toast"
    ].forEach(function (id) {
      elements[id] = byId(id);
    });
  }

  async function api(path, options) {
    var response = await fetch(path, options || {});
    var payload;
    try {
      payload = await response.json();
    } catch (error) {
      throw new Error("サーバーから不正な応答が返されました");
    }
    if (!response.ok) {
      throw new Error(payload.error || "処理に失敗しました");
    }
    return payload;
  }

  function showLoading(message) {
    elements["loading-message"].textContent = message || "読み込んでいます";
    elements["loading-overlay"].classList.remove("hidden");
  }

  function hideLoading() {
    elements["loading-overlay"].classList.add("hidden");
  }

  var toastTimer = 0;
  function toast(message, error) {
    window.clearTimeout(toastTimer);
    elements.toast.textContent = message;
    elements.toast.classList.toggle("error", Boolean(error));
    elements.toast.classList.add("visible");
    toastTimer = window.setTimeout(function () {
      elements.toast.classList.remove("visible");
    }, 2800);
  }

  function normalize(value) {
    return String(value || "").normalize("NFKC").toLocaleLowerCase("ja");
  }

  function formatTimestamp(value) {
    if (!value) {
      return "";
    }
    var date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    return new Intl.DateTimeFormat("ja-JP", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit"
    }).format(date);
  }

  function setOptions(select, rows, firstLabel) {
    var current = select.value;
    select.replaceChildren();
    var first = create("option", "", firstLabel);
    first.value = "";
    select.appendChild(first);
    rows.forEach(function (row) {
      var option = create("option", "", row.label);
      option.value = row.id;
      select.appendChild(option);
    });
    if (Array.from(select.options).some(function (option) { return option.value === current; })) {
      select.value = current;
    }
  }

  async function initialize() {
    cacheElements();
    bindEvents();
    setFormDisabled(true);
    try {
      var data = await api("/api/dates");
      state.dates = data.dates || [];
      elements["date-select"].replaceChildren();
      state.dates.forEach(function (row) {
        var option = create("option", "", row.date + "  (" + row.article_count + "件)");
        option.value = row.date;
        elements["date-select"].appendChild(option);
      });
      if (!state.dates.length) {
        throw new Error("レビューMarkdownが見つかりません");
      }
      var remembered = window.localStorage.getItem("article-review-date");
      state.date = state.dates.some(function (row) { return row.date === remembered; })
        ? remembered
        : data.default_date;
      elements["date-select"].value = state.date;
      await loadDate(false);
    } catch (error) {
      hideLoading();
      toast(error.message, true);
      elements["loading-message"].textContent = error.message;
      elements["loading-overlay"].classList.remove("hidden");
    }
  }

  async function loadDate(refresh) {
    showLoading(refresh ? "Markdownを再読み込みしています" : state.date + " を読み込んでいます");
    state.selectedKey = "";
    state.selected = null;
    state.dirty = false;
    setFormDisabled(true);
    try {
      var suffix = refresh ? "&refresh=1" : "";
      var data = await api("/api/articles?date=" + encodeURIComponent(state.date) + suffix);
      state.articles = data.articles || [];
      state.keywords = data.keywords || [];
      setOptions(elements["keyword-filter"], state.keywords, "すべて");
      updateProgress();
      applyFilters();
      var first = state.filtered[0] || state.articles[0];
      if (first) {
        await selectArticle(first.article_key, true);
      } else {
        showReaderEmpty();
      }
    } catch (error) {
      toast(error.message, true);
      showReaderEmpty();
    } finally {
      hideLoading();
    }
  }

  function applyFilters() {
    var query = normalize(elements["search-input"].value.trim());
    var decision = elements["decision-filter"].value;
    var keyword = elements["keyword-filter"].value;
    var status = elements["status-filter"].value;

    state.filtered = state.articles.filter(function (article) {
      if (decision && article.decision !== decision) {
        return false;
      }
      if (status && article.content_status !== status) {
        return false;
      }
      if (keyword && article.matched_keyword_ids.indexOf(keyword) < 0) {
        return false;
      }
      if (query) {
        var haystack = normalize([
          article.title,
          article.publisher,
          article.source_domain,
          article.overview,
          article.matched_keywords.map(function (item) { return item.label; }).join(" ")
        ].join(" "));
        if (haystack.indexOf(query) < 0) {
          return false;
        }
      }
      return true;
    });
    renderArticleList();
  }

  function updateProgress() {
    var total = state.articles.length;
    var reviewed = state.articles.filter(function (article) {
      return article.decision !== "pending";
    }).length;
    var percentage = total ? Math.round(reviewed * 100 / total) : 0;
    elements["progress-label"].textContent = state.date + " の評価進捗";
    elements["progress-value"].textContent = reviewed.toLocaleString("ja-JP") + " / " + total.toLocaleString("ja-JP") + "件";
    elements["progress-bar"].style.width = percentage + "%";
  }

  function renderArticleList() {
    var list = elements["article-list"];
    list.replaceChildren();
    elements["result-count"].textContent = state.filtered.length.toLocaleString("ja-JP") + "件";
    elements["list-empty"].classList.toggle("hidden", state.filtered.length > 0);

    var fragment = document.createDocumentFragment();
    state.filtered.forEach(function (article) {
      var button = create("button", "article-card");
      button.type = "button";
      button.setAttribute("role", "option");
      button.setAttribute("aria-selected", String(article.article_key === state.selectedKey));
      button.dataset.key = article.article_key;

      var top = create("div", "card-topline");
      top.appendChild(create("span", "mini-badge " + article.decision, decisionLabels[article.decision] || article.decision));
      var dot = create("span", "content-dot " + article.content_status);
      dot.title = statusLabels[article.content_status] || article.content_status;
      top.appendChild(dot);
      var keywordText = article.primary_keywords.length
        ? article.primary_keywords.map(function (item) { return item.label; }).join("、")
        : "キーワード未一致";
      top.appendChild(create("span", "card-meta", keywordText));
      button.appendChild(top);

      button.appendChild(create("p", "card-title", article.title));

      var meta = create("div", "card-meta");
      meta.appendChild(create("span", "", article.publisher || article.source_domain || "配信元不明"));
      if (article.published_at) {
        meta.appendChild(create("span", "", "·"));
        meta.appendChild(create("span", "", formatTimestamp(article.published_at)));
      }
      button.appendChild(meta);
      button.addEventListener("click", function () {
        selectArticle(article.article_key, false);
      });
      fragment.appendChild(button);
    });
    list.appendChild(fragment);
  }

  async function selectArticle(articleKey, force) {
    if (!articleKey || articleKey === state.selectedKey && state.selected) {
      return;
    }
    if (!force && state.dirty && !window.confirm("保存していない変更があります。破棄して別の記事を開きますか？")) {
      return;
    }
    var token = ++state.requestToken;
    state.selectedKey = articleKey;
    state.dirty = false;
    renderArticleList();
    setSaveState("読み込み中", "");
    setFormDisabled(true);
    try {
      var article = await api(
        "/api/article/" + encodeURIComponent(state.date) + "/" + encodeURIComponent(articleKey)
      );
      if (token !== state.requestToken) {
        return;
      }
      state.selected = article;
      renderReader(article);
      populateReviewForm(article);
      setFormDisabled(false);
      setSaveState(article.reviewed_at ? "保存済み" : "未評価", article.reviewed_at ? "saved" : "");
      var selectedCard = elements["article-list"].querySelector('[data-key="' + window.CSS.escape(articleKey) + '"]');
      if (selectedCard) {
        selectedCard.scrollIntoView({ block: "nearest" });
      }
    } catch (error) {
      if (token === state.requestToken) {
        toast(error.message, true);
        setSaveState("読込失敗", "dirty");
      }
    }
  }

  function showReaderEmpty() {
    elements["reader-empty"].classList.remove("hidden");
    elements["reader-content"].classList.add("hidden");
    setFormDisabled(true);
  }

  function safeWebUrl(value) {
    try {
      var url = new URL(String(value || ""));
      return url.protocol === "https:" || url.protocol === "http:" ? url.href : "";
    } catch (error) {
      return "";
    }
  }

  function setViewMode(mode, persist) {
    var originalUrl = state.selected ? safeWebUrl(state.selected.embed_url || state.selected.resolved_url || state.selected.original_url) : "";
    var effectiveMode = mode === "original" && !originalUrl ? "markdown" : mode;
    var showOriginal = effectiveMode === "original";

    if (originalUrl) {
      state.viewMode = effectiveMode;
    }
    if (persist && originalUrl) {
      window.localStorage.setItem("article-review-view", effectiveMode);
    }

    elements["original-view"].classList.toggle("hidden", !showOriginal);
    elements["markdown-view"].classList.toggle("hidden", showOriginal);
    elements["original-tab"].classList.toggle("active", showOriginal);
    elements["markdown-tab"].classList.toggle("active", !showOriginal);
    elements["original-tab"].setAttribute("aria-selected", String(showOriginal));
    elements["markdown-tab"].setAttribute("aria-selected", String(!showOriginal));
    elements["original-tab"].disabled = !originalUrl;

    var frame = elements["original-frame"];
    if (!originalUrl) {
      frame.src = "about:blank";
      frame.dataset.loadedUrl = "";
    } else if (showOriginal && frame.dataset.loadedUrl !== originalUrl) {
      frame.src = originalUrl;
      frame.dataset.loadedUrl = originalUrl;
    } else if (!showOriginal && frame.dataset.loadedUrl &&
        frame.dataset.loadedUrl !== originalUrl) {
      frame.src = "about:blank";
      frame.dataset.loadedUrl = "";
    }
  }

  function renderReader(article) {
    elements["reader-empty"].classList.add("hidden");
    elements["reader-content"].classList.remove("hidden");
    var status = elements["article-status"];
    status.className = "status-badge " + article.content_status;
    status.textContent = statusLabels[article.content_status] || article.content_status;
    elements["article-publisher"].textContent = article.publisher || article.source_domain || "配信元不明";
    elements["article-date"].textContent = formatTimestamp(article.published_at);
    elements["article-title"].textContent = article.title;

    var keywordRow = elements["article-keywords"];
    keywordRow.replaceChildren();
    if (article.matched_keywords.length) {
      article.matched_keywords.forEach(function (keyword) {
        keywordRow.appendChild(create("span", "keyword-chip", keyword.label));
      });
    } else {
      keywordRow.appendChild(create("span", "keyword-chip unmatched", "キーワード未一致"));
    }

    var externalUrl = safeWebUrl(article.resolved_url || article.original_url);
    var embedUrl = safeWebUrl(article.embed_url || externalUrl);
    var video = article.video_metadata || {};
    elements["original-view"].classList.toggle("youtube-mode", Boolean(article.is_youtube));
    elements["original-frame"].title = article.is_youtube ? "YouTube動画" : "元記事";
    elements["original-external-link"].href = externalUrl || "#";
    elements["original-external-link"].classList.toggle("hidden", !externalUrl);
    elements["original-domain"].textContent = article.is_youtube
      ? "YouTube"
      : (externalUrl ? new URL(externalUrl).hostname : "元記事URLなし");
    elements["youtube-details"].classList.toggle("hidden", !article.is_youtube);
    elements["youtube-title"].textContent = video.title || article.title || "タイトル不明";
    elements["youtube-channel"].textContent = video.channel || "情報なし";
    elements["youtube-provider"].textContent = video.provider || "YouTube";
    elements["youtube-description"].textContent = video.description || "保存された概要はありません。";
    renderMarkdown(article.body_markdown, elements["markdown-view"]);
    setViewMode(embedUrl ? state.viewMode : "markdown", false);
    document.querySelector(".reader-panel").scrollTop = 0;
  }

  function appendInline(parent, text) {
    var pattern = /(\x60[^\x60]+\x60|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)|\*\*([^*]+)\*\*)/g;
    var last = 0;
    var match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > last) {
        parent.appendChild(document.createTextNode(text.slice(last, match.index)));
      }
      if (match[0].charAt(0) === String.fromCharCode(96)) {
        parent.appendChild(create("code", "", match[0].slice(1, -1)));
      } else if (match[2] && match[3]) {
        var link = create("a", "", match[2]);
        link.href = match[3];
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        parent.appendChild(link);
      } else if (match[4]) {
        parent.appendChild(create("strong", "", match[4]));
      }
      last = pattern.lastIndex;
    }
    if (last < text.length) {
      parent.appendChild(document.createTextNode(text.slice(last)));
    }
  }

  function tableCells(line) {
    var trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
    return trimmed.split("|").map(function (cell) { return cell.trim(); });
  }

  function isTableDivider(line) {
    var cells = tableCells(line);
    return cells.length > 0 && cells.every(function (cell) {
      return /^:?-{3,}:?$/.test(cell);
    });
  }

  function isBlockStart(lines, index) {
    var line = lines[index] || "";
    if (!line.trim()) {
      return true;
    }
    if (/^#{1,3}\s+/.test(line) || /^>\s?/.test(line) || /^\s*[-*+]\s+/.test(line)) {
      return true;
    }
    if (/^\s*\d+\.\s+/.test(line) || /^~~~|^\x60\x60\x60/.test(line)) {
      return true;
    }
    return line.indexOf("|") >= 0 && index + 1 < lines.length && isTableDivider(lines[index + 1]);
  }

  function renderMarkdown(markdown, root) {
    root.replaceChildren();
    var lines = String(markdown || "").split(/\r?\n/);
    var index = 0;
    while (index < lines.length) {
      var line = lines[index];
      if (!line.trim() || /^<!--/.test(line.trim())) {
        index += 1;
        continue;
      }

      var heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        var headingNode = create("h" + heading[1].length);
        appendInline(headingNode, heading[2]);
        root.appendChild(headingNode);
        index += 1;
        continue;
      }

      if (/^~~~|^\x60\x60\x60/.test(line)) {
        var fence = line.slice(0, 3);
        var codeLines = [];
        index += 1;
        while (index < lines.length && lines[index].slice(0, 3) !== fence) {
          codeLines.push(lines[index]);
          index += 1;
        }
        index += 1;
        var pre = create("pre");
        pre.appendChild(create("code", "", codeLines.join("\n")));
        root.appendChild(pre);
        continue;
      }

      if (line.indexOf("|") >= 0 && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
        var wrap = create("div", "markdown-table-wrap");
        var table = create("table");
        var thead = create("thead");
        var headRow = create("tr");
        tableCells(line).forEach(function (cell) {
          var th = create("th");
          appendInline(th, cell);
          headRow.appendChild(th);
        });
        thead.appendChild(headRow);
        table.appendChild(thead);
        var tbody = create("tbody");
        index += 2;
        while (index < lines.length && lines[index].indexOf("|") >= 0 && lines[index].trim()) {
          var row = create("tr");
          tableCells(lines[index]).forEach(function (cell) {
            var td = create("td");
            appendInline(td, cell);
            row.appendChild(td);
          });
          tbody.appendChild(row);
          index += 1;
        }
        table.appendChild(tbody);
        wrap.appendChild(table);
        root.appendChild(wrap);
        continue;
      }

      if (/^>\s?/.test(line)) {
        var quote = create("blockquote");
        var quoteLines = [];
        while (index < lines.length && /^>\s?/.test(lines[index])) {
          quoteLines.push(lines[index].replace(/^>\s?/, ""));
          index += 1;
        }
        appendInline(quote, quoteLines.join(" "));
        root.appendChild(quote);
        continue;
      }

      var unordered = /^\s*[-*+]\s+/.test(line);
      var ordered = /^\s*\d+\.\s+/.test(line);
      if (unordered || ordered) {
        var list = create(ordered ? "ol" : "ul");
        var listPattern = ordered ? /^\s*\d+\.\s+/ : /^\s*[-*+]\s+/;
        while (index < lines.length && listPattern.test(lines[index])) {
          var item = create("li");
          appendInline(item, lines[index].replace(listPattern, ""));
          list.appendChild(item);
          index += 1;
        }
        root.appendChild(list);
        continue;
      }

      var paragraphLines = [line.trim()];
      index += 1;
      while (index < lines.length && !isBlockStart(lines, index)) {
        paragraphLines.push(lines[index].trim());
        index += 1;
      }
      var paragraph = create("p");
      appendInline(paragraph, paragraphLines.join(" "));
      root.appendChild(paragraph);
    }
  }

  function populateReviewForm(article) {
    var form = elements["review-form"];
    var decisionInput = form.querySelector('input[name="decision"][value="' + article.decision + '"]');
    if (decisionInput) {
      decisionInput.checked = true;
    }
    form.elements.keyword_check.value = article.keyword_check;
    form.elements.content_check.value = article.content_check;
    form.elements.reason_code.value = article.reason_code;
    form.elements.note.value = article.note || "";
    elements["note-count"].textContent = String((article.note || "").length);
    updateReasonVisibility();
    elements["validation-message"].classList.add("hidden");
    state.dirty = false;
  }

  function setFormDisabled(disabled) {
    elements["review-form"].setAttribute("aria-disabled", String(disabled));
    Array.from(elements["review-form"].elements).forEach(function (field) {
      field.disabled = disabled;
    });
  }

  function setSaveState(text, className) {
    elements["save-state"].textContent = text;
    elements["save-state"].className = "save-state" + (className ? " " + className : "");
  }

  function markDirty() {
    if (!state.selected) {
      return;
    }
    state.dirty = true;
    setSaveState("未保存", "dirty");
    elements["validation-message"].classList.add("hidden");
  }

  function updateReasonVisibility() {
    var checked = elements["review-form"].querySelector('input[name="decision"]:checked');
    elements["reason-field"].classList.toggle("hidden", !checked || checked.value !== "rejected");
  }

  function reviewPayload() {
    var form = elements["review-form"];
    var checked = form.querySelector('input[name="decision"]:checked');
    return {
      decision: checked ? checked.value : "pending",
      keyword_check: form.elements.keyword_check.value,
      content_check: form.elements.content_check.value,
      reason_code: checked && checked.value === "rejected" ? form.elements.reason_code.value : "",
      note: form.elements.note.value,
      expected_mtime_ns: state.selected.mtime_ns
    };
  }

  function validateReview(payload) {
    if (payload.decision === "rejected" && !payload.reason_code) {
      return "不可にする場合は理由を選択してください。";
    }
    return "";
  }

  async function saveReview(goNext) {
    if (!state.selected || state.busy) {
      return;
    }
    var payload = reviewPayload();
    var validation = validateReview(payload);
    if (validation) {
      elements["validation-message"].textContent = validation;
      elements["validation-message"].classList.remove("hidden");
      return;
    }

    var currentKey = state.selectedKey;
    var currentIndex = state.filtered.findIndex(function (article) {
      return article.article_key === currentKey;
    });
    var nextCandidate = state.filtered[currentIndex + 1] || state.filtered[0];
    var nextKey = nextCandidate && nextCandidate.article_key !== currentKey
      ? nextCandidate.article_key
      : "";

    state.busy = true;
    elements["save-button"].disabled = true;
    elements["save-next-button"].disabled = true;
    setSaveState("保存中", "dirty");
    try {
      var result = await api(
        "/api/article/" + encodeURIComponent(state.date) + "/" + encodeURIComponent(currentKey) + "/review",
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        }
      );
      state.selected = result.article;
      var row = state.articles.find(function (article) {
        return article.article_key === currentKey;
      });
      if (row) {
        [
          "decision", "keyword_check", "content_check", "reason_code",
          "note", "reviewed_at", "mtime_ns"
        ].forEach(function (key) {
          row[key] = result.article[key];
        });
      }
      state.dirty = false;
      setSaveState("保存済み", "saved");
      toast("評価をMarkdownへ保存しました");
      updateProgress();
      applyFilters();

      if (goNext) {
        var available = state.filtered.some(function (article) {
          return article.article_key === nextKey;
        });
        var target = available ? nextKey : (state.filtered[0] ? state.filtered[0].article_key : "");
        if (target && target !== currentKey) {
          await selectArticle(target, true);
        } else if (!state.filtered.length) {
          state.selected = null;
          state.selectedKey = "";
          renderArticleList();
          showReaderEmpty();
          toast("この条件のレビューが完了しました");
        }
      } else {
        renderArticleList();
        populateReviewForm(result.article);
        setSaveState("保存済み", "saved");
      }
    } catch (error) {
      elements["validation-message"].textContent = error.message;
      elements["validation-message"].classList.remove("hidden");
      setSaveState("保存失敗", "dirty");
      toast(error.message, true);
    } finally {
      state.busy = false;
      elements["save-button"].disabled = !state.selected;
      elements["save-next-button"].disabled = !state.selected;
    }
  }

  function moveSelection(direction) {
    if (!state.filtered.length) {
      return;
    }
    var index = state.filtered.findIndex(function (article) {
      return article.article_key === state.selectedKey;
    });
    if (index < 0) {
      index = direction > 0 ? -1 : 0;
    }
    var nextIndex = Math.max(0, Math.min(state.filtered.length - 1, index + direction));
    selectArticle(state.filtered[nextIndex].article_key, false);
  }

  function chooseDecision(value) {
    if (!state.selected) {
      return;
    }
    var input = elements["review-form"].querySelector('input[name="decision"][value="' + value + '"]');
    if (input) {
      input.checked = true;
      updateReasonVisibility();
      markDirty();
    }
  }

  function bindEvents() {
    elements["date-select"].addEventListener("change", async function () {
      if (state.dirty && !window.confirm("保存していない変更があります。破棄して日付を変更しますか？")) {
        elements["date-select"].value = state.date;
        return;
      }
      state.date = elements["date-select"].value;
      window.localStorage.setItem("article-review-date", state.date);
      await loadDate(false);
    });

    elements["decision-filter"].addEventListener("change", applyFilters);
    elements["keyword-filter"].addEventListener("change", applyFilters);
    elements["status-filter"].addEventListener("change", applyFilters);
    elements["search-input"].addEventListener("input", applyFilters);
    elements["refresh-button"].addEventListener("click", function () {
      if (!state.dirty || window.confirm("保存していない変更を破棄して再読み込みしますか？")) {
        loadDate(true);
      }
    });
    elements["next-pending-button"].addEventListener("click", function () {
      var pending = state.articles.find(function (article) {
        return article.decision === "pending" && article.article_key !== state.selectedKey;
      });
      if (pending) {
        elements["decision-filter"].value = "pending";
        applyFilters();
        selectArticle(pending.article_key, false);
      } else {
        toast("未評価の記事はありません");
      }
    });

    elements["review-form"].addEventListener("change", function () {
      updateReasonVisibility();
      markDirty();
    });
    elements["review-form"].addEventListener("input", function () {
      elements["note-count"].textContent = String(elements["review-form"].elements.note.value.length);
      markDirty();
    });
    elements["review-form"].addEventListener("submit", function (event) {
      event.preventDefault();
      saveReview(false);
    });
    elements["save-next-button"].addEventListener("click", function () {
      saveReview(true);
    });
    elements["original-tab"].addEventListener("click", function () {
      setViewMode("original", true);
    });
    elements["markdown-tab"].addEventListener("click", function () {
      setViewMode("markdown", true);
    });
    elements["copy-key-button"].addEventListener("click", async function () {
      if (!state.selectedKey) {
        return;
      }
      try {
        await navigator.clipboard.writeText(state.selectedKey);
        toast("記事キーをコピーしました");
      } catch (error) {
        toast("コピーできませんでした", true);
      }
    });

    window.addEventListener("beforeunload", function (event) {
      if (state.dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    });

    document.addEventListener("keydown", function (event) {
      var target = event.target;
      var typing = target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target instanceof HTMLSelectElement;
      if (event.key === "/" && !typing) {
        event.preventDefault();
        elements["search-input"].focus();
        return;
      }
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        saveReview(true);
        return;
      }
      if (typing || event.ctrlKey || event.metaKey || event.altKey) {
        return;
      }
      if (event.key.toLowerCase() === "j") {
        moveSelection(1);
      } else if (event.key.toLowerCase() === "k") {
        moveSelection(-1);
      } else if (event.key.toLowerCase() === "a") {
        chooseDecision("approved");
      } else if (event.key.toLowerCase() === "r") {
        chooseDecision("rejected");
      }
    });
  }

  document.addEventListener("DOMContentLoaded", initialize);
}());
