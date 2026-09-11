import { escapeHtml } from "./components.js";

function numeric(value, fallback = 0) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(-1e12, Math.min(1e12, parsed));
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

  dynagraph(samples, { title = "示功图", xLabel = "位移 (m)", yLabel = "载荷 (kN)", reference = [] } = {}) {
    if (!Array.isArray(samples) || samples.length < 3) return placeholder("暂无功图点集");
    const width = 560, height = 300, left = 68, right = 24, top = 24, bottom = 58;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
    const points = samples.map((item, index) => ({ x: numeric(item.x ?? item.displacement ?? index), y: numeric(item.y ?? item.load ?? item) }));
    const referencePoints = Array.isArray(reference) ? reference.map((item, index) => ({ x: numeric(item.x ?? item.displacement ?? index), y: numeric(item.y ?? item.load ?? item) })) : [];
    const domainPoints = [...points, ...referencePoints];
    const xs = scale(domainPoints.map((item) => item.x)), ys = scale(domainPoints.map((item) => item.y));
    const x = (value) => left + (numeric(value) - xs.min) / xs.span * plotWidth;
    const y = (value) => top + (ys.max - numeric(value)) / ys.span * plotHeight;
    const coords = (items) => items.map((item) => `${x(item.x).toFixed(1)},${y(item.y).toFixed(1)}`).join(" ");
    const divisions = [0, .25, .5, .75, 1];
    const xTicks = divisions.map((ratio) => {
      const px = left + ratio * plotWidth;
      const value = xs.min + ratio * xs.span;
      return `<g data-axis="x" class="chart-tick"><line x1="${px.toFixed(1)}" y1="${top}" x2="${px.toFixed(1)}" y2="${height - bottom}" /><text x="${px.toFixed(1)}" y="${height - bottom + 19}" text-anchor="middle">${escapeHtml(value.toFixed(2))}</text></g>`;
    }).join("");
    const yTicks = divisions.map((ratio) => {
      const py = top + ratio * plotHeight;
      const value = ys.max - ratio * ys.span;
      return `<g data-axis="y" class="chart-tick"><line x1="${left}" y1="${py.toFixed(1)}" x2="${width - right}" y2="${py.toFixed(1)}" /><text x="${left - 10}" y="${(py + 4).toFixed(1)}" text-anchor="end">${escapeHtml(value.toFixed(2))}</text></g>`;
    }).join("");
    const tooltips = points.map((item) => `<circle cx="${x(item.x).toFixed(1)}" cy="${y(item.y).toFixed(1)}" r="3"><title>${escapeHtml(xLabel)}：${escapeHtml(item.x.toFixed(3))}；${escapeHtml(yLabel)}：${escapeHtml(item.y.toFixed(3))}</title></circle>`).join("");
    return `<figure class="chart"><figcaption class="visually-hidden">${escapeHtml(title)}</figcaption><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(title)}"><rect class="plot-frame" x="${left}" y="${top}" width="${plotWidth}" height="${plotHeight}" /><g class="chart-grid dynagraph-grid">${xTicks}${yTicks}</g>${referencePoints.length ? `<polyline class="dynagraph-reference" points="${coords(referencePoints)}" />` : ""}<polyline class="dynagraph-line" points="${coords(points)}" /><g class="chart-points dynagraph-points">${tooltips}</g><text class="chart-axis-title" x="${left + plotWidth / 2}" y="${height - 10}" text-anchor="middle">${escapeHtml(xLabel)}</text><text class="chart-axis-title" x="16" y="${top + plotHeight / 2}" text-anchor="middle" transform="rotate(-90 16 ${top + plotHeight / 2})">${escapeHtml(yLabel)}</text></svg></figure>`;
  },
};
