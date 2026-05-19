import type { JobTableRow, MonitoringOverview } from "./contracts";

export type StatusTone = "good" | "active" | "bad" | "neutral";

export interface JobsSummary {
  readonly total: number;
  readonly running: number;
  readonly queued: number;
  readonly failed: number;
  readonly complete: number;
  readonly paused: number;
}

export interface MetricItem {
  readonly label: string;
  readonly value: string;
  readonly tone?: StatusTone;
}

export function fallbackText(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  switch (typeof value) {
    case "string":
      return value;
    case "number":
    case "boolean":
    case "bigint":
      return value.toString();
    default:
      return "-";
  }
}

export function formatPercent(value: unknown): string {
  return typeof value === "number" ? `${String(value)}%` : "-";
}

export function statusTone(status: string | null | undefined): StatusTone {
  const normalized = (status || "unknown").toLowerCase();
  if (["complete", "completed", "done", "healthy", "ok", "ready", "running"].includes(normalized)) {
    return normalized === "running" ? "active" : "good";
  }
  if (["pending", "queued", "active", "learning"].includes(normalized)) {
    return "active";
  }
  if (["failed", "error", "stale", "orphaned", "stuck", "regressed", "slow"].includes(normalized)) {
    return "bad";
  }
  return "neutral";
}

export function statusClass(status: string | null | undefined): string {
  const tone = statusTone(status);
  return tone === "neutral" ? "status" : `status status--${tone}`;
}

export function statusLabel(status: string | null | undefined, paused = false): string {
  if (paused) {
    return "paused";
  }
  return fallbackText(status).toLowerCase();
}

export function serviceName(service: {
  readonly service?: string;
  readonly service_id?: string;
  readonly service_type?: string;
}): string {
  return service.service || service.service_id || service.service_type || "service";
}

export function jobTarget(job: JobTableRow): string {
  const args = job.args ?? {};
  const candidates = [
    args.hostname_pattern,
    args.vm_name,
    args.template_name,
    args.serial,
    args.sequence_name,
    args.target
  ];
  const found = candidates.find((candidate) => typeof candidate === "string" && candidate.trim());
  return typeof found === "string" ? found : "-";
}

export function summarizeJobs(jobs: readonly JobTableRow[]): JobsSummary {
  return jobs.reduce<JobsSummary>(
    (summary, job) => {
      const status = (job.status || "").toLowerCase();
      return {
        total: summary.total + 1,
        running: summary.running + (status === "running" && !job.paused ? 1 : 0),
        queued: summary.queued + (status === "pending" || status === "queued" ? 1 : 0),
        failed: summary.failed + (status === "failed" || status === "orphaned" ? 1 : 0),
        complete: summary.complete + (status === "complete" || status === "completed" ? 1 : 0),
        paused: summary.paused + (job.paused ? 1 : 0)
      };
    },
    { total: 0, running: 0, queued: 0, failed: 0, complete: 0, paused: 0 }
  );
}

export function monitoringStrip(overview: MonitoringOverview): readonly MetricItem[] {
  const runtimeValue = overview.runtime.available ? String(overview.runtime.containers.length) : "-";
  const activeDeployments = overview.deployments.running ?? overview.deployments.active ?? 0;
  const succeededDeployments = overview.deployments.succeeded ?? overview.deployments.completed ?? 0;
  const keytabStatus = overview.keytab.status;
  return [
    {
      label: "Runtime",
      value: runtimeValue,
      tone: overview.runtime.available ? "good" : "bad"
    },
    {
      label: "Deployments",
      value: String(overview.deployments.total),
      tone: activeDeployments > 0 ? "active" : succeededDeployments > 0 ? "good" : "neutral"
    },
    {
      label: "Failed",
      value: String(overview.deployments.failed),
      tone: overview.deployments.failed > 0 ? "bad" : "good"
    },
    {
      label: "Keytab",
      value: fallbackText(keytabStatus),
      tone: statusTone(keytabStatus)
    }
  ];
}
