// Переключатель темы и кнопки «Копировать». Это весь JavaScript сайта.
// Без JavaScript всё остальное работает: элементы управления скрыты CSS (см. site.css, правило html:not(.js)).
(function () {
  'use strict';

  var THEME_KEY = 'pb-theme';
  var root = document.documentElement;

  function savedTheme() {
    try {
      return localStorage.getItem(THEME_KEY) || 'auto';
    } catch (e) {
      return 'auto';
    }
  }

  function saveTheme(value) {
    try {
      localStorage.setItem(THEME_KEY, value);
    } catch (e) {
      // Без хранилища выбор действует до перезагрузки страницы.
    }
  }

  function applyTheme(value) {
    if (value === 'light' || value === 'dark') {
      root.setAttribute('data-theme', value);
    } else {
      root.removeAttribute('data-theme');
    }
  }

  function initThemeSwitch() {
    var box = document.querySelector('[data-theme-switch]');
    if (!box) return;

    var buttons = box.querySelectorAll('[data-theme-value]');
    var current = savedTheme();

    function markPressed() {
      for (var i = 0; i < buttons.length; i++) {
        var value = buttons[i].getAttribute('data-theme-value');
        buttons[i].setAttribute('aria-pressed', value === current ? 'true' : 'false');
      }
    }

    box.addEventListener('click', function (event) {
      var button = event.target.closest('[data-theme-value]');
      if (!button || !box.contains(button)) return;
      current = button.getAttribute('data-theme-value');
      saveTheme(current);
      applyTheme(current);
      markPressed();
    });

    markPressed();
  }

  function copyWithFallback(text, element) {
    function selectionCopy() {
      try {
        var range = document.createRange();
        range.selectNodeContents(element);
        var selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        var ok = document.execCommand('copy');
        selection.removeAllRanges();
        return ok;
      } catch (e) {
        return false;
      }
    }

    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(
        function () { return true; },
        function () { return selectionCopy(); }
      );
    }
    return Promise.resolve(selectionCopy());
  }

  function onCopyClick(event) {
    var button = event.currentTarget;
    var code = document.getElementById(button.getAttribute('data-copy-target'));
    var block = button.closest('.pb-code-block');
    var status = block ? block.querySelector('[data-copy-status]') : null;
    if (!code) return;

    var text = code.textContent.replace(/\n$/, '');
    var label = button.getAttribute('data-label');

    copyWithFallback(text, code).then(function (ok) {
      button.textContent = ok ? 'Скопировано' : 'Не получилось';
      button.setAttribute('data-state', ok ? 'copied' : 'failed');
      if (status) status.textContent = ok ? 'Команда скопирована' : 'Не удалось скопировать, выделите текст вручную';
      window.setTimeout(function () {
        button.textContent = label;
        button.removeAttribute('data-state');
        if (status) status.textContent = '';
      }, 2000);
    });
  }

  function initCopyButtons() {
    var buttons = document.querySelectorAll('[data-copy-target]');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener('click', onCopyClick);
    }
  }

  initThemeSwitch();
  initCopyButtons();
})();
