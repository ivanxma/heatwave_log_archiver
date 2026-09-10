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

// Keep the shared Tab View accessible as well as visually selected.
document.querySelectorAll('[data-tab-view]').forEach((view) => {
  const tabs = [...view.querySelectorAll('[role=tab]')];
  const select = (selected) => tabs.forEach((tab) => tab.setAttribute('aria-selected', String(tab === selected)));
  tabs.forEach((tab) => tab.addEventListener('click', () => select(tab)));
  select(tabs.find((tab) => tab.classList.contains('active')) || tabs[0]);
});

// Redraw the activity chart after layout has settled. This avoids a zero-width
// canvas when the dashboard is restored from a browser cache or a tab view.
(() => {
  const canvas = document.getElementById('activity-chart');
  if (!canvas) return;
  let activity;
  try { activity = JSON.parse(canvas.dataset.activity || '[]'); } catch (_) { activity = []; }
  const draw = () => {
    const width = Math.max(320, Math.floor(canvas.parentElement.getBoundingClientRect().width - 2));
    const height = 180;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    const context = canvas.getContext('2d');
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    context.strokeStyle = '#cbd5e1';
    context.beginPath(); context.moveTo(36, 145); context.lineTo(width - 12, 145); context.stroke();
    if (!activity.length) {
      context.fillStyle = '#64748b'; context.font = '14px system-ui';
      context.fillText('No archive executions recorded in the last 24 hours.', 36, 86);
      return;
    }
    const maximum = Math.max(1, ...activity.map((item) => Number(item.count) || 0));
    const slot = (width - 52) / activity.length;
    activity.forEach((item, index) => {
      const count = Number(item.count) || 0;
      const barHeight = Math.max(count ? 3 : 0, (count / maximum) * 110);
      context.fillStyle = item.status === 'Succeeded' ? '#b91c1c' : '#94a3b8';
      context.fillRect(38 + index * slot, 145 - barHeight, Math.max(2, slot - 3), barHeight);
    });
    context.fillStyle = '#475569'; context.font = '11px system-ui';
    context.fillText('0', 12, 148); context.fillText(String(maximum), 4, 33);
    context.fillText(activity[0].time || '', 38, 165);
    if (activity.length > 1) context.fillText(activity[activity.length - 1].time || '', width - 45, 165);
  };
  requestAnimationFrame(draw);
  window.addEventListener('resize', draw);
})();
