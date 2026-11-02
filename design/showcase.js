// Interactions of the showcase page. No external requests, no dependencies.
(() => {
  const root = document.documentElement;
  const KEY = 'pb-theme';

  const store = {
    get() {
      try {
        return localStorage.getItem(KEY);
      } catch {
        return null;
      }
    },
    set(value) {
      try {
        if (value) localStorage.setItem(KEY, value);
        else localStorage.removeItem(KEY);
      } catch {
        // storage may be blocked: the choice then lasts until the page is closed
      }
    },
  };

  // Theme: auto (system), light or dark ------------------------------------

  const themeButtons = document.querySelectorAll('[data-theme-choice]');
  function applyTheme(choice) {
    if (choice === 'light' || choice === 'dark') root.dataset.theme = choice;
    else delete root.dataset.theme;
    const active = choice === 'light' || choice === 'dark' ? choice : 'auto';
    themeButtons.forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.themeChoice === active)));
  }
  applyTheme(store.get());
  themeButtons.forEach((button) =>
    button.addEventListener('click', () => {
      const choice = button.dataset.themeChoice;
      store.set(choice === 'auto' ? null : choice);
      applyTheme(choice);
    }),
  );

  // Copy buttons -------------------------------------------------------------

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch {
        // permission or focus problem: try the fallback below
      }
    }
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.append(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    if (!ok) throw new Error('copy failed');
  }

  document.querySelectorAll('[data-copy]').forEach((button) => {
    const label = button.querySelector('[data-copy-label]');
    const original = label.textContent;
    const status = button.parentElement.querySelector('[data-copy-status]') ?? button.closest('.pb-field')?.querySelector('[data-copy-status]');
    let timer;

    button.addEventListener('click', async () => {
      const target = document.querySelector(button.dataset.copy);
      const text = 'value' in target ? target.value : target.textContent;
      let message;
      try {
        await copyText(text);
        button.dataset.state = 'copied';
        message = 'Скопировано';
      } catch {
        button.dataset.state = 'failed';
        message = 'Не удалось скопировать';
      }
      label.textContent = message;
      if (status) status.textContent = message;
      clearTimeout(timer);
      timer = setTimeout(() => {
        delete button.dataset.state;
        label.textContent = original;
        if (status) status.textContent = '';
      }, 2000);
    });
  });

  // Replay the logo animation -----------------------------------------------

  function replay(svg) {
    svg.classList.remove('pb-logo--animate');
    void svg.getBoundingClientRect(); // restart the animations
    svg.classList.add('pb-logo--animate');
  }
  document.querySelectorAll('[data-replay]').forEach((button) =>
    button.addEventListener('click', () => {
      const svg = document.querySelector(button.dataset.replay);
      if (svg) replay(svg);
    }),
  );

  // The logo in the "Анимации" section plays when it scrolls into view.
  const motionLogo = document.querySelector('#motion-logo');
  if (motionLogo && 'IntersectionObserver' in window) {
    motionLogo.classList.remove('pb-logo--animate');
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          replay(motionLogo);
          observer.disconnect();
        }
      },
      { threshold: 0.6 },
    );
    observer.observe(motionLogo);
  }

  // Easing demo -------------------------------------------------------------

  const motionToggle = document.querySelector('[data-motion-toggle]');
  const motionGrid = document.querySelector('.sc-motion-grid');
  if (motionToggle && motionGrid) {
    motionToggle.addEventListener('click', () => {
      const running = motionGrid.classList.toggle('is-run');
      motionToggle.textContent = running ? 'Вернуть' : 'Запустить';
    });
  }

  // Busy button demo ----------------------------------------------------------

  const busyButton = document.querySelector('[data-busy-demo]');
  if (busyButton) {
    const busyLabel = busyButton.querySelector('[data-busy-label]');
    busyButton.addEventListener('click', () => {
      busyButton.setAttribute('aria-busy', 'true');
      busyLabel.textContent = 'Отправляем…';
      setTimeout(() => {
        busyButton.removeAttribute('aria-busy');
        busyLabel.textContent = 'Отправить';
      }, 2000);
    });
  }
})();
