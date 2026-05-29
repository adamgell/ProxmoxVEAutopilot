import { Disc3, Hammer, Play, RefreshCw } from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import { fetchJson, postForm, postJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { bytesLabel, textValue } from "../utilityModels";

interface OemProfile {
  readonly manufacturer?: string;
  readonly product?: string;
}

interface UbuntuSequence {
  readonly id: number | string;
  readonly name: string;
  readonly target_os?: string;
}

interface UtnIsoRow {
  readonly name: string;
  readonly size_bytes?: number;
  readonly mtime?: number;
}

interface UtnVmRow {
  readonly uuid: string;
  readonly status: string;
  readonly name: string;
}

interface UtnIsoPayload {
  readonly iso_dir?: string;
  readonly isos?: readonly UtnIsoRow[];
  readonly note?: string;
  readonly warning?: string;
}

interface UtnVmPayload {
  readonly vms?: readonly UtnVmRow[];
  readonly note?: string;
  readonly error?: string;
}

interface TemplatePayload {
  readonly profiles: Readonly<Record<string, OemProfile>>;
  readonly ubuntu_sequences: readonly UbuntuSequence[];
  readonly hypervisor_type: string;
  readonly utm_iso_dir?: string;
}

interface JobActionResponse {
  readonly ok?: boolean;
  readonly job_id?: string;
  readonly error?: string;
}

interface BuildActionResponse {
  readonly ok?: boolean;
  readonly path?: string;
  readonly bytes?: number;
  readonly iso?: string;
  readonly error?: string;
}

type TemplateTarget = "windows" | "ubuntu";

function defaultPayload(): TemplatePayload {
  return {
    profiles: {},
    ubuntu_sequences: [],
    hypervisor_type: "proxmox",
    utm_iso_dir: ""
  };
}

function profileLabel(key: string, profile: OemProfile): string {
  const detail = [profile.manufacturer, profile.product].filter(Boolean).join(" ");
  return detail ? `${key} - ${detail}` : key;
}

function actionMessage(response: JobActionResponse | BuildActionResponse): string {
  if (response.error) {
    return response.error;
  }
  if ("job_id" in response && response.job_id) {
    return `started ${response.job_id}`;
  }
  if ("path" in response && response.path) {
    return `rebuilt ${response.path}${typeof response.bytes === "number" ? ` (${bytesLabel(response.bytes)})` : ""}`;
  }
  if ("iso" in response && response.iso) {
    return `rebuilt ${response.iso}`;
  }
  return response.ok === false ? "failed" : "complete";
}

export function TemplatePage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [payload, setPayload] = useState<TemplatePayload>(defaultPayload);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [target, setTarget] = useState<TemplateTarget>("windows");
  const [status, setStatus] = useState("");
  const [pending, setPending] = useState("");

  const load = useCallback(async () => {
    try {
      setPayload(await fetchJson<TemplatePayload>("/api/template/page"));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load template page");
    } finally {
      setLoading(false);
    }
  }, []);

  usePolling(load);

  const profileEntries = useMemo(() => Object.entries(payload.profiles), [payload.profiles]);
  const firstProfile = profileEntries[0]?.[0] ?? "";
  const ubuntuSequences = payload.ubuntu_sequences;

  const setActionResult = (response: JobActionResponse | BuildActionResponse) => {
    setStatus(actionMessage(response));
  };

  const runAnswerIsoRebuild = async () => {
    setPending("answer-iso");
    try {
      setActionResult(await postJson<BuildActionResponse>("/api/answer-iso/rebuild"));
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "Answer ISO rebuild failed");
    } finally {
      setPending("");
    }
  };

  const runUbuntuSeedRebuild = async () => {
    const sequenceId = (document.getElementById("ubuntu-template-sequence") as HTMLSelectElement | null)?.value ?? "";
    if (!sequenceId) {
      setStatus("Select a sequence first");
      return;
    }
    setPending("ubuntu-seed");
    try {
      setActionResult(await postJson<BuildActionResponse>(`/api/ubuntu/rebuild-seed-iso?sequence_id=${encodeURIComponent(sequenceId)}`));
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "Ubuntu seed rebuild failed");
    } finally {
      setPending("");
    }
  };

  const runUbuntuTemplateBuild = async () => {
    const sequenceId = (document.getElementById("ubuntu-template-sequence") as HTMLSelectElement | null)?.value ?? "";
    if (!sequenceId) {
      setStatus("Select a sequence first");
      return;
    }
    setPending("ubuntu-template");
    try {
      setActionResult(await postJson<JobActionResponse>(`/api/ubuntu/build-template?sequence_id=${encodeURIComponent(sequenceId)}`));
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "Ubuntu template build failed");
    } finally {
      setPending("");
    }
  };

  const submitTemplateForm = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    setPending(form.dataset.templateKind || "template");
    try {
      setActionResult(await postForm<JobActionResponse>("/api/jobs/template", new FormData(form)));
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "Template build failed");
    } finally {
      setPending("");
    }
  };

  return (
    <PageFrame bootstrap={bootstrap} title="Build Template" section="Build" path="/react/template">
      {loading ? (
        <div className="load-strip" role="status" aria-live="polite">
          <span>Loading Template</span>
          <div className="load-strip__track" role="progressbar" aria-label="Template loading"><span /></div>
        </div>
      ) : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      {status ? <p className="notice" role="status">{status}</p> : null}

      <section className="metric-strip" aria-label="Template metrics">
        <Metric label="Hypervisor" value={textValue(payload.hypervisor_type)} />
        <Metric label="OEM profiles" value={String(profileEntries.length)} />
        <Metric label="Ubuntu sequences" value={String(ubuntuSequences.length)} />
      </section>

      <Panel title="Template Flow">
        <div className="cockpit-lifecycle">
          <div className="cockpit-lifecycle-step">Select OS</div>
          <div className="cockpit-lifecycle-step">Stage ISO</div>
          <div className="cockpit-lifecycle-step">Install</div>
          <div className="cockpit-lifecycle-step">Pause Gate</div>
          <div className="cockpit-lifecycle-step">Seal Template</div>
        </div>
      </Panel>

      {!loading && payload.hypervisor_type === "utm" ? (
        <UtmTemplatePanel onSubmit={submitTemplateForm} pending={pending} />
      ) : null}
      {!loading && payload.hypervisor_type !== "utm" ? (
        <>
          <div className="segmented" role="group" aria-label="Template target">
            {(["windows", "ubuntu"] as const).map((item) => (
              <button
                key={item}
                type="button"
                className={target === item ? "is-active" : ""}
                aria-pressed={target === item}
                onClick={() => {
                  setTarget(item);
                }}
              >
                {item === "windows" ? "Windows" : "Ubuntu"}
              </button>
            ))}
          </div>

          {target === "windows" ? (
            <Panel title="Windows Template Builder">
              <form className="utility-form" data-testid="windows-template-form" data-template-kind="windows-template" onSubmit={(event) => { void submitTemplateForm(event); }}>
                <div className="utility-field-grid">
                  <label className="utility-field">
                    <span>OEM Profile</span>
                    <select name="profile" defaultValue={firstProfile}>
                      {profileEntries.map(([key, profile]) => (
                        <option key={key} value={key}>{profileLabel(key, profile)}</option>
                      ))}
                    </select>
                  </label>
                  <label className="utility-field">
                    <span>Pause before sysprep</span>
                    <input type="checkbox" name="pause_before_sysprep" value="on" aria-label="Pause before sysprep" />
                    <small>Machine-scoped installs only before resume.</small>
                  </label>
                </div>
                <div className="utility-form-actions">
                  <button className="utility-button" type="button" onClick={() => { void runAnswerIsoRebuild(); }} disabled={pending === "answer-iso"}>
                    <RefreshCw size={15} aria-hidden="true" /> Rebuild Answer ISO
                  </button>
                  <button className="utility-button" type="submit" disabled={!firstProfile || pending === "windows-template"}>
                    <Hammer size={15} aria-hidden="true" /> Build Template
                  </button>
                </div>
              </form>
            </Panel>
          ) : (
            <Panel title="Ubuntu Template Builder">
              {ubuntuSequences.length ? (
                <div className="utility-form">
                  <label className="utility-field">
                    <span>Sequence</span>
                    <select id="ubuntu-template-sequence" defaultValue={String(ubuntuSequences[0]?.id ?? "")}>
                      {ubuntuSequences.map((sequence) => (
                        <option key={String(sequence.id)} value={String(sequence.id)}>{sequence.name}</option>
                      ))}
                    </select>
                  </label>
                  <div className="utility-form-actions">
                    <button className="utility-button" type="button" onClick={() => { void runUbuntuSeedRebuild(); }} disabled={pending === "ubuntu-seed"}>
                      <Disc3 size={15} aria-hidden="true" /> Rebuild Ubuntu Seed ISO
                    </button>
                    <button className="utility-button" type="button" onClick={() => { void runUbuntuTemplateBuild(); }} disabled={pending === "ubuntu-template"}>
                      <Play size={15} aria-hidden="true" /> Build Ubuntu Template
                    </button>
                  </div>
                </div>
              ) : (
                <p className="empty">No Ubuntu sequences defined.</p>
              )}
            </Panel>
          )}
        </>
      ) : null}
    </PageFrame>
  );
}

function UtmTemplatePanel({
  onSubmit,
  pending
}: {
  readonly onSubmit: (event: React.FormEvent<HTMLFormElement>) => Promise<void>;
  readonly pending: string;
}) {
  const [osKind, setOsKind] = useState("windows11");
  const [isos, setIsos] = useState<UtnIsoPayload>({});
  const [vms, setVms] = useState<UtnVmPayload>({});

  const load = useCallback(async () => {
    const [isoPayload, vmPayload] = await Promise.all([
      fetchJson<UtnIsoPayload>("/api/utm/isos").catch((err: unknown) => ({ warning: err instanceof Error ? err.message : "ISO list unavailable", isos: [] })),
      fetchJson<UtnVmPayload>("/api/utm/vms").catch((err: unknown) => ({ error: err instanceof Error ? err.message : "UTM VM list unavailable", vms: [] }))
    ]);
    setIsos(isoPayload);
    setVms(vmPayload);
  }, []);

  usePolling(load);

  const defaultName = osKind === "windows_server" ? "winserver-template" : "win11-template";

  return (
    <section className="utility-settings-grid">
      <Panel title="UTM Template Builder">
        <form className="utility-form" data-template-kind="utm-template" onSubmit={(event) => { void onSubmit(event); }}>
          <div className="utility-field-grid">
            <label className="utility-field">
              <span>OS</span>
              <select name="vm_os_kind" value={osKind} onChange={(event) => {
                setOsKind(event.target.value);
              }}>
                <option value="windows11">Windows 11 ARM64</option>
                <option value="windows_server">Windows Server ARM64</option>
              </select>
            </label>
            <label className="utility-field">
              <span>Template Name</span>
              <input name="vm_name" pattern="[a-zA-Z0-9_-]{1,64}" defaultValue={defaultName} key={defaultName} required />
            </label>
            <label className="utility-field utility-field--wide">
              <span>ISO</span>
              <select name="utm_iso_name" defaultValue="">
                <option value="">{isos.isos?.length ? "Select ISO" : "No ISO selected"}</option>
                {(isos.isos ?? []).map((iso) => (
                  <option key={iso.name} value={iso.name}>{iso.name} ({bytesLabel(iso.size_bytes)})</option>
                ))}
              </select>
              <small>{textValue(isos.warning ?? isos.note ?? isos.iso_dir, "ISO list unavailable")}</small>
            </label>
            <label className="utility-field">
              <span>CPU Cores</span>
              <input type="number" name="vm_cpu_cores" defaultValue="4" min="1" max="32" />
            </label>
            <label className="utility-field">
              <span>RAM MB</span>
              <input type="number" name="vm_memory_mb" defaultValue="8192" min="2048" step="512" />
            </label>
            <label className="utility-field">
              <span>Disk GB</span>
              <input type="number" name="vm_disk_gb" defaultValue="80" min="40" max="2048" />
            </label>
          </div>
          <button className="utility-button" type="submit" disabled={pending === "utm-template"}>
            <Hammer size={15} aria-hidden="true" /> Build UTM Template
          </button>
        </form>
      </Panel>
      <Panel title="Existing UTM VMs">
        {vms.error || vms.note ? <p className={vms.error ? "notice notice--bad" : "muted"}>{vms.error ?? vms.note}</p> : null}
        {(vms.vms ?? []).length ? (
          <div className="table-wrap">
            <table className="jobs-table utility-table" aria-label="UTM VMs">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Status</th>
                  <th scope="col">UUID</th>
                </tr>
              </thead>
              <tbody>
                {(vms.vms ?? []).map((vm) => (
                  <tr key={vm.uuid}>
                    <td>{vm.name}</td>
                    <td>{vm.status}</td>
                    <td>{vm.uuid}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <p className="empty">No UTM VMs found.</p>}
      </Panel>
    </section>
  );
}
