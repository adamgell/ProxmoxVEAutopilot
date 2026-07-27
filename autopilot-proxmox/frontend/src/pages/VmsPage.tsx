import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useState
} from "react";
import {
  BadgeCheck,
  Camera,
  CircleStop,
  FileArchive,
  Hash,
  Keyboard,
  Monitor,
  Pencil,
  Play,
  Power,
  RefreshCw,
  RotateCcw,
  TerminalSquare,
  Trash2,
  UserPlus
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import {
  fetchJson,
  postJson
} from "../apiClient";
import {
  PageFrame
} from "../components/Shell";
import {
  Metric,
  Panel
} from "../components/ui";
import {
  useModalDialog
} from "../components/useModalDialog";
import {
  type BubbleAssignment,
  type BubbleBoundNetwork,
  type BubbleDraftMode,
  type BubbleFormField,
  type BubbleFormValues,
  type InfraDraft,
  type InfraEditDraft,
  type InfraMoveDraft,
  type OrphanVnet,
  type ServiceDraft,
  type ServiceDraftMode,
  BubbleTopologyOverview,
  blankInfraDraft,
  blankServiceDraft,
  bubbleFormFromBubble,
  bubbleFormPayload,
  topologyAssets,
  topologyAssignmentsByVmid,
  topologyBubbles,
  parseDhcpRange,
  roleLabel,
  vmAssetLabel,
  credentialIdsFromService,
  deleteJson
} from "./fleet/BubbleTopology";

import {
  VmEvidencePanels
} from "../components/VmEvidencePanels";
import {
  VmActionWorkspace,
  type ScreenshotWorkspaceState,
  type VmActionMode,
  type VmActionSelection
} from "../components/VmActionWorkspace";
import type {
  AgentFleetRow,
  AppBootstrap,
  CredentialSummary,
  LabBubble,
  LabBubbleConnectedService,
  LabBubbleInfrastructureNode,
  LabBubbleTopology,
  LiveSocketMessage,
  VmDetailEvidenceResponse,
  VmFleetRow,
  VmsFleetResponse
} from "../contracts";
import {
  connectFleetLive
} from "../liveSocket";
import {
  reactHrefForUiPath
} from "../routes";
import {
  buildFleetMachineRows,
  fleetAgentClass,
  fleetAgentLabel,
  fleetManagedByLabel,
  fleetOsName,
  fleetOsVersion,
  fleetRuntimeLabel,
  type FleetMachineRow,
  type FleetPreset,
  fallbackText,
  formatRelativeAge,
  formatShortDateTime,
  machineAttention,
  machineMatchesFilter,
  machineMatchesPreset,
  summarizeFleet,
  vmDisplayName
} from "../viewModels";

const emptyFleet: VmsFleetResponse = {
  vms: [],
  proxmox_vms: [],
  missing_vms: [],
  agents: [],
  agent_identity_warnings: [],
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
type ActionStatusTone = "info" | "bad";

type FleetView = "machines" | "topology";

type ActionStatusLink = {
  readonly href: string;
  readonly label: string;
};

type CollectLogsResponse = {
  readonly ok: boolean;
  readonly job_id: string;
  readonly work_item_id: string;
  readonly vmid: number;
  readonly job_type: string;
  readonly status_url: string;
  readonly web_url: string;
};

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


interface BubbleSdnAdoption {
  readonly vnet: string;
  readonly zone: string;
  readonly subnet: string;
}

type AgentFormDraft = {
  mode: "create" | "edit";
  agentId: string;
  vmid: string;
  computerName: string;
  serialNumber: string;
  agentVersion: string;
};

type MachineTagDraft = {
  readonly rowId: string;
  readonly bubbleId: string;
  readonly assetRole: string;
};

const FLEET_PRESETS: readonly { readonly id: FleetPreset; readonly label: string }[] = [
  { id: "all", label: "All" },
  { id: "attention", label: "Attention" },
  { id: "stale", label: "Stale" },
  { id: "pending", label: "Pending" },
  { id: "no-agent", label: "No agent" }
];

function countLabel(count: number, noun: string): string {
  return `${String(count)} ${noun}${count === 1 ? "" : "s"}`;
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
  const [preset, setPreset] = useState<FleetPreset>("all");
  const [view, setView] = useState<FleetView>(() =>
    new URLSearchParams(window.location.search).get("view") === "topology" ? "topology" : "machines"
  );
  const selectView = useCallback((next: FleetView) => {
    setView(next);
    window.history.replaceState({}, "", `${window.location.pathname}${next === "machines" ? "" : `?view=${next}`}`);
  }, []);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionStatus, setActionStatusMessage] = useState("");
  const [actionStatusTone, setActionStatusTone] = useState<ActionStatusTone>("info");
  const [actionStatusLink, setActionStatusLink] = useState<ActionStatusLink | null>(null);
  // Informational statuses ("Rename VM 213 complete") clear themselves; errors
  // stay put until the next action replaces them.
  const setActionStatus = useCallback((message: string, tone: ActionStatusTone = "info") => {
    setActionStatusMessage(message);
    setActionStatusTone(tone);
  }, []);
  const [socketState, setSocketState] = useState("closed");
  const [sendLive, setSendLive] = useState<SendLiveMessage | null>(null);
  const [activeAction, setActiveAction] = useState<VmActionSelection | null>(null);
  // routes.ts rewrites /vms/{vmid}/console to /react/vms/{vmid}?action=console,
  // but nothing ever read location.search, so that deep link opened an empty
  // action zone. Seeded once, after the fleet load supplies the VM.
  const requestedAction = useMemo<VmActionMode | null>(() => {
    const value = new URLSearchParams(window.location.search).get("action");
    return value === "console" || value === "screenshot" ? value : null;
  }, []);
  const [seedDismissed, setSeedDismissed] = useState(false);
  const [screenshot, setScreenshot] = useState<ScreenshotWorkspaceState>({ status: "idle" });
  const [detailEvidence, setDetailEvidence] = useState<VmDetailEvidenceResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [credentialSummaries, setCredentialSummaries] = useState<readonly CredentialSummary[]>([]);
  const [bubbleDraftMode, setBubbleDraftMode] = useState<BubbleDraftMode | null>(null);
  const [bubbleDraftId, setBubbleDraftId] = useState<string | null>(null);
  const [bubbleDraft, setBubbleDraft] = useState<BubbleFormValues>(blankBubbleForm);
  const [orphanVnets, setOrphanVnets] = useState<readonly OrphanVnet[]>([]);
  const [bubbleAdoptedVnet, setBubbleAdoptedVnet] = useState<BubbleSdnAdoption | null>(null);
  const [bubbleBoundNetwork, setBubbleBoundNetwork] = useState<BubbleBoundNetwork | null>(null);
  const [deleteBubbleId, setDeleteBubbleId] = useState<string | null>(null);
  const [machineTagDraft, setMachineTagDraft] = useState<MachineTagDraft | null>(null);
  const [infraDraftOpen, setInfraDraftOpen] = useState(false);
  const [infraDraft, setInfraDraft] = useState<InfraDraft>(blankInfraDraft);
  const [infraEditDraft, setInfraEditDraft] = useState<InfraEditDraft | null>(null);
  const [infraMoveDraft, setInfraMoveDraft] = useState<InfraMoveDraft | null>(null);
  const [retireInfraId, setRetireInfraId] = useState<string | null>(null);
  const [serviceDraftMode, setServiceDraftMode] = useState<ServiceDraftMode | null>(null);
  const [serviceDraftId, setServiceDraftId] = useState<string | null>(null);
  const [serviceDraft, setServiceDraft] = useState<ServiceDraft>(blankServiceDraft);
  const [deleteServiceId, setDeleteServiceId] = useState<string | null>(null);

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

  // Only the topology view's service editor reads these, so the detail route
  // and the default machines view stop paying for the request.
  useEffect(() => {
    if (view !== "topology" || detailVmid !== null) {
      return;
    }
    void (async () => {
      try {
        const credentials = await fetchJson<CredentialSummary[]>("/api/credentials");
        setCredentialSummaries(credentials);
      } catch {
        // The service editor's picker degrades to an empty list; the
        // credential inventory itself lives at /react/credentials.
        setCredentialSummaries([]);
      }
    })();
  }, [detailVmid, view]);

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
          setActionStatus(message.detail || message.error || "Live action failed", "bad");
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
  }, [detailVmid, load, loadDetail, setActionStatus]);

  const counts = useMemo(() => summarizeFleet(fleet), [fleet]);
  const machineRows = useMemo(() => buildFleetMachineRows(fleet), [fleet]);
  const bubbleTopology = fleet.bubble_topology ?? emptyBubbleTopology;
  const bubbleOptions = useMemo(() => topologyBubbles(bubbleTopology), [bubbleTopology]);
  const assignmentsByVmid = useMemo(() => topologyAssignmentsByVmid(bubbleTopology), [bubbleTopology]);
  const bubbleAssets = useMemo(() => topologyAssets(bubbleTopology), [bubbleTopology]);
  const infraVmCandidates = useMemo(
    () => fleet.proxmox_vms?.length ? fleet.proxmox_vms : (fleet.vms.length ? fleet.vms : bubbleTopology.unassigned_assets),
    [bubbleTopology.unassigned_assets, fleet.proxmox_vms, fleet.vms]
  );
  const detailRow = useMemo(
    () => detailVmid === null ? undefined : machineRows.find((row) => row.vmid === detailVmid),
    [detailVmid, machineRows]
  );
  const filteredMachines = useMemo(
    () => machineRows.filter((row) => machineMatchesPreset(row, preset) && machineMatchesFilter(row, filter)),
    [filter, machineRows, preset]
  );
  const presetCounts = useMemo(() => {
    const counts: Record<FleetPreset, number> = { "all": machineRows.length, "attention": 0, "stale": 0, "pending": 0, "no-agent": 0 };
    for (const row of machineRows) {
      for (const option of FLEET_PRESETS) {
        if (option.id !== "all" && machineMatchesPreset(row, option.id)) {
          counts[option.id] += 1;
        }
      }
    }
    return counts;
  }, [machineRows]);
  // Summary shown on the collapsed topology disclosure, so folding it away
  // still tells you whether anything in there wants attention.
  const topologySummary = useMemo(() => {
    const parts = [
      countLabel(bubbleOptions.length, "bubble"),
      countLabel(bubbleTopology.critical_infrastructure.length, "infra VM"),
      countLabel(bubbleTopology.connected_services.length, "service")
    ];
    if (bubbleTopology.warnings.length) {
      parts.push(countLabel(bubbleTopology.warnings.length, "warning"));
    }
    return parts.join(" / ");
  }, [
    bubbleOptions.length,
    bubbleTopology.connected_services.length,
    bubbleTopology.critical_infrastructure.length,
    bubbleTopology.warnings.length
  ]);
  useEffect(() => {
    if (!actionStatus || actionStatusTone === "bad" || actionStatus.endsWith("...") || actionStatusLink) {
      return undefined;
    }
    const timer = window.setTimeout(() => {
      setActionStatusMessage("");
      setActionStatusLink(null);
    }, 8000);
    return () => { window.clearTimeout(timer); };
  }, [actionStatus, actionStatusLink, actionStatusTone]);

  const seededAction = useMemo<VmActionSelection | null>(() => {
    const vm = detailRow?.vm;
    if (requestedAction === null || seedDismissed || !vm) {
      return null;
    }
    return { mode: requestedAction, vm };
  }, [detailRow, requestedAction, seedDismissed]);
  const effectiveAction = activeAction ?? seededAction;

  const stale = typeof fleet.cache_age_seconds === "number" && fleet.cache_age_seconds > 60;
  // Three separate advisory paragraphs became one line. None of these is an
  // error, so none of them should render in the error colour.
  const fleetAdvisories = useMemo(() => {
    const notes: string[] = [];
    if (stale) {
      notes.push(`Cache ${String(fleet.cache_age_seconds)}s old`);
    }
    if (fleet.agent_identity_warnings?.length) {
      notes.push(...fleet.agent_identity_warnings);
    }
    return notes;
  }, [fleet.agent_identity_warnings, fleet.cache_age_seconds, stale]);
  const [selectedAgentIds, setSelectedAgentIds] = useState<ReadonlySet<string>>(new Set());
  const [agentFormDraft, setAgentFormDraft] = useState<AgentFormDraft | null>(null);

  // Drop selections when the underlying row set changes (filter, refresh).
  // Avoid keeping stale ids that no longer match a visible row.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSelectedAgentIds((current) => {
      if (!current.size) {
        return current;
      }
      const visibleAgentIds = new Set(
        filteredMachines
          .map((row) => row.agentId)
          .filter((id): id is string => Boolean(id))
      );
      let dropped = false;
      const next = new Set<string>();
      for (const id of current) {
        if (visibleAgentIds.has(id)) {
          next.add(id);
        } else {
          dropped = true;
        }
      }
      return dropped ? next : current;
    });
  }, [filteredMachines]);

  const selectableAgentIds = useMemo(
    () => filteredMachines.map((row) => row.agentId).filter((id): id is string => Boolean(id)),
    [filteredMachines]
  );
  const allSelected = selectableAgentIds.length > 0 && selectableAgentIds.every((id) => selectedAgentIds.has(id));
  const someSelected = selectedAgentIds.size > 0 && !allSelected;

  const toggleRowSelected = useCallback((agentId: string) => {
    setSelectedAgentIds((current) => {
      const next = new Set(current);
      if (next.has(agentId)) {
        next.delete(agentId);
      } else {
        next.add(agentId);
      }
      return next;
    });
  }, []);

  const toggleSelectAll = useCallback(() => {
    setSelectedAgentIds((current) => {
      if (current.size && selectableAgentIds.every((id) => current.has(id))) {
        return new Set();
      }
      return new Set(selectableAgentIds);
    });
  }, [selectableAgentIds]);

  const clearSelection = useCallback(() => {
    setSelectedAgentIds(new Set());
  }, []);

  const runAction = useCallback(async (label: string, action: () => Promise<unknown>) => {
    setActionStatusLink(null);
    setActionStatus(`${label}...`);
    try {
      await action();
      setActionStatus(`${label} complete`);
      await load();
      return true;
    } catch (err) {
      setActionStatusLink(null);
      setActionStatus(err instanceof Error ? err.message : `${label} failed`, "bad");
      return false;
    }
  }, [load, setActionStatus]);

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

  const collectLogs = useCallback((vm: VmFleetRow) => {
    const vmid = vm.vmid;
    if (typeof vmid !== "number") {
      setActionStatusLink(null);
      setActionStatus("Cannot collect logs without a VMID", "bad");
      return;
    }
    setActionStatusLink(null);
    setActionStatus(`Collect logs VM ${String(vmid)}...`);
    void (async () => {
      try {
        const queued = await postJson<CollectLogsResponse>("/api/jobs/collect-logs", { vmid, vm_name: vmDisplayName(vm) });
        setActionStatus(`Log collection queued for VM ${String(queued.vmid)}`);
        setActionStatusLink({ href: reactHrefForUiPath(queued.web_url || `/react/jobs/${queued.job_id}`), label: queued.job_id });
        await load();
      } catch (err) {
        setActionStatusLink(null);
        setActionStatus(err instanceof Error ? err.message : `Collect logs VM ${String(vmid)} failed`, "bad");
      }
    })();
  }, [load, setActionStatus]);

  const checkEnrollment = useCallback((vm: VmFleetRow) => {
    void runAction(`Check enrollment VM ${String(vm.vmid)}`, () => postJson(`/api/ubuntu/check-enrollment/${String(vm.vmid)}`));
  }, [runAction]);

  const selectConsole = useCallback((vm: VmFleetRow) => {
    setActiveAction({ mode: "console", vm });
    setActionStatus(`Console selected for VM ${String(vm.vmid)}`);
  }, [setActionStatus]);

  const selectActionMode = useCallback((mode: VmActionMode) => {
    const base = activeAction ?? seededAction;
    if (!base) {
      return;
    }
    setActiveAction({ ...base, mode });
  }, [activeAction, seededAction]);

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
    setActionStatus(sent ? `Screenshot requested for VM ${String(vm.vmid)}` : "Live WebSocket is not connected", sent ? "info" : "bad");
  }, [sendLive, setActionStatus]);

  const qgaProbe = useCallback((vm: VmFleetRow) => {
    const sent = sendLive?.({ type: "qga_probe", correlation_id: `qga-${String(vm.vmid)}-${String(Date.now())}`, vmid: vm.vmid });
    setActionStatus(sent ? `QGA probe requested for VM ${String(vm.vmid)}` : "Live WebSocket is not connected", sent ? "info" : "bad");
  }, [sendLive, setActionStatus]);

  const createBubble = useCallback(() => {
    setDeleteBubbleId(null);
    setBubbleDraftMode("create");
    setBubbleDraftId(null);
    setBubbleDraft(blankBubbleForm);
    setBubbleAdoptedVnet(null);
    // Lazily fetch the orphan-vnet inventory so the operator can adopt an
    // existing isolated network instead of typing CIDR/gateway/DHCP by hand.
    void fetchJson<{ readonly orphan_vnets?: readonly OrphanVnet[] }>("/api/sdn/labs/orphan-vnets")
      .then((data) => {
        setOrphanVnets(data.orphan_vnets ?? []);
      })
      .catch(() => {
        setOrphanVnets([]);
      });
  }, []);

  const editBubble = useCallback((bubble: LabBubble) => {
    setDeleteBubbleId(null);
    setBubbleDraftMode("edit");
    setBubbleDraftId(bubble.id);
    setBubbleDraft(bubbleFormFromBubble(bubble));
    setBubbleAdoptedVnet(null);
    setBubbleBoundNetwork(null);
    // Bubbles store a denormalized copy of cidr / gateway_ip / dhcp_pool_*
    // that drifts the moment the operator changes the SDN subnet on the
    // Networks page. When this bubble has an SDN binding, treat the live
    // PVE subnet config as the source of truth and pre-fill the form
    // with it (and lock those fields in the UI). 404 just means there's
    // no binding -- fall back to the bubble's own copy.
    void fetchJson<{
      readonly binding?: { readonly vnet: string; readonly zone: string; readonly subnet: string };
      readonly subnet?: {
        readonly subnet?: string;
        readonly cidr?: string;
        readonly gateway?: string;
        readonly dhcp_dns_server?: string;
        readonly dhcp_range?: string;
      } | null;
    }>(`/api/sdn/labs/${encodeURIComponent(bubble.id)}/network`)
      .then((data) => {
        if (!data.binding) {
          return;
        }
        // Prefer the human-readable CIDR ("192.168.16.0/24") over the internal
        // provider id ("labz1-192.168.16.0-24"); fall back to the binding.
        const subnetCidr = data.subnet?.cidr ?? data.subnet?.subnet ?? data.binding.subnet;
        const range = parseDhcpRange(data.subnet?.dhcp_range);
        const gateway = data.subnet?.gateway ?? "";
        const dhcpDns = data.subnet?.dhcp_dns_server ?? "";
        setBubbleBoundNetwork({
          vnet: data.binding.vnet,
          zone: data.binding.zone,
          subnet: subnetCidr,
          gateway,
          dhcpStart: range.start,
          dhcpEnd: range.end,
          dhcpDnsServer: dhcpDns,
          subnetSource: data.subnet ? "sdn" : "binding"
        });
        setBubbleDraft((current) => ({
          ...current,
          cidr: subnetCidr || current.cidr,
          gateway_ip: gateway || current.gateway_ip,
          dhcp_scope: data.binding?.vnet ?? current.dhcp_scope,
          dhcp_pool_start: range.start || current.dhcp_pool_start,
          dhcp_pool_end: range.end || current.dhcp_pool_end
        }));
      })
      .catch(() => {
        // 404 (no binding) or transient failure; keep the bubble's own
        // copy editable as before.
      });
  }, []);

  const adoptOrphanVnet = useCallback((vnetId: string) => {
    if (!vnetId) {
      setBubbleAdoptedVnet(null);
      return;
    }
    const match = orphanVnets.find((entry) => entry.vnet === vnetId);
    if (!match) {
      return;
    }
    const subnetCidr = match.subnet?.subnet ?? "";
    const gateway = match.subnet?.gateway ?? "";
    const range = parseDhcpRange(match.subnet?.dhcp_range);
    setBubbleAdoptedVnet({ vnet: match.vnet, zone: match.zone, subnet: subnetCidr });
    setBubbleDraft((current) => ({
      ...current,
      cidr: subnetCidr || current.cidr,
      gateway_ip: gateway || current.gateway_ip,
      dhcp_scope: match.alias || match.vnet || current.dhcp_scope,
      dhcp_pool_start: range.start || current.dhcp_pool_start,
      dhcp_pool_end: range.end || current.dhcp_pool_end,
      isolation_status: current.isolation_status === "planned" ? "ready" : current.isolation_status
    }));
  }, [orphanVnets]);

  const updateBubbleDraft = useCallback((field: BubbleFormField, value: string) => {
    setBubbleDraft((current) => ({ ...current, [field]: value }));
  }, []);

  const cancelBubbleDraft = useCallback(() => {
    setBubbleDraftMode(null);
    setBubbleDraftId(null);
    setBubbleDraft(blankBubbleForm);
    setBubbleAdoptedVnet(null);
    setBubbleBoundNetwork(null);
  }, []);

  const saveBubbleDraft = useCallback(() => {
    const payload = bubbleFormPayload(bubbleDraft);
    const bubbleName = bubbleDraft.name.trim() || "bubble";
    if (!bubbleDraftMode || !bubbleName.trim()) {
      return;
    }
    if (bubbleDraftMode === "create") {
      // Route through /api/sdn/labs when the operator adopted an existing
      // unbound vnet. That endpoint creates BOTH the bubble row and the
      // lab_sdn_bindings entry in one transaction so the bubble is
      // network-isolated from the moment it exists. Plain bubbles (no
      // adoption) still use /api/bubbles to skip the SDN side.
      if (bubbleAdoptedVnet) {
        const labPayload = {
          name: bubbleName,
          zone: bubbleAdoptedVnet.zone,
          vnet: bubbleAdoptedVnet.vnet,
          subnet: bubbleAdoptedVnet.subnet,
          domain_name: bubbleDraft.domain_name.trim(),
          cidr: bubbleDraft.cidr.trim(),
          gateway_ip: bubbleDraft.gateway_ip.trim()
        };
        void runAction(`Create bubble ${bubbleName}`, () => postJson("/api/sdn/labs", labPayload)).then((ok) => {
          if (ok) {
            cancelBubbleDraft();
          }
        });
        return;
      }
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
  }, [bubbleAdoptedVnet, bubbleDraft, bubbleDraftId, bubbleDraftMode, cancelBubbleDraft, runAction]);

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
      setActionStatus("Create a bubble before tagging VM assets.", "bad");
      return;
    }
    const current = assignmentsByVmid.get(row.vmid);
    setMachineTagDraft({
      rowId: row.id,
      bubbleId: current?.bubble.id ?? bubbleOptions[0]?.id ?? "",
      assetRole: current?.asset.asset_role ?? "workstation"
    });
  }, [assignmentsByVmid, bubbleOptions, setActionStatus]);

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
      setActionStatus("Bubble selection did not match an existing bubble.", "bad");
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
  }, [assignmentsByVmid, bubbleOptions, machineTagDraft, runAction, setActionStatus]);

  const startInfraDraft = useCallback(() => {
    if (!bubbleOptions.length) {
      setActionStatus("Create a bubble before tagging infrastructure.", "bad");
      return;
    }
    const runningCandidate = infraVmCandidates.find((vm) => vm.status === "running") ?? infraVmCandidates[0];
    setInfraDraft({
      ...blankInfraDraft,
      bubbleId: bubbleOptions[0]?.id ?? "",
      vmid: runningCandidate ? String(runningCandidate.vmid) : ""
    });
    setInfraDraftOpen(true);
  }, [bubbleOptions, infraVmCandidates, setActionStatus]);

  const updateInfraDraft = useCallback((field: keyof InfraDraft, value: string) => {
    setInfraDraft((current) => ({ ...current, [field]: value }));
  }, []);

  const cancelInfraDraft = useCallback(() => {
    setInfraDraftOpen(false);
    setInfraDraft(blankInfraDraft);
  }, []);

  const saveInfraDraft = useCallback(() => {
    const targetBubble = bubbleOptions.find((bubble) => bubble.id === infraDraft.bubbleId);
    const vmid = Number.parseInt(infraDraft.vmid, 10);
    const role = infraDraft.role.trim();
    if (!targetBubble || !Number.isFinite(vmid) || !role) {
      return;
    }
    void runAction(`Add infra VM ${String(vmid)}`, () => postJson(`/api/bubbles/${targetBubble.id}/assets`, {
      asset_type: "vm",
      asset_role: role,
      vmid,
      membership_state: "active",
      evidence_state: "operator_tagged",
      notes: infraDraft.notes.trim() || `Tagged from React VMs as ${roleLabel(role)}`
    })).then((ok) => {
      if (ok) {
        cancelInfraDraft();
      }
    });
  }, [bubbleOptions, cancelInfraDraft, infraDraft, runAction]);

  const editInfra = useCallback((node: LabBubbleInfrastructureNode) => {
    setInfraMoveDraft(null);
    setRetireInfraId(null);
    setInfraEditDraft({
      assetId: node.asset.id,
      role: node.asset.asset_role,
      notes: node.asset.notes ?? ""
    });
  }, []);

  const updateInfraEditDraft = useCallback((field: "role" | "notes", value: string) => {
    setInfraEditDraft((current) => current ? { ...current, [field]: value } : current);
  }, []);

  const cancelInfraEdit = useCallback(() => {
    setInfraEditDraft(null);
  }, []);

  const saveInfraEdit = useCallback((node: LabBubbleInfrastructureNode) => {
    if (!infraEditDraft || infraEditDraft.assetId !== node.asset.id) {
      return;
    }
    const role = infraEditDraft.role.trim();
    if (!role) {
      return;
    }
    void runAction(`Edit infra ${vmAssetLabel(node.asset, node.vm)}`, () => fetchJson(`/api/bubbles/${node.bubble.id}/assets/${node.asset.id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        asset_role: role,
        notes: infraEditDraft.notes.trim()
      })
    })).then((ok) => {
      if (ok) {
        setInfraEditDraft(null);
      }
    });
  }, [infraEditDraft, runAction]);

  const startInfraMove = useCallback((node: LabBubbleInfrastructureNode) => {
    setInfraEditDraft(null);
    setRetireInfraId(null);
    setInfraMoveDraft({
      assetId: node.asset.id,
      bubbleId: node.bubble.id
    });
  }, []);

  const updateInfraMoveDraft = useCallback((bubbleId: string) => {
    setInfraMoveDraft((current) => current ? { ...current, bubbleId } : current);
  }, []);

  const cancelInfraMove = useCallback(() => {
    setInfraMoveDraft(null);
  }, []);

  const confirmInfraMove = useCallback((node: LabBubbleInfrastructureNode) => {
    if (!infraMoveDraft || infraMoveDraft.assetId !== node.asset.id) {
      return;
    }
    void runAction(`Move infra ${vmAssetLabel(node.asset, node.vm)}`, () => postJson(`/api/bubbles/${node.bubble.id}/assets/${node.asset.id}/move`, {
      target_bubble_id: infraMoveDraft.bubbleId,
      reason: "React VMs infra move"
    })).then((ok) => {
      if (ok) {
        setInfraMoveDraft(null);
      }
    });
  }, [infraMoveDraft, runAction]);

  const requestRetireInfra = useCallback((node: LabBubbleInfrastructureNode) => {
    setInfraEditDraft(null);
    setInfraMoveDraft(null);
    setRetireInfraId(node.asset.id);
  }, []);

  const cancelRetireInfra = useCallback(() => {
    setRetireInfraId(null);
  }, []);

  const confirmRetireInfra = useCallback((node: LabBubbleInfrastructureNode) => {
    void runAction(`Retire infra ${vmAssetLabel(node.asset, node.vm)}`, () => fetchJson(`/api/bubbles/${node.bubble.id}/assets/${node.asset.id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        membership_state: "retired"
      })
    })).then((ok) => {
      if (ok) {
        setRetireInfraId(null);
      }
    });
  }, [runAction]);

  const startServiceDraft = useCallback(() => {
    if (!bubbleOptions.length) {
      setActionStatus("Create a bubble before adding connected services.", "bad");
      return;
    }
    setDeleteServiceId(null);
    setServiceDraftMode("create");
    setServiceDraftId(null);
    setServiceDraft({
      ...blankServiceDraft,
      bubbleId: bubbleOptions[0]?.id ?? "",
      providerAssetId: bubbleAssets.find((item) => item.bubble.id === bubbleOptions[0]?.id)?.asset.id ?? ""
    });
  }, [bubbleAssets, bubbleOptions, setActionStatus]);

  const editService = useCallback((service: LabBubbleConnectedService) => {
    setDeleteServiceId(null);
    setServiceDraftMode("edit");
    setServiceDraftId(service.id);
    setServiceDraft({
      bubbleId: service.bubble_id,
      serviceKind: service.service_kind,
      serviceName: service.service_name,
      scope: service.scope ?? "bubble_local",
      providerAssetId: service.provider_asset_id ?? "",
      readinessState: service.readiness_state ?? "unknown",
      credentialIds: credentialIdsFromService(service)
    });
  }, []);

  const updateServiceDraft = useCallback((field: keyof ServiceDraft, value: string | readonly string[]) => {
    setServiceDraft((current) => {
      const next = { ...current, [field]: value };
      if (field === "bubbleId") {
        const providerInBubble = bubbleAssets.some((item) => item.bubble.id === value && item.asset.id === current.providerAssetId);
        return {
          ...next,
          providerAssetId: providerInBubble ? current.providerAssetId : bubbleAssets.find((item) => item.bubble.id === value)?.asset.id ?? ""
        };
      }
      return next;
    });
  }, [bubbleAssets]);

  const cancelServiceDraft = useCallback(() => {
    setServiceDraftMode(null);
    setServiceDraftId(null);
    setServiceDraft(blankServiceDraft);
  }, []);

  const saveServiceDraft = useCallback(() => {
    const bubbleId = serviceDraft.bubbleId;
    const serviceName = serviceDraft.serviceName.trim();
    if (!bubbleId || !serviceName || !serviceDraftMode) {
      return;
    }
    const payload = {
      service_kind: serviceDraft.serviceKind.trim(),
      service_name: serviceName,
      scope: serviceDraft.scope.trim() || "bubble_local",
      provider_asset_id: serviceDraft.providerAssetId || null,
      readiness_state: serviceDraft.readinessState.trim() || "unknown",
      evidence_summary: {
        credential_ids: serviceDraft.credentialIds.map((id) => Number.parseInt(id, 10)).filter(Number.isFinite)
      }
    };
    const label = serviceDraftMode === "create" ? `Create service ${serviceName}` : `Edit service ${serviceName}`;
    const request = serviceDraftMode === "create"
      ? () => postJson(`/api/bubbles/${bubbleId}/services`, payload)
      : () => fetchJson(`/api/bubbles/${bubbleId}/services/${String(serviceDraftId)}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload)
      });
    void runAction(label, request).then((ok) => {
      if (ok) {
        cancelServiceDraft();
      }
    });
  }, [cancelServiceDraft, runAction, serviceDraft, serviceDraftId, serviceDraftMode]);

  const requestDeleteService = useCallback((service: LabBubbleConnectedService) => {
    setServiceDraftMode(null);
    setServiceDraftId(null);
    setServiceDraft(blankServiceDraft);
    setDeleteServiceId(service.id);
  }, []);

  const cancelDeleteService = useCallback(() => {
    setDeleteServiceId(null);
  }, []);

  const deleteService = useCallback((service: LabBubbleConnectedService) => {
    void runAction(`Delete service ${service.service_name}`, () => deleteJson(`/api/bubbles/${service.bubble_id}/services/${service.id}`)).then((ok) => {
      if (ok) {
        setDeleteServiceId(null);
      }
    });
  }, [runAction]);

  const deleteAgent = useCallback((agent: AgentFleetRow) => {
    const typed = window.prompt(`Type ${agent.agent_id} to delete agent`);
    if (typed !== agent.agent_id) {
      return;
    }
    void runAction(`Delete ${agent.agent_id}`, () => postJson(`/api/agents/${encodeURIComponent(agent.agent_id)}/delete`));
  }, [runAction]);

  const createAgent = useCallback(() => {
    setAgentFormDraft({
      mode: "create",
      agentId: "",
      vmid: "",
      computerName: "",
      serialNumber: "",
      agentVersion: ""
    });
  }, []);

  const updateAgent = useCallback((agent: AgentFleetRow) => {
    setAgentFormDraft({
      mode: "edit",
      agentId: agent.agent_id,
      vmid: agent.vmid ? String(agent.vmid) : "",
      computerName: agent.computer_name || "",
      serialNumber: agent.serial_number || "",
      agentVersion: agent.agent_version || ""
    });
  }, []);

  const submitAgentForm = useCallback(async () => {
    const draft = agentFormDraft;
    if (!draft) {
      return;
    }
    const trimmedId = draft.agentId.trim();
    if (!trimmedId) {
      setActionStatus("Agent ID is required", "bad");
      return;
    }
    const body = {
      vmid: draft.vmid.trim(),
      computer_name: draft.computerName.trim(),
      serial_number: draft.serialNumber.trim(),
      agent_version: draft.agentVersion.trim()
    };
    if (draft.mode === "create") {
      await runAction(`Add ${trimmedId}`, () => postJson("/api/agents", {
        agent_id: trimmedId,
        ...body
      }));
    } else {
      await runAction(`Update ${trimmedId}`, () => postJson(
        `/api/agents/${encodeURIComponent(trimmedId)}/update`,
        body
      ));
    }
    setAgentFormDraft(null);
  }, [agentFormDraft, runAction, setActionStatus]);

  const cancelAgentForm = useCallback(() => {
    setAgentFormDraft(null);
  }, []);

  const bulkDeleteSelected = useCallback(async () => {
    if (!selectedAgentIds.size) {
      return;
    }
    const ids = [...selectedAgentIds];
    const selectedCount = String(ids.length);
    const confirmText = `Delete ${selectedCount} agent${ids.length === 1 ? "" : "s"}? This cannot be undone.`;
    if (typeof window !== "undefined" && !window.confirm(confirmText)) {
      return;
    }
    await runAction(
      `Delete ${selectedCount} agent${ids.length === 1 ? "" : "s"}`,
      () => postJson("/api/agents/bulk-delete", { agent_ids: ids })
    );
    clearSelection();
  }, [clearSelection, runAction, selectedAgentIds]);

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
        path={`/react/vms/${String(detailVmid)}`}
        socketState={socketState}
        action={<a className="action-link" href="/react/vms">VMs</a>}
      >
        {loading ? <div className="progress" role="progressbar" aria-label="Loading VM"><span /></div> : null}
        {detailLoading ? <div className="progress" role="progressbar" aria-label="Loading VM evidence"><span /></div> : null}
        {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
        {detailError ? <p className="notice notice--bad" role="alert">{detailError}</p> : null}
        {actionStatus ? (
          <p
            className={actionStatusTone === "bad" ? "notice notice--bad" : "notice"}
            role={actionStatusTone === "bad" ? "alert" : "status"}
          >
            {actionStatus}
            {actionStatusLink ? <> <a href={actionStatusLink.href}>{actionStatusLink.label}</a></> : null}
          </p>
        ) : null}
        {detailRow?.vm ? (
          <VmDetailWorkspace
            row={detailRow}
            evidence={detailEvidence}
            activeAction={effectiveAction}
            screenshot={screenshot}
            socketState={socketState}
            onPower={power}
            onRename={rename}
            onTypeText={typeText}
            onSendKey={sendKey}
            onCapture={captureHash}
            onCollectLogs={collectLogs}
            onCheckEnrollment={checkEnrollment}
            onConsole={selectConsole}
            onScreenshot={screenshotVm}
            onQgaProbe={qgaProbe}
            onApproveAgent={approveAgent}
            onDeleteAgent={deleteAgent}
            onModeChange={selectActionMode}
            onRequestScreenshot={screenshotVm}
            onCloseAction={() => {
              setActiveAction(null);
              setSeedDismissed(true);
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
      action={
        <div className="page-head__actions">
          <button
            type="button"
            className="action-link"
            onClick={() => { void runAction("Refresh fleet", () => postJson("/api/vms/refresh")); }}
          >
            Refresh
          </button>
          <a className="action-link" href="/react/monitoring">Signals</a>
        </div>
      }
    >
      {loading ? <div className="progress" role="progressbar" aria-label="Loading fleet"><span /></div> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      {actionStatus ? (
        <p
          className={actionStatusTone === "bad" ? "notice notice--bad" : "notice"}
          role={actionStatusTone === "bad" ? "alert" : "status"}
        >
          {actionStatus}
          {actionStatusLink ? <> <a href={actionStatusLink.href}>{actionStatusLink.label}</a></> : null}
        </p>
      ) : null}
      {/* Advisories collapse into one line. Only a real failure gets --bad. */}
      {fleetAdvisories.length ? (
        <p className="notice" role="status">{fleetAdvisories.join(" / ")}</p>
      ) : null}
      {fleet.ap_error ? (
        <p className="notice notice--warn" role="status">Intune unavailable: {fleet.ap_error}</p>
      ) : null}

      {/* Four tiles, not ten. A count is only toned when it is asking for work;
          a healthy fleet renders entirely neutral so colour keeps its meaning.
          The counts that lost a tile (stale, upgrade, approvals, pairing,
          Intune, missing) stay reachable through the fleet filter. */}
      <section className="metric-strip" aria-label="Fleet metrics">
        <Metric label="Proxmox VMs" value={String(counts.total)} />
        <Metric label="Running" value={String(counts.running)} />
        <button
          type="button"
          className="metric-button"
          aria-label={`Show ${countLabel(presetCounts.attention, "machine")} needing attention`}
          onClick={() => { setPreset(presetCounts.attention ? "attention" : "all"); }}
        >
          <Metric label="Attention" value={String(presetCounts.attention)} tone={presetCounts.attention ? "bad" : "neutral"} />
        </button>
        <Metric label="Agents" value={String(counts.agents)} />
      </section>

      {/* The machine table is what this page is for. Bubbles, infrastructure
          and services are configuration jobs on a sibling view, addressable as
          ?view=topology so the state survives a reload or a shared link. */}
      <div className="segmented fleet-views" role="group" aria-label="Fleet views">
        <button
          type="button"
          className={view === "machines" ? "is-active" : ""}
          aria-pressed={view === "machines"}
          onClick={() => { selectView("machines"); }}
        >
          Machines
        </button>
        <button
          type="button"
          className={view === "topology" ? "is-active" : ""}
          aria-pressed={view === "topology"}
          onClick={() => { selectView("topology"); }}
        >
          Topology
          <span className="fleet-views__hint">{topologySummary}</span>
        </button>
      </div>

      {view === "machines" ? (
      <section className="fleet-lanes" aria-label="Fleet lanes">
        <div className="fleet-primary-stack">
          <FleetMachineTable
            rows={filteredMachines}
            totalCount={machineRows.length}
            filter={filter}
            onFilterChange={setFilter}
            loading={loading}
            onApproveAgent={approveAgent}
            preset={preset}
            onPresetChange={setPreset}
            presetCounts={presetCounts}
            onCreateAgent={createAgent}
            onTagMachine={tagMachine}
            tagDraft={machineTagDraft}
            bubbleOptions={bubbleOptions}
            onTagDraftChange={updateMachineTagDraft}
            onSaveTag={saveMachineTag}
            onCancelTag={cancelMachineTagDraft}
            assignmentsByVmid={assignmentsByVmid}
            selectedAgentIds={selectedAgentIds}
            onToggleRow={toggleRowSelected}
            onToggleSelectAll={toggleSelectAll}
            allSelected={allSelected}
            someSelected={someSelected}
            onBulkDelete={() => { void bulkDeleteSelected(); }}
            onClearSelection={clearSelection}
            onEditAgent={updateAgent}
          />
        </div>
      </section>
      ) : null}

      {view === "topology" ? (
        <BubbleTopologyOverview
        topology={bubbleTopology}
        infraVmCandidates={infraVmCandidates}
        credentials={credentialSummaries}
        onCreateBubble={createBubble}
        onEditBubble={editBubble}
        onRequestDeleteBubble={requestDeleteBubble}
        onConfirmDeleteBubble={deleteBubble}
        onCancelDeleteBubble={cancelDeleteBubble}
        bubbleDraftMode={bubbleDraftMode}
        bubbleDraftId={bubbleDraftId}
        bubbleDraft={bubbleDraft}
        orphanVnets={orphanVnets}
        adoptedVnetId={bubbleAdoptedVnet?.vnet}
        onAdoptVnet={adoptOrphanVnet}
        boundNetwork={bubbleBoundNetwork}
        onBubbleDraftChange={updateBubbleDraft}
        onSaveBubbleDraft={saveBubbleDraft}
        onCancelBubbleDraft={cancelBubbleDraft}
        deleteBubbleId={deleteBubbleId}
        infraDraftOpen={infraDraftOpen}
        infraDraft={infraDraft}
        infraEditDraft={infraEditDraft}
        infraMoveDraft={infraMoveDraft}
        retireInfraId={retireInfraId}
        onStartInfraDraft={startInfraDraft}
        onInfraDraftChange={updateInfraDraft}
        onSaveInfraDraft={saveInfraDraft}
        onCancelInfraDraft={cancelInfraDraft}
        onEditInfra={editInfra}
        onInfraEditDraftChange={updateInfraEditDraft}
        onSaveInfraEdit={saveInfraEdit}
        onCancelInfraEdit={cancelInfraEdit}
        onStartInfraMove={startInfraMove}
        onInfraMoveDraftChange={updateInfraMoveDraft}
        onConfirmInfraMove={confirmInfraMove}
        onCancelInfraMove={cancelInfraMove}
        onRequestRetireInfra={requestRetireInfra}
        onConfirmRetireInfra={confirmRetireInfra}
        onCancelRetireInfra={cancelRetireInfra}
        onApproveAgent={approveAgent}
        serviceDraftMode={serviceDraftMode}
        serviceDraftId={serviceDraftId}
        serviceDraft={serviceDraft}
        deleteServiceId={deleteServiceId}
        onStartServiceDraft={startServiceDraft}
        onEditService={editService}
        onServiceDraftChange={updateServiceDraft}
        onSaveServiceDraft={saveServiceDraft}
        onCancelServiceDraft={cancelServiceDraft}
        onRequestDeleteService={requestDeleteService}
        onConfirmDeleteService={deleteService}
          onCancelDeleteService={cancelDeleteService}
        />
      ) : null}

      {agentFormDraft ? (
        <FleetAgentFormModal
          draft={agentFormDraft}
          onChange={(field, value) => {
            setAgentFormDraft((current) => (current ? { ...current, [field]: value } : current));
          }}
          onSubmit={() => { void submitAgentForm(); }}
          onCancel={cancelAgentForm}
        />
      ) : null}

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
  assignmentsByVmid,
  selectedAgentIds,
  onToggleRow,
  onToggleSelectAll,
  allSelected,
  someSelected,
  onBulkDelete,
  onClearSelection,
  onEditAgent,
  totalCount,
  filter,
  onFilterChange,
  loading,
  onApproveAgent,
  preset,
  onPresetChange,
  presetCounts
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
  readonly selectedAgentIds: ReadonlySet<string>;
  readonly onToggleRow: (agentId: string) => void;
  readonly onToggleSelectAll: () => void;
  readonly allSelected: boolean;
  readonly someSelected: boolean;
  readonly onBulkDelete: () => void;
  readonly onClearSelection: () => void;
  readonly onEditAgent: (agent: AgentFleetRow) => void;
  readonly totalCount: number;
  readonly filter: string;
  readonly onFilterChange: (value: string) => void;
  readonly loading: boolean;
  readonly onApproveAgent: (agent: AgentFleetRow) => void;
  readonly preset: FleetPreset;
  readonly onPresetChange: (preset: FleetPreset) => void;
  readonly presetCounts: Readonly<Record<FleetPreset, number>>;
}) {
  const selectionCount = selectedAgentIds.size;
  return (
    <Panel
      title="Fleet machines"
      className="fleet-machines-panel"
      action={
        <button type="button" className="fleet-action fleet-action--command" onClick={onCreateAgent}>
          <UserPlus aria-hidden="true" focusable="false" size={14} strokeWidth={2.4} />
          <span>Add agent</span>
        </button>
      }
    >
      {/* The filter lives with the table it filters, and reports how much of
          the fleet it is hiding. .result-count is the house pattern already
          used by Jobs, Hashes, Credentials, Files and Cloud devices. */}
      <div className="filter-row__top">
        <label className="filter">
          <span>Filter fleet</span>
          <input
            aria-label="Filter fleet"
            value={filter}
            onChange={(event) => { onFilterChange(event.target.value); }}
            placeholder="VMID, name, serial, IP, phase, enrollment"
          />
        </label>
        <p className="result-count" role="status">
          {rows.length === totalCount
            ? countLabel(totalCount, "machine")
            : `${String(rows.length)} of ${countLabel(totalCount, "machine")}`}
        </p>
      </div>

      <div className="fleet-presets" role="group" aria-label="Fleet presets">
        {FLEET_PRESETS.map((option) => (
          <button
            key={option.id}
            type="button"
            className="fleet-preset"
            aria-pressed={preset === option.id}
            onClick={() => { onPresetChange(option.id); }}
          >
            {option.label}
            {option.id === "all" ? null : <span>{presetCounts[option.id]}</span>}
          </button>
        ))}
      </div>
      {selectionCount > 0 ? (
        <div className="fleet-bulk-bar" role="region" aria-label="Bulk fleet actions">
          <span className="fleet-bulk-bar__count">
            {selectionCount} agent{selectionCount === 1 ? "" : "s"} selected
          </span>
          <div className="fleet-bulk-bar__actions">
            <button
              type="button"
              className="fleet-bulk-bar__action fleet-bulk-bar__action--danger"
              onClick={onBulkDelete}
            >
              Delete selected
            </button>
            <button type="button" className="fleet-bulk-bar__action" onClick={onClearSelection}>
              Clear selection
            </button>
          </div>
        </div>
      ) : null}
      <div className="fleet-machine-table-wrap">
        {rows.length ? (
          <table className="fleet-machine-table" aria-label="Fleet machines">
            <thead>
              <tr>
                <th scope="col" className="fleet-machine-table__check">
                  <input
                    type="checkbox"
                    aria-label="Select all visible agents"
                    checked={allSelected}
                    ref={(el) => {
                      if (el) {
                        el.indeterminate = someSelected;
                      }
                    }}
                    onChange={onToggleSelectAll}
                  />
                </th>
                <th scope="col">Device name</th>
                <th scope="col">Runtime</th>
                <th scope="col">Agent</th>
                <th scope="col">Phase</th>
                <th scope="col">Heartbeat</th>
                <th scope="col">Bubble</th>
                <th scope="col" className="fleet-machine-table__row-actions" aria-label="Row actions" />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const agentId = row.agentId;
                const selected = agentId ? selectedAgentIds.has(agentId) : false;
                return (
                  <Fragment key={row.id}>
                    <MachineRow
                      row={row}
                      assignment={row.vmid === undefined ? undefined : assignmentsByVmid.get(row.vmid)}
                      onTag={onTagMachine}
                      selected={selected}
                      onToggleSelect={agentId ? () => { onToggleRow(agentId); } : undefined}
                      onEditAgent={agentId ? onEditAgent : undefined}
                      onApproveAgent={onApproveAgent}
                    />
                    {tagDraft?.rowId === row.id && row.vmid !== undefined ? (
                      <tr className="machine-tag-row">
                        <td colSpan={8}>
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
                );
              })}
            </tbody>
          </table>
        ) : loading ? (
          <p className="empty">Loading fleet machines...</p>
        ) : filter ? (
          <p className="empty">No machines match &ldquo;{filter}&rdquo;.</p>
        ) : (
          <p className="empty">No fleet machines found.</p>
        )}
      </div>
    </Panel>
  );
}

function FleetAgentFormModal({
  draft,
  onChange,
  onSubmit,
  onCancel
}: {
  readonly draft: AgentFormDraft;
  readonly onChange: (field: keyof AgentFormDraft, value: string) => void;
  readonly onSubmit: () => void;
  readonly onCancel: () => void;
}) {
  const title = draft.mode === "create" ? "Add fleet agent" : `Edit agent ${draft.agentId}`;
  const dialogRef = useModalDialog(onCancel);
  return (
    <div className="fleet-modal-backdrop" role="presentation" onClick={onCancel}>
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="fleet-agent-form-title"
        className="fleet-modal"
        onClick={(event) => { event.stopPropagation(); }}
      >
        <header className="fleet-modal__header">
          <h3 id="fleet-agent-form-title">{title}</h3>
          <button type="button" className="fleet-modal__close" onClick={onCancel} aria-label="Close">
            x
          </button>
        </header>
        <form
          className="fleet-modal__body"
          onSubmit={(event) => {
            event.preventDefault();
            onSubmit();
          }}
        >
          <label className="cloudosd-field">
            <span>Agent ID *</span>
            <input
              value={draft.agentId}
              onChange={(event) => { onChange("agentId", event.currentTarget.value); }}
              disabled={draft.mode === "edit"}
              placeholder="agent-id"
              required
              aria-label="Agent ID"
            />
          </label>
          <label className="cloudosd-field">
            <span>VMID</span>
            <input
              value={draft.vmid}
              onChange={(event) => { onChange("vmid", event.currentTarget.value); }}
              placeholder="113"
              aria-label="VMID"
            />
          </label>
          <label className="cloudosd-field">
            <span>Computer name</span>
            <input
              value={draft.computerName}
              onChange={(event) => { onChange("computerName", event.currentTarget.value); }}
              placeholder="DESKTOP-XYZ"
              aria-label="Computer name"
            />
          </label>
          <label className="cloudosd-field">
            <span>Serial number</span>
            <input
              value={draft.serialNumber}
              onChange={(event) => { onChange("serialNumber", event.currentTarget.value); }}
              aria-label="Serial number"
            />
          </label>
          <label className="cloudosd-field">
            <span>Agent version</span>
            <input
              value={draft.agentVersion}
              onChange={(event) => { onChange("agentVersion", event.currentTarget.value); }}
              placeholder="1.0.0"
              aria-label="Agent version"
            />
          </label>
          <div className="fleet-modal__actions">
            <button type="button" className="fleet-modal__secondary" onClick={onCancel}>
              Cancel
            </button>
            <button type="submit" className="utility-button">
              {draft.mode === "create" ? "Add agent" : "Save changes"}
            </button>
          </div>
        </form>
      </div>
    </div>
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
  onTag,
  selected,
  onToggleSelect,
  onEditAgent,
  onApproveAgent
}: {
  readonly row: FleetMachineRow;
  readonly assignment: BubbleAssignment | undefined;
  readonly onTag: (row: FleetMachineRow) => void;
  readonly selected?: boolean | undefined;
  readonly onToggleSelect?: (() => void) | undefined;
  readonly onEditAgent?: ((agent: AgentFleetRow) => void) | undefined;
  readonly onApproveAgent?: ((agent: AgentFleetRow) => void) | undefined;
}) {
  const runtimeLabel = fleetRuntimeLabel(row);
  const agentLabel = fleetAgentLabel(row);
  const editableAgent: AgentFleetRow | null = row.agent ?? null;
  const pendingAgent = editableAgent?.approval_status === "pending" && editableAgent.approval_id
    ? editableAgent
    : null;
  const attention = machineAttention(row);
  // VMID, IP, OS and OS version stopped being columns. They are identifying
  // detail you read for one machine, not values you scan down a column, and
  // on a homogeneous Windows fleet the OS pair was the same string on every
  // row. They live under the name, and all four stay filterable.
  const subline = [
    row.vmid === undefined ? null : `#${String(row.vmid)}`,
    row.ipAddress || null,
    [fleetOsName(row), fleetOsVersion(row)].filter((part) => part && part !== "-").join(" ") || null
  ].filter(Boolean).join("  \u00b7  ");
  return (
    <tr className={selected ? "is-selected" : undefined}>
      <td className="fleet-machine-table__check">
        {onToggleSelect ? (
          <input
            type="checkbox"
            checked={Boolean(selected)}
            onChange={onToggleSelect}
            aria-label={`Select agent ${row.agentId ?? row.name}`}
          />
        ) : null}
      </td>
      <th scope="row">
        <span className={attention ? `machine-mark machine-mark--${attention}` : "machine-mark"} aria-hidden="true" />
        {row.vmid !== undefined ? (
          <a className="machine-name machine-name--link" href={`/react/vms/${String(row.vmid)}`}>{row.name}</a>
        ) : (
          <span className="machine-name">{row.name}</span>
        )}
        <span className="machine-subline">{subline}</span>
      </th>
      <td>
        <span className={runtimeLabel === "running" ? "status" : "status status--warn"}>
          {runtimeLabel}
        </span>
      </td>
      <td>
        <span className={fleetAgentClass(agentLabel)}>
          {agentLabel}
        </span>
      </td>
      <td>
        {/* Populated at viewModels.ts and already searchable, but it rendered
            nowhere, so filtering by phase narrowed the list for no visible
            reason. It is also the only value that moves during a deploy. */}
        <span className="machine-primary-value">{fallbackText(row.phase)}</span>
      </td>
      <td>
        <span className="machine-primary-value" title={formatShortDateTime(row.heartbeat)}>
          {formatRelativeAge(row.heartbeat)}
        </span>
      </td>
      <td>
        <span className="machine-primary-value">
          {assignment ? `${assignment.bubble.name} / ${roleLabel(assignment.asset.asset_role)}` : "-"}
        </span>
      </td>
      <td className="fleet-machine-table__row-actions">
        <div className="machine-row-actions">
          {pendingAgent && onApproveAgent ? (
            <button
              type="button"
              className="fleet-action"
              aria-label={`Approve agent ${pendingAgent.agent_id}`}
              onClick={() => { onApproveAgent(pendingAgent); }}
            >
              Approve
            </button>
          ) : null}
          {row.vmid !== undefined ? (
            <button
              type="button"
              className="fleet-action"
              aria-label={`Tag VM ${String(row.vmid)}`}
              onClick={() => { onTag(row); }}
            >
              Tag
            </button>
          ) : null}
          {editableAgent && onEditAgent ? (
            <button
              type="button"
              className="fleet-action"
              aria-label={`Edit agent ${editableAgent.agent_id}`}
              onClick={() => { onEditAgent(editableAgent); }}
            >
              Edit
            </button>
          ) : null}
        </div>
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
  onCollectLogs,
  onCheckEnrollment,
  onConsole,
  onScreenshot,
  onQgaProbe,
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
  readonly onCollectLogs: (vm: VmFleetRow) => void;
  readonly onCheckEnrollment: (vm: VmFleetRow) => void;
  readonly onConsole: (vm: VmFleetRow) => void;
  readonly onScreenshot: (vm: VmFleetRow) => void;
  readonly onQgaProbe: (vm: VmFleetRow) => void;
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
            {/* Absorbs what the deleted Essentials panel uniquely carried.
                Everything else in that panel was already the hero, a badge,
                or the PVE panel. */}
            <p>
              {fleetOsName(row)} {fleetOsVersion(row)} / VMID {String(vm.vmid)} / {fallbackText(row.ipAddress)}
              {row.phase ? ` / ${row.phase}` : ""} / heartbeat {formatRelativeAge(row.heartbeat)}
            </p>
          </div>
          <div className="vm-detail-badges">
            <span className={fleetRuntimeLabel(row) === "running" ? "status" : "status status--warn"}>{fleetRuntimeLabel(row)}</span>
            <span className="status">{fleetManagedByLabel(row)}</span>
            <span className={fleetAgentClass(fleetAgentLabel(row))}>{fleetAgentLabel(row)}</span>
          </div>
        </div>
      </section>

      {/* Three groups, not one flat row of fifteen. Watch is what you came
          for, Drive changes the machine, Evidence collects proof. Rename and
          both deletes sit behind a disclosure so the two irreversible controls
          are no longer adjacent to Shutdown. */}
      <section className="vm-detail-toolbar" aria-label={`VM ${String(vm.vmid)} actions`}>
        {isRunning ? (
          <>
            <div className="vm-action-group" role="group" aria-label="Watch">
              <span>Watch</span>
              <ActionButton label="Console" ariaLabel={`Console VM ${String(vm.vmid)}`} icon={Monitor} onClick={() => { onConsole(vm); }} />
              <ActionButton label="Screenshot" ariaLabel={`Screenshot VM ${String(vm.vmid)}`} icon={Camera} onClick={() => { onScreenshot(vm); }} />
            </div>
            <div className="vm-action-group" role="group" aria-label="Drive">
              <span>Drive</span>
              <ActionButton label="Shutdown" icon={Power} onClick={() => { onPower(vm, "shutdown"); }} />
              <ActionButton label="Stop" icon={CircleStop} tone="danger" onClick={() => { onPower(vm, "stop"); }} />
              <ActionButton label="Reset" icon={RotateCcw} onClick={() => { onPower(vm, "reset"); }} />
              <ActionButton label="Type" icon={Keyboard} onClick={() => { onTypeText(vm); }} />
              <ActionButton label="CAD" icon={TerminalSquare} onClick={() => { onSendKey(vm, "ctrl-alt-delete"); }} />
              <ActionButton label="Enter" icon={TerminalSquare} onClick={() => { onSendKey(vm, "ret"); }} />
            </div>
            <div className="vm-action-group" role="group" aria-label="Evidence">
              <span>Evidence</span>
              <ActionButton label="Hash" icon={Hash} onClick={() => { onCapture(vm); }} />
              <ActionButton label="Logs" icon={FileArchive} onClick={() => { onCollectLogs(vm); }} />
              <ActionButton label="QGA" icon={RefreshCw} onClick={() => { onQgaProbe(vm); }} />
              {vm.target_os === "ubuntu" ? <ActionButton label="Enroll" icon={BadgeCheck} onClick={() => { onCheckEnrollment(vm); }} /> : null}
            </div>
          </>
        ) : (
          <div className="vm-action-group" role="group" aria-label="Drive">
            <span>Drive</span>
            <ActionButton label="Start" icon={Play} onClick={() => { onPower(vm, "start"); }} />
          </div>
        )}
        {agent?.approval_status === "pending" && agent.approval_id ? (
          <div className="vm-action-group" role="group" aria-label="Agent">
            <span>Agent</span>
            <ActionButton label="Approve agent" icon={BadgeCheck} onClick={() => { onApproveAgent(agent); }} />
          </div>
        ) : null}
        <details className="vm-manage">
          <summary aria-label={`Manage VM ${String(vm.vmid)}`}>Manage this VM</summary>
          <div className="vm-manage__body">
            <ActionButton label="Rename" icon={Pencil} onClick={() => { onRename(vm); }} />
            {agent ? (
              <ActionButton label="Delete agent" icon={Trash2} tone="danger" onClick={() => { onDeleteAgent(agent); }} />
            ) : null}
            <ActionButton label="Delete VM" ariaLabel={`Delete VM ${String(vm.vmid)}`} icon={Trash2} tone="danger" onClick={() => { onPower(vm, "delete"); }} />
          </div>
        </details>
      </section>

      <section className="vm-detail-action-zone">
        <VmActionWorkspace
          selection={activeAction}
          evidence={evidence}
          screenshot={screenshot}
          socketState={socketState}
          onModeChange={onModeChange}
          onRequestScreenshot={onRequestScreenshot}
          onClose={onCloseAction}
        />
      </section>

      {/* Essentials used to lead here and was the nine fleet table headers
          verbatim, in the same order, through the same accessors. Agent and
          Intune render only when there is something behind them; on a lab VM
          with neither they were 15 rows of dashes. */}
      <section className="vm-detail-grid" aria-label="VM details">
        <DetailPanel title="PVE" rows={[
          ["Name", vmDisplayName(vm)],
          ["Status", fallbackText(vm.status)],
          ["Serial", fallbackText(vm.serial)],
          ["QGA", fallbackText(vm.qga)],
          ["Target OS", fallbackText(vm.target_os)],
          ["Sequence", fallbackText(vm.sequence_name)]
        ]} />
        {agent ? (
          <DetailPanel title="Agent" rows={[
            ["Agent ID", fallbackText(row.agentId)],
            ["Computer", fallbackText(agent.computer_name)],
            ["Version", fallbackText(row.version)],
            ["Published", fallbackText(agent.published_agent_version)],
            ["Update", fallbackText(agent.update_status)],
            ["Pairing", fallbackText(agent.pairing_status)],
            ["Phase", fallbackText(row.phase)],
            ["QGA", fallbackText(agent.qga_state)],
            ["Last seen", formatShortDateTime(agent.last_seen_at)]
          ]} />
        ) : null}
        {row.autopilotDevice ? (
          <DetailPanel title="Intune" rows={[
            ["Device", fallbackText(row.autopilotDevice.display_name)],
            ["Serial", fallbackText(row.autopilotDevice.serial)],
            ["Enrollment", fallbackText(row.autopilotDevice.enrollment_state)],
            ["Profile", fallbackText(row.autopilotDevice.profile_status)],
            ["Group tag", fallbackText(row.autopilotDevice.group_tag)],
            ["Last contact", formatShortDateTime(row.autopilotDevice.last_contact)]
          ]} />
        ) : null}
      </section>

      <VmEvidencePanels
        vmid={vm.vmid}
        evidence={evidence}
        onRefreshScreenshot={() => { onScreenshot(vm); }}
      />
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

/**
 * Ready is a resting state, so it renders neutral. "Not yet probed"
 * (undefined) is not the same thing as "probed and failing" (false), and
 * collapsing both into a red pill made every unprobed bubble look broken.
 */