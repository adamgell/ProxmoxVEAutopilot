import { useCallback, useEffect, useRef, useState } from "react";
import { Clipboard, Eye, EyeOff, Keyboard, KeyRound, Send, User } from "lucide-react";

import { fetchJson, postJson } from "../apiClient";
import type { VmCredentialsRevealResponse, VmDetailEvidenceResponse, VmFleetRow, VmKnownCredential, VmRevealedCredential } from "../contracts";
import { fallbackText, statusClass, statusLabel, vmDisplayName } from "../viewModels";

export type VmActionMode = "console" | "screenshot";

export interface VmActionSelection {
  readonly mode: VmActionMode;
  readonly vm: VmFleetRow;
}

type WorkspaceLayout = "rail" | "expanded" | "minimized";

interface WorkspaceLayoutState {
  readonly layout: WorkspaceLayout;
  readonly vmid?: number;
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

type ConsoleControlState =
  | { readonly status: "idle"; readonly message: string }
  | { readonly status: "working"; readonly message: string }
  | { readonly status: "ready"; readonly message: string }
  | { readonly status: "failed"; readonly message: string };

interface VmTypeResponse {
  readonly ok?: boolean;
  readonly sent?: number;
  readonly skipped?: readonly string[];
  readonly error?: string;
}

interface VmKeyResponse {
  readonly ok?: boolean;
  readonly key?: string;
  readonly error?: string;
}

interface VmPowerResponse {
  readonly ok?: boolean;
  readonly action?: string;
  readonly error?: string;
}

type BrowserClipboard = {
  readonly readText?: () => Promise<string>;
  readonly writeText?: (value: string) => Promise<void>;
};

function browserClipboard(): BrowserClipboard | undefined {
  return typeof navigator === "undefined" ? undefined : navigator.clipboard;
}

function withoutCredentialKey(current: Readonly<Record<string, string>>, key: string): Record<string, string> {
  return Object.fromEntries(Object.entries(current).filter(([entryKey]) => entryKey !== key));
}

export function VmActionWorkspace({
  selection,
  evidence,
  screenshot,
  socketState,
  onModeChange,
  onRequestScreenshot,
  onClose
}: {
  readonly selection: VmActionSelection | null;
  readonly evidence?: VmDetailEvidenceResponse | null;
  readonly screenshot: ScreenshotWorkspaceState;
  readonly socketState: string;
  readonly onModeChange: (mode: VmActionMode) => void;
  readonly onRequestScreenshot: (vm: VmFleetRow) => void;
  readonly onClose: () => void;
}) {
  const vm = selection?.vm ?? null;
  const mode = selection?.mode ?? "console";
  const [layoutState, setLayoutState] = useState<WorkspaceLayoutState>({ layout: "rail" });
  const selectedVmid = vm?.vmid;
  const layout = layoutState.vmid === selectedVmid ? layoutState.layout : "rail";

  const setWorkspaceLayout = useCallback((nextLayout: WorkspaceLayout) => {
    setLayoutState(selectedVmid === undefined ? { layout: nextLayout } : { layout: nextLayout, vmid: selectedVmid });
  }, [selectedVmid]);

  const expanded = layout === "expanded";
  const minimized = layout === "minimized";
  const workspaceClass = [
    "vm-action-workspace",
    expanded ? "vm-action-workspace--expanded" : "",
    minimized ? "vm-action-workspace--minimized" : ""
  ].filter(Boolean).join(" ");

  return (
    <aside className={workspaceClass} role="region" aria-label="VM action workspace">
      <header className="vm-action-workspace__head">
        <div>
          <span className="eyebrow">Action</span>
          <h2>{vm ? `VM ${String(vm.vmid)} action` : "VM action"}</h2>
        </div>
        {vm ? (
          <div className="vm-action-window-controls" aria-label="Action workspace controls">
            {minimized ? (
              <button type="button" className="fleet-action" onClick={() => { setWorkspaceLayout("rail"); }}>Restore action</button>
            ) : (
              <>
                <button
                  type="button"
                  className="fleet-action"
                  onClick={() => { setWorkspaceLayout(expanded ? "rail" : "expanded"); }}
                >
                  {expanded ? "Dock console" : "Expand console"}
                </button>
                <button type="button" className="fleet-action" onClick={() => { setWorkspaceLayout("minimized"); }}>Minimize action</button>
              </>
            )}
            <button type="button" className="fleet-action" onClick={onClose}>Clear</button>
          </div>
        ) : null}
      </header>

      {vm ? (
        <div className="vm-action-workspace__body" aria-hidden={minimized}>
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

          {mode === "console" ? <VmConsolePanel vm={vm} evidence={evidence ?? null} /> : null}
          {mode === "screenshot" ? (
            <VmScreenshotPanel
              vm={vm}
              screenshot={screenshot}
              socketState={socketState}
              onRequestScreenshot={onRequestScreenshot}
            />
          ) : null}
        </div>
      ) : (
        <div className="vm-action-empty">
          <h3>Choose a VM action</h3>
          <p>Console and screenshots open here.</p>
        </div>
      )}
    </aside>
  );
}

function VmConsolePanel({ vm, evidence }: { readonly vm: VmFleetRow; readonly evidence: VmDetailEvidenceResponse | null }) {
  const screenRef = useRef<HTMLDivElement | null>(null);
  const rfbRef = useRef<import("@novnc/novnc").default | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [textToType, setTextToType] = useState("");
  const [pressEnter, setPressEnter] = useState(false);
  const [revealedPasswords, setRevealedPasswords] = useState<Readonly<Record<string, string>>>({});
  const [revealingKey, setRevealingKey] = useState("");
  const [consoleState, setConsoleState] = useState<ConsoleState>({
    status: "idle",
    message: "Console idle"
  });
  const [controlState, setControlState] = useState<ConsoleControlState>({
    status: "idle",
    message: "Console controls ready"
  });
  const knownCredentials = evidence?.known_credentials ?? [];

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

  const typeIntoVm = useCallback(async (text: string, includeEnter: boolean, label: string) => {
    if (!text) {
      setControlState({ status: "failed", message: "Nothing to type" });
      return;
    }
    setControlState({ status: "working", message: `${label}...` });
    try {
      const body: Record<string, unknown> = { text };
      if (includeEnter) {
        body.press_enter = "1";
      }
      const response = await postJson<VmTypeResponse>(`/api/vms/${String(vm.vmid)}/type`, body);
      if (response.error || response.ok === false) {
        throw new Error(response.error || "Type request failed");
      }
      const skipped = response.skipped?.length ? `, skipped ${response.skipped.join("")}` : "";
      setControlState({ status: "ready", message: `${label} sent ${String(response.sent ?? text.length)} key(s)${skipped}` });
      rfbRef.current?.focus();
    } catch (error) {
      setControlState({ status: "failed", message: error instanceof Error ? error.message : `${label} failed` });
    }
  }, [vm.vmid]);

  const sendVmKey = useCallback(async (key: string, label: string) => {
    setControlState({ status: "working", message: `Sending ${label}...` });
    try {
      const response = await postJson<VmKeyResponse>(`/api/vms/${String(vm.vmid)}/key`, { key });
      if (response.error || response.ok === false) {
        throw new Error(response.error || `Send ${label} failed`);
      }
      setControlState({ status: "ready", message: `${label} sent` });
      rfbRef.current?.focus();
    } catch (error) {
      setControlState({ status: "failed", message: error instanceof Error ? error.message : `Send ${label} failed` });
    }
  }, [vm.vmid]);

  const sendPowerAction = useCallback(async (action: string) => {
    setControlState({ status: "working", message: `${action}...` });
    try {
      const response = await postJson<VmPowerResponse>(`/api/vms/${String(vm.vmid)}/action/${action}`);
      if (response.error || response.ok === false) {
        throw new Error(response.error || `${action} failed`);
      }
      setControlState({ status: "ready", message: `${action} sent` });
    } catch (error) {
      setControlState({ status: "failed", message: error instanceof Error ? error.message : `${action} failed` });
    }
  }, [vm.vmid]);

  const pasteClipboardIntoVm = useCallback(async () => {
    const clipboard = browserClipboard();
    if (typeof clipboard?.readText !== "function") {
      setControlState({ status: "failed", message: "Clipboard read is unavailable in this browser context" });
      return;
    }
    setControlState({ status: "working", message: "Reading clipboard..." });
    try {
      const text = await clipboard.readText();
      await typeIntoVm(text, false, "Clipboard text");
    } catch (error) {
      setControlState({ status: "failed", message: error instanceof Error ? error.message : "Clipboard paste failed" });
    }
  }, [typeIntoVm]);

  const copyToClipboard = useCallback(async (value: string, label: string) => {
    const clipboard = browserClipboard();
    if (typeof clipboard?.writeText !== "function") {
      setControlState({ status: "failed", message: "Clipboard write is unavailable in this browser context" });
      return;
    }
    try {
      await clipboard.writeText(value);
      setControlState({ status: "ready", message: `${label} copied` });
    } catch (error) {
      setControlState({ status: "failed", message: error instanceof Error ? error.message : `${label} copy failed` });
    }
  }, []);

  const revealCredentialPassword = useCallback(async (credential: VmKnownCredential): Promise<string | null> => {
    const key = credentialKey(credential);
    const current = revealedPasswords[key];
    if (current) {
      return current;
    }
    setRevealingKey(key);
    setControlState({ status: "working", message: `Revealing ${fallbackText(credential.label)}...` });
    try {
      const response = await postJson<VmCredentialsRevealResponse>(`/api/vms/${String(vm.vmid)}/credentials/reveal`);
      const next: Record<string, string> = {};
      for (const item of response.credentials) {
        if (item.password) {
          next[credentialKey(item)] = item.password;
        }
      }
      setRevealedPasswords((currentPasswords) => ({ ...currentPasswords, ...next }));
      const password = next[key];
      if (!password) {
        throw new Error("Credential password was not returned for this VM");
      }
      setControlState({ status: "ready", message: `${fallbackText(credential.label)} revealed` });
      return password;
    } catch (error) {
      setControlState({ status: "failed", message: error instanceof Error ? error.message : "Credential reveal failed" });
      return null;
    } finally {
      setRevealingKey("");
    }
  }, [revealedPasswords, vm.vmid]);

  const toggleCredentialReveal = useCallback((credential: VmKnownCredential) => {
    const key = credentialKey(credential);
    if (revealedPasswords[key]) {
      setRevealedPasswords((current) => withoutCredentialKey(current, key));
      setControlState({ status: "ready", message: `${fallbackText(credential.label)} hidden` });
      return;
    }
    void revealCredentialPassword(credential);
  }, [revealCredentialPassword, revealedPasswords]);

  const typeCredentialPassword = useCallback(async (credential: VmKnownCredential) => {
    const password = await revealCredentialPassword(credential);
    if (password) {
      await typeIntoVm(password, false, `${fallbackText(credential.label)} password`);
    }
  }, [revealCredentialPassword, typeIntoVm]);

  const copyCredentialPassword = useCallback(async (credential: VmKnownCredential) => {
    const password = await revealCredentialPassword(credential);
    if (password) {
      await copyToClipboard(password, `${fallbackText(credential.label)} password`);
    }
  }, [copyToClipboard, revealCredentialPassword]);

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
      <div className="vm-console-controls" aria-label={`VM ${String(vm.vmid)} console controls`}>
        <div className="vm-action-status vm-action-status--control">
          <span className={`status status--${controlState.status === "ready" ? "good" : controlState.status === "failed" ? "bad" : controlState.status === "working" ? "active" : "neutral"}`}>
            {controlState.status}
          </span>
          <span>{controlState.message}</span>
        </div>
        <form
          className="vm-console-type-row"
          onSubmit={(event) => {
            event.preventDefault();
            void typeIntoVm(textToType, pressEnter, "Text");
          }}
        >
          <label>
            <span>Type text</span>
            <input
              aria-label={`Text to type into VM ${String(vm.vmid)}`}
              value={textToType}
              onChange={(event) => { setTextToType(event.target.value); }}
              placeholder="Text to type into VM"
            />
          </label>
          <label className="vm-console-check">
            <input
              type="checkbox"
              checked={pressEnter}
              onChange={(event) => { setPressEnter(event.target.checked); }}
            />
            <span>Enter after</span>
          </label>
          <button type="submit" className="fleet-action" aria-label={`Send text to VM ${String(vm.vmid)}`}>
            <Send aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
            <span>Send</span>
          </button>
          <button type="button" className="fleet-action" aria-label={`Paste clipboard text into VM ${String(vm.vmid)}`} onClick={() => { void pasteClipboardIntoVm(); }}>
            <Clipboard aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
            <span>Paste clipboard</span>
          </button>
        </form>
        <div className="vm-console-toolbar" aria-label={`VM ${String(vm.vmid)} send keys`}>
          <button type="button" className="fleet-action" aria-label={`Send Tab to VM ${String(vm.vmid)}`} onClick={() => { void sendVmKey("tab", "Tab"); }}>
            <Keyboard aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
            <span>Tab</span>
          </button>
          <button type="button" className="fleet-action" aria-label={`Send Enter to VM ${String(vm.vmid)}`} onClick={() => { void sendVmKey("ret", "Enter"); }}>
            <Keyboard aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
            <span>Enter</span>
          </button>
          <button type="button" className="fleet-action" aria-label={`Send Escape to VM ${String(vm.vmid)}`} onClick={() => { void sendVmKey("esc", "Escape"); }}>
            <Keyboard aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
            <span>Esc</span>
          </button>
        </div>
        <div className="vm-console-toolbar" aria-label={`VM ${String(vm.vmid)} power controls`}>
          {["start", "shutdown", "reboot", "stop", "reset"].map((action) => (
            <button
              key={action}
              type="button"
              className={action === "stop" || action === "reset" ? "fleet-action fleet-action--danger" : "fleet-action"}
              onClick={() => { void sendPowerAction(action); }}
            >
              {action}
            </button>
          ))}
        </div>
        <div className="vm-console-credentials" aria-label={`VM ${String(vm.vmid)} saved credentials`}>
          <h3>Saved credentials</h3>
          {knownCredentials.length ? (
            <div className="vm-console-credential-list">
              {knownCredentials.map((credential) => (
                <ConsoleCredentialRow
                  key={credentialKey(credential)}
                  vmid={vm.vmid}
                  credential={credential}
                  password={revealedPasswords[credentialKey(credential)]}
                  isRevealing={revealingKey === credentialKey(credential)}
                  onToggleReveal={() => { toggleCredentialReveal(credential); }}
                  onTypeUsername={() => { void typeIntoVm(credential.username, false, `${fallbackText(credential.label)} username`); }}
                  onTypePassword={() => { void typeCredentialPassword(credential); }}
                  onCopyUsername={() => { void copyToClipboard(credential.username, `${fallbackText(credential.label)} username`); }}
                  onCopyPassword={() => { void copyCredentialPassword(credential); }}
                />
              ))}
            </div>
          ) : (
            <p className="empty">No saved credentials for this VM.</p>
          )}
        </div>
      </div>
      <div ref={screenRef} className="vm-console-screen" aria-label={`VM ${String(vm.vmid)} console screen`} />
    </section>
  );
}

function ConsoleCredentialRow({
  vmid,
  credential,
  password,
  isRevealing,
  onToggleReveal,
  onTypeUsername,
  onTypePassword,
  onCopyUsername,
  onCopyPassword
}: {
  readonly vmid: number;
  readonly credential: VmKnownCredential;
  readonly password: string | undefined;
  readonly isRevealing: boolean;
  readonly onToggleReveal: () => void;
  readonly onTypeUsername: () => void;
  readonly onTypePassword: () => void;
  readonly onCopyUsername: () => void;
  readonly onCopyPassword: () => void;
}) {
  const label = fallbackText(credential.label);
  const username = fallbackText(credential.username);
  const isRevealed = Boolean(password);
  return (
    <div className="vm-console-credential">
      <div>
        <strong>{label}</strong>
        <span>{fallbackText(credential.source)} / {username}</span>
      </div>
      <code>{credential.password_available ? (password ?? credential.password_mask) : "-"}</code>
      <div className="vm-console-credential__actions">
        <button type="button" className="fleet-action" aria-label={`Type ${label} username for ${username} into VM ${String(vmid)}`} onClick={onTypeUsername}>
          <User aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
          <span>Type user</span>
        </button>
        <button
          type="button"
          className="fleet-action"
          aria-label={`Type ${label} password for ${username} into VM ${String(vmid)}`}
          onClick={onTypePassword}
          disabled={!credential.password_available || isRevealing}
        >
          <KeyRound aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
          <span>Type password</span>
        </button>
        <button type="button" className="fleet-action" aria-label={`Copy ${label} username for ${username}`} onClick={onCopyUsername}>
          <Clipboard aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
          <span>Copy user</span>
        </button>
        <button
          type="button"
          className="fleet-action"
          aria-label={`Copy ${label} password for ${username}`}
          onClick={onCopyPassword}
          disabled={!credential.password_available || isRevealing}
        >
          <Clipboard aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
          <span>Copy password</span>
        </button>
        {credential.password_available ? (
          <button
            type="button"
            className="credential-reveal-button"
            aria-label={`${isRevealed ? "Hide" : "Reveal"} ${label} password for ${username}`}
            title={`${isRevealed ? "Hide" : "Reveal"} ${label} password for ${username}`}
            onClick={onToggleReveal}
            disabled={isRevealing}
          >
            {isRevealed ? (
              <EyeOff aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
            ) : (
              <Eye aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
            )}
          </button>
        ) : null}
      </div>
    </div>
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

function credentialKey(credential: Pick<VmKnownCredential | VmRevealedCredential, "source" | "label" | "username" | "run_id" | "updated_at">): string {
  return [
    credential.source,
    credential.label,
    credential.username,
    credential.run_id,
    credential.updated_at ?? ""
  ].join("\u001f");
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
