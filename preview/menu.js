/* ==========================================================================
   menu.js — 手机端导航（抽屉 + 手风琴二级菜单）
   纯原生 JavaScript，无第三方依赖。

   2026-10-19 重写，原因（用户反馈）：
     ① 导航条无法滑动 —— 原来 .nav-links 用 max-height + overflow:hidden
        做展开动画，这个组合下内容超出就被裁掉，且完全滚不动。
     ② 二级菜单直接展开 —— 原来 .nav-menu 在手机上被设成 position:static
        + opacity:1，13 个子项一次平铺，主次不分、面板过长。

   现在的行为（参考百科类站点的目录）：
     - 抽屉从导航条下方滑下，内容超出时可滚动（CSS 用 flex + overflow-y）
     - 每个有子项的导航，前面多一个 ▾ 按钮，点了才展开它自己的子菜单
     - 同时只展开一个（手风琴），避免又变成一长条
     - 抽屉打开时锁住 body 滚动；关闭/跳转/放大视口时自动复位
     - 键盘：Esc 关闭并还焦点；Tab 顺序天然正确（按钮在 DOM 里）
   ========================================================================== */
(function () {
  'use strict';

  // 与 styles.css 的 859.98px 断点对应
  var BREAKPOINT = 860;

  function initNav() {
    var toggle = document.querySelector('[data-nav-toggle]');
    var links = document.getElementById('nav-links');
    if (!toggle || !links) return;

    var items = Array.prototype.slice.call(
      links.querySelectorAll('.nav-item.has-menu')
    );

    /* ---------- 1. 给每个有子项的导航注入展开按钮 ---------- */
    items.forEach(function (item) {
      var menu = item.querySelector('.nav-menu');
      var mainLink = item.querySelector(':scope > a');
      if (!menu || !mainLink) return;
      if (item.querySelector('.nav-sub-toggle')) return;  // 幂等

      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'nav-sub-toggle';
      btn.setAttribute('aria-expanded', 'false');
      btn.setAttribute('aria-label', '展开「' + mainLink.textContent.trim() + '」子菜单');

      var menuId = 'nav-sub-' + Math.random().toString(36).slice(2, 8);
      menu.id = menu.id || menuId;
      btn.setAttribute('aria-controls', menu.id);

      // 按钮插在主链接之后
      mainLink.parentNode.insertBefore(btn, mainLink.nextSibling);

      btn.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        var willOpen = !item.classList.contains('is-expanded');
        // 手风琴：先收起其他
        items.forEach(function (other) {
          if (other !== item) collapse(other);
        });
        if (willOpen) expand(item); else collapse(item);
      });
    });

    function expand(item) {
      item.classList.add('is-expanded');
      var b = item.querySelector('.nav-sub-toggle');
      if (b) b.setAttribute('aria-expanded', 'true');
    }
    function collapse(item) {
      item.classList.remove('is-expanded');
      var b = item.querySelector('.nav-sub-toggle');
      if (b) b.setAttribute('aria-expanded', 'false');
    }
    function collapseAll() {
      items.forEach(collapse);
    }

    /* ---------- 2. 抽屉开关 ---------- */
    function setOpen(open) {
      links.classList.toggle('is-open', open);
      toggle.setAttribute('aria-expanded', String(open));
      links.setAttribute('aria-hidden', String(!open));
      // 锁 body 滚动，避免背景跟着动
      document.body.classList.toggle('nav-open', open);
      if (!open) {
        collapseAll();
        links.scrollTop = 0;
      }
    }

    function isOpen() {
      return toggle.getAttribute('aria-expanded') === 'true';
    }

    toggle.addEventListener('click', function (e) {
      e.stopPropagation();
      setOpen(!isOpen());
    });

    // 点击抽屉以外 → 收起
    document.addEventListener('click', function (e) {
      if (!isOpen()) return;
      if (links.contains(e.target) || toggle.contains(e.target)) return;
      setOpen(false);
    });

    // Esc → 收起并还焦点
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && isOpen()) {
        setOpen(false);
        toggle.focus();
      }
    });

    // 点了子项里的链接 → 收起抽屉（要跳转了，别挡着）
    links.addEventListener('click', function (e) {
      var a = e.target.closest && e.target.closest('a');
      if (a && isOpen()) setOpen(false);
    });

    // 视口放大到桌面宽度 → 复位
    function syncToViewport() {
      if (window.innerWidth >= BREAKPOINT) setOpen(false);
    }
    window.addEventListener('resize', syncToViewport);
    window.addEventListener('orientationchange', syncToViewport);

    // 初始状态
    setOpen(false);
    syncToViewport();
  }

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  ready(initNav);
})();
