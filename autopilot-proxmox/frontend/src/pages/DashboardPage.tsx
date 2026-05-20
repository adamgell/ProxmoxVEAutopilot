import { useCallback, useEffect, useState } from "react";

import { fetchJson } from "../apiClient";
import { PageFrame } from "../components/Shell";
import { JobsTable, Metric, MetricTerm, Panel } from "../components/ui";
import type {
  AppBootstrap,
  FleetSummary,
  JobTableRow,
  RecentJob,
  RecentJobsResponse,
  RunningJobsResponse,
  ServicesResponse
} from "../contracts";
import { usePolling } from "../hooks/usePolling";
import { connectJobsLive } from "../liveSocket";
import { fallbackText, formatPercent, serviceName, statusClass, statusLabel } from "../viewModels";

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

export function DashboardPage({ bootstrap }: { readonly bootstrap: AppBootstrap }) {
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
