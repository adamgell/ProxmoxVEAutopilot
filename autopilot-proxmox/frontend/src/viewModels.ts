import type {
  AgentFleetRow,
  AutopilotDeviceFleetRow,
  JobTableRow,
  MonitoringOverview,
  OperatorPath,
  SignalMetric,
  SignalsHubResponse,
  VmFleetRow,
  VmsFleetResponse
} from "./contracts";

export type StatusTone = "good" | "active" | "bad" | "neutral";

export interface JobsSummary {
  readonly total: number;
  readonly running: number;
  readonly queued: number;
  readonly failed: number;
  readonly complete: number;
  readonly paused: number;
}

export interface MetricItem {
  readonly label: string;
  readonly value: string;
  readonly tone?: StatusTone;
}

export const jobStatusFilters = ["all", "failed", "running", "queued", "complete", "paused"] as const;

export type JobStatusFilter = (typeof jobStatusFilters)[number];

const shortMonths = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] as const;

export function fallbackText(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  switch (typeof value) {
    case "string":
      return value;
    case "number":
    case "boolean":
    case "bigint":
      return value.toString();
    default:
      return "-";
  }
}

export function formatShortDateTime(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return fallbackText(value);
  }
  const month = shortMonths[date.getUTCMonth()] ?? "";
  const day = String(date.getUTCDate()).padStart(2, "0");
  const hour = String(date.getUTCHours()).padStart(2, "0");
  const minute = String(date.getUTCMinutes()).padStart(2, "0");
  return `${month} ${day} ${hour}:${minute}Z`;
}

export function formatPercent(value: unknown): string {
  return typeof value === "number" ? `${String(value)}%` : "-";
}

export function statusTone(status: string | null | undefined): StatusTone {
  const normalized = (status || "unknown").toLowerCase();
  if (["complete", "completed", "done", "healthy", "ok", "ready", "running"].includes(normalized)) {
    return normalized === "running" ? "active" : "good";
  }
  if (["pending", "queued", "active", "learning"].includes(normalized)) {
    return "active";
  }
  if (["failed", "error", "stale", "orphaned", "stuck", "regressed", "slow"].includes(normalized)) {
    return "bad";
  }
  return "neutral";
}

export function statusClass(status: string | null | undefined): string {
  const tone = statusTone(status);
  return tone === "neutral" ? "status" : `status status--${tone}`;
}

export function statusLabel(status: string | null | undefined, paused = false): string {
  if (paused) {
    return "paused";
  }
  return fallbackText(status).toLowerCase();
}

export function serviceName(service: {
  readonly service?: string;
  readonly service_id?: string;
  readonly service_type?: string;
}): string {
  return service.service || service.service_id || service.service_type || "service";
}

export function jobTarget(job: JobTableRow): string {
  const args = job.args ?? {};
  const candidates = [
    args.hostname_pattern,
    args.vm_name,
    args.template_name,
    args.serial,
    args.sequence_name,
    args.target
  ];
  const found = candidates.find((candidate) => typeof candidate === "string" && candidate.trim());
  return typeof found === "string" ? found : "-";
}

export function summarizeJobs(jobs: readonly JobTableRow[]): JobsSummary {
  return jobs.reduce<JobsSummary>(
    (summary, job) => {
      const status = (job.status || "").toLowerCase();
      return {
        total: summary.total + 1,
        running: summary.running + (status === "running" && !job.paused ? 1 : 0),
        queued: summary.queued + (status === "pending" || status === "queued" ? 1 : 0),
        failed: summary.failed + (status === "failed" || status === "orphaned" ? 1 : 0),
        complete: summary.complete + (status === "complete" || status === "completed" ? 1 : 0),
        paused: summary.paused + (job.paused ? 1 : 0)
      };
    },
    { total: 0, running: 0, queued: 0, failed: 0, complete: 0, paused: 0 }
  );
}

export function jobMatchesStatus(job: JobTableRow, filter: JobStatusFilter): boolean {
  if (filter === "all") {
    return true;
  }
  if (filter === "paused") {
    return job.paused === true;
  }
  const status = (job.status || "").toLowerCase();
  if (filter === "failed") {
    return status === "failed" || status === "orphaned";
  }
  if (filter === "queued") {
    return status === "pending" || status === "queued";
  }
  if (filter === "complete") {
    return status === "complete" || status === "completed";
  }
  return status === filter && job.paused !== true;
}

export function monitoringStrip(overview: MonitoringOverview): readonly MetricItem[] {
  const runtimeValue = overview.runtime.available ? String(overview.runtime.containers.length) : "-";
  const activeDeployments = overview.deployments.running ?? overview.deployments.active ?? 0;
  const succeededDeployments = overview.deployments.succeeded ?? overview.deployments.completed ?? 0;
  const keytabStatus = overview.keytab.status;
  return [
    {
      label: "Runtime",
      value: runtimeValue,
      tone: overview.runtime.available ? "good" : "bad"
    },
    {
      label: "Deployments",
      value: String(overview.deployments.total),
      tone: activeDeployments > 0 ? "active" : succeededDeployments > 0 ? "good" : "neutral"
    },
    {
      label: "Failed",
      value: String(overview.deployments.failed),
      tone: overview.deployments.failed > 0 ? "bad" : "good"
    },
    {
      label: "Keytab",
      value: fallbackText(keytabStatus),
      tone: statusTone(keytabStatus)
    }
  ];
}

export function buildSignalMetrics(hub: SignalsHubResponse): readonly SignalMetric[] {
  const critical = hub.signals.filter((signal) => signal.tone === "bad").length;
  const needsOperator = hub.signals.filter((signal) => signal.tone === "bad" || signal.tone === "active").length;
  const ready = hub.signals.filter((signal) => signal.tone === "good").length;
  const runtimeAvailable = hub.source_health.runtime_available;
  const setupHealth = hub.source_health.setup_health || "unknown";

  return [
    { label: "Critical", value: String(critical), tone: critical > 0 ? "bad" : "good" },
    { label: "Needs operator", value: String(needsOperator), tone: needsOperator > 0 ? "bad" : "good" },
    { label: "Ready", value: String(ready), tone: ready > 0 ? "good" : "neutral" },
    { label: "Runtime", value: runtimeAvailable ? "up" : "down", tone: runtimeAvailable ? "good" : "bad" },
    { label: "Setup", value: setupHealth, tone: statusTone(setupHealth) }
  ];
}

export function rankedSignalPaths(paths: readonly OperatorPath[]): readonly OperatorPath[] {
  return paths.toSorted((left, right) => left.priority - right.priority || left.label.localeCompare(right.label));
}

export interface FleetCounts {
  readonly total: number;
  readonly running: number;
  readonly attention: number;
  readonly agents: number;
  readonly staleAgents: number;
  readonly autopilotDevices: number;
  readonly missingAutopilot: number;
}

export interface FleetMachineRow {
  readonly id: string;
  readonly name: string;
  readonly vmid?: number;
  readonly vm?: VmFleetRow;
  readonly agent?: AgentFleetRow;
  readonly autopilotDevice?: AutopilotDeviceFleetRow;
  readonly agentId?: string;
  readonly status: string | undefined;
  readonly serial: string | undefined;
  readonly ipAddress: string | undefined;
  readonly os: string | undefined;
  readonly qga: string | undefined;
  readonly phase: string | undefined;
  readonly heartbeat: string | undefined;
  readonly version: string | undefined;
  readonly method: string;
  readonly mdmEnrollment: string;
  readonly lifecycleLabels: readonly string[];
  readonly stale: boolean;
}

export function vmDisplayName(vm: VmFleetRow): string {
  return vm.name || vm.hostname || `VM ${String(vm.vmid)}`;
}

export function vmJoinLabels(vm: VmFleetRow): readonly string[] {
  const labels: string[] = [];
  if (vm.lifecycle_state) {
    if (vm.lifecycle_domain_joined || vm.lifecycle_state === "ad_domain_joined" || vm.lifecycle_state === "hybrid_joined") {
      labels.push("domain");
    }
    if (vm.lifecycle_entra_joined || vm.lifecycle_state === "entra_joined" || vm.lifecycle_state === "hybrid_joined") {
      labels.push("Entra ID");
    }
    if (vm.lifecycle_intune_enrolled || vm.lifecycle_state === "intune_enrolled") {
      labels.push("Intune");
    }
    if (vm.lifecycle_autopilot_registered || vm.lifecycle_state === "autopilot_registered") {
      labels.push("Autopilot ID");
    }
    if (vm.lifecycle_state === "workgroup_unenrolled" && !labels.length) {
      labels.push("unenrolled");
    }
    if (vm.has_hash) {
      labels.push("hash");
    }
    return labels;
  }
  if (vm.part_of_domain || vm.hybrid_joined) {
    labels.push("domain");
  }
  if (vm.aad_joined || vm.entra_id_joined) {
    labels.push("Entra ID");
  }
  if (vm.in_intune) {
    labels.push("Intune");
  }
  if (vm.in_autopilot) {
    labels.push("Autopilot ID");
  }
  if (vm.has_hash) {
    labels.push("hash");
  }
  if (!labels.length && (vm.status || "").toLowerCase() === "running") {
    labels.push("unenrolled");
  }
  return labels;
}

export function agentLifecycleLabels(agent: AgentFleetRow): readonly string[] {
  const labels: string[] = [];
  if (agent.lifecycle_state) {
    if (
      agent.lifecycle_domain_joined
      || agent.lifecycle_state === "ad_domain_joined"
      || agent.lifecycle_state === "hybrid_joined"
    ) {
      labels.push("domain");
    }
    if (
      agent.lifecycle_entra_joined
      || agent.lifecycle_state === "entra_joined"
      || agent.lifecycle_state === "hybrid_joined"
    ) {
      labels.push("Entra ID");
    }
    if (agent.lifecycle_intune_enrolled || agent.lifecycle_state === "intune_enrolled") {
      labels.push("Intune");
    }
    if (agent.lifecycle_autopilot_registered || agent.lifecycle_state === "autopilot_registered") {
      labels.push("Autopilot ID");
    }
    if (agent.lifecycle_state === "workgroup_unenrolled" && !labels.length) {
      labels.push("unenrolled");
    }
  } else {
    if (agent.domain_joined) {
      labels.push("domain");
    }
    if (agent.entra_joined) {
      labels.push("Entra ID");
    }
  }
  if (agent.hash_capture_supported) {
    labels.push("hash");
  }
  return labels;
}

export function agentIsStale(agent: AgentFleetRow, now = Date.now()): boolean {
  if (!agent.last_heartbeat_at) {
    return agent.approval_status !== "pending";
  }
  const at = new Date(agent.last_heartbeat_at).getTime();
  if (Number.isNaN(at)) {
    return false;
  }
  return now - at > 15 * 60 * 1000;
}

export function summarizeFleet(fleet: VmsFleetResponse): FleetCounts {
  const running = fleet.vms.filter((vm) => (vm.status || "").toLowerCase() === "running").length;
  const attention = fleet.vms.filter((vm) => vmJoinLabels(vm).includes("unenrolled")).length + fleet.missing_vms.length;
  return {
    total: fleet.vms.length,
    running,
    attention,
    agents: fleet.agents.length,
    staleAgents: fleet.agents.filter((agent) => agentIsStale(agent)).length,
    autopilotDevices: fleet.autopilot_devices.length,
    missingAutopilot: fleet.missing_vms.length
  };
}

function normalizedIdentity(value: unknown): string {
  return typeof value === "string" && value.trim() ? value.trim().toLowerCase() : "";
}

function addAgentIndexes(
  agent: AgentFleetRow,
  byVmid: Map<number, AgentFleetRow>,
  byIdentity: Map<string, AgentFleetRow>
): void {
  if (typeof agent.vmid === "number") {
    byVmid.set(agent.vmid, agent);
  }
  for (const value of [agent.serial_number, agent.computer_name, agent.primary_ipv4]) {
    const key = normalizedIdentity(value);
    if (key && !byIdentity.has(key)) {
      byIdentity.set(key, agent);
    }
  }
}

function addDeviceIndexes(
  device: AutopilotDeviceFleetRow,
  byIdentity: Map<string, AutopilotDeviceFleetRow>
): void {
  for (const value of [device.serial, device.display_name]) {
    const key = normalizedIdentity(value);
    if (key && !byIdentity.has(key)) {
      byIdentity.set(key, device);
    }
  }
}

function findAgentForVm(
  vm: VmFleetRow,
  byVmid: Map<number, AgentFleetRow>,
  byIdentity: Map<string, AgentFleetRow>
): AgentFleetRow | undefined {
  return byVmid.get(vm.vmid)
    ?? byIdentity.get(normalizedIdentity(vm.serial))
    ?? byIdentity.get(normalizedIdentity(vm.hostname))
    ?? byIdentity.get(normalizedIdentity(vm.name))
    ?? byIdentity.get(normalizedIdentity(vm.ip_address));
}

function findDeviceForMachine(
  values: readonly unknown[],
  byIdentity: Map<string, AutopilotDeviceFleetRow>
): AutopilotDeviceFleetRow | undefined {
  for (const value of values) {
    const key = normalizedIdentity(value);
    if (!key) {
      continue;
    }
    const found = byIdentity.get(key);
    if (found) {
      return found;
    }
  }
  return undefined;
}

function uniqueLabels(labels: readonly string[]): readonly string[] {
  return Array.from(new Set(labels));
}

function machineMethod(vm: VmFleetRow | undefined, agent: AgentFleetRow | undefined): string {
  if (vm && agent) {
    return "agent + monitor";
  }
  if (agent) {
    return "agent";
  }
  return "monitor";
}

function machineLabels(
  vm: VmFleetRow | undefined,
  agent: AgentFleetRow | undefined,
  device: AutopilotDeviceFleetRow | undefined
): readonly string[] {
  const labels = vm ? [...vmJoinLabels(vm)] : [...(agent ? agentLifecycleLabels(agent) : [])];
  if (device?.enrollment_state && !labels.includes("Intune") && device.enrollment_state.toLowerCase() === "enrolled") {
    labels.push("Intune");
  }
  if (device && !labels.includes("Autopilot ID")) {
    labels.push("Autopilot ID");
  }
  if (device?.has_local_hash && !labels.includes("hash")) {
    labels.push("hash");
  }
  return uniqueLabels(labels);
}

export function buildFleetMachineRows(fleet: VmsFleetResponse): readonly FleetMachineRow[] {
  const agentsByVmid = new Map<number, AgentFleetRow>();
  const agentsByIdentity = new Map<string, AgentFleetRow>();
  const devicesByIdentity = new Map<string, AutopilotDeviceFleetRow>();

  for (const agent of fleet.agents) {
    addAgentIndexes(agent, agentsByVmid, agentsByIdentity);
  }
  for (const device of fleet.autopilot_devices) {
    addDeviceIndexes(device, devicesByIdentity);
  }

  const rows: FleetMachineRow[] = fleet.vms.map((vm) => {
    const agent = findAgentForVm(vm, agentsByVmid, agentsByIdentity);
    const device = findDeviceForMachine(
      [vm.serial, vm.hostname, vm.name, agent?.serial_number, agent?.computer_name],
      devicesByIdentity
    );
    return {
      id: `vm-${String(vm.vmid)}`,
      name: vmDisplayName(vm),
      vmid: vm.vmid,
      vm,
      ...(agent ? { agent, agentId: agent.agent_id } : {}),
      ...(device ? { autopilotDevice: device } : {}),
      status: vm.status,
      serial: vm.serial ?? agent?.serial_number,
      ipAddress: vm.ip_address ?? agent?.primary_ipv4,
      os: vm.os_caption ?? vm.os_build ?? agent?.os_name ?? agent?.os_build,
      qga: vm.qga ?? agent?.qga_state,
      phase: agent?.current_phase ?? (vm.sequence_name || undefined),
      heartbeat: agent?.last_heartbeat_at ?? agent?.last_seen_at ?? vm.monitor_checked_at ?? vm.monitor_probed_at,
      version: agent?.agent_version,
      method: machineMethod(vm, agent),
      mdmEnrollment: device?.enrollment_state ?? (vm.in_intune ? "Intune" : "-"),
      lifecycleLabels: machineLabels(vm, agent, device),
      stale: agent ? agentIsStale(agent) : false
    };
  });

  return rows.toSorted((left, right) => {
    if (left.vmid !== undefined && right.vmid !== undefined) {
      return left.vmid - right.vmid;
    }
    if (left.vmid !== undefined) {
      return -1;
    }
    if (right.vmid !== undefined) {
      return 1;
    }
    return left.name.localeCompare(right.name);
  });
}

export function vmMatchesFilter(vm: VmFleetRow, filter: string): boolean {
  const query = filter.trim().toLowerCase();
  if (!query) {
    return true;
  }
  return [
    String(vm.vmid),
    vm.name,
    vm.hostname,
    vm.serial,
    vm.status,
    vm.ip_address,
    vm.sequence_name,
    ...vmJoinLabels(vm)
  ].some((value) => fallbackText(value).toLowerCase().includes(query));
}

export function machineMatchesFilter(row: FleetMachineRow, filter: string): boolean {
  const query = filter.trim().toLowerCase();
  if (!query) {
    return true;
  }
  return [
    row.name,
    row.vmid === undefined ? "" : String(row.vmid),
    row.agentId,
    row.status,
    row.serial,
    row.ipAddress,
    row.os,
    row.qga,
    row.phase,
    row.method,
    row.mdmEnrollment,
    ...row.lifecycleLabels
  ].some((value) => fallbackText(value).toLowerCase().includes(query));
}

export function deviceDisplayName(device: AutopilotDeviceFleetRow): string {
  return device.display_name || device.serial || device.id || "-";
}
