import { useCallback, useEffect, useMemo, useState } from "react";

import { fetchJson, postJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
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
  agentIsStale,
  deviceDisplayName,
  fallbackText,
  formatShortDateTime,
  statusClass,
  statusLabel,
  summarizeFleet,
  vmDisplayName,
  vmJoinLabels,
  vmMatchesFilter
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
  tone = "neutral"
}: {
  readonly label: string;
  readonly onClick: () => void;
  readonly tone?: "neutral" | "danger";
}) {
  return (
    <button type="button" className={tone === "danger" ? "fleet-action fleet-action--danger" : "fleet-action"} onClick={onClick}>
      {label}
    </button>
  );
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
  const [screenshot, setScreenshot] = useState<{ readonly vmid: number; readonly imageUrl: string } | null>(null);

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
          setScreenshot({ vmid: message.vmid, imageUrl: message.image_url });
          setActionStatus(`Screenshot captured for VM ${String(message.vmid)}`);
          return;
        }
        if (message.type === "error") {
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
      onSendReady: setSendLive,
      onState: (state) => {
        setSocketState(state);
        if (state === "closed") {
          void load();
        }
      }
    });
  }, [load]);

  const counts = useMemo(() => summarizeFleet(fleet), [fleet]);
  const filteredVms = useMemo(() => fleet.vms.filter((vm) => vmMatchesFilter(vm, filter)), [filter, fleet.vms]);
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

  const screenshotVm = useCallback((vm: VmFleetRow) => {
    const sent = sendLive?.({ type: "screenshot.request", correlation_id: `vm-${String(vm.vmid)}-${String(Date.now())}`, vmid: vm.vmid, format: "png" });
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
          <VmLane
            vms={filteredVms}
            onPower={power}
            onRename={rename}
            onTypeText={typeText}
            onSendKey={sendKey}
            onCapture={captureHash}
            onCheckEnrollment={checkEnrollment}
            onScreenshot={screenshotVm}
            onQgaProbe={qgaProbe}
          />
          <AgentLane agents={fleet.agents} onCreate={createAgent} onUpdate={updateAgent} onApprove={approveAgent} onDelete={deleteAgent} />
        </div>
        <IntuneLane devices={fleet.autopilot_devices} onDelete={deleteAutopilotDevice} onSync={() => { void runAction("Sync Autopilot", () => postJson("/api/autopilot/sync")); }} />
      </section>

      {screenshot ? (
        <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label={`VM ${String(screenshot.vmid)} screenshot`}>
          <div className="screenshot-modal">
            <div>
              <strong>VM {screenshot.vmid} screenshot</strong>
              <button type="button" onClick={() => { setScreenshot(null); }}>Close</button>
            </div>
            <img src={screenshot.imageUrl} alt={`VM ${String(screenshot.vmid)} screenshot`} />
            <a className="action-link" href={screenshot.imageUrl} download={`vm-${String(screenshot.vmid)}-screenshot.png`}>Download</a>
          </div>
        </div>
      ) : null}
    </PageFrame>
  );
}

function VmLane({
  vms,
  onPower,
  onRename,
  onTypeText,
  onSendKey,
  onCapture,
  onCheckEnrollment,
  onScreenshot,
  onQgaProbe
}: {
  readonly vms: readonly VmFleetRow[];
  readonly onPower: (vm: VmFleetRow, action: "start" | "shutdown" | "stop" | "reset" | "delete") => void;
  readonly onRename: (vm: VmFleetRow) => void;
  readonly onTypeText: (vm: VmFleetRow) => void;
  readonly onSendKey: (vm: VmFleetRow, key: "ctrl-alt-delete" | "ret") => void;
  readonly onCapture: (vm: VmFleetRow) => void;
  readonly onCheckEnrollment: (vm: VmFleetRow) => void;
  readonly onScreenshot: (vm: VmFleetRow) => void;
  readonly onQgaProbe: (vm: VmFleetRow) => void;
}) {
  return (
    <Panel title="Proxmox VMs">
      <div className="fleet-card-list">
        {vms.length ? vms.map((vm) => (
          <article key={vm.vmid} className="fleet-card">
            <header>
              <div>
                <span className={statusClass(vm.status)}>{statusLabel(vm.status)}</span>
                <h3>{vmDisplayName(vm)}</h3>
              </div>
              <a href={`/devices/${String(vm.vmid)}`}>VM {vm.vmid}</a>
            </header>
            <dl className="fleet-detail-grid">
              <div><dt>Host</dt><dd>{fallbackText(vm.hostname)}</dd></div>
              <div><dt>Serial</dt><dd>{fallbackText(vm.serial)}</dd></div>
              <div><dt>IP</dt><dd>{fallbackText(vm.ip_address)}</dd></div>
              <div><dt>OS</dt><dd>{fallbackText(vm.os_caption || vm.os_build)}</dd></div>
            </dl>
            <Chips labels={vmJoinLabels(vm)} />
            {vm.sequence_name ? <p className="muted">sequence: {vm.sequence_name}</p> : null}
            <div className="fleet-actions" aria-label={`VM ${String(vm.vmid)} actions`}>
              {(vm.status || "").toLowerCase() === "running" ? (
                <>
                  <ActionButton label="Shutdown" onClick={() => { onPower(vm, "shutdown"); }} />
                  <ActionButton label="Stop" tone="danger" onClick={() => { onPower(vm, "stop"); }} />
                  <ActionButton label="Reset" onClick={() => { onPower(vm, "reset"); }} />
                  <ActionButton label="Hash" onClick={() => { onCapture(vm); }} />
                  <ActionButton label="Rename" onClick={() => { onRename(vm); }} />
                  <a className="fleet-action" href={`/api/vms/${String(vm.vmid)}/console`} target="_blank" rel="noreferrer" aria-label={`Console VM ${String(vm.vmid)}`}>Console</a>
                  <ActionButton label="Type" onClick={() => { onTypeText(vm); }} />
                  <ActionButton label="CAD" onClick={() => { onSendKey(vm, "ctrl-alt-delete"); }} />
                  <ActionButton label="Enter" onClick={() => { onSendKey(vm, "ret"); }} />
                  <ActionButton label={`Screenshot VM ${String(vm.vmid)}`} onClick={() => { onScreenshot(vm); }} />
                  <ActionButton label="QGA" onClick={() => { onQgaProbe(vm); }} />
                  {vm.target_os === "ubuntu" ? <ActionButton label="Enroll" onClick={() => { onCheckEnrollment(vm); }} /> : null}
                </>
              ) : (
                <ActionButton label="Start" onClick={() => { onPower(vm, "start"); }} />
              )}
              <ActionButton label={`Delete VM ${String(vm.vmid)}`} tone="danger" onClick={() => { onPower(vm, "delete"); }} />
            </div>
          </article>
        )) : <p className="empty">No Proxmox VMs found.</p>}
      </div>
    </Panel>
  );
}

function AgentLane({
  agents,
  onCreate,
  onUpdate,
  onApprove,
  onDelete
}: {
  readonly agents: readonly AgentFleetRow[];
  readonly onCreate: () => void;
  readonly onUpdate: (agent: AgentFleetRow) => void;
  readonly onApprove: (agent: AgentFleetRow) => void;
  readonly onDelete: (agent: AgentFleetRow) => void;
}) {
  return (
    <Panel title="AutopilotAgent">
      <div className="fleet-lane-command">
        <button type="button" className="action-link" onClick={onCreate}>Add agent</button>
      </div>
      <div className="fleet-card-list fleet-card-list--compact">
        {agents.length ? agents.map((agent) => (
          <article key={agent.agent_id} className="fleet-card fleet-card--agent">
            <header>
              <div>
                <span className={agentIsStale(agent) ? "status status--bad" : "status status--good"}>{agent.approval_status || "active"}</span>
                <h3>{agent.agent_id}</h3>
              </div>
              <strong>{agent.vmid ? `VM ${String(agent.vmid)}` : "-"}</strong>
            </header>
            <dl className="fleet-detail-grid">
              <div><dt>Computer</dt><dd>{fallbackText(agent.computer_name || agent.serial_number)}</dd></div>
              <div><dt>IP</dt><dd>{fallbackText(agent.primary_ipv4)}</dd></div>
              <div><dt>Guest</dt><dd>{fallbackText(agent.qga_state)}</dd></div>
              <div><dt>Phase</dt><dd>{fallbackText(agent.current_phase)}</dd></div>
              <div><dt>Heartbeat</dt><dd>{formatShortDateTime(agent.last_heartbeat_at)}</dd></div>
              <div><dt>Version</dt><dd>{fallbackText(agent.agent_version)}</dd></div>
            </dl>
            <Chips labels={[
              ...(agent.domain_joined ? ["domain"] : []),
              ...(agent.entra_joined ? ["Entra ID"] : []),
              ...(agent.hash_capture_supported ? ["hash"] : [])
            ]} />
            <div className="fleet-actions">
              {agent.approval_status === "pending" && agent.approval_id ? (
                <ActionButton label="Approve" onClick={() => { onApprove(agent); }} />
              ) : null}
              <ActionButton label="Update" onClick={() => { onUpdate(agent); }} />
              <ActionButton label="Delete" tone="danger" onClick={() => { onDelete(agent); }} />
            </div>
          </article>
        )) : <p className="empty">No agent heartbeats yet.</p>}
      </div>
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
