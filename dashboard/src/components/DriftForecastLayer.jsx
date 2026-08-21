import { useEffect, useState } from "react";
import { GeoJSON } from "react-leaflet";
import { getSpillForecast } from "../lib/apiClient";

export function DriftForecastLayer({ selectedSpillId }) {
  const [forecasts, setForecasts] = useState([]);

  useEffect(() => {
    let mounted = true;
    if (!selectedSpillId) {
      setForecasts([]);
      return;
    }
    const fetchForecasts = async () => {
      try {
        const res = await getSpillForecast(selectedSpillId);
        if (mounted && res.horizons) setForecasts(res.horizons);
      } catch (e) {
        console.error("Failed to fetch spill forecast", e);
      }
    };
    fetchForecasts();
    return () => {
      mounted = false;
    };
  }, [selectedSpillId]);

  return forecasts.map((f, idx) => {
    if (!f.geometry) return null;
    const opacity = Math.max(0.1, 0.4 - (idx * 0.1));
    return (
      <GeoJSON
        key={`${selectedSpillId}-${f.horizon_hours}`}
        data={f.geometry}
        style={() => ({
          color: "#0284c7", // blue-600
          weight: 1,
          fillColor: "#0ea5e9", // blue-500
          fillOpacity: opacity,
          dashArray: "4 4",
        })}
      />
    );
  });
}
