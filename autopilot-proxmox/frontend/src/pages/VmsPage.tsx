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
  fleetAgentLabel,
  fleetManagedByLabel,
  fleetOsName,
  fleetOsVersion,
  fleetRuntimeLabel,
  type FleetMachineRow,
  fallbackText,
  formatRelativeAge,
  formatShortDateTime,
  machineMatchesFilter,
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

function detailVmidFromPath(path: string): number | null {
  const match = /^\/react\/vms\/(\d+)$/.exec(path);
  if (!match?.[1]) {
    return null;
  }
  const vmid = Number.parseInt(match[1], 10);
  return Number.isFinite(vmid) ? vmid : null;
}

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

export function VmsPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const detailVmid = detailVmidFromPath(window.location.pathname);
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
  const detailRow = useMemo(
    () => detailVmid === null ? undefined : machineRows.find((row) => row.vmid === detailVmid),
    [detailVmid, machineRows]
  );
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

  if (detailVmid !== null) {
    return (
      <PageFrame
        bootstrap={bootstrap}
        title={detailRow?.name ?? `VM ${String(detailVmid)}`}
        section="Fleet"
        path="/react/vms"
        socketState={socketState}
        action={<a className="action-link" href="/react/vms">VMs</a>}
      >
        {loading ? <div className="progress" aria-label="Loading VM"><span /></div> : null}
        {error ? <p className="notice" role="status">{error}</p> : null}
        {actionStatus ? <p className="notice" role="status">{actionStatus}</p> : null}
        {detailRow?.vm ? (
          <VmDetailWorkspace
            row={detailRow}
            activeAction={activeAction}
            screenshot={screenshot}
            socketState={socketState}
            onPower={power}
            onRename={rename}
            onTypeText={typeText}
            onSendKey={sendKey}
            onCapture={captureHash}
            onCheckEnrollment={checkEnrollment}
            onConsole={selectConsole}
            onScreenshot={screenshotVm}
            onQgaProbe={qgaProbe}
            onUpdateAgent={updateAgent}
            onApproveAgent={approveAgent}
            onDeleteAgent={deleteAgent}
            onModeChange={selectActionMode}
            onRequestScreenshot={screenshotVm}
            onCloseAction={() => {
              setActiveAction(null);
              setScreenshot({ status: "idle" });
            }}
          />
        ) : loading ? null : (
          <Panel title="VM not found">
            <p className="empty">No current VM {String(detailVmid)} in Fleet.</p>
          </Panel>
        )}
      </PageFrame>
    );
  }

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
            onCreateAgent={createAgent}
          />
        </div>
        <div className="fleet-side-stack">
          <IntuneLane devices={fleet.autopilot_devices} onDelete={deleteAutopilotDevice} onSync={() => { void runAction("Sync Autopilot", () => postJson("/api/autopilot/sync")); }} />
        </div>
      </section>
    </PageFrame>
  );
}

function FleetMachineTable({
  rows,
  onCreateAgent
}: {
  readonly rows: readonly FleetMachineRow[];
  readonly onCreateAgent: () => void;
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
                <th scope="col">Device Name</th>
                <th scope="col">Heartbeat</th>
                <th scope="col">Managed By</th>
                <th scope="col">OS</th>
                <th scope="col">OS Version</th>
                <th scope="col">VMID</th>
                <th scope="col">IP Address</th>
                <th scope="col">Runtime</th>
                <th scope="col">Agent</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <MachineRow
                  key={row.id}
                  row={row}
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
  row
}: {
  readonly row: FleetMachineRow;
}) {
  const runtimeLabel = fleetRuntimeLabel(row);
  const agentLabel = fleetAgentLabel(row);
  return (
    <tr>
      <th scope="row">
        {row.vmid !== undefined ? (
          <a className="machine-name machine-name--link" href={`/react/vms/${String(row.vmid)}`}>{row.name}</a>
        ) : (
          <span className="machine-name">{row.name}</span>
        )}
      </th>
      <td>
        <span className="machine-primary-value" title={formatShortDateTime(row.heartbeat)}>
          {formatRelativeAge(row.heartbeat)}
        </span>
      </td>
      <td>
        <span className={fleetManagedByLabel(row) === "Intune" ? "status status--good" : "status"}>
          {fleetManagedByLabel(row)}
        </span>
      </td>
      <td>
        <span className="machine-primary-value">{fleetOsName(row)}</span>
      </td>
      <td>
        <span className="machine-primary-value">{fleetOsVersion(row)}</span>
      </td>
      <td>
        {row.vmid !== undefined ? <a className="machine-vmid-link" href={`/devices/${String(row.vmid)}`}>{row.vmid}</a> : <span className="machine-primary-value">-</span>}
      </td>
      <td>
        <span className="machine-primary-value">{fallbackText(row.ipAddress)}</span>
      </td>
      <td>
        <span className={runtimeLabel === "running" ? "status status--active" : "status"}>
          {runtimeLabel}
        </span>
      </td>
      <td>
        <span className={agentLabel === "Stale" || agentLabel === "None" ? "status status--bad" : "status status--good"}>
          {agentLabel}
        </span>
      </td>
    </tr>
  );
}

function VmDetailWorkspace({
  row,
  activeAction,
  screenshot,
  socketState,
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
  onDeleteAgent,
  onModeChange,
  onRequestScreenshot,
  onCloseAction
}: {
  readonly row: FleetMachineRow;
  readonly activeAction: VmActionSelection | null;
  readonly screenshot: ScreenshotWorkspaceState;
  readonly socketState: string;
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
  readonly onModeChange: (mode: VmActionMode) => void;
  readonly onRequestScreenshot: (vm: VmFleetRow) => void;
  readonly onCloseAction: () => void;
}) {
  const vm = row.vm;
  if (!vm) {
    return null;
  }
  const agent = row.agent;
  const isRunning = (vm.status || "").toLowerCase() === "running";
  return (
    <div className="vm-detail-layout">
      <section className="vm-detail-hero">
        <nav className="breadcrumb" aria-label="Breadcrumb">
          <a href="/react/vms">VMs</a>
          <span>/</span>
          <span>{vmDisplayName(vm)}</span>
        </nav>
        <div className="vm-detail-hero__main">
          <div>
            <h2>{vmDisplayName(vm)}</h2>
            <p>{fleetOsName(row)} {fleetOsVersion(row)} / VMID {String(vm.vmid)} / {fallbackText(row.ipAddress)}</p>
          </div>
          <div className="vm-detail-badges">
            <span className={fleetRuntimeLabel(row) === "running" ? "status status--active" : "status"}>{fleetRuntimeLabel(row)}</span>
            <span className={fleetManagedByLabel(row) === "Intune" ? "status status--good" : "status"}>{fleetManagedByLabel(row)}</span>
            <span className={fleetAgentLabel(row) === "Stale" || fleetAgentLabel(row) === "None" ? "status status--bad" : "status status--good"}>{fleetAgentLabel(row)}</span>
          </div>
        </div>
      </section>

      <section className="vm-detail-toolbar" aria-label={`VM ${String(vm.vmid)} actions`}>
        {isRunning ? (
          <>
            <ActionButton label="Console" ariaLabel={`Console VM ${String(vm.vmid)}`} icon={Monitor} onClick={() => { onConsole(vm); }} />
            <ActionButton label="Screenshot" ariaLabel={`Screenshot VM ${String(vm.vmid)}`} icon={Camera} onClick={() => { onScreenshot(vm); }} />
            <ActionButton label="Shutdown" icon={Power} onClick={() => { onPower(vm, "shutdown"); }} />
            <ActionButton label="Stop" icon={CircleStop} tone="danger" onClick={() => { onPower(vm, "stop"); }} />
            <ActionButton label="Reset" icon={RotateCcw} onClick={() => { onPower(vm, "reset"); }} />
            <ActionButton label="Hash" icon={Hash} onClick={() => { onCapture(vm); }} />
            <ActionButton label="Rename" icon={Pencil} onClick={() => { onRename(vm); }} />
            <ActionButton label="Type" icon={Keyboard} onClick={() => { onTypeText(vm); }} />
            <ActionButton label="CAD" icon={TerminalSquare} onClick={() => { onSendKey(vm, "ctrl-alt-delete"); }} />
            <ActionButton label="Enter" icon={TerminalSquare} onClick={() => { onSendKey(vm, "ret"); }} />
            <ActionButton label="QGA" icon={RefreshCw} onClick={() => { onQgaProbe(vm); }} />
            {vm.target_os === "ubuntu" ? <ActionButton label="Enroll" icon={BadgeCheck} onClick={() => { onCheckEnrollment(vm); }} /> : null}
          </>
        ) : (
          <ActionButton label="Start" icon={Play} onClick={() => { onPower(vm, "start"); }} />
        )}
        {agent?.approval_status === "pending" && agent.approval_id ? (
          <ActionButton label="Approve agent" icon={BadgeCheck} onClick={() => { onApproveAgent(agent); }} />
        ) : null}
        {agent ? (
          <>
            <ActionButton label="Update agent" icon={Save} onClick={() => { onUpdateAgent(agent); }} />
            <ActionButton label="Delete agent" icon={Trash2} tone="danger" onClick={() => { onDeleteAgent(agent); }} />
          </>
        ) : null}
        <ActionButton label="Delete VM" ariaLabel={`Delete VM ${String(vm.vmid)}`} icon={Trash2} tone="danger" onClick={() => { onPower(vm, "delete"); }} />
      </section>

      <section className="vm-detail-grid" aria-label="VM details">
        <DetailPanel title="Essentials" rows={[
          ["Device name", row.name],
          ["Heartbeat", formatRelativeAge(row.heartbeat)],
          ["Managed by", fleetManagedByLabel(row)],
          ["OS", fleetOsName(row)],
          ["OS version", fleetOsVersion(row)],
          ["VMID", String(vm.vmid)],
          ["IP address", fallbackText(row.ipAddress)],
          ["Runtime", fleetRuntimeLabel(row)],
          ["Agent", fleetAgentLabel(row)]
        ]} />
        <DetailPanel title="PVE" rows={[
          ["Name", vmDisplayName(vm)],
          ["Status", fallbackText(vm.status)],
          ["Serial", fallbackText(vm.serial)],
          ["QGA", fallbackText(vm.qga)],
          ["Target OS", fallbackText(vm.target_os)],
          ["Sequence", fallbackText(vm.sequence_name)]
        ]} />
        <DetailPanel title="Agent" rows={[
          ["Agent ID", fallbackText(row.agentId)],
          ["Computer", fallbackText(row.agent?.computer_name)],
          ["Version", fallbackText(row.version)],
          ["Phase", fallbackText(row.phase)],
          ["QGA", fallbackText(row.agent?.qga_state)],
          ["Last seen", formatShortDateTime(row.agent?.last_seen_at)]
        ]} />
        <DetailPanel title="Intune" rows={[
          ["Device", fallbackText(row.autopilotDevice?.display_name)],
          ["Serial", fallbackText(row.autopilotDevice?.serial)],
          ["Enrollment", fallbackText(row.autopilotDevice?.enrollment_state)],
          ["Profile", fallbackText(row.autopilotDevice?.profile_status)],
          ["Group tag", fallbackText(row.autopilotDevice?.group_tag)],
          ["Last contact", formatShortDateTime(row.autopilotDevice?.last_contact)]
        ]} />
      </section>

      <section className="vm-detail-action-zone">
        <VmActionWorkspace
          selection={activeAction}
          screenshot={screenshot}
          socketState={socketState}
          onModeChange={onModeChange}
          onRequestScreenshot={onRequestScreenshot}
          onClose={onCloseAction}
        />
      </section>
    </div>
  );
}

function DetailPanel({ title, rows }: { readonly title: string; readonly rows: readonly (readonly [string, string])[] }) {
  return (
    <Panel title={title}>
      <dl className="vm-detail-list">
        {rows.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </Panel>
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
