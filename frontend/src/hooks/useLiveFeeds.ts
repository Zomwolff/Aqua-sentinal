import { useEffect, useState } from "react";

export type FeedItem = { time: string; kind: "spill" | "risk" | "dark" | "system" | "sar"; title: string; body: string };
export type ConnectionStatus = "connecting" | "connected" | "reconnecting" | "offline";

const RECONNECT_DELAY_MS = 3000;

export function useLiveFeeds(historicalFeed: FeedItem[] = []) {
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [liveEvent, setLiveEvent] = useState<any>(null);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");

  useEffect(() => {
    if (historicalFeed.length > 0 && feed.length === 0) {
      setFeed(historicalFeed);
    }
  }, [historicalFeed]);

  useEffect(() => {
    let ws: WebSocket;
    let reconnectTimeout: ReturnType<typeof setTimeout>;
    let isSubscribed = true;
    let closedPermanently = false;

    function connect() {
      if (closedPermanently) return;
      // Same-origin WebSocket through the reverse proxy (/live -> gateway).
      // Falls back to the explicit gateway port when opened without the proxy.
      const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const wsUrl = `${wsProto}//${window.location.host}/live`;
      setStatus((s) => (s === "connected" ? s : "connecting"));
      ws = new WebSocket(wsUrl);

      ws.onopen = () => {
        if (isSubscribed) setStatus("connected");
      };

      ws.onmessage = (event) => {
        if (!isSubscribed) return;
        try {
          const data = JSON.parse(event.data);
          if (data.type === "heartbeat") return;

          setLiveEvent(data);

          const timeStr = new Date(data.at || Date.now()).toLocaleTimeString("en-US", { hour12: false, timeZone: "UTC" });
          let newFeedItem: FeedItem | null = null;

          if (data.type === "anomaly") {
            newFeedItem = { time: timeStr, kind: "risk", title: `Anomaly: ${data.data?.anomaly_type}`, body: `Vessel ${data.data?.mmsi} - ${data.data?.severity} severity` };
          } else if (data.type === "risk") {
            if (data.data?.tier === "CRITICAL") {
               newFeedItem = { time: timeStr, kind: "system", title: "SAR Tasking Triggered", body: `Satellite tasked for CRITICAL vessel ${data.data?.mmsi}` };
            } else {
               newFeedItem = { time: timeStr, kind: "risk", title: "Risk Tier Changed", body: `Vessel ${data.data?.mmsi} is now ${data.data?.tier} risk` };
            }
          } else if (data.type === "spill_attributed") {
            newFeedItem = { time: timeStr, kind: "spill", title: "Spill Attributed", body: `Spill ${data.data?.spill_id?.substring(0,8)} attributed to Vessel ${data.data?.top_vessel_mmsi}` };
          } else if (data.type === "spill_severity") {
            newFeedItem = { time: timeStr, kind: "spill", title: "Spill Severity Scored", body: `Spill ${data.data?.spill_id?.substring(0,8)} scored as ${data.data?.severity_level}` };
          } else if (data.type === "spill_response") {
            newFeedItem = { time: timeStr, kind: "system", title: "Response Rules Generated", body: `Recommendations ready for Spill ${data.data?.spill_id?.substring(0,8)}` };
          } else if (data.type === "incident_fused") {
            newFeedItem = { time: timeStr, kind: "spill", title: "Spill Incident Fused", body: `Spill ${data.data?.candidate_id?.substring(0,8)} detected` };
          } else if (data.type === "ais") {
             // Always show AIS telemetry in the feed
             newFeedItem = { time: timeStr, kind: "system", title: "Live AIS Ingestion", body: `Processing telemetry for MMSI ${data.data?.mmsi}` };
          } else if (data.type === "ais_fetch") {
             const received = data.data?.received;
             const processed = data.data?.processed;
             const trigger = data.data?.trigger === "manual" ? "Manual fetch" : "Scheduled poll";
             newFeedItem = { time: timeStr, kind: "dark", title: "Live AIS Fetch Complete",
               body: Number(received) < 0
                 ? (data.data?.detail || "Continuous stream active")
                 : `${trigger}: ${received ?? "?"} positions received, ${processed ?? "?"} ingested` };
          } else if (data.type === "dark_vessel") {
             newFeedItem = { time: timeStr, kind: "dark", title: "Dark Vessel Detected",
               body: data.data?.matched_mmsi ? `Vessel ${data.data.matched_mmsi} went dark` : `Unidentified target at ${Number(data.data?.latitude || 0).toFixed(2)}N ${Number(data.data?.longitude || 0).toFixed(2)}E` };
          } else if (data.type === "system") {
             newFeedItem = { time: timeStr, kind: "system", title: "System Event", body: data.data?.detail || data.data?.event || "" };
          } else if (data.type === "sts") {
             newFeedItem = { time: timeStr, kind: "dark", title: "STS Encounter", body: `Vessels ${data.data?.vessel_a_mmsi} ↔ ${data.data?.vessel_b_mmsi}` };
          } else if (data.type === "sar_tasking") {
             const stepMap: Record<string, string> = {
               sar_tasking: "Acquiring SAR",
               sar_fetching: "Downloading Scene",
               sar_despeckling: "Despeckling Filter",
               sar_cfar: "CFAR Object Detection",
               sar_morphology: "Morphological Cleaning",
               sar_polygonize: "Polygon Extraction",
               sar_complete: "Spill Processing Complete"
             };
             const stepTitle = stepMap[data.data?.step] || "SAR Processing";
             newFeedItem = { time: timeStr, kind: "sar", title: stepTitle, body: `Scene ${data.data?.scene_id?.substring(0,8)}` };
          }

          if (newFeedItem) {
            setFeed(prev => {
              return [newFeedItem!, ...prev].slice(0, 50);
            });
          }
        } catch (e) {
          console.error("Failed to parse websocket message", e);
        }
      };

      ws.onclose = () => {
        if (!isSubscribed) return;
        setStatus("reconnecting");
        reconnectTimeout = setTimeout(connect, RECONNECT_DELAY_MS);
      };

      ws.onerror = () => {
        try { ws.close(); } catch {}
      };
    }

    connect();

    return () => {
      isSubscribed = false;
      closedPermanently = true;
      if (ws) ws.close();
      clearTimeout(reconnectTimeout);
    };
  }, []);

  return { feed, liveEvent, connectionStatus: status };
}
