import { useEffect, useState } from "react";

export type FeedItem = { time: string; kind: "spill" | "risk" | "dark" | "system" | "sar"; title: string; body: string };

export function useLiveFeeds(historicalFeed: FeedItem[] = []) {
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [liveEvent, setLiveEvent] = useState<any>(null);

  useEffect(() => {
    if (historicalFeed.length > 0 && feed.length === 0) {
      setFeed(historicalFeed);
    }
  }, [historicalFeed]);

  useEffect(() => {
    let ws: WebSocket;
    let reconnectTimeout: ReturnType<typeof setTimeout>;
    let isSubscribed = true;

    function connect() {
      const wsUrl = `ws://${window.location.hostname}:8015/live`;
      ws = new WebSocket(wsUrl);
      
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
        if (isSubscribed) reconnectTimeout = setTimeout(connect, 3000);
      };
    }

    connect();

    return () => {
      isSubscribed = false;
      if (ws) ws.close();
      clearTimeout(reconnectTimeout);
    };
  }, []);

  return { feed, liveEvent };
}
