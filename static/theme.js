(function () {
  'use strict';

  var KEY = 'schedul-theme';
  var root = document.documentElement;

  function apply(theme) {
    root.setAttribute('data-theme', theme === 'dark' ? 'dark' : 'light');
  }

  var stored = null;
  try {
    stored = localStorage.getItem(KEY);
  } catch (err) {
    stored = null;
  }
  var systemDark = false;
  try {
    systemDark = !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  } catch (err) {
    systemDark = false;
  }
  apply(stored === 'dark' || stored === 'light' ? stored : systemDark ? 'dark' : 'light');

  document.addEventListener('DOMContentLoaded', function () {
    var button = document.getElementById('theme-toggle');
    if (!button) {
      return;
    }
    button.addEventListener('click', function () {
      var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      apply(next);
      try {
        localStorage.setItem(KEY, next);
      } catch (err) {
        /* 隐私模式下无法存储时忽略 */
      }
    });
  });
})();
