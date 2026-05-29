import { OctagonAlert, Play, Square } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { fetchJson, postJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { textValue } from "../utilityModels";
import { formatShortDateTime, statusClass, statusLabel } from "../viewModels";

interface JobPlan {
  readonly title?: string;
  readonly summary?: string;
  readonly metadata?: readonly (readonly [string, string])[];
  readonly steps?: readonly string[];
  readonly end_goal?: string;
}

interface JobDetail {
  readonly id: string;
  readonly playbook?: string;
  readonly status?: string;
  readonly started?: string;
  readonly ended?: string | null;
  readonly exit_code?: number | null;
  readonly paused?: boolean;
  readonly args?: Readonly<Record<string, unknown>>;
}

interface JobDetailPayload {
  readonly job: JobDetail | null;
  readonly plan?: JobPlan | null;
  readonly log?: readonly string[];
  readonly log_content?: string;
  readonly stream_url: string;
  readonly error?: string;
}

interface RunRow {
  readonly id: number | string;
  readonly sequence_name?: string;
  readonly sequence_id?: number | string;
  readonly vmid?: number | string | null;
  readonly vm_uuid?: string | null;
  readonly state?: string;
  readonly provision_path?: string;
  readonly ok_count?: number;
  readonly running_count?: number;
  readonly error_count?: number;
  readonly step_count?: number;
  readonly started_at?: string;
  readonly finished_at?: string | null;
  readonly last_error?: string | null;
}

interface RunStep {
  readonly order_index?: number;
  readonly phase?: string;
  readonly kind?: string;
  readonly state?: string;
  readonly started_at?: string | null;
  readonly finished_at?: string | null;
  readonly error?: string | null;
}

interface RunsPayload {
  readonly runs: readonly RunRow[];
}

interface RunDetailPayload {
  readonly run: RunRow;
  readonly steps: readonly RunStep[];
  readonly step_counts?: Readonly<Record<string, number>>;
  readonly summary?: Readonly<Record<string, number>>;
}

function Badge({ status }: { readonly status: string | undefined }) {
  const normalized = textValue(status, "unknown");
  return <span className={statusClass(normalized)}>{statusLabel(normalized)}</span>;
}

function usePayload<T>(endpoint: string, fallback: T) {
  const [payload, setPayload] = useState<T>(fallback);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setPayload(await fetchJson<T>(endpoint));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load page");
    } finally {
      setLoading(false);
    }
  }, [endpoint]);

  usePolling(load);
  return { payload, loading, error, reload: load };
}

function Loading({ label }: { readonly label: string }) {
  return (
    <div className="load-strip" role="status" aria-live="polite">
      <span>Loading {label}</span>
      <div className="load-strip__track" role="progressbar" aria-label={`${label} loading`}><span /></div>
    </div>
  );
}

export function JobDetailPage({ bootstrap, jobId }: { readonly bootstrap: AppBootstrap; readonly jobId: string }) {
  const { payload, loading, error, reload } = usePayload<JobDetailPayload>(`/api/jobs/${encodeURIComponent(jobId)}/page`, {
    job: null,
    plan: null,
    log: [],
    log_content: "",
    stream_url: `/api/jobs/${jobId}/stream`
  });
  const [streamLines, setStreamLines] = useState<readonly string[]>([]);
  const [actionStatus, setActionStatus] = useState("");
  const job = payload.job;
  const logLines = [...(payload.log ?? []), ...streamLines];

  useEffect(() => {
    if (!job || job.status !== "running" || typeof WebSocket === "undefined") {
      return undefined;
    }
    const scheme = window.location.protocol === "https:" ? "wss://" : "ws://";
    const socket = new WebSocket(`${scheme}${window.location.host}${payload.stream_url}`);
    socket.addEventListener("message", (event) => {
      setStreamLines((current) => [...current, String(event.data)]);
    });
    socket.addEventListener("close", () => {
      setStreamLines((current) => [...current, "[log stream closed]"]);
    });
    return () => {
      socket.close();
    };
  }, [job, payload.stream_url]);

  const resume = async () => {
    try {
      await postJson(`/api/jobs/${encodeURIComponent(jobId)}/resume-template-build`);
      setActionStatus("resume signal sent");
      await reload();
    } catch (err) {
      setActionStatus(err instanceof Error ? err.message : "resume failed");
    }
  };

  const kill = async () => {
    try {
      await fetch(`/api/jobs/${encodeURIComponent(jobId)}/kill`, { method: "POST", credentials: "same-origin" });
      setActionStatus("stop requested");
      await reload();
    } catch (err) {
      setActionStatus(err instanceof Error ? err.message : "stop failed");
    }
  };

  return (
    <PageFrame bootstrap={bootstrap} title={`Job ${jobId}`} section="Observe" path={`/react/jobs/${jobId}`}>
      {loading ? <Loading label="job detail" /> : null}
      {error || payload.error ? <p className="notice notice--bad" role="alert">{error || payload.error}</p> : null}
      {actionStatus ? <p className="notice" role="status">{actionStatus}</p> : null}

      {job ? (
        <>
          <Panel title="Job Metadata">
            <dl className="utility-definition-grid">
              <div><dt>Playbook</dt><dd>{textValue(job.playbook)}</dd></div>
              <div><dt>Status</dt><dd><Badge status={job.paused ? "paused" : job.status} /></dd></div>
              <div><dt>Started</dt><dd>{formatShortDateTime(job.started)}</dd></div>
              <div><dt>Ended</dt><dd>{formatShortDateTime(job.ended)}</dd></div>
              <div><dt>Exit code</dt><dd>{textValue(job.exit_code)}</dd></div>
              <div><dt>Stream</dt><dd><code>{payload.stream_url}</code></dd></div>
            </dl>
            {job.status === "running" ? (
              <div className="utility-form-actions">
                {job.args?.pause_enabled ? <button className="utility-button" type="button" onClick={() => { void resume(); }}><Play size={15} aria-hidden="true" /> Resume</button> : null}
                <button className="utility-button utility-button--danger" type="button" onClick={() => { void kill(); }}><Square size={15} aria-hidden="true" /> Emergency stop</button>
              </div>
            ) : null}
          </Panel>

          {payload.plan ? (
            <Panel title={textValue(payload.plan.title, "Job Plan")}>
              {payload.plan.summary ? <p className="muted">{payload.plan.summary}</p> : null}
              {payload.plan.metadata?.length ? (
                <dl className="utility-definition-grid">
                  {payload.plan.metadata.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
                </dl>
              ) : null}
              {payload.plan.steps?.length ? <ol>{payload.plan.steps.map((step) => <li key={step}>{step}</li>)}</ol> : null}
              {payload.plan.end_goal ? <p className="notice"><strong>End state:</strong> {payload.plan.end_goal}</p> : null}
            </Panel>
          ) : null}
        </>
      ) : null}

      <Panel title="Output">
        <pre className="terminal" aria-label="Job output">{logLines.join("\n")}</pre>
      </Panel>
    </PageFrame>
  );
}

export function RunsPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
  const { payload, loading, error } = usePayload<RunsPayload>("/api/runs/page", { runs: [] });
  const [filter, setFilter] = useState("");
  const needle = filter.trim().toLowerCase();
  const runs = useMemo(() => payload.runs.filter((run) => {
    if (!needle) {
      return true;
    }
    return [run.id, run.sequence_name, run.vmid, run.vm_uuid, run.state].map((value) => textValue(value).toLowerCase()).join(" ").includes(needle);
  }), [needle, payload.runs]);

  return (
    <PageFrame bootstrap={bootstrap} title="WinPE Runs" section="Observe" path="/react/runs">
      {loading ? <Loading label="runs" /> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <section className="metric-strip" aria-label="WinPE run metrics">
        <Metric label="Runs" value={String(payload.runs.length)} />
        <Metric label="Running" value={String(payload.runs.filter((run) => run.state === "running").length)} />
        <Metric label="Attention" value={String(payload.runs.filter((run) => run.error_count || run.state === "error").length)} />
      </section>
      <Panel title="Runs">
        <label className="utility-field utility-field--wide">
          <span>Filter runs</span>
          <input type="search" value={filter} placeholder="sequence, state, VMID, UUID" onChange={(event) => {
            setFilter(event.target.value);
          }} />
        </label>
        <div className="table-wrap">
          <table className="jobs-table utility-table" aria-label="WinPE runs">
            <thead><tr><th>Run</th><th>Sequence</th><th>VMID</th><th>State</th><th>Tasks</th><th>Started</th><th>VM UUID</th></tr></thead>
            <tbody>
              {runs.map((run) => (
                <tr key={String(run.id)}>
                  <td><a href={`/react/runs/${encodeURIComponent(String(run.id))}`}>#{run.id}</a></td>
                  <td>{textValue(run.sequence_name ?? run.sequence_id)}</td>
                  <td>{textValue(run.vmid)}</td>
                  <td><Badge status={run.state} /></td>
                  <td>{textValue(run.ok_count, "0")}/{textValue(run.step_count, "0")} ok</td>
                  <td>{formatShortDateTime(run.started_at)}</td>
                  <td><code>{textValue(run.vm_uuid, "pending")}</code></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </PageFrame>
  );
}

export function RunDetailPage({ bootstrap, runId }: { readonly bootstrap: AppBootstrap; readonly runId: string }) {
  const { payload, loading, error } = usePayload<RunDetailPayload>(`/api/runs/${encodeURIComponent(runId)}/page`, {
    run: { id: runId },
    steps: [],
    step_counts: {}
  });
  const counts = payload.step_counts ?? payload.summary ?? {};
  return (
    <PageFrame bootstrap={bootstrap} title={`Run ${runId}`} section="Observe" path={`/react/runs/${runId}`}>
      {loading ? <Loading label="run detail" /> : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}
      <section className="metric-strip" aria-label="Run task metrics">
        <Metric label="Tasks" value={textValue(counts.total, "0")} />
        <Metric label="OK" value={textValue(counts.ok, "0")} />
        <Metric label="Running" value={textValue(counts.running, "0")} />
        <Metric label="Attention" value={textValue(counts.error, "0")} />
      </section>
      <Panel title="Run Metadata">
        <dl className="utility-definition-grid">
          <div><dt>Task sequence</dt><dd>{textValue(payload.run.sequence_name ?? payload.run.sequence_id)}</dd></div>
          <div><dt>VMID</dt><dd>{textValue(payload.run.vmid)}</dd></div>
          <div><dt>VM UUID</dt><dd><code>{textValue(payload.run.vm_uuid)}</code></dd></div>
          <div><dt>State</dt><dd><Badge status={payload.run.state} /></dd></div>
          <div><dt>Started</dt><dd>{formatShortDateTime(payload.run.started_at)}</dd></div>
          <div><dt>Finished</dt><dd>{formatShortDateTime(payload.run.finished_at)}</dd></div>
        </dl>
        {payload.run.last_error ? <p className="notice notice--bad"><OctagonAlert size={15} aria-hidden="true" /> {payload.run.last_error}</p> : null}
      </Panel>
      <Panel title="WinPE Task Plan">
        <div className="table-wrap">
          <table className="jobs-table utility-table" aria-label="WinPE task plan">
            <thead><tr><th>#</th><th>Phase</th><th>Kind</th><th>State</th><th>Started</th><th>Finished</th><th>Error</th></tr></thead>
            <tbody>
              {payload.steps.map((step, index) => (
                <tr key={`${textValue(step.kind)}-${String(index)}`}>
                  <td>{textValue(step.order_index, String(index))}</td>
                  <td>{textValue(step.phase)}</td>
                  <td><code>{textValue(step.kind)}</code></td>
                  <td><Badge status={step.state} /></td>
                  <td>{formatShortDateTime(step.started_at)}</td>
                  <td>{formatShortDateTime(step.finished_at)}</td>
                  <td>{textValue(step.error)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </PageFrame>
  );
}
