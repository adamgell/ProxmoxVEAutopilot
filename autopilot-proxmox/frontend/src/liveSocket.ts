import type { AgentFleetRow, FleetLivePayload, JobsLivePayload, LiveSocketMessage, VmFleetRow } from "./contracts";

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

export function fleetSubscribeMessage(): string {
  return JSON.stringify({ type: "subscribe", topics: ["fleet", "agents"] });
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

export function isFleetLiveMessage(message: LiveSocketMessage | null): message is LiveSocketMessage & {
  readonly topic: "fleet";
  readonly type: "snapshot" | "patch";
  readonly data?: FleetLivePayload;
  readonly rows?: readonly VmFleetRow[];
} {
  return (
    message?.topic === "fleet" &&
    (message.type === "snapshot" || message.type === "patch") &&
    ((message.data !== null && typeof message.data === "object") || Array.isArray(message.rows))
  );
}

export function isAgentsLiveMessage(message: LiveSocketMessage | null): message is LiveSocketMessage & {
  readonly topic: "agents";
  readonly type: "snapshot" | "patch";
  readonly data?: { readonly agents?: readonly AgentFleetRow[] };
  readonly agents?: readonly AgentFleetRow[];
} {
  return (
    message?.topic === "agents" &&
    (message.type === "snapshot" || message.type === "patch") &&
    ((message.data !== null && typeof message.data === "object") || Array.isArray(message.agents))
  );
}

function isVmFleetRow(value: unknown): value is VmFleetRow {
  if (value === null || typeof value !== "object") {
    return false;
  }
  return typeof (value as { readonly vmid?: unknown }).vmid === "number";
}

function isAgentFleetRow(value: unknown): value is AgentFleetRow {
  if (value === null || typeof value !== "object") {
    return false;
  }
  return typeof (value as { readonly agent_id?: unknown }).agent_id === "string";
}

export function fleetRowsFromMessage(message: LiveSocketMessage): readonly VmFleetRow[] {
  const rows = message.rows;
  if (Array.isArray(rows)) {
    return rows.filter(isVmFleetRow);
  }
  const data = message.data as FleetLivePayload | undefined;
  const dataRows: unknown = data?.rows;
  return Array.isArray(dataRows) ? dataRows.filter(isVmFleetRow) : [];
}

export function agentsFromMessage(message: LiveSocketMessage): readonly AgentFleetRow[] {
  const agents = message.agents;
  if (Array.isArray(agents)) {
    return agents.filter(isAgentFleetRow);
  }
  const data = message.data as { readonly agents?: readonly AgentFleetRow[] } | undefined;
  const dataAgents: unknown = data?.agents;
  return Array.isArray(dataAgents) ? dataAgents.filter(isAgentFleetRow) : [];
}

interface JobsLiveOptions {
  readonly onPayload: (payload: JobsLivePayload) => void;
  readonly onState?: (state: "connecting" | "open" | "closed") => void;
  readonly origin?: string;
  readonly retryMs?: number;
}

interface FleetLiveOptions {
  readonly onFleetRows: (rows: readonly VmFleetRow[], replace: boolean) => void;
  readonly onAgents: (agents: readonly AgentFleetRow[]) => void;
  readonly onEvent?: (message: LiveSocketMessage) => void;
  readonly onSendReady?: (send: (message: Readonly<Record<string, unknown>>) => boolean) => void;
  readonly onState?: (state: "connecting" | "open" | "closed") => void;
  readonly origin?: string;
  readonly retryMs?: number;
}

export function connectFleetLive({
  onFleetRows,
  onAgents,
  onEvent,
  onSendReady,
  onState,
  origin,
  retryMs = 3000
}: FleetLiveOptions): () => void {
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
    onSendReady?.((message) => {
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        return false;
      }
      socket.send(JSON.stringify(message));
      return true;
    });
    socket.addEventListener("open", () => {
      onState?.("open");
      socket?.send(fleetSubscribeMessage());
    });
    socket.addEventListener("message", (event: MessageEvent) => {
      if (typeof event.data !== "string") {
        return;
      }
      const message = parseLiveSocketMessage(event.data);
      if (isFleetLiveMessage(message)) {
        onFleetRows(fleetRowsFromMessage(message), message.type === "snapshot");
        return;
      }
      if (isAgentsLiveMessage(message)) {
        onAgents(agentsFromMessage(message));
        return;
      }
      if (message?.type === "event" || message?.type === "screenshot.result" || message?.type === "error") {
        onEvent?.(message);
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
