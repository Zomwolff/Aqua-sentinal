import { useMemo, useState } from "react";
import { metric, normalizeRecommendations, percentValue, pct, numberValue } from "../lib/incidentViewModel";
import "./AuthorityDashboard.css";

type Props = { incidents: any[]; vessels: any[]; details: any; liveStatus: string; onClose: () => void; onSelectIncident: (incident: any) => void; onSpotVessel: (mmsi: string) => void };
const horizons = [0, 1, 3, 6, 12, 24];

export function AuthorityDashboard({ incidents, vessels, details, liveStatus, onClose, onSelectIncident, onSpotVessel }: Props) {
  const [horizon, setHorizon] = useState(3);
  const [layers, setLayers] = useState({ vessels: true, spills: true, dark: true, risk: true, forecast: true, fishing: false, protected: true });
  const [statuses, setStatuses] = useState<Record<number, string>>({});
  const incident = details?.incident;
  const severity = details?.severity;
  const forecasts = details?.forecasts || [];
  const forecast = forecasts.find((f: any) => Number(f.horizon_hours) === horizon) || forecasts[0];
  const source = details?.attribution?.[0];
  const actions = useMemo(() => normalizeRecommendations(details?.recommendations), [details]);
  const selected = incidents.find((i) => i.rawId === incident?.id);
  const advance = (i: number) => setStatuses((s) => ({ ...s, [i]: ({ PENDING: "ACKNOWLEDGED", ACKNOWLEDGED: "IN PROGRESS", "IN PROGRESS": "COMPLETED", COMPLETED: "COMPLETED" } as any)[s[i] || actions[i]?.status || "PENDING"] }));

  return <div className="acd-shell">
    <header className="acd-header"><div><small>UNIFIED OPERATIONAL PICTURE</small><h1>Authority Command Dashboard</h1></div><div className="acd-live"><i /> {liveStatus.toUpperCase()} · AUTO REFRESH</div><button onClick={onClose}>×</button></header>
    <div className="acd-body">
      <aside className="acd-incidents"><small>ACTIVE INCIDENTS</small>{incidents.map((row) => <button key={row.rawId} className={row.rawId === incident?.id ? "active" : ""} onClick={() => onSelectIncident(row)}><i className={row.severity}/><span><b>{row.id}</b><em>{row.severity.toUpperCase()} · {row.area}</em></span></button>)}{!incidents.length && <p>No active incidents.</p>}</aside>
      <main className="acd-main">
        <section className="acd-top-grid">
          <div className="acd-map-card">
            <div className="acd-map-head"><div><small>LIVE MARITIME MAP</small><b>{selected?.location || "Select an incident"}</b></div><span>HORIZON {horizon ? `+${horizon}H` : "NOW"}</span></div>
            <div className="acd-map">
              <div className="acd-grid" />
              {layers.risk && <div className="acd-risk-zone">PROTECTED / RISK ZONE</div>}
              {layers.forecast && <div className="acd-forecast-shape" style={{ transform: `translate(${Math.min(horizon * 2, 42)}px, ${Math.min(horizon, 24)}px) scale(${1 + horizon / 60})` }}/>} 
              {layers.spills && <button className="acd-spill" title="Detected spill"><i /></button>}
              {layers.vessels && vessels.slice(0, 8).map((v, i) => <button key={v.mmsi} title={`${v.name} · ${v.risk} risk`} className="acd-vessel" style={{ left: `${18 + (i * 11) % 68}%`, top: `${20 + (i * 17) % 58}%` }} onClick={() => onSpotVessel(v.mmsi)}>▲</button>)}
              {layers.dark && <button className="acd-dark" title="Dark-vessel detection">◌</button>}
              <div className="acd-legend"><span>▲ AIS vessel</span><span><i className="spill"/> spill</span><span>◌ dark vessel</span><span><i className="zone"/> risk zone</span><span><i className="forecast"/> forecast</span></div>
              <div className="acd-layers"><small>MAP LAYERS</small>{Object.entries(layers).map(([key, enabled]) => <label key={key}><input type="checkbox" checked={enabled} onChange={() => setLayers((s) => ({ ...s, [key]: !enabled }))}/>{key.replace("protected", "protected areas").replace("fishing", "fishing zones")}</label>)}</div>
            </div>
            <div className="acd-horizons">{horizons.map((h) => <button className={horizon === h ? "active" : ""} onClick={() => setHorizon(h)} key={h}>{h ? `+${h}H` : "NOW"}</button>)}</div>
          </div>
          <div className="acd-overview">
            <div className="acd-card"><small>INCIDENT OVERVIEW</small><h2>{incident?.id?.slice(0, 8) || "No incident selected"}</h2><div className="acd-kpis"><div><span>Risk level</span><b className="danger">{severity?.severity_level || selected?.severity || "Unavailable"}</b></div><div><span>Detection confidence</span><b>{pct(percentValue(incident?.confidence))}</b></div><div><span>Spill area</span><b>{metric(numberValue(incident?.area_km2), "km²", 2)}</b></div><div><span>Ecological exposure</span><b>{pct(percentValue(severity?.ecological_exposure, severity?.protected_area_risk))}</b></div></div></div>
            <div className="acd-card"><small>SPILL FORECAST</small><h2>{horizon ? `+${horizon} hour outlook` : "Current footprint"}</h2><div className="acd-kpis"><div><span>Drift distance</span><b>{metric(numberValue(forecast?.drift_distance_m) === null ? null : Number(forecast.drift_distance_m) / 1000, "km")}</b></div><div><span>Predicted area</span><b>{metric(numberValue(forecast?.area_km2, forecast?.forecast_area_km2), "km²")}</b></div><div><span>Spread radius</span><b>{metric(numberValue(forecast?.spread_radius_m) === null ? null : Number(forecast.spread_radius_m) / 1000, "km")}</b></div><div><span>Confidence</span><b>{pct(percentValue(forecast?.confidence))}</b></div></div></div>
            {source && <div className="acd-card"><small>SOURCE VESSEL · CIRCUMSTANTIAL</small><h2>{source.vessel_name || `MMSI ${source.mmsi}`}</h2><p>{pct(percentValue(source.final_score))} attribution · {source.classification || "review evidence"}</p><button className="acd-link" onClick={() => onSpotVessel(String(source.mmsi))}>Track / show on map →</button></div>}
          </div>
        </section>
        <section className="acd-actions"><div className="acd-section-head"><div><small>RESPONSE PLAN</small><h2>{actions.length} actions recommended</h2></div><p>Decision support only · status changes remain local until backend action support is available.</p></div><div className="acd-action-grid">{actions.map((action, i) => <article key={i} className={`acd-action ${action.priority.toLowerCase()}`}><div><span className="acd-priority">{action.priority}</span>{action.synthetic && <span className="acd-demo">DEMO</span>}<span className="acd-status">{statuses[i] || action.status}</span></div><h3>{action.title}</h3><p>{action.description}</p><details><summary>Why recommended?</summary><p>{action.rationale || (Array.isArray(action.evidence) ? action.evidence.join(" · ") : String(action.evidence || "Generated from incident severity, forecast, exposure and attribution evidence."))}</p><p><b>Expected outcome:</b> {action.expected_outcome || "Reduce operational risk after operator validation."}</p></details><div className="acd-action-buttons"><button onClick={() => advance(i)} disabled={(statuses[i] || action.status) === "COMPLETED"}>Update status</button>{action.location && <button>Show on map</button>}</div></article>)}{!actions.length && <div className="acd-empty">No response recommendations are available for this incident.</div>}</div></section>
      </main>
    </div>
  </div>;
}
