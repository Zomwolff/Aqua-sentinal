import { useEffect, useState } from "react";

export type FeedItem = { time: string; kind: "spill" | "risk" | "dark" | "system"; title: string; body: string };

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
      ws = new WebSocket("ws://localhost:8015/live");
      
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
             // To prevent flooding the feed, we only create a feed item occasionally,
             // but we always pass the liveEvent down so App.tsx can animate the map.
             if (Math.random() < 0.05) { // roughly 1 in 20 UI-received AIS pings gets a feed item
                 newFeedItem = { time: timeStr, kind: "system", title: "Live AIS Ingestion", body: `Processing telemetry for MMSI ${data.data?.mmsi}` };
             }
          }

          if (newFeedItem) {
            setFeed(prev => {
              // De-duplicate "Live AIS Ingestion" messages to keep the feed clean
              if (newFeedItem!.title === "Live AIS Ingestion" && prev.length > 0 && prev[0].title === "Live AIS Ingestion") {
                 return prev;
              }
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
