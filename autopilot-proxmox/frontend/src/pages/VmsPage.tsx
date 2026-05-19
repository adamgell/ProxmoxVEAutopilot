import { useCallback, useEffect, useMemo, useState } from "react";
import {
  BadgeCheck,
  Camera,
  CircleStop,
  Hash,
  Keyboard,
  Monitor,
  Pencil,
  Play,
  Power,
  RefreshCw,
  RotateCcw,
  Save,
  TerminalSquare,
  Trash2,
  UserPlus
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { fetchJson, postJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import { VmActionWorkspace, type ScreenshotWorkspaceState, type VmActionMode, type VmActionSelection } from "../components/VmActionWorkspace";
import type {
  AgentFleetRow,
  AppBootstrap,
  AutopilotDeviceFleetRow,
  LiveSocketMessage,
  VmFleetRow,
  VmsFleetResponse
} from "../contracts";
import { connectFleetLive } from "../liveSocket";
import {
  buildFleetMachineRows,
  deviceDisplayName,
  type FleetMachineRow,
  fallbackText,
  formatShortDateTime,
  machineMatchesFilter,
  statusClass,
  statusLabel,
  summarizeFleet,
  vmDisplayName
} from "../viewModels";

const emptyFleet: VmsFleetResponse = {
  vms: [],
  missing_vms: [],
  agents: [],
  autopilot_devices: [],
  ap_error: "",
  cache_refreshing: false,
  generated_at: ""
};

type SendLiveMessage = (message: Readonly<Record<string, unknown>>) => boolean;
type ActionIcon = LucideIcon;

function mergeRows(existing: readonly VmFleetRow[], patchRows: readonly VmFleetRow[]): readonly VmFleetRow[] {
  const byVmid = new Map(existing.map((row) => [row.vmid, row]));
  for (const row of patchRows) {
    byVmid.set(row.vmid, { ...(byVmid.get(row.vmid) ?? {}), ...row });
  }
  return Array.from(byVmid.values()).toSorted((left, right) => left.vmid - right.vmid);
}

function ActionButton({
  label,
  onClick,
  tone = "neutral",
  icon: Icon,
  ariaLabel
}: {
  readonly label: string;
  readonly onClick: () => void;
  readonly tone?: "neutral" | "danger";
  readonly icon?: ActionIcon;
  readonly ariaLabel?: string;
}) {
  return (
    <button
      type="button"
      className={tone === "danger" ? "fleet-action fleet-action--danger" : "fleet-action"}
      onClick={onClick}
      aria-label={ariaLabel}
    >
      {Icon ? <Icon aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} /> : null}
      <span>{label}</span>
    </button>
  );
}

function screenshotMatches(current: ScreenshotWorkspaceState, message: LiveSocketMessage): boolean {
  if (current.status === "idle") {
    return false;
  }
  if (current.correlationId && message.correlation_id) {
    return current.correlationId === message.correlation_id;
  }
  return typeof message.vmid === "number" && current.vmid === message.vmid;
}

function screenshotErrorMatches(current: ScreenshotWorkspaceState, message: LiveSocketMessage): boolean {
  if (current.status === "idle") {
    return false;
  }
  if (message.error && message.error !== "screenshot_failed") {
    return false;
  }
  if (current.correlationId && message.correlation_id) {
    return current.correlationId === message.correlation_id;
  }
  return typeof message.vmid !== "number" || current.vmid === message.vmid;
}

function Chips({ labels }: { readonly labels: readonly string[] }) {
  if (!labels.length) {
    return <span className="muted">-</span>;
  }
  return (
    <span className="chip-row">
      {labels.map((label) => (
        <span key={label} className={label === "unenrolled" ? "status status--bad" : "status status--good"}>
          {label}
        </span>
      ))}
    </span>
  );
}

export function VmsPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [fleet, setFleet] = useState<VmsFleetResponse>(emptyFleet);
  const [filter, setFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionStatus, setActionStatus] = useState("");
  const [socketState, setSocketState] = useState("closed");
  const [sendLive, setSendLive] = useState<SendLiveMessage | null>(null);
  const [activeAction, setActiveAction] = useState<VmActionSelection | null>(null);
  const [screenshot, setScreenshot] = useState<ScreenshotWorkspaceState>({ status: "idle" });

  const load = useCallback(async () => {
    try {
      const data = await fetchJson<VmsFleetResponse>("/api/vms/fleet");
      setFleet(data);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load fleet");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void load();
    }, 0);
    return () => {
      window.clearTimeout(timer);
    };
  }, [load]);

  useEffect(() => {
    return connectFleetLive({
      onFleetRows: (rows, replace) => {
        setFleet((current) => ({ ...current, vms: replace ? rows : mergeRows(current.vms, rows) }));
        if (replace) {
          setLoading(false);
          setError("");
        }
      },
      onAgents: (agents) => {
        setFleet((current) => ({ ...current, agents }));
      },
      onEvent: (message: LiveSocketMessage) => {
        if (message.type === "screenshot.result" && message.image_url && typeof message.vmid === "number") {
          const imageUrl = message.image_url;
          const resultVmid = message.vmid;
          const correlationId = message.correlation_id;
          setScreenshot((current) => {
            if (!screenshotMatches(current, message)) {
              return current;
            }
            return {
              status: "ready",
              vmid: resultVmid,
              imageUrl,
              message: `Screenshot captured for VM ${String(resultVmid)}`,
              ...(correlationId ? { correlationId } : {})
            };
          });
          setActionStatus(`Screenshot captured for VM ${String(resultVmid)}`);
          return;
        }
        if (message.type === "error") {
          setScreenshot((current) => {
            if (!screenshotErrorMatches(current, message)) {
              return current;
            }
            const currentVmid = current.status === "idle" ? undefined : current.vmid;
            const vmid = typeof message.vmid === "number" ? message.vmid : currentVmid;
            return {
              status: "failed",
              message: message.detail || message.error || "Live action failed",
              ...(typeof vmid === "number" ? { vmid } : {}),
              ...(message.correlation_id ? { correlationId: message.correlation_id } : {}),
              ...((current.status === "ready" || current.status === "failed") && current.imageUrl ? { imageUrl: current.imageUrl } : {})
            };
          });
          setActionStatus(message.detail || message.error || "Live action failed");
        }
        if (message.event === "sweep_started") {
          setActionStatus("Fleet refresh started");
        }
        if (message.event === "sweep_finished") {
          setActionStatus("Fleet refresh complete");
          void load();
        }
        if (message.event === "qga_probe.result") {
          setActionStatus(`QGA ${fallbackText((message.result as { qga?: string } | undefined)?.qga)}`);
        }
      },
      onSendReady: (send) => {
        setSendLive(() => send);
      },
      onState: (state) => {
        setSocketState(state);
        if (state === "closed") {
          void load();
        }
      }
    });
  }, [load]);

  const counts = useMemo(() => summarizeFleet(fleet), [fleet]);
  const machineRows = useMemo(() => buildFleetMachineRows(fleet), [fleet]);
  const filteredMachines = useMemo(() => machineRows.filter((row) => machineMatchesFilter(row, filter)), [filter, machineRows]);
  const stale = typeof fleet.cache_age_seconds === "number" && fleet.cache_age_seconds > 60;

  const runAction = useCallback(async (label: string, action: () => Promise<unknown>) => {
    setActionStatus(`${label}...`);
    try {
      await action();
      setActionStatus(`${label} complete`);
      await load();
    } catch (err) {
      setActionStatus(err instanceof Error ? err.message : `${label} failed`);
    }
  }, [load]);

  const power = useCallback((vm: VmFleetRow, action: "start" | "shutdown" | "stop" | "reset" | "delete") => {
    const label = `${action} VM ${String(vm.vmid)}`;
    if (action === "delete") {
      const typed = window.prompt(`Type ${String(vm.vmid)} to delete VM ${String(vm.vmid)}`);
      if (typed !== String(vm.vmid)) {
        return;
      }
    } else if ((action === "shutdown" || action === "stop") && !window.confirm(`${label}?`)) {
      return;
    }
    void runAction(label, () => postJson(`/api/vms/${String(vm.vmid)}/${action}`));
  }, [runAction]);

  const rename = useCallback((vm: VmFleetRow) => {
    void runAction(`Rename VM ${String(vm.vmid)}`, async () => {
      const suggestion = await fetchJson<{ readonly sanitized?: string; readonly suggested?: string }>(`/api/vms/${String(vm.vmid)}/rename-suggest`);
      const target = window.prompt(`Rename VM ${String(vm.vmid)}`, suggestion.sanitized || suggestion.suggested || vmDisplayName(vm));
      if (!target) {
        return;
      }
      await postJson(`/api/vms/${String(vm.vmid)}/rename`, { new_name: target });
    });
  }, [runAction]);

  const typeText = useCallback((vm: VmFleetRow) => {
    const text = window.prompt(`Text for VM ${String(vm.vmid)}`);
    if (!text) {
      return;
    }
    void runAction(`Type text VM ${String(vm.vmid)}`, () => postJson(`/api/vms/${String(vm.vmid)}/type`, { text }));
  }, [runAction]);

  const sendKey = useCallback((vm: VmFleetRow, key: "ctrl-alt-delete" | "ret") => {
    void runAction(`Send ${key} VM ${String(vm.vmid)}`, () => postJson(`/api/vms/${String(vm.vmid)}/key`, { key }));
  }, [runAction]);

  const captureHash = useCallback((vm: VmFleetRow) => {
    void runAction(`Capture hash VM ${String(vm.vmid)}`, () => postJson("/api/jobs/capture", { vmid: vm.vmid, vm_name: vmDisplayName(vm) }));
  }, [runAction]);

  const checkEnrollment = useCallback((vm: VmFleetRow) => {
    void runAction(`Check enrollment VM ${String(vm.vmid)}`, () => postJson(`/api/ubuntu/check-enrollment/${String(vm.vmid)}`));
  }, [runAction]);

  const selectConsole = useCallback((vm: VmFleetRow) => {
    setActiveAction({ mode: "console", vm });
    setActionStatus(`Console selected for VM ${String(vm.vmid)}`);
  }, []);

  const selectActionMode = useCallback((mode: VmActionMode) => {
    setActiveAction((current) => current ? { ...current, mode } : current);
  }, []);

  const screenshotVm = useCallback((vm: VmFleetRow) => {
    const correlationId = `vm-${String(vm.vmid)}-${String(Date.now())}`;
    setActiveAction({ mode: "screenshot", vm });
    const sent = sendLive?.({ type: "screenshot.request", correlation_id: correlationId, vmid: vm.vmid, format: "png" });
    if (sent) {
      setScreenshot({
        status: "requesting",
        vmid: vm.vmid,
        correlationId,
        message: `Screenshot requested for VM ${String(vm.vmid)}`
      });
    } else {
      setScreenshot({
        status: "failed",
        vmid: vm.vmid,
        correlationId,
        message: "Live WebSocket is not connected"
      });
    }
    setActionStatus(sent ? `Screenshot requested for VM ${String(vm.vmid)}` : "Live WebSocket is not connected");
  }, [sendLive]);

  const qgaProbe = useCallback((vm: VmFleetRow) => {
    const sent = sendLive?.({ type: "qga_probe", correlation_id: `qga-${String(vm.vmid)}-${String(Date.now())}`, vmid: vm.vmid });
    setActionStatus(sent ? `QGA probe requested for VM ${String(vm.vmid)}` : "Live WebSocket is not connected");
  }, [sendLive]);

  const deleteAutopilotDevice = useCallback((device: AutopilotDeviceFleetRow) => {
    const typed = window.prompt(`Type ${device.serial} to delete Autopilot identity`);
    if (typed !== device.serial) {
      return;
    }
    void runAction(`Delete Autopilot ${device.serial}`, () => postJson("/api/autopilot/delete", { device_id: device.id }));
  }, [runAction]);

  const deleteAgent = useCallback((agent: AgentFleetRow) => {
    const typed = window.prompt(`Type ${agent.agent_id} to delete agent`);
    if (typed !== agent.agent_id) {
      return;
    }
    void runAction(`Delete ${agent.agent_id}`, () => postJson(`/api/agents/${encodeURIComponent(agent.agent_id)}/delete`));
  }, [runAction]);

  const createAgent = useCallback(() => {
    const agentId = window.prompt("Agent ID");
    if (!agentId) {
      return;
    }
    const vmid = window.prompt("VMID");
    const computerName = window.prompt("Computer name") || "";
    void runAction(`Add ${agentId}`, () => postJson("/api/agents", {
      agent_id: agentId,
      vmid: vmid || "",
      computer_name: computerName
    }));
  }, [runAction]);

  const updateAgent = useCallback((agent: AgentFleetRow) => {
    const vmid = window.prompt(`VMID for ${agent.agent_id}`, agent.vmid ? String(agent.vmid) : "");
    if (vmid === null) {
      return;
    }
    const computerName = window.prompt(`Computer name for ${agent.agent_id}`, agent.computer_name || "") ?? agent.computer_name ?? "";
    void runAction(`Update ${agent.agent_id}`, () => postJson(`/api/agents/${encodeURIComponent(agent.agent_id)}/update`, {
      vmid,
      computer_name: computerName,
      serial_number: agent.serial_number || "",
      agent_version: agent.agent_version || ""
    }));
  }, [runAction]);

  const approveAgent = useCallback((agent: AgentFleetRow) => {
    const approvalId = agent.approval_id;
    if (!approvalId) {
      return;
    }
    void runAction(`Approve ${agent.agent_id}`, () => postJson(`/api/agent-approvals/${encodeURIComponent(approvalId)}/approve`));
  }, [runAction]);

  return (
    <PageFrame
      bootstrap={bootstrap}
      title="VMs"
      section="Fleet"
      path="/react/vms"
      socketState={socketState}
      action={<a className="action-link" href="/vms">Legacy VMs</a>}
    >
      {loading ? <div className="progress" aria-label="Loading fleet"><span /></div> : null}
      {error ? <p className="notice" role="status">{error}</p> : null}
      {actionStatus ? <p className="notice" role="status">{actionStatus}</p> : null}
      {stale ? <p className="notice" role="status">Fleet cache is {String(fleet.cache_age_seconds)}s old.</p> : null}
      {fleet.ap_error ? <p className="notice" role="status">Intune unavailable: {fleet.ap_error}</p> : null}

      <section className="metric-strip metric-strip--fleet" aria-label="Fleet metrics">
        <Metric label="Proxmox VMs" value={String(counts.total)} tone={counts.total ? "good" : "neutral"} />
        <Metric label="Running" value={String(counts.running)} tone={counts.running ? "active" : "neutral"} />
        <Metric label="Attention" value={String(counts.attention)} tone={counts.attention ? "bad" : "good"} />
        <Metric label="Agents" value={String(counts.agents)} tone={counts.agents ? "good" : "neutral"} />
        <Metric label="Stale agents" value={String(counts.staleAgents)} tone={counts.staleAgents ? "bad" : "good"} />
        <Metric label="Intune" value={String(counts.autopilotDevices)} tone={counts.autopilotDevices ? "good" : "neutral"} />
        <Metric label="Missing" value={String(counts.missingAutopilot)} tone={counts.missingAutopilot ? "bad" : "good"} />
      </section>

      <section className="filter-row" aria-label="Fleet filters">
        <div className="filter-row__top">
          <label className="filter">
            <span>Filter fleet</span>
            <input
              aria-label="Filter fleet"
              value={filter}
              onChange={(event) => { setFilter(event.target.value); }}
              placeholder="VMID, name, serial, IP, enrollment"
            />
          </label>
          <button type="button" className="action-link" onClick={() => { void runAction("Refresh fleet", () => postJson("/api/vms/refresh")); }}>
            Refresh
          </button>
        </div>
      </section>

      <section className="fleet-lanes" aria-label="Fleet lanes">
        <div className="fleet-primary-stack">
          <FleetMachineTable
            rows={filteredMachines}
            onPower={power}
            onRename={rename}
            onTypeText={typeText}
            onSendKey={sendKey}
            onCapture={captureHash}
            onCheckEnrollment={checkEnrollment}
            onConsole={selectConsole}
            onScreenshot={screenshotVm}
            onQgaProbe={qgaProbe}
            onCreateAgent={createAgent}
            onUpdateAgent={updateAgent}
            onApproveAgent={approveAgent}
            onDeleteAgent={deleteAgent}
          />
        </div>
        <div className="fleet-side-stack">
          <VmActionWorkspace
            selection={activeAction}
            screenshot={screenshot}
            socketState={socketState}
            onModeChange={selectActionMode}
            onRequestScreenshot={screenshotVm}
            onClose={() => {
              setActiveAction(null);
              setScreenshot({ status: "idle" });
            }}
          />
          <IntuneLane devices={fleet.autopilot_devices} onDelete={deleteAutopilotDevice} onSync={() => { void runAction("Sync Autopilot", () => postJson("/api/autopilot/sync")); }} />
        </div>
      </section>
    </PageFrame>
  );
}

function FleetMachineTable({
  rows,
  onPower,
  onRename,
  onTypeText,
  onSendKey,
  onCapture,
  onCheckEnrollment,
  onConsole,
  onScreenshot,
  onQgaProbe,
  onCreateAgent,
  onUpdateAgent,
  onApproveAgent,
  onDeleteAgent
}: {
  readonly rows: readonly FleetMachineRow[];
  readonly onPower: (vm: VmFleetRow, action: "start" | "shutdown" | "stop" | "reset" | "delete") => void;
  readonly onRename: (vm: VmFleetRow) => void;
  readonly onTypeText: (vm: VmFleetRow) => void;
  readonly onSendKey: (vm: VmFleetRow, key: "ctrl-alt-delete" | "ret") => void;
  readonly onCapture: (vm: VmFleetRow) => void;
  readonly onCheckEnrollment: (vm: VmFleetRow) => void;
  readonly onConsole: (vm: VmFleetRow) => void;
  readonly onScreenshot: (vm: VmFleetRow) => void;
  readonly onQgaProbe: (vm: VmFleetRow) => void;
  readonly onCreateAgent: () => void;
  readonly onUpdateAgent: (agent: AgentFleetRow) => void;
  readonly onApproveAgent: (agent: AgentFleetRow) => void;
  readonly onDeleteAgent: (agent: AgentFleetRow) => void;
}) {
  return (
    <Panel title="Fleet machines">
      <div className="fleet-lane-command">
        <button type="button" className="fleet-action fleet-action--command" onClick={onCreateAgent}>
          <UserPlus aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
          <span>Add agent</span>
        </button>
      </div>
      <div className="fleet-machine-table-wrap">
        {rows.length ? (
          <table className="fleet-machine-table" aria-label="Fleet machines">
            <thead>
              <tr>
                <th scope="col">Machine</th>
                <th scope="col">Runtime</th>
                <th scope="col">Agent</th>
                <th scope="col">Lifecycle</th>
                <th scope="col">Join / MDM</th>
                <th scope="col">Freshness</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <MachineRow
                  key={row.id}
                  row={row}
                  onPower={onPower}
                  onRename={onRename}
                  onTypeText={onTypeText}
                  onSendKey={onSendKey}
                  onCapture={onCapture}
                  onCheckEnrollment={onCheckEnrollment}
                  onConsole={onConsole}
                  onScreenshot={onScreenshot}
                  onQgaProbe={onQgaProbe}
                  onUpdateAgent={onUpdateAgent}
                  onApproveAgent={onApproveAgent}
                  onDeleteAgent={onDeleteAgent}
                />
              ))}
            </tbody>
          </table>
        ) : <p className="empty">No fleet machines found.</p>}
      </div>
    </Panel>
  );
}

function MachineRow({
  row,
  onPower,
  onRename,
  onTypeText,
  onSendKey,
  onCapture,
  onCheckEnrollment,
  onConsole,
  onScreenshot,
  onQgaProbe,
  onUpdateAgent,
  onApproveAgent,
  onDeleteAgent
}: {
  readonly row: FleetMachineRow;
  readonly onPower: (vm: VmFleetRow, action: "start" | "shutdown" | "stop" | "reset" | "delete") => void;
  readonly onRename: (vm: VmFleetRow) => void;
  readonly onTypeText: (vm: VmFleetRow) => void;
  readonly onSendKey: (vm: VmFleetRow, key: "ctrl-alt-delete" | "ret") => void;
  readonly onCapture: (vm: VmFleetRow) => void;
  readonly onCheckEnrollment: (vm: VmFleetRow) => void;
  readonly onConsole: (vm: VmFleetRow) => void;
  readonly onScreenshot: (vm: VmFleetRow) => void;
  readonly onQgaProbe: (vm: VmFleetRow) => void;
  readonly onUpdateAgent: (agent: AgentFleetRow) => void;
  readonly onApproveAgent: (agent: AgentFleetRow) => void;
  readonly onDeleteAgent: (agent: AgentFleetRow) => void;
}) {
  const vm = row.vm;
  const agent = row.agent;
  const isRunning = (vm?.status || "").toLowerCase() === "running";
  return (
    <tr>
      <th scope="row">
        <span className="machine-name">{row.name}</span>
        <span className="machine-subline">
          {row.vmid !== undefined ? <a href={`/devices/${String(row.vmid)}`}>VM {row.vmid}</a> : "No VM"}
          <span>{fallbackText(row.serial)}</span>
          <span>{fallbackText(row.ipAddress)}</span>
        </span>
      </th>
      <td>
        <span className={statusClass(row.status)}>{statusLabel(row.status)}</span>
        <span className="machine-meta">{fallbackText(row.os)}</span>
        <span className="machine-meta">QGA {fallbackText(row.qga)}</span>
      </td>
      <td>
        <span className={row.stale ? "status status--bad" : "status status--good"}>{agent?.approval_status || (agent ? "active" : "missing")}</span>
        <span className="machine-meta">{fallbackText(row.agentId)}</span>
        <span className="machine-meta">{fallbackText(row.phase)}</span>
        <span className="machine-meta">v{fallbackText(row.version)}</span>
      </td>
      <td>
        <Chips labels={row.lifecycleLabels} />
      </td>
      <td>
        <span className="machine-meta">method {row.method}</span>
        <span className="machine-meta">MDM {fallbackText(row.mdmEnrollment)}</span>
        <span className="machine-meta">hash {row.lifecycleLabels.includes("hash") ? "ready" : "-"}</span>
      </td>
      <td>
        <span className="machine-meta">heartbeat {formatShortDateTime(row.heartbeat)}</span>
        <span className="machine-meta">source {fallbackText(vm?.lifecycle_source ?? agent?.lifecycle_source)}</span>
      </td>
      <td>
        <div className="fleet-actions fleet-actions--table" aria-label={`${row.name} actions`}>
          {vm ? (
            isRunning ? (
              <>
                <ActionButton label="Shutdown" icon={Power} onClick={() => { onPower(vm, "shutdown"); }} />
                <ActionButton label="Stop" icon={CircleStop} tone="danger" onClick={() => { onPower(vm, "stop"); }} />
                <ActionButton label="Reset" icon={RotateCcw} onClick={() => { onPower(vm, "reset"); }} />
                <ActionButton label="Hash" icon={Hash} onClick={() => { onCapture(vm); }} />
                <ActionButton label="Rename" icon={Pencil} onClick={() => { onRename(vm); }} />
                <ActionButton label="Console" ariaLabel={`Console VM ${String(vm.vmid)}`} icon={Monitor} onClick={() => { onConsole(vm); }} />
                <ActionButton label="Type" icon={Keyboard} onClick={() => { onTypeText(vm); }} />
                <ActionButton label="CAD" icon={TerminalSquare} onClick={() => { onSendKey(vm, "ctrl-alt-delete"); }} />
                <ActionButton label="Enter" icon={TerminalSquare} onClick={() => { onSendKey(vm, "ret"); }} />
                <ActionButton label="Screenshot" ariaLabel={`Screenshot VM ${String(vm.vmid)}`} icon={Camera} onClick={() => { onScreenshot(vm); }} />
                <ActionButton label="QGA" icon={RefreshCw} onClick={() => { onQgaProbe(vm); }} />
                {vm.target_os === "ubuntu" ? <ActionButton label="Enroll" icon={BadgeCheck} onClick={() => { onCheckEnrollment(vm); }} /> : null}
              </>
            ) : (
              <ActionButton label="Start" icon={Play} onClick={() => { onPower(vm, "start"); }} />
            )
          ) : null}
          {agent?.approval_status === "pending" && agent.approval_id ? (
            <ActionButton label="Approve" icon={BadgeCheck} onClick={() => { onApproveAgent(agent); }} />
          ) : null}
          {agent ? (
            <>
              <ActionButton label="Update agent" icon={Save} onClick={() => { onUpdateAgent(agent); }} />
              <ActionButton label="Delete agent" icon={Trash2} tone="danger" onClick={() => { onDeleteAgent(agent); }} />
            </>
          ) : null}
          {vm ? <ActionButton label="Delete VM" ariaLabel={`Delete VM ${String(vm.vmid)}`} icon={Trash2} tone="danger" onClick={() => { onPower(vm, "delete"); }} /> : null}
        </div>
      </td>
    </tr>
  );
}

function IntuneLane({
  devices,
  onDelete,
  onSync
}: {
  readonly devices: readonly AutopilotDeviceFleetRow[];
  readonly onDelete: (device: AutopilotDeviceFleetRow) => void;
  readonly onSync: () => void;
}) {
  return (
    <Panel title="Autopilot Devices">
      <div className="fleet-lane-command">
        <button type="button" className="action-link" onClick={onSync}>Sync Autopilot</button>
      </div>
      <div className="fleet-card-list fleet-card-list--compact">
        {devices.length ? devices.map((device) => (
          <article key={device.id || device.serial} className="fleet-card fleet-card--device">
            <header>
              <div>
                <span className={device.profile_ok ? "status status--good" : "status status--bad"}>
                  {fallbackText(device.profile_status)}
                </span>
                <h3>{deviceDisplayName(device)}</h3>
              </div>
              <strong>{fallbackText(device.group_tag)}</strong>
            </header>
            <dl className="fleet-detail-grid">
              <div><dt>Serial</dt><dd>{fallbackText(device.serial)}</dd></div>
              <div><dt>Enroll</dt><dd>{fallbackText(device.enrollment_state)}</dd></div>
              <div><dt>Model</dt><dd>{fallbackText(device.model)}</dd></div>
              <div><dt>Hash</dt><dd>{device.has_local_hash ? "local" : "-"}</dd></div>
            </dl>
            <div className="fleet-actions">
              <ActionButton label="Delete" tone="danger" onClick={() => { onDelete(device); }} />
            </div>
          </article>
        )) : <p className="empty">No matching Autopilot devices.</p>}
      </div>
    </Panel>
  );
}
