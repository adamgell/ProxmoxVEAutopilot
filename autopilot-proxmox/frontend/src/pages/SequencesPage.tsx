import { Copy, Save, Trash2 } from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import { deleteJson, fetchJson, postJson, putJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { textValue } from "../utilityModels";
import { formatShortDateTime } from "../viewModels";

interface LegacyStep {
  readonly order_index?: number;
  readonly type?: string;
  readonly kind?: string;
  readonly phase?: string;
  readonly params?: Readonly<Record<string, unknown>>;
}

interface LegacySequence {
  readonly id?: number;
  readonly name?: string;
  readonly description?: string;
  readonly target_os?: string;
  readonly step_count?: number;
  readonly steps?: readonly LegacyStep[];
  readonly is_default?: boolean;
  readonly produces_autopilot_hash?: boolean;
  readonly hash_capture_phase?: string;
  readonly winpe_action_kinds?: readonly string[];
  readonly updated_at?: string;
}

interface SequencesPayload {
  readonly sequences: readonly LegacySequence[];
  readonly error?: string;
}

interface SequenceFormPayload {
  readonly sequence?: LegacySequence | null;
  readonly seq?: LegacySequence | null;
  readonly oem_profiles?: unknown;
}

function sequenceStepKind(step: LegacyStep): string {
  return textValue(step.kind ?? step.type, "step");
}

export function SequencesPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const path = window.location.pathname;
  const editMatch = /^\/react\/sequences\/(\d+)\/edit$/u.exec(path);
  if (path === "/react/sequences/new") {
    return <SequenceFormPage bootstrap={bootstrap} mode="new" />;
  }
  if (editMatch?.[1]) {
    return <SequenceFormPage bootstrap={bootstrap} mode="edit" sequenceId={editMatch[1]} />;
  }
  return <SequenceListPage bootstrap={bootstrap} />;
}

function SequenceListPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const [payload, setPayload] = useState<SequencesPayload>({ sequences: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const [actionStatus, setActionStatus] = useState("");

  const load = useCallback(async () => {
    try {
      const nextPayload = await fetchJson<SequencesPayload>("/api/sequences/page");
      setPayload(nextPayload);
      setError(textValue(nextPayload.error));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load sequences");
    } finally {
      setLoading(false);
    }
  }, []);

  usePolling(load);

  const needle = filter.trim().toLowerCase();
  const sequences = useMemo(() => payload.sequences.filter((sequence) => {
    if (!needle) {
      return true;
    }
    return [sequence.name, sequence.description, sequence.target_os, sequence.is_default ? "default" : ""]
      .map((value) => textValue(value).toLowerCase())
      .join(" ")
      .includes(needle);
  }), [needle, payload.sequences]);

  const duplicate = async (sequence: LegacySequence) => {
    if (!sequence.id) {
      return;
    }
    try {
      await postJson(`/api/sequences/${String(sequence.id)}/duplicate`, { new_name: `${textValue(sequence.name, "Sequence")} (copy)` });
      setActionStatus("sequence duplicated");
      await load();
    } catch (err) {
      setActionStatus(err instanceof Error ? err.message : "duplicate failed");
    }
  };

  const remove = async (sequence: LegacySequence) => {
    if (!sequence.id) {
      return;
    }
    try {
      await deleteJson(`/api/sequences/${String(sequence.id)}`);
      setActionStatus("sequence deleted");
      await load();
    } catch (err) {
      setActionStatus(err instanceof Error ? err.message : "delete failed");
    }
  };

  return (
    <PageFrame bootstrap={bootstrap} title="Task Sequences" section="Fleet" path="/react/sequences">
      {loading ? <div className="load-strip" role="status"><span>Loading task sequences</span><div className="load-strip__track" role="progressbar" aria-label="Task sequences loading"><span /></div></div> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      {actionStatus ? <p className="notice" role="status">{actionStatus}</p> : null}
      <section className="metric-strip" aria-label="Legacy task sequence metrics">
        <Metric label="Sequences" value={String(payload.sequences.length)} />
        <Metric label="Default" value={payload.sequences.find((sequence) => sequence.is_default)?.name ?? "-"} />
        <Metric label="Windows" value={String(payload.sequences.filter((sequence) => (sequence.target_os ?? "windows") === "windows").length)} />
        <Metric label="Ubuntu" value={String(payload.sequences.filter((sequence) => sequence.target_os === "ubuntu").length)} />
      </section>
      <Panel title="Sequences" action={<a className="utility-button" href="/react/sequences/new">New sequence</a>}>
        <label className="utility-field utility-field--wide">
          <span>Filter sequences</span>
          <input type="search" value={filter} placeholder="name, OS, description, default" onChange={(event) => {
            setFilter(event.target.value);
          }} />
        </label>
        <div className="table-wrap">
          <table className="jobs-table utility-table" aria-label="Task sequences">
            <thead><tr><th>Name</th><th>Target OS</th><th>Description</th><th>Steps</th><th>Default</th><th>Hash</th><th>WinPE task plan</th><th>Updated</th><th>Actions</th></tr></thead>
            <tbody>
              {sequences.map((sequence) => (
                <tr key={textValue(sequence.id)}>
                  <td><strong>{textValue(sequence.name)}</strong></td>
                  <td>{textValue(sequence.target_os, "windows")}</td>
                  <td>{textValue(sequence.description)}</td>
                  <td>{textValue(sequence.step_count ?? sequence.steps?.length, "0")}</td>
                  <td>{sequence.is_default ? "default" : "-"}</td>
                  <td>{sequence.produces_autopilot_hash ? textValue(sequence.hash_capture_phase, "oobe") : "-"}</td>
                  <td>{sequence.winpe_action_kinds?.length ? sequence.winpe_action_kinds.map((kind) => <code key={kind}>{kind}</code>) : "-"}</td>
                  <td>{formatShortDateTime(sequence.updated_at)}</td>
                  <td>
                    <div className="utility-row-actions">
                      <a href={`/react/sequences/${String(sequence.id)}/edit`}>Edit</a>
                      <button type="button" onClick={() => { void duplicate(sequence); }}><Copy size={14} aria-hidden="true" /> Duplicate</button>
                      <button type="button" onClick={() => { void remove(sequence); }}><Trash2 size={14} aria-hidden="true" /> Delete</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </PageFrame>
  );
}

function SequenceFormPage({
  bootstrap,
  mode,
  sequenceId
}: {
  readonly bootstrap: AppBootstrap;
  readonly mode: "new" | "edit";
  readonly sequenceId?: string;
}) {
  const endpoint = mode === "edit" && sequenceId ? `/api/sequences/${encodeURIComponent(sequenceId)}/edit/page` : "/api/sequences/new/page";
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [targetOs, setTargetOs] = useState("windows");
  const [isDefault, setIsDefault] = useState(false);
  const [producesHash, setProducesHash] = useState(false);
  const [hashPhase, setHashPhase] = useState("oobe");
  const [stepsJson, setStepsJson] = useState("[]");

  const load = useCallback(async () => {
    try {
      const payload = await fetchJson<SequenceFormPayload>(endpoint);
      const sequence = payload.sequence ?? payload.seq ?? null;
      setName(textValue(sequence?.name));
      setDescription(textValue(sequence?.description));
      setTargetOs(textValue(sequence?.target_os, "windows"));
      setIsDefault(sequence?.is_default ?? false);
      setProducesHash(sequence?.produces_autopilot_hash ?? false);
      setHashPhase(textValue(sequence?.hash_capture_phase, "oobe"));
      setStepsJson(JSON.stringify(sequence?.steps ?? [], null, 2));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load sequence");
    } finally {
      setLoading(false);
    }
  }, [endpoint]);

  usePolling(load);

  const save = async () => {
    try {
      const parsed = JSON.parse(stepsJson) as unknown;
      if (!Array.isArray(parsed)) {
        setStatus("steps must be a JSON array");
        return;
      }
      const body = {
        name,
        description,
        target_os: targetOs,
        is_default: isDefault,
        produces_autopilot_hash: producesHash,
        hash_capture_phase: hashPhase,
        steps: parsed
      };
      if (mode === "edit" && sequenceId) {
        const result = await putJson<{ readonly ok: boolean }>(`/api/sequences/${encodeURIComponent(sequenceId)}`, body);
        setStatus(result.ok ? `saved ${sequenceId}` : "save returned without confirmation");
        return;
      }
      const result = await postJson<{ readonly id: number }>("/api/sequences", body);
      setStatus(`created ${String(result.id)}`);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : "save failed");
    }
  };

  return (
    <PageFrame bootstrap={bootstrap} title={mode === "edit" ? `Edit Sequence ${sequenceId ?? ""}` : "New Sequence"} section="Fleet" path={window.location.pathname}>
      {loading ? <div className="load-strip" role="status"><span>Loading sequence</span><div className="load-strip__track" role="progressbar" aria-label="Sequence loading"><span /></div></div> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      {status ? <p className="notice" role="status">{status}</p> : null}
      <Panel title="Sequence">
        <div className="utility-field-grid">
          <label className="utility-field utility-field--wide"><span>Name</span><input value={name} onChange={(event) => { setName(event.target.value); }} /></label>
          <label className="utility-field utility-field--wide"><span>Description</span><textarea rows={3} value={description} onChange={(event) => { setDescription(event.target.value); }} /></label>
          <label className="utility-field"><span>Target OS</span><select value={targetOs} onChange={(event) => { setTargetOs(event.target.value); }}><option value="windows">Windows</option><option value="ubuntu">Ubuntu</option></select></label>
          <label className="utility-field"><span>Hash phase</span><select value={hashPhase} onChange={(event) => { setHashPhase(event.target.value); }}><option value="oobe">OOBE</option><option value="winpe">WinPE</option><option value="full_os">Full OS</option></select></label>
          <label className="utility-check"><input type="checkbox" checked={isDefault} onChange={(event) => { setIsDefault(event.target.checked); }} /><span>Default</span></label>
          <label className="utility-check"><input type="checkbox" checked={producesHash} onChange={(event) => { setProducesHash(event.target.checked); }} /><span>Produces Autopilot hash</span></label>
          <label className="utility-field utility-field--wide"><span>Steps JSON</span><textarea rows={12} value={stepsJson} onChange={(event) => { setStepsJson(event.target.value); }} /></label>
        </div>
        <div className="utility-form-actions">
          <button className="utility-button utility-button--primary" type="button" onClick={() => { void save(); }}><Save size={15} aria-hidden="true" /> Save</button>
          <a className="utility-button" href="/react/sequences">Cancel</a>
        </div>
      </Panel>
      <Panel title="Step Preview">
        <div className="table-wrap">
          <table className="jobs-table utility-table" aria-label="Sequence step preview">
            <thead><tr><th>#</th><th>Kind</th><th>Phase</th></tr></thead>
            <tbody>
              {(() => {
                try {
                  const parsed = JSON.parse(stepsJson) as unknown;
                  return Array.isArray(parsed) ? parsed.map((step, index) => {
                    const record = step && typeof step === "object" && !Array.isArray(step) ? step as LegacyStep : {};
                    return <tr key={String(index)}><td>{index + 1}</td><td><code>{sequenceStepKind(record)}</code></td><td>{textValue(record.phase)}</td></tr>;
                  }) : [];
                } catch {
                  return [];
                }
              })()}
            </tbody>
          </table>
        </div>
      </Panel>
    </PageFrame>
  );
}
