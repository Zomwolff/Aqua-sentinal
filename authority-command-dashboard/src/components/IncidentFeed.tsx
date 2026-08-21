import type { IncidentSummary } from "../types";
import { ago, percent, severity, severityClass } from "../utils/format";

export function IncidentFeed({ incidents, selectedId, loading, onSelect }: { incidents: IncidentSummary[]; selectedId?: string; loading: boolean; onSelect: (id: string) => void }) {
  return <section className="incident-feed panel"><div className="section-title"><div><span>ACTIVE INCIDENTS</span><b>{incidents.length}</b></div></div>{loading ? <p className="empty">Loading active incidents…</p> : incidents.length === 0 ? <p className="empty">NO ACTIVE INCIDENTS</p> : <div className="incident-list">{incidents.map(incident => <button key={incident.id} className={`incident-row ${selectedId === incident.id ? "selected" : ""}`} onClick={() => onSelect(incident.id)}><i className={severityClass(incident.severity_level)} /><div><strong>{incident.id.slice(0, 8).toUpperCase()}</strong><span className={`severity ${severityClass(incident.severity_level)}`}>{severity(incident.severity_level)}</span><p>{percent(incident.confidence)} confidence · {ago(incident.detected_at)}</p></div><em>›</em></button>)}</div>}</section>;
}
