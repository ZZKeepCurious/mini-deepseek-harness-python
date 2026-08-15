// Mermaid 图点击放大弹窗（移植自原 HTML 报告的 zoom-modal 脚本）
// 适配 MkDocs Material 内置 mermaid：SVG 渲染进 closed shadow DOM，
// 故用 composedPath() 穿透 shadow 查找 svg，并清除内联 max-width 以支持放大。
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

  function apply() {
    if (!svgEl) return;
    svgEl.style.transform = 'translate(' + tx + 'px, ' + ty + 'px) scale(' + scale + ')';
    label.textContent = Math.round(scale * 100) + '%';
  }
  function close() {
    modal.classList.remove('show');
    svgEl = null;
  }
  function findSvg(e) {
    var path = e.composedPath ? e.composedPath() : [];
    for (var i = 0; i < path.length; i++) {
      var n = path[i];
      if (n && n.tagName === 'svg') return n;
    }
    return null;
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
      var svg = findSvg(e);
      if (!svg) return;
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
  function scan() { document.querySelectorAll('.mermaid').forEach(bindZoom); }
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
  function init() {
    scan();
    new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();