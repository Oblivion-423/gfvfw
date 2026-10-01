// 让资料库页突破 .container 的 max-width
(function () {
  const page = document.querySelector('.explorer-page');
  if (!page) return;
  const container = page.closest('.container');
  if (container) container.classList.add('container-wide');
})();

(function () {
  'use strict';

  const root = document.querySelector('.explorer-page');
  if (!root) return;

  const treeEl   = document.getElementById('tree');
  const bodyEl   = document.getElementById('viewerBody');
  const headEl   = document.getElementById('viewerHead');
  const nameEl   = document.getElementById('fileName');
  const typeEl   = document.getElementById('fileType');
  const visEl    = document.getElementById('fileVisibility');
  const pathEl   = document.getElementById('curPath');
  const dlBtn    = document.getElementById('downloadBtn');
  const searchEl = document.getElementById('searchInput');
  const csrf     = window.LIBRARY_CSRF || '';

  const CAN_UPLOAD = root.dataset.canUpload === '1';

  // ---------------- 图标 ----------------
  function fileIcon(name) {
    const ext = (name.split('.').pop() || '').toLowerCase();
    const map = {
      pdf: '📕', doc: '📘', docx: '📘',
      xls: '📗', xlsx: '📗', csv: '📗',
      png: '🖼', jpg: '🖼', jpeg: '🖼', gif: '🖼',
      webp: '🖼', bmp: '🖼', svg: '🖼', tif: '🖼', tiff: '🖼',
      md: '📝', markdown: '📝',
      txt: '📄', log: '📄', json: '📄', xml: '📄',
    };
    return map[ext] || '📄';
  }

  // ---------------- 文件树 ----------------
  async function loadFolder(path, container) {
    container.innerHTML = '<div class="muted small" style="padding:8px 14px">加载中…</div>';
    const res = await fetch('/library/api/tree?path=' + encodeURIComponent(path));
    if (!res.ok) {
      container.innerHTML = '<div class="error small" style="padding:8px 14px">加载失败</div>';
      return;
    }
    const data = await res.json();
    container.innerHTML = '';

    for (const name of data.folders) {
      const sub = path ? path + '/' + name : name;
      const node = document.createElement('div');
      node.className = 'tree-node';
      node.dataset.name = name.toLowerCase();

      const row = document.createElement('div');
      row.className = 'tree-row';
      row.innerHTML = '<span class="icon">📁</span><span class="name"></span>';
      row.querySelector('.name').textContent = name;
      node.appendChild(row);

      const children = document.createElement('div');
      children.className = 'tree-children';
      children.style.display = 'none';
      node.appendChild(children);

      let loaded = false;
      row.addEventListener('click', async e => {
        e.stopPropagation();
        const opening = children.style.display === 'none';
        if (opening && !loaded) {
          await loadFolder(sub, children);
          loaded = true;
        }
        children.style.display = opening ? 'block' : 'none';
        row.classList.toggle('open', opening);
        if (opening) pathEl.textContent = '/' + sub;
      });

      container.appendChild(node);
    }

    for (const f of data.files) {
      const node = document.createElement('div');
      node.className = 'tree-node';
      node.dataset.name = (f.name || '').toLowerCase();

      const row = document.createElement('div');
      row.className = 'tree-row';
      row.innerHTML = '<span class="icon"></span><span class="name"></span>';
      row.querySelector('.icon').textContent = fileIcon(f.name);
      row.querySelector('.name').textContent = f.name;
      row.dataset.docId = f.id;

      row.addEventListener('click', e => {
        e.stopPropagation();
        document.querySelectorAll('.tree-row.active')
          .forEach(el => el.classList.remove('active'));
        row.classList.add('active');
        preview(f.id, f.name, f.visibility);
      });

      node.appendChild(row);
      container.appendChild(node);
    }

    if (!data.folders.length && !data.files.length) {
      container.innerHTML = '<div class="muted small" style="padding:8px 14px">（空目录）</div>';
    }
  }

  // ---------------- 预览 ----------------
  async function preview(id, name, visibility) {
    resetZoom();
    headEl.style.display = 'flex';
    nameEl.textContent = name;
    typeEl.textContent = (name.split('.').pop() || '?').toUpperCase();
    visEl.textContent = { public: '公开', members: '队内', command: '指挥层' }[visibility] || visibility;
    if (dlBtn) dlBtn.href = '/library/api/download?id=' + encodeURIComponent(id);

    bodyEl.classList.remove('pdf-mode');
    bodyEl.innerHTML = '<div class="empty">加载中…</div>';

    let data;
    try {
      const res = await fetch('/library/api/preview?id=' + encodeURIComponent(id));
      if (!res.ok) throw new Error('HTTP ' + res.status);
      data = await res.json();
    } catch (err) {
      bodyEl.innerHTML = '<div class="empty">加载失败：' + err.message + '</div>';
      return;
    }
    render(data);
  }

  function render(data) {
    bodyEl.innerHTML = '';
    bodyEl.classList.remove('pdf-mode');
    const rawUrl = '/library/api/raw?id=' + encodeURIComponent(data.id);

    switch (data.type) {
      case 'image':
        bodyEl.innerHTML = '<div class="viewer-image"><img alt=""></div>';
        bodyEl.querySelector('img').src = rawUrl;
        break;

      case 'pdf':
        bodyEl.classList.add('pdf-mode');
        bodyEl.innerHTML = '<iframe class="viewer-pdf"></iframe>';
        bodyEl.querySelector('iframe').src = rawUrl;
        break;

      case 'tiff':
        renderTiff(data.pages);
        break;

      case 'html':
        bodyEl.innerHTML = '<div class="viewer-doc"></div>';
        bodyEl.querySelector('.viewer-doc').innerHTML = data.html;
        break;

      case 'text':
        bodyEl.innerHTML = '<pre class="viewer-text"></pre>';
        bodyEl.querySelector('pre').textContent = data.text;
        break;

      case 'spreadsheet':
        renderSheets(data.sheets);
        break;

      default:
        bodyEl.innerHTML = '<div class="empty"></div>';
        bodyEl.querySelector('.empty').textContent =
          data.message || '暂不支持预览该文件';
    }
  }

  function renderTiff(pages) {
    const wrap = document.createElement('div');
    wrap.className = 'viewer-tiff';
    pages.forEach((b64, i) => {
      const label = document.createElement('div');
      label.className = 'page-label';
      label.textContent = `第 ${i + 1} 页 / 共 ${pages.length} 页`;
      wrap.appendChild(label);

      const img = document.createElement('img');
      img.src = 'data:image/png;base64,' + b64;
      img.alt = `第 ${i + 1} 页`;
      wrap.appendChild(img);

      if (i < pages.length - 1) {
        const sep = document.createElement('div');
        sep.className = 'page-sep';
        wrap.appendChild(sep);
      }
    });
    bodyEl.appendChild(wrap);
  }

  function renderSheets(sheets) {
    const wrap = document.createElement('div');
    wrap.className = 'viewer-sheets';
    sheets.forEach(sheet => {
      const block = document.createElement('div');
      block.className = 'sheet';

      const h = document.createElement('h3');
      const n = document.createElement('span');
      n.textContent = sheet.name;
      const b = document.createElement('span');
      b.className = 'badge';
      b.textContent = `${sheet.rows.length} 行`;
      h.appendChild(n); h.appendChild(b);
      block.appendChild(h);

      const scroll = document.createElement('div');
      scroll.className = 'table-scroll';

      const table = document.createElement('table');
      sheet.rows.forEach((row, ri) => {
        const tr = document.createElement('tr');
        row.forEach(cell => {
          const td = document.createElement(ri === 0 ? 'th' : 'td');
          td.textContent = cell;
          tr.appendChild(td);
        });
        table.appendChild(tr);
      });
      scroll.appendChild(table);
      block.appendChild(scroll);
      wrap.appendChild(block);
    });
    bodyEl.appendChild(wrap);
  }

  // ---------------- 搜索 ----------------
  let searchTimer = null;
  searchEl && searchEl.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runSearch, 220);
  });

  async function runSearch() {
    const q = searchEl.value.trim();
    if (!q) { refreshTree(); return; }
    const res = await fetch('/library/api/search?q=' + encodeURIComponent(q));
    if (!res.ok) return;
    const data = await res.json();

    treeEl.innerHTML = '';
    if (!data.results.length) {
      treeEl.innerHTML = '<div class="muted small" style="padding:8px 14px">无匹配</div>';
      return;
    }
    data.results.forEach(f => {
      const row = document.createElement('div');
      row.className = 'tree-row';
      row.innerHTML = '<span class="icon"></span><span class="name"></span>';
      row.querySelector('.icon').textContent = fileIcon(f.name);
      row.querySelector('.name').textContent = f.title + '  ·  /' + f.folder;
      row.addEventListener('click', () => preview(f.id, f.name, ''));
      treeEl.appendChild(row);
    });
  }

  // ---------------- 上传 ----------------
  const uploadBtn = document.getElementById('uploadBtn');
  const uploadDialog = document.getElementById('uploadDialog');
  const uploadForm = document.getElementById('uploadForm');
  const uploadCancel = document.getElementById('uploadCancel');
  const uploadMsg = document.getElementById('uploadMsg');

  if (uploadBtn && uploadDialog) {
    uploadBtn.addEventListener('click', () => {
      uploadMsg.textContent = '';
      uploadDialog.showModal();
    });
    uploadCancel.addEventListener('click', () => uploadDialog.close());

    uploadForm.addEventListener('submit', async e => {
      e.preventDefault();
      const fd = new FormData(uploadForm);       // 自动带 csrf_token
      uploadMsg.textContent = '上传中…';

      try {
        const res = await fetch('/library/api/upload', {
          method: 'POST',
          body: fd,
          credentials: 'same-origin',
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || ('HTTP ' + res.status));
        }
        uploadDialog.close();
        refreshTree();
      } catch (err) {
        uploadMsg.textContent = '失败：' + err.message;
      }
    });
  }

    // ---------------- 缩放 ----------------
  const ZOOM_MIN  = 0.25;
  const ZOOM_MAX  = 5;
  const ZOOM_STEP = 0.1;
  let zoom = 1;

  const zoomLabel  = document.getElementById('zoomLabel');
  const zoomInBtn  = document.getElementById('zoomIn');
  const zoomOutBtn = document.getElementById('zoomOut');
  const zoomResetBtn = document.getElementById('zoomReset');

  function applyZoom() {
    bodyEl.style.setProperty('--viewer-zoom', String(zoom));
    if (zoomLabel) zoomLabel.textContent = Math.round(zoom * 100) + '%';
  }

  function setZoom(z) {
    zoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, z));
    applyZoom();
  }

  function resetZoom() {
    zoom = 1;
    applyZoom();
  }

  zoomInBtn  && zoomInBtn.addEventListener('click', () => setZoom(zoom + ZOOM_STEP));
  zoomOutBtn && zoomOutBtn.addEventListener('click', () => setZoom(zoom - ZOOM_STEP));
  zoomResetBtn && zoomResetBtn.addEventListener('click', resetZoom);

  // Ctrl + 滚轮：图片 / 文本 / 表格都能缩放
  bodyEl.addEventListener('wheel', e => {
    if (!e.ctrlKey) return;
    // PDF：交给浏览器原生 viewer，不接管
    if (bodyEl.classList.contains('pdf-mode')) return;
    e.preventDefault();
    const dir = e.deltaY > 0 ? -1 : 1;
    setZoom(zoom + dir * ZOOM_STEP);
  }, { passive: false });

  // 键盘快捷键：Ctrl + / Ctrl - / Ctrl 0
  document.addEventListener('keydown', e => {
    if (!e.ctrlKey) return;
    if (e.key === '=' || e.key === '+') { e.preventDefault(); setZoom(zoom + ZOOM_STEP); }
    if (e.key === '-')                  { e.preventDefault(); setZoom(zoom - ZOOM_STEP); }
    if (e.key === '0')                  { e.preventDefault(); resetZoom(); }
  });

  // ---------------- 刷新 ----------------
  function refreshTree() {
    treeEl.innerHTML = '';
    loadFolder('', treeEl);
  }

  const refreshBtn = document.getElementById('refreshBtn');
  if (refreshBtn) refreshBtn.addEventListener('click', refreshTree);
  refreshTree();
})();

  function fitExplorerHeight() {
  const page = document.querySelector('.explorer-page');
  if (!page) return;

  // 强制布局（防 CSS 层叠被覆盖）
  page.style.display = 'flex';
  page.style.flexDirection = 'column';
  page.style.height = '';

  const top = page.getBoundingClientRect().top;
  const avail = window.innerHeight - top;

  if (avail > 200) {
    page.style.height = avail + 'px';
  }
}

let _fitTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(_fitTimer);
  _fitTimer = setTimeout(fitExplorerHeight, 60);
});
fitExplorerHeight();