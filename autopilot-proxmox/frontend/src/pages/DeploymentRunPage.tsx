import { useCallback, useState } from "react";

import { fetchJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { Metric, Panel } from "../components/ui";
import type { AppBootstrap, OperatorGroupLabel } from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { textValue } from "../utilityModels";
import { formatShortDateTime, statusClass, statusLabel } from "../viewModels";

interface DeploymentStep {
  readonly id?: string;
  readonly ordinal?: number;
  readonly phase?: string;
  readonly name?: string;
  readonly kind?: string;
  readonly state?: string;
  readonly attempt?: number;
  readonly last_error?: string | null;
}

interface DeploymentEvent {
  readonly id?: string | number;
  readonly created_at?: string;
  readonly phase?: string;
  readonly event_type?: string;
  readonly type?: string;
  readonly message?: string | null;
  readonly detail?: string | null;
}

interface RelatedJob {
  readonly id?: string;
  readonly playbook?: string;
  readonly status?: string;
}

interface DeploymentRunPayload {
  readonly run: Readonly<Record<string, unknown>>;
  readonly artifact?: Readonly<Record<string, unknown>> | null;
  readonly readiness?: Readonly<Record<string, unknown>> | null;
  readonly latest_heartbeat?: Readonly<Record<string, unknown>> | null;
  readonly heartbeat?: Readonly<Record<string, unknown>> | null;
  readonly events?: readonly DeploymentEvent[];
  readonly v2_steps?: readonly DeploymentStep[];
  readonly related_jobs?: readonly RelatedJob[];
  readonly v2_operator_status?: Readonly<Record<string, unknown>> | null;
  readonly v2_completion?: Readonly<Record<string, unknown>> | null;
}

function Badge({ status }: { readonly status: string | undefined }) {
  const normalized = textValue(status, "unknown");
  return <span className={statusClass(normalized)}>{statusLabel(normalized)}</span>;
}

function field(record: Readonly<Record<string, unknown>> | null | undefined, key: string): unknown {
  return record?.[key];
}

export function DeploymentRunPage({
  bootstrap,
  kind,
  runId
}: {
  readonly bootstrap: AppBootstrap;
  readonly kind: "cloudosd" | "osdeploy";
  readonly runId: string;
}) {
  const label = kind === "cloudosd" ? "OSDCloud" : "OSDeploy";
  const section: OperatorGroupLabel = "Deploy";
  const endpoint = `/api/${kind}/runs/${encodeURIComponent(runId)}/page`;
  const [payload, setPayload] = useState<DeploymentRunPayload>({
    run: {},
    artifact: null,
    events: [],
    v2_steps: []
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setPayload(await fetchJson<DeploymentRunPayload>(endpoint));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to load ${label} run`);
    } finally {
      setLoading(false);
    }
  }, [endpoint, label]);

  usePolling(load);

  const run = payload.run;
  const heartbeat = payload.latest_heartbeat ?? payload.heartbeat;
  const steps = payload.v2_steps ?? [];
  const events = payload.events ?? [];
  const relatedJobs = payload.related_jobs ?? [];
  const title = `${label} Run ${runId.slice(0, 8)}`;

  return (
    <PageFrame bootstrap={bootstrap} title={title} section={section} path={`/react/${kind}/runs/${runId}`}>
      {loading ? (
        <div className="load-strip" role="status" aria-live="polite">
          <span>Loading {label} run</span>
          <div className="load-strip__track" role="progressbar" aria-label={`${label} run loading`}><span /></div>
        </div>
      ) : null}
      {error ? <p className="notice notice--bad" role="alert">{error}</p> : null}

      <section className="metric-strip" aria-label={`${label} run metrics`}>
        <Metric label="State" value={textValue(field(run, "state"), "-")} />
        <Metric label="V2 steps" value={String(steps.length)} />
        <Metric label="Events" value={String(events.length)} />
        <Metric label="Related jobs" value={String(relatedJobs.length)} />
      </section>

      <section className="utility-settings-grid utility-settings-grid--wide">
        <Panel title="Run">
          <dl className="utility-definition-grid">
            <div><dt>Requested VM</dt><dd>{textValue(field(run, "requested_vm_name") ?? field(run, "vm_name"))}</dd></div>
            <div><dt>Computer</dt><dd>{textValue(field(run, "expected_computer_name") ?? field(run, "computer_name"))}</dd></div>
            <div><dt>VMID</dt><dd>{textValue(field(run, "vmid") ?? field(run, "pve_vmid"))}</dd></div>
            <div><dt>State</dt><dd><Badge status={textValue(field(run, "state"))} /></dd></div>
            <div><dt>Started</dt><dd>{formatShortDateTime(textValue(field(run, "created_at") ?? field(run, "started_at")))}</dd></div>
            <div><dt>Artifact</dt><dd><code>{textValue(field(payload.artifact, "build_sha") ?? field(run, "artifact_id"))}</code></dd></div>
          </dl>
        </Panel>
        <Panel title={kind === "cloudosd" ? "Heartbeat And Identity" : "Server Readiness"}>
          <dl className="utility-definition-grid">
            <div><dt>Heartbeat</dt><dd>{formatShortDateTime(textValue(field(heartbeat, "received_at") ?? field(payload.readiness, "heartbeat_at")))}</dd></div>
            <div><dt>Agent</dt><dd>{textValue(field(payload.readiness, "agent_status") ?? field(heartbeat, "agent_version"))}</dd></div>
            <div><dt>QGA</dt><dd>{textValue(field(payload.readiness, "qga_status"))}</dd></div>
            <div><dt>Role</dt><dd>{textValue(field(payload.readiness, "server_role_status") ?? field(run, "server_role"))}</dd></div>
            <div><dt>V2 status</dt><dd>{textValue(field(payload.v2_operator_status, "label") ?? field(payload.v2_completion, "state"))}</dd></div>
          </dl>
        </Panel>
      </section>

      <Panel title={`${label} v2 Plan`}>
        <div className="table-wrap">
          <table className="jobs-table utility-table" aria-label={`${label} v2 steps`}>
            <thead><tr><th>Ordinal</th><th>Phase</th><th>Step</th><th>Kind</th><th>State</th><th>Attempt</th><th>Error</th></tr></thead>
            <tbody>
              {steps.map((step, index) => (
                <tr key={textValue(step.id, String(index))}>
                  <td>{textValue(step.ordinal, String(index))}</td>
                  <td>{textValue(step.phase)}</td>
                  <td>{textValue(step.name)}</td>
                  <td><code>{textValue(step.kind)}</code></td>
                  <td><Badge status={step.state} /></td>
                  <td>{textValue(step.attempt)}</td>
                  <td>{textValue(step.last_error, "-")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Events">
        <div className="table-wrap">
          <table className="jobs-table utility-table" aria-label={`${label} events`}>
            <thead><tr><th>Time</th><th>Phase</th><th>Type</th><th>Message</th></tr></thead>
            <tbody>
              {events.map((event, index) => (
                <tr key={textValue(event.id, String(index))}>
                  <td>{formatShortDateTime(event.created_at)}</td>
                  <td>{textValue(event.phase)}</td>
                  <td><code>{textValue(event.event_type ?? event.type)}</code></td>
                  <td>{textValue(event.message ?? event.detail, "-")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      {relatedJobs.length ? (
        <Panel title="Related Jobs">
          <div className="table-wrap">
            <table className="jobs-table utility-table" aria-label="Related jobs">
              <thead><tr><th>Job</th><th>Playbook</th><th>Status</th></tr></thead>
              <tbody>{relatedJobs.map((job) => <tr key={textValue(job.id)}><td><a href={`/react/jobs/${textValue(job.id)}`}>{textValue(job.id)}</a></td><td>{textValue(job.playbook)}</td><td><Badge status={job.status} /></td></tr>)}</tbody>
            </table>
          </div>
        </Panel>
      ) : null}
    </PageFrame>
  );
}
