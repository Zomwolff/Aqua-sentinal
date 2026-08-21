import { useEffect, useRef, useState } from "react";
import { liveUrl } from "../api/gateway";
import { reconnectDelay } from "../utils/format";

export type LiveStatus = "connecting" | "live" | "disconnected";

export function useLiveUpdates(onEvent: (event: any) => void) {
  const [status, setStatus] = useState<LiveStatus>("connecting");
  const callback = useRef(onEvent);
  callback.current = onEvent;
  useEffect(() => {
    let socket: WebSocket | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    let attempt = 0;
    const connect = () => {
      if (stopped) return;
      setStatus(attempt ? "disconnected" : "connecting");
      socket = new WebSocket(liveUrl());
      socket.onopen = () => { attempt = 0; setStatus("live"); };
      socket.onmessage = (message) => {
        try { const event = JSON.parse(message.data); if (event.type !== "heartbeat") callback.current(event); } catch { /* Ignore malformed event envelopes. */ }
      };
      socket.onclose = () => {
        if (stopped) return;
        setStatus("disconnected");
        timer = setTimeout(connect, reconnectDelay(attempt++));
      };
      socket.onerror = () => socket?.close();
    };
    connect();
    return () => { stopped = true; if (timer) clearTimeout(timer); socket?.close(); };
  }, []);
  return status;
}
