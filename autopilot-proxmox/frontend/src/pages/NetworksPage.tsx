import { useCallback, useMemo, useState } from "react";

import { fetchJson, postJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { textValue } from "../utilityModels";

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

function ObjectTable({ rows, kind }: { readonly rows: readonly SdnObject[]; readonly kind: string }) {
  if (!rows.length) {
    return <p className="empty">No {kind} found.</p>;
  }
  return (
    <div className="table-wrap">
      <table className="cloudosd-table networks-table">
        <thead>
          <tr>
            <th scope="col">Name</th>
            <th scope="col">Type</th>
            <th scope="col">Zone</th>
            <th scope="col">Detail</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={`${kind}-${objectId(row)}-${String(index)}`}>
              <td><code>{objectId(row)}</code></td>
              <td>{objectType(row)}</td>
              <td>{row.zone ? `zone ${textValue(row.zone)}` : "-"}</td>
              <td>{textValue(row.gateway ?? row.subnet ?? row.vnet, "-")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SubnetTable({ rows }: { readonly rows: readonly (SdnObject & { readonly parentVnet: string })[] }) {
  if (!rows.length) {
    return <p className="empty">No subnets found.</p>;
  }
  return (
    <div className="table-wrap">
      <table className="cloudosd-table networks-table">
        <thead>
          <tr>
            <th scope="col">Subnet</th>
            <th scope="col">VNet</th>
            <th scope="col">Gateway</th>
            <th scope="col">SNAT</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.parentVnet}-${textValue(row.subnet)}`}>
              <td><code>{textValue(row.subnet)}</code></td>
              <td>{row.parentVnet}</td>
              <td>{textValue(row.gateway, "-")}</td>
              <td>{row.snat ? "enabled" : "not set"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
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

  const load = useCallback(async () => {
    try {
      setPayload(await fetchJson<NetworksPayload>("/api/sdn/inventory"));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load network inventory");
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
      {error ? <p className="notice" role="status">{error}</p> : null}

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
            <ObjectTable rows={zoneRows} kind="zones" />
          </Panel>
        ) : null}

        {(activeTab === "overview" || activeTab === "vnets") ? (
          <Panel title="VNets">
            <ObjectTable rows={vnetRows} kind="vnets" />
          </Panel>
        ) : null}

        {(activeTab === "overview" || activeTab === "subnets") ? (
          <Panel title="Subnets">
            <SubnetTable rows={subnetRows} />
          </Panel>
        ) : null}

        {activeTab === "controllers" ? (
          <Panel title="Controllers">
            <ObjectTable rows={controllerRows} kind="controllers" />
          </Panel>
        ) : null}

        {activeTab === "ipam" ? (
          <Panel title="IPAM">
            <ObjectTable rows={ipamRows} kind="ipam" />
          </Panel>
        ) : null}

        {activeTab === "dns" ? (
          <Panel title="DNS">
            <ObjectTable rows={dnsRows} kind="dns" />
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
    </PageFrame>
  );
}
