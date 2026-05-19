import { useCallback, useEffect, useRef, useState } from "react";

import { fetchJson } from "../apiClient";
import type { VmFleetRow } from "../contracts";
import { fallbackText, statusClass, statusLabel, vmDisplayName } from "../viewModels";

export type VmActionMode = "console" | "screenshot";

export interface VmActionSelection {
  readonly mode: VmActionMode;
  readonly vm: VmFleetRow;
}

export type ScreenshotWorkspaceState =
  | { readonly status: "idle" }
  | { readonly status: "requesting"; readonly vmid: number; readonly correlationId: string; readonly message: string }
  | { readonly status: "ready"; readonly vmid: number; readonly imageUrl: string; readonly message: string; readonly correlationId?: string }
  | { readonly status: "failed"; readonly vmid?: number; readonly message: string; readonly correlationId?: string; readonly imageUrl?: string };

interface VncInitResponse {
  readonly node?: string;
  readonly vmid?: number;
  readonly port?: number | string;
  readonly ticket?: string;
  readonly user?: string;
  readonly error?: string;
}

interface VncTicket extends VncInitResponse {
  readonly port: number | string;
  readonly ticket: string;
}

type ConsoleState =
  | { readonly status: "idle"; readonly message: string }
  | { readonly status: "connecting"; readonly message: string }
  | { readonly status: "open"; readonly message: string }
  | { readonly status: "failed"; readonly message: string };

export function VmActionWorkspace({
  selection,
  screenshot,
  socketState,
  onModeChange,
  onRequestScreenshot,
  onClose
}: {
  readonly selection: VmActionSelection | null;
  readonly screenshot: ScreenshotWorkspaceState;
  readonly socketState: string;
  readonly onModeChange: (mode: VmActionMode) => void;
  readonly onRequestScreenshot: (vm: VmFleetRow) => void;
  readonly onClose: () => void;
}) {
  const vm = selection?.vm ?? null;
  const mode = selection?.mode ?? "console";

  return (
    <aside className="vm-action-workspace" role="region" aria-label="VM action workspace">
      <header className="vm-action-workspace__head">
        <div>
          <span className="eyebrow">Action</span>
          <h2>{vm ? `VM ${String(vm.vmid)} action` : "VM action"}</h2>
        </div>
        {vm ? <button type="button" className="fleet-action" onClick={onClose}>Clear</button> : null}
      </header>

      {vm ? (
        <>
          <div className="vm-action-workspace__target">
            <span className={statusClass(vm.status)}>{statusLabel(vm.status)}</span>
            <strong>{vmDisplayName(vm)}</strong>
            <span>{fallbackText(vm.ip_address)}</span>
          </div>
          <div className="vm-action-tabs" role="tablist" aria-label={`VM ${String(vm.vmid)} actions`}>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "console"}
              className={mode === "console" ? "is-active" : ""}
              onClick={() => {
                onModeChange("console");
              }}
            >
              Console
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "screenshot"}
              className={mode === "screenshot" ? "is-active" : ""}
              onClick={() => {
                onModeChange("screenshot");
              }}
            >
              Screenshot
            </button>
          </div>

          {mode === "console" ? <VmConsolePanel vm={vm} /> : null}
          {mode === "screenshot" ? (
            <VmScreenshotPanel
              vm={vm}
              screenshot={screenshot}
              socketState={socketState}
              onRequestScreenshot={onRequestScreenshot}
            />
          ) : null}
        </>
      ) : (
        <div className="vm-action-empty">
          <h3>Choose a VM action</h3>
          <p>Console and screenshots open here.</p>
        </div>
      )}
    </aside>
  );
}

function VmConsolePanel({ vm }: { readonly vm: VmFleetRow }) {
  const screenRef = useRef<HTMLDivElement | null>(null);
  const rfbRef = useRef<import("@novnc/novnc").default | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [consoleState, setConsoleState] = useState<ConsoleState>({
    status: "idle",
    message: "Console idle"
  });

  useEffect(() => {
    const target = screenRef.current;
    if (!target) {
      return () => undefined;
    }

    let cancelled = false;
    let localRfb: import("@novnc/novnc").default | null = null;

    const start = async () => {
      setConsoleState({ status: "connecting", message: "Requesting VNC ticket" });
      target.replaceChildren();
      try {
        const ticket = await fetchJson<VncInitResponse>(`/api/vms/${String(vm.vmid)}/vnc-init`);
        if (cancelled) {
          return;
        }
        if (ticket.error || !ticket.ticket || ticket.port === undefined) {
          setConsoleState({ status: "failed", message: ticket.error || "VNC ticket unavailable" });
          return;
        }
        const vncTicket: VncTicket = { ...ticket, port: ticket.port, ticket: ticket.ticket };

        const { default: RFB } = await import("@novnc/novnc");
        localRfb = new RFB(target, vncWebSocketUrl(vm.vmid, vncTicket), {
          credentials: { password: vncTicket.ticket },
          wsProtocols: ["binary"]
        });
        localRfb.viewOnly = false;
        localRfb.scaleViewport = true;
        localRfb.resizeSession = false;
        localRfb.addEventListener("connect", () => {
          setConsoleState({ status: "open", message: `Connected ${vncTicket.user ? `/ ${vncTicket.user}` : ""}`.trim() });
        });
        localRfb.addEventListener("disconnect", (event) => {
          setConsoleState({ status: "failed", message: `Disconnected: ${eventReason(event)}` });
        });
        localRfb.addEventListener("credentialsrequired", () => {
          localRfb?.sendCredentials({ password: vncTicket.ticket });
        });
        localRfb.addEventListener("securityfailure", (event) => {
          setConsoleState({ status: "failed", message: `VNC auth failed: ${eventReason(event)}` });
        });
        rfbRef.current = localRfb;
        setConsoleState({ status: "connecting", message: "Opening console websocket" });
      } catch (error) {
        if (!cancelled) {
          setConsoleState({ status: "failed", message: error instanceof Error ? error.message : "Console failed" });
        }
      }
    };

    void start();

    return () => {
      cancelled = true;
      rfbRef.current = null;
      localRfb?.disconnect();
      target.replaceChildren();
    };
  }, [attempt, vm.vmid]);

  const reconnect = useCallback(() => {
    setAttempt((current) => current + 1);
  }, []);

  return (
    <section className="vm-console-panel" aria-label={`VM ${String(vm.vmid)} console`}>
      <div className="vm-action-status">
        <span className={`status status--${consoleState.status === "open" ? "good" : consoleState.status === "failed" ? "bad" : "active"}`}>
          {consoleState.status}
        </span>
        <span>{consoleState.message}</span>
      </div>
      <div className="vm-console-toolbar">
        <button type="button" className="fleet-action" onClick={() => { rfbRef.current?.focus(); }}>Focus</button>
        <button type="button" className="fleet-action" onClick={() => { rfbRef.current?.sendCtrlAltDel(); }}>CAD</button>
        <button type="button" className="fleet-action" onClick={reconnect}>Reconnect</button>
        <a className="action-link" href={`/vms/${String(vm.vmid)}/console`}>Open legacy console</a>
      </div>
      <div ref={screenRef} className="vm-console-screen" aria-label={`VM ${String(vm.vmid)} console screen`} />
    </section>
  );
}

function VmScreenshotPanel({
  vm,
  screenshot,
  socketState,
  onRequestScreenshot
}: {
  readonly vm: VmFleetRow;
  readonly screenshot: ScreenshotWorkspaceState;
  readonly socketState: string;
  readonly onRequestScreenshot: (vm: VmFleetRow) => void;
}) {
  const relevant = screenshot.status !== "idle" && (screenshot.vmid === undefined || screenshot.vmid === vm.vmid);
  const isRequesting = relevant && screenshot.status === "requesting";
  const imageUrl = relevant && (screenshot.status === "ready" || screenshot.status === "failed") ? screenshot.imageUrl : undefined;
  const message = relevant ? screenshot.message : "Ready";

  return (
    <section className="vm-screenshot-panel" aria-label={`VM ${String(vm.vmid)} screenshot`}>
      <header>
        <div>
          <h3>Screenshot</h3>
          <p>Live socket: {socketState}</p>
        </div>
        <button type="button" className="fleet-action" onClick={() => { onRequestScreenshot(vm); }}>
          {imageUrl ? "Refresh" : "Capture"}
        </button>
      </header>
      {isRequesting ? <div className="progress progress--compact" aria-label="Screenshot loading"><span /></div> : null}
      <div className="vm-action-status">
        <span className={`status status--${screenshotTone(screenshot.status)}`}>{relevant ? screenshot.status : "idle"}</span>
        <span>{message}</span>
      </div>
      {imageUrl ? (
        <>
          <img className="vm-screenshot-image" src={imageUrl} alt={`VM ${String(vm.vmid)} screenshot`} />
          <div className="vm-console-toolbar">
            <a className="action-link" href={imageUrl} download={`vm-${String(vm.vmid)}-screenshot.png`}>Download</a>
            <a className="action-link" href={`/vms/${String(vm.vmid)}/console`}>Open legacy console</a>
          </div>
        </>
      ) : (
        <div className="vm-action-empty vm-action-empty--compact">
          <p>Capture opens here.</p>
        </div>
      )}
    </section>
  );
}

function vncWebSocketUrl(vmid: number, ticket: VncTicket): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams();
  params.set("port", String(ticket.port));
  params.set("vncticket", ticket.ticket);
  if (ticket.node) {
    params.set("node", ticket.node);
  }
  return `${protocol}//${window.location.host}/api/vms/${String(vmid)}/vnc-ws?${params.toString()}`;
}

function eventReason(event: Event): string {
  const detail = (event as CustomEvent<{ readonly reason?: unknown }>).detail;
  return typeof detail.reason === "string" && detail.reason.trim() ? detail.reason : "closed";
}

function screenshotTone(status: ScreenshotWorkspaceState["status"]): "good" | "active" | "bad" | "neutral" {
  if (status === "ready") {
    return "good";
  }
  if (status === "requesting") {
    return "active";
  }
  if (status === "failed") {
    return "bad";
  }
  return "neutral";
}
