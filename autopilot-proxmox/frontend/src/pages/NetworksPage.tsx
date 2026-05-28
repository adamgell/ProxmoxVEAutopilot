import { useCallback, useMemo, useState } from "react";

import { deleteJson, fetchJson, patchJson, postJson } from "../apiClient";
import { SdnInlineForm } from "../components/SdnInlineForm";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import {
  bodyFromValues,
  cidrHostPrefix,
  controllerSchema,
  dnsSchema,
  ipamSchema,
  sdnSchemas,
  subnetSchema,
  vnetSchema,
  zoneSchema,
  type SdnKindKey,
  type SdnKindSchema
} from "../networksSchema";
import { textValue } from "../utilityModels";

interface SdnTarget {
  readonly kind: SdnKindKey;
  readonly id: string;
  readonly parent?: string | undefined;
}

type NetworksTab = "overview" | "zones" | "vnets" | "subnets" | "controllers" | "ipam" | "dns" | "firewall" | "pending";

interface SdnObject {
  readonly id?: string;
  readonly zone?: string;
  readonly vnet?: string;
  readonly subnet?: string;
  readonly type?: string;
  readonly gateway?: string;
  readonly snat?: boolean;
  readonly [key: string]: unknown;
}

interface SdnInventory {
  readonly zones?: readonly SdnObject[];
  readonly vnets?: readonly SdnObject[];
  readonly subnets_by_vnet?: Readonly<Record<string, readonly SdnObject[]>>;
  readonly controllers?: readonly SdnObject[];
  readonly ipams?: readonly SdnObject[];
  readonly dns?: readonly SdnObject[];
  readonly fabrics?: readonly SdnObject[];
}

interface FirewallInventory {
  readonly cluster?: {
    readonly options?: Readonly<Record<string, unknown>>;
    readonly rules?: readonly SdnObject[];
  };
  readonly nodes?: Readonly<Record<string, unknown>>;
  readonly vnets?: Readonly<Record<string, unknown>>;
  readonly vms?: Readonly<Record<string, unknown>>;
}

interface NetworksPayload {
  readonly sdn?: SdnInventory;
  readonly firewall?: FirewallInventory;
  readonly labs?: readonly unknown[];
}

interface SdnLabForm {
  readonly name: string;
  readonly zone: string;
  readonly vnet: string;
  readonly subnet: string;
  readonly domain_name: string;
  readonly cidr: string;
  readonly gateway_ip: string;
}

interface SdnLabPreflight {
  readonly ok?: boolean;
  readonly blocking?: readonly unknown[];
  readonly warnings?: readonly unknown[];
}

const emptyPayload: NetworksPayload = {
  sdn: {
    zones: [],
    vnets: [],
    subnets_by_vnet: {},
    controllers: [],
    ipams: [],
    dns: [],
    fabrics: []
  },
  firewall: { cluster: { options: {}, rules: [] }, nodes: {}, vnets: {}, vms: {} },
  labs: []
};

const tabs: readonly { readonly id: NetworksTab; readonly label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "zones", label: "Zones" },
  { id: "vnets", label: "VNets" },
  { id: "subnets", label: "Subnets" },
  { id: "controllers", label: "Controllers" },
  { id: "ipam", label: "IPAM" },
  { id: "dns", label: "DNS" },
  { id: "firewall", label: "Firewall" },
  { id: "pending", label: "Pending Apply" }
];

function asArray(value: readonly SdnObject[] | undefined): readonly SdnObject[] {
  return Array.isArray(value) ? value : [];
}

function objectId(item: SdnObject): string {
  return textValue(item.id ?? item.vnet ?? item.zone ?? item.subnet, "-");
}

function objectType(item: SdnObject): string {
  return textValue(item.type, "-");
}

function formObjectId(item: SdnObject | undefined, ...keys: readonly (keyof SdnObject)[]): string {
  if (!item) {
    return "";
  }
  for (const key of ["id", ...keys] as readonly (keyof SdnObject)[]) {
    const value = textValue(item[key], "");
    if (value) {
      return value;
    }
  }
  return "";
}

function subnets(payload: NetworksPayload): readonly (SdnObject & { readonly parentVnet: string })[] {
  const byVnet = payload.sdn?.subnets_by_vnet ?? {};
  return Object.entries(byVnet).flatMap(([parentVnet, rows]) =>
    asArray(rows).map((row) => ({ ...row, parentVnet }))
  );
}

interface RowActions {
  readonly kind: SdnKindKey;
  readonly canEdit: boolean;
  readonly editingId?: string | undefined;
  readonly editingParent?: string | undefined;
  readonly busy?: string | undefined;
  readonly onEdit: (id: string, parent?: string) => void;
  readonly onCancelEdit: () => void;
  readonly onDelete: (id: string, parent?: string) => void;
  readonly onSubmitEdit: (id: string, values: Readonly<Record<string, string>>, parent?: string) => void;
  readonly mutationError?: string | undefined;
}

function rowActionLabels(kind: SdnKindKey) {
  return sdnSchemas[kind].singular;
}

function ActionsCell({
  id,
  parent,
  actions
}: {
  readonly id: string;
  readonly parent?: string | undefined;
  readonly actions?: RowActions | undefined;
}) {
  if (!actions) {
    return null;
  }
  const busyKey = parent ? `${actions.kind}:${parent}/${id}` : `${actions.kind}:${id}`;
  const isBusy = actions.busy === busyKey;
  const label = rowActionLabels(actions.kind);
  return (
    <td className="networks-actions">
      {actions.canEdit ? (
        <button
          type="button"
          className="networks-row-action"
          onClick={() => {
            actions.onEdit(id, parent);
          }}
          disabled={isBusy}
          aria-label={`Edit ${label} ${id}`}
        >
          Edit
        </button>
      ) : null}
      <button
        type="button"
        className="networks-row-action networks-row-action--danger"
        onClick={() => {
          actions.onDelete(id, parent);
        }}
        disabled={isBusy}
        aria-label={`Delete ${label} ${id}`}
      >
        {isBusy ? "..." : "Delete"}
      </button>
    </td>
  );
}

function ObjectTable({
  rows,
  kind,
  actions
}: {
  readonly rows: readonly SdnObject[];
  readonly kind: string;
  readonly actions?: RowActions | undefined;
}) {
  if (!rows.length) {
    return <p className="empty">No {kind} found.</p>;
  }
  return (
    <div className="table-wrap">
      <table className="jobs-table cloudosd-table networks-table">
        <thead>
          <tr>
            <th scope="col">Name</th>
            <th scope="col">Type</th>
            <th scope="col">Zone</th>
            <th scope="col">Detail</th>
            {actions ? <th scope="col" aria-label="actions" className="networks-actions-col" /> : null}
          </tr>
        </thead>
        <tbody>
          {rows.flatMap((row, index) => {
            const id = objectId(row);
            const editing = Boolean(actions && actions.editingId === id && !actions.editingParent);
            const baseRow = (
              <tr key={`${kind}-${id}-${String(index)}`}>
                <td><code>{id}</code></td>
                <td>{objectType(row)}</td>
                <td>{row.zone ? `zone ${textValue(row.zone)}` : "-"}</td>
                <td>{textValue(row.gateway ?? row.subnet ?? row.vnet, "-")}</td>
                <ActionsCell id={id} actions={actions} />
              </tr>
            );
            if (editing && actions) {
              const schema = sdnSchemas[actions.kind];
              const colSpan = 4 + 1;
              return [
                baseRow,
                (
                  <tr key={`${kind}-${id}-${String(index)}-edit`}>
                    <td colSpan={colSpan} className="networks-edit-cell">
                      <SdnInlineForm
                        mode="edit"
                        title={`Edit ${schema.singular} ${id}`}
                        fields={schema.editFields}
                        initialValues={row as Readonly<Record<string, string>>}
                        busy={actions.busy === `${actions.kind}:${id}`}
                        error={actions.mutationError}
                        onCancel={actions.onCancelEdit}
                        onSubmit={(values) => {
                          actions.onSubmitEdit(id, values);
                        }}
                      />
                    </td>
                  </tr>
                )
              ];
            }
            return [baseRow];
          })}
        </tbody>
      </table>
    </div>
  );
}

function SubnetTable({
  rows,
  actions
}: {
  readonly rows: readonly (SdnObject & { readonly parentVnet: string })[];
  readonly actions?: RowActions | undefined;
}) {
  if (!rows.length) {
    return <p className="empty">No subnets found.</p>;
  }
  return (
    <div className="table-wrap">
      <table className="jobs-table cloudosd-table networks-table">
        <thead>
          <tr>
            <th scope="col">Subnet</th>
            <th scope="col">VNet</th>
            <th scope="col">Gateway</th>
            <th scope="col">SNAT</th>
            {actions ? <th scope="col" aria-label="actions" className="networks-actions-col" /> : null}
          </tr>
        </thead>
        <tbody>
          {rows.flatMap((row) => {
            const id = textValue(row.subnet);
            const editing = Boolean(
              actions && actions.editingId === id && actions.editingParent === row.parentVnet
            );
            const baseRow = (
              <tr key={`${row.parentVnet}-${id}`}>
                <td><code>{id}</code></td>
                <td>{row.parentVnet}</td>
                <td>{textValue(row.gateway, "-")}</td>
                <td>{row.snat ? "enabled" : "not set"}</td>
                <ActionsCell id={id} parent={row.parentVnet} actions={actions} />
              </tr>
            );
            if (editing && actions) {
              return [
                baseRow,
                (
                  <tr key={`${row.parentVnet}-${id}-edit`}>
                    <td colSpan={5} className="networks-edit-cell">
                      <SdnInlineForm
                        mode="edit"
                        title={`Edit subnet ${id}`}
                        fields={subnetSchema.editFields}
                        initialValues={row as Readonly<Record<string, string>>}
                        busy={actions.busy === `${actions.kind}:${row.parentVnet}/${id}`}
                        error={actions.mutationError}
                        onCancel={actions.onCancelEdit}
                        onSubmit={(values) => {
                          actions.onSubmitEdit(id, values, row.parentVnet);
                        }}
                      />
                    </td>
                  </tr>
                )
              ];
            }
            return [baseRow];
          })}
        </tbody>
      </table>
    </div>
  );
}

function CreatePanelAffordance({
  schema,
  open,
  busy,
  error,
  parentOptions,
  onToggle,
  onSubmit
}: {
  readonly schema: SdnKindSchema;
  readonly open: boolean;
  readonly busy: boolean;
  readonly error?: string | undefined;
  readonly parentOptions?: readonly string[] | undefined;
  readonly onToggle: () => void;
  readonly onSubmit: (values: Readonly<Record<string, string>>) => void;
}) {
  if (!open) {
    return (
      <div className="networks-create-toggle">
        <button type="button" className="utility-button" onClick={onToggle}>
          + New {schema.singular}
        </button>
      </div>
    );
  }
  let fields = schema.createFields;
  if (schema.key === "subnet") {
    const parents = parentOptions ?? [];
    fields = [
      {
        name: "parentVnet",
        label: "Under VNet",
        kind: "select",
        required: true,
        options: parents.map((value) => ({ value, label: value }))
      },
      ...fields
    ];
  } else if (schema.key === "vnet") {
    // Replace the free-text zone input with a select of existing zones so
    // operators can't fat-finger a non-existent zone id and trigger an
    // upstream "zone does not exist" 400. parentOptions carries the zone
    // ids the caller already fetched into the inventory.
    const zoneOptions = parentOptions ?? [];
    fields = fields.map((field) =>
      field.name === "zone"
        ? {
            ...field,
            kind: "select" as const,
            options: zoneOptions.map((value) => ({ value, label: value }))
          }
        : field
    );
  }
  return (
    <SdnInlineForm
      mode="create"
      title={`Create ${schema.singular}`}
      fields={fields}
      submitLabel={`Create ${schema.singular}`}
      busy={busy}
      error={error}
      onCancel={onToggle}
      onSubmit={onSubmit}
    />
  );
}

function FirewallSurface({ payload }: { readonly payload: NetworksPayload }) {
  const clusterRules = payload.firewall?.cluster?.rules?.length ?? 0;
  return (
    <div className="networks-scope-grid">
      {[
        ["Cluster", "options, aliases, IP sets, security groups, datacenter rules"],
        ["Node", "host-local options and rules"],
        ["VNet", "forward policy and VNet-specific rules"],
        ["VM", "per-machine rules, aliases, and IP sets"]
      ].map(([label, detail]) => (
        <div className="networks-scope" key={label}>
          <strong>{label}</strong>
          <span>{detail}</span>
        </div>
      ))}
      <p className="networks-note">Cluster firewall rules discovered: {String(clusterRules)}</p>
    </div>
  );
}

export function NetworksPage({
  bootstrap,
  path = "/react/networks"
}: {
  readonly bootstrap: AppBootstrap;
  readonly path?: string;
}) {
  const [payload, setPayload] = useState<NetworksPayload>(emptyPayload);
  const [activeTab, setActiveTab] = useState<NetworksTab>("overview");
  const [error, setError] = useState("");
  const [lockToken, setLockToken] = useState("");
  const [applyStatus, setApplyStatus] = useState("");
  const [labStatus, setLabStatus] = useState("");
  const [labForm, setLabForm] = useState<SdnLabForm>({
    name: "",
    zone: "",
    vnet: "",
    subnet: "",
    domain_name: "",
    cidr: "",
    gateway_ip: ""
  });
  const [editing, setEditing] = useState<SdnTarget | null>(null);
  const [creating, setCreating] = useState<SdnKindKey | null>(null);
  const [busyKey, setBusyKey] = useState<string>("");
  const [mutationError, setMutationError] = useState<string>("");
  const [initialLoaded, setInitialLoaded] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    setRefreshing(true);
    try {
      setPayload(await fetchJson<NetworksPayload>("/api/sdn/inventory"));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load network inventory");
    } finally {
      setRefreshing(false);
      setInitialLoaded(true);
    }
  }, []);

  usePolling(load);

  const zoneRows = asArray(payload.sdn?.zones);
  const vnetRows = asArray(payload.sdn?.vnets);
  const subnetRows = useMemo(() => subnets(payload), [payload]);
  const controllerRows = asArray(payload.sdn?.controllers);
  const ipamRows = asArray(payload.sdn?.ipams);
  const dnsRows = asArray(payload.sdn?.dns);
  const selectedZone = labForm.zone || formObjectId(zoneRows[0], "zone");
  const selectedVnet = labForm.vnet || formObjectId(vnetRows[0], "vnet");
  const selectedSubnetRows = subnetRows.filter((row) => row.parentVnet === selectedVnet);
  const selectedSubnet = labForm.subnet || formObjectId(selectedSubnetRows[0], "subnet");
  const canCreateLab = Boolean(selectedZone && selectedVnet && selectedSubnet);

  function makeBusyKey(kind: SdnKindKey, id: string, parent?: string): string {
    return parent ? `${kind}:${parent}/${id}` : `${kind}:${id}`;
  }

  /**
   * Apply schema-level defaults (e.g. type=subnet) plus subnet-specific
   * dhcp range octet combination. Called by both createObject and
   * updateObject so the same fields land in POST and PATCH.
   */
  function finalizeBody(
    kind: SdnKindKey,
    body: Record<string, unknown>,
    values: Readonly<Record<string, string>>
  ): Record<string, unknown> {
    const schema = sdnSchemas[kind];
    const merged: Record<string, unknown> = { ...(schema.defaultBody ?? {}), ...body };
    if (kind === "subnet") {
      const cidr = values["subnet"] ?? "";
      const prefix = cidrHostPrefix(cidr);
      const start = (values["dhcp_range_start"] ?? "").trim();
      const end = (values["dhcp_range_end"] ?? "").trim();
      if (prefix && start && end) {
        merged["dhcp-range"] = `start-address=${prefix}.${start},end-address=${prefix}.${end}`;
      }
    }
    return merged;
  }

  async function createObject(kind: SdnKindKey, values: Readonly<Record<string, string>>) {
    const schema = sdnSchemas[kind];
    const id = (values[schema.idField] ?? "").trim();
    if (!id) {
      setMutationError(`${schema.singular} ID is required`);
      return;
    }
    const parent = kind === "subnet" ? (values.parentVnet ?? "").trim() : undefined;
    if (kind === "subnet" && !parent) {
      setMutationError("Parent VNet is required for a subnet");
      return;
    }
    const body = finalizeBody(kind, bodyFromValues(schema.createFields, values, { includeEmptyBooleans: true }), values);
    setBusyKey(makeBusyKey(kind, id, parent));
    setMutationError("");
    try {
      await postJson<Record<string, unknown>>(schema.createPath({ ...values, parentVnet: parent ?? "" }), body);
      setCreating(null);
      await load();
    } catch (err) {
      setMutationError(err instanceof Error ? err.message : `Failed to create ${schema.singular}`);
    } finally {
      setBusyKey("");
    }
  }

  async function updateObject(kind: SdnKindKey, id: string, values: Readonly<Record<string, string>>, parent?: string) {
    const schema = sdnSchemas[kind];
    const body = finalizeBody(kind, bodyFromValues(schema.editFields, values, { includeEmptyBooleans: false }), values);
    setBusyKey(makeBusyKey(kind, id, parent));
    setMutationError("");
    try {
      await patchJson<Record<string, unknown>>(schema.editPath(id, parent), body);
      setEditing(null);
      await load();
    } catch (err) {
      setMutationError(err instanceof Error ? err.message : `Failed to update ${schema.singular}`);
    } finally {
      setBusyKey("");
    }
  }

  async function deleteObject(kind: SdnKindKey, id: string, parent?: string) {
    const schema = sdnSchemas[kind];
    const label = parent ? `${schema.singular} ${id} (under ${parent})` : `${schema.singular} ${id}`;
    if (typeof window !== "undefined" && !window.confirm(`Delete ${label}? Pending changes still need Apply SDN to take effect.`)) {
      return;
    }
    setBusyKey(makeBusyKey(kind, id, parent));
    setMutationError("");
    try {
      await deleteJson<Record<string, unknown>>(schema.deletePath(id, parent));
      if (editing && editing.kind === kind && editing.id === id && editing.parent === parent) {
        setEditing(null);
      }
      await load();
    } catch (err) {
      setMutationError(err instanceof Error ? err.message : `Failed to delete ${schema.singular}`);
    } finally {
      setBusyKey("");
    }
  }

  function buildActions(kind: SdnKindKey, canEdit: boolean): RowActions {
    return {
      kind,
      canEdit,
      editingId: editing && editing.kind === kind ? editing.id : undefined,
      editingParent: editing && editing.kind === kind ? editing.parent : undefined,
      busy: busyKey,
      mutationError,
      onEdit: (id, parent) => {
        setMutationError("");
        setEditing({ kind, id, parent });
      },
      onCancelEdit: () => {
        setMutationError("");
        setEditing(null);
      },
      onDelete: (id, parent) => {
        void deleteObject(kind, id, parent);
      },
      onSubmitEdit: (id, values, parent) => {
        void updateObject(kind, id, values, parent);
      }
    };
  }

  function toggleCreate(kind: SdnKindKey) {
    setMutationError("");
    setCreating((current) => (current === kind ? null : kind));
  }

  async function applySdn() {
    const token = lockToken.trim();
    if (!token) {
      return;
    }
    setApplyStatus("Applying SDN changes...");
    try {
      await postJson<Record<string, unknown>>("/api/sdn/apply", { lock_token: token });
      setLockToken("");
      setApplyStatus("SDN apply requested. Inventory is refreshing.");
      await load();
    } catch (err) {
      setApplyStatus(err instanceof Error ? err.message : "Failed to apply SDN changes");
    }
  }

  async function createLab() {
    if (!canCreateLab) {
      return;
    }
    const body = {
      name: labForm.name.trim() || selectedVnet,
      zone: selectedZone,
      vnet: selectedVnet,
      subnet: selectedSubnet,
      domain_name: labForm.domain_name.trim(),
      cidr: labForm.cidr.trim(),
      gateway_ip: labForm.gateway_ip.trim(),
      egress_policy: "open",
      snat_enabled: true,
      firewall_profile: "isolated_open_egress"
    };
    setLabStatus("Checking SDN lab preflight...");
    try {
      const preflight = await postJson<SdnLabPreflight>("/api/sdn/labs/preflight", body);
      const blockingCount = preflight.blocking?.length ?? 0;
      if (blockingCount > 0) {
        setLabStatus(`Preflight blocked by ${String(blockingCount)} issue${blockingCount === 1 ? "" : "s"}.`);
        return;
      }
      await postJson<Record<string, unknown>>("/api/sdn/labs", body);
      const warningCount = preflight.warnings?.length ?? 0;
      setLabStatus(
        warningCount > 0
          ? `Lab saved with open egress and ${String(warningCount)} preflight warning${warningCount === 1 ? "" : "s"}.`
          : "Lab saved with outbound egress open by default."
      );
      await load();
    } catch (err) {
      setLabStatus(err instanceof Error ? err.message : "Failed to create isolated lab");
    }
  }

  return (
    <PageFrame bootstrap={bootstrap} title="Networks" section="Infrastructure" path={path}>
      <div
        className={`networks-loading-bar${refreshing ? " is-active" : ""}`}
        role="progressbar"
        aria-label="Loading network inventory"
        aria-busy={refreshing}
      />
      {error ? <p className="notice" role="status">{error}</p> : null}

      {!initialLoaded ? (
        <p className="networks-loading-status" role="status" aria-live="polite">
          Loading network inventory from Proxmox SDN...
        </p>
      ) : null}

      {!initialLoaded ? null : (
      <>
      <section className="metric-strip metric-strip--networks" aria-label="SDN inventory">
        <Metric label="Zones" value={String(zoneRows.length)} />
        <Metric label="VNets" value={String(vnetRows.length)} tone={vnetRows.length ? "active" : "neutral"} />
        <Metric label="Subnets" value={String(subnetRows.length)} />
        <Metric label="Egress" value="Open" tone="good" />
      </section>

      <div className="segmented networks-tabs" role="tablist" aria-label="Networks workspace">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.id}
            className={activeTab === tab.id ? "is-active" : ""}
            onClick={() => {
              setActiveTab(tab.id);
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <section className="networks-layout">
        {(activeTab === "overview" || activeTab === "zones") ? (
          <Panel title="Zones">
            <ObjectTable rows={zoneRows} kind="zones" actions={buildActions("zone", true)} />
            <CreatePanelAffordance
              schema={zoneSchema}
              open={creating === "zone"}
              busy={Boolean(busyKey) && busyKey.startsWith("zone:")}
              error={creating === "zone" ? mutationError : undefined}
              onToggle={() => {
                toggleCreate("zone");
              }}
              onSubmit={(values) => {
                void createObject("zone", values);
              }}
            />
          </Panel>
        ) : null}

        {(activeTab === "overview" || activeTab === "vnets") ? (
          <Panel title="VNets">
            <ObjectTable rows={vnetRows} kind="vnets" actions={buildActions("vnet", true)} />
            <CreatePanelAffordance
              schema={vnetSchema}
              open={creating === "vnet"}
              busy={Boolean(busyKey) && busyKey.startsWith("vnet:")}
              error={creating === "vnet" ? mutationError : undefined}
              parentOptions={zoneRows.map((row) => formObjectId(row, "zone")).filter(Boolean)}
              onToggle={() => {
                toggleCreate("vnet");
              }}
              onSubmit={(values) => {
                void createObject("vnet", values);
              }}
            />
          </Panel>
        ) : null}

        {(activeTab === "overview" || activeTab === "subnets") ? (
          <Panel title="Subnets">
            <SubnetTable rows={subnetRows} actions={buildActions("subnet", true)} />
            <CreatePanelAffordance
              schema={subnetSchema}
              open={creating === "subnet"}
              busy={Boolean(busyKey) && busyKey.startsWith("subnet:")}
              error={creating === "subnet" ? mutationError : undefined}
              parentOptions={vnetRows.map((row) => formObjectId(row, "vnet")).filter(Boolean)}
              onToggle={() => {
                toggleCreate("subnet");
              }}
              onSubmit={(values) => {
                void createObject("subnet", values);
              }}
            />
          </Panel>
        ) : null}

        {activeTab === "controllers" ? (
          <Panel title="Controllers">
            <ObjectTable rows={controllerRows} kind="controllers" actions={buildActions("controller", true)} />
            <CreatePanelAffordance
              schema={controllerSchema}
              open={creating === "controller"}
              busy={Boolean(busyKey) && busyKey.startsWith("controller:")}
              error={creating === "controller" ? mutationError : undefined}
              onToggle={() => {
                toggleCreate("controller");
              }}
              onSubmit={(values) => {
                void createObject("controller", values);
              }}
            />
          </Panel>
        ) : null}

        {activeTab === "ipam" ? (
          <Panel title="IPAM">
            <ObjectTable rows={ipamRows} kind="ipam" actions={buildActions("ipam", false)} />
            <CreatePanelAffordance
              schema={ipamSchema}
              open={creating === "ipam"}
              busy={Boolean(busyKey) && busyKey.startsWith("ipam:")}
              error={creating === "ipam" ? mutationError : undefined}
              onToggle={() => {
                toggleCreate("ipam");
              }}
              onSubmit={(values) => {
                void createObject("ipam", values);
              }}
            />
          </Panel>
        ) : null}

        {activeTab === "dns" ? (
          <Panel title="DNS">
            <ObjectTable rows={dnsRows} kind="dns" actions={buildActions("dns", false)} />
            <CreatePanelAffordance
              schema={dnsSchema}
              open={creating === "dns"}
              busy={Boolean(busyKey) && busyKey.startsWith("dns:")}
              error={creating === "dns" ? mutationError : undefined}
              onToggle={() => {
                toggleCreate("dns");
              }}
              onSubmit={(values) => {
                void createObject("dns", values);
              }}
            />
          </Panel>
        ) : null}

        {(activeTab === "overview" || activeTab === "firewall") ? (
          <Panel title="Firewall Editor">
            <FirewallSurface payload={payload} />
          </Panel>
        ) : null}

        {(activeTab === "overview" || activeTab === "pending") ? (
          <Panel title="Apply Gate">
            <div className="networks-apply">
              <label className="cloudosd-field">
                <span>Lock token</span>
                <input
                  value={lockToken}
                  onChange={(event) => {
                    setLockToken(event.currentTarget.value);
                  }}
                  placeholder="digest from SDN lock"
                />
              </label>
              <button
                className="utility-button"
                type="button"
                disabled={!lockToken.trim()}
                onClick={() => {
                  void applySdn();
                }}
              >
                Apply SDN
              </button>
              {applyStatus ? <p className="networks-note" role="status">{applyStatus}</p> : null}
            </div>
          </Panel>
        ) : null}

        {activeTab === "overview" ? (
          <Panel title="Create Isolated Lab">
            <form
              className="networks-lab-form"
              onSubmit={(event) => {
                event.preventDefault();
                void createLab();
              }}
            >
              <div className="networks-lab-default">
                <strong>Outbound egress open by default</strong>
                <span>Lab workloads use the selected subnet and SNAT path for outbound access while inbound, inter-lab, and management-plane paths stay constrained.</span>
              </div>
              <label className="cloudosd-field">
                <span>Lab name</span>
                <input
                  aria-label="Lab name"
                  value={labForm.name}
                  onChange={(event) => {
                    const value = event.currentTarget.value;
                    setLabForm((current) => ({ ...current, name: value }));
                  }}
                  placeholder={selectedVnet || "lab name"}
                />
              </label>
              <div className="networks-lab-grid">
                <label className="cloudosd-field">
                  <span>Zone</span>
                  <select
                    aria-label="Zone"
                    value={selectedZone}
                    onChange={(event) => {
                      const value = event.currentTarget.value;
                      setLabForm((current) => ({ ...current, zone: value }));
                    }}
                  >
                    {!zoneRows.length ? <option value="">No zones</option> : null}
                    {zoneRows.map((row) => {
                      const value = formObjectId(row, "zone");
                      return <option key={`zone-${value}`} value={value}>{value}</option>;
                    })}
                  </select>
                </label>
                <label className="cloudosd-field">
                  <span>VNet</span>
                  <select
                    aria-label="VNet"
                    value={selectedVnet}
                    onChange={(event) => {
                      const value = event.currentTarget.value;
                      setLabForm((current) => ({ ...current, vnet: value, subnet: "" }));
                    }}
                  >
                    {!vnetRows.length ? <option value="">No VNets</option> : null}
                    {vnetRows.map((row) => {
                      const value = formObjectId(row, "vnet");
                      const zone = textValue(row.zone, "");
                      return <option key={`vnet-${value}`} value={value}>{zone ? `${value} / ${zone}` : value}</option>;
                    })}
                  </select>
                </label>
                <label className="cloudosd-field">
                  <span>Subnet</span>
                  <select
                    aria-label="Subnet"
                    value={selectedSubnet}
                    onChange={(event) => {
                      const value = event.currentTarget.value;
                      setLabForm((current) => ({ ...current, subnet: value }));
                    }}
                  >
                    {!selectedSubnetRows.length ? <option value="">No subnet selected</option> : null}
                    {selectedSubnetRows.map((row) => {
                      const value = formObjectId(row, "subnet");
                      return <option key={`subnet-${selectedVnet}-${value}`} value={value}>{value}</option>;
                    })}
                  </select>
                </label>
              </div>
              <div className="networks-lab-grid">
                <label className="cloudosd-field">
                  <span>Domain</span>
                  <input
                  aria-label="Domain"
                  value={labForm.domain_name}
                  onChange={(event) => {
                      const value = event.currentTarget.value;
                      setLabForm((current) => ({ ...current, domain_name: value }));
                  }}
                  placeholder="lab.example.test"
                />
                </label>
                <label className="cloudosd-field">
                  <span>CIDR</span>
                  <input
                  aria-label="CIDR"
                  value={labForm.cidr}
                  onChange={(event) => {
                      const value = event.currentTarget.value;
                      setLabForm((current) => ({ ...current, cidr: value }));
                  }}
                  placeholder="10.60.10.0/24"
                />
                </label>
                <label className="cloudosd-field">
                  <span>Gateway IP</span>
                  <input
                  aria-label="Gateway IP"
                  value={labForm.gateway_ip}
                  onChange={(event) => {
                      const value = event.currentTarget.value;
                      setLabForm((current) => ({ ...current, gateway_ip: value }));
                  }}
                  placeholder="10.60.10.1"
                />
                </label>
              </div>
              <button className="utility-button" type="submit" disabled={!canCreateLab}>
                Create isolated lab
              </button>
              {labStatus ? <p className="networks-note" role="status">{labStatus}</p> : null}
            </form>
          </Panel>
        ) : null}
      </section>
      </>
      )}
    </PageFrame>
  );
}
