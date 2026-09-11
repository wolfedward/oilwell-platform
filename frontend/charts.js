import { escapeHtml } from "./components.js";

function numeric(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function scale(values, floor = 0) {
  const safe = values.map((value) => numeric(value));
  const min = Math.min(floor, ...safe);
  const max = Math.max(...safe, min + 1);
  return { min, max, span: max - min };
}

function placeholder(message = "暂无趋势数据") {
  return `<div class="chart-empty">${escapeHtml(message)}</div>`;
}

export const charts = {
  line(items, { xKey = "x", yKey = "y", title = "趋势图", unit = "", color = "#1e6ee8" } = {}) {
    if (!Array.isArray(items) || items.length < 2) return placeholder();
    const width = 720, height = 240, left = 54, right = 18, top = 20, bottom = 38;
    const values = items.map((item) => numeric(item[yKey]));
    const domain = scale(values);
    const x = (index) => left + index * (width - left - right) / Math.max(1, items.length - 1);
    const y = (value) => top + (domain.max - value) * (height - top - bottom) / domain.span;
    const path = items.map((item, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(numeric(item[yKey])).toFixed(1)}`).join(" ");
    const guide = [0, .25, .5, .75, 1].map((ratio) => {
      const py = top + ratio * (height - top - bottom);
      const value = domain.max - ratio * domain.span;
      return `<line x1="${left}" y1="${py}" x2="${width - right}" y2="${py}" />
        <text x="${left - 9}" y="${py + 4}" text-anchor="end">${escapeHtml(value.toFixed(1))}</text>`;
    }).join("");
    const labels = items.map((item, index) => index % Math.max(1, Math.ceil(items.length / 6)) === 0 || index === items.length - 1
      ? `<text x="${x(index)}" y="${height - 12}" text-anchor="middle">${escapeHtml(String(item[xKey] ?? "").slice(5, 10))}</text>` : "").join("");
    const points = items.map((item, index) => `<circle cx="${x(index)}" cy="${y(numeric(item[yKey]))}" r="3"><title>${escapeHtml(item[xKey])}：${escapeHtml(numeric(item[yKey]).toFixed(2))}${escapeHtml(unit)}</title></circle>`).join("");
    return `<figure class="chart"><figcaption class="visually-hidden">${escapeHtml(title)}</figcaption>
      <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(title)}">
        <g class="chart-grid">${guide}${labels}</g>
        <path class="chart-area" style="--chart-color:${color}" d="${path} L${x(items.length - 1)},${height - bottom} L${x(0)},${height - bottom} Z" />
        <path class="chart-line" style="--chart-color:${color}" d="${path}" />
        <g class="chart-points" style="--chart-color:${color}">${points}</g>
      </svg></figure>`;
  },

  bar(items, { labelKey = "label", valueKey = "value", title = "柱状图", unit = "" } = {}) {
    if (!Array.isArray(items) || !items.length) return placeholder();
    const max = Math.max(1, ...items.map((item) => numeric(item[valueKey])));
    return `<figure class="bar-chart" role="img" aria-label="${escapeHtml(title)}"><figcaption class="visually-hidden">${escapeHtml(title)}</figcaption>
      ${items.map((item) => { const value = numeric(item[valueKey]); return `<div><span>${escapeHtml(item[labelKey])}</span><i><b style="width:${Math.max(1, value / max * 100).toFixed(1)}%"></b></i><strong>${escapeHtml(value.toFixed(1))}${escapeHtml(unit)}</strong></div>`; }).join("")}</figure>`;
  },

  gauge(value, { title = "完成率", max = 1, unit = "%" } = {}) {
    const ratio = Math.max(0, Math.min(1, numeric(value) / Math.max(0.0001, numeric(max, 1))));
    const percent = ratio * 100;
    return `<figure class="gauge" role="img" aria-label="${escapeHtml(title)} ${percent.toFixed(1)}${escapeHtml(unit)}">
      <div style="--gauge:${percent.toFixed(1)}deg"><strong>${percent.toFixed(1)}<small>${escapeHtml(unit)}</small></strong></div><figcaption>${escapeHtml(title)}</figcaption></figure>`;
  },

  dynagraph(samples, { title = "示功图" } = {}) {
    if (!Array.isArray(samples) || samples.length < 3) return placeholder("暂无功图点集");
    const points = samples.map((item, index) => ({ x: numeric(item.x ?? item.displacement ?? index), y: numeric(item.y ?? item.load ?? item) }));
    const xs = scale(points.map((item) => item.x)), ys = scale(points.map((item) => item.y));
    const coords = points.map((item) => `${(24 + (item.x - xs.min) / xs.span * 432).toFixed(1)},${(16 + (ys.max - item.y) / ys.span * 188).toFixed(1)}`).join(" ");
    return `<figure class="chart"><figcaption class="visually-hidden">${escapeHtml(title)}</figcaption><svg viewBox="0 0 480 230" role="img" aria-label="${escapeHtml(title)}"><rect class="plot-frame" x="24" y="16" width="432" height="188" /><polyline class="dynagraph-line" points="${coords}" /></svg></figure>`;
  },
};
