export function getSeverityColor(level) {
  switch (level?.toUpperCase()) {
    case "CRITICAL": return "#dc2626"; // red
    case "HIGH": return "#f97316";     // orange
    case "MODERATE": return "#eab308"; // yellow
    case "LOW": return "#22c55e";      // green
    default: return "#9ca3af";         // gray
  }
}

export function getPriorityColor(priority) {
  switch (priority?.toUpperCase()) {
    case "URGENT": return "#ef4444";
    case "HIGH": return "#f97316";
    case "MEDIUM": return "#eab308";
    case "LOW": return "#3b82f6";
    default: return "#9ca3af";
  }
}

export function formatScore(score) {
  if (score === null || score === undefined) return "N/A";
  return Number(score).toFixed(2);
}
