export function text(value, fallback = "—") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

export function number(value, digits = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed)
    ? new Intl.NumberFormat("zh-CN", { maximumFractionDigits: digits }).format(parsed)
    : "—";
}

export function percent(value, digits = 1) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${number(parsed * 100, digits)}%` : "—";
}

export function dateTime(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(parsed);
}

export function energy(value) {
  return `${number(value, 1)} kWh`;
}

export function duration(hours) {
  const value = Number(hours);
  if (!Number.isFinite(value)) return "—";
  if (value < 24) return `${number(value, 1)} 小时`;
  return `${number(value / 24, 1)} 天`;
}
