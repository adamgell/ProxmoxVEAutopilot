import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
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
import { VmEvidencePanels } from "../components/VmEvidencePanels";
import { VmActionWorkspace, type ScreenshotWorkspaceState, type VmActionMode, type VmActionSelection } from "../components/VmActionWorkspace";
import type {
  AgentFleetRow,
  AppBootstrap,
  LabBubble,
  LabBubbleAsset,
  LabBubbleTopology,
  LiveSocketMessage,
  VmDetailEvidenceResponse,
  VmFleetRow,
  VmsFleetResponse
} from "../contracts";
import { connectFleetLive } from "../liveSocket";
import {
  buildFleetMachineRows,
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
  bubble_topology: {
    workstation_fleets: [],
    critical_infrastructure: [],
    connected_services: [],
    unassigned_assets: [],
    warnings: [],
    gate_states: []
  },
  ap_error: "",
  cache_refreshing: false,
  generated_at: ""
};

const emptyBubbleTopology: LabBubbleTopology = {
  workstation_fleets: [],
  critical_infrastructure: [],
  connected_services: [],
  unassigned_assets: [],
  warnings: [],
  gate_states: []
};

type SendLiveMessage = (message: Readonly<Record<string, unknown>>) => boolean;
type ActionIcon = LucideIcon;
type BubbleAssignment = {
  readonly bubble: LabBubble;
  readonly asset: LabBubbleAsset;
};

function bubbleSort(left: LabBubble, right: LabBubble): number {
  return left.name.localeCompare(right.name, undefined, { sensitivity: "base" });
}

function topologyBubbles(topology: LabBubbleTopology): readonly LabBubble[] {
  const byId = new Map<string, LabBubble>();
  for (const fleet of topology.workstation_fleets) {
    byId.set(fleet.bubble.id, fleet.bubble);
  }
  for (const node of topology.critical_infrastructure) {
    byId.set(node.bubble.id, node.bubble);
  }
  for (const service of topology.connected_services) {
    byId.set(service.bubble.id, service.bubble);
  }
  return Array.from(byId.values()).toSorted(bubbleSort);
}

function topologyAssignmentsByVmid(topology: LabBubbleTopology): ReadonlyMap<number, BubbleAssignment> {
  const byVmid = new Map<number, BubbleAssignment>();
  for (const fleet of topology.workstation_fleets) {
    for (const asset of fleet.assets ?? []) {
      if (typeof asset.vmid === "number") {
        byVmid.set(asset.vmid, { bubble: fleet.bubble, asset });
      }
    }
  }
  for (const node of topology.critical_infrastructure) {
    if (typeof node.asset.vmid === "number") {
      byVmid.set(node.asset.vmid, { bubble: node.bubble, asset: node.asset });
    }
  }
  return byVmid;
}

type BubbleDraftMode = "create" | "edit";

type BubbleFormValues = {
  readonly name: string;
  readonly domain_name: string;
  readonly netbios_name: string;
  readonly cidr: string;
  readonly gateway_ip: string;
  readonly dhcp_scope: string;
  readonly dhcp_pool_start: string;
  readonly dhcp_pool_end: string;
  readonly lifecycle_state: string;
  readonly isolation_status: string;
};

type BubbleFormField = keyof BubbleFormValues;

const blankBubbleForm: BubbleFormValues = {
  name: "",
  domain_name: "",
  netbios_name: "",
  cidr: "",
  gateway_ip: "",
  dhcp_scope: "",
  dhcp_pool_start: "",
  dhcp_pool_end: "",
  lifecycle_state: "planned",
  isolation_status: "planned"
};

function bubbleFormFromBubble(bubble: LabBubble): BubbleFormValues {
  return {
    name: bubble.name,
    domain_name: bubble.domain_name ?? "",
    netbios_name: bubble.netbios_name ?? "",
    cidr: bubble.cidr ?? "",
    gateway_ip: bubble.gateway_ip ?? "",
    dhcp_scope: bubble.dhcp_scope ?? "",
    dhcp_pool_start: bubble.dhcp_pool_start ?? "",
    dhcp_pool_end: bubble.dhcp_pool_end ?? "",
    lifecycle_state: bubble.lifecycle_state ?? "planned",
    isolation_status: bubble.isolation_status ?? "planned"
  };
}

function bubbleFormPayload(values: BubbleFormValues): Readonly<Record<string, unknown>> {
  return {
    name: values.name.trim(),
    domain_name: values.domain_name.trim(),
    netbios_name: values.netbios_name.trim(),
    cidr: values.cidr.trim(),
    gateway_ip: values.gateway_ip.trim(),
    dhcp_scope: values.dhcp_scope.trim(),
    dhcp_pool_start: values.dhcp_pool_start.trim(),
    dhcp_pool_end: values.dhcp_pool_end.trim(),
    lifecycle_state: values.lifecycle_state.trim() || "planned",
    isolation_status: values.isolation_status.trim() || "planned"
  };
}

type MachineTagDraft = {
  readonly rowId: string;
  readonly bubbleId: string;
  readonly assetRole: string;
};

async function deleteJson(path: string): Promise<void> {
  const response = await fetch(path, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { accept: "application/json" }
  });
  if (!response.ok) {
    throw new Error(`DELETE ${path} failed: ${response.statusText || String(response.status)}`);
  }
}

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
  const [detailEvidence, setDetailEvidence] = useState<VmDetailEvidenceResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [bubbleDraftMode, setBubbleDraftMode] = useState<BubbleDraftMode | null>(null);
  const [bubbleDraftId, setBubbleDraftId] = useState<string | null>(null);
  const [bubbleDraft, setBubbleDraft] = useState<BubbleFormValues>(blankBubbleForm);
  const [deleteBubbleId, setDeleteBubbleId] = useState<string | null>(null);
  const [machineTagDraft, setMachineTagDraft] = useState<MachineTagDraft | null>(null);

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

  const loadDetail = useCallback(async (vmid: number) => {
    setDetailLoading(true);
    try {
      const evidence = await fetchJson<VmDetailEvidenceResponse>(`/api/vms/${String(vmid)}/detail`);
      setDetailEvidence(evidence);
      setDetailError("");
    } catch (err) {
      setDetailEvidence(null);
      setDetailError(err instanceof Error ? err.message : "Failed to load VM evidence");
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    if (detailVmid === null) {
      return;
    }
    const timer = window.setTimeout(() => {
      void loadDetail(detailVmid);
    }, 0);
    return () => {
      window.clearTimeout(timer);
    };
  }, [detailVmid, loadDetail]);

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
          if (detailVmid === resultVmid) {
            void loadDetail(resultVmid);
          }
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
  }, [detailVmid, load, loadDetail]);

  const counts = useMemo(() => summarizeFleet(fleet), [fleet]);
  const machineRows = useMemo(() => buildFleetMachineRows(fleet), [fleet]);
  const bubbleTopology = fleet.bubble_topology ?? emptyBubbleTopology;
  const bubbleOptions = useMemo(() => topologyBubbles(bubbleTopology), [bubbleTopology]);
  const assignmentsByVmid = useMemo(() => topologyAssignmentsByVmid(bubbleTopology), [bubbleTopology]);
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
      return true;
    } catch (err) {
      setActionStatus(err instanceof Error ? err.message : `${label} failed`);
      return false;
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

  const createBubble = useCallback(() => {
    setDeleteBubbleId(null);
    setBubbleDraftMode("create");
    setBubbleDraftId(null);
    setBubbleDraft(blankBubbleForm);
  }, []);

  const editBubble = useCallback((bubble: LabBubble) => {
    setDeleteBubbleId(null);
    setBubbleDraftMode("edit");
    setBubbleDraftId(bubble.id);
    setBubbleDraft(bubbleFormFromBubble(bubble));
  }, []);

  const updateBubbleDraft = useCallback((field: BubbleFormField, value: string) => {
    setBubbleDraft((current) => ({ ...current, [field]: value }));
  }, []);

  const cancelBubbleDraft = useCallback(() => {
    setBubbleDraftMode(null);
    setBubbleDraftId(null);
    setBubbleDraft(blankBubbleForm);
  }, []);

  const saveBubbleDraft = useCallback(() => {
    const payload = bubbleFormPayload(bubbleDraft);
    const bubbleName = bubbleDraft.name.trim() || "bubble";
    if (!bubbleDraftMode || !bubbleName.trim()) {
      return;
    }
    if (bubbleDraftMode === "create") {
      void runAction(`Create bubble ${bubbleName}`, () => postJson("/api/bubbles", payload)).then((ok) => {
        if (ok) {
          cancelBubbleDraft();
        }
      });
      return;
    }
    if (!bubbleDraftId) {
      return;
    }
    void runAction(`Edit bubble ${bubbleName}`, () => fetchJson(`/api/bubbles/${bubbleDraftId}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload)
    })).then((ok) => {
      if (ok) {
        cancelBubbleDraft();
      }
    });
  }, [bubbleDraft, bubbleDraftId, bubbleDraftMode, cancelBubbleDraft, runAction]);

  const requestDeleteBubble = useCallback((bubble: LabBubble) => {
    setBubbleDraftMode(null);
    setBubbleDraftId(null);
    setBubbleDraft(blankBubbleForm);
    setDeleteBubbleId(bubble.id);
  }, []);

  const cancelDeleteBubble = useCallback(() => {
    setDeleteBubbleId(null);
  }, []);

  const deleteBubble = useCallback((bubble: LabBubble) => {
    void runAction(`Delete bubble ${bubble.name}`, () => deleteJson(`/api/bubbles/${bubble.id}`)).then((ok) => {
      if (ok) {
        setDeleteBubbleId(null);
      }
    });
  }, [runAction]);

  const tagMachine = useCallback((row: FleetMachineRow) => {
    if (row.vmid === undefined) {
      return;
    }
    if (!bubbleOptions.length) {
      setActionStatus("Create a bubble before tagging VM assets.");
      return;
    }
    const current = assignmentsByVmid.get(row.vmid);
    setMachineTagDraft({
      rowId: row.id,
      bubbleId: current?.bubble.id ?? bubbleOptions[0]?.id ?? "",
      assetRole: current?.asset.asset_role ?? "workstation"
    });
  }, [assignmentsByVmid, bubbleOptions]);

  const updateMachineTagDraft = useCallback((field: "bubbleId" | "assetRole", value: string) => {
    setMachineTagDraft((current) => current ? { ...current, [field]: value } : current);
  }, []);

  const cancelMachineTagDraft = useCallback(() => {
    setMachineTagDraft(null);
  }, []);

  const saveMachineTag = useCallback((row: FleetMachineRow) => {
    if (row.vmid === undefined || !machineTagDraft || machineTagDraft.rowId !== row.id) {
      return;
    }
    const targetBubble = bubbleOptions.find((bubble) => bubble.id === machineTagDraft.bubbleId);
    if (!targetBubble) {
      setActionStatus("Bubble selection did not match an existing bubble.");
      return;
    }
    const role = machineTagDraft.assetRole.trim();
    if (!role) {
      return;
    }
    const current = assignmentsByVmid.get(row.vmid);
    void runAction(`Tag VM ${String(row.vmid)}`, async () => {
      if (!current) {
        await postJson(`/api/bubbles/${targetBubble.id}/assets`, {
          asset_type: "vm",
          asset_role: role,
          vmid: row.vmid,
          membership_state: "active",
          evidence_state: "operator_tagged",
          notes: `Tagged from React VMs as ${row.name}`
        });
        return;
      }
      if (current.bubble.id !== targetBubble.id) {
        await postJson(`/api/bubbles/${current.bubble.id}/assets/${current.asset.id}/move`, {
          target_bubble_id: targetBubble.id,
          reason: `React VMs retag to ${targetBubble.name}`
        });
      }
      await fetchJson(`/api/bubbles/${targetBubble.id}/assets/${current.asset.id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          asset_role: role,
          vmid: row.vmid,
          membership_state: "active"
        })
      });
    }).then((ok) => {
      if (ok) {
        setMachineTagDraft(null);
      }
    });
  }, [assignmentsByVmid, bubbleOptions, machineTagDraft, runAction]);

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
        {detailLoading ? <div className="progress" aria-label="Loading VM evidence"><span /></div> : null}
        {error ? <p className="notice" role="status">{error}</p> : null}
        {detailError ? <p className="notice" role="status">{detailError}</p> : null}
        {actionStatus ? <p className="notice" role="status">{actionStatus}</p> : null}
        {detailRow?.vm ? (
          <VmDetailWorkspace
            row={detailRow}
            evidence={detailEvidence}
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
      action={<a className="action-link" href="/react/monitoring">Signals</a>}
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

      <BubbleTopologyOverview
        topology={bubbleTopology}
        onCreateBubble={createBubble}
        onEditBubble={editBubble}
        onRequestDeleteBubble={requestDeleteBubble}
        onConfirmDeleteBubble={deleteBubble}
        onCancelDeleteBubble={cancelDeleteBubble}
        bubbleDraftMode={bubbleDraftMode}
        bubbleDraftId={bubbleDraftId}
        bubbleDraft={bubbleDraft}
        onBubbleDraftChange={updateBubbleDraft}
        onSaveBubbleDraft={saveBubbleDraft}
        onCancelBubbleDraft={cancelBubbleDraft}
        deleteBubbleId={deleteBubbleId}
      />

      <section className="fleet-lanes" aria-label="Fleet lanes">
        <div className="fleet-primary-stack">
          <FleetMachineTable
            rows={filteredMachines}
            onCreateAgent={createAgent}
            onTagMachine={tagMachine}
            tagDraft={machineTagDraft}
            bubbleOptions={bubbleOptions}
            onTagDraftChange={updateMachineTagDraft}
            onSaveTag={saveMachineTag}
            onCancelTag={cancelMachineTagDraft}
            assignmentsByVmid={assignmentsByVmid}
          />
        </div>
      </section>
    </PageFrame>
  );
}

function FleetMachineTable({
  rows,
  onCreateAgent,
  onTagMachine,
  tagDraft,
  bubbleOptions,
  onTagDraftChange,
  onSaveTag,
  onCancelTag,
  assignmentsByVmid
}: {
  readonly rows: readonly FleetMachineRow[];
  readonly onCreateAgent: () => void;
  readonly onTagMachine: (row: FleetMachineRow) => void;
  readonly tagDraft: MachineTagDraft | null;
  readonly bubbleOptions: readonly LabBubble[];
  readonly onTagDraftChange: (field: "bubbleId" | "assetRole", value: string) => void;
  readonly onSaveTag: (row: FleetMachineRow) => void;
  readonly onCancelTag: () => void;
  readonly assignmentsByVmid: ReadonlyMap<number, BubbleAssignment>;
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
                <th scope="col">Bubble</th>
                <th scope="col">Tag</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <Fragment key={row.id}>
                  <MachineRow
                    row={row}
                    assignment={row.vmid === undefined ? undefined : assignmentsByVmid.get(row.vmid)}
                    onTag={onTagMachine}
                  />
                  {tagDraft?.rowId === row.id && row.vmid !== undefined ? (
                    <tr className="machine-tag-row">
                      <td colSpan={11}>
                        <MachineTagEditor
                          row={row}
                          values={tagDraft}
                          bubbleOptions={bubbleOptions}
                          onChange={onTagDraftChange}
                          onSave={() => { onSaveTag(row); }}
                          onCancel={onCancelTag}
                        />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              ))}
            </tbody>
          </table>
        ) : <p className="empty">No fleet machines found.</p>}
      </div>
    </Panel>
  );
}

function MachineTagEditor({
  row,
  values,
  bubbleOptions,
  onChange,
  onSave,
  onCancel
}: {
  readonly row: FleetMachineRow;
  readonly values: MachineTagDraft;
  readonly bubbleOptions: readonly LabBubble[];
  readonly onChange: (field: "bubbleId" | "assetRole", value: string) => void;
  readonly onSave: () => void;
  readonly onCancel: () => void;
}) {
  const vmid = row.vmid ?? 0;
  return (
    <form
      className="machine-tag-editor"
      aria-label={`Tag VM ${String(vmid)} into a bubble`}
      onSubmit={(event) => {
        event.preventDefault();
        onSave();
      }}
    >
      <label className="bubble-form-field">
        <span>Bubble</span>
        <select
          aria-label={`Bubble for VM ${String(vmid)}`}
          value={values.bubbleId}
          onChange={(event) => { onChange("bubbleId", event.target.value); }}
        >
          {bubbleOptions.map((bubble) => (
            <option key={bubble.id} value={bubble.id}>
              {bubble.name}{bubble.domain_name ? ` / ${bubble.domain_name}` : ""}
            </option>
          ))}
        </select>
      </label>
      <label className="bubble-form-field">
        <span>Asset role</span>
        <input
          aria-label={`Asset role for VM ${String(vmid)}`}
          value={values.assetRole}
          onChange={(event) => { onChange("assetRole", event.target.value); }}
        />
      </label>
      <div className="machine-tag-editor__actions">
        <button type="submit" className="fleet-action fleet-action--command" aria-label={`Save VM ${String(vmid)} bubble tag`}>
          Save tag
        </button>
        <button type="button" className="fleet-action" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function MachineRow({
  row,
  assignment,
  onTag
}: {
  readonly row: FleetMachineRow;
  readonly assignment: BubbleAssignment | undefined;
  readonly onTag: (row: FleetMachineRow) => void;
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
      <td>
        <span className="machine-primary-value">
          {assignment ? `${assignment.bubble.name} / ${roleLabel(assignment.asset.asset_role)}` : "-"}
        </span>
      </td>
      <td>
        {row.vmid !== undefined ? (
          <button
            type="button"
            className="fleet-action"
            aria-label={`Tag VM ${String(row.vmid)}`}
            onClick={() => { onTag(row); }}
          >
            Tag
          </button>
        ) : <span className="machine-primary-value">-</span>}
      </td>
    </tr>
  );
}

function VmDetailWorkspace({
  row,
  evidence,
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
  readonly evidence: VmDetailEvidenceResponse | null;
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

      <VmEvidencePanels
        vmid={vm.vmid}
        evidence={evidence}
        onRefreshScreenshot={() => { onScreenshot(vm); }}
      />

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

function readinessClass(ok: unknown): string {
  return ok === true ? "status status--good" : "status status--bad";
}

function readinessLabel(ok: unknown): string {
  return ok === true ? "ready" : "waiting";
}

function roleLabel(value: string | undefined): string {
  return fallbackText(value).replaceAll("_", " ");
}

function gateLabel(gate: Readonly<Record<string, unknown>> | undefined): string {
  if (!gate) {
    return "-";
  }
  const state = typeof gate.state === "string" ? gate.state : "";
  const allowed = gate.allowed === true;
  if (state) {
    return allowed ? state : `blocked: ${state}`;
  }
  return allowed ? "allowed" : "blocked";
}

function BubbleEditor({
  mode,
  bubbleName,
  values,
  onChange,
  onSave,
  onCancel
}: {
  readonly mode: BubbleDraftMode;
  readonly bubbleName?: string;
  readonly values: BubbleFormValues;
  readonly onChange: (field: BubbleFormField, value: string) => void;
  readonly onSave: () => void;
  readonly onCancel: () => void;
}) {
  const saveLabel = mode === "create" ? "Create bubble" : `Save bubble ${bubbleName ?? values.name}`;
  return (
    <form
      className="bubble-form"
      aria-label={mode === "create" ? "Create bubble" : `Edit bubble ${bubbleName ?? values.name}`}
      onSubmit={(event) => {
        event.preventDefault();
        onSave();
      }}
    >
      <div className="bubble-form-grid">
        <BubbleTextField label="Bubble name" field="name" value={values.name} onChange={onChange} required />
        <BubbleTextField label="Domain name" field="domain_name" value={values.domain_name} onChange={onChange} />
        <BubbleTextField label="NetBIOS name" field="netbios_name" value={values.netbios_name} onChange={onChange} />
        <BubbleTextField label="Isolated CIDR" field="cidr" value={values.cidr} onChange={onChange} />
        <BubbleTextField label="Gateway IP" field="gateway_ip" value={values.gateway_ip} onChange={onChange} />
        <BubbleTextField label="DHCP scope" field="dhcp_scope" value={values.dhcp_scope} onChange={onChange} />
        <BubbleTextField label="DHCP pool start" field="dhcp_pool_start" value={values.dhcp_pool_start} onChange={onChange} />
        <BubbleTextField label="DHCP pool end" field="dhcp_pool_end" value={values.dhcp_pool_end} onChange={onChange} />
        <BubbleTextField label="Lifecycle state" field="lifecycle_state" value={values.lifecycle_state} onChange={onChange} />
        <BubbleTextField label="Isolation status" field="isolation_status" value={values.isolation_status} onChange={onChange} />
      </div>
      <div className="bubble-form-actions">
        <button type="submit" className="fleet-action fleet-action--command" aria-label={saveLabel}>
          {mode === "create" ? "Create bubble" : "Save"}
        </button>
        <button type="button" className="fleet-action" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function BubbleTextField({
  label,
  field,
  value,
  onChange,
  required = false
}: {
  readonly label: string;
  readonly field: BubbleFormField;
  readonly value: string;
  readonly onChange: (field: BubbleFormField, value: string) => void;
  readonly required?: boolean;
}) {
  return (
    <label className="bubble-form-field">
      <span>{label}</span>
      <input
        aria-label={label}
        value={value}
        required={required}
        onChange={(event) => { onChange(field, event.target.value); }}
      />
    </label>
  );
}

function BubbleTopologyOverview({
  topology,
  onCreateBubble,
  onEditBubble,
  onRequestDeleteBubble,
  onConfirmDeleteBubble,
  onCancelDeleteBubble,
  bubbleDraftMode,
  bubbleDraftId,
  bubbleDraft,
  onBubbleDraftChange,
  onSaveBubbleDraft,
  onCancelBubbleDraft,
  deleteBubbleId
}: {
  readonly topology: LabBubbleTopology;
  readonly onCreateBubble: () => void;
  readonly onEditBubble: (bubble: LabBubble) => void;
  readonly onRequestDeleteBubble: (bubble: LabBubble) => void;
  readonly onConfirmDeleteBubble: (bubble: LabBubble) => void;
  readonly onCancelDeleteBubble: () => void;
  readonly bubbleDraftMode: BubbleDraftMode | null;
  readonly bubbleDraftId: string | null;
  readonly bubbleDraft: BubbleFormValues;
  readonly onBubbleDraftChange: (field: BubbleFormField, value: string) => void;
  readonly onSaveBubbleDraft: () => void;
  readonly onCancelBubbleDraft: () => void;
  readonly deleteBubbleId: string | null;
}) {
  const fleets = topology.workstation_fleets;
  const infra = topology.critical_infrastructure;
  const services = topology.connected_services;
  const gateByBubble = new Map(topology.gate_states.map((gate) => [gate.bubble_id, gate]));
  return (
    <section className="bubble-layout" aria-label="Tenant bubbles">
      <div className="bubble-primary-stack">
        <Panel title="VM Workstation Fleets">
          <div className="fleet-lane-command">
            <button type="button" className="fleet-action fleet-action--command" onClick={onCreateBubble}>
              <span>New bubble</span>
            </button>
          </div>
          {bubbleDraftMode === "create" ? (
            <BubbleEditor
              mode="create"
              values={bubbleDraft}
              onChange={onBubbleDraftChange}
              onSave={onSaveBubbleDraft}
              onCancel={onCancelBubbleDraft}
            />
          ) : null}
          {topology.warnings.length ? (
            <p className="notice" role="status">{topology.warnings.join(" ")}</p>
          ) : null}
          {fleets.length ? (
            <div className="bubble-fleet-grid">
              {fleets.map((fleet) => {
                const gate = gateByBubble.get(fleet.bubble.id);
                return (
                  <article key={fleet.bubble.id} className="bubble-card">
                    <header>
                      <div>
                        <span className="status status--active">{fallbackText(fleet.bubble.lifecycle_state || "planned")}</span>
                        <h3>{fleet.bubble.name}</h3>
                      </div>
                      <div className="bubble-card-actions">
                        <strong>{String(fleet.workstation_count ?? 0)} VMs</strong>
                        <button
                          type="button"
                          className="fleet-action"
                          aria-label={`Edit bubble ${fleet.bubble.name}`}
                          onClick={() => { onEditBubble(fleet.bubble); }}
                        >
                          Edit
                        </button>
                        <button
                          type="button"
                          className="fleet-action fleet-action--danger"
                          aria-label={`Delete bubble ${fleet.bubble.name}`}
                          onClick={() => { onRequestDeleteBubble(fleet.bubble); }}
                        >
                          Delete
                        </button>
                      </div>
                    </header>
                    {bubbleDraftMode === "edit" && bubbleDraftId === fleet.bubble.id ? (
                      <BubbleEditor
                        mode="edit"
                        bubbleName={fleet.bubble.name}
                        values={bubbleDraft}
                        onChange={onBubbleDraftChange}
                        onSave={onSaveBubbleDraft}
                        onCancel={onCancelBubbleDraft}
                      />
                    ) : null}
                    {deleteBubbleId === fleet.bubble.id ? (
                      <div className="bubble-delete-confirm" role="group" aria-label={`Delete ${fleet.bubble.name}`}>
                        <strong>Delete {fleet.bubble.name}?</strong>
                        <button
                          type="button"
                          className="fleet-action fleet-action--danger"
                          aria-label={`Confirm delete bubble ${fleet.bubble.name}`}
                          onClick={() => { onConfirmDeleteBubble(fleet.bubble); }}
                        >
                          Confirm
                        </button>
                        <button type="button" className="fleet-action" onClick={onCancelDeleteBubble}>
                          Cancel
                        </button>
                      </div>
                    ) : null}
                    <dl className="fleet-detail-grid">
                      <div><dt>Domain</dt><dd>{fallbackText(fleet.bubble.domain_name)}</dd></div>
                      <div><dt>Network</dt><dd>{fallbackText(fleet.bubble.cidr)}</dd></div>
                      <div><dt>DHCP</dt><dd>{fallbackText(fleet.bubble.dhcp_scope)}</dd></div>
                      <div><dt>Running</dt><dd>{String(fleet.running_count ?? 0)}</dd></div>
                    </dl>
                    <div className="chip-row">
                      <span className={readinessClass(fleet.readiness?.dc_ready)}>DC {readinessLabel(fleet.readiness?.dc_ready)}</span>
                      <span className={readinessClass(fleet.readiness?.dns_ready)}>DNS {readinessLabel(fleet.readiness?.dns_ready)}</span>
                      <span className={readinessClass(fleet.readiness?.dhcp_ready)}>DHCP {readinessLabel(fleet.readiness?.dhcp_ready)}</span>
                    </div>
                    <p className="muted">Workgroup launch: {gateLabel(gate?.workgroup)} / Domain launch: {gateLabel(gate?.domain_join)}</p>
                  </article>
                );
              })}
            </div>
          ) : <p className="empty">No workstation bubbles tagged yet.</p>}
        </Panel>
      </div>
      <div className="bubble-side-stack">
        <Panel title="Critical Infrastructure">
          {infra.length ? (
            <div className="fleet-card-list fleet-card-list--compact">
              {infra.map((node) => (
                <article key={node.asset.id} className="fleet-card">
                  <header>
                    <div>
                      <span className="status">{node.bubble.name}</span>
                      <h3>{roleLabel(node.role)}</h3>
                    </div>
                    <strong>{node.asset.vmid ? `VM ${String(node.asset.vmid)}` : fallbackText(node.asset.agent_id)}</strong>
                  </header>
                  <dl className="fleet-detail-grid">
                    <div><dt>State</dt><dd>{fallbackText(node.asset.membership_state)}</dd></div>
                    <div><dt>Evidence</dt><dd>{fallbackText(node.asset.evidence_state)}</dd></div>
                    <div><dt>Agent</dt><dd>{fallbackText(node.agent?.agent_id ?? node.asset.agent_id)}</dd></div>
                    <div><dt>Runtime</dt><dd>{fallbackText(node.vm?.status)}</dd></div>
                  </dl>
                </article>
              ))}
            </div>
          ) : <p className="empty">No infrastructure assets tagged yet.</p>}
        </Panel>
        <Panel title="Connected Services">
          {services.length ? (
            <div className="fleet-card-list fleet-card-list--compact">
              {services.map((service) => (
                <article key={service.id} className="fleet-card">
                  <header>
                    <div>
                      <span className={service.readiness_state === "ready" ? "status status--good" : "status"}>{fallbackText(service.readiness_state)}</span>
                      <h3>{service.service_name}</h3>
                    </div>
                    <strong>{service.bubble.name}</strong>
                  </header>
                  <dl className="fleet-detail-grid">
                    <div><dt>Kind</dt><dd>{roleLabel(service.service_kind)}</dd></div>
                    <div><dt>Scope</dt><dd>{fallbackText(service.scope)}</dd></div>
                    <div><dt>Provider</dt><dd>{fallbackText(service.provider_asset_id)}</dd></div>
                    <div><dt>Consumers</dt><dd>{String(service.consumer_refs?.length ?? 0)}</dd></div>
                  </dl>
                </article>
              ))}
            </div>
          ) : <p className="empty">No connected services linked yet.</p>}
        </Panel>
      </div>
    </section>
  );
}
