import { useCallback, useEffect, useMemo, useState } from "react";

import { fetchJson } from "./apiClient";
import type {
  AppBootstrap,
  FleetSummary,
  JobTableRow,
  JobsLivePayload,
  RecentJob,
  RecentJobsResponse,
  RunningJobsResponse,
  ServiceHealth,
  ServicesResponse
} from "./contracts";
import { connectJobsLive } from "./liveSocket";
import { migratedRoutes } from "./routes";

interface AppProps {
  readonly bootstrap: AppBootstrap;
}

const emptyRunning: RunningJobsResponse = {
  running: [],
  running_count: 0,
  queued_count: 0
};

const emptyServices: ServicesResponse = {
  services: [],
  available: true,
  error: ""
};

const emptyFleet: FleetSummary = {
  total: 0
};

function fallback(value: unknown): string {
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

function percent(value: unknown): string {
  return typeof value === "number" ? `${String(value)}%` : "-";
}

function statusClass(status: string | null | undefined): string {
  const normalized = (status || "unknown").toLowerCase();
  if (["complete", "healthy", "ok", "ready"].includes(normalized)) {
    return "status status--good";
  }
  if (["running", "pending", "queued"].includes(normalized)) {
    return "status status--active";
  }
  if (["failed", "error", "stale", "orphaned"].includes(normalized)) {
    return "status status--bad";
  }
  return "status";
}

function statusLabel(status: string | null | undefined, paused = false): string {
  if (paused) {
    return "paused";
  }
  return fallback(status).toLowerCase();
}

function serviceName(service: ServiceHealth): string {
  return service.service || service.service_id || service.service_type || "service";
}

function jobTarget(job: JobTableRow): string {
  const args = job.args ?? {};
  const candidates = [
    args.hostname_pattern,
    args.vm_name,
    args.template_name,
    args.serial,
    args.sequence_name
  ];
  const found = candidates.find((candidate) => typeof candidate === "string" && candidate.trim());
  return typeof found === "string" ? found : "-";
}

function ShellIndex({ bootstrap }: AppProps) {
  const buildLabel = bootstrap.buildSha ? `Build ${bootstrap.buildSha}` : "Build unknown";

  return (
    <main className="shell">
      <section className="shell__hero" aria-labelledby="shell-title">
        <div>
          <p className="shell__eyebrow">React shell foundation</p>
          <h1 id="shell-title">Proxmox VE Autopilot</h1>
          <p className="shell__copy">
            The authenticated React runtime is mounted. Operational pages remain on the existing
            Jinja console until each route passes parity checks.
          </p>
        </div>
        <div className="shell__status" aria-label="Build status">
          <span>{buildLabel}</span>
          {bootstrap.buildTime ? <time dateTime={bootstrap.buildTime}>{bootstrap.buildTime}</time> : null}
        </div>
      </section>

      <section className="shell__panel" aria-labelledby="routes-title">
        <h2 id="routes-title">Migrated routes</h2>
        <ul>
          {migratedRoutes.map((route) => (
            <li key={route.path}>
              <a href={route.path}>{route.label}</a>
              <span>{route.phase}</span>
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}

interface PageFrameProps {
  readonly title: string;
  readonly eyebrow: string;
  readonly children: React.ReactNode;
  readonly socketState?: string;
}

function PageFrame({ title, eyebrow, children, socketState }: PageFrameProps) {
  return (
    <main className="console">
      <nav className="console__nav" aria-label="React routes">
        <a href="/react/dashboard">Dashboard</a>
        <a href="/react/jobs">Jobs</a>
        <a href="/">Jinja console</a>
      </nav>
      <header className="console__header">
        <div>
          <p className="console__eyebrow">{eyebrow}</p>
          <h1>{title}</h1>
        </div>
        {socketState ? <span className="socket-state">Live {socketState}</span> : null}
      </header>
      {children}
    </main>
  );
}

function DashboardPage() {
  const [services, setServices] = useState<ServicesResponse>(emptyServices);
  const [running, setRunning] = useState<RunningJobsResponse>(emptyRunning);
  const [recent, setRecent] = useState<readonly RecentJob[]>([]);
  const [fleet, setFleet] = useState<FleetSummary>(emptyFleet);
  const [error, setError] = useState<string>("");
  const [socketState, setSocketState] = useState("closed");

  const load = useCallback(async () => {
    try {
      const [servicesData, runningData, recentData, fleetData] = await Promise.all([
        fetchJson<ServicesResponse>("/api/services"),
        fetchJson<RunningJobsResponse>("/api/jobs/running"),
        fetchJson<RecentJobsResponse>("/api/jobs/recent?limit=5"),
        fetchJson<FleetSummary>("/api/fleet/summary")
      ]);
      setServices(servicesData);
      setRunning(runningData);
      setRecent(recentData.jobs);
      setFleet(fleetData);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load dashboard data");
    }
  }, []);

  useEffect(() => {
    const initialTimer = window.setTimeout(() => {
      void load();
    }, 0);
    const timer = window.setInterval(() => {
      void load();
    }, 15000);
    return () => {
      window.clearTimeout(initialTimer);
      window.clearInterval(timer);
    };
  }, [load]);

  useEffect(() => {
    return connectJobsLive({
      onPayload: (payload) => {
        if (payload.running) {
          setRunning(payload.running);
        }
        if (payload.recent) {
          setRecent(payload.recent.jobs);
        }
      },
      onState: setSocketState
    });
  }, []);

  return (
    <PageFrame title="Dashboard" eyebrow="Read-only React slice" socketState={socketState}>
      {error ? <p className="notice" role="status">{error}</p> : null}

      <section className="metric-strip" aria-label="Job status">
        <div>
          <span>Running</span>
          <strong>{running.running_count}</strong>
        </div>
        <div>
          <span>Queued</span>
          <strong>{running.queued_count}</strong>
        </div>
        <div>
          <span>Fleet</span>
          <strong>{fleet.total}</strong>
        </div>
      </section>

      <section className="dashboard-grid">
        <Panel title="Service health">
          {services.services.length ? (
            <ul className="row-list">
              {services.services.map((service) => (
                <li key={`${service.service_type || "svc"}-${serviceName(service)}`}>
                  <span className={statusClass(service.status)}>{statusLabel(service.status)}</span>
                  <strong>{serviceName(service)}</strong>
                  <span>{fallback(service.detail || service.message)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="empty">{services.available ? "No service heartbeat rows yet." : "Service health unavailable."}</p>
          )}
        </Panel>

        <Panel title="Live jobs">
          {running.running.length ? (
            <ul className="row-list">
              {running.running.map((job) => (
                <li key={job.id}>
                  <span className={statusClass(job.paused ? "paused" : "running")}>
                    {statusLabel("running", job.paused)}
                  </span>
                  <strong>{fallback(job.target)}</strong>
                  <span>{fallback(job.playbook)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="empty">No running jobs.</p>
          )}
        </Panel>

        <Panel title="Fleet summary">
          <dl className="fleet-grid">
            <div>
              <dt>Total</dt>
              <dd>{fleet.total}</dd>
            </div>
            <div>
              <dt>AD joined</dt>
              <dd>{percent(fleet.ad_joined_pct)}</dd>
            </div>
            <div>
              <dt>Autopilot</dt>
              <dd>{percent(fleet.autopilot_pct)}</dd>
            </div>
            <div>
              <dt>Intune</dt>
              <dd>{percent(fleet.intune_pct)}</dd>
            </div>
            <div>
              <dt>MDE</dt>
              <dd>{percent(fleet.mde_pct)}</dd>
            </div>
          </dl>
        </Panel>

        <Panel title="Launchpad">
          <div className="link-grid">
            <a href="/provision">Provision</a>
            <a href="/template">Template</a>
            <a href="/hashes">Hashes</a>
          </div>
        </Panel>
      </section>

      <Panel title="Recent jobs">
        <JobsTable jobs={recent.map(recentToTableRow)} compact />
      </Panel>
    </PageFrame>
  );
}

function recentToTableRow(job: RecentJob): JobTableRow {
  return {
    id: job.id,
    ...(job.playbook !== undefined ? { playbook: job.playbook } : {}),
    ...(job.status !== undefined ? { status: job.status } : {}),
    ...(job.started !== undefined ? { started: job.started } : {}),
    ...(job.ended !== undefined ? { ended: job.ended } : {}),
    ...(job.duration !== undefined ? { duration: job.duration } : {}),
    args: { target: job.target || "" },
    paused: false
  };
}

function JobsPage() {
  const [jobs, setJobs] = useState<readonly JobTableRow[]>([]);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState("");
  const [socketState, setSocketState] = useState("closed");

  const load = useCallback(async () => {
    try {
      setJobs(await fetchJson<readonly JobTableRow[]>("/api/jobs"));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load jobs");
    }
  }, []);

  useEffect(() => {
    const initialTimer = window.setTimeout(() => {
      void load();
    }, 0);
    const timer = window.setInterval(() => {
      void load();
    }, 15000);
    return () => {
      window.clearTimeout(initialTimer);
      window.clearInterval(timer);
    };
  }, [load]);

  useEffect(() => {
    return connectJobsLive({
      onPayload: (payload: JobsLivePayload) => {
        if (payload.table) {
          setJobs(payload.table.jobs);
        }
      },
      onState: setSocketState
    });
  }, []);

  const filteredJobs = useMemo(() => {
    const query = filter.trim().toLowerCase();
    if (!query) {
      return jobs;
    }
    return jobs.filter((job) =>
      [job.id, job.playbook, job.status, jobTarget(job)].some((value) =>
        (value ?? "").toLowerCase().includes(query)
      )
    );
  }, [filter, jobs]);

  const counts = useMemo(
    () => ({
      total: jobs.length,
      running: jobs.filter((job) => job.status === "running").length,
      queued: jobs.filter((job) => job.status === "pending").length,
      failed: jobs.filter((job) => job.status === "failed").length
    }),
    [jobs]
  );

  return (
    <PageFrame title="Jobs" eyebrow="Read-only React slice" socketState={socketState}>
      {error ? <p className="notice" role="status">{error}</p> : null}
      <section className="metric-strip" aria-label="Jobs metrics">
        <div>
          <span>Total</span>
          <strong>{counts.total}</strong>
        </div>
        <div>
          <span>Running</span>
          <strong>{counts.running}</strong>
        </div>
        <div>
          <span>Queued</span>
          <strong>{counts.queued}</strong>
        </div>
        <div>
          <span>Failed</span>
          <strong>{counts.failed}</strong>
        </div>
      </section>
      <label className="filter">
        <span>Filter jobs</span>
        <input
          aria-label="Filter jobs"
          value={filter}
          onChange={(event) => {
            setFilter(event.target.value);
          }}
          placeholder="Job id, playbook, target, status"
        />
      </label>
      <Panel title="Jobs table">
        <JobsTable jobs={filteredJobs} />
      </Panel>
    </PageFrame>
  );
}

interface PanelProps {
  readonly title: string;
  readonly children: React.ReactNode;
}

function Panel({ title, children }: PanelProps) {
  return (
    <section className="panel" aria-labelledby={`${title.toLowerCase().replaceAll(" ", "-")}-title`}>
      <h2 id={`${title.toLowerCase().replaceAll(" ", "-")}-title`}>{title}</h2>
      {children}
    </section>
  );
}

interface JobsTableProps {
  readonly jobs: readonly JobTableRow[];
  readonly compact?: boolean;
}

function JobsTable({ jobs, compact = false }: JobsTableProps) {
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
              <td>{fallback(job.playbook)}</td>
              <td>{job.args?.target ? fallback(job.args.target) : jobTarget(job)}</td>
              <td>{fallback(job.duration)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function App({ bootstrap }: AppProps) {
  const path = window.location.pathname;
  if (path === "/react/dashboard") {
    return <DashboardPage />;
  }
  if (path === "/react/jobs") {
    return <JobsPage />;
  }
  return <ShellIndex bootstrap={bootstrap} />;
}
