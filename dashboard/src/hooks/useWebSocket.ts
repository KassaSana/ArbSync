import { useEffect, useRef, useState } from "react";
import { WS_BASE } from "../api/client";

export type ConnectionStatus = "connecting" | "connected" | "reconnecting";

// The socket delivers `MessageEvent<any>`; the handler decides whether a
// frame's data is the text it expects.
type MessageHandler = (event: MessageEvent<unknown>, connectionId: number) => void;

type WebSocketState = {
  status: ConnectionStatus;
  connectionId: number;
};

const MAX_RETRY_DELAY_MS = 30_000;
const STABLE_CONNECTION_MS = 5_000;

export function useWebSocket(handler: MessageHandler): WebSocketState {
  const handlerRef = useRef(handler);
  const [state, setState] = useState<WebSocketState>({
    status: "connecting",
    connectionId: 0,
  });

  useEffect(() => {
    handlerRef.current = handler;
  }, [handler]);

  useEffect(() => {
    let active = true;
    let socket: WebSocket | null = null;
    let retryTimer: number | null = null;
    let stableTimer: number | null = null;
    let retryAttempt = 0;
    let connectionId = 0;

    const clearTimer = (timer: number | null) => {
      if (timer !== null) {
        window.clearTimeout(timer);
      }
    };

    const connect = () => {
      if (!active) {
        return;
      }
      connectionId += 1;
      const thisConnection = connectionId;
      const thisSocket = new WebSocket(`${WS_BASE}/ws/live`);
      socket = thisSocket;

      thisSocket.onopen = () => {
        if (!active || socket !== thisSocket || thisConnection !== connectionId) {
          return;
        }
        setState({ status: "connected", connectionId: thisConnection });
        thisSocket.send("dashboard_connected");
        clearTimer(stableTimer);
        stableTimer = window.setTimeout(() => {
          if (active && thisConnection === connectionId) {
            retryAttempt = 0;
          }
        }, STABLE_CONNECTION_MS);
      };

      thisSocket.onmessage = (event: MessageEvent<unknown>) => {
        if (active && socket === thisSocket && thisConnection === connectionId) {
          handlerRef.current(event, thisConnection);
        }
      };

      thisSocket.onclose = () => {
        if (!active || socket !== thisSocket || thisConnection !== connectionId) {
          return;
        }
        clearTimer(stableTimer);
        stableTimer = null;
        socket = null;
        setState({ status: "reconnecting", connectionId: thisConnection });
        const exponentialDelay = Math.min(MAX_RETRY_DELAY_MS, 1_000 * 2 ** retryAttempt);
        const jitteredDelay = Math.min(
          MAX_RETRY_DELAY_MS,
          exponentialDelay * (0.8 + Math.random() * 0.4),
        );
        retryAttempt += 1;
        retryTimer = window.setTimeout(connect, jitteredDelay);
      };

      thisSocket.onerror = () => {
        if (active && socket === thisSocket && thisConnection === connectionId) {
          thisSocket.close();
        }
      };
    };

    connect();
    return () => {
      active = false;
      clearTimer(retryTimer);
      clearTimer(stableTimer);
      if (socket !== null) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onclose = null;
        socket.onerror = null;
        socket.close();
      }
    };
  }, []);

  return state;
}
