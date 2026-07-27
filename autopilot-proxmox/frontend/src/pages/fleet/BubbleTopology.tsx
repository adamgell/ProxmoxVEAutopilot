
import { Panel } from "../../components/ui";
import type {
  AgentFleetRow,
  CredentialSummary,
  LabBubble,
  LabBubbleAsset,
  LabBubbleConnectedService,
  LabBubbleInfrastructureNode,
  LabBubbleTopology,
  VmFleetRow
} from "../../contracts";
import { fallbackText } from "../../viewModels";

export interface OrphanVnetSubnet {
  readonly subnet?: string;
  readonly gateway?: string;
  readonly snat?: boolean;
  readonly dhcp_dns_server?: string;
  readonly dhcp_range?: string;
}

export type BubbleDraftMode = "create" | "edit";

export type BubbleFormField = keyof BubbleFormValues;

/**
 * The lab_bubbles schema stores lifecycle_state and isolation_status as
 * free text so values can grow over time, but in practice operators move
 * a bubble through a small set of well-known states. Surface those as the
 * primary dropdown options so the edit form is a focused choice instead
 * of a blank text box.
 */
export interface OrphanVnet {
  readonly vnet: string;
  readonly zone: string;
  readonly alias?: string;
  readonly type?: string;
  readonly subnet?: OrphanVnetSubnet | undefined;
}

export interface BubbleBoundNetwork {
  readonly vnet: string;
  readonly zone: string;
  readonly subnet: string;
  readonly gateway: string;
  readonly dhcpStart: string;
  readonly dhcpEnd: string;
  readonly dhcpDnsServer: string;
  readonly subnetSource: "sdn" | "binding";
}

// Parse a PVE-formatted dhcp-range string
// ("start-address=192.168.55.100,end-address=192.168.55.199")
// into separate start/end IPs for pre-filling the bubble form's
// dhcp_pool_start / dhcp_pool_end inputs.

export type BubbleAssignment = {
  readonly bubble: LabBubble;
  readonly asset: LabBubbleAsset;
};

function bubbleSort(left: LabBubble, right: LabBubble): number {
  return left.name.localeCompare(right.name, undefined, { sensitivity: "base" });
}

export function topologyBubbles(topology: LabBubbleTopology): readonly LabBubble[] {
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

export function topologyAssignmentsByVmid(topology: LabBubbleTopology): ReadonlyMap<number, BubbleAssignment> {
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

export function topologyAssets(topology: LabBubbleTopology): readonly {
  readonly bubble: LabBubble;
  readonly asset: LabBubbleAsset;
  readonly vm: VmFleetRow | null | undefined;
  readonly agent: AgentFleetRow | null | undefined;
}[] {
  const items: {
    readonly bubble: LabBubble;
    readonly asset: LabBubbleAsset;
    readonly vm: VmFleetRow | null | undefined;
    readonly agent: AgentFleetRow | null | undefined;
  }[] = [];
  for (const fleet of topology.workstation_fleets) {
    const vmById = new Map((fleet.vms ?? []).map((vm) => [vm.vmid, vm]));
    for (const asset of fleet.assets ?? []) {
      const vm = typeof asset.vmid === "number" ? vmById.get(asset.vmid) : undefined;
      items.push({ bubble: fleet.bubble, asset, vm, agent: undefined });
    }
  }
  for (const node of topology.critical_infrastructure) {
    items.push({ bubble: node.bubble, asset: node.asset, vm: node.vm, agent: node.agent });
  }
  return items;
}

export function credentialIdsFromService(service: LabBubbleConnectedService): readonly string[] {
  const rawIds = service.evidence_summary?.credential_ids;
  return Array.isArray(rawIds) ? rawIds.map((id) => String(id)) : [];
}

export function vmAssetLabel(asset: LabBubbleAsset, vm?: VmFleetRow | null): string {
  const vmid = typeof asset.vmid === "number" ? asset.vmid : vm?.vmid;
  if (vm?.name && typeof vmid === "number") {
    return `${vm.name} (VM ${String(vmid)})`;
  }
  if (typeof vmid === "number") {
    return `VM ${String(vmid)}`;
  }
  return fallbackText(asset.agent_id ?? asset.id);
}

export type BubbleFormValues = {
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

const BUBBLE_LIFECYCLE_OPTIONS = [
  { value: "planned", label: "Planned" },
  { value: "building", label: "Building" },
  { value: "ready", label: "Ready" },
  { value: "active", label: "Active" },
  { value: "draining", label: "Draining" },
  { value: "retired", label: "Retired" }
] as const;

const BUBBLE_ISOLATION_OPTIONS = [
  { value: "planned", label: "Planned" },
  { value: "provisioning", label: "Provisioning" },
  { value: "ready", label: "Ready" },
  { value: "isolated", label: "Isolated" },
  { value: "verified", label: "Verified" },
  { value: "breached", label: "Breached" },
  { value: "open", label: "Open" }
] as const;

export function bubbleFormFromBubble(bubble: LabBubble): BubbleFormValues {
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

export function bubbleFormPayload(values: BubbleFormValues): Readonly<Record<string, unknown>> {
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

export function parseDhcpRange(value: string | undefined): { start: string; end: string } {
  if (!value) {
    return { start: "", end: "" };
  }
  let start = "";
  let end = "";
  for (const part of value.split(",")) {
    const [key, raw] = part.split("=", 2);
    if (!key || !raw) {
      continue;
    }
    const trimmed = raw.trim();
    if (key.trim().toLowerCase() === "start-address") {
      start = trimmed;
    } else if (key.trim().toLowerCase() === "end-address") {
      end = trimmed;
    }
  }
  return { start, end };
}

export type InfraDraft = {
  readonly vmid: string;
  readonly bubbleId: string;
  readonly role: string;
  readonly notes: string;
};

export type InfraEditDraft = {
  readonly assetId: string;
  readonly role: string;
  readonly notes: string;
};

export type InfraMoveDraft = {
  readonly assetId: string;
  readonly bubbleId: string;
};

export type ServiceDraftMode = "create" | "edit";

export type ServiceDraft = {
  readonly bubbleId: string;
  readonly serviceKind: string;
  readonly serviceName: string;
  readonly scope: string;
  readonly providerAssetId: string;
  readonly readinessState: string;
  readonly credentialIds: readonly string[];
};

const infraRoleOptions = [
  "domain_controller",
  "dhcp_server",
  "dns_server",
  "configmgr",
  "file_server",
  "firewall_router",
  "management_server",
  "other"
] as const;

const serviceKindOptions = [
  "ad_ds",
  "dns",
  "dhcp",
  "entra",
  "configmgr",
  "file_service",
  "identity",
  "other"
] as const;

const serviceScopeOptions = ["bubble_local", "external", "shared"] as const;
const serviceReadinessOptions = ["unknown", "planned", "provisioning", "ready", "degraded"] as const;

export const blankInfraDraft: InfraDraft = {
  vmid: "",
  bubbleId: "",
  role: "domain_controller",
  notes: ""
};

export const blankServiceDraft: ServiceDraft = {
  bubbleId: "",
  serviceKind: "ad_ds",
  serviceName: "",
  scope: "bubble_local",
  providerAssetId: "",
  readinessState: "unknown",
  credentialIds: []
};

export async function deleteJson(path: string): Promise<void> {
  const response = await fetch(path, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { accept: "application/json" }
  });
  if (!response.ok) {
    throw new Error(`DELETE ${path} failed: ${response.statusText || String(response.status)}`);
  }
}

function readinessClass(ok: unknown): string {
  if (ok === true) {
    return "status";
  }
  return ok === false ? "status status--bad" : "status status--warn";
}

function readinessLabel(ok: unknown): string {
  if (ok === true) {
    return "ready";
  }
  return ok === false ? "blocked" : "unknown";
}

export function roleLabel(value: string | undefined): string {
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
  onCancel,
  orphanVnets,
  adoptedVnetId,
  onAdoptVnet,
  boundNetwork
}: {
  readonly mode: BubbleDraftMode;
  readonly bubbleName?: string;
  readonly values: BubbleFormValues;
  readonly onChange: (field: BubbleFormField, value: string) => void;
  readonly onSave: () => void;
  readonly onCancel: () => void;
  readonly orphanVnets?: readonly OrphanVnet[];
  readonly adoptedVnetId?: string | undefined;
  readonly onAdoptVnet?: (vnetId: string) => void;
  readonly boundNetwork?: BubbleBoundNetwork | null;
}) {
  const saveLabel = mode === "create" ? "Create bubble" : `Save bubble ${bubbleName ?? values.name}`;
  const showAdoption = mode === "create" && orphanVnets !== undefined && onAdoptVnet !== undefined;
  const adoptionVnets = orphanVnets ?? [];
  const hasOrphans = adoptionVnets.length > 0;
  return (
    <form
      className="bubble-form"
      aria-label={mode === "create" ? "Create bubble" : `Edit bubble ${bubbleName ?? values.name}`}
      onSubmit={(event) => {
        event.preventDefault();
        onSave();
      }}
    >
      {showAdoption ? (
        hasOrphans ? (
          <label className="bubble-form-field bubble-form-adoption">
            <span>Adopt existing isolated network</span>
            <select
              aria-label="Adopt existing isolated network"
              value={adoptedVnetId ?? ""}
              onChange={(event) => {
                onAdoptVnet(event.currentTarget.value);
              }}
            >
              <option value="">- create a new bubble without SDN binding -</option>
              {adoptionVnets.map((vnet) => {
                const cidrLabel = vnet.subnet?.subnet ? ` (${vnet.subnet.subnet})` : "";
                const zoneLabel = vnet.zone ? ` / zone ${vnet.zone}` : "";
                const aliasLabel = vnet.alias && vnet.alias !== vnet.vnet ? ` - ${vnet.alias}` : "";
                return (
                  <option key={vnet.vnet} value={vnet.vnet}>
                    {vnet.vnet}{zoneLabel}{cidrLabel}{aliasLabel}
                  </option>
                );
              })}
            </select>
            <span className="bubble-form-help">
              Found {adoptionVnets.length} vnet{adoptionVnets.length === 1 ? "" : "s"} not yet
              bound to a bubble. Picking one pre-fills the CIDR/gateway/DHCP fields and binds the new
              bubble to its SDN isolation on save.
            </span>
          </label>
        ) : (
          <p className="bubble-form-note">
            No unbound SDN vnets available. Create a vnet + subnet under the Networks page first if you
            want to adopt an existing isolated network for this bubble.
          </p>
        )
      ) : null}
      <div className="bubble-form-section">
        <h4 className="bubble-form-section__title">Identity</h4>
        <div className="bubble-form-grid">
          <BubbleTextField label="Bubble name" field="name" value={values.name} onChange={onChange} required placeholder="e.g. lab30" />
          <BubbleTextField label="Domain name" field="domain_name" value={values.domain_name} onChange={onChange} placeholder="lab30.example.test" />
          <BubbleTextField label="NetBIOS name" field="netbios_name" value={values.netbios_name} onChange={onChange} placeholder="LAB30" />
        </div>
      </div>

      <div className="bubble-form-section">
        <h4 className="bubble-form-section__title">Network</h4>
        {boundNetwork ? (
          <>
            <p className="bubble-form-note">
              This bubble is bound to SDN vnet <code>{boundNetwork.vnet}</code>{" "}
              (zone <code>{boundNetwork.zone}</code>). Network details are
              pulled from the live PVE subnet config; edit them on the
              Networks page so the rest of the cluster sees the change too.
            </p>
            <dl className="bubble-network-readonly">
              <div><dt>Isolated CIDR</dt><dd>{boundNetwork.subnet || "-"}</dd></div>
              <div><dt>Gateway IP</dt><dd>{boundNetwork.gateway || "-"}</dd></div>
              <div><dt>DHCP network ID</dt><dd>{boundNetwork.vnet}</dd></div>
              <div><dt>DHCP DNS server</dt><dd>{boundNetwork.dhcpDnsServer || "-"}</dd></div>
              <div><dt>DHCP pool start</dt><dd>{boundNetwork.dhcpStart || "-"}</dd></div>
              <div><dt>DHCP pool end</dt><dd>{boundNetwork.dhcpEnd || "-"}</dd></div>
            </dl>
          </>
        ) : (
          <div className="bubble-form-grid">
            <BubbleTextField label="Isolated CIDR" field="cidr" value={values.cidr} onChange={onChange} placeholder="10.77.30.0/24" />
            <BubbleTextField label="Gateway IP" field="gateway_ip" value={values.gateway_ip} onChange={onChange} placeholder="10.77.30.1" />
            <BubbleTextField label="DHCP network ID" field="dhcp_scope" value={values.dhcp_scope} onChange={onChange} placeholder="vnet alias / scope" />
            <BubbleTextField label="DHCP pool start" field="dhcp_pool_start" value={values.dhcp_pool_start} onChange={onChange} placeholder="10.77.30.100" />
            <BubbleTextField label="DHCP pool end" field="dhcp_pool_end" value={values.dhcp_pool_end} onChange={onChange} placeholder="10.77.30.199" />
          </div>
        )}
      </div>

      <div className="bubble-form-section">
        <h4 className="bubble-form-section__title">Lifecycle</h4>
        <div className="bubble-form-grid">
          <BubbleSelectField
            label="Lifecycle state"
            field="lifecycle_state"
            value={values.lifecycle_state}
            onChange={onChange}
            options={BUBBLE_LIFECYCLE_OPTIONS}
          />
          <BubbleSelectField
            label="Isolation status"
            field="isolation_status"
            value={values.isolation_status}
            onChange={onChange}
            options={BUBBLE_ISOLATION_OPTIONS}
          />
        </div>
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
  required = false,
  placeholder
}: {
  readonly label: string;
  readonly field: BubbleFormField;
  readonly value: string;
  readonly onChange: (field: BubbleFormField, value: string) => void;
  readonly required?: boolean;
  readonly placeholder?: string | undefined;
}) {
  return (
    <label className="bubble-form-field">
      <span>{label}</span>
      <input
        aria-label={label}
        value={value}
        required={required}
        placeholder={placeholder}
        onChange={(event) => { onChange(field, event.target.value); }}
      />
    </label>
  );
}

function BubbleSelectField({
  label,
  field,
  value,
  onChange,
  options
}: {
  readonly label: string;
  readonly field: BubbleFormField;
  readonly value: string;
  readonly onChange: (field: BubbleFormField, value: string) => void;
  readonly options: readonly { readonly value: string; readonly label: string }[];
}) {
  // If the saved value isn't one of the canonical options (free-text from
  // an older bubble or an external tool), keep it visible at the top of the
  // list so editing doesn't silently rewrite history.
  const knownValues = new Set(options.map((opt) => opt.value));
  const showCustomFirst = value && !knownValues.has(value);
  return (
    <label className="bubble-form-field">
      <span>{label}</span>
      <select
        aria-label={label}
        value={value}
        onChange={(event) => { onChange(field, event.target.value); }}
      >
        {showCustomFirst ? <option value={value}>{value} (custom)</option> : null}
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>{opt.label}</option>
        ))}
      </select>
    </label>
  );
}

export function BubbleTopologyOverview({
  topology,
  infraVmCandidates,
  credentials,
  onCreateBubble,
  onEditBubble,
  onRequestDeleteBubble,
  onConfirmDeleteBubble,
  onCancelDeleteBubble,
  bubbleDraftMode,
  bubbleDraftId,
  bubbleDraft,
  orphanVnets,
  adoptedVnetId,
  onAdoptVnet,
  boundNetwork,
  onBubbleDraftChange,
  onSaveBubbleDraft,
  onCancelBubbleDraft,
  deleteBubbleId,
  infraDraftOpen,
  infraDraft,
  infraEditDraft,
  infraMoveDraft,
  retireInfraId,
  onStartInfraDraft,
  onInfraDraftChange,
  onSaveInfraDraft,
  onCancelInfraDraft,
  onEditInfra,
  onInfraEditDraftChange,
  onSaveInfraEdit,
  onCancelInfraEdit,
  onStartInfraMove,
  onInfraMoveDraftChange,
  onConfirmInfraMove,
  onCancelInfraMove,
  onRequestRetireInfra,
  onConfirmRetireInfra,
  onCancelRetireInfra,
  onApproveAgent,
  serviceDraftMode,
  serviceDraftId,
  serviceDraft,
  deleteServiceId,
  onStartServiceDraft,
  onEditService,
  onServiceDraftChange,
  onSaveServiceDraft,
  onCancelServiceDraft,
  onRequestDeleteService,
  onConfirmDeleteService,
  onCancelDeleteService
}: {
  readonly topology: LabBubbleTopology;
  readonly infraVmCandidates: readonly VmFleetRow[];
  readonly credentials: readonly CredentialSummary[];
  readonly onCreateBubble: () => void;
  readonly onEditBubble: (bubble: LabBubble) => void;
  readonly onRequestDeleteBubble: (bubble: LabBubble) => void;
  readonly onConfirmDeleteBubble: (bubble: LabBubble) => void;
  readonly onCancelDeleteBubble: () => void;
  readonly bubbleDraftMode: BubbleDraftMode | null;
  readonly bubbleDraftId: string | null;
  readonly bubbleDraft: BubbleFormValues;
  readonly orphanVnets: readonly OrphanVnet[];
  readonly adoptedVnetId: string | undefined;
  readonly onAdoptVnet: (vnetId: string) => void;
  readonly boundNetwork: BubbleBoundNetwork | null;
  readonly onBubbleDraftChange: (field: BubbleFormField, value: string) => void;
  readonly onSaveBubbleDraft: () => void;
  readonly onCancelBubbleDraft: () => void;
  readonly deleteBubbleId: string | null;
  readonly infraDraftOpen: boolean;
  readonly infraDraft: InfraDraft;
  readonly infraEditDraft: InfraEditDraft | null;
  readonly infraMoveDraft: InfraMoveDraft | null;
  readonly retireInfraId: string | null;
  readonly onStartInfraDraft: () => void;
  readonly onInfraDraftChange: (field: keyof InfraDraft, value: string) => void;
  readonly onSaveInfraDraft: () => void;
  readonly onCancelInfraDraft: () => void;
  readonly onEditInfra: (node: LabBubbleInfrastructureNode) => void;
  readonly onInfraEditDraftChange: (field: "role" | "notes", value: string) => void;
  readonly onSaveInfraEdit: (node: LabBubbleInfrastructureNode) => void;
  readonly onCancelInfraEdit: () => void;
  readonly onStartInfraMove: (node: LabBubbleInfrastructureNode) => void;
  readonly onInfraMoveDraftChange: (bubbleId: string) => void;
  readonly onConfirmInfraMove: (node: LabBubbleInfrastructureNode) => void;
  readonly onCancelInfraMove: () => void;
  readonly onRequestRetireInfra: (node: LabBubbleInfrastructureNode) => void;
  readonly onConfirmRetireInfra: (node: LabBubbleInfrastructureNode) => void;
  readonly onCancelRetireInfra: () => void;
  readonly onApproveAgent: (agent: AgentFleetRow) => void;
  readonly serviceDraftMode: ServiceDraftMode | null;
  readonly serviceDraftId: string | null;
  readonly serviceDraft: ServiceDraft;
  readonly deleteServiceId: string | null;
  readonly onStartServiceDraft: () => void;
  readonly onEditService: (service: LabBubbleConnectedService) => void;
  readonly onServiceDraftChange: (field: keyof ServiceDraft, value: string | readonly string[]) => void;
  readonly onSaveServiceDraft: () => void;
  readonly onCancelServiceDraft: () => void;
  readonly onRequestDeleteService: (service: LabBubbleConnectedService) => void;
  readonly onConfirmDeleteService: (service: LabBubbleConnectedService) => void;
  readonly onCancelDeleteService: () => void;
}) {
  const fleets = topology.workstation_fleets;
  const infra = topology.critical_infrastructure;
  const services = topology.connected_services;
  const bubbleOptions = topologyBubbles(topology);
  const assetOptions = topologyAssets(topology);
  const providerById = new Map(assetOptions.map((item) => [item.asset.id, item]));
  const credentialById = new Map(credentials.map((credential) => [credential.id, credential]));
  const gateByBubble = new Map(topology.gate_states.map((gate) => [gate.bubble_id, gate]));
  return (
    <section className="bubble-layout" aria-label="Tenant bubbles">
      <div className="bubble-primary-stack">
        <Panel
          title="VM Workstation Fleets"
          action={(
            <button type="button" className="fleet-action fleet-action--command" onClick={onCreateBubble}>
              <span>New bubble</span>
            </button>
          )}
        >
          {bubbleDraftMode === "create" ? (
            <BubbleEditor
              mode="create"
              values={bubbleDraft}
              onChange={onBubbleDraftChange}
              onSave={onSaveBubbleDraft}
              onCancel={onCancelBubbleDraft}
              orphanVnets={orphanVnets}
              adoptedVnetId={adoptedVnetId}
              onAdoptVnet={onAdoptVnet}
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
                        boundNetwork={boundNetwork}
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
        <Panel
          title="Critical Infrastructure"
          action={(
            <button type="button" className="fleet-action fleet-action--command" onClick={onStartInfraDraft}>
              <span>Add infra VM</span>
            </button>
          )}
        >
          {infraDraftOpen ? (
            <InfraDraftEditor
              values={infraDraft}
              bubbleOptions={bubbleOptions}
              candidateVms={infraVmCandidates}
              onChange={onInfraDraftChange}
              onSave={onSaveInfraDraft}
              onCancel={onCancelInfraDraft}
            />
          ) : null}
          {infra.length ? (
            <div className="fleet-card-list fleet-card-list--compact">
              {infra.map((node) => {
                const assetLabel = vmAssetLabel(node.asset, node.vm);
                const actionLabel = node.vm?.name ?? assetLabel;
                return (
                  <article key={node.asset.id} className="fleet-card">
                    <header>
                      <div>
                        <span className="status">{node.bubble.name}</span>
                        <h3>{roleLabel(node.role)}</h3>
                      </div>
                      <strong>{assetLabel}</strong>
                    </header>
                    <dl className="fleet-detail-grid">
                      <div><dt>State</dt><dd>{fallbackText(node.asset.membership_state)}</dd></div>
                      <div><dt>Evidence</dt><dd>{fallbackText(node.asset.evidence_state)}</dd></div>
                      <div><dt>Agent</dt><dd>{fallbackText(node.agent?.agent_id ?? node.asset.agent_id)}</dd></div>
                      <div><dt>Runtime</dt><dd>{fallbackText(node.vm?.status)}</dd></div>
                    </dl>
                    {node.agent ? (
                      <div className="chip-row">
                        {node.agent.upgrade_available ? <span className="status status--bad">Upgrade available</span> : null}
                        {node.agent.needs_pairing ? <span className="status status--active">Approved</span> : null}
                      </div>
                    ) : null}
                    <div className="bubble-card-actions bubble-card-actions--left">
                      {node.agent?.approval_status === "pending" && node.agent.approval_id ? (
                        <button type="button" className="fleet-action" aria-label={`Approve agent ${node.agent.agent_id}`} onClick={() => {
                          if (node.agent) {
                            onApproveAgent(node.agent);
                          }
                        }}>
                          Approve agent
                        </button>
                      ) : null}
                      <button type="button" className="fleet-action" aria-label={`Edit infra ${actionLabel}`} onClick={() => { onEditInfra(node); }}>
                        Edit
                      </button>
                      <button type="button" className="fleet-action" aria-label={`Move infra ${actionLabel}`} onClick={() => { onStartInfraMove(node); }}>
                        Move
                      </button>
                      <button type="button" className="fleet-action fleet-action--danger" aria-label={`Retire infra ${actionLabel}`} onClick={() => { onRequestRetireInfra(node); }}>
                        Retire
                      </button>
                    </div>
                    {infraEditDraft?.assetId === node.asset.id ? (
                      <InfraEditEditor
                        assetLabel={actionLabel}
                        values={infraEditDraft}
                        onChange={onInfraEditDraftChange}
                        onSave={() => { onSaveInfraEdit(node); }}
                        onCancel={onCancelInfraEdit}
                      />
                    ) : null}
                    {infraMoveDraft?.assetId === node.asset.id ? (
                      <InfraMoveEditor
                        assetLabel={actionLabel}
                        values={infraMoveDraft}
                        bubbleOptions={bubbleOptions}
                        onChange={onInfraMoveDraftChange}
                        onConfirm={() => { onConfirmInfraMove(node); }}
                        onCancel={onCancelInfraMove}
                      />
                    ) : null}
                    {retireInfraId === node.asset.id ? (
                      <div className="bubble-delete-confirm" role="group" aria-label={`Retire ${actionLabel}`}>
                        <strong>Retire {actionLabel}?</strong>
                        <button type="button" className="fleet-action fleet-action--danger" aria-label={`Confirm retire infra ${actionLabel}`} onClick={() => { onConfirmRetireInfra(node); }}>
                          Confirm
                        </button>
                        <button type="button" className="fleet-action" onClick={onCancelRetireInfra}>
                          Cancel
                        </button>
                      </div>
                    ) : null}
                  </article>
                );
              })}
            </div>
          ) : <p className="empty">No infrastructure assets tagged yet.</p>}
        </Panel>
        <Panel
          title="Connected Services"
          action={(
            <button type="button" className="fleet-action fleet-action--command" onClick={onStartServiceDraft}>
              <span>Add service</span>
            </button>
          )}
        >
          {serviceDraftMode === "create" ? (
            <ServiceEditor
              mode="create"
              values={serviceDraft}
              bubbleOptions={bubbleOptions}
              assetOptions={assetOptions}
              credentials={credentials}
              onChange={onServiceDraftChange}
              onSave={onSaveServiceDraft}
              onCancel={onCancelServiceDraft}
            />
          ) : null}
          {services.length ? (
            <div className="fleet-card-list fleet-card-list--compact">
              {services.map((service) => {
                const provider = service.provider_asset_id ? providerById.get(service.provider_asset_id) : undefined;
                const serviceCredentials = credentialIdsFromService(service)
                  .map((id) => credentialById.get(Number.parseInt(id, 10)))
                  .filter((credential): credential is CredentialSummary => Boolean(credential));
                return (
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
                      <div><dt>Provider</dt><dd>{provider ? vmAssetLabel(provider.asset, provider.vm) : fallbackText(service.provider_asset_id)}</dd></div>
                      <div><dt>Credentials</dt><dd>{serviceCredentials.length ? serviceCredentials.map((credential) => credential.name).join(", ") : "-"}</dd></div>
                    </dl>
                    <div className="bubble-card-actions bubble-card-actions--left">
                      <button type="button" className="fleet-action" aria-label={`Edit service ${service.service_name}`} onClick={() => { onEditService(service); }}>
                        Edit
                      </button>
                      <button type="button" className="fleet-action fleet-action--danger" aria-label={`Delete service ${service.service_name}`} onClick={() => { onRequestDeleteService(service); }}>
                        Delete
                      </button>
                    </div>
                    {serviceDraftMode === "edit" && serviceDraftId === service.id ? (
                      <ServiceEditor
                        mode="edit"
                        values={serviceDraft}
                        bubbleOptions={bubbleOptions}
                        assetOptions={assetOptions}
                        credentials={credentials}
                        onChange={onServiceDraftChange}
                        onSave={onSaveServiceDraft}
                        onCancel={onCancelServiceDraft}
                      />
                    ) : null}
                    {deleteServiceId === service.id ? (
                      <div className="bubble-delete-confirm" role="group" aria-label={`Delete ${service.service_name}`}>
                        <strong>Delete {service.service_name}?</strong>
                        <button type="button" className="fleet-action fleet-action--danger" aria-label={`Confirm delete service ${service.service_name}`} onClick={() => { onConfirmDeleteService(service); }}>
                          Confirm
                        </button>
                        <button type="button" className="fleet-action" onClick={onCancelDeleteService}>
                          Cancel
                        </button>
                      </div>
                    ) : null}
                  </article>
                );
              })}
            </div>
          ) : <p className="empty">No connected services linked yet.</p>}
          {/* Credential CRUD was settings-grade work nested two levels inside
              a panel about services. It has its own registered route. */}
          <p className="empty">
            Credentials live in <a href="/react/credentials">Credentials</a>.
          </p>
        </Panel>
      </div>
    </section>
  );
}

function InfraDraftEditor({
  values,
  bubbleOptions,
  candidateVms,
  onChange,
  onSave,
  onCancel
}: {
  readonly values: InfraDraft;
  readonly bubbleOptions: readonly LabBubble[];
  readonly candidateVms: readonly VmFleetRow[];
  readonly onChange: (field: keyof InfraDraft, value: string) => void;
  readonly onSave: () => void;
  readonly onCancel: () => void;
}) {
  return (
    <form
      className="bubble-form"
      aria-label="Add critical infrastructure"
      onSubmit={(event) => {
        event.preventDefault();
        onSave();
      }}
    >
      <div className="bubble-form-grid">
        <label className="bubble-form-field">
          <span>Bubble</span>
          <select aria-label="Critical infrastructure bubble" value={values.bubbleId} onChange={(event) => { onChange("bubbleId", event.target.value); }}>
            {bubbleOptions.map((bubble) => (
              <option key={bubble.id} value={bubble.id}>{bubble.name}</option>
            ))}
          </select>
        </label>
        <label className="bubble-form-field">
          <span>VM</span>
          <select aria-label="Critical infrastructure VM" value={values.vmid} onChange={(event) => { onChange("vmid", event.target.value); }}>
            <option value="">Select VM</option>
            {candidateVms.map((vm) => (
              <option key={vm.vmid} value={String(vm.vmid)}>{vm.name} / VM {String(vm.vmid)} / {fallbackText(vm.status)}</option>
            ))}
          </select>
        </label>
        <label className="bubble-form-field">
          <span>Role</span>
          <select aria-label="Critical infrastructure role" value={values.role} onChange={(event) => { onChange("role", event.target.value); }}>
            {infraRoleOptions.map((role) => (
              <option key={role} value={role}>{roleLabel(role)}</option>
            ))}
          </select>
        </label>
        <label className="bubble-form-field">
          <span>Notes</span>
          <input aria-label="Critical infrastructure notes" value={values.notes} onChange={(event) => { onChange("notes", event.target.value); }} />
        </label>
      </div>
      <div className="bubble-form-actions">
        <button type="submit" className="fleet-action fleet-action--command">Save critical infrastructure</button>
        <button type="button" className="fleet-action" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

function InfraEditEditor({
  assetLabel,
  values,
  onChange,
  onSave,
  onCancel
}: {
  readonly assetLabel: string;
  readonly values: InfraEditDraft;
  readonly onChange: (field: "role" | "notes", value: string) => void;
  readonly onSave: () => void;
  readonly onCancel: () => void;
}) {
  return (
    <form
      className="bubble-form"
      aria-label={`Edit infra ${assetLabel}`}
      onSubmit={(event) => {
        event.preventDefault();
        onSave();
      }}
    >
      <div className="bubble-form-grid">
        <label className="bubble-form-field">
          <span>Role</span>
          <select aria-label={`Role for infra ${assetLabel}`} value={values.role} onChange={(event) => { onChange("role", event.target.value); }}>
            {infraRoleOptions.map((role) => (
              <option key={role} value={role}>{roleLabel(role)}</option>
            ))}
          </select>
        </label>
        <label className="bubble-form-field">
          <span>Notes</span>
          <input aria-label={`Notes for infra ${assetLabel}`} value={values.notes} onChange={(event) => { onChange("notes", event.target.value); }} />
        </label>
      </div>
      <div className="bubble-form-actions">
        <button type="submit" className="fleet-action fleet-action--command" aria-label={`Save infra ${assetLabel}`}>Save</button>
        <button type="button" className="fleet-action" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

function InfraMoveEditor({
  assetLabel,
  values,
  bubbleOptions,
  onChange,
  onConfirm,
  onCancel
}: {
  readonly assetLabel: string;
  readonly values: InfraMoveDraft;
  readonly bubbleOptions: readonly LabBubble[];
  readonly onChange: (bubbleId: string) => void;
  readonly onConfirm: () => void;
  readonly onCancel: () => void;
}) {
  return (
    <div className="bubble-delete-confirm" role="group" aria-label={`Move ${assetLabel}`}>
      <label className="bubble-form-field">
        <span>Target bubble</span>
        <select aria-label={`Move bubble for infra ${assetLabel}`} value={values.bubbleId} onChange={(event) => { onChange(event.target.value); }}>
          {bubbleOptions.map((bubble) => (
            <option key={bubble.id} value={bubble.id}>{bubble.name}</option>
          ))}
        </select>
      </label>
      <button type="button" className="fleet-action fleet-action--command" aria-label={`Confirm move infra ${assetLabel}`} onClick={onConfirm}>
        Move
      </button>
      <button type="button" className="fleet-action" onClick={onCancel}>
        Cancel
      </button>
    </div>
  );
}

function ServiceEditor({
  mode,
  values,
  bubbleOptions,
  assetOptions,
  credentials,
  onChange,
  onSave,
  onCancel
}: {
  readonly mode: ServiceDraftMode;
  readonly values: ServiceDraft;
  readonly bubbleOptions: readonly LabBubble[];
  readonly assetOptions: readonly { readonly bubble: LabBubble; readonly asset: LabBubbleAsset; readonly vm: VmFleetRow | null | undefined }[];
  readonly credentials: readonly CredentialSummary[];
  readonly onChange: (field: keyof ServiceDraft, value: string | readonly string[]) => void;
  readonly onSave: () => void;
  readonly onCancel: () => void;
}) {
  const providers = assetOptions.filter((item) => item.bubble.id === values.bubbleId);
  return (
    <form
      className="bubble-form"
      aria-label={mode === "create" ? "Add connected service" : "Edit connected service"}
      onSubmit={(event) => {
        event.preventDefault();
        onSave();
      }}
    >
      <div className="bubble-form-grid">
        <label className="bubble-form-field">
          <span>Bubble</span>
          <select
            aria-label="Service bubble"
            value={values.bubbleId}
            disabled={mode === "edit"}
            onChange={(event) => { onChange("bubbleId", event.target.value); }}
          >
            {bubbleOptions.map((bubble) => (
              <option key={bubble.id} value={bubble.id}>{bubble.name}</option>
            ))}
          </select>
        </label>
        <label className="bubble-form-field">
          <span>Kind</span>
          <select aria-label="Service kind" value={values.serviceKind} onChange={(event) => { onChange("serviceKind", event.target.value); }}>
            {serviceKindOptions.map((kind) => (
              <option key={kind} value={kind}>{roleLabel(kind)}</option>
            ))}
          </select>
        </label>
        <label className="bubble-form-field">
          <span>Name</span>
          <input aria-label="Service name" value={values.serviceName} onChange={(event) => { onChange("serviceName", event.target.value); }} />
        </label>
        <label className="bubble-form-field">
          <span>Scope</span>
          <select aria-label="Service scope" value={values.scope} onChange={(event) => { onChange("scope", event.target.value); }}>
            {serviceScopeOptions.map((scope) => (
              <option key={scope} value={scope}>{roleLabel(scope)}</option>
            ))}
          </select>
        </label>
        <label className="bubble-form-field">
          <span>Provider</span>
          <select aria-label="Provider asset" value={values.providerAssetId} onChange={(event) => { onChange("providerAssetId", event.target.value); }}>
            <option value="">No provider</option>
            {providers.map((item) => (
              <option key={item.asset.id} value={item.asset.id}>{vmAssetLabel(item.asset, item.vm)} / {roleLabel(item.asset.asset_role)}</option>
            ))}
          </select>
        </label>
        <label className="bubble-form-field">
          <span>Readiness</span>
          <select aria-label="Readiness state" value={values.readinessState} onChange={(event) => { onChange("readinessState", event.target.value); }}>
            {serviceReadinessOptions.map((state) => (
              <option key={state} value={state}>{roleLabel(state)}</option>
            ))}
          </select>
        </label>
        <label className="bubble-form-field bubble-form-field--wide">
          <span>Credentials</span>
          <select
            aria-label="Service credentials"
            multiple
            value={[...values.credentialIds]}
            onChange={(event) => {
              const selected = Array.from(event.currentTarget.selectedOptions).map((option) => option.value);
              onChange("credentialIds", selected.length ? selected : event.currentTarget.value ? [event.currentTarget.value] : []);
            }}
          >
            {credentials.map((credential) => (
              <option key={credential.id} value={String(credential.id)}>{credential.name} / {roleLabel(credential.type)}</option>
            ))}
          </select>
        </label>
      </div>
      <div className="bubble-form-actions">
        <button type="submit" className="fleet-action fleet-action--command">{mode === "create" ? "Create connected service" : "Save connected service"}</button>
        <button type="button" className="fleet-action" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}
