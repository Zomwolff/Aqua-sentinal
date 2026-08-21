import { useEffect, useState } from "react";
import { getSpillIncidents } from "../lib/apiClient";
import { getSeverityColor } from "../lib/spillIncidentHelpers";

export function IncidentList({ onIncidentSelect }) {
  const [incidents, setIncidents] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    const fetchIncidents = async () => {
      try {
        const res = await getSpillIncidents();
        if (mounted) {
          setIncidents(res.incidents || []);
          setLoading(false);
        }
      } catch (e) {
        console.error("Failed to fetch incidents", e);
        if (mounted) setLoading(false);
      }
    };
    
    fetchIncidents();
    const interval = setInterval(fetchIncidents, 30000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, []);

  if (loading) return <div className="p-4">Loading incidents...</div>;
  if (incidents.length === 0) return <div className="p-4 text-gray-500">No recent incidents.</div>;

  return (
    <div className="incident-list">
      {incidents.map((inc) => (
        <div 
          key={inc.id} 
          className="incident-item"
          onClick={() => onIncidentSelect?.(inc.id)}
        >
          <div className="incident-header">
            <span className="incident-id">Spill {inc.id.substring(0, 8)}</span>
            <span 
              className="severity-badge"
              style={{ backgroundColor: getSeverityColor(inc.severity_level) }}
            >
              {inc.severity_level || "PENDING"}
            </span>
          </div>
          <div className="incident-details">
            <div>Detected: {new Date(inc.detected_at).toLocaleString()}</div>
            <div>Area: {inc.area_km2?.toFixed(2)} km²</div>
            {inc.top_vessel_mmsi && (
              <div>Source: Vessel {inc.top_vessel_mmsi}</div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
