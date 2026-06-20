export interface AppBootstrap {
  readonly buildSha?: string;
  readonly buildTime?: string;
  readonly userName?: string;
  readonly userEmail?: string;
}

export interface MigratedRoute {
  readonly path: string;
  readonly label: string;
  readonly group: OperatorGroupLabel;
  readonly phase: "foundation" | "read-only" | "operational";
}

export type OperatorGroupLabel = "Observe" | "Deploy" | "Build" | "Infrastructure" | "Fleet" | "Settings";

export interface OperatorRoute {
  readonly path: string;
  readonly label: string;
  readonly group: OperatorGroupLabel;
  readonly phase: MigratedRoute["phase"] | "legacy";
  readonly active: boolean;
  readonly legacy?: boolean;
  readonly navParentPath?: string;
  readonly showInNav?: boolean;
}

export interface OperatorNavGroup {
  readonly label: OperatorGroupLabel;
  readonly items: readonly OperatorRoute[];
}

export type OperatorModeId = "home" | "deploy" | "build" | "infra" | "fleet" | "settings";

export type OperatorOutcomeTone = "good" | "blue" | "teal" | "purple" | "warn" | "bad";

export interface OperatorMode {
  readonly id: OperatorModeId;
  readonly label: string;
  readonly longLabel: string;
  readonly href: string;
}

export interface OperatorOutcomeRoute {
  readonly label: string;
  readonly href: string;
  readonly purpose: string;
}

export interface OperatorOutcome {
  readonly id: string;
  readonly mode: OperatorModeId;
  readonly eyebrow: string;
  readonly title: string;
  readonly summary: string;
  readonly primaryHref: string;
  readonly actionLabel: string;
  readonly tone: OperatorOutcomeTone;
  readonly relatedRoutes: readonly OperatorOutcomeRoute[];
}

export interface OperatorQuickRoute {
  readonly label: string;
  readonly href: string;
  readonly summary: string;
  readonly mode: OperatorModeId;
}

export interface OperatorFlowStep {
  readonly label: string;
  readonly href: string;
  readonly group: OperatorGroupLabel;
  readonly state: "React" | "Jinja";
}

export interface OperatorFlow {
  readonly id: string;
  readonly label: string;
  readonly group: OperatorGroupLabel;
  readonly summary: string;
  readonly steps: readonly OperatorFlowStep[];
}

export interface LiveSocketMessage {
  readonly topic?: string;
  readonly type?: string;
  readonly event?: string;
  readonly rows?: unknown;
  readonly agents?: unknown;
  readonly result?: unknown;
  readonly image_url?: string;
  readonly vmid?: number;
  readonly correlation_id?: string;
  readonly error?: string;
  readonly detail?: string;
  readonly data?: unknown;
  readonly payload?: unknown;
}

export interface ServiceHealth {
  readonly service?: string;
  readonly service_id?: string;
  readonly service_type?: string;
  readonly status?: string;
  readonly age_seconds?: number | null;
  readonly detail?: string;
  readonly message?: string;
}

export interface ServicesResponse {
  readonly services: readonly ServiceHealth[];
  readonly available: boolean;
  readonly error?: string;
}

export interface RunningJob {
  readonly id: string;
  readonly playbook?: string;
  readonly target?: string;
  readonly started?: string | null;
  readonly elapsed_seconds?: number;
  readonly progress_pct?: number;
  readonly paused?: boolean;
}

export interface RunningJobsResponse {
  readonly running: readonly RunningJob[];
  readonly running_count: number;
  readonly queued_count: number;
}

export interface RecentJob {
  readonly id: string;
  readonly playbook?: string | null;
  readonly status?: string | null;
  readonly started?: string | null;
  readonly ended?: string | null;
  readonly duration?: string | null;
  readonly target?: string;
}

export interface RecentJobsResponse {
  readonly jobs: readonly RecentJob[];
}

export interface JobTableRow {
  readonly id: string;
  readonly playbook?: string | null;
  readonly status?: string | null;
  readonly started?: string | null;
  readonly ended?: string | null;
  readonly duration?: string | null;
  readonly args?: Readonly<Record<string, unknown>>;
  readonly paused?: boolean;
}

export interface JobsTableResponse {
  readonly jobs: readonly JobTableRow[];
}

export interface FleetSummary {
  readonly total: number;
  readonly ad_joined_pct?: number;
  readonly autopilot_pct?: number;
  readonly intune_pct?: number;
  readonly mde_pct?: number;
  readonly [key: string]: unknown;
}

export interface MonitoringSummary {
  readonly devices: number;
  readonly ad: number;
  readonly entra: number;
  readonly intune: number;
}

export interface CockpitSummary {
  readonly readiness_score: number;
  readonly jobs: RunningJobsResponse;
  readonly recent_jobs: readonly RecentJob[];
  readonly services: ServicesResponse;
  readonly fleet: FleetSummary;
  readonly monitoring: MonitoringSummary;
}

export interface JobsLivePayload {
  readonly running?: RunningJobsResponse;
  readonly recent?: RecentJobsResponse;
  readonly table?: JobsTableResponse;
  readonly generated_at?: string;
}

export interface RuntimeContainer {
  readonly id?: string;
  readonly name: string;
  readonly service: string;
  readonly image?: string;
  readonly status: string;
  readonly health?: string;
  readonly started_at?: string;
  readonly finished_at?: string;
  readonly restart_count?: number;
  readonly log_url?: string;
}

export interface RuntimeServicesResponse {
  readonly available: boolean;
  readonly error: string;
  readonly containers: readonly RuntimeContainer[];
}

export interface ServiceLogsResponse {
  readonly container: string;
  readonly service: string;
  readonly tail: number;
  readonly lines: readonly string[];
}

export interface DeploymentSummary {
  readonly total: number;
  readonly active?: number;
  readonly running?: number;
  readonly completed?: number;
  readonly succeeded?: number;
  readonly failed: number;
  readonly stuck?: number;
  readonly regressed?: number;
  readonly slow?: number;
  readonly median_completion_seconds?: number | null;
  readonly p95_completion_seconds?: number | null;
  readonly recent_failure_rate?: number;
}

export interface KeytabHealth {
  readonly status?: string;
  readonly detail?: string;
  readonly message?: string;
  readonly checked_at?: string;
  readonly [key: string]: unknown;
}

export interface MonitoringOverview {
  readonly runtime: RuntimeServicesResponse;
  readonly deployments: DeploymentSummary;
  readonly keytab: KeytabHealth;
}

export type SignalTone = "good" | "active" | "bad" | "neutral";

export type SignalFamily =
  | "runtime"
  | "service_health"
  | "jobs"
  | "build_host"
  | "artifacts"
  | "deploy_readiness"
  | "deployment_speed"
  | "agent"
  | "lifecycle"
  | "identity"
  | "fleet_evidence";

export interface SignalBuildInfo {
  readonly sha?: string;
  readonly sha_short?: string;
  readonly build_time?: string;
}

export interface SignalSourceHealth {
  readonly runtime_available: boolean;
  readonly setup_health?: string;
  readonly keytab_status?: string;
}

export interface OperatorSignal {
  readonly id: string;
  readonly family: SignalFamily;
  readonly label: string;
  readonly status: string;
  readonly tone: SignalTone;
  readonly summary: string;
  readonly count?: string | number;
  readonly source?: string;
  readonly href?: string;
}

export interface SignalMetric {
  readonly label: string;
  readonly value: string;
  readonly tone?: SignalTone;
}

export interface OperatorPath {
  readonly id: string;
  readonly priority: number;
  readonly label: string;
  readonly status: string;
  readonly tone: SignalTone;
  readonly summary: string;
  readonly action_label: string;
  readonly href: string;
  readonly source?: string;
}

export interface LifecycleLane {
  readonly id: string;
  readonly label: string;
  readonly value: string;
  readonly detail?: string;
  readonly status: string;
  readonly tone: SignalTone;
}

export interface DeploymentRunDigest {
  readonly deployment_key?: string;
  readonly deployment_type?: string;
  readonly current_phase?: string;
  readonly elapsed_seconds?: number | null;
  readonly duration_seconds?: number | null;
  readonly slowest_phase?: string;
  readonly slowest_phase_seconds?: number | null;
  readonly health?: string;
  readonly state?: string;
  readonly next_expected_evidence?: string;
  readonly evidence?: Readonly<Record<string, unknown>>;
}

export interface DeploymentBottleneck {
  readonly deployment_type?: string;
  readonly phase_key?: string;
  readonly phase_label?: string;
  readonly count?: number;
  readonly health?: string;
  readonly p95_seconds?: number | null;
}

export interface DeploymentHealthDigest {
  readonly summary: DeploymentSummary;
  readonly active: readonly DeploymentRunDigest[];
  readonly recent_completions: readonly DeploymentRunDigest[];
  readonly bottlenecks: readonly DeploymentBottleneck[];
}

export interface FleetSignalRow {
  readonly vmid: number;
  readonly vm_name: string;
  readonly node?: string;
  readonly lifecycle: string;
  readonly tone: SignalTone;
  readonly pve_status?: string;
  readonly windows?: string;
  readonly serial?: string;
  readonly ad?: string;
  readonly entra?: string;
  readonly intune?: string;
  readonly last_checked?: string;
  readonly href: string;
}

export interface SignalsHubResponse {
  readonly generated_at: string;
  readonly build: SignalBuildInfo;
  readonly source_health: SignalSourceHealth;
  readonly metrics: readonly SignalMetric[];
  readonly signals: readonly OperatorSignal[];
  readonly operator_paths: readonly OperatorPath[];
  readonly lifecycle_lanes: readonly LifecycleLane[];
  readonly deployment_health: DeploymentHealthDigest;
  readonly services: readonly ServiceHealth[];
  readonly runtime: RuntimeServicesResponse;
  readonly fleet_attention: readonly FleetSignalRow[];
}

export interface VmFleetRow {
  readonly vmid: number;
  readonly name: string;
  readonly hostname?: string;
  readonly serial?: string;
  readonly status?: string;
  readonly ip_address?: string;
  readonly os_caption?: string;
  readonly os_build?: string;
  readonly in_autopilot?: boolean;
  readonly in_intune?: boolean;
  readonly aad_joined?: boolean;
  readonly part_of_domain?: boolean;
  readonly hybrid_joined?: boolean;
  readonly entra_id_joined?: boolean;
  readonly has_hash?: boolean;
  readonly lifecycle_state?: string;
  readonly lifecycle_label?: string;
  readonly lifecycle_source?: string;
  readonly lifecycle_observed_at?: string;
  readonly lifecycle_domain_joined?: boolean;
  readonly lifecycle_entra_joined?: boolean;
  readonly lifecycle_intune_enrolled?: boolean;
  readonly lifecycle_autopilot_registered?: boolean;
  readonly target_os?: string;
  readonly sequence_name?: string | null;
  readonly monitor_checked_at?: string;
  readonly monitor_probed_at?: string;
  readonly hostname_join_label?: string;
  readonly hostname_join_title?: string;
  readonly qga?: string;
  readonly qga_error?: string;
  readonly qga_retry_in_seconds?: number;
  readonly [key: string]: unknown;
}

export interface AgentFleetRow {
  readonly agent_id: string;
  readonly approval_id?: string;
  readonly approval_status?: string;
  readonly pairing_status?: "waiting_for_approval" | "waiting_for_claim" | "waiting_for_heartbeat" | "paired" | "unknown";
  readonly needs_pairing?: boolean;
  readonly update_status?: "current" | "upgrade_available" | "blocked" | "unknown";
  readonly upgrade_available?: boolean;
  readonly published_agent_version?: string;
  readonly update_reason?: string;
  readonly agent_msi_sha256?: string;
  readonly agent_msi_size_bytes?: number | null;
  readonly vmid?: number | null;
  readonly computer_name?: string;
  readonly serial_number?: string;
  readonly primary_ipv4?: string;
  readonly os_name?: string;
  readonly os_build?: string;
  readonly qga_state?: string;
  readonly domain_joined?: boolean | null;
  readonly entra_joined?: boolean | null;
  readonly lifecycle_state?: string;
  readonly lifecycle_label?: string;
  readonly lifecycle_source?: string;
  readonly lifecycle_observed_at?: string;
  readonly lifecycle_domain_joined?: boolean;
  readonly lifecycle_entra_joined?: boolean;
  readonly lifecycle_intune_enrolled?: boolean;
  readonly lifecycle_autopilot_registered?: boolean;
  readonly current_phase?: string;
  readonly current_run_id?: string;
  readonly agent_version?: string;
  readonly hash_capture_supported?: boolean;
  readonly last_heartbeat_at?: string;
  readonly last_seen_at?: string;
}

export interface AutopilotDeviceFleetRow {
  readonly id: string;
  readonly serial: string;
  readonly display_name?: string;
  readonly group_tag?: string;
  readonly profile_status?: string;
  readonly profile_ok?: boolean;
  readonly enrollment_state?: string;
  readonly manufacturer?: string;
  readonly model?: string;
  readonly last_contact?: string;
  readonly has_local_hash?: boolean;
}

export interface LabBubble {
  readonly id: string;
  readonly name: string;
  readonly slug?: string;
  readonly lifecycle_state?: string;
  readonly domain_name?: string;
  readonly netbios_name?: string;
  readonly cidr?: string;
  readonly gateway_ip?: string;
  readonly planned_bridge?: string;
  readonly planned_vlan?: number | null;
  readonly isolation_status?: string;
  readonly dhcp_scope?: string;
  readonly dhcp_pool_start?: string;
  readonly dhcp_pool_end?: string;
  readonly dhcp_owner_asset_id?: string | null;
  readonly dc_ready?: boolean;
  readonly dns_ready?: boolean;
  readonly dhcp_ready?: boolean;
  readonly workload_ready?: boolean;
}

export interface LabBubbleAsset {
  readonly id: string;
  readonly bubble_id: string;
  readonly asset_type: string;
  readonly asset_role: string;
  readonly vmid?: number | null;
  readonly vm_uuid?: string | null;
  readonly run_id?: string | null;
  readonly agent_id?: string | null;
  readonly service_id?: string | null;
  readonly membership_state?: string;
  readonly evidence_state?: string;
  readonly notes?: string;
}

export interface LabBubbleService {
  readonly id: string;
  readonly bubble_id: string;
  readonly service_kind: string;
  readonly service_name: string;
  readonly scope?: string;
  readonly provider_asset_id?: string | null;
  readonly readiness_state?: string;
  readonly consumer_refs?: readonly unknown[];
  readonly evidence_summary?: Readonly<Record<string, unknown>> & {
    readonly credential_ids?: readonly number[];
  };
}

export interface LabBubbleFleetSection {
  readonly bubble: LabBubble;
  readonly workstation_count?: number;
  readonly running_count?: number;
  readonly stopped_count?: number;
  readonly assets?: readonly LabBubbleAsset[];
  readonly vms?: readonly VmFleetRow[];
  readonly readiness?: Readonly<Record<string, boolean>>;
}

export interface LabBubbleInfrastructureNode {
  readonly bubble: LabBubble;
  readonly asset: LabBubbleAsset;
  readonly role: string;
  readonly vm?: VmFleetRow | null;
  readonly agent?: AgentFleetRow | null;
}

export interface LabBubbleConnectedService extends LabBubbleService {
  readonly bubble: LabBubble;
}

export interface LabBubbleGateState {
  readonly bubble_id: string;
  readonly workgroup?: Readonly<Record<string, unknown>>;
  readonly domain_join?: Readonly<Record<string, unknown>>;
}

export interface LabBubbleTopology {
  readonly workstation_fleets: readonly LabBubbleFleetSection[];
  readonly critical_infrastructure: readonly LabBubbleInfrastructureNode[];
  readonly connected_services: readonly LabBubbleConnectedService[];
  readonly unassigned_assets: readonly VmFleetRow[];
  readonly warnings: readonly string[];
  readonly gate_states: readonly LabBubbleGateState[];
}

export interface VmsFleetResponse {
  readonly vms: readonly VmFleetRow[];
  readonly proxmox_vms?: readonly VmFleetRow[];
  readonly missing_vms: readonly VmFleetRow[];
  readonly agents: readonly AgentFleetRow[];
  readonly autopilot_devices: readonly AutopilotDeviceFleetRow[];
  readonly bubble_topology?: LabBubbleTopology;
  readonly ap_error: string;
  readonly cache_age_seconds?: number | null;
  readonly cache_fetched_at_iso?: string;
  readonly cache_refreshing: boolean;
  readonly monitor_sweep?: Readonly<Record<string, unknown>> | null;
  readonly generated_at: string;
}

export interface AgentDownloadBootstrapTokenResponse {
  readonly schema_version?: number;
  readonly bootstrap_token: string;
  readonly token_kind?: string;
}

export interface CredentialSummary {
  readonly id: number;
  readonly name: string;
  readonly type: string;
  readonly created_at?: string | null;
  readonly updated_at?: string | null;
}

export interface VmScreenshot {
  readonly vmid: number;
  readonly image_url: string;
  readonly content_type: string;
  readonly captured_at: string;
  readonly expires_at: string;
  readonly source: string;
  readonly bytes: number;
}

export interface VmLinkageCheck {
  readonly label: string;
  readonly ok?: boolean | null;
  readonly value: string;
}

export interface VmKnownCredential {
  readonly source: string;
  readonly label: string;
  readonly username: string;
  readonly password_available: boolean;
  readonly password_mask: string;
  readonly vm_name: string;
  readonly run_id: string;
  readonly run_url: string;
  readonly updated_at?: string | null;
  readonly note: string;
}

export interface VmTimelineEvent {
  readonly at: string;
  readonly source: string;
  readonly type: string;
  readonly severity: string;
  readonly summary: string;
  readonly details?: Readonly<Record<string, unknown>>;
}

export interface VmIdentitySync {
  readonly source: string;
  readonly last_checked_at: string;
  readonly ad_count: number;
  readonly entra_count: number;
  readonly intune_count: number;
}

export interface VmDetailEvidenceResponse {
  readonly vmid: number;
  readonly fleet_vm?: VmFleetRow | null;
  readonly pve: Readonly<Record<string, unknown>>;
  readonly probe: Readonly<Record<string, unknown>>;
  readonly ad_matches: readonly Readonly<Record<string, unknown>>[];
  readonly entra_matches: readonly Readonly<Record<string, unknown>>[];
  readonly intune_matches: readonly Readonly<Record<string, unknown>>[];
  readonly linkage: readonly VmLinkageCheck[];
  readonly known_credentials: readonly VmKnownCredential[];
  readonly latest_screenshot?: VmScreenshot | null;
  readonly screenshot_history: readonly VmScreenshot[];
  readonly timeline: readonly VmTimelineEvent[];
  readonly history: Readonly<Record<string, unknown>>;
  readonly identity_sync: VmIdentitySync;
}

export interface FleetLivePayload {
  readonly rows?: readonly VmFleetRow[];
  readonly agents?: readonly AgentFleetRow[];
  readonly monitor_sweep?: Readonly<Record<string, unknown>>;
  readonly cache_age_seconds?: number | null;
  readonly refreshing?: boolean;
  readonly generated_at?: string;
}

export interface CloudDeviceRecord {
  readonly id?: string;
  readonly object_id?: string;
  readonly serial?: string;
  readonly display_name?: string;
  readonly name?: string;
  readonly source?: string;
  readonly profile?: string;
  readonly group_tag?: string;
  readonly last_contact?: string;
  readonly [key: string]: unknown;
}

export interface CloudDeviceGroup {
  readonly serial: string;
  readonly display_name?: string;
  readonly intune?: CloudDeviceRecord | null;
  readonly autopilot?: CloudDeviceRecord | null;
  readonly entra?: CloudDeviceRecord | null;
  readonly pve?: Readonly<Record<string, unknown>> | null;
  readonly [key: string]: unknown;
}

export interface CloudDevicesResponse {
  readonly groups: readonly CloudDeviceGroup[];
  readonly unmatched: Readonly<Record<string, readonly CloudDeviceRecord[]>>;
  readonly meta: Readonly<Record<string, unknown>>;
  readonly windows_only: boolean;
  readonly deletions: readonly Readonly<Record<string, unknown>>[];
}

export interface HashFileRow {
  readonly filename: string;
  readonly serial?: string;
  readonly name?: string;
  readonly group_tag?: string;
  readonly size?: number;
  readonly mtime?: string;
  readonly in_intune?: boolean;
  readonly [key: string]: unknown;
}

export interface HashesResponse {
  readonly hash_files: readonly HashFileRow[];
}

export interface FileShelfRow {
  readonly name: string;
  readonly url: string;
  readonly size?: string;
  readonly size_bytes?: number;
  readonly modified?: string;
  readonly modified_epoch?: number;
  readonly [key: string]: unknown;
}

export interface FilesResponse {
  readonly files: readonly FileShelfRow[];
}

export interface SettingsField {
  readonly key: string;
  readonly label?: string;
  readonly type: string;
  readonly value?: string | number | boolean | null;
  readonly is_set?: boolean;
  readonly readonly?: boolean;
  readonly source?: string;
  readonly options?: readonly string[];
  readonly labels?: Readonly<Record<string, string>>;
  readonly help?: string;
}

export interface SettingsSection {
  readonly section: string;
  readonly source: string;
  readonly fields: readonly SettingsField[];
}

export interface SettingsResponse {
  readonly sections: readonly SettingsSection[];
  readonly saved?: boolean;
  readonly hypervisor_type: string;
  readonly proxmox_bootstrap: Readonly<Record<string, unknown>>;
}

export interface MonitoringSearchOu {
  readonly id: number;
  readonly dn: string;
  readonly label: string;
  readonly enabled: boolean;
  readonly sort_order: number;
  readonly created_at?: string;
  readonly updated_at?: string;
}

export interface MonitoringSettingsFullResponse {
  readonly settings: {
    readonly enabled: boolean;
    readonly interval_seconds: number;
    readonly ad_credential_id: number;
    readonly updated_at?: string;
    readonly error?: string;
  };
  readonly search_ous: readonly MonitoringSearchOu[];
  readonly domain_creds: readonly CredentialSummary[];
  readonly keytab: Readonly<Record<string, unknown>>;
}
