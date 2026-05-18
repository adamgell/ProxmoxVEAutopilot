import type { JobsLivePayload, LiveSocketMessage } from "./contracts";

export function liveSocketUrl(origin: string = window.location.origin): string {
  const url = new URL("/api/live/ws", origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

export function parseLiveSocketMessage(raw: string): LiveSocketMessage | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    return parsed !== null && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

export function jobsSubscribeMessage(): string {
  return JSON.stringify({ type: "subscribe", topics: ["jobs"] });
}

export function isJobsLiveMessage(message: LiveSocketMessage | null): message is LiveSocketMessage & {
  readonly topic: "jobs";
  readonly type: "snapshot" | "patch";
  readonly data: JobsLivePayload;
} {
  return (
    message?.topic === "jobs" &&
    (message.type === "snapshot" || message.type === "patch") &&
    message.data !== null &&
    typeof message.data === "object"
  );
}

interface JobsLiveOptions {
  readonly onPayload: (payload: JobsLivePayload) => void;
  readonly onState?: (state: "connecting" | "open" | "closed") => void;
  readonly origin?: string;
  readonly retryMs?: number;
}

export function connectJobsLive({
  onPayload,
  onState,
  origin,
  retryMs = 3000
}: JobsLiveOptions): () => void {
  if (typeof WebSocket === "undefined") {
    onState?.("closed");
    return () => undefined;
  }

  let closedByClient = false;
  let retryTimer: number | undefined;
  let socket: WebSocket | undefined;

  const connect = () => {
    onState?.("connecting");
    socket = new WebSocket(liveSocketUrl(origin));
    socket.addEventListener("open", () => {
      onState?.("open");
      socket?.send(jobsSubscribeMessage());
    });
    socket.addEventListener("message", (event: MessageEvent) => {
      if (typeof event.data !== "string") {
        return;
      }
      const message = parseLiveSocketMessage(event.data);
      if (isJobsLiveMessage(message)) {
        onPayload(message.data);
      }
    });
    socket.addEventListener("close", () => {
      onState?.("closed");
      if (!closedByClient) {
        retryTimer = window.setTimeout(connect, retryMs);
      }
    });
    socket.addEventListener("error", () => {
      socket?.close();
    });
  };

  connect();

  return () => {
    closedByClient = true;
    if (retryTimer !== undefined) {
      window.clearTimeout(retryTimer);
    }
    socket?.close();
  };
}
