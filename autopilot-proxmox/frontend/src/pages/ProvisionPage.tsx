import { useCallback, useId, useMemo, useState } from "react";

import { fetchJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { networkTargetOptions, type NetworkTargetOption } from "../networkTargets";
import { textValue } from "../utilityModels";

type BootMode = "cloudosd" | "osdeploy" | "ubuntu" | "winpe" | "clone";

interface ProvisionPagePayload {
  readonly profiles: Readonly<Record<string, OemProfile>>;
  readonly defaults: ProvisionDefaults;
  readonly template_disk_gb?: number | null;
  readonly winpe_enabled?: boolean;
  readonly cloudosd_catalog: CatalogPayload;
  readonly cloudosd_options: ProxmoxOptionsPayload;
  readonly osdeploy_catalog: OsdeployCatalogPayload;
  readonly osdeploy_options: ProxmoxOptionsPayload;
  readonly cloudosd_artifacts: readonly CloudosdArtifact[];
  readonly osdeploy_artifacts: readonly OsdeployArtifact[];
  readonly cloudosd_ready_artifacts: readonly CloudosdArtifact[];
  readonly osdeploy_ready_artifacts: readonly OsdeployArtifact[];
  readonly cloudosd_batch_progress: CloudosdBatchProgress;
  readonly cloudosd_cache: CachePayload;
  readonly osdeploy_cache: CachePayload;
  readonly ubuntu_v2_sequences: readonly UbuntuSequence[];
}

interface OemProfile {
  readonly manufacturer?: string;
  readonly product?: string;
}

interface ProvisionDefaults {
  readonly cores?: number | string;
  readonly memory_mb?: number | string;
  readonly disk_size_gb?: number | string;
  readonly count?: number | string;
  readonly serial_prefix?: string;
  readonly group_tag?: string;
  readonly oem_profile?: string;
  readonly template_vmid?: string | number;
  readonly hostname_pattern?: string;
}

interface CatalogPayload {
  readonly os_versions?: readonly string[];
  readonly os_editions?: readonly string[];
  readonly os_activations?: readonly string[];
  readonly os_languages?: readonly string[];
  readonly driver_pack_policies?: readonly string[];
  readonly defaults?: Readonly<Record<string, string | number | boolean | null | undefined>>;
}

interface OsdeployCatalogPayload {
  readonly server_roles?: readonly string[];
  readonly os_versions?: readonly string[];
  readonly os_editions?: readonly string[];
  readonly os_languages?: readonly string[];
  readonly defaults?: Readonly<Record<string, string | number | boolean | null | undefined>>;
}

interface ProxmoxOptionsPayload {
  readonly nodes?: readonly string[];
  readonly bridges?: readonly string[];
  readonly network_targets?: readonly NetworkTargetOption[];
  readonly storages?: {
    readonly iso?: readonly string[];
    readonly disk?: readonly string[];
  };
  readonly defaults?: {
    readonly node?: string;
    readonly iso_storage?: string;
    readonly disk_storage?: string;
    readonly bridge?: string;
  };
}

interface CloudosdArtifact {
  readonly id?: string;
  readonly build_sha?: string;
  readonly osdcloud_module_version?: string;
  readonly readiness?: string;
  readonly ready?: boolean;
  readonly proxmox_volid?: string;
}

interface OsdeployArtifact {
  readonly id?: string;
  readonly build_sha?: string;
  readonly os_version?: string;
  readonly os_edition?: string;
  readonly readiness?: string;
  readonly ready?: boolean;
  readonly proxmox_volid?: string;
}

interface UbuntuSequence {
  readonly id?: string | number;
  readonly name?: string;
  readonly step_count?: number;
}

interface CachePayload {
  readonly storage?: {
    readonly ready?: boolean;
    readonly root?: string;
  };
  readonly summary?: {
    readonly ready?: number;
    readonly total?: number;
  };
}

interface CloudosdBatchProgress {
  readonly summary?: {
    readonly total?: number;
    readonly deployed?: number;
    readonly uploaded?: number;
    readonly assigned?: number;
    readonly contacted_enrolled?: number;
  };
  readonly runs?: readonly CloudosdProgressRun[];
}

interface CloudosdProgressRun {
  readonly run_id?: string;
  readonly vm_name?: string;
  readonly vmid?: number | string | null;
  readonly done_count?: number;
  readonly total_count?: number;
  readonly failed_count?: number;
  readonly milestones?: Readonly<Record<string, ProgressMilestone | undefined>>;
}

interface ProgressMilestone {
  readonly state?: string;
  readonly label?: string;
  readonly detail?: string;
  readonly at?: string;
}

const EMPTY_PAYLOAD: ProvisionPagePayload = {
  profiles: {},
  defaults: {},
  cloudosd_catalog: {},
  cloudosd_options: {},
  osdeploy_catalog: {},
  osdeploy_options: {},
  cloudosd_artifacts: [],
  osdeploy_artifacts: [],
  cloudosd_ready_artifacts: [],
  osdeploy_ready_artifacts: [],
  cloudosd_batch_progress: {},
  cloudosd_cache: {},
  osdeploy_cache: {},
  ubuntu_v2_sequences: []
};

function asRecord(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Readonly<Record<string, unknown>> : {};
}

function asNumber(value: unknown, fallback: number): number {
  const numberValue = typeof value === "number" ? value : Number(value);
  return Number.isFinite(numberValue) ? numberValue : fallback;
}

function provisionPayloadFromUnknown(value: unknown): ProvisionPagePayload {
  const record = asRecord(value);
  const defaults = asRecord(record.defaults);
  const cloudosdCatalog = asRecord(record.cloudosd_catalog);
  const osdeployCatalog = asRecord(record.osdeploy_catalog);
  const cloudosdOptions = asRecord(record.cloudosd_options);
  const osdeployOptions = asRecord(record.osdeploy_options);
  return {
    profiles: asRecord(record.profiles) as Readonly<Record<string, OemProfile>>,
    defaults,
    template_disk_gb: typeof record.template_disk_gb === "number" ? record.template_disk_gb : null,
    winpe_enabled: Boolean(record.winpe_enabled),
    cloudosd_catalog: cloudosdCatalog,
    cloudosd_options: cloudosdOptions,
    osdeploy_catalog: osdeployCatalog,
    osdeploy_options: osdeployOptions,
    cloudosd_artifacts: Array.isArray(record.cloudosd_artifacts) ? record.cloudosd_artifacts as readonly CloudosdArtifact[] : [],
    osdeploy_artifacts: Array.isArray(record.osdeploy_artifacts) ? record.osdeploy_artifacts as readonly OsdeployArtifact[] : [],
    cloudosd_ready_artifacts: Array.isArray(record.cloudosd_ready_artifacts) ? record.cloudosd_ready_artifacts as readonly CloudosdArtifact[] : [],
    osdeploy_ready_artifacts: Array.isArray(record.osdeploy_ready_artifacts) ? record.osdeploy_ready_artifacts as readonly OsdeployArtifact[] : [],
    cloudosd_batch_progress: asRecord(record.cloudosd_batch_progress),
    cloudosd_cache: asRecord(record.cloudosd_cache),
    osdeploy_cache: asRecord(record.osdeploy_cache),
    ubuntu_v2_sequences: Array.isArray(record.ubuntu_v2_sequences) ? record.ubuntu_v2_sequences as readonly UbuntuSequence[] : []
  };
}

function firstReadyArtifact(artifacts: readonly { readonly id?: string; readonly ready?: boolean }[]): string {
  return textValue(artifacts.find((artifact) => artifact.ready)?.id ?? artifacts[0]?.id, "");
}

function optionLabel(parts: readonly unknown[]): string {
  return parts.map((part) => textValue(part, "")).filter(Boolean).join(" / ") || "-";
}

function SelectField({
  label,
  name,
  value,
  defaultValue,
  onChange,
  options,
  help
}: {
  readonly label: string;
  readonly name: string;
  readonly value?: string | undefined;
  readonly defaultValue?: string | undefined;
  readonly onChange?: (value: string) => void;
  readonly options: readonly { readonly value: string; readonly label: string }[];
  readonly help?: string;
}) {
  const id = useId();
  const helpId = `${id}-help`;
  return (
    <div className="utility-field">
      <label htmlFor={id}>{label}</label>
      <select
        id={id}
        name={name}
        value={value}
        defaultValue={value === undefined ? defaultValue : undefined}
        aria-describedby={help ? helpId : undefined}
        onChange={onChange ? (event) => { onChange(event.currentTarget.value); } : undefined}
      >
        {options.map((option) => (
          <option key={`${name}-${option.value}-${option.label}`} value={option.value}>{option.label}</option>
        ))}
      </select>
      {help ? <small id={helpId}>{help}</small> : null}
    </div>
  );
}

function TextField({
  label,
  name,
  defaultValue,
  help
}: {
  readonly label: string;
  readonly name: string;
  readonly defaultValue?: string | number | undefined;
  readonly help?: string;
}) {
  const id = useId();
  const helpId = `${id}-help`;
  return (
    <div className="utility-field">
      <label htmlFor={id}>{label}</label>
      <input id={id} name={name} type="text" defaultValue={textValue(defaultValue, "")} aria-describedby={help ? helpId : undefined} />
      {help ? <small id={helpId}>{help}</small> : null}
    </div>
  );
}

function NumberField({
  label,
  name,
  defaultValue,
  min,
  max,
  step,
  help
}: {
  readonly label: string;
  readonly name: string;
  readonly defaultValue?: string | number | undefined;
  readonly min?: number | undefined;
  readonly max?: number | undefined;
  readonly step?: number | undefined;
  readonly help?: string;
}) {
  const id = useId();
  const helpId = `${id}-help`;
  return (
    <div className="utility-field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        name={name}
        type="number"
        defaultValue={textValue(defaultValue, "")}
        min={min}
        max={max}
        step={step}
        aria-describedby={help ? helpId : undefined}
      />
      {help ? <small id={helpId}>{help}</small> : null}
    </div>
  );
}

function CheckboxField({
  label,
  name,
  defaultChecked = false
}: {
  readonly label: string;
  readonly name: string;
  readonly defaultChecked?: boolean;
}) {
  return (
    <label className="provision-checkbox">
      <input name={name} type="checkbox" defaultChecked={defaultChecked} />
      <span>{label}</span>
    </label>
  );
}

function optionsFrom(values: readonly string[] | undefined, selected = ""): readonly { readonly value: string; readonly label: string }[] {
  const items = values ?? [];
  if (!items.length && selected) {
    return [{ value: selected, label: selected }];
  }
  return items.map((item) => ({ value: item, label: item }));
}

function targetOptions(options: ProxmoxOptionsPayload) {
  return {
    nodes: optionsFrom(options.nodes, options.defaults?.node),
    isoStorages: optionsFrom(options.storages?.iso, options.defaults?.iso_storage),
    diskStorages: optionsFrom(options.storages?.disk, options.defaults?.disk_storage),
    networkTargets: networkTargetOptions(options)
  };
}

function CacheStatus({ cache, fallbackRoot }: { readonly cache: CachePayload; readonly fallbackRoot: string }) {
  const ready = Boolean(cache.storage?.ready);
  const summary = cache.summary ?? {};
  return (
    <div className="provision-cache-line">
      <span className={ready ? "status-pill status--good" : "status-pill status--active"}>{ready ? "Ready" : "Not ready"}</span>
      <span>{String(summary.ready ?? 0)}/{String(summary.total ?? 0)} cache entries ready</span>
      <code>{textValue(cache.storage?.root, fallbackRoot)}</code>
    </div>
  );
}

function CloudosdSection({ payload }: { readonly payload: ProvisionPagePayload }) {
  const target = targetOptions(payload.cloudosd_options);
  const catalogDefaults = payload.cloudosd_catalog.defaults ?? {};
  const artifactOptions = [
    { value: "", label: "Select ready artifact" },
    ...payload.cloudosd_artifacts.map((artifact) => ({
      value: textValue(artifact.id, ""),
      label: optionLabel([artifact.build_sha, artifact.osdcloud_module_version, artifact.readiness, artifact.proxmox_volid])
    }))
  ];
  return (
    <Panel title="OSDCloud Desktop">
      <div className="utility-field-grid">
        <SelectField
          label="OSDCloud artifact"
          name="artifact_id"
          defaultValue={firstReadyArtifact(payload.cloudosd_artifacts)}
          options={artifactOptions}
          help="Ready artifacts include hashes and a Proxmox ISO volid."
        />
        <SelectField label="Node" name="node" defaultValue={payload.cloudosd_options.defaults?.node} options={target.nodes} />
        <SelectField label="ISO storage" name="iso_storage" defaultValue={payload.cloudosd_options.defaults?.iso_storage} options={target.isoStorages} />
        <SelectField label="Disk storage" name="storage" defaultValue={payload.cloudosd_options.defaults?.disk_storage} options={target.diskStorages} />
        <SelectField label="Network target" name="network_bridge" defaultValue={payload.cloudosd_options.defaults?.bridge} options={target.networkTargets} />
        <SelectField label="OS version" name="os_version" defaultValue={textValue(catalogDefaults.os_version, "")} options={optionsFrom(payload.cloudosd_catalog.os_versions, textValue(catalogDefaults.os_version, ""))} />
        <SelectField label="OS edition" name="os_edition" defaultValue={textValue(catalogDefaults.os_edition, "")} options={optionsFrom(payload.cloudosd_catalog.os_editions, textValue(catalogDefaults.os_edition, ""))} />
        <SelectField label="OS activation" name="os_activation" defaultValue={textValue(catalogDefaults.os_activation, "")} options={optionsFrom(payload.cloudosd_catalog.os_activations, textValue(catalogDefaults.os_activation, ""))} />
        <SelectField label="OS language" name="os_language" defaultValue={textValue(catalogDefaults.os_language, "")} options={optionsFrom(payload.cloudosd_catalog.os_languages, textValue(catalogDefaults.os_language, ""))} />
        <SelectField label="Driver pack policy" name="driver_pack_policy" defaultValue={textValue(catalogDefaults.driver_pack_policy, "")} options={optionsFrom(payload.cloudosd_catalog.driver_pack_policies, textValue(catalogDefaults.driver_pack_policy, ""))} />
        <SelectField
          label="Outbound policy"
          name="outbound_policy_mode"
          defaultValue="blocked"
          options={[
            { value: "blocked", label: "analytics blocked" },
            { value: "allowed", label: "analytics allowed" }
          ]}
        />
        <div className="utility-field utility-field--wide">
          <span>OSDCloud policy</span>
          <div className="provision-checkbox-row">
            <CheckboxField label="TPM" name="tpm_enabled" defaultChecked />
            <CheckboxField label="Secure Boot" name="secure_boot" defaultChecked />
            <CheckboxField label="Firmware updates" name="firmware_updates_enabled" />
            <CheckboxField label="Allow analytics" name="analytics_enabled" />
          </div>
        </div>
        <div className="utility-field utility-field--wide">
          <span>OSDCloud cache</span>
          <CacheStatus cache={payload.cloudosd_cache} fallbackRoot="/app/cache/cloudosd" />
        </div>
      </div>
    </Panel>
  );
}

function OsdeploySection({ payload }: { readonly payload: ProvisionPagePayload }) {
  const target = targetOptions(payload.osdeploy_options);
  const catalogDefaults = payload.osdeploy_catalog.defaults ?? {};
  const artifactOptions = [
    { value: "", label: "Select ready Server artifact" },
    ...payload.osdeploy_artifacts.map((artifact) => ({
      value: textValue(artifact.id, ""),
      label: optionLabel([artifact.build_sha, artifact.os_version, artifact.os_edition, artifact.readiness, artifact.proxmox_volid])
    }))
  ];
  return (
    <Panel title="OSDeploy Server">
      <div className="utility-field-grid">
        <SelectField
          label="OSDeploy artifact"
          name="osdeploy_artifact_id"
          defaultValue={firstReadyArtifact(payload.osdeploy_artifacts)}
          options={artifactOptions}
          help="Ready Server artifacts include media metadata, hashes, manifest, and Proxmox ISO volid."
        />
        <SelectField label="Server role" name="osdeploy_server_role" defaultValue="base" options={optionsFrom(payload.osdeploy_catalog.server_roles, "base")} />
        <SelectField label="OSDeploy node" name="osdeploy_node" defaultValue={payload.osdeploy_options.defaults?.node} options={target.nodes} />
        <SelectField label="OSDeploy ISO storage" name="osdeploy_iso_storage" defaultValue={payload.osdeploy_options.defaults?.iso_storage} options={target.isoStorages} />
        <SelectField label="OSDeploy disk storage" name="osdeploy_storage" defaultValue={payload.osdeploy_options.defaults?.disk_storage} options={target.diskStorages} />
        <SelectField label="OSDeploy network target" name="osdeploy_network_bridge" defaultValue={payload.osdeploy_options.defaults?.bridge} options={target.networkTargets} />
        <SelectField label="OSDeploy OS version" name="osdeploy_os_version" defaultValue={textValue(catalogDefaults.os_version, "")} options={optionsFrom(payload.osdeploy_catalog.os_versions, textValue(catalogDefaults.os_version, ""))} />
        <SelectField label="OSDeploy OS edition" name="osdeploy_os_edition" defaultValue={textValue(catalogDefaults.os_edition, "")} options={optionsFrom(payload.osdeploy_catalog.os_editions, textValue(catalogDefaults.os_edition, ""))} />
        <SelectField label="OSDeploy OS language" name="osdeploy_os_language" defaultValue={textValue(catalogDefaults.os_language, "")} options={optionsFrom(payload.osdeploy_catalog.os_languages, textValue(catalogDefaults.os_language, ""))} />
        <SelectField
          label="Outbound policy"
          name="outbound_policy_mode"
          defaultValue="blocked"
          options={[
            { value: "blocked", label: "analytics blocked" },
            { value: "allowed", label: "analytics allowed" }
          ]}
        />
        <div className="utility-field utility-field--wide">
          <span>OSDeploy cache</span>
          <CacheStatus cache={payload.osdeploy_cache} fallbackRoot="/app/cache/osdeploy" />
        </div>
      </div>
    </Panel>
  );
}

function UbuntuSection({ payload }: { readonly payload: ProvisionPagePayload }) {
  const sequenceOptions = [
    { value: "", label: "Select Ubuntu v2 sequence" },
    ...payload.ubuntu_v2_sequences.map((sequence) => ({
      value: textValue(sequence.id, ""),
      label: `${textValue(sequence.name, "Ubuntu sequence")} / ${String(sequence.step_count ?? 0)} steps`
    }))
  ];
  return (
    <Panel title="Ubuntu v2">
      <div className="utility-field-grid">
        <SelectField
          label="Ubuntu v2 sequence"
          name="ubuntu_v2_sequence_id"
          defaultValue=""
          options={sequenceOptions}
          help="Ubuntu uses the v2 sequence library, cloud-init, QGA, and the Linux agent."
        />
        <NumberField
          label="Ubuntu template VMID"
          name="ubuntu_template_vmid"
          defaultValue={payload.defaults.template_vmid}
          min={1}
          max={999999}
          help="Clone source for Ubuntu v2."
        />
      </div>
      <div className="chip-row provision-chip-row">
        <span className="status-pill status--active">cloud-init</span>
        <span className="status-pill status--active">QGA</span>
        <span className="status-pill status--active">Linux agent</span>
        <span className="status-pill status--active">Intune waits for sign-in</span>
      </div>
    </Panel>
  );
}

function LegacyNotice({ mode, winpeEnabled }: { readonly mode: BootMode; readonly winpeEnabled: boolean }) {
  if (mode === "winpe" && !winpeEnabled) {
    return <p className="notice notice--bad">WinPE is visible, but inventory settings are not complete for launch.</p>;
  }
  if (mode === "clone") {
    return <p className="muted">Clone remains available as a fallback template workflow.</p>;
  }
  if (mode === "winpe") {
    return <p className="muted">Legacy WinPE launches one VM and lands on the run timeline.</p>;
  }
  return null;
}

function ProgressCell({ milestone }: { readonly milestone: ProgressMilestone | undefined }) {
  const state = milestone?.state ?? "waiting";
  const tone = state === "done" ? "status--good" : state === "failed" ? "status--bad" : "status--active";
  return (
    <>
      <span className={`status-pill ${tone}`}>{textValue(milestone?.label, state)}</span>
      <small>{textValue(milestone?.at ?? milestone?.detail)}</small>
    </>
  );
}

function BatchProgress({ progress }: { readonly progress: CloudosdBatchProgress }) {
  const summary = progress.summary ?? {};
  const total = summary.total ?? 0;
  const runs = progress.runs ?? [];
  return (
    <Panel title="OSDCloud Batch Progress">
      <section className="metric-strip provision-progress-metrics" aria-label="OSDCloud batch progress">
        <Metric label="Deployed" value={`${String(summary.deployed ?? 0)}/${String(total)}`} />
        <Metric label="Uploaded" value={`${String(summary.uploaded ?? 0)}/${String(total)}`} />
        <Metric label="Assigned" value={`${String(summary.assigned ?? 0)}/${String(total)}`} />
        <Metric label="Contacted" value={`${String(summary.contacted_enrolled ?? 0)}/${String(total)}`} />
      </section>
      {!runs.length ? (
        <p className="empty">No OSDCloud runs launched from Provision yet.</p>
      ) : (
        <div className="table-wrap">
          <table className="jobs-table provision-progress-table">
            <thead>
              <tr>
                <th scope="col">VM</th>
                <th scope="col">VM created</th>
                <th scope="col">PE registered</th>
                <th scope="col">OSDCloud done</th>
                <th scope="col">Agent heartbeat</th>
                <th scope="col">v2 steps</th>
                <th scope="col">Intune</th>
                <th scope="col">Progress</th>
                <th scope="col">Run</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run, index) => (
                <tr key={textValue(run.run_id, String(index))}>
                  <td>
                    <strong>{textValue(run.vm_name, "VM")}</strong>
                    {run.vmid ? <small>VMID {textValue(run.vmid)}</small> : null}
                  </td>
                  <td><ProgressCell milestone={run.milestones?.vm_created} /></td>
                  <td><ProgressCell milestone={run.milestones?.pe_registered} /></td>
                  <td><ProgressCell milestone={run.milestones?.osdcloud_done} /></td>
                  <td><ProgressCell milestone={run.milestones?.agent_heartbeat} /></td>
                  <td><ProgressCell milestone={run.milestones?.v2_steps_done} /></td>
                  <td><ProgressCell milestone={run.milestones?.intune_state} /></td>
                  <td>{String(run.done_count ?? 0)}/{String(run.total_count ?? 0)}{run.failed_count ? <small>{String(run.failed_count)} failed</small> : null}</td>
                  <td><a href={`/react/cloudosd/runs/${encodeURIComponent(textValue(run.run_id, ""))}`}>Open</a></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

export function ProvisionPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [payload, setPayload] = useState<ProvisionPagePayload>(EMPTY_PAYLOAD);
  const [bootMode, setBootMode] = useState<BootMode>("cloudosd");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setPayload(provisionPayloadFromUnknown(await fetchJson<unknown>("/api/provision/page")));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load provision payload");
    } finally {
      setLoading(false);
    }
  }, []);

  usePolling(load);

  const profileOptions = useMemo(() => {
    const rows = Object.entries(payload.profiles).map(([key, profile]) => ({
      value: key,
      label: `${key} - ${textValue(profile.manufacturer)} ${textValue(profile.product)}`.trim()
    }));
    return rows.length ? rows : [{ value: "", label: "No OEM profiles" }];
  }, [payload.profiles]);

  const defaults = payload.defaults;
  const cloudosdDefaults = payload.cloudosd_catalog.defaults ?? {};
  const osdeployDefaults = payload.osdeploy_catalog.defaults ?? {};
  const activeDefaults = bootMode === "osdeploy" ? osdeployDefaults : cloudosdDefaults;
  const templateMinimum = payload.template_disk_gb ?? 1;

  return (
    <PageFrame bootstrap={bootstrap} title="Provision" section="Deploy" path="/react/provision">
      {loading ? (
        <div className="load-strip" role="status" aria-live="polite">
          <span>Loading Provision</span>
          <div className="load-strip__track" role="progressbar" aria-label="Provision loading"><span /></div>
        </div>
      ) : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <section className="metric-strip" aria-label="Provision readiness">
        <Metric label="Template VMID" value={textValue(defaults.template_vmid)} />
        <Metric label="Default count" value={textValue(defaults.count, "1")} />
        <Metric label="Hostname pattern" value={textValue(defaults.hostname_pattern, "autopilot-{serial}")} />
        <Metric label="Cloud artifacts" value={String(payload.cloudosd_artifacts.length)} />
      </section>

      {!loading ? (
        <>
          <form
            className="utility-form provision-builder"
            method="post"
            action="/api/jobs/provision"
            data-testid="provision-builder-form"
          >
            <Panel title="Command Packet">
              <div className="utility-field-grid">
                <SelectField
                  label="Boot mode"
                  name="boot_mode"
                  value={bootMode}
                  onChange={(value) => { setBootMode(value as BootMode); }}
                  options={[
                    { value: "cloudosd", label: "OSDCloud" },
                    { value: "osdeploy", label: "OSDeploy v2" },
                    { value: "ubuntu", label: "Ubuntu v2" },
                    { value: "winpe", label: "Legacy WinPE" },
                    { value: "clone", label: "Clone" }
                  ]}
                  help="OSDCloud for desktop clients, OSDeploy for Server, Ubuntu v2 for Linux clients."
                />
                <SelectField label="OEM profile" name="profile" defaultValue={textValue(defaults.oem_profile, "")} options={profileOptions} />
                <SelectField
                  label="Chassis type override"
                  name="chassis_type_override"
                  defaultValue="0"
                  options={[
                    { value: "0", label: "Use profile default" },
                    { value: "3", label: "Desktop" },
                    { value: "8", label: "Portable" },
                    { value: "9", label: "Laptop" },
                    { value: "10", label: "Notebook" },
                    { value: "14", label: "Sub Notebook" },
                    { value: "15", label: "Space-saving" },
                    { value: "30", label: "Tablet" },
                    { value: "31", label: "Convertible" },
                    { value: "32", label: "Detachable" },
                    { value: "35", label: "Mini PC" }
                  ]}
                />
                <NumberField label="VM count" name="count" defaultValue={defaults.count ?? 1} min={1} max={50} />
                <TextField label="Hostname pattern" name="hostname_pattern" defaultValue={defaults.hostname_pattern ?? "autopilot-{serial}"} help="Tokens: {serial}, {vmid}, {index}." />
                <NumberField label="CPU cores" name="cores" defaultValue={textValue(defaults.cores ?? activeDefaults.vm_cores, "")} min={1} max={64} />
                <NumberField label="Memory MB" name="memory_mb" defaultValue={textValue(defaults.memory_mb ?? activeDefaults.vm_memory_mb, "")} min={asNumber(activeDefaults.minimum_vm_memory_mb, 512)} step={512} />
                <NumberField label="Disk GB" name="disk_size_gb" defaultValue={textValue(defaults.disk_size_gb ?? activeDefaults.vm_disk_size_gb, "")} min={Math.max(templateMinimum, asNumber(activeDefaults.minimum_vm_disk_size_gb, 1))} max={2048} />
                <TextField label="Serial prefix" name="serial_prefix" defaultValue={defaults.serial_prefix} />
                <TextField label="Group tag" name="group_tag" defaultValue={defaults.group_tag} />
              </div>
              <LegacyNotice mode={bootMode} winpeEnabled={Boolean(payload.winpe_enabled)} />
            </Panel>

            {bootMode === "cloudosd" ? <CloudosdSection payload={payload} /> : null}
            {bootMode === "osdeploy" ? <OsdeploySection payload={payload} /> : null}
            {bootMode === "ubuntu" ? <UbuntuSection payload={payload} /> : null}

            <div className="utility-form-actions">
              <button className="utility-button" type="submit">Provision VMs</button>
              <a className="utility-button" href="/react/cloudosd">OSDCloud</a>
              <a className="utility-button" href="/react/osdeploy">OSDeploy</a>
            </div>
          </form>

          <BatchProgress progress={payload.cloudosd_batch_progress} />
        </>
      ) : null}
    </PageFrame>
  );
}
