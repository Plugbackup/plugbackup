// Тема до первой отрисовки: читаем сохранённый выбор и ставим data-theme на <html>.
// Скрипт короткий и подключается в <head> без defer, чтобы не было вспышки не той темы.
// Класс js на <html> показывает элементы управления (переключатель темы, кнопки копирования) сразу,
// без перестройки вёрстки после загрузки. Без JavaScript эти элементы скрыты, тема следует за системной.
(function () {
  'use strict';
  document.documentElement.classList.add('js');
  try {
    var saved = localStorage.getItem('pb-theme');
    if (saved === 'light' || saved === 'dark') {
      document.documentElement.setAttribute('data-theme', saved);
    }
  } catch (e) {
    // Хранилище недоступно (приватный режим, блокировка): остаётся системная тема.
  }
})();
