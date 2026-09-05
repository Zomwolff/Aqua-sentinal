import "./SpillForecastAnalysis.css";

type Props = { incidentDetail: any; horizon: number; onHorizonChange: (hours: number) => void };
const first = (...values: any[]) => values.find((v) => v !== undefined && v !== null && v !== "");
const num = (...values: any[]): number | null => { const v = first(...values); return v === undefined || !Number.isFinite(Number(v)) ? null : Number(v); };
const pct = (v: any) => { const n = num(v); return n === null ? "Unavailable" : `${(n <= 1 ? n * 100 : n).toFixed(0)}%`; };
const distance = (meters: any) => { const n = num(meters); return n === null ? "Unavailable" : `${(n / 1000).toFixed(2)} km`; };
const area = (v: any) => { const n = num(v); return n === null ? "Unavailable" : `${n.toFixed(2)} km²`; };
const speed = (v: any, unit: string) => { const n = num(v); return n === null ? "Unavailable" : `${n.toFixed(2)} ${unit}`; };
const compass = (degrees: any) => { const n = num(degrees); if (n === null) return "Unavailable"; const names = ["N","NE","E","SE","S","SW","W","NW"]; return `${names[Math.round((((n % 360) + 360) % 360) / 45) % 8]} · ${n.toFixed(0)}°`; };

const forecastArea = (row: any) => first(row?.forecast_area_km2, row?.area_km2, row?.predicted_area_km2, row?.geometry_area_km2);
const forcing = (detail: any, row: any) => first(row?.environmental_forcing, row?.forcing, detail?.environmental_forcing, detail?.environmental_conditions, detail?.forecast_forcing) || {};

export function SpillForecastAnalysis({ incidentDetail, horizon, onHorizonChange }: Props) {
  const incident = incidentDetail?.incident || {};
  const forecasts = [...(incidentDetail?.forecasts || [])].sort((a: any, b: any) => Number(a.horizon_hours) - Number(b.horizon_hours));
  const horizons = Array.from(new Set([0, ...forecasts.map((row: any) => Number(row.horizon_hours)).filter(Number.isFinite)]));
  const selected = horizon === 0 ? null : forecasts.find((row: any) => Number(row.horizon_hours) === horizon) || null;
  const env = forcing(incidentDetail, selected);
  const selectedArea = selected ? forecastArea(selected) : incident.area_km2;
  const model = first(selected?.model_type, selected?.model, selected?.model_version);
  const modelName = model ? String(model) : "Unavailable";
  const ensemble = /ensemble|particle/i.test(modelName);
  const areas = [{ h: 0, value: num(incident.area_km2), observed: true }, ...forecasts.map((row: any) => ({ h: Number(row.horizon_hours), value: num(forecastArea(row)), observed: false }))];
  const validAreas = areas.filter((point) => point.value !== null) as { h: number; value: number; observed: boolean }[];
  const maxArea = validAreas.length ? Math.max(...validAreas.map((p) => p.value), .01) : 1;
  const maxHorizon = Math.max(...areas.map((p) => p.h), 1);
  const chartPoints = validAreas.map((p) => `${22 + (p.h / maxHorizon) * 256},${104 - (p.value / maxArea) * 82}`).join(" ");
  const windSpeedMs = first(env.wind_speed_ms, selected?.wind_speed_ms);
  const windSpeedKmh = first(env.wind_speed_kmh, selected?.wind_speed_kmh, num(windSpeedMs) === null ? undefined : Number(windSpeedMs) * 3.6);
  const currentSpeed = first(env.current_speed_ms, selected?.current_speed_ms);
  const windDirection = first(env.wind_direction_deg, env.wind_dir_deg, selected?.wind_direction_deg);
  const currentDirection = first(env.current_direction_deg, env.current_dir_deg, selected?.current_direction_deg);
  const direction = first(selected?.direction, selected?.drift_direction);
  const windLeeway = first(selected?.wind_leeway_percent, env.wind_leeway_percent);
  const currentContribution = first(selected?.current_contribution_percent, env.current_contribution_percent);

  return <section className="idp-card sfa-card">
    <div className="sfa-heading"><div><small>MODEL OUTLOOK · NOT OBSERVED EXTENT</small><h3>Spill Forecast</h3></div><span>{horizon === 0 ? "OBSERVED NOW" : `+${horizon}H FORECAST`}</span></div>
    <div className="sfa-horizons">{horizons.map((hours) => <button key={hours} className={hours === horizon ? "active" : ""} onClick={() => onHorizonChange(hours)}>{hours === 0 ? "NOW" : `+${hours}H`}</button>)}</div>

    <div className="sfa-summary">
      <div><span>Expected movement</span><b>{horizon === 0 ? "—" : distance(selected?.drift_distance_m)}</b><small>Centroid displacement</small></div>
      <div><span>{horizon === 0 ? "Observed area" : "Predicted area"}</span><b>{area(selectedArea)}</b><small>{horizon === 0 ? "Current detected extent" : "Forecast footprint"}</small></div>
      <div><span>Spread radius</span><b>{horizon === 0 ? "—" : distance(selected?.spread_radius_m)}</b><small>Expansion around centroid</small></div>
      <div><span>Forecast confidence</span><b>{horizon === 0 ? "—" : pct(selected?.confidence)}</b><small>Model quality, not spill probability</small></div>
    </div>
    {!selected && horizon !== 0 && <div className="sfa-notice">No backend forecast is available for the selected horizon.</div>}

    <div className="sfa-explanation"><b>How is the spill moving?</b><p>{horizon === 0 ? "This is the currently observed spill extent. Select a forecast horizon to inspect predicted movement and expansion." : <>The selected model forecasts movement using the returned wind/current forcing. At +{horizon}h, backend-reported drift is <strong>{distance(selected?.drift_distance_m)}</strong> and the predicted footprint is <strong>{area(selectedArea)}</strong>. Direction: <strong>{String(direction || "Unavailable")}</strong>.</>}</p></div>

    <div className="sfa-chart-card"><div className="sfa-subhead"><div><b>Predicted spill area</b><span>Observed extent and backend forecast areas</span></div></div>{validAreas.length > 1 ? <><svg className="sfa-chart" viewBox="0 0 300 125" role="img" aria-label="Forecast spill area growth"><line x1="22" y1="104" x2="282" y2="104"/><line x1="22" y1="18" x2="22" y2="104"/><polyline points={chartPoints}/>{validAreas.map((p) => <g key={p.h}><circle cx={22 + (p.h/maxHorizon)*256} cy={104-(p.value/maxArea)*82} r="4"/><text x={22+(p.h/maxHorizon)*256} y="119" textAnchor="middle">{p.h ? `${p.h}h` : "Now"}</text></g>)}</svg><p>Forecast footprint change is shown only where the backend supplies an area.</p></> : <div className="sfa-empty">Area-growth visualization unavailable until forecast area values are returned.</div>}</div>

    <div className="sfa-drivers"><div className="sfa-subhead"><div><b>Environmental drivers</b><span>Forcing returned for the selected horizon</span></div></div><div><span><small>Wind</small><b>{speed(windSpeedKmh, "km/h")}</b><em>{compass(windDirection)}</em></span><span><small>Current</small><b>{speed(currentSpeed, "m/s")}</b><em>{compass(currentDirection)}</em></span><span><small>Wind leeway</small><b>{windLeeway === undefined ? "Unavailable" : pct(windLeeway)}</b><em>surface contribution</em></span><span><small>Current contribution</small><b>{currentContribution === undefined ? "Unavailable" : pct(currentContribution)}</b><em>advection forcing</em></span></div></div>

    <div className="sfa-model"><div><small>ACTIVE FORECAST MODEL</small><b>{modelName}</b></div><ul>{ensemble ? <><li>Time-varying wind/current forcing</li><li>Particle advection</li><li>Random-walk turbulent diffusion</li></> : <><li>Current and wind-driven drift</li><li>Diffusion-based footprint spread</li><li>Analytic fallback when forcing samples are sparse</li></>}<li>Model confidence decreases with horizon</li></ul></div>

    <details className="sfa-details"><summary>Why does the area increase?</summary><p>The model does not assume a fixed-size shape. Longer forecast time allows more turbulent diffusion or dispersion, creating a larger and less certain predicted footprint.</p><div><span>Longer forecast time</span><i>↓</i><span>More uncertainty + diffusion</span><i>↓</i><span>Larger predicted footprint</span></div><p>{ensemble ? "The particle ensemble advects particles using current and wind forcing, then disperses them through a random-walk diffusion component." : "The analytic model expands the footprint using its diffusion formulation."}</p></details>
    <details className="sfa-details"><summary>Why does confidence decrease?</summary><p>Longer horizons introduce greater uncertainty because future wind, current, and turbulent-dispersion conditions can change. Forecast confidence describes model quality; it is not the probability that the spill exists.</p></details>

    <details className="sfa-table"><summary>View all forecast metrics</summary><div><table><thead><tr><th>Horizon</th><th>Drift distance</th><th>Area</th><th>Spread radius</th><th>Confidence</th></tr></thead><tbody><tr className="observed"><td>Now · observed</td><td>—</td><td>{area(incident.area_km2)}</td><td>—</td><td>—</td></tr>{forecasts.map((row: any) => <tr key={row.horizon_hours}><td>+{Number(row.horizon_hours)}h</td><td>{distance(row.drift_distance_m)}</td><td>{area(forecastArea(row))}</td><td>{distance(row.spread_radius_m)}</td><td>{pct(row.confidence)}</td></tr>)}</tbody></table></div></details>
    <div className="sfa-provenance">Forecast values are displayed directly from the backend. The frontend does not estimate drift distance, polygon area, or spread radius. “Now” is observed evidence; future horizons are model predictions.</div>
  </section>;
}
