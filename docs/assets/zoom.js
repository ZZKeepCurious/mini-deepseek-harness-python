// Mermaid 渲染 + 点击放大（绕开 Material 的 closed shadow DOM）
//
// Material for MkDocs 会把 mermaid 渲染进 closed shadow DOM，外部 JS 完全
// 无法访问内部节点（shadowRoot 为 null，composedPath 也不暴露内部），
// 任何 viewer 库都无法拿到 SVG。因此这里自己接管渲染：
//   1. 扫描 <pre class="mermaid-source">（superfences custom_fence 输出）
//   2. 动态加载 mermaid.js，render 进普通 DOM 的 <div class="mermaid">
//   3. 绑定点击放大（滚轮缩放 + 拖拽平移 + 双击复位 + Esc 关闭）
(function () {
  var modal = document.getElementById('zoom-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'zoom-modal';
    modal.innerHTML =
      '<div class="zoom-pane"><div class="zoom-box"><div class="zoom-stage"></div></div></div>' +
      '<div class="zoom-label">100%</div><div class="zoom-hint">滚轮缩放 · 拖拽平移 · 双击复位 · 单击 / Esc 关闭</div>';
    document.body.appendChild(modal);
  }
  var box = modal.querySelector('.zoom-box');
  var stage = modal.querySelector('.zoom-stage');
  var label = modal.querySelector('.zoom-label');
  var svgEl = null, scale = 1, tx = 0, ty = 0;
  var dragging = false, moved = false, suppress = false, downX = 0, downY = 0;
  var mermaidPromise = null, renderCounter = 0;

  function apply() {
    if (!svgEl) return;
    svgEl.style.transform = 'translate(' + tx + 'px, ' + ty + 'px) scale(' + scale + ')';
    label.textContent = Math.round(scale * 100) + '%';
  }
  function close() {
    modal.classList.remove('show');
    svgEl = null;
  }
  function viewBoxBounds(svg) {
    var vb = svg.getAttribute && svg.getAttribute('viewBox');
    if (!vb) return null;
    var p = vb.trim().split(/[\s,]+/).map(Number);
    if (p.length !== 4 || isNaN(p[0]) || isNaN(p[1]) || isNaN(p[2]) || isNaN(p[3])) return null;
    if (p[2] <= 0 || p[3] <= 0) return null;
    return { x: p[0], y: p[1], w: p[2], h: p[3] };
  }
  function bindZoom(container) {
    if (container.dataset.zoomBound) return;
    container.dataset.zoomBound = '1';
    container.addEventListener('click', function (e) {
      var svg = null;
      if (e.target && e.target.closest) {
        svg = e.target.closest('svg');
      }
      if (!svg && e.composedPath) {
        for (var i = 0; i < e.composedPath().length; i++) {
          if (e.composedPath()[i] && e.composedPath()[i].tagName === 'SVG') {
            svg = e.composedPath()[i];
            break;
          }
        }
      }
      if (!svg || !container.contains(svg)) return;
      var clone = svg.cloneNode(true);
      var bounds = viewBoxBounds(svg);
      if (!bounds) {
        try {
          var bb = svg.getBBox();
          if (bb.width > 0 && bb.height > 0) bounds = { x: bb.x, y: bb.y, w: bb.width, h: bb.height };
        } catch (err) {}
      }
      if (!bounds) {
        var g = svg.querySelector('g');
        if (g) {
          var gr = g.getBoundingClientRect();
          var sr = svg.getBoundingClientRect();
          if (gr.width > 0 && gr.height > 0) bounds = { x: gr.left - sr.left, y: gr.top - sr.top, w: gr.width, h: gr.height };
        }
      }
      if (!bounds) {
        var rr = svg.getBoundingClientRect();
        if (rr.width > 0 && rr.height > 0) bounds = { x: 0, y: 0, w: rr.width, h: rr.height };
      }
      if (!clone.getAttribute('viewBox') && bounds) {
        clone.setAttribute('viewBox', bounds.x + ' ' + bounds.y + ' ' + bounds.w + ' ' + bounds.h);
      }
      clone.removeAttribute('width');
      clone.removeAttribute('height');
      clone.style.maxWidth = 'none';
      clone.style.height = 'auto';
      if (bounds) {
        var availW = Math.min(window.innerWidth * 0.94, 1600);
        var availH = window.innerHeight * 0.78;
        var fit = Math.min(availW / bounds.w, availH / bounds.h);
        clone.style.width = Math.max(1, Math.round(bounds.w * fit)) + 'px';
      }
      clone.style.transform = '';
      stage.innerHTML = '';
      stage.appendChild(clone);
      svgEl = clone;
      scale = 1; tx = 0; ty = 0;
      apply();
      modal.classList.add('show');
    });
  }

  // ---- 自己渲染 mermaid 到普通 DOM（绕开 Material 的 closed shadow）----
  function loadMermaid() {
    if (!mermaidPromise) {
      mermaidPromise = new Promise(function (resolve, reject) {
        if (window.mermaid) { resolve(window.mermaid); return; }
        var s = document.createElement('script');
        s.src = 'https://unpkg.com/mermaid@11/dist/mermaid.min.js';
        s.onload = function () {
          window.mermaid.initialize({
            startOnLoad: false,
            theme: getMermaidTheme()
          });
          resolve(window.mermaid);
        };
        s.onerror = function () { reject(new Error('mermaid CDN 加载失败')); };
        document.head.appendChild(s);
      });
    }
    return mermaidPromise;
  }
  function getMermaidTheme() {
    var scheme = document.body && document.body.getAttribute('data-md-color-scheme');
    return scheme === 'slate' ? 'dark' : 'default';
  }
  function renderOne(pre) {
    if (pre.dataset.rendering) return;
    pre.dataset.rendering = '1';
    loadMermaid().then(function (mermaid) {
      var id = '__mermaid_' + (renderCounter++);
      var src = pre.textContent;
      return mermaid.render(id, src).then(function (result) {
        var div = document.createElement('div');
        div.className = 'mermaid';
        div.innerHTML = result.svg;
        if (result.bindFunctions) {
          try { result.bindFunctions(div); } catch (e) {}
        }
        pre.replaceWith(div);
        bindZoom(div);
      });
    }).catch(function (err) {
      pre.removeAttribute('data-rendering');
      console.warn('[zoom.js] mermaid render failed:', err);
    });
  }
  function scan() {
    document.querySelectorAll('pre.mermaid-source').forEach(renderOne);
    document.querySelectorAll('.mermaid').forEach(bindZoom);
  }
  function init() {
    scan();
    new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
  }

  // 暗色/亮色切换时重新初始化 mermaid 主题（已渲染的图不重画，简单处理）
  var lastScheme = getMermaidTheme();
  setInterval(function () {
    var s = getMermaidTheme();
    if (s !== lastScheme) {
      lastScheme = s;
      if (window.mermaid) window.mermaid.initialize({ startOnLoad: false, theme: s });
    }
  }, 1000);

  stage.addEventListener('wheel', function (e) {
    e.preventDefault();
    var factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    scale = Math.min(6, Math.max(0.5, scale * factor));
    apply();
  }, { passive: false });
  stage.addEventListener('dblclick', function () {
    scale = 1; tx = 0; ty = 0;
    apply();
  });
  stage.addEventListener('mousedown', function (e) {
    if (e.button !== 0) return;
    dragging = true; moved = false; suppress = false;
    downX = e.clientX; downY = e.clientY;
    stage.classList.add('dragging');
    e.preventDefault();
  });
  window.addEventListener('mousemove', function (e) {
    if (!dragging) return;
    var dx = e.clientX - downX, dy = e.clientY - downY;
    if (!moved && Math.abs(dx) + Math.abs(dy) > 4) moved = true;
    if (moved) {
      tx += dx / scale; ty += dy / scale;
      downX = e.clientX; downY = e.clientY;
      apply();
    }
  });
  window.addEventListener('mouseup', function () {
    if (!dragging) return;
    dragging = false;
    stage.classList.remove('dragging');
    if (moved) suppress = true;
  });
  stage.addEventListener('click', function () {
    if (suppress) { suppress = false; return; }
    close();
  });
  modal.addEventListener('click', function (e) {
    if (!box.contains(e.target)) close();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') close();
  });
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();