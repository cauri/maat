"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Subscribe to the console's live event stream (SSE) with automatic, backed-off
 * reconnection. One instance lives in the shell; the live indicator and the Audit
 * drawer both read it. Until the #304 endpoint exists the stream reports "offline"
 * and keeps retrying on a long interval — the wiring is complete and lights up the
 * moment the API lands.
 *
 * Status is only ever set from subscription callbacks (the React-endorsed pattern for
 * effects that sync with an external system); the "no URL ⇒ idle" case is derived at
 * render, so nothing is set synchronously in the effect body.
 */

export type StreamStatus = "idle" | "connecting" | "live" | "offline";

export interface ConsoleEvent {
  /** Stable client-side key (monotonic). */
  key: number;
  /** Event type, e.g. `admin.threshold.changed`. */
  type: string;
  /** Who emitted it, when known. */
  actor?: string;
  /** Epoch milliseconds (server-provided, else client receive time). */
  ts: number;
  /** Parsed payload, when the frame was JSON. */
  data?: Record<string, unknown>;
  /** The raw frame, always kept. */
  raw: string;
}

const MAX_EVENTS = 200;
const BACKOFF_MS = [1000, 2000, 5000, 10000, 30000];

function parseEvent(raw: string, key: number): ConsoleEvent {
  try {
    const obj = JSON.parse(raw) as Record<string, unknown>;
    const type = typeof obj.type === "string" ? obj.type : "event";
    const actor =
      typeof obj.actor === "string"
        ? obj.actor
        : typeof obj.email === "string"
          ? obj.email
          : undefined;
    const ts =
      typeof obj.ts === "number" ? obj.ts : typeof obj.at === "number" ? obj.at : Date.now();
    return { key, type, actor, ts, data: obj, raw };
  } catch {
    return { key, type: "event", ts: Date.now(), raw };
  }
}

export function useEventStream(
  url: string | null,
  options: { max?: number } = {},
): { status: StreamStatus; events: ConsoleEvent[]; clear: () => void } {
  const max = options.max ?? MAX_EVENTS;
  // Internal connection status; "connecting" is the natural starting point once a URL is set.
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [events, setEvents] = useState<ConsoleEvent[]>([]);
  const attempt = useRef(0);
  const seq = useRef(0);

  const clear = useCallback(() => setEvents([]), []);

  useEffect(() => {
    if (!url) return;
    let closed = false;
    let source: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const scheduleReconnect = () => {
      if (closed) return;
      const delay = BACKOFF_MS[Math.min(attempt.current, BACKOFF_MS.length - 1)];
      attempt.current += 1;
      timer = setTimeout(connect, delay);
    };

    function connect() {
      if (closed) return;
      try {
        source = new EventSource(url as string, { withCredentials: true });
      } catch {
        setStatus("offline");
        scheduleReconnect();
        return;
      }
      source.onopen = () => {
        attempt.current = 0;
        setStatus("live");
      };
      source.onmessage = (ev: MessageEvent<string>) => {
        seq.current += 1;
        const parsed = parseEvent(ev.data, seq.current);
        setEvents((prev) => [parsed, ...prev].slice(0, max));
      };
      source.onerror = () => {
        source?.close();
        source = null;
        setStatus("offline");
        scheduleReconnect();
      };
    }

    connect();
    return () => {
      closed = true;
      if (timer) clearTimeout(timer);
      source?.close();
    };
  }, [url, max]);

  return { status: url ? status : "idle", events, clear };
}
