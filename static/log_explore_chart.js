// Keep every interval inside the chart panel without shrinking axis text.
(() => {
  const svg = document.getElementById('explore-chart');
  if (!svg) return;
  const buckets = JSON.parse(svg.dataset.buckets);
  const detail = document.getElementById('explore-chart-detail');
  const ns = 'http://www.w3.org/2000/svg';
  const left = 62, top = 24, bottom = 254;
  let width = 0, step = 0;
  const add = (tag, attrs, value) => {
    const element = document.createElementNS(ns, tag);
    Object.entries(attrs).forEach(([key, val]) => element.setAttribute(key, val));
    if (value !== undefined) element.textContent = value;
    svg.append(element);
    return element;
  };
  const draw = () => {
    width = Math.max(1, svg.parentElement.getBoundingClientRect().width);
    const right = Math.max(left + 1, width - 16);
    step = (right - left) / buckets.length;
    const maximum = Math.max(1, ...buckets.map(bucket => bucket.count));
    svg.setAttribute('viewBox', `0 0 ${width} 320`);
    svg.replaceChildren();
    add('title', {}, svg.getAttribute('aria-label'));
    [0, 0.5, 1].forEach(fraction => {
      const y = bottom - fraction * (bottom - top);
      add('line', {x1: left, y1: y, x2: right, y2: y, stroke: '#e2e8f0'});
      add('text', {x: left - 8, y: y + 4, 'text-anchor': 'end', 'font-size': 12, fill: '#475569'}, String(Math.round(maximum * fraction)));
    });
    buckets.forEach((bucket, index) => {
      const height = bucket.count / maximum * (bottom - top);
      const bar = add('rect', {x: left + index * step, y: bottom - height, width: step * 0.8, height, fill: '#a71925'});
      const title = document.createElementNS(ns, 'title');
      title.textContent = `${bucket.time} UTC: ${bucket.count} records`;
      bar.append(title);
    });
    const ticks = Math.min(buckets.length, Math.max(1, Math.floor((right - left) / 150)));
    for (let tick = 0; tick < ticks; tick++) {
      const index = ticks === 1 ? 0 : Math.round(tick * (buckets.length - 1) / (ticks - 1));
      const x = left + (index + 0.5) * step;
      const anchor = tick === 0 ? 'start' : tick === ticks - 1 ? 'end' : 'middle';
      add('text', {x, y: bottom + 24, 'text-anchor': anchor, 'font-size': 11, fill: '#475569'}, buckets[index].time);
    }
    add('text', {x: (left + right) / 2, y: 308, 'text-anchor': 'middle', 'font-size': 12, fill: '#475569'}, 'Time (UTC)');
  };
  svg.addEventListener('pointermove', event => {
    const x = event.clientX - svg.getBoundingClientRect().left;
    if (x < left || x >= width - 16) return;
    const index = Math.min(buckets.length - 1, Math.floor((x - left) / step));
    const bucket = buckets[index];
    detail.textContent = `${bucket.time} UTC: ${bucket.count} records`;
  });
  draw();
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(draw).observe(svg.parentElement);
  } else {
    window.addEventListener('resize', draw);
  }
})();
