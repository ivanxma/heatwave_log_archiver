(() => {
  const keyFor = (table, index) => `error-archiver-table-layout-${table.id || index}`;
  const moveColumn = (table, from, to) => {
    [...table.rows].forEach((row) => {
      const cell = row.cells[from];
      if (!cell) return;
      row.insertBefore(cell, row.cells[to] || null);
    });
  };
  document.querySelectorAll('.listing-table').forEach((table, tableIndex) => {
    const key = keyFor(table, tableIndex);
    [...table.tHead.rows[0].cells].forEach((head, index) => { head.dataset.original = String(index); });
    try {
      const saved = JSON.parse(localStorage.getItem(key) || '{}');
      if (saved.order) saved.order.forEach((original, target) => moveColumn(table, [...table.tHead.rows[0].cells].findIndex((h) => h.dataset.original === String(original)), target));
      if (saved.widths) [...table.tHead.rows[0].cells].forEach((head) => { if (saved.widths[head.dataset.original]) head.style.width = saved.widths[head.dataset.original]; });
    } catch (_) {}
    const save = () => localStorage.setItem(key, JSON.stringify({ order: [...table.tHead.rows[0].cells].map((h) => Number(h.dataset.original)), widths: Object.fromEntries([...table.tHead.rows[0].cells].map((h) => [h.dataset.original, h.style.width])) }));
    [...table.tHead.rows[0].cells].forEach((head, index) => {
      head.dataset.original = String(index);
      head.draggable = true;
      head.addEventListener('dragstart', (event) => event.dataTransfer.setData('text/plain', String(index)));
      head.addEventListener('dragover', (event) => event.preventDefault());
      head.addEventListener('drop', (event) => { event.preventDefault(); const from = Number(event.dataTransfer.getData('text/plain')); const to = [...table.tHead.rows[0].cells].indexOf(head); if (from !== to) { moveColumn(table, from, to); save(); } });
      const handle = document.createElement('span'); handle.className = 'column-resize'; handle.title = 'Drag to resize'; handle.style.cssText = 'float:right;width:7px;height:18px;cursor:col-resize;border-right:2px solid #94a3b8'; head.append(handle);
      handle.addEventListener('mousedown', (event) => { event.preventDefault(); event.stopPropagation(); const start = event.clientX, width = head.getBoundingClientRect().width; const resize = (move) => { head.style.width = `${Math.max(70, width + move.clientX - start)}px`; }; const done = () => { document.removeEventListener('mousemove', resize); document.removeEventListener('mouseup', done); save(); }; document.addEventListener('mousemove', resize); document.addEventListener('mouseup', done); });
    });
  });
  document.querySelectorAll('[data-reset-table]').forEach((button) => button.addEventListener('click', () => { const table = document.getElementById(button.dataset.resetTable); const index = [...document.querySelectorAll('.listing-table')].indexOf(table); localStorage.removeItem(keyFor(table, index)); location.reload(); }));
})();
