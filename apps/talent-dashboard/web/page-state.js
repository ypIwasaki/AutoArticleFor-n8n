(() => {
  const storageKey = 'talent-index:page-state:v1:' + window.location.pathname + window.location.search;

  function read() {
    try {
      const value = window.sessionStorage.getItem(storageKey);
      return value ? JSON.parse(value) : {};
    } catch {
      return {};
    }
  }

  function write(value) {
    try {
      window.sessionStorage.setItem(storageKey, JSON.stringify(value));
    } catch {
      // State restoration is an enhancement; private browsing can disable storage.
    }
  }

  const api = {
    get(name, fallback = null) {
      const value = read()[name];
      return value === undefined ? fallback : value;
    },
    set(name, value) {
      const state = read();
      state[name] = value;
      write(state);
    },
    clear(name) {
      const state = read();
      delete state[name];
      write(state);
    },
    restoreScroll() {
      const position = Number(read().scrollY);
      if (!Number.isFinite(position) || position <= 0) return;
      window.requestAnimationFrame(() => {
        window.requestAnimationFrame(() => window.scrollTo({ top: position, behavior: 'auto' }));
      });
    },
  };

  window.TalentIndexPageState = api;
  window.addEventListener('pagehide', () => api.set('scrollY', Math.round(window.scrollY || window.pageYOffset || 0)));
})();
