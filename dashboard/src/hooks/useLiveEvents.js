import { useEffect, useRef } from "react";

/**
 * Single WebSocket client to api-gateway `/live`.
 *
 * One connection is shared by all layers (vessels + spill candidates). Incoming
 * frames have the gateway envelope {type, stream, id, data, at}; the caller's
 * `onEvent` receives the whole envelope and routes by `stream`/`type`.
 * Reconnects with a short delay; no external library.
 */
function defaultWsUrl() {
  if (import.meta.env && import.meta.env.VITE_WS_URL) {
    return import.meta.env.VITE_WS_URL;
  }
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  // Development: dashboard runs on :3000 while api-gateway is on :8015.
  const port = window.location.port === "3000" ? "8015" : window.location.port;
  return `${proto}://${window.location.hostname}:${port}/live`;
}

export function useLiveEvents({ onEvent, enabled = true } = {}) {
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;

  useEffect(() => {
    if (!enabled) return undefined;
    let closed = false;
    let ws = null;
    let retry = null;
    let heartbeatTimer = null;

    const connect = () => {
      try {
        ws = new WebSocket(defaultWsUrl());
      } catch {
        scheduleRetry();
        return;
      }
      ws.onmessage = (message) => {
        try {
          const envelope = JSON.parse(message.data);
          if (envelope.type === "heartbeat") return;
          handlerRef.current?.(envelope);
        } catch {
          /* ignore non-JSON / malformed frames */
        }
      };
      ws.onclose = () => {
        if (!closed) scheduleRetry();
      };
      ws.onerror = () => {
        try {
          ws?.close();
        } catch {
          /* noop */
        }
      };
    };

    const scheduleRetry = () => {
      if (closed) return;
      clearTimeout(retry);
      retry = setTimeout(connect, 3000);
    };

    // Re-sync if a tab regains visibility.
    const onVisible = () => {
      if (document.visibilityState === "visible") {
        try {
          ws?.close();
        } catch {
          /* noop */
        }
        connect();
      }
    };
    document.addEventListener("visibilitychange", onVisible);

    connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      clearInterval(heartbeatTimer);
      document.removeEventListener("visibilitychange", onVisible);
      try {
        ws?.close();
      } catch {
        /* noop */
      }
    };
  }, [enabled]);

  return {};
}