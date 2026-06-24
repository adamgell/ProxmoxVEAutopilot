import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";

import { fetchJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { networkTargetOptions, type NetworkTargetOption } from "../networkTargets";
import { deriveProvisionNaming, previewHostnamePattern } from "../provisionNaming";
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
  readonly osdeploy_credentials: readonly OsdeployCredential[];
  readonly bubbles: readonly BubbleOption[];
}

interface OsdeployCredential {
  readonly id?: string | number;
  readonly name?: string;
  readonly type?: string;
}

interface BubbleOption {
  readonly id?: string | number;
  readonly name?: string;
  readonly domain_name?: string;
  readonly netbios_name?: string;
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

type ProvisionFormSnapshot = Readonly<Record<string, string | readonly string[]>>;

interface ProvisionTemplate {
  readonly name: string;
  readonly savedAt: string;
  readonly fields: ProvisionFormSnapshot;
}

type ProvisionTemplateMap = Readonly<Record<string, ProvisionTemplate>>;

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
  ubuntu_v2_sequences: [],
  osdeploy_credentials: [],
  bubbles: []
};

const PROVISION_TEMPLATE_STORAGE_KEY = "pveautopilot.provision.templates.v1";
const PROVISION_TEMPLATE_SEED_STORAGE_KEY = "pveautopilot.provision.templates.seeded.v1";
const PROVISION_DRAFT_STORAGE_KEY = "pveautopilot.provision.draft.v1";
const BUILT_IN_TEMPLATE_SAVED_AT = "built-in";
const DEFAULT_PROVISION_TEMPLATES: ProvisionTemplateMap = {
  "Ring 0 Ivy24 Dell OSDCloud": {
    name: "Ring 0 Ivy24 Dell OSDCloud",
    savedAt: BUILT_IN_TEMPLATE_SAVED_AT,
    fields: {
      boot_mode: "cloudosd",
      run_tag: "ring0ivy24",
      group_tag: "ring0ivy24",
      profile: "dell-precision-3591",
      chassis_type_override: "0",
      count: "4",
      hostname_pattern: "ring0ivy24-{index}",
      cores: "4",
      memory_mb: "8192",
      disk_size_gb: "256",
      serial_prefix: "ring0",
      node: "pve2",
      network_bridge: "vmbr0",
      os_version: "Windows 11 25H2",
      os_edition: "Enterprise",
      os_activation: "Volume",
      os_language: "en-us",
      iso_storage: "isos",
      storage: "ssdpool",
      driver_pack_policy: "None",
      outbound_policy_mode: "blocked",
      tpm_enabled: "on",
      secure_boot: "on"
    }
  },
  "Single Desktop OSDCloud Test": {
    name: "Single Desktop OSDCloud Test",
    savedAt: BUILT_IN_TEMPLATE_SAVED_AT,
    fields: {
      boot_mode: "cloudosd",
      run_tag: "desktop-test",
      group_tag: "desktop-test",
      count: "1",
      hostname_pattern: "dt-{index}",
      cores: "4",
      memory_mb: "8192",
      disk_size_gb: "128",
      serial_prefix: "test",
      node: "pve2",
      network_bridge: "vmbr0",
      os_version: "Windows 11 25H2",
      os_edition: "Enterprise",
      os_activation: "Volume",
      os_language: "en-us",
      iso_storage: "isos",
      storage: "ssdpool",
      driver_pack_policy: "None",
      outbound_policy_mode: "blocked",
      tpm_enabled: "on",
      secure_boot: "on"
    }
  },
  "Server 2025 OSDeploy Base": {
    name: "Server 2025 OSDeploy Base",
    savedAt: BUILT_IN_TEMPLATE_SAVED_AT,
    fields: {
      boot_mode: "osdeploy",
      run_tag: "server2025",
      group_tag: "server2025",
      count: "1",
      hostname_pattern: "srv25-{index}",
      cores: "4",
      memory_mb: "8192",
      disk_size_gb: "160",
      serial_prefix: "srv",
      osdeploy_server_role: "base",
      osdeploy_node: "pve2",
      osdeploy_iso_storage: "isos",
      osdeploy_storage: "ssdpool",
      osdeploy_network_bridge: "vmbr0",
      osdeploy_os_version: "Windows Server 2025",
      osdeploy_os_edition: "Datacenter",
      osdeploy_os_language: "en-us",
      outbound_policy_mode: "blocked"
    }
  }
};

function asRecord(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Readonly<Record<string, unknown>> : {};
}

function asNumber(value: unknown, fallback: number): number {
  const numberValue = typeof value === "number" ? value : Number(value);
  return Number.isFinite(numberValue) ? numberValue : fallback;
}

function readStorageJson<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) {
      return fallback;
    }
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function readProvisionTemplates(): ProvisionTemplateMap {
  try {
    const raw = window.localStorage.getItem(PROVISION_TEMPLATE_STORAGE_KEY);
    const savedTemplates = raw ? JSON.parse(raw) as ProvisionTemplateMap : {};
    if (window.localStorage.getItem(PROVISION_TEMPLATE_SEED_STORAGE_KEY) === "1") {
      return savedTemplates;
    }
    const seededTemplates = { ...DEFAULT_PROVISION_TEMPLATES, ...savedTemplates };
    writeStorageJson(PROVISION_TEMPLATE_STORAGE_KEY, seededTemplates);
    window.localStorage.setItem(PROVISION_TEMPLATE_SEED_STORAGE_KEY, "1");
    return seededTemplates;
  } catch {
    return DEFAULT_PROVISION_TEMPLATES;
  }
}

function writeStorageJson(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    return;
  }
}

function isBootMode(value: string): value is BootMode {
  return ["cloudosd", "osdeploy", "ubuntu", "winpe", "clone"].includes(value);
}

function snapshotForm(form: HTMLFormElement): ProvisionFormSnapshot {
  const snapshot: Record<string, string | string[]> = {};
  const data = new FormData(form);
  data.forEach((value, key) => {
    const text = typeof value === "string" ? value : value.name;
    const existing = snapshot[key];
    if (Array.isArray(existing)) {
      existing.push(text);
    } else if (existing !== undefined) {
      snapshot[key] = [existing, text];
    } else {
      snapshot[key] = text;
    }
  });
  return snapshot;
}

function valuesForField(snapshot: ProvisionFormSnapshot, name: string): readonly string[] {
  const value = snapshot[name];
  if (Array.isArray(value)) {
    return value.map(String);
  }
  return value === undefined ? [] : [String(value)];
}

function firstValueForField(snapshot: ProvisionFormSnapshot, name: string): string {
  return valuesForField(snapshot, name)[0] ?? "";
}

function applyFormSnapshot(form: HTMLFormElement, snapshot: ProvisionFormSnapshot): void {
  const controls = form.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(
    "input[name], select[name], textarea[name]"
  );
  controls.forEach((control) => {
    const name = control.name;
    const values = valuesForField(snapshot, name);
    if (control instanceof HTMLInputElement && control.type === "checkbox") {
      control.checked = values.includes(control.value || "on");
      return;
    }
    if (control instanceof HTMLInputElement && control.type === "radio") {
      control.checked = values.includes(control.value);
      return;
    }
    if (control instanceof HTMLSelectElement && control.multiple) {
      Array.from(control.options).forEach((option) => {
        option.selected = values.includes(option.value);
      });
      control.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }
    const next = firstValueForField(snapshot, name);
    if (next || Object.hasOwn(snapshot, name)) {
      control.value = next;
      control.dispatchEvent(new Event("input", { bubbles: true }));
      control.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });
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
    ubuntu_v2_sequences: Array.isArray(record.ubuntu_v2_sequences) ? record.ubuntu_v2_sequences as readonly UbuntuSequence[] : [],
    osdeploy_credentials: Array.isArray(record.osdeploy_credentials) ? record.osdeploy_credentials as readonly OsdeployCredential[] : [],
    bubbles: Array.isArray(record.bubbles) ? record.bubbles as readonly BubbleOption[] : []
  };
}

function SelectField({
  label,
  name,
  value,
  defaultValue,
  onChange,
  options,
  help,
  required
}: {
  readonly label: string;
  readonly name: string;
  readonly value?: string | undefined;
  readonly defaultValue?: string | undefined;
  readonly onChange?: (value: string) => void;
  readonly options: readonly { readonly value: string; readonly label: string }[];
  readonly help?: string;
  readonly required?: boolean;
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
        required={required}
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
  value,
  onChange,
  readOnly,
  maxLength,
  help
}: {
  readonly label: string;
  readonly name: string;
  readonly defaultValue?: string | number | undefined;
  readonly value?: string | number | undefined;
  readonly onChange?: (value: string) => void;
  readonly readOnly?: boolean;
  readonly maxLength?: number | undefined;
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
        type="text"
        value={value === undefined ? undefined : textValue(value, "")}
        defaultValue={value === undefined ? textValue(defaultValue, "") : undefined}
        readOnly={readOnly}
        maxLength={maxLength}
        aria-describedby={help ? helpId : undefined}
        onChange={onChange ? (event) => { onChange(event.currentTarget.value); } : undefined}
      />
      {help ? <small id={helpId}>{help}</small> : null}
    </div>
  );
}

function NumberField({
  label,
  name,
  defaultValue,
  value,
  onChange,
  readOnly,
  maxLength,
  min,
  max,
  step,
  help
}: {
  readonly label: string;
  readonly name: string;
  readonly defaultValue?: string | number | undefined;
  readonly value?: string | number | undefined;
  readonly onChange?: (value: string) => void;
  readonly readOnly?: boolean;
  readonly maxLength?: number | undefined;
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
        value={value === undefined ? undefined : value}
        defaultValue={value === undefined ? textValue(defaultValue, "") : undefined}
        readOnly={readOnly}
        maxLength={maxLength}
        min={min}
        max={max}
        step={step}
        aria-describedby={help ? helpId : undefined}
        onChange={onChange ? (event) => { onChange(event.currentTarget.value); } : undefined}
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

const BOOT_MODE_OPTIONS: readonly { readonly value: BootMode; readonly label: string; readonly summary: string }[] = [
  { value: "cloudosd", label: "OSDCloud", summary: "Desktop CloudOSD path" },
  { value: "osdeploy", label: "OSDeploy v2", summary: "Windows Server path" },
  { value: "ubuntu", label: "Ubuntu v2", summary: "Linux v2 sequence path" },
  { value: "winpe", label: "Legacy WinPE", summary: "Classic WinPE path" },
  { value: "clone", label: "Clone", summary: "Template clone fallback" }
];

function bootModeLabel(mode: BootMode): string {
  return BOOT_MODE_OPTIONS.find((option) => option.value === mode)?.label ?? mode;
}

function BootPathRail({
  bootMode,
  onChange
}: {
  readonly bootMode: BootMode;
  readonly onChange: (mode: BootMode) => void;
}) {
  return (
    <Panel title="Boot Path">
      <input type="hidden" name="boot_mode" value={bootMode} />
      <div className="provision-boot-rail" role="radiogroup" aria-label="Boot mode">
        {BOOT_MODE_OPTIONS.map((option) => (
          <label key={option.value} className="provision-boot-option">
            <input
              type="radio"
              aria-label={option.label}
              value={option.value}
              checked={bootMode === option.value}
              onChange={() => { onChange(option.value); }}
            />
            <span>{option.label}</span>
            <small>{option.summary}</small>
          </label>
        ))}
      </div>
    </Panel>
  );
}

function RunTagComposer({
  runTag,
  groupTag,
  bootMode,
  previewName,
  previewLength,
  previewLimit,
  previewSafe,
  normalizedPreviewName,
  onRunTagChange
}: {
  readonly runTag: string;
  readonly groupTag: string;
  readonly bootMode: BootMode;
  readonly previewName: string;
  readonly previewLength: number;
  readonly previewLimit: number;
  readonly previewSafe: boolean;
  readonly normalizedPreviewName: string;
  readonly onRunTagChange: (value: string) => void;
}) {
  const runTagId = useId();
  const helpId = `${runTagId}-help`;
  return (
    <Panel title="Run Tag">
      <div className="utility-field-grid provision-run-tag-grid">
        <div className="utility-field">
          <label htmlFor={runTagId}>Run tag</label>
          <input
            id={runTagId}
            name="run_tag"
            type="text"
            value={runTag}
            aria-describedby={helpId}
            onChange={(event) => { onRunTagChange(event.currentTarget.value); }}
          />
          <small id={helpId}>Fills Group tag and, unless manually changed, the hostname pattern.</small>
        </div>
        <div className="utility-field">
          <span>Preview name</span>
          <strong className={`provision-hostname-preview${previewSafe ? "" : " provision-hostname-preview--unsafe"}`}>{previewName}</strong>
          <small className={previewSafe ? undefined : "provision-hostname-warning"}>{previewLength} / {previewLimit}</small>
          {!previewSafe ? (
            <small className="provision-hostname-warning" role="alert">
              Exceeds the Windows/Intune 15-character limit. Normalized preview: {normalizedPreviewName}.
            </small>
          ) : null}
        </div>
        <div className="utility-field">
          <span>Group tag preview</span>
          <strong>{groupTag || "none"}</strong>
          <small>Submitted as group_tag</small>
        </div>
        <div className="utility-field">
          <span>Active boot path</span>
          <strong>{bootModeLabel(bootMode)}</strong>
          <small>Submitted as boot_mode</small>
        </div>
      </div>
    </Panel>
  );
}

function LaunchEssentials({
  payload,
  profileOptions,
  activeDefaults,
  templateMinimum,
  hostnamePattern,
  hostnamePreview,
  vmCount,
  onHostnamePatternChange,
  onVmCountChange,
  onResetHostname
}: {
  readonly payload: ProvisionPagePayload;
  readonly profileOptions: readonly { readonly value: string; readonly label: string }[];
  readonly activeDefaults: Readonly<Record<string, string | number | boolean | null | undefined>>;
  readonly templateMinimum: number;
  readonly hostnamePattern: string;
  readonly hostnamePreview: ReturnType<typeof previewHostnamePattern>;
  readonly vmCount: number;
  readonly onHostnamePatternChange: (value: string) => void;
  readonly onVmCountChange: (value: string) => void;
  readonly onResetHostname: () => void;
}) {
  const defaults = payload.defaults;
  const hostnameHelp = hostnamePreview.safe
    ? "Tokens: {serial}, {vmid}, {index}."
    : `Exceeds the Windows/Intune 15-character limit. Reset from run tag or shorten the pattern. Normalized preview: ${hostnamePreview.normalizedName}.`;
  return (
    <Panel title="Launch Essentials">
      <div className="utility-field-grid">
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
        <NumberField label="VM count" name="count" value={vmCount} onChange={onVmCountChange} min={1} max={50} />
        <div className="utility-field provision-hostname-field">
          <TextField
            label="Hostname pattern"
            name="hostname_pattern"
            value={hostnamePattern}
            onChange={onHostnamePatternChange}
            help={hostnameHelp}
          />
          <button className="utility-button provision-icon-button" type="button" aria-label="Reset hostname from run tag" onClick={onResetHostname}>
            <RefreshCw aria-hidden="true" size={16} />
          </button>
          {!hostnamePreview.safe ? (
            <p className="notice notice--warn provision-hostname-warning" role="alert">
              Provisioning is blocked until the hostname preview is 15 characters or fewer.
            </p>
          ) : null}
        </div>
        <NumberField label="CPU cores" name="cores" defaultValue={textValue(defaults.cores ?? activeDefaults.vm_cores, "")} min={1} max={64} />
        <NumberField label="Memory MB" name="memory_mb" defaultValue={textValue(defaults.memory_mb ?? activeDefaults.vm_memory_mb, "")} min={asNumber(activeDefaults.minimum_vm_memory_mb, 512)} step={512} />
        <NumberField label="Disk GB" name="disk_size_gb" defaultValue={textValue(defaults.disk_size_gb ?? activeDefaults.vm_disk_size_gb, "")} min={Math.max(templateMinimum, asNumber(activeDefaults.minimum_vm_disk_size_gb, 1))} max={2048} />
        <TextField label="Serial prefix" name="serial_prefix" defaultValue={defaults.serial_prefix} />
      </div>
    </Panel>
  );
}

function AutopilotEnrollmentPanel({
  bootMode,
  groupTag,
  onGroupTagChange
}: {
  readonly bootMode: BootMode;
  readonly groupTag: string;
  readonly onGroupTagChange: (value: string) => void;
}) {
  const isUbuntu = bootMode === "ubuntu";
  const status = isUbuntu
    ? "Not a Windows Autopilot hash capture path"
    : "Windows Autopilot hash capture path";
  return (
    <Panel title="Autopilot Enrollment">
      <div className="utility-field-grid">
        <TextField label="Group tag" name="group_tag" value={groupTag} onChange={onGroupTagChange} />
        <div className="utility-field utility-field--wide">
          <span>Hash capture</span>
          <div className="provision-hash-capture-stack">
            <span className={isUbuntu ? "status-pill status--active" : "status-pill status--good"}>{status}</span>
            <small>{isUbuntu ? "Ubuntu can keep the submitted group tag for backend compatibility." : "CloudOSD and Windows paths stage hash capture for Autopilot enrollment."}</small>
          </div>
        </div>
      </div>
    </Panel>
  );
}

function ArtifactReadiness({
  label,
  artifact
}: {
  readonly label: string;
  readonly artifact: CloudosdArtifact | OsdeployArtifact | undefined;
}) {
  const ready = Boolean(artifact?.ready);
  const build = textValue(artifact?.build_sha ?? artifact?.id, "none");
  return (
    <div className="utility-field utility-field--wide">
      <span>{label}</span>
      <div className="provision-artifact-readiness">
        <span className={ready ? "status-pill status--good" : "status-pill status--active"}>{textValue(artifact?.readiness, ready ? "ready" : "not ready")}</span>
        <code>{build}</code>
        {artifact?.proxmox_volid ? <small>{artifact.proxmox_volid}</small> : null}
      </div>
    </div>
  );
}

function CloudosdDesktopPanel({ payload }: { readonly payload: ProvisionPagePayload }) {
  const target = targetOptions(payload.cloudosd_options);
  const catalogDefaults = payload.cloudosd_catalog.defaults ?? {};
  return (
    <Panel title="OSDCloud Desktop">
      <div className="utility-field-grid">
        <SelectField label="Node" name="node" defaultValue={payload.cloudosd_options.defaults?.node} options={target.nodes} />
        <SelectField label="Network target" name="network_bridge" defaultValue={payload.cloudosd_options.defaults?.bridge} options={target.networkTargets} />
        <SelectField label="OS version" name="os_version" defaultValue={textValue(catalogDefaults.os_version, "")} options={optionsFrom(payload.cloudosd_catalog.os_versions, textValue(catalogDefaults.os_version, ""))} />
        <SelectField label="OS edition" name="os_edition" defaultValue={textValue(catalogDefaults.os_edition, "")} options={optionsFrom(payload.cloudosd_catalog.os_editions, textValue(catalogDefaults.os_edition, ""))} />
        <SelectField label="OS activation" name="os_activation" defaultValue={textValue(catalogDefaults.os_activation, "")} options={optionsFrom(payload.cloudosd_catalog.os_activations, textValue(catalogDefaults.os_activation, ""))} />
        <SelectField label="OS language" name="os_language" defaultValue={textValue(catalogDefaults.os_language, "")} options={optionsFrom(payload.cloudosd_catalog.os_languages, textValue(catalogDefaults.os_language, ""))} />
      </div>
    </Panel>
  );
}

function AdvancedCloudosdOptions({ payload }: { readonly payload: ProvisionPagePayload }) {
  const target = targetOptions(payload.cloudosd_options);
  const catalogDefaults = payload.cloudosd_catalog.defaults ?? {};
  const artifact = payload.cloudosd_ready_artifacts[0] ?? payload.cloudosd_artifacts[0];
  return (
    <Panel title="Advanced OSDCloud Options">
      <details className="provision-advanced-options" open>
        <summary>Advanced OSDCloud Options</summary>
        <div className="utility-field-grid">
          <SelectField label="ISO storage" name="iso_storage" defaultValue={payload.cloudosd_options.defaults?.iso_storage} options={target.isoStorages} />
          <SelectField label="Disk storage" name="storage" defaultValue={payload.cloudosd_options.defaults?.disk_storage} options={target.diskStorages} />
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
          <ArtifactReadiness label="Artifact readiness" artifact={artifact} />
        </div>
      </details>
    </Panel>
  );
}

function OsdeployServerPanel({ payload }: { readonly payload: ProvisionPagePayload }) {
  const target = targetOptions(payload.osdeploy_options);
  const catalogDefaults = payload.osdeploy_catalog.defaults ?? {};
  const artifact = payload.osdeploy_ready_artifacts[0] ?? payload.osdeploy_artifacts[0];
  const [role, setRole] = useState("base");
  const [dcMode, setDcMode] = useState("new_forest");
  const [forestFqdn, setForestFqdn] = useState("");
  const [netbios, setNetbios] = useState("");
  const forestId = useId();
  const netbiosId = useId();
  const isDc = role === "isolated_domain_controller";
  const existing = dcMode === "additional_dc";
  const credentialOptions = [
    { value: "", label: "select credential" },
    ...payload.osdeploy_credentials.map((cred) => ({
      value: textValue(cred.id, ""),
      label: [cred.name, cred.type].filter(Boolean).join(" / ") || textValue(cred.id)
    }))
  ];
  const bubbleOptions = [
    { value: "", label: "(none)" },
    ...payload.bubbles.map((bubble) => ({
      value: textValue(bubble.id, ""),
      label: textValue(bubble.name, textValue(bubble.domain_name, "bubble"))
    }))
  ];
  const applyBubble = (bubbleId: string) => {
    const bubble = payload.bubbles.find((item) => textValue(item.id, "") === bubbleId);
    if (bubble) {
      setForestFqdn(textValue(bubble.domain_name, ""));
      setNetbios(textValue(bubble.netbios_name, ""));
    }
  };
  return (
    <Panel title="OSDeploy Server">
      <div className="utility-field-grid">
        <SelectField label="Server role" name="osdeploy_server_role" value={role} onChange={setRole} options={optionsFrom(payload.osdeploy_catalog.server_roles, "base")} />
        <SelectField label="OSDeploy node" name="osdeploy_node" defaultValue={payload.osdeploy_options.defaults?.node} options={target.nodes} />
        <SelectField label="OSDeploy ISO storage" name="osdeploy_iso_storage" defaultValue={payload.osdeploy_options.defaults?.iso_storage} options={target.isoStorages} />
        <SelectField label="OSDeploy disk storage" name="osdeploy_storage" defaultValue={payload.osdeploy_options.defaults?.disk_storage} options={target.diskStorages} />
        <SelectField label="OSDeploy network target" name="osdeploy_network_bridge" defaultValue={payload.osdeploy_options.defaults?.bridge} options={target.networkTargets} />
        <SelectField label="OSDeploy OS version" name="osdeploy_os_version" defaultValue={textValue(catalogDefaults.os_version, "")} options={optionsFrom(payload.osdeploy_catalog.os_versions, textValue(catalogDefaults.os_version, ""))} />
        <SelectField label="OSDeploy OS edition" name="osdeploy_os_edition" defaultValue={textValue(catalogDefaults.os_edition, "")} options={optionsFrom(payload.osdeploy_catalog.os_editions, textValue(catalogDefaults.os_edition, ""))} />
        <SelectField label="OSDeploy OS language" name="osdeploy_os_language" defaultValue={textValue(catalogDefaults.os_language, "")} options={optionsFrom(payload.osdeploy_catalog.os_languages, textValue(catalogDefaults.os_language, ""))} />
        {isDc ? (
          <>
            <SelectField
              label="Domain mode"
              name="osdeploy_dc_mode"
              value={dcMode}
              onChange={setDcMode}
              options={[
                { value: "new_forest", label: "New forest / domain" },
                { value: "additional_dc", label: "Additional DC (existing domain)" }
              ]}
              help={existing ? "Replica DC joined to an existing domain (needs DNS to that domain)." : "Stands up a fresh isolated forest."}
            />
            {payload.bubbles.length ? (
              <SelectField
                label="Prefill from bubble"
                name="osdeploy_bubble_prefill"
                defaultValue=""
                onChange={applyBubble}
                options={bubbleOptions}
                help="Fills domain FQDN + NetBIOS from the selected bubble."
              />
            ) : null}
            <div className="utility-field">
              <label htmlFor={forestId}>{existing ? "Existing domain FQDN" : "Forest FQDN"}</label>
              <input id={forestId} name="osdeploy_role_forest_fqdn" required value={forestFqdn} onChange={(event) => { setForestFqdn(event.currentTarget.value); }} placeholder="lab.gell.one" />
            </div>
            <div className="utility-field">
              <label htmlFor={netbiosId}>NetBIOS name</label>
              <input id={netbiosId} name="osdeploy_role_netbios_name" required value={netbios} onChange={(event) => { setNetbios(event.currentTarget.value); }} placeholder="LAB" />
            </div>
            <SelectField label={existing ? "Domain admin credential" : "Forest admin credential"} name="osdeploy_role_forest_admin_credential_id" defaultValue="" options={credentialOptions} required />
            <SelectField label="DSRM credential" name="osdeploy_role_dsrm_credential_id" defaultValue="" options={credentialOptions} required />
          </>
        ) : null}
      </div>
      <details className="provision-advanced-options">
        <summary>Advanced OSDeploy Options</summary>
        <div className="utility-field-grid">
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
          <ArtifactReadiness label="OSDeploy artifact readiness" artifact={artifact} />
        </div>
      </details>
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

function LaunchReviewRail({
  payload,
  vmCount,
  previewName
}: {
  readonly payload: ProvisionPagePayload;
  readonly vmCount: number;
  readonly previewName: string;
}) {
  return (
    <Panel title="Launch Review">
      <section className="metric-strip provision-review-metrics" aria-label="Launch review">
        <Metric label="Template VMID" value={textValue(payload.defaults.template_vmid)} />
        <Metric label="Count" value={String(vmCount)} />
        <Metric label="Hostname Preview" value={previewName} />
        <Metric label="Cloud Artifacts" value={String(payload.cloudosd_artifacts.length)} />
      </section>
    </Panel>
  );
}

function initialHostnamePattern(defaultGroupTag: string, defaultPattern: string): string {
  if (defaultPattern && previewHostnamePattern(defaultPattern).safe) {
    return defaultPattern;
  }

  return deriveProvisionNaming(defaultGroupTag).hostnamePattern;
}

export function ProvisionPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [payload, setPayload] = useState<ProvisionPagePayload>(EMPTY_PAYLOAD);
  const [initialDraft] = useState(() => readStorageJson<{ readonly fields?: ProvisionFormSnapshot } | null>(PROVISION_DRAFT_STORAGE_KEY, null));
  const [bootMode, setBootMode] = useState<BootMode>(() => {
    const draftBootMode = initialDraft?.fields ? firstValueForField(initialDraft.fields, "boot_mode") : "";
    return isBootMode(draftBootMode) ? draftBootMode : "cloudosd";
  });
  const [runTag, setRunTag] = useState("");
  const [groupTag, setGroupTag] = useState("");
  const [hostnamePattern, setHostnamePattern] = useState("ap-{index}");
  const [hostnameIsManual, setHostnameIsManual] = useState(false);
  const [vmCount, setVmCount] = useState(1);
  const [defaultsApplied, setDefaultsApplied] = useState(false);
  const [templates, setTemplates] = useState<ProvisionTemplateMap>(readProvisionTemplates);
  const [selectedTemplate, setSelectedTemplate] = useState("");
  const [templateName, setTemplateName] = useState("");
  const [templateMessage, setTemplateMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const formRef = useRef<HTMLFormElement | null>(null);
  const draftAppliedRef = useRef(false);

  const load = useCallback(async () => {
    try {
      const nextPayload = provisionPayloadFromUnknown(await fetchJson<unknown>("/api/provision/page"));
      setPayload(nextPayload);
      if (!defaultsApplied) {
        const defaults = nextPayload.defaults;
        const defaultGroupTag = textValue(defaults.group_tag, "");
        setRunTag(defaultGroupTag);
        setGroupTag(defaultGroupTag);
        setHostnamePattern(initialHostnamePattern(defaultGroupTag, textValue(defaults.hostname_pattern, "")));
        setHostnameIsManual(false);
        setVmCount(asNumber(defaults.count, 1));
        setDefaultsApplied(true);
      }
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load provision payload");
    } finally {
      setLoading(false);
    }
  }, [defaultsApplied]);

  usePolling(load);

  const templateOptions = useMemo(() => Object.values(templates).sort((a, b) => a.name.localeCompare(b.name)), [templates]);

  const persistDraft = useCallback(() => {
    if (!formRef.current) {
      return;
    }
    writeStorageJson(PROVISION_DRAFT_STORAGE_KEY, {
      savedAt: new Date().toISOString(),
      fields: snapshotForm(formRef.current)
    });
  }, []);

  const syncControlledFieldsFromSnapshot = useCallback((fields: ProvisionFormSnapshot) => {
    const nextRunTag = firstValueForField(fields, "run_tag");
    const nextGroupTag = firstValueForField(fields, "group_tag");
    if (Object.hasOwn(fields, "run_tag") || Object.hasOwn(fields, "group_tag")) {
      setRunTag(nextRunTag || nextGroupTag);
    }
    if (Object.hasOwn(fields, "group_tag")) {
      setGroupTag(nextGroupTag);
    }
    if (Object.hasOwn(fields, "hostname_pattern")) {
      setHostnamePattern(firstValueForField(fields, "hostname_pattern"));
      setHostnameIsManual(true);
    }
    if (Object.hasOwn(fields, "count")) {
      setVmCount(asNumber(firstValueForField(fields, "count"), 1));
    }
    const nextBootMode = firstValueForField(fields, "boot_mode");
    if (isBootMode(nextBootMode)) {
      setBootMode(nextBootMode);
    }
  }, []);

  const applySavedFields = useCallback((fields: ProvisionFormSnapshot) => {
    syncControlledFieldsFromSnapshot(fields);
    window.setTimeout(() => {
      if (!formRef.current) {
        return;
      }
      applyFormSnapshot(formRef.current, fields);
      persistDraft();
    }, 0);
  }, [persistDraft, syncControlledFieldsFromSnapshot]);

  const applySavedFieldsImmediately = useCallback((fields: ProvisionFormSnapshot) => {
    syncControlledFieldsFromSnapshot(fields);
    if (!formRef.current) {
      return;
    }
    applyFormSnapshot(formRef.current, fields);
    persistDraft();
  }, [persistDraft, syncControlledFieldsFromSnapshot]);

  const saveTemplate = useCallback(() => {
    if (!formRef.current) {
      return;
    }
    const name = templateName.trim() || selectedTemplate.trim();
    if (!name) {
      setTemplateMessage("Name the template before saving.");
      return;
    }
    const next: ProvisionTemplateMap = {
      ...templates,
      [name]: {
        name,
        savedAt: new Date().toISOString(),
        fields: snapshotForm(formRef.current)
      }
    };
    setTemplates(next);
    setSelectedTemplate(name);
    setTemplateName(name);
    setTemplateMessage(`Saved ${name}.`);
    writeStorageJson(PROVISION_TEMPLATE_STORAGE_KEY, next);
  }, [selectedTemplate, templateName, templates]);

  const loadTemplate = useCallback(() => {
    const template = templates[selectedTemplate];
    if (!template) {
      setTemplateMessage("Select a template to load.");
      return;
    }
    const nextBootMode = firstValueForField(template.fields, "boot_mode");
    if (isBootMode(nextBootMode) && nextBootMode !== bootMode) {
      setBootMode(nextBootMode);
      applySavedFields(template.fields);
    } else {
      applySavedFieldsImmediately(template.fields);
    }
    setTemplateName(template.name);
    setTemplateMessage(`Loaded ${template.name}.`);
  }, [applySavedFields, applySavedFieldsImmediately, bootMode, selectedTemplate, templates]);

  const deleteTemplate = useCallback(() => {
    if (!selectedTemplate || !templates[selectedTemplate]) {
      setTemplateMessage("Select a template to delete.");
      return;
    }
    const next = Object.fromEntries(Object.entries(templates).filter(([name]) => name !== selectedTemplate)) as ProvisionTemplateMap;
    setTemplates(next);
    setSelectedTemplate("");
    setTemplateName("");
    setTemplateMessage(`Deleted ${selectedTemplate}.`);
    writeStorageJson(PROVISION_TEMPLATE_STORAGE_KEY, next);
  }, [selectedTemplate, templates]);

  useEffect(() => {
    if (loading || draftAppliedRef.current || !formRef.current) {
      return;
    }
    draftAppliedRef.current = true;
    const fields = initialDraft?.fields;
    if (fields) {
      window.setTimeout(() => {
        applySavedFields(fields);
      }, 0);
    }
  }, [applySavedFields, initialDraft, loading]);

  const profileOptions = useMemo(() => {
    const rows = Object.entries(payload.profiles).map(([key, profile]) => ({
      value: key,
      label: `${key} - ${textValue(profile.manufacturer)} ${textValue(profile.product)}`.trim()
    }));
    return rows.length ? rows : [{ value: "", label: "No OEM profiles" }];
  }, [payload.profiles]);

  const cloudosdDefaults = payload.cloudosd_catalog.defaults ?? {};
  const osdeployDefaults = payload.osdeploy_catalog.defaults ?? {};
  const activeDefaults = bootMode === "osdeploy" ? osdeployDefaults : cloudosdDefaults;
  const templateMinimum = payload.template_disk_gb ?? 1;
  const hostnamePreview = previewHostnamePattern(hostnamePattern);
  const applyRunTag = useCallback((value: string) => {
    setRunTag(value);
    setGroupTag(value);
    if (!hostnameIsManual) {
      setHostnamePattern(deriveProvisionNaming(value).hostnamePattern);
    }
  }, [hostnameIsManual]);
  const updateHostnamePattern = useCallback((value: string) => {
    setHostnamePattern(value);
    setHostnameIsManual(true);
  }, []);
  const resetHostnameFromRunTag = useCallback(() => {
    setHostnamePattern(deriveProvisionNaming(runTag).hostnamePattern);
    setHostnameIsManual(false);
  }, [runTag]);
  const updateVmCount = useCallback((value: string) => {
    setVmCount(asNumber(value, 1));
  }, []);

  return (
    <PageFrame bootstrap={bootstrap} title="Provision" section="Deploy" path="/react/provision">
      {loading ? (
        <div className="load-strip" role="status" aria-live="polite">
          <span>Loading Provision</span>
          <div className="load-strip__track" role="progressbar" aria-label="Provision loading"><span /></div>
        </div>
      ) : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}

      {!loading ? (
        <>
          <Panel title="Provision Templates">
            <div className="utility-field-grid provision-template-grid">
              <div className="utility-field">
                <label htmlFor="provision-template-select">Saved template</label>
                <select
                  id="provision-template-select"
                  value={selectedTemplate}
                  onChange={(event) => {
                    setSelectedTemplate(event.currentTarget.value);
                    if (event.currentTarget.value) {
                      setTemplateName(event.currentTarget.value);
                    }
                  }}
                >
                  <option value="">Select template</option>
                  {templateOptions.map((template) => (
                    <option key={template.name} value={template.name}>{template.name}</option>
                  ))}
                </select>
              </div>
              <div className="utility-field">
                <label htmlFor="provision-template-name">Template name</label>
                <input
                  id="provision-template-name"
                  value={templateName}
                  onChange={(event) => { setTemplateName(event.currentTarget.value); }}
                />
              </div>
              <div className="utility-row-actions provision-template-actions">
                <button type="button" className="utility-button" onClick={saveTemplate}>Save template</button>
                <button type="button" className="utility-button" onClick={loadTemplate}>Load template</button>
                <button type="button" className="utility-button utility-button--danger" onClick={deleteTemplate}>Delete template</button>
              </div>
            </div>
            {templateMessage ? <p className="muted provision-template-message" role="status">{templateMessage}</p> : null}
          </Panel>
          <form
            ref={formRef}
            className="utility-form provision-builder"
            method="post"
            action="/api/jobs/provision"
            data-testid="provision-builder-form"
            onChange={persistDraft}
            onSubmit={persistDraft}
          >
            <div className="provision-launch-grid">
              <div className="provision-section-stack">
                <RunTagComposer
                  runTag={runTag}
                  groupTag={groupTag}
                  bootMode={bootMode}
                  previewName={hostnamePreview.previewName}
                  previewLength={hostnamePreview.previewLength}
                  previewLimit={hostnamePreview.limit}
                  previewSafe={hostnamePreview.safe}
                  normalizedPreviewName={hostnamePreview.normalizedName}
                  onRunTagChange={applyRunTag}
                />
                <BootPathRail bootMode={bootMode} onChange={setBootMode} />
              </div>

              <div className="provision-enrollment-stack">
                <LaunchEssentials
                  payload={payload}
                  profileOptions={profileOptions}
                  activeDefaults={activeDefaults}
                  templateMinimum={templateMinimum}
                  hostnamePattern={hostnamePattern}
                  hostnamePreview={hostnamePreview}
                  vmCount={vmCount}
                  onHostnamePatternChange={updateHostnamePattern}
                  onVmCountChange={updateVmCount}
                  onResetHostname={resetHostnameFromRunTag}
                />
                {bootMode === "cloudosd" ? (
                  <>
                    <CloudosdDesktopPanel payload={payload} />
                    <AdvancedCloudosdOptions payload={payload} />
                  </>
                ) : null}
                {bootMode === "osdeploy" ? <OsdeployServerPanel payload={payload} /> : null}
                {bootMode === "ubuntu" ? <UbuntuSection payload={payload} /> : null}
              </div>

              <aside className="provision-review-column">
                <AutopilotEnrollmentPanel bootMode={bootMode} groupTag={groupTag} onGroupTagChange={setGroupTag} />
                <LegacyNotice mode={bootMode} winpeEnabled={Boolean(payload.winpe_enabled)} />
                <LaunchReviewRail payload={payload} vmCount={vmCount} previewName={hostnamePreview.previewName} />
                <div className="utility-form-actions provision-review-actions">
                  <button className="utility-button" type="submit" disabled={!hostnamePreview.safe}>Provision VMs</button>
                  <a className="utility-button" href="/react/cloudosd">OSDCloud</a>
                  <a className="utility-button" href="/react/osdeploy">OSDeploy</a>
                </div>
              </aside>
            </div>
          </form>

          <BatchProgress progress={payload.cloudosd_batch_progress} />
        </>
      ) : null}
    </PageFrame>
  );
}
