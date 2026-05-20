import type { ReactNode } from "react";

import type { JobTableRow } from "../contracts";
import { fallbackText, jobTarget, statusClass, statusLabel } from "../viewModels";

export function Metric({
  label,
  value,
  tone = "neutral"
}: {
  readonly label: string;
  readonly value: string;
  readonly tone?: string | undefined;
}) {
  return (
    <div className={`metric metric--${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function MetricTerm({ label, value }: { readonly label: string; readonly value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

interface PanelProps {
  readonly title: string;
  readonly action?: ReactNode;
  readonly children: ReactNode;
}

export function Panel({ title, action, children }: PanelProps) {
  const titleId = `${title.toLowerCase().replaceAll(" ", "-")}-title`;
  return (
    <section className="panel" aria-labelledby={titleId}>
      <div className="panel__header">
        <h2 id={titleId}>{title}</h2>
        {action ? <div className="panel__action">{action}</div> : null}
      </div>
      {children}
    </section>
  );
}

interface JobsTableProps {
  readonly jobs: readonly JobTableRow[];
  readonly compact?: boolean;
}

export function JobsTable({ jobs, compact = false }: JobsTableProps) {
  if (!jobs.length) {
    return <p className="empty">No jobs found.</p>;
  }
  return (
    <div className="table-wrap">
      <table className={compact ? "jobs-table jobs-table--compact" : "jobs-table"}>
        <thead>
          <tr>
            <th scope="col">Job</th>
            <th scope="col">Status</th>
            <th scope="col">Playbook</th>
            <th scope="col">Target</th>
            <th scope="col">Duration</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.id}>
              <td>
                <a href={`/jobs/${encodeURIComponent(job.id)}`}>{job.id}</a>
              </td>
              <td>
                <span className={statusClass(job.paused ? "paused" : job.status)}>
                  {statusLabel(job.status, job.paused)}
                </span>
              </td>
              <td>{fallbackText(job.playbook)}</td>
              <td>{job.args?.target ? fallbackText(job.args.target) : jobTarget(job)}</td>
              <td>{fallbackText(job.duration)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
