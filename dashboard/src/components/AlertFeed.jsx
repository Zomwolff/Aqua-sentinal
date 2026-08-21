import { useEffect, useState } from "react";
import { getSeverityColor } from "../lib/spillIncidentHelpers";

export function AlertFeed({ event }) {
  const [alerts, setAlerts] = useState([]);

  useEffect(() => {
    if (!event) return;
    // We only care about anomalies, risks, and spill intelligence events
    if (["heartbeat", "sts"].includes(event.type)) return;

    setAlerts((prev) => {
      const newAlerts = [event, ...prev].slice(0, 50); // Keep last 50
      return newAlerts;
    });
  }, [event]);

  return (
    <div className="alert-feed">
      {alerts.length === 0 && <div className="empty-state">No active alerts...</div>}
      {alerts.map((alert) => (
        <AlertItem key={alert.id} alert={alert} />
      ))}
    </div>
  );
}

function AlertItem({ alert }) {
  const data = alert.data || {};
  let title = "Alert";
  let color = "#94a3b8";
  let message = "";

  if (alert.type === "anomaly") {
    title = `Anomaly: ${data.anomaly_type}`;
    color = getSeverityColor(data.severity);
    message = `Vessel ${data.mmsi} - ${data.severity} severity`;
  } else if (alert.type === "risk") {
    title = `Risk Tier Changed`;
    color = getSeverityColor(data.tier);
    message = `Vessel ${data.mmsi} is now ${data.tier} risk`;
  } else if (alert.type === "spill_attributed") {
    title = `Spill Attributed`;
    color = "#8b5cf6"; // purple
    message = `Spill ${data.spill_id?.substring(0,8)} attributed to Vessel ${data.top_vessel_mmsi} (Score: ${(data.top_score || 0).toFixed(2)})`;
  } else if (alert.type === "spill_severity") {
    title = `Spill Severity Scored`;
    color = getSeverityColor(data.severity_level);
    message = `Spill ${data.spill_id?.substring(0,8)} scored as ${data.severity_level}`;
  } else if (alert.type === "spill_response") {
    title = `Response Rules Generated`;
    color = "#3b82f6"; // blue
    message = `New recommendations generated for Spill ${data.spill_id?.substring(0,8)}`;
  } else if (alert.type === "incident_fused") {
    title = `Spill Incident Fused`;
    color = "#f59e0b"; // yellow
    message = `Spill ${data.candidate_id?.substring(0,8)} detected`;
  }

  return (
    <div className="alert-item" style={{ borderLeftColor: color }}>
      <div className="alert-title">{title}</div>
      <div className="alert-message">{message}</div>
      <div className="alert-time">
        {new Date(alert.at).toLocaleTimeString()}
      </div>
    </div>
  );
}
