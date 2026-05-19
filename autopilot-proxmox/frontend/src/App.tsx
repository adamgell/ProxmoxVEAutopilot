import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { fetchJson } from "./apiClient";
import type {
  AppBootstrap,
  FleetSummary,
  JobTableRow,
  JobsLivePayload,
  OperatorPath,
  OperatorSignal,
  RecentJob,
  RecentJobsResponse,
  RunningJobsResponse,
  ServicesResponse,
  SignalsHubResponse
} from "./contracts";
import { connectJobsLive } from "./liveSocket";
import { migratedRoutes, operatorNavGroups, reactRouteForPath } from "./routes";
import {
  fallbackText,
  formatPercent,
  jobTarget,
  buildSignalMetrics,
  rankedSignalPaths,
  serviceName,
  statusClass,
  statusLabel,
  summarizeJobs
} from "./viewModels";

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

const emptySignalsHub: SignalsHubResponse = {
  generated_at: "",
  build: {},
  source_health: { runtime_available: true },
  metrics: [],
  signals: [],
  operator_paths: []
};

function currentPageLabel(path: string): string {
  return reactRouteForPath(path)?.label ?? "Shell";
}

function OperatorShell({
  bootstrap,
  path,
  socketState,
  children
}: {
  readonly bootstrap: AppBootstrap;
  readonly path: string;
  readonly socketState?: string | undefined;
  readonly children: ReactNode;
}) {
  const buildLabel = bootstrap.buildSha ? `Build ${bootstrap.buildSha}` : "Build unknown";
  const pageLabel = currentPageLabel(path);

  return (
    <div className="workspace">
      <aside className="workspace__rail">
        <a className="workspace__brand" href="/react/dashboard" aria-label="Proxmox VE Autopilot dashboard">
          <span>Autopilot</span>
          <small>Operator</small>
        </a>
        <nav className="workspace__nav" aria-label="Operator workspace">
          {operatorNavGroups.map((group) => (
            <section key={group.label} aria-labelledby={`nav-${group.label.toLowerCase()}`}>
              <h2 id={`nav-${group.label.toLowerCase()}`}>{group.label}</h2>
              {group.items.map((item) => (
                <a
                  key={item.path}
                  className={[
                    item.path === path ? "is-current" : "",
                    item.legacy ? "is-legacy" : ""
                  ].filter(Boolean).join(" ")}
                  href={item.path}
                  aria-label={item.legacy ? `${item.label} legacy page` : item.label}
                  aria-current={item.path === path ? "page" : undefined}
                >
                  <span>{item.label}</span>
                  {item.legacy ? <small>Jinja</small> : null}
                </a>
              ))}
            </section>
          ))}
        </nav>
      </aside>

      <div className="workspace__main">
        <header className="workspace__topbar">
          <div>
            <span className="workspace__kicker">React operator console</span>
            <strong>{pageLabel}</strong>
          </div>
          <div className="workspace__status" aria-label="Runtime status">
            {socketState ? <span className={`socket-state socket-state--${socketState}`}>Live {socketState}</span> : null}
            <span>{buildLabel}</span>
            {bootstrap.buildTime ? <time dateTime={bootstrap.buildTime}>{bootstrap.buildTime}</time> : null}
          </div>
        </header>
        <main className="workspace__content">{children}</main>
      </div>
    </div>
  );
}

function ShellIndex({ bootstrap }: AppProps) {
  return (
    <OperatorShell bootstrap={bootstrap} path="/react-shell">
      <section className="page-head" aria-labelledby="shell-title">
        <div>
          <p>Observe</p>
          <h1 id="shell-title">Proxmox VE Autopilot</h1>
        </div>
        <a className="action-link" href="/">Jinja console</a>
      </section>

      <section className="metric-strip" aria-label="Migrated routes">
        {migratedRoutes.map((route) => (
          <a key={route.path} href={route.path} aria-label={`Open ${route.label}`}>
            <span>{route.group}</span>
            <strong>{route.label}</strong>
          </a>
        ))}
      </section>

      <section className="section-grid">
        {operatorNavGroups.map((group) => (
          <Panel key={group.label} title={group.label}>
            <div className="link-stack">
              {group.items.map((item) => (
                <a
                  key={item.path}
                  href={item.path}
                  className={item.legacy ? "legacy-link" : undefined}
                  aria-label={item.legacy ? item.label : `${item.label} route`}
                >
                  <span>{item.label}</span>
                  {item.legacy ? <small>existing page</small> : <small>{item.phase}</small>}
                </a>
              ))}
            </div>
          </Panel>
        ))}
      </section>
    </OperatorShell>
  );
}

interface PageFrameProps {
  readonly bootstrap: AppBootstrap;
  readonly title: string;
  readonly section: string;
  readonly path: string;
  readonly children: ReactNode;
  readonly socketState?: string;
  readonly action?: ReactNode;
}

function PageFrame({ bootstrap, title, section, path, children, socketState, action }: PageFrameProps) {
  return (
    <OperatorShell bootstrap={bootstrap} path={path} socketState={socketState}>
      <header className="page-head">
        <div>
          <p>{section}</p>
          <h1>{title}</h1>
        </div>
        {action}
      </header>
      {children}
    </OperatorShell>
  );
}

function DashboardPage({ bootstrap }: AppProps) {
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

  usePolling(load);

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
    <PageFrame
      bootstrap={bootstrap}
      title="Dashboard"
      section="Observe"
      path="/react/dashboard"
      socketState={socketState}
      action={<a className="action-link" href="/monitoring">Legacy monitoring</a>}
    >
      {error ? <p className="notice" role="status">{error}</p> : null}

      <section className="metric-strip" aria-label="Job status">
        <Metric label="Running" value={String(running.running_count)} tone={running.running_count > 0 ? "active" : "neutral"} />
        <Metric label="Queued" value={String(running.queued_count)} tone={running.queued_count > 0 ? "active" : "neutral"} />
        <Metric label="Fleet" value={String(fleet.total)} tone={fleet.total > 0 ? "good" : "neutral"} />
        <Metric label="Readiness" value={formatPercent(fleet.ad_joined_pct)} tone="good" />
      </section>

      <section className="section-grid section-grid--wide">
        <Panel title="Service health">
          {services.services.length ? (
            <ul className="row-list">
              {services.services.map((service) => (
                <li key={`${service.service_type || "svc"}-${serviceName(service)}`}>
                  <span className={statusClass(service.status)}>{statusLabel(service.status)}</span>
                  <strong>{serviceName(service)}</strong>
                  <span>{fallbackText(service.detail || service.message)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="empty">{services.available ? "No service heartbeats." : "Service health unavailable."}</p>
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
                  <strong>{fallbackText(job.target)}</strong>
                  <span>{fallbackText(job.playbook)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="empty">No running jobs.</p>
          )}
        </Panel>

        <Panel title="Fleet summary">
          <dl className="fleet-grid">
            <MetricTerm label="Total" value={String(fleet.total)} />
            <MetricTerm label="AD joined" value={formatPercent(fleet.ad_joined_pct)} />
            <MetricTerm label="Autopilot" value={formatPercent(fleet.autopilot_pct)} />
            <MetricTerm label="Intune" value={formatPercent(fleet.intune_pct)} />
            <MetricTerm label="MDE" value={formatPercent(fleet.mde_pct)} />
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

function JobsPage({ bootstrap }: AppProps) {
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

  usePolling(load);

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

  const counts = useMemo(() => summarizeJobs(jobs), [jobs]);

  return (
    <PageFrame bootstrap={bootstrap} title="Jobs" section="Observe" path="/react/jobs" socketState={socketState}>
      {error ? <p className="notice" role="status">{error}</p> : null}
      <section className="metric-strip" aria-label="Jobs metrics">
        <Metric label="Total" value={String(counts.total)} />
        <Metric label="Running" value={String(counts.running)} tone={counts.running > 0 ? "active" : "neutral"} />
        <Metric label="Queued" value={String(counts.queued)} tone={counts.queued > 0 ? "active" : "neutral"} />
        <Metric label="Failed" value={String(counts.failed)} tone={counts.failed > 0 ? "bad" : "good"} />
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

function signalToneClass(tone: string | undefined): string {
  return tone && tone !== "neutral" ? `status status--${tone}` : "status";
}

function MonitoringPage({ bootstrap }: AppProps) {
  const [hub, setHub] = useState<SignalsHubResponse>(emptySignalsHub);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const signals = await fetchJson<SignalsHubResponse>("/api/monitoring/signals");
      setHub(signals);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load signals");
    }
  }, []);

  usePolling(load);

  const metrics = hub.metrics.length ? hub.metrics : buildSignalMetrics(hub);
  const rankedPaths = rankedSignalPaths(hub.operator_paths);
  const selectedSignal = hub.signals[0];
  const selectedPath = rankedPaths[0];

  return (
    <PageFrame
      bootstrap={bootstrap}
      title="Signals Hub"
      section="Observe"
      path="/react/monitoring"
      action={
        <span className="action-cluster">
          <a className="action-link" href="/react/jobs">Jobs</a>
          <a className="action-link" href="/monitoring/settings">Monitoring settings</a>
        </span>
      }
    >
      {error ? <p className="notice" role="status">{error}</p> : null}
      <section className="metric-strip metric-strip--signals" aria-label="Signals Hub metrics">
        {metrics.map((item) => (
          <Metric key={item.label} label={item.label} value={item.value} tone={item.tone} />
        ))}
      </section>

      <section className="signals-layout">
        <Panel title="Signal families">
          {hub.signals.length ? (
            <ul className="signal-list">
              {hub.signals.map((signal: OperatorSignal) => (
                <li key={signal.id}>
                  <span className={signalToneClass(signal.tone)}>{statusLabel(signal.status)}</span>
                  <div>
                    <strong>{signal.label}</strong>
                    <p>{signal.summary}</p>
                    <small>{signal.source || signal.family}</small>
                  </div>
                  <span>{fallbackText(signal.count)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="empty">No signals collected.</p>
          )}
        </Panel>

        <Panel title="Ranked operator paths">
          {rankedPaths.length ? (
            <ul className="path-list">
              {rankedPaths.map((path: OperatorPath) => (
                <li key={path.id}>
                  <span className={signalToneClass(path.tone)}>{statusLabel(path.status)}</span>
                  <div>
                    <strong>{path.label}</strong>
                    <p>{path.summary}</p>
                    <small>{path.source || "Signals Hub"}</small>
                  </div>
                  <a href={path.href}>{path.action_label}</a>
                </li>
              ))}
            </ul>
          ) : (
            <p className="empty">No operator paths ranked.</p>
          )}
        </Panel>

        <Panel title="Selected signal">
          <div className="signal-detail">
            <dl className="fleet-grid fleet-grid--four">
              <MetricTerm label="Runtime" value={hub.source_health.runtime_available ? "up" : "down"} />
              <MetricTerm label="Setup" value={fallbackText(hub.source_health.setup_health)} />
              <MetricTerm label="Keytab" value={fallbackText(hub.source_health.keytab_status)} />
              <MetricTerm label="Generated" value={fallbackText(hub.generated_at)} />
            </dl>
            {selectedSignal ? (
              <div className="detail-callout">
                <span className={signalToneClass(selectedSignal.tone)}>{statusLabel(selectedSignal.status)}</span>
                <div>
                  <strong>{selectedSignal.label}</strong>
                  <p>{selectedSignal.summary}</p>
                </div>
              </div>
            ) : null}
            {selectedPath ? (
              <div className="detail-callout">
                <span className={signalToneClass(selectedPath.tone)}>{String(selectedPath.priority)}</span>
                <div>
                  <strong>{selectedPath.label}</strong>
                  <p>{selectedPath.summary}</p>
                </div>
              </div>
            ) : null}
          </div>
        </Panel>
      </section>
    </PageFrame>
  );
}

function usePolling(load: () => Promise<void>) {
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
}

function Metric({
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

function MetricTerm({ label, value }: { readonly label: string; readonly value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

interface PanelProps {
  readonly title: string;
  readonly children: ReactNode;
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

export function App({ bootstrap }: AppProps) {
  const path = window.location.pathname;
  if (path === "/react/dashboard") {
    return <DashboardPage bootstrap={bootstrap} />;
  }
  if (path === "/react/jobs") {
    return <JobsPage bootstrap={bootstrap} />;
  }
  if (path === "/react/monitoring") {
    return <MonitoringPage bootstrap={bootstrap} />;
  }
  return <ShellIndex bootstrap={bootstrap} />;
}
