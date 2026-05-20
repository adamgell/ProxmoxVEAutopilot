import { CopyPlus, Library, ListTree, Plus, Save, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { fetchJson, postJson, putJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { textValue } from "../utilityModels";
import { formatShortDateTime, statusClass, statusLabel } from "../viewModels";

interface V2Step {
  readonly id?: string;
  readonly client_id?: string;
  readonly parent_id?: string | null;
  readonly node_type?: string;
  readonly name?: string;
  readonly description?: string;
  readonly kind?: string;
  readonly phase?: string;
  readonly enabled?: boolean;
  readonly condition?: Readonly<Record<string, unknown>>;
  readonly variables?: Readonly<Record<string, unknown>>;
  readonly params?: Readonly<Record<string, unknown>>;
  readonly state?: string;
  readonly retry_count?: number;
  readonly retry_delay_seconds?: number;
  readonly timeout_seconds?: number | null;
  readonly reboot_behavior?: string;
  readonly continue_on_error?: boolean;
  readonly content_refs?: readonly string[];
}

interface V2Sequence {
  readonly id: string;
  readonly name: string;
  readonly description?: string;
  readonly enabled?: boolean;
  readonly target_os?: string;
  readonly step_count?: number;
  readonly current_version_id?: string | null;
  readonly updated_at?: string;
  readonly steps?: readonly V2Step[];
}

interface V2Run {
  readonly id: string;
  readonly sequence_name?: string;
  readonly sequence_version?: number;
  readonly state?: string;
  readonly phase?: string;
  readonly vmid?: number | null;
  readonly computer_name?: string;
  readonly serial_number?: string;
  readonly done_count?: number;
  readonly running_count?: number;
  readonly failed_count?: number;
  readonly step_count?: number;
  readonly manifest_count?: number;
  readonly started_at?: string;
  readonly steps?: readonly V2Step[];
}

interface ContentVersion {
  readonly version?: string;
  readonly source_uri?: string;
}

interface ContentItem {
  readonly id?: string;
  readonly name?: string;
  readonly description?: string;
  readonly content_type?: string;
  readonly enabled?: boolean;
  readonly latest_version?: ContentVersion | null;
}

interface ManifestItem {
  readonly run_id?: string;
  readonly sequence_name?: string;
  readonly logical_name?: string;
  readonly content_type?: string;
  readonly required_phase?: string;
  readonly status?: string;
  readonly source_uri?: string;
}

interface LegacySequence {
  readonly id: number;
  readonly name: string;
  readonly target_os?: string;
}

interface FlowTemplate {
  readonly id: string;
  readonly name: string;
  readonly target_os?: string;
  readonly path?: string;
  readonly status?: string;
  readonly description?: string;
  readonly step_count?: number;
  readonly read_only?: boolean;
  readonly notes?: readonly string[];
  readonly nodes?: readonly V2Step[];
}

interface StepTemplate {
  readonly kind: string;
  readonly label: string;
  readonly phase?: string;
  readonly category?: string;
  readonly description?: string;
  readonly retry_count?: number;
  readonly retry_delay_seconds?: number;
  readonly timeout_seconds?: number;
  readonly content_refs?: readonly string[];
}

interface BuilderPayload {
  readonly sequence: V2Sequence | null;
  readonly nodes: readonly V2Step[];
  readonly step_templates: readonly StepTemplate[];
  readonly legacy_sequences: readonly LegacySequence[];
  readonly legacy_source_id?: number | null;
  readonly flow_templates: readonly FlowTemplate[];
  readonly template_source?: FlowTemplate | null;
}

interface BuilderNode {
  readonly id?: string;
  readonly client_id: string;
  readonly parent_id?: string | null;
  readonly node_type: "step";
  readonly name: string;
  readonly description: string;
  readonly kind: string;
  readonly phase: string;
  readonly enabled: boolean;
  readonly condition: Readonly<Record<string, unknown>>;
  readonly variables: Readonly<Record<string, unknown>>;
  readonly params: Readonly<Record<string, unknown>>;
  readonly content_refs: readonly string[];
  readonly continue_on_error: boolean;
  readonly retry_count: number;
  readonly retry_delay_seconds: number;
  readonly timeout_seconds?: number | null;
  readonly reboot_behavior: string;
}

interface TaskEnginePayload {
  readonly sequences: readonly V2Sequence[];
  readonly runs: readonly V2Run[];
  readonly cloudosd_runs?: readonly V2Run[];
  readonly content_items?: readonly ContentItem[];
  readonly manifest_items?: readonly ManifestItem[];
  readonly legacy_sequences?: readonly LegacySequence[];
  readonly flow_templates: readonly FlowTemplate[];
  readonly target_os_filter?: string;
  readonly error?: string;
}

interface TemplateDetailPayload {
  readonly template: FlowTemplate;
}

interface ImportLegacyResponse {
  readonly id: string;
  readonly current_version_id?: string;
}

function defaultTaskEnginePayload(): TaskEnginePayload {
  return {
    sequences: [],
    runs: [],
    cloudosd_runs: [],
    content_items: [],
    manifest_items: [],
    legacy_sequences: [],
    flow_templates: [],
    target_os_filter: "",
    error: ""
  };
}

function stepCount(sequence: {
  readonly step_count?: number;
  readonly steps?: readonly V2Step[];
  readonly nodes?: readonly V2Step[];
}): string {
  return String(sequence.step_count ?? sequence.steps?.length ?? sequence.nodes?.length ?? 0);
}

function lowerHaystack(values: readonly unknown[]): string {
  return values.map((value) => textValue(value, "")).join(" ").toLowerCase();
}

function StatusBadge({ status }: { readonly status: string | undefined }) {
  const normalized = textValue(status, "unknown");
  return <span className={statusClass(normalized)}>{statusLabel(normalized)}</span>;
}

function TemplateCard({ template }: { readonly template: FlowTemplate }) {
  return (
    <article className="operator-map-card">
      <div>
        <strong>{template.name}</strong>
        <small>{textValue(template.path)} / {textValue(template.target_os, "windows")} / {stepCount(template)} steps</small>
      </div>
      <p>{textValue(template.description)}</p>
      <div className="utility-row-actions">
        <a aria-label={`Inspect ${template.name}`} href={`/react/task-engine/sequences/templates/${encodeURIComponent(template.id)}`}>Inspect</a>
        <a aria-label={`Clone ${template.name}`} href={`/react/task-engine/sequences/new?template_id=${encodeURIComponent(template.id)}`}>Clone</a>
      </div>
    </article>
  );
}

export function TaskEnginePage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const path = window.location.pathname;
  const templateMatch = /^\/react\/task-engine\/sequences\/templates\/([^/]+)$/u.exec(path);
  const editMatch = /^\/react\/task-engine\/sequences\/([^/]+)\/edit$/u.exec(path);
  if (templateMatch?.[1]) {
    return <TaskTemplateDetailPage bootstrap={bootstrap} templateId={templateMatch[1]} />;
  }
  if (editMatch?.[1]) {
    return <TaskSequenceBuilderPage bootstrap={bootstrap} mode="edit" sequenceId={editMatch[1]} />;
  }
  if (path === "/react/task-engine/sequences/new") {
    return <TaskSequenceBuilderPage bootstrap={bootstrap} mode="new" />;
  }
  if (path === "/react/task-engine/sequences/list") {
    return <TaskSequenceLibraryPage bootstrap={bootstrap} />;
  }
  return <TaskEngineOverviewPage bootstrap={bootstrap} />;
}

function useTaskEnginePayload(endpoint: string) {
  const [payload, setPayload] = useState<TaskEnginePayload>(defaultTaskEnginePayload);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const nextPayload = await fetchJson<TaskEnginePayload>(endpoint);
      setPayload(nextPayload);
      setError(textValue(nextPayload.error, ""));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load task engine");
    } finally {
      setLoading(false);
    }
  }, [endpoint]);

  usePolling(load);

  return { payload, loading, error, reload: load };
}

function LoadingStrip({ label }: { readonly label: string }) {
  return (
    <div className="load-strip" role="status" aria-live="polite">
      <span>Loading {label}</span>
      <div className="load-strip__track" role="progressbar" aria-label={`${label} loading`}><span /></div>
    </div>
  );
}

const WINDOWS_PHASES: readonly [string, string][] = [
  ["controller", "Controller"],
  ["pe", "WinPE"],
  ["specialize", "Specialize"],
  ["full_os", "Full OS"],
  ["verify", "Verify"]
];

const PHASES_BY_TARGET_OS: Readonly<Record<string, readonly [string, string][]>> = {
  windows: WINDOWS_PHASES,
  ubuntu: [
    ["controller", "Controller"],
    ["install", "Install"],
    ["first_boot", "First Boot"],
    ["full_os", "Full OS"],
    ["verify", "Verify"]
  ]
};

function phasesForTarget(targetOs: string): readonly [string, string][] {
  return PHASES_BY_TARGET_OS[targetOs] ?? WINDOWS_PHASES;
}

function templateToNode(template: StepTemplate, index: number): BuilderNode {
  return {
    client_id: `step-${Date.now().toString(36)}-${template.kind}-${String(index)}`,
    node_type: "step",
    name: template.label,
    description: textValue(template.description),
    kind: template.kind,
    phase: textValue(template.phase, "full_os"),
    enabled: true,
    condition: {},
    variables: {},
    params: {},
    content_refs: template.content_refs ?? [],
    continue_on_error: false,
    retry_count: template.retry_count ?? 0,
    retry_delay_seconds: template.retry_delay_seconds ?? 10,
    timeout_seconds: template.timeout_seconds ?? null,
    reboot_behavior: "none"
  };
}

function nodeFromPayload(node: V2Step, index: number): BuilderNode {
  const normalized: BuilderNode = {
    client_id: textValue(node.client_id ?? node.id, `step-${String(index)}`),
    parent_id: node.parent_id ?? null,
    node_type: "step",
    name: textValue(node.name, "Untitled step"),
    description: textValue(node.description),
    kind: textValue(node.kind),
    phase: textValue(node.phase, "full_os"),
    enabled: node.enabled !== false,
    condition: node.condition ?? {},
    variables: node.variables ?? {},
    params: node.params ?? {},
    content_refs: node.content_refs ?? [],
    continue_on_error: node.continue_on_error ?? false,
    retry_count: node.retry_count ?? 0,
    retry_delay_seconds: node.retry_delay_seconds ?? 10,
    timeout_seconds: node.timeout_seconds ?? null,
    reboot_behavior: textValue(node.reboot_behavior, "none")
  };
  return node.id ? { ...normalized, id: node.id } : normalized;
}

function uniqueCategories(templates: readonly StepTemplate[]): readonly string[] {
  return [...new Set(templates.map((template) => textValue(template.category, "General")))].toSorted();
}

function TaskSequenceBuilderPage({
  bootstrap,
  mode,
  sequenceId
}: {
  readonly bootstrap: AppBootstrap;
  readonly mode: "new" | "edit";
  readonly sequenceId?: string;
}) {
  const endpoint = mode === "edit" && sequenceId
    ? `/api/task-engine/sequences/${encodeURIComponent(sequenceId)}/edit/page`
    : `/api/task-engine/sequences/new/page${window.location.search}`;
  const [payload, setPayload] = useState<BuilderPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [targetOs, setTargetOs] = useState("windows");
  const [enabled, setEnabled] = useState(true);
  const [nodes, setNodes] = useState<readonly BuilderNode[]>([]);
  const [activeNodeId, setActiveNodeId] = useState("");
  const [paletteFilter, setPaletteFilter] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const nextPayload = await fetchJson<BuilderPayload>(endpoint);
        if (cancelled) {
          return;
        }
        const sequence = nextPayload.sequence;
        const nextNodes = nextPayload.nodes.map(nodeFromPayload);
        setPayload(nextPayload);
        setName(textValue(sequence?.name));
        setDescription(textValue(sequence?.description));
        setTargetOs(textValue(sequence?.target_os, "windows"));
        setEnabled(sequence?.enabled !== false);
        setNodes(nextNodes);
        setActiveNodeId(nextNodes[0]?.client_id ?? "");
        setError("");
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load builder");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [endpoint]);

  const activeNode = nodes.find((node) => node.client_id === activeNodeId);
  const filteredTemplates = useMemo(() => {
    const needle = paletteFilter.trim().toLowerCase();
    return (payload?.step_templates ?? []).filter((template) => {
      if (targetOs === "ubuntu" && (template.phase ?? "") === "pe") {
        return false;
      }
      if (!needle) {
        return true;
      }
      return lowerHaystack([template.kind, template.label, template.category, template.description]).includes(needle);
    });
  }, [paletteFilter, payload?.step_templates, targetOs]);
  const categories = useMemo(() => uniqueCategories(filteredTemplates), [filteredTemplates]);
  const title = mode === "edit" ? `Edit v2 sequence: ${textValue(name, sequenceId ?? "")}` : "New v2 task sequence";

  const updateNode = (clientId: string, patch: Partial<BuilderNode>) => {
    setNodes((current) => current.map((node) => node.client_id === clientId ? { ...node, ...patch } : node));
  };

  const addTemplate = (template: StepTemplate) => {
    const nextNode = templateToNode(template, nodes.length);
    setNodes((current) => [...current, nextNode]);
    setActiveNodeId(nextNode.client_id);
  };

  const removeNode = (clientId: string) => {
    setNodes((current) => current.filter((node) => node.client_id !== clientId));
    if (activeNodeId === clientId) {
      setActiveNodeId(nodes.find((node) => node.client_id !== clientId)?.client_id ?? "");
    }
  };

  const saveSequence = async () => {
    const body = {
      name,
      description,
      target_os: targetOs,
      enabled,
      nodes: nodes.map((node) => ({
        id: node.id,
        client_id: node.client_id,
        parent_id: node.parent_id ?? null,
        node_type: node.node_type,
        name: node.name,
        description: node.description,
        kind: node.kind,
        phase: node.phase,
        enabled: node.enabled,
        condition: node.condition,
        variables: node.variables,
        params: node.params,
        content_refs: node.content_refs,
        continue_on_error: node.continue_on_error,
        retry_count: node.retry_count,
        retry_delay_seconds: node.retry_delay_seconds,
        timeout_seconds: node.timeout_seconds ?? null,
        reboot_behavior: node.reboot_behavior
      }))
    };
    try {
      const result = mode === "edit" && sequenceId
        ? await putJson<ImportLegacyResponse>(`/api/osd/v2/builder/sequences/${encodeURIComponent(sequenceId)}`, body)
        : await postJson<ImportLegacyResponse>("/api/osd/v2/builder/sequences", body);
      setStatus(`saved ${result.id}`);
      if (mode === "new") {
        window.history.replaceState({}, "", `/react/task-engine/sequences/${encodeURIComponent(result.id)}/edit`);
      }
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "Save failed");
    }
  };

  return (
    <PageFrame bootstrap={bootstrap} title={title} section="Build" path={window.location.pathname}>
      {loading ? <LoadingStrip label="V2 Builder" /> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      {status ? <p className="notice" role="status">{status}</p> : null}

      <section className="utility-settings-grid utility-settings-grid--wide">
        <Panel title="Sequence">
          <div className="utility-field-grid">
            <label className="utility-field utility-field--wide">
              <span>Name</span>
              <input value={name} placeholder="OSDCloud desktop baseline" onChange={(event) => {
                setName(event.target.value);
              }} />
            </label>
            <label className="utility-field utility-field--wide">
              <span>Description</span>
              <textarea rows={4} value={description} placeholder="What this plan owns and when it runs" onChange={(event) => {
                setDescription(event.target.value);
              }} />
            </label>
            <label className="utility-field">
              <span>Target OS</span>
              <select value={targetOs} onChange={(event) => {
                setTargetOs(event.target.value);
              }}>
                <option value="windows">Windows</option>
                <option value="ubuntu">Ubuntu</option>
              </select>
            </label>
            <label className="utility-check">
              <input type="checkbox" checked={enabled} onChange={(event) => {
                setEnabled(event.target.checked);
              }} />
              <span>Enabled</span>
            </label>
          </div>
          <div className="utility-form-actions">
            <button className="utility-button utility-button--primary" type="button" onClick={() => { void saveSequence(); }}>
              <Save size={15} aria-hidden="true" /> Save and compile
            </button>
            <a className="utility-button" href="/react/task-engine/sequences/list">Cancel</a>
          </div>
        </Panel>

        <Panel title="Start From Template">
          {payload?.template_source ? (
            <p><StatusBadge status="ready" /> <strong>{payload.template_source.name}</strong><br /><small>{payload.template_source.description}</small></p>
          ) : null}
          <div className="utility-list" aria-label="Template starting points">
            {(payload?.flow_templates ?? []).slice(0, 8).map((template) => (
              <a key={template.id} href={`/react/task-engine/sequences/new?template_id=${encodeURIComponent(template.id)}`}>
                {template.name}<small>{textValue(template.path)} / {stepCount(template)} steps</small>
              </a>
            ))}
          </div>
        </Panel>
      </section>

      <Panel title="Phase Timeline">
        <div className="operator-map-grid task-builder-lanes">
          {phasesForTarget(targetOs).map(([phase, label]) => {
            const phaseNodes = nodes.filter((node) => node.phase === phase);
            return (
              <section className="operator-map-card" key={phase} aria-label={`${label} phase`}>
                <div><strong>{label}</strong><small>{phaseNodes.length} steps</small></div>
                <div className="task-builder-step-stack">
                  {phaseNodes.map((node) => (
                    <button
                      className="task-builder-step"
                      data-active={node.client_id === activeNodeId}
                      key={node.client_id}
                      type="button"
                      onClick={() => { setActiveNodeId(node.client_id); }}
                    >
                      <span>{node.name}</span>
                      <small>{node.kind}</small>
                    </button>
                  ))}
                </div>
              </section>
            );
          })}
        </div>
      </Panel>

      <section className="utility-settings-grid utility-settings-grid--wide">
        <Panel title="Step Palette">
          <label className="utility-field utility-field--wide">
            <span>Search full catalog</span>
            <input type="search" value={paletteFilter} placeholder="hash, qga, domain, package, ubuntu, mde" onChange={(event) => {
              setPaletteFilter(event.target.value);
            }} />
          </label>
          {categories.map((category) => (
            <section className="task-builder-palette-section" key={category}>
              <h3>{category}</h3>
              <div className="task-builder-palette-grid">
                {filteredTemplates.filter((template) => textValue(template.category, "General") === category).map((template) => (
                  <button className="task-builder-palette-item" key={template.kind} type="button" onClick={() => { addTemplate(template); }}>
                    <Plus size={14} aria-hidden="true" />
                    <span>{template.label}</span>
                    <small>{template.kind}</small>
                  </button>
                ))}
              </div>
            </section>
          ))}
        </Panel>

        <Panel title="Selected Step">
          {activeNode ? (
            <div className="utility-field-grid">
              <label className="utility-field utility-field--wide">
                <span>Step name</span>
                <input value={activeNode.name} onChange={(event) => {
                  updateNode(activeNode.client_id, { name: event.target.value });
                }} />
              </label>
              <label className="utility-field utility-field--wide">
                <span>Description</span>
                <textarea rows={3} value={activeNode.description} onChange={(event) => {
                  updateNode(activeNode.client_id, { description: event.target.value });
                }} />
              </label>
              <label className="utility-field">
                <span>Phase</span>
                <select value={activeNode.phase} onChange={(event) => {
                  updateNode(activeNode.client_id, { phase: event.target.value });
                }}>
                  {phasesForTarget(targetOs).map(([phase, label]) => <option key={phase} value={phase}>{label}</option>)}
                </select>
              </label>
              <label className="utility-field">
                <span>Retry count</span>
                <input type="number" min="0" value={activeNode.retry_count} onChange={(event) => {
                  updateNode(activeNode.client_id, { retry_count: Number(event.target.value) });
                }} />
              </label>
              <label className="utility-field">
                <span>Retry delay seconds</span>
                <input type="number" min="0" value={activeNode.retry_delay_seconds} onChange={(event) => {
                  updateNode(activeNode.client_id, { retry_delay_seconds: Number(event.target.value) });
                }} />
              </label>
              <label className="utility-check">
                <input type="checkbox" checked={activeNode.enabled} onChange={(event) => {
                  updateNode(activeNode.client_id, { enabled: event.target.checked });
                }} />
                <span>Enabled</span>
              </label>
              <button className="utility-button utility-button--danger" type="button" onClick={() => { removeNode(activeNode.client_id); }}>
                <Trash2 size={15} aria-hidden="true" /> Remove step
              </button>
            </div>
          ) : <p className="empty">Select a step to edit its phase, retries, content refs, and parameters.</p>}
        </Panel>
      </section>
    </PageFrame>
  );
}

function TaskEngineOverviewPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const { payload, loading, error } = useTaskEnginePayload("/api/task-engine/page");
  const [legacyId, setLegacyId] = useState("");
  const [status, setStatus] = useState("");
  const legacySequences = payload.legacy_sequences ?? [];
  const selectedLegacyId = legacyId || String(legacySequences[0]?.id ?? "");

  const importLegacy = async () => {
    if (!selectedLegacyId) {
      setStatus("select legacy sequence first");
      return;
    }
    try {
      const response = await postJson<ImportLegacyResponse>(`/api/osd/v2/builder/import-legacy/${encodeURIComponent(selectedLegacyId)}`);
      setStatus(`created ${response.id}`);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "Import failed");
    }
  };

  return (
    <PageFrame bootstrap={bootstrap} title="Task Sequence Engine v2" section="Build" path="/react/task-engine">
      {loading ? <LoadingStrip label="Task Engine" /> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      {status ? <p className="notice" role="status">{status}</p> : null}

      <Panel title="Smart V2 Builder" action={<a className="utility-button" href="/react/task-engine/sequences/list"><Library size={15} aria-hidden="true" /> Sequence library</a>}>
        <div className="utility-form-actions">
          <a className="utility-button" href="/react/task-engine/sequences/new"><ListTree size={15} aria-hidden="true" /> New v2 sequence</a>
          {legacySequences.length ? (
            <>
              <label className="utility-field" style={{ minWidth: "240px" }}>
                <span>Import v1</span>
                <select value={selectedLegacyId} onChange={(event) => {
                  setLegacyId(event.target.value);
                }}>
                  {legacySequences.map((sequence) => <option key={sequence.id} value={sequence.id}>{sequence.name}</option>)}
                </select>
              </label>
              <button className="utility-button" type="button" onClick={() => { void importLegacy(); }}>
                <CopyPlus size={15} aria-hidden="true" /> Create v2 copy
              </button>
            </>
          ) : null}
        </div>
      </Panel>

      <section className="metric-strip" aria-label="Task Engine metrics">
        <Metric label="V2 sequences" value={String(payload.sequences.length)} />
        <Metric label="V2 runs" value={String(payload.runs.length)} />
        <Metric label="Content items" value={String(payload.content_items?.length ?? 0)} />
        <Metric label="Manifest rows" value={String(payload.manifest_items?.length ?? 0)} />
      </section>

      <Panel title="Read-only Flow Templates">
        <div className="operator-map-grid">
          {payload.flow_templates.map((template) => <TemplateCard key={template.id} template={template} />)}
        </div>
      </Panel>

      <SequencesTable sequences={payload.sequences} label="V2 task sequences" />
      <RunsTable runs={payload.runs} label="V2 runs" />
      <OsdPlansTable runs={payload.cloudosd_runs ?? []} />
      <ContentTable items={payload.content_items ?? []} />
      <ManifestTable items={payload.manifest_items ?? []} />
    </PageFrame>
  );
}

function TaskSequenceLibraryPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const params = new URLSearchParams(window.location.search);
  const initialTarget = params.get("target_os") ?? "";
  const endpoint = `/api/task-engine/sequences/list/page${window.location.search}`;
  const { payload, loading, error } = useTaskEnginePayload(endpoint);
  const [filter, setFilter] = useState("");
  const [targetOs, setTargetOs] = useState(initialTarget);
  const lowerFilter = filter.trim().toLowerCase();
  const filteredSequences = useMemo(() => payload.sequences.filter((sequence) => {
    if (targetOs && (sequence.target_os || "windows") !== targetOs) {
      return false;
    }
    if (!lowerFilter) {
      return true;
    }
    return lowerHaystack([sequence.name, sequence.description, sequence.target_os, sequence.current_version_id]).includes(lowerFilter);
  }), [lowerFilter, payload.sequences, targetOs]);
  const filteredTemplates = useMemo(() => payload.flow_templates.filter((template) => {
    if (targetOs && (template.target_os || "windows") !== targetOs) {
      return false;
    }
    if (!lowerFilter) {
      return true;
    }
    return lowerHaystack([template.name, template.description, template.path, template.status, template.target_os]).includes(lowerFilter);
  }), [lowerFilter, payload.flow_templates, targetOs]);

  return (
    <PageFrame bootstrap={bootstrap} title="V2 Sequence Library" section="Build" path="/react/task-engine/sequences/list">
      {loading ? <LoadingStrip label="Task Sequences" /> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}

      <Panel title="Sequences" action={<a className="utility-button" href="/react/task-engine/sequences/new">New blank sequence</a>}>
        <div className="utility-field-grid">
          <label className="utility-field utility-field--wide">
            <span>Filter sequences and templates</span>
            <input type="search" value={filter} onChange={(event) => {
              setFilter(event.target.value);
            }} />
          </label>
          <label className="utility-field">
            <span>Target OS</span>
            <select value={targetOs} onChange={(event) => {
              setTargetOs(event.target.value);
            }}>
              <option value="">All</option>
              <option value="windows">Windows</option>
              <option value="ubuntu">Ubuntu</option>
            </select>
          </label>
        </div>
      </Panel>

      <SequencesTable sequences={filteredSequences} label="Editable V2 sequences" />
      <Panel title="Read-only Flow Templates">
        <div className="operator-map-grid">
          {filteredTemplates.map((template) => <TemplateCard key={template.id} template={template} />)}
        </div>
      </Panel>
    </PageFrame>
  );
}

function TaskTemplateDetailPage({
  bootstrap,
  templateId
}: {
  readonly bootstrap: AppBootstrap;
  readonly templateId: string;
}) {
  const [template, setTemplate] = useState<FlowTemplate | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const payload = await fetchJson<TemplateDetailPayload>(`/api/task-engine/sequences/templates/${encodeURIComponent(templateId)}/page`);
      setTemplate(payload.template);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load template");
    } finally {
      setLoading(false);
    }
  }, [templateId]);

  usePolling(load);

  return (
    <PageFrame bootstrap={bootstrap} title={template?.name ?? "Task Template"} section="Build" path={`/react/task-engine/sequences/templates/${templateId}`}>
      {loading ? <LoadingStrip label="Task Template" /> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      {template ? (
        <>
          <Panel
            title="Template Summary"
            action={<a className="utility-button" href={`/react/task-engine/sequences/new?template_id=${encodeURIComponent(template.id)}`}>Clone into builder</a>}
          >
            <dl className="utility-definition-grid">
              <div><dt>Path</dt><dd>{textValue(template.path)}</dd></div>
              <div><dt>Target OS</dt><dd>{textValue(template.target_os, "windows")}</dd></div>
              <div><dt>Status</dt><dd>{textValue(template.status)}</dd></div>
              <div><dt>Template ID</dt><dd>{template.id}</dd></div>
            </dl>
            <p className="muted">{textValue(template.description)}</p>
          </Panel>
          <Panel title="Operator Notes">
            <ul className="utility-list" aria-label="Operator notes">
              {(template.notes ?? []).map((note) => <li key={note}><span>{note}</span></li>)}
            </ul>
          </Panel>
          <StepPlanTable steps={template.nodes ?? []} label="Read-only step plan" />
        </>
      ) : null}
    </PageFrame>
  );
}

function SequencesTable({ sequences, label }: { readonly sequences: readonly V2Sequence[]; readonly label: string }) {
  return (
    <Panel title={label}>
      {sequences.length ? (
        <div className="table-wrap">
          <table className="jobs-table utility-table" aria-label={label}>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Target OS</th>
                <th scope="col">Status</th>
                <th scope="col">Steps</th>
                <th scope="col">Current Version</th>
                <th scope="col">Updated</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {sequences.map((sequence) => (
                <tr key={sequence.id}>
                  <td><strong>{sequence.name}</strong><br /><small>{textValue(sequence.description)}</small></td>
                  <td>{textValue(sequence.target_os, "windows")}</td>
                  <td>{sequence.enabled === false ? "disabled" : "enabled"}</td>
                  <td>{stepCount(sequence)}</td>
                  <td><code>{textValue(sequence.current_version_id, "uncompiled")}</code></td>
                  <td>{formatShortDateTime(sequence.updated_at)}</td>
                  <td><a href={`/react/task-engine/sequences/${encodeURIComponent(sequence.id)}/edit`}>Open builder</a></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="empty">No v2 task sequences.</p>}
    </Panel>
  );
}

function RunsTable({ runs, label }: { readonly runs: readonly V2Run[]; readonly label: string }) {
  return (
    <Panel title={label}>
      {runs.length ? (
        <div className="table-wrap">
          <table className="jobs-table utility-table" aria-label={label}>
            <thead>
              <tr>
                <th scope="col">Run</th>
                <th scope="col">Sequence</th>
                <th scope="col">State</th>
                <th scope="col">VMID</th>
                <th scope="col">Steps</th>
                <th scope="col">Content</th>
                <th scope="col">Started</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.id}>
                  <td><code>{run.id}</code></td>
                  <td>{textValue(run.sequence_name)} <small>v{textValue(run.sequence_version)}</small></td>
                  <td><StatusBadge status={run.state} /> <small>{textValue(run.phase)}</small></td>
                  <td>{textValue(run.vmid)}</td>
                  <td>{textValue(run.done_count, "0")}/{textValue(run.step_count, "0")} done</td>
                  <td>{textValue(run.manifest_count)}</td>
                  <td>{formatShortDateTime(run.started_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="empty">No v2 runs.</p>}
    </Panel>
  );
}

function OsdPlansTable({ runs }: { readonly runs: readonly V2Run[] }) {
  return (
    <Panel title="OSDCloud V2 OSD Task Plans">
      {runs.length ? (
        <div className="table-wrap">
          <table className="jobs-table utility-table" aria-label="OSDCloud V2 OSD task plans">
            <thead><tr><th>Run</th><th>Target</th><th>State</th><th>Visible OSD Plan</th></tr></thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.id}>
                  <td><code>{run.id}</code></td>
                  <td>VMID {textValue(run.vmid)}<br /><small>{textValue(run.computer_name)}</small></td>
                  <td><StatusBadge status={run.state} /><br /><small>{textValue(run.done_count, "0")}/{textValue(run.step_count, "0")} done</small></td>
                  <td>{(run.steps ?? []).map((step) => <span className="badge" key={`${run.id}-${textValue(step.kind)}-${textValue(step.name)}`}>{textValue(step.name)} / {textValue(step.kind)}</span>)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="empty">No OSDCloud v2 OSD run plans.</p>}
    </Panel>
  );
}

function ContentTable({ items }: { readonly items: readonly ContentItem[] }) {
  return (
    <Panel title="Content Library">
      {items.length ? (
        <div className="table-wrap">
          <table className="jobs-table utility-table" aria-label="Content library">
            <thead><tr><th>Name</th><th>Type</th><th>Latest Version</th><th>Source</th><th>Status</th></tr></thead>
            <tbody>
              {items.map((item) => (
                <tr key={textValue(item.id ?? item.name)}>
                  <td>{textValue(item.name)}<br /><small>{textValue(item.description)}</small></td>
                  <td>{textValue(item.content_type)}</td>
                  <td>{textValue(item.latest_version?.version)}</td>
                  <td><code>{textValue(item.latest_version?.source_uri)}</code></td>
                  <td>{item.enabled === false ? "disabled" : "enabled"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="empty">No v2 content items.</p>}
    </Panel>
  );
}

function ManifestTable({ items }: { readonly items: readonly ManifestItem[] }) {
  return (
    <Panel title="Content Manifest">
      {items.length ? (
        <div className="table-wrap">
          <table className="jobs-table utility-table" aria-label="Content manifest">
            <thead><tr><th>Run</th><th>Sequence</th><th>Logical Name</th><th>Type</th><th>Phase</th><th>Status</th><th>Source</th></tr></thead>
            <tbody>
              {items.map((item, index) => (
                <tr key={`${textValue(item.run_id)}-${textValue(item.logical_name)}-${String(index)}`}>
                  <td><code>{textValue(item.run_id)}</code></td>
                  <td>{textValue(item.sequence_name)}</td>
                  <td>{textValue(item.logical_name)}</td>
                  <td>{textValue(item.content_type)}</td>
                  <td>{textValue(item.required_phase)}</td>
                  <td><StatusBadge status={item.status} /></td>
                  <td><code>{textValue(item.source_uri)}</code></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="empty">No resolved v2 content manifest rows.</p>}
    </Panel>
  );
}

function StepPlanTable({ steps, label }: { readonly steps: readonly V2Step[]; readonly label: string }) {
  return (
    <Panel title="Read-only Step Plan">
      <div className="table-wrap">
        <table className="jobs-table utility-table" aria-label={label}>
          <thead><tr><th>#</th><th>Step</th><th>Kind</th><th>Phase</th><th>Retry</th><th>Content</th></tr></thead>
          <tbody>
            {steps.map((step, index) => (
              <tr key={`${textValue(step.kind)}-${String(index)}`}>
                <td>{index + 1}</td>
                <td><strong>{textValue(step.name)}</strong><br /><small>{textValue(step.description)}</small></td>
                <td><code>{textValue(step.kind)}</code></td>
                <td>{textValue(step.phase)}</td>
                <td>{textValue(step.retry_count, "0")} / {textValue(step.retry_delay_seconds, "0")}s</td>
                <td>{step.content_refs?.length ? step.content_refs.join(", ") : "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
