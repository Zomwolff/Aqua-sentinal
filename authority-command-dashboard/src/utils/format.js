export function severity(value) {
  const normalized = String(value || "UNASSESSED").toUpperCase();
  return normalized === "MODERATE" ? "MEDIUM" : normalized;
}
export function severityClass(value) { return ["CRITICAL", "HIGH", "MEDIUM", "LOW"].includes(severity(value)) ? severity(value).toLowerCase() : "unknown"; }
export function percent(value) { return value === null || value === undefined || Number.isNaN(Number(value)) ? "—" : `${Math.round(Number(value) <= 1 ? Number(value) * 100 : Number(value))}%`; }
export function km2(value) { return value === null || value === undefined || Number.isNaN(Number(value)) ? "—" : `${Number(value).toFixed(2)} km²`; }
export function ago(value) { if (!value) return "—"; const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000); return seconds < 60 ? "just now" : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` : `${Math.floor(seconds / 3600)}h ago`; }
export function selectedForecast(forecasts, horizon) { return forecasts.find((item) => Number(item.horizon_hours) === Number(horizon)) || null; }
export function reconnectDelay(attempt) { return Math.min(30000, 1000 * 2 ** Math.min(attempt, 5)); }
