import type { LiveSocketMessage } from "./contracts";

export function liveSocketUrl(origin: string = window.location.origin): string {
  const url = new URL("/api/live/ws", origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

export function parseLiveSocketMessage(raw: string): LiveSocketMessage | null {
  try {
    return JSON.parse(raw) as LiveSocketMessage;
  } catch {
    return null;
  }
}
